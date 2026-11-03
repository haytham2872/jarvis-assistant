"""P3: the week planner's placement -- pure, deterministic, and the only place that does
calendar arithmetic.

**The model says what it wants; code does the exact part.** the owner says "organise my working
hours next week, add gym four times, not after 20:00, keep Wednesday evening free". The model
turns that into typed rules (`normalize_rules`); this module reads the week's real busy times,
places every block, and reports what it could not fit and why. Nothing here reads the clock
(the caller passes `now`), touches the calendar, the database or the network, and everything
is in the owner's zone (Europe/Paris in the tests): a week with a clock change (25 October 2026, 28 March 2027) is arithmetic on
real elapsed minutes, so a block never drifts an hour.

**Instants inside, local time at the edges.** Every datetime in here is an aware UTC instant.
Two aware datetimes that share one tzinfo (both local time) compare and subtract as *wall clocks*
and ignore the offset, so an hour-long block across a clock change came out two hours long,
and a time inside the repeated hour was never equal to itself in another zone. Instants have
neither problem. Wall-clock questions (which day, what hour) go through `local()`.

Guarantees, each pinned by a test (including a randomised one over hundreds of weeks):
  * a block never overlaps a busy event, a keep-free window, or another block;
  * a block that is not work keeps a buffer clear on both sides;
  * every block sits inside the rules' hours (never before/after, the kind's own limits);
  * a block is never placed in the past;
  * a request that cannot be met is reported with the reason, never dropped or bent;
  * the same inputs give the same plan, whatever order the busy events arrive in.

Placement is a greedy search over 15-minute slots. Counted blocks (gym, reading...) go first,
in the order given, and work fills the free time inside working hours afterwards. Where a block
goes is chosen by, in order: how far it is from the preferred part of the day, how close it
sits to a block of the same kind (they are spread over the week), and then the earliest day and
time, so ties are broken the same way every time.
"""
from __future__ import annotations
from backend import localtime as _localtime

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

LOCAL_TZ = _localtime.ZONE
STEP = 15                                    # minutes: every start and end is on this grid
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_LONG = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_MAX_SPECS = 8
_MAX_COUNT = 14
_MIN_MINUTES, _MAX_MINUTES = 15, 600
_MAX_BUFFER = 60
_KIND_RE = re.compile(r"^[a-z][a-z0-9 _-]{0,29}$")

_DEFAULT_NOT_BEFORE = "06:00"
_DEFAULT_NOT_AFTER = "22:00"
_DEFAULT_WORK_MIN = 45

# Where a block should start when the owner names a part of the day: (from, to, nominal start).
_PART_OF_DAY = {
    "morning": (dtime(6, 0), dtime(12, 0), dtime(7, 30)),
    "afternoon": (dtime(12, 0), dtime(17, 0), dtime(14, 0)),
    "evening": (dtime(17, 0), dtime(22, 0), dtime(18, 30)),
}

# Spreading same-kind blocks over the week: how much a neighbour on the next day (or the day
# after) counts against a candidate day, in the same units as minutes-off-preference.
_ADJACENT_COST = 120
_TWO_APART_COST = 40
_WEEKEND_COST = 150            # a soft dislike, only when he did not say which days are allowed

_TOP_KEYS = {"work", "blocks", "keep_free", "not_before", "not_after", "buffer"}
_WORK_KEYS = {"days", "start", "end", "min_minutes"}
_BLOCK_KEYS = {"kind", "count", "minutes", "prefer", "days", "not_before", "not_after", "max_per_day"}
_FREE_KEYS = {"days", "from", "to"}


class RulesError(ValueError):
    """The rules cannot be used; the message says exactly what to change."""


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def local(dt: datetime) -> datetime:
    """The instant as local time wall-clock time (for the day, the hour, what the owner reads)."""
    return dt.astimezone(LOCAL_TZ)


def at(day: date, hhmm: str | dtime) -> datetime:
    """`hhmm` local time wall-clock time on `day`, as a UTC instant.

    A time that does not exist (02:30 on the night the clocks go forward) moves to the real
    instant instead of being kept as a fiction; one that happens twice (02:30 when they go
    back) is the first of the two.
    """
    t = hhmm if isinstance(hhmm, dtime) else dtime.fromisoformat(hhmm)
    return datetime.combine(day, t).replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def plus(dt: datetime, minutes: int) -> datetime:
    """Real elapsed minutes: an hour-long block that spans a clock change is still an hour."""
    return dt.astimezone(timezone.utc) + timedelta(minutes=minutes)


def minutes_between(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _align_up(dt: datetime) -> datetime:
    """The next :00/:15/:30/:45 at or after `dt`. (local time is a whole number of hours from UTC,
    so the quarter hours are the same in both.)"""
    base = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if dt.second or dt.microsecond:
        base += timedelta(minutes=1)
    return base + timedelta(minutes=(STEP - base.minute % STEP) % STEP)


def day_label(day: date) -> str:
    """'Tue 2026-10-06': for listings (the weekday to read, the date to pass back)."""
    return f"{DAYS[day.weekday()].capitalize()} {day.isoformat()}"


def day_name(day: date) -> str:
    """'Tuesday': for a sentence the owner reads."""
    return _DAY_LONG[day.weekday()]


# ---------------------------------------------------------------------------
# Rules: what the model hands over, checked so that nothing is silently ignored
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    label: str = ""

    def __post_init__(self):
        object.__setattr__(self, "start", self.start.astimezone(timezone.utc))
        object.__setattr__(self, "end", self.end.astimezone(timezone.utc))

    def overlaps(self, other_start: datetime, other_end: datetime) -> bool:
        return self.start < other_end and other_start < self.end


@dataclass(frozen=True)
class BlockSpec:
    kind: str
    count: int
    minutes: int
    prefer: str                      # "" | morning | afternoon | evening
    days: tuple[int, ...]
    days_given: bool                 # he named the days: no soft dislike of the weekend
    not_before: str
    not_after: str
    max_per_day: int


@dataclass(frozen=True)
class Rules:
    work_days: tuple[int, ...]
    work_start: str
    work_end: str
    work_min: int
    blocks: tuple[BlockSpec, ...]
    keep_free: tuple[tuple[tuple[int, ...], str, str], ...]     # (days, from, to)
    not_before: str
    not_after: str
    buffer: int

    @property
    def has_work(self) -> bool:
        return bool(self.work_days)

    def as_dict(self) -> dict:
        """The normalized rules as plain data, for storage. `normalize_rules(as_dict())` gives
        the same rules back."""
        out: dict = {"not_before": self.not_before, "not_after": self.not_after,
                     "buffer": self.buffer}
        if self.has_work:
            out["work"] = {"days": _days_text(self.work_days), "start": self.work_start,
                           "end": self.work_end, "min_minutes": self.work_min}
        out["blocks"] = [{"kind": b.kind, "count": b.count, "minutes": b.minutes,
                          **({"prefer": b.prefer} if b.prefer else {}),
                          **({"days": _days_text(b.days)} if b.days_given else {}),
                          "not_before": b.not_before, "not_after": b.not_after,
                          "max_per_day": b.max_per_day} for b in self.blocks]
        out["keep_free"] = [{"days": _days_text(d), "from": a, "to": z} for d, a, z in self.keep_free]
        return out


def _days_text(days: Iterable[int]) -> str:
    return ",".join(DAYS[d] for d in sorted(set(days)))


_ALIASES = {"weekdays": (0, 1, 2, 3, 4), "weekday": (0, 1, 2, 3, 4), "workdays": (0, 1, 2, 3, 4),
            "weekend": (5, 6), "weekends": (5, 6),
            "all": tuple(range(7)), "daily": tuple(range(7)), "everyday": tuple(range(7)),
            "every day": tuple(range(7))}
_SHORT = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2,
          "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4,
          "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}


def parse_days(spec, *, default: tuple[int, ...]) -> tuple[tuple[int, ...], bool]:
    """('mon-fri' | 'mon,wed,fri' | 'weekdays' | ['mon', 'wed'] | None) -> (days, was_given).

    Raises RulesError on a word that is not a day, rather than guessing.
    """
    if spec is None or spec == "" or spec == []:
        return default, False
    tokens: list[str] = []
    for item in (spec if isinstance(spec, (list, tuple)) else [spec]):
        tokens += [t for t in re.split(r"[,;/&]+|\s+and\s+", str(item).strip().lower()) if t.strip()]
    days: set[int] = set()
    for tok in tokens:
        tok = tok.strip()
        if tok in _ALIASES:
            days |= set(_ALIASES[tok])
        elif "-" in tok and not tok.startswith("-"):
            a, _, b = tok.partition("-")
            if a.strip() not in _SHORT or b.strip() not in _SHORT:
                raise RulesError(f"'{tok}' is not a range of days (use for example mon-fri)")
            i, j = _SHORT[a.strip()], _SHORT[b.strip()]
            span = range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1))
            days |= set(span)
        elif tok in _SHORT:
            days.add(_SHORT[tok])
        else:
            raise RulesError(f"'{tok}' is not a day; use mon..sun, mon-fri, weekdays, weekend or all")
    return tuple(sorted(days)), True


def _clock(value, what: str, default: str | None = None) -> str:
    if value in (None, ""):
        if default is None:
            raise RulesError(f"{what} is missing (a time such as 09:00)")
        return default
    text = str(value).strip()
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", text)
    if not m:
        raise RulesError(f"{what} '{text}' is not a time; write it as HH:MM, for example 18:30")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        raise RulesError(f"{what} '{text}' is not a valid time of day")
    return f"{h:02d}:{mi:02d}"


def _int(value, what: str, lo: int, hi: int, default: int | None = None) -> int:
    if value in (None, ""):
        if default is None:
            raise RulesError(f"{what} is missing")
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise RulesError(f"{what} must be a whole number, not {value!r}") from None
    if not lo <= n <= hi:
        raise RulesError(f"{what} must be between {lo} and {hi}, not {n}")
    return n


def _unknown(keys: Iterable[str], allowed: set[str], where: str) -> None:
    extra = sorted(set(keys) - allowed)
    if extra:
        raise RulesError(f"{where} has unknown field(s) {', '.join(extra)}; allowed: "
                         f"{', '.join(sorted(allowed))}. A rule I cannot read is not applied, so "
                         "say it with these fields.")


def normalize_rules(raw) -> Rules:
    """Rules as the model sends them (a dict, or a JSON string of one) -> checked `Rules`.

    Strict on purpose: an unknown field is an error, because a rule that is silently ignored
    ("keep Wednesday evening free" typed under the wrong name) produces a plan that looks fine
    and breaks it. Raises RulesError with the exact thing to fix.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise RulesError("the rules are not valid JSON") from None
    if not isinstance(raw, dict):
        raise RulesError("the rules must be an object with work, blocks, keep_free, not_before, "
                         "not_after and buffer")
    _unknown(raw.keys(), _TOP_KEYS, "the rules")

    not_before = _clock(raw.get("not_before"), "not_before", _DEFAULT_NOT_BEFORE)
    not_after = _clock(raw.get("not_after"), "not_after", _DEFAULT_NOT_AFTER)
    if not_before >= not_after:
        raise RulesError(f"not_before {not_before} must be earlier than not_after {not_after}")
    buffer = _int(raw.get("buffer"), "buffer", 0, _MAX_BUFFER, 0)

    work_days: tuple[int, ...] = ()
    work_start = work_end = ""
    work_min = _DEFAULT_WORK_MIN
    work = raw.get("work")
    if work:
        if not isinstance(work, dict):
            raise RulesError("work must be an object such as "
                             '{"days": "mon-fri", "start": "09:00", "end": "17:00"}')
        _unknown(work.keys(), _WORK_KEYS, "work")
        work_days, _ = parse_days(work.get("days"), default=(0, 1, 2, 3, 4))
        work_start = _clock(work.get("start"), "work.start")
        work_end = _clock(work.get("end"), "work.end")
        if work_start >= work_end:
            raise RulesError(f"work.start {work_start} must be earlier than work.end {work_end}")
        work_min = _int(work.get("min_minutes"), "work.min_minutes", STEP, _MAX_MINUTES,
                        _DEFAULT_WORK_MIN)
        if not work_days:
            raise RulesError("work.days is empty")

    specs: list[BlockSpec] = []
    blocks = raw.get("blocks") or []
    if not isinstance(blocks, list):
        raise RulesError("blocks must be a list of objects such as "
                         '{"kind": "gym", "count": 4, "minutes": 60}')
    if len(blocks) > _MAX_SPECS:
        raise RulesError(f"at most {_MAX_SPECS} kinds of block per plan")
    for i, b in enumerate(blocks, 1):
        where = f"blocks[{i}]"
        if not isinstance(b, dict):
            raise RulesError(f"{where} must be an object")
        _unknown(b.keys(), _BLOCK_KEYS, where)
        kind = str(b.get("kind") or "").strip().lower()
        if not _KIND_RE.match(kind):
            raise RulesError(f"{where}.kind must be a short lowercase name such as gym or reading")
        if kind == "work":
            raise RulesError("work is set with the work field (days, start, end), not as a block")
        if any(s.kind == kind for s in specs):
            raise RulesError(f"the kind '{kind}' appears twice; give one entry with the total count")
        prefer = str(b.get("prefer") or "").strip().lower()
        if prefer and prefer not in _PART_OF_DAY:
            raise RulesError(f"{where}.prefer must be morning, afternoon or evening, not '{prefer}'")
        days, given = parse_days(b.get("days"), default=tuple(range(7)))
        lo = _clock(b.get("not_before"), f"{where}.not_before", not_before)
        hi = _clock(b.get("not_after"), f"{where}.not_after", not_after)
        lo, hi = max(lo, not_before), min(hi, not_after)          # a kind cannot widen the global hours
        if lo >= hi:
            raise RulesError(f"{where}: no hours are left between {lo} and {hi}")
        specs.append(BlockSpec(
            kind=kind, count=_int(b.get("count"), f"{where}.count", 1, _MAX_COUNT, 1),
            minutes=_int(b.get("minutes"), f"{where}.minutes", _MIN_MINUTES, _MAX_MINUTES),
            prefer=prefer, days=days, days_given=given, not_before=lo, not_after=hi,
            max_per_day=_int(b.get("max_per_day"), f"{where}.max_per_day", 1, 6, 1)))

    free: list[tuple[tuple[int, ...], str, str]] = []
    keep = raw.get("keep_free") or []
    if not isinstance(keep, list):
        raise RulesError("keep_free must be a list of objects such as "
                         '{"days": "wed", "from": "18:00", "to": "23:00"}')
    for i, w in enumerate(keep, 1):
        if not isinstance(w, dict):
            raise RulesError(f"keep_free[{i}] must be an object")
        _unknown(w.keys(), _FREE_KEYS, f"keep_free[{i}]")
        days, _ = parse_days(w.get("days"), default=tuple(range(7)))
        a, z = _clock(w.get("from"), f"keep_free[{i}].from"), _clock(w.get("to"), f"keep_free[{i}].to")
        if a >= z:
            raise RulesError(f"keep_free[{i}]: from {a} must be earlier than to {z}")
        free.append((days, a, z))

    if not work_days and not specs:
        raise RulesError("the rules ask for nothing: give work hours, at least one block, or both")
    return Rules(work_days=work_days, work_start=work_start, work_end=work_end, work_min=work_min,
                 blocks=tuple(specs), keep_free=tuple(free), not_before=not_before,
                 not_after=not_after, buffer=buffer)


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Block:
    kind: str
    start: datetime
    end: datetime

    def __post_init__(self):
        object.__setattr__(self, "start", self.start.astimezone(timezone.utc))
        object.__setattr__(self, "end", self.end.astimezone(timezone.utc))

    @property
    def key(self) -> str:
        """Stable within a plan; the calendar event id is derived from it. In UTC, because
        02:30 happens twice on the night the clocks go back."""
        return f"{self.kind.replace(' ', '-')}-{self.start:%Y%m%dT%H%MZ}"

    @property
    def minutes(self) -> int:
        return minutes_between(self.start, self.end)


@dataclass(frozen=True)
class Unplaced:
    kind: str
    wanted: int
    placed: int
    minutes: int
    reason: str


@dataclass(frozen=True)
class Placement:
    week_start: date
    blocks: tuple[Block, ...]
    unplaced: tuple[Unplaced, ...]


def _free_stretches(lo: datetime, hi: datetime, blockers: Sequence[tuple[datetime, datetime]]
                    ) -> list[tuple[datetime, datetime]]:
    """[lo, hi) minus the blockers, as the stretches that are left, in order."""
    out: list[tuple[datetime, datetime]] = []
    cursor = lo
    for s, e in sorted(blockers):
        if e <= cursor:
            continue
        if s >= hi:
            break
        if s > cursor:
            out.append((cursor, min(s, hi)))
        cursor = max(cursor, e)
        if cursor >= hi:
            break
    if cursor < hi:
        out.append((cursor, hi))
    return out


def _blockers(day: date, busy: Sequence[Interval], rules: Rules, placed: Sequence[Block],
              *, pad_busy: int, pad_blocks: int) -> list[tuple[datetime, datetime]]:
    """Everything that occupies `day`: events widened by `pad_busy` minutes on both sides,
    blocks already placed widened by `pad_blocks`, and the keep-free windows exactly."""
    day_lo, day_hi = at(day, "00:00"), at(day + timedelta(days=1), "00:00")
    out = []
    for iv in busy:
        if iv.start < day_hi and day_lo < iv.end:
            out.append((plus(iv.start, -pad_busy), plus(iv.end, pad_busy)))
    for b in placed:
        if b.start < day_hi and day_lo < b.end:
            out.append((plus(b.start, -pad_blocks), plus(b.end, pad_blocks)))
    for days, a, z in rules.keep_free:
        if day.weekday() in days:
            out.append((at(day, a), at(day, z)))
    return out


def _window(day: date, lo: str, hi: str, now: datetime | None) -> tuple[datetime, datetime] | None:
    start, end = at(day, lo), at(day, hi)
    if now is not None:
        start = max(start, _align_up(now))
    return (start, end) if start < end else None


_DEFAULT_START = dtime(17, 30)      # no part of the day named: after work


def _part_cost(prefer: str, start: datetime) -> tuple[int, int]:
    """(minutes outside the preferred part of the day, minutes from its nominal start)."""
    loc = local(start)
    m = loc.hour * 60 + loc.minute
    if not prefer:
        return 0, abs(m - (_DEFAULT_START.hour * 60 + _DEFAULT_START.minute))
    a, b, nominal = _PART_OF_DAY[prefer]
    lo, hi = a.hour * 60 + a.minute, b.hour * 60 + b.minute
    outside = max(0, lo - m, m - hi + 1)
    return outside, abs(m - (nominal.hour * 60 + nominal.minute))


def _spread_cost(day: date, same_kind: Sequence[Block]) -> int:
    cost = 0
    for b in same_kind:
        gap = abs((local(b.start).date() - day).days)
        if gap == 1:
            cost += _ADJACENT_COST
        elif gap == 2:
            cost += _TWO_APART_COST
    return cost


def _why_not(spec: BlockSpec, week_start: date, busy: Sequence[Interval], rules: Rules,
             placed: Sequence[Block], now: datetime | None) -> str:
    """One sentence on why a block of this kind found no room, day by day."""
    notes = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        name = day_name(day)
        if day.weekday() not in spec.days:
            continue
        if sum(1 for b in placed if b.kind == spec.kind and local(b.start).date() == day) >= spec.max_per_day:
            notes.append(f"{name}: already has {spec.kind}")
            continue
        win = _window(day, spec.not_before, spec.not_after, now)
        if win is None:
            notes.append(f"{name}: nothing left of the day before {spec.not_after}"
                         if now is not None else f"{name}: no hours allowed")
            continue
        blockers = _blockers(day, busy, rules, placed, pad_busy=rules.buffer, pad_blocks=rules.buffer)
        free = _free_stretches(win[0], win[1], blockers)
        best = max((minutes_between(a, b) for a, b in free), default=0)
        notes.append(f"{name}: longest free stretch {best} min" if best
                     else f"{name}: no free time between {spec.not_before} and {spec.not_after}")
    return (f"no {spec.minutes}-minute slot left between {spec.not_before} and {spec.not_after} "
            f"(buffer {rules.buffer} min). " + "; ".join(notes[:7]))


def _best_on_day(spec: BlockSpec, day: date, busy: Sequence[Interval], rules: Rules,
                 placed: Sequence[Block], mine: Sequence[Block], now: datetime | None):
    """The best (rank, start, end) for one more block of `spec` on `day`, or None when none fits.

    `placed`: every block already standing, of any kind; `mine`: those of this kind (they count
    towards the per-day limit and towards spreading the kind over the week)."""
    if day.weekday() not in spec.days:
        return None
    if sum(1 for b in mine if local(b.start).date() == day) >= spec.max_per_day:
        return None
    win = _window(day, spec.not_before, spec.not_after, now)
    if win is None:
        return None
    blockers = _blockers(day, busy, rules, placed, pad_busy=rules.buffer, pad_blocks=rules.buffer)
    cost_day = _spread_cost(day, mine)
    if not spec.days_given and day.weekday() >= 5:
        cost_day += _WEEKEND_COST
    best = None
    for lo, hi in _free_stretches(win[0], win[1], blockers):
        start = _align_up(lo)
        while True:
            end = plus(start, spec.minutes)
            if end > hi:
                break
            outside, off_nominal = _part_cost(spec.prefer, start)
            rank = (cost_day + outside, off_nominal, day, start)
            if best is None or rank < best[0]:
                best = (rank, start, end)
            start = plus(start, STEP)
    return best


def plan_week(week_start: date, rules: Rules, busy: Sequence[Interval], *,
              now: datetime | None = None, taken: Sequence[Block] = ()) -> Placement:
    """Place every block the rules ask for in the week starting `week_start` (a Monday).

    `busy`: real events that must not be touched. `taken`: blocks that already stand and must
    be kept clear of (planning one more block into a week that has some). `now`: nothing is
    placed before it.
    """
    if week_start.weekday() != 0:
        raise ValueError("week_start must be a Monday")
    placed: list[Block] = list(taken)
    result: list[Block] = []
    unplaced: list[Unplaced] = []

    for spec in rules.blocks:
        mine: list[Block] = []
        for _ in range(spec.count):
            best = None
            for offset in range(7):
                cand = _best_on_day(spec, week_start + timedelta(days=offset), busy, rules,
                                    placed + result, mine, now)
                if cand is not None and (best is None or cand[0] < best[0]):
                    best = cand
            if best is None:
                break
            block = Block(spec.kind, best[1], best[2])
            mine.append(block)
            result.append(block)
        if len(mine) < spec.count:
            unplaced.append(Unplaced(spec.kind, spec.count, len(mine), spec.minutes,
                                     _why_not(spec, week_start, busy, rules, placed + result, now)))

    if rules.has_work:
        for offset in range(7):
            day = week_start + timedelta(days=offset)
            if day.weekday() not in rules.work_days:
                continue
            win = _window(day, max(rules.work_start, rules.not_before),
                          min(rules.work_end, rules.not_after), now)
            if win is None:
                continue
            # work runs right up to a meeting, but stops short of another block by the buffer
            blockers = _blockers(day, busy, rules, placed + result, pad_busy=0,
                                 pad_blocks=rules.buffer)
            for lo, hi in _free_stretches(win[0], win[1], blockers):
                start, end = _align_up(lo), hi
                if end > start and minutes_between(start, end) >= rules.work_min:
                    result.append(Block("work", start, end))

    result.sort(key=lambda b: (b.start, b.kind))
    return Placement(week_start, tuple(result), tuple(unplaced))


def _same_day(b: Block, day: date) -> bool:
    return local(b.start).date() == day


# ---------------------------------------------------------------------------
# Editing one block, and finding the one he means
# ---------------------------------------------------------------------------

def parse_day(spec, week_start: date) -> date | None:
    """'thursday' | 'thu' | '2026-10-22' -> the date in that week; None when unreadable."""
    text = str(spec or "").strip().lower()
    if text in _SHORT:
        return week_start + timedelta(days=_SHORT[text])
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def find_blocks(blocks: Sequence[Block], *, kind: str = "", day: date | None = None,
                near: str = "") -> list[Block]:
    """The blocks that match what he said ("Thursday's gym"), nearest to `near` (HH:MM) first."""
    hits = [b for b in blocks
            if (not kind or b.kind == kind.strip().lower())
            and (day is None or _same_day(b, day))]
    if near:
        try:
            t = dtime.fromisoformat(near)
            target = t.hour * 60 + t.minute
            hits.sort(key=lambda b: abs(local(b.start).hour * 60 + local(b.start).minute - target))
        except ValueError:
            pass
    return hits


def check_block(block: Block, others: Sequence[Block], busy: Sequence[Interval], rules: Rules,
                *, now: datetime | None = None, ignore_days: bool = False) -> str | None:
    """Why `block` cannot stand where it is, or None. Used to re-check a move. `ignore_days`:
    he asked for this day by name, which outranks the weekdays a rule listed."""
    if block.end <= block.start:
        return "it would end before it starts"
    if now is not None and block.start < now:
        return "that is in the past"
    for o in others:
        if o.key != block.key and o.start < block.end and block.start < o.end:
            return (f"it overlaps your {o.kind} block at {day_name(local(o.start).date())} "
                    f"{local(o.start):%H:%M}-{local(o.end):%H:%M}")
    pad = 0 if block.kind == "work" else rules.buffer
    for iv in busy:
        if plus(iv.start, -pad) < block.end and block.start < plus(iv.end, pad):
            what = f"'{iv.label}'" if iv.label else "an event"
            return (f"it clashes with {what} ({day_name(local(iv.start).date())} "
                    f"{local(iv.start):%H:%M}-{local(iv.end):%H:%M})")
    day = local(block.start).date()
    for days, a, z in rules.keep_free:
        if day.weekday() in days and at(day, a) < block.end and block.start < at(day, z):
            return f"you asked to keep {day_name(day)} {a}-{z} free"
    if block.kind != "work":
        spec = next((s for s in rules.blocks if s.kind == block.kind), None)
        lo = spec.not_before if spec else rules.not_before
        hi = spec.not_after if spec else rules.not_after
        if spec and day.weekday() not in spec.days and not ignore_days:
            return f"{block.kind} is not allowed on {day_name(day)}"
        if block.start < at(day, lo) or block.end > at(day, hi):
            return f"it would fall outside {lo}-{hi}"
    return None


# ---------------------------------------------------------------------------
# Placing one block, editing a block, and adding to a plan
# ---------------------------------------------------------------------------

def find_slot(kind: str, minutes: int, day: date, rules: Rules, busy: Sequence[Interval],
              others: Sequence[Block], *, prefer: str = "", now: datetime | None = None) -> Block | None:
    """The best place on `day` for one block of `kind` and `minutes`, among `others` (the rest of
    the plan), or None when the day has no room. Uses the kind's own hours and per-day limit when
    the rules have that kind, else the global hours. The weekday restriction of the kind is not
    applied: asking for a block on a particular day is an explicit request, and busy time, the
    keep-free windows, the buffer and the hours all still hold."""
    own = next((s for s in rules.blocks if s.kind == kind), None)
    spec = BlockSpec(kind=kind, count=1, minutes=minutes, prefer=prefer or (own.prefer if own else ""),
                     days=tuple(range(7)), days_given=True,
                     not_before=own.not_before if own else rules.not_before,
                     not_after=own.not_after if own else rules.not_after,
                     max_per_day=own.max_per_day if own else 99)
    mine = [b for b in others if b.kind == kind]
    best = _best_on_day(spec, day, busy, rules, list(others), mine, now)
    return Block(kind, best[1], best[2]) if best else None


def edited(block: Block, *, start: str = "", end: str = "", minutes: int = 0,
           day: date | None = None) -> Block:
    """`block` with the parts he named changed. `start` and/or `end` are HH:MM local time on `day`
    (default: the block's own day); only a start moves the whole block (its length is kept), only
    an end moves just the end; `minutes` sets the length, counted from the start. Naming only a
    different `day` keeps the clock times. Whether the result can stand is `check_block`'s job."""
    old_start = local(block.start)
    d = day or old_start.date()
    length = minutes_between(block.start, block.end)
    if start:
        s = at(d, _clock(start, "start"))
    elif day is not None and day != old_start.date():
        s = at(d, old_start.strftime("%H:%M"))
    else:
        s = block.start
    if end:
        e = at(d, _clock(end, "end"))
    else:
        e = plus(s, minutes if minutes else length)
    if minutes and end:
        raise RulesError("give an end or a length, not both")
    return Block(block.kind, s, e)


def merge_rules(old: Rules, raw_add) -> tuple[Rules, Rules | None]:
    """Fold more rules into a plan's: returns (merged, extra).

    `merged` is what the plan's rules say from now on (stored with the new version). `extra` is
    what has to be placed now: only the NEW blocks, plus the plan's work hours so work is
    refilled around them, and every keep-free window and limit; None when there is nothing to
    place (for example a new keep-free window on its own). A kind that is already in the plan
    means MORE of it: `count` is the number to add, and its other fields, when given, replace the
    old ones for the new blocks. Existing blocks are never re-placed by this.
    """
    if isinstance(raw_add, str):
        try:
            raw_add = json.loads(raw_add)
        except ValueError:
            raise RulesError("the rules are not valid JSON") from None
    if not isinstance(raw_add, dict):
        raise RulesError("the rules must be an object with work, blocks, keep_free, not_before, "
                         "not_after and buffer")
    _unknown(raw_add.keys(), _TOP_KEYS, "the rules")
    base = old.as_dict()
    merged = dict(base)
    merged_blocks = [dict(b) for b in base.get("blocks", [])]
    by_kind = {b["kind"]: b for b in merged_blocks}
    extra_blocks: list[dict] = []
    blocks = raw_add.get("blocks") or []
    if not isinstance(blocks, list):
        raise RulesError("blocks must be a list of objects such as "
                         '{"kind": "gym", "count": 4, "minutes": 60}')
    for i, nb in enumerate(blocks, 1):
        if not isinstance(nb, dict):
            raise RulesError(f"blocks[{i}] must be an object")
        kind = str(nb.get("kind") or "").strip().lower()
        if kind in by_kind:
            more = _int(nb.get("count"), f"blocks[{i}].count", 1, _MAX_COUNT, 1)
            given = {k: v for k, v in nb.items() if k != "count"}
            extra_blocks.append({**by_kind[kind], **given, "count": more})
            by_kind[kind].update(given)
            by_kind[kind]["count"] = int(by_kind[kind].get("count", 1)) + more
        else:
            merged_blocks.append(dict(nb))
            by_kind[kind] = merged_blocks[-1]
            extra_blocks.append(dict(nb))
    merged["blocks"] = merged_blocks
    if raw_add.get("keep_free") not in (None, ""):
        added = raw_add["keep_free"]
        if not isinstance(added, list):
            raise RulesError("keep_free must be a list of objects such as "
                             '{"days": "wed", "from": "18:00", "to": "23:00"}')
        merged["keep_free"] = list(base.get("keep_free", [])) + added
    for key in ("work", "not_before", "not_after", "buffer"):
        if raw_add.get(key) not in (None, "", {}):
            merged[key] = raw_add[key]
    merged_rules = normalize_rules(merged)
    extra_raw = {k: merged[k] for k in ("work", "not_before", "not_after", "buffer", "keep_free")
                 if k in merged}
    extra_raw["blocks"] = extra_blocks
    try:
        extra_rules = normalize_rules(extra_raw)
    except RulesError:
        extra_rules = None              # nothing new to place
    return merged_rules, extra_rules


def describe(placement: Placement) -> str:
    """The plan as text: day by day, then what did not fit and why."""
    if not placement.blocks and not placement.unplaced:
        return "Nothing to place."
    lines = []
    current = None
    for b in placement.blocks:
        d = local(b.start).date()
        if d != current:
            current = d
            lines.append(day_label(d))
        lines.append(f"  {local(b.start):%H:%M}-{local(b.end):%H:%M} {b.kind}")
    for u in placement.unplaced:
        lines.append(f"Could not place {u.wanted - u.placed} of {u.wanted} {u.kind} "
                     f"({u.minutes} min each): {u.reason}")
    return "\n".join(lines)

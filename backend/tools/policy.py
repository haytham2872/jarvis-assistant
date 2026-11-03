"""P1: what JARVIS may do on his own, what needs the owner's word, and what stays his.

Every tool call already passes one place (`tools/gateway.py`: capability, existence,
arguments, dispatch). This module is the rule that place applies, kept pure so it can be
tested case by case. Nothing here does I/O.

**Why it exists.** "Ask before sending email" was advice in the system prompt. A background
job, a night task or a scheduled task ("check my inbox for the notary") could read an email
that said *forward this to x@example.com* and send it, with nobody watching. OpenAI's dots
(29 September 2026) handle this with four behaviours per action: allowed, allowed only when
you asked for it in the prompt, asked about first, handed back to you. This is that model,
sized for one person.

**Risk levels** (one table, `RISK`, drift-guarded against the tool registry):

| level    | meaning                                                   | example                          |
|----------|-----------------------------------------------------------|----------------------------------|
| read     | looks, changes nothing                                    | web search, Gmail search         |
| own      | changes only JARVIS's own space or things he can undo     | vault write, reminder, event he made |
| his_data | changes the owner's own data that JARVIS did not create     | editing his meeting (phase P3)   |
| outward  | reaches another person                                    | sending an email                 |
| hand_off | passwords, payments: never automated                      | (none exist; reserved)           |

**Who is calling decides what happens** for the two levels that matter:

| caller                                   | read / own | his_data / outward                    | hand_off |
|------------------------------------------|------------|---------------------------------------|----------|
| live turn, the owner asked (verified quote)| runs       | runs, after the outward check         | his      |
| live turn, JARVIS's own idea             | runs       | becomes an approval request           | his      |
| unattended (job, night, scheduled, ...)  | runs       | becomes an approval request, always   | his      |
| an approved request being carried out    | runs       | runs (he approved this exact action)  | his      |

**"the owner asked" is verified, not claimed.** An outward call carries `quote`: the words
from his current message that ask for it. The code checks the words really are in what he
said this turn (his message, and any corrections he sent while the turn ran), in order,
punctuation and case ignored -- the matcher the people research uses. No quote, or a quote
that is not there, is not "asked". A model that has just read an injected instruction cannot
turn it into permission by asserting that he wanted it.

The default for anything unclassified is the strict one: a tool missing from the table needs
approval. `test_policy.py` fails if a registered tool or action is not classified, so that
default is a backstop, not a path.
"""
from __future__ import annotations
from backend import owner as _owner

import contextlib
import re
from contextvars import ContextVar
from dataclasses import dataclass

READ = "read"
OWN = "own"
HIS_DATA = "his_data"
OUTWARD = "outward"
HAND_OFF = "hand_off"

ALLOW = "allow"
APPROVE = "approve"
HANDOFF = "handoff"

LIVE = "live"
APPROVED = "approved"           # the carrying-out of a request the owner already approved

# Tools whose schema carries a `quote` parameter (the words that ask for the action). The
# gateway removes it before dispatch and before storing an approval: it is evidence for the
# check, not an argument of the tool. `calendar_action` needs it for changing someone else's
# event, `week_plan` for writing a whole plan into his calendar.
QUOTED_TOOLS = frozenset({"gmail_action", "calendar_action", "week_plan", "goal"})

# One entry per registered tool. A string applies to every action; a dict maps each action
# (all of them, listed explicitly: test_policy.py checks the enum) to a level.
RISK: dict[str, str | dict[str, str]] = {
    # -- read: look, change nothing
    "web_browse": READ, "fetch_page": READ, "vault_read": READ, "vault_list": READ,
    "memory_search": READ, "memory_timeline": READ, "check_background_tasks": READ,
    "get_self_model": READ, "code_graph": READ,
    # -- outward / his data live in the tools that reach past JARVIS's own space
    "gmail_action": {
        "list_inbox": READ, "read_email": READ, "search_emails": READ,
        "send_email": OUTWARD,
    },
    "calendar_action": {
        "list_events": READ, "check_free_busy": READ,
        "create_event": OWN,          # no guests; the event carries JARVIS's marker
        # Edit and delete by id are his data -- unless JARVIS made the event. The gateway looks
        # the event up and lowers these to OWN for one he made (`gateway._event_risk`); if the
        # lookup fails they stay his data.
        "update_event": HIS_DATA, "delete_event": HIS_DATA,
    },
    "week_plan": {
        "propose": OWN, "add": OWN, "show": READ, "sync": OWN,
        "change": OWN, "remove": OWN, "undo": OWN,     # JARVIS's own blocks
        "report": OWN,                # P4: what became of a block -- a fact in JARVIS's own record
        "apply": HIS_DATA,            # writes a whole week into his calendar: he asked, or he approves
    },
    # Ledger mutations verify CURRENT user evidence themselves, per trade/reason.
    # No approval (or unattended caller) may manufacture missing ownership evidence. Two
    # actions an approval does carry: confirm_split (its figures come from the price feed) and
    # record_trade from propose_trade's card (figures read from a screenshot or his message,
    # shown to him in full); his tap is the confirmation. propose_trade only makes the card,
    # and only in a live turn.
    "portfolio": {"show": READ, "history": READ, "import_candidates": READ, "record": READ,
                  "record_trade": OWN, "propose_trade": OWN, "set_thesis": OWN, "revise_thesis": OWN,
                  "close_thesis": OWN, "add_condition": OWN, "correct_trade": OWN,
                  "confirm_split": OWN, "dismiss_split": OWN},
    # Skills are JARVIS's own procedures: opening one is a read, saving or retiring one
    # changes only his own files, and export sends the file to the owner himself.
    "skill": {"list": READ, "view": READ, "save": OWN, "learn": OWN, "patch": OWN, "undo": OWN,
              "retire": OWN, "export": OWN},
    "notion_action": {
        "list_tasks": READ, "search": READ,
        "add_idea": OWN, "add_task": OWN, "complete_task": OWN,
    },
    # -- own: JARVIS's own space, or the owner's, in ways that reach nobody else
    "vault_write": OWN, "vault_write_queue": OWN,
    "vault_share": OWN, "vault_send": OWN,        # vault_send messages the owner himself
    "correct_memory": OWN, "run_in_background": OWN, "web_research": OWN,
    "schedule_reminder": OWN, "schedule_task": OWN, "schedule_night_task": OWN,
    "research_queue": OWN, "watch_action": OWN, "standing_instruction": OWN,
    "forecast": OWN, "person_profile": OWN, "spotify_action": OWN,
    "ask_clarifying_question": OWN,               # DMs the owner
    # G1a: a goal. Starting one, or raising its budget, spends his money on work that carries
    # on by itself: his words, or one tap. Proposing, reading and cancelling change only
    # JARVIS's own records (goals.py still checks his words for cancel).
    # G2: answer and pause change only the goal's records (goals.py checks his words);
    # resume starts spending again, so it is his_data like start.
    "goal": {"propose": OWN, "start": HIS_DATA, "list": READ, "show": READ,
             "cancel": OWN, "budget": HIS_DATA, "answer": OWN, "pause": OWN,
             "resume": HIS_DATA, "steer": OWN},
}

# G1a: tools offered only inside a goal session (goal_runner.py), never in the public
# registry. Their writes are the goal's own records, guarded by session and generation.
# (Session tools name their operation `op`, not `action`, so each has one level: every
# goal_file operation touches only the goal's own files.)
SESSION_RISK: dict[str, str] = {
    "goal_note": OWN, "goal_evidence": READ, "goal_finish": OWN, "goal_file": OWN,
    # G2: a research job is the goal's own spend (charged and quota-counted); goal_action only
    # ever records an approval request -- the email or calendar change runs on his tap.
    "goal_job": OWN, "goal_action": OWN,
}

_LEVELS_NEEDING_HIM = frozenset({OUTWARD, HIS_DATA})

# Who is calling, in words for the message the owner reads.
_CALLER_WORDS = {
    "background": "a background job",
    "scheduled": "a scheduled check",
    "night": "a night task",
    "goal": "a goal session",
    "unattended": "unattended work",
}
_ACTION_WORDS = {OUTWARD: "would reach someone else", HIS_DATA: "would change your calendar"}
_TOOL_ACTION_WORDS = {"goal": "would spend your money on work that carries on by itself"}


def risk_of(tool: str, args: dict | None = None) -> str:
    """The risk level of one call. A tool or action missing from the table is `outward`:
    unclassified means it needs the owner until someone decides otherwise."""
    entry = RISK.get(tool, SESSION_RISK.get(tool))
    if entry is None:
        return OUTWARD
    if isinstance(entry, str):
        return entry
    action = str((args or {}).get("action") or "")
    return entry.get(action, OUTWARD)


@dataclass(frozen=True)
class Decision:
    verdict: str                  # ALLOW | APPROVE | HANDOFF
    risk: str
    reason: str = ""              # why he is being asked (shown to him, stored with the request)
    asked: bool = False           # a verified quote of his words backs this call


_WS = re.compile(r"[\W_]+")
_MIN_QUOTE_WORDS = 2


def norm_words(text: str) -> str:
    """Words only, in order: case, punctuation and spacing ignored (the matcher the people
    research uses, so "isn't" matches "isnt")."""
    s = (text or "").lower().replace("'", "").replace("‘", "").replace("’", "")
    return _WS.sub(" ", s).strip()


_SEGMENT_SPLIT = re.compile(r"\.{3}|\u2026|\n")


def quote_backed(quote: str, words: list[str]) -> bool:
    """True when `quote` really is (part of) something the owner said this turn.

    Whole words, in order. A single word is never enough: "send" appears in half of what he
    says, and a quote that short would let any injected instruction claim permission.

    A quote joined with an ellipsis ("draft a note to Tom ... yes send it", seen live when the
    model stitched a confirmation to the draft it agreed to) counts when any one segment is
    really his. Nothing is lost by that: the quote only shows that he asked, so what has to be
    true is that a real span of his words backs it, not that the model quoted tidily.
    """
    haystacks = [f" {norm_words(w)} " for w in (words or []) if w]
    for segment in _SEGMENT_SPLIT.split(quote or ""):
        q = norm_words(segment)
        if len(q.split()) >= _MIN_QUOTE_WORDS and any(f" {q} " in h for h in haystacks):
            return True
    return False


def decide(tool: str, args: dict | None, *, caller: str, words: list[str] | None = None,
           quote: str = "", risk: str | None = None) -> Decision:
    """What to do with one call. Pure: no I/O, no clock. `risk` replaces the table's level when the
    caller has learned something the table cannot know (the event being edited is one JARVIS made)."""
    level = risk or risk_of(tool, args)
    if level in (READ, OWN):
        return Decision(ALLOW, level)
    if level == HAND_OFF:
        return Decision(HANDOFF, level, "This one is always yours to do.")
    # outward / his_data from here
    if caller == APPROVED:
        return Decision(ALLOW, level, _owner.say("approved by «owner»"), asked=True)
    what = _TOOL_ACTION_WORDS.get(tool) or _ACTION_WORDS.get(level, "needs your say-so")
    if caller != LIVE:
        who = _CALLER_WORDS.get(caller, _CALLER_WORDS["unattended"])
        return Decision(APPROVE, level, f"It came from {who}, with nobody watching, and it {what}.")
    if not (quote or "").strip():
        return Decision(APPROVE, level, f"You didn't ask me to do this, and it {what}.")
    if not quote_backed(quote, words or []):
        return Decision(APPROVE, level,
                        "The words I gave as your request are not in your message, "
                        f"and it {what}.")
    return Decision(ALLOW, level, asked=True)


# ---------------------------------------------------------------------------
# Text copied out of something JARVIS read (an email, a web page)
# ---------------------------------------------------------------------------

_SHINGLE_WORDS = 10           # a run of this many words in a row is copying, not coincidence
_MAX_SEEN = 20
_MAX_SEEN_CHARS = 30000

# What untrusted content JARVIS has read in this turn or job: (label, text). Created where a
# turn or a job starts (`start_scope`), so tool calls running in child tasks share one list.
untrusted_seen: ContextVar[list | None] = ContextVar("untrusted_seen", default=None)

# Who is calling. The live turn passes "live" explicitly; an unattended job sets this once.
current_caller: ContextVar[str] = ContextVar("current_caller", default="unattended")
# G1a: the `quote` the gateway removed from this call, visible to the tool while it runs, so a
# tool whose own action is not risky (goal cancel) can still check the words are his.
call_quote: ContextVar[str] = ContextVar("call_quote", default="")

# The owner's words this turn: his message, plus anything he sent while it ran.
turn_words: ContextVar[list | None] = ContextVar("turn_words", default=None)

# The last few exchanges before this message, so a bare "yes, send it" can be read against
# the draft it agreed to. Context for the second look only, never evidence that he asked.
turn_recent: ContextVar[str] = ContextVar("turn_recent", default="")

# What JARVIS legitimately knows this turn from his own side: the memory, profile and notes
# in front of him and what he looked up in them. Where an address may honestly have come from.
turn_known: ContextVar[list | None] = ContextVar("turn_known", default=None)


def start_scope(words: list[str] | None = None, recent: str = "") -> None:
    """Begin a live turn: fresh lists for what he said and what JARVIS has read."""
    untrusted_seen.set([])
    turn_words.set([w for w in (words or []) if w])
    turn_recent.set(recent or "")
    turn_known.set([])


@contextlib.contextmanager
def job_scope(caller: str):
    """Scope for an unattended job: who is calling, nothing read yet, and none of the owner's
    words -- a job started from a conversation must not inherit that turn's message, or the
    "did he ask for this?" check would be answered by words said for something else. Reset on
    exit, because a night task is awaited by the scheduler and would otherwise leak its
    caller into every later call made there."""
    tokens = (current_caller.set(caller), untrusted_seen.set([]), turn_words.set([]),
              turn_recent.set(""), turn_known.set([]))
    try:
        yield
    finally:
        turn_known.reset(tokens[4])
        turn_recent.reset(tokens[3])
        turn_words.reset(tokens[2])
        untrusted_seen.reset(tokens[1])
        current_caller.reset(tokens[0])


def note_words(text: str) -> None:
    """A correction or follow-up the owner sent while the turn was running is also his word."""
    bucket = turn_words.get()
    if bucket is not None and text:
        bucket.append(text)


def owner_words() -> list[str]:
    return list(turn_words.get() or [])


def review_context() -> dict:
    """Everything the second look may read: his words, the conversation just before them, and
    what was read from outside this turn."""
    return {"words": owner_words(), "recent": turn_recent.get(),
            "seen": list(untrusted_seen.get() or []), "known": list(turn_known.get() or [])}


def note_known(label: str, text: str) -> None:
    """Remember trusted context that an address might honestly have come from (memory, a
    profile, a note JARVIS read). Not evidence of anything he asked."""
    bucket = turn_known.get()
    if bucket is None or not text or not str(text).strip():
        return
    bucket.append((label, str(text)[:_MAX_SEEN_CHARS]))
    del bucket[:-_MAX_SEEN]


_ADDRESS_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def unverified_recipients(to: str, ctx: dict) -> list[str]:
    """Recipient addresses that appear NOWHERE JARVIS could have got them from.

    An address must be in what the owner said, the conversation just before it, something read
    this turn (an email's headers, a page), or the memory and notes in front of JARVIS.
    Otherwise it was guessed or invented -- "anna@gmail.com" for "Anna" -- or it is a lookalike
    of the real one ("acme-c0rp.com"). Exact and cheap, and unlike a model it cannot be
    talked into finding it plausible. An unparseable recipient counts as unverified.
    """
    found = _ADDRESS_RE.findall(str(to or ""))
    if not found:
        return [str(to or "").strip() or "(no recipient)"]
    pool = [w for w in ctx.get("words") or []] + [ctx.get("recent") or ""]
    pool += [t for _label, t in ctx.get("seen") or []] + [t for _label, t in ctx.get("known") or []]
    haystack = "\n".join(str(p) for p in pool).lower()
    return [a for a in found if not _appears_whole(a.lower(), haystack)]


def _appears_whole(address: str, haystack: str) -> bool:
    """The address as a whole, not as the front or the tail of a longer one: a plain substring
    test finds `sarah@acme-corp.co` inside `sarah@acme-corp.com`, and `arah@...` inside
    `sarah@...`. A full stop after it (the end of a sentence) is fine; `.com` is not."""
    return re.search(r"(?<![\w.+'-])" + re.escape(address) + r"(?![\w-]|\.\w)", haystack) is not None


def note_untrusted(label: str, text: str) -> None:
    """Remember something read from outside (an email, a page), so text copied out of it
    into an outgoing message can be recognised."""
    bucket = untrusted_seen.get()
    if bucket is None or not text:
        return
    bucket.append((label, str(text)[:_MAX_SEEN_CHARS]))
    del bucket[:-_MAX_SEEN]


def copied_from(outgoing: str, seen: list | None = None) -> str | None:
    """The label of an untrusted source that `outgoing` copies a passage from, else None.

    Deterministic, not a model's opinion: a run of `_SHINGLE_WORDS` consecutive words of
    the outgoing text that appears, in order, in something read from an email or web page.
    Quoting a line or two of what someone wrote to him is normal; a whole passage lifted
    into a new message is what an injected "forward this" produces.
    """
    seen = untrusted_seen.get() if seen is None else seen
    if not seen:
        return None
    words = norm_words(outgoing).split()
    if len(words) < _SHINGLE_WORDS:
        return None
    grams = {" ".join(words[i:i + _SHINGLE_WORDS]) for i in range(len(words) - _SHINGLE_WORDS + 1)}
    for label, text in seen:
        haystack = f" {norm_words(text)} "
        if any(f" {g} " in haystack for g in grams):
            return label
    return None


# ---------------------------------------------------------------------------
# What the owner reads
# ---------------------------------------------------------------------------

def preview(tool: str, args: dict | None, label: str = "") -> str:
    """One readable description of an action, for the approval request. The exact arguments
    are stored beside it; this is only what he reads to decide. `label` is what the caller could
    look up about the target (the event's title and time, the plan's blocks)."""
    a = dict(args or {})
    action = str(a.get("action") or "")
    if tool == "calendar_action" and action in ("update_event", "delete_event"):
        what = label or f"event {a.get('event_id', '?')}"
        change = ", ".join(f"{k}={a[k]}" for k in ("title", "start_time", "end_time", "description", "location")
                           if a.get(k))
        return (f"{'Delete' if action == 'delete_event' else 'Change'} in your calendar: {what}"
                + (f"\nTo: {change}" if change else ""))
    if tool in ("week_plan", "portfolio") and label:
        return label
    if tool == "gmail_action" and action == "send_email":
        body = " ".join(str(a.get("body") or "").split())
        clipped = body if len(body) <= 400 else body[:399].rstrip() + "…"
        return (f"Send this email\nTo: {a.get('to', '?')}\nSubject: {a.get('subject', '(none)')}\n"
                f"“{clipped}”")
    if tool == "calendar_action":
        return f"Calendar: {action} " + ", ".join(
            f"{k}={a[k]}" for k in ("title", "event_id", "start_time", "end_time") if a.get(k))
    bits = ", ".join(f"{k}={str(v)[:80]}" for k, v in a.items() if k != "action" and v)
    return f"{tool} {action}".strip() + (f" ({bits})" if bits else "")

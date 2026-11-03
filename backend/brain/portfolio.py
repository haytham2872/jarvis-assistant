"""Portfolio tool, bounded turn context, daily marks and linked thesis reviews.

No orders, no model arithmetic, no inferred holdings. Its own paced loop fetches closes
(code only, no model) and checks conditions; price alerts share the existing watches
delivery state machine and next-turn notices.

Three rules worth knowing before changing anything here:
  * A trade is recorded only from the owner's own words: the quote must be his, and the
    stock, share count and price must all be in it, said as a holding or a trade -- not a
    question, a hypothetical, a denial or something JARVIS said. A wrong entry is
    cancelled with `correct_trade`, never edited.
  * A split the feed reports but the ledger lacks holds that stock's figures and price
    alerts at the last close before it, and he is asked once (Approve / Deny). Ten shares
    at $1,000 must never read as ten shares at $105.
  * A close missed once (both feeds down, or /pause) is fetched later. A past close is a
    fixed fact; unlike a scheduled "price at the open" report, fetching it late is not
    wrong.
"""
from __future__ import annotations
from backend import owner as _owner

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

from backend.market import data
from backend.storage import lifelog, operations, portfolio as store
from backend.tools import policy

log = logging.getLogger(__name__)
_notifier = None
_cycle_lock = asyncio.Lock()

_CHECK_S = 60.0                 # its own loop: a slow feed never delays reminders
_LOOKBACK_DAYS = 45             # closes missed this far back are still fetched
_MAX_FETCHES_PER_CYCLE = 2
_RETRY_S = 1800                 # the evening of the close: every 30 minutes
_MISSING_AFTER_S = 6 * 3600     # then it is "missing", and tried again every 6 hours
_MISSING_RETRY_S = 6 * 3600
_SLOW_RETRY_AFTER = 12          # ...and after 12 tries (about three days) once a week
_SLOW_RETRY_S = 7 * 86400
_SPLIT_RETRY_S = 1800
_SPLIT_ASK_RETRY_S = 3600
# A close this far from the previous one is held back from price alerts until the split
# check has covered its date (a 2-for-1 split reads as -50%, a reverse split as +100%).
_JUMP_LOW, _JUMP_HIGH = Decimal("0.6"), Decimal("1.67")
_split_next_try: dict[str, float] = {}

# Explicit aliases, never guessed tickers. Unknown names require an exchange/ticker.
CATALOGUE = {
    "NVDA": ("NVIDIA", "NASDAQ", "USD", "NVDA", ["nvidia", "nvda"]),
    "TTWO": ("Take-Two Interactive", "NASDAQ", "USD", "TTWO", ["take-two", "take two", "take-two interactive", "ttwo"]),
    "TSLA": ("Tesla", "NASDAQ", "USD", "TSLA", ["tesla", "tsla"]),
    "AAPL": ("Apple", "NASDAQ", "USD", "AAPL", ["apple", "aapl"]),
    "MSFT": ("Microsoft", "NASDAQ", "USD", "MSFT", ["microsoft", "msft"]),
    "AMD": ("Advanced Micro Devices", "NASDAQ", "USD", "AMD", ["amd", "advanced micro devices"]),
    "AMZN": ("Amazon", "NASDAQ", "USD", "AMZN", ["amazon", "amzn"]),
    "GOOGL": ("Alphabet class A", "NASDAQ", "USD", "GOOGL", ["googl", "alphabet class a"]),
    "GOOG": ("Alphabet class C", "NASDAQ", "USD", "GOOG", ["goog", "alphabet class c"]),
    "AIR.PA": ("Airbus", "XPAR", "EUR", "AIR.PA", ["airbus", "air.pa"]),
    "MC.PA": ("LVMH", "XPAR", "EUR", "MC.PA", ["lvmh", "mc.pa"]),
    "ASML.AS": ("ASML Amsterdam", "XAMS", "EUR", "ASML.AS", ["asml.as", "asml amsterdam"]),
    "ASML": ("ASML US ADR", "NASDAQ", "USD", "ASML", ["asml us adr"]),
}
AMBIGUOUS = {"alphabet", "google", "asml"}
# "146,68" is his French decimal comma, "1,000" a thousands separator (a comma followed by exactly
# three digits): live (28 October) "Sell all my spcx at 146,68" was read as 146 and 68.
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:(?:,\d{3})+(?![\d,])(?:\.\d+)?|[.,]\d+)?(?!\w|[.,]\d)")
_THOUSANDS = re.compile(r"\d+(?:,\d{3})+(?:\.\d+)?")


def _decimal(figure: str) -> Decimal:
    """A figure as he wrote it: "1,000" is a thousand, "146,68" is 146.68."""
    return Decimal(figure.replace(",", "") if _THOUSANDS.fullmatch(figure) else figure.replace(",", "."))

# --- reading a holding out of his words -------------------------------------------------
# Measured on 40 phrasings (test_portfolio.py, ACCEPTED / REFUSED): the first version took
# 7 of 16 plain statements ("my NVDA position is 200 shares at 118" was refused), so
# JARVIS would have asked him to repeat what he had just said.
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_TEXT = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
                        rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}|\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.I)
# Pieces of a sentence: one holding each. A comma inside 1,000 is not a break.
_PIECE_SPLIT = re.compile(r"\s*(?:;|\n|(?<!\d),|,(?!\d)|\band\b|\bthen\b|\bplus\b|\.(?=\s|$))\s*", re.I)
_TICKER_LIKE = re.compile(r"(?<![\w.$€£])[A-Z]{2,5}(?:\.[A-Z]{1,2})?(?!\w)")
_NOT_TICKERS = {"USD", "EUR", "GBP", "CHF", "ETF", "AI", "CEO", "CFO", "OK", "PM", "AM", "IPO",
                "EPS", "PE", "US", "EU", "UK", "FX", "PNL", "ATH", "YTD", "DM", "TBH", "IMO", "ASAP"}
_SIDES = {
    "opening": re.compile(r"\b(have|has|hold|holds|holding|holdings|own|owns|got|position|positions|"
                          r"balance|i'?m in|i am in|sitting on|long)\b", re.I),
    "buy": re.compile(r"\b(bought|purchased|added|picked up|acquired)\b", re.I),
    "sell": re.compile(r"\b(sold|trimmed|dumped|offloaded|exited|closed out|disposed of)\b", re.I),
    "split": re.compile(r"\bsplit\b", re.I),
}
# Before the figures, any of these means it is not a holding of his.
_NOT_A_HOLDING = re.compile(
    r"\b(if|suppose|supposing|hypothetically|imagine|pretend|assume|assuming|what if|let'?s say|say i|for example|for instance|example"
    r"|would|could|might|should|will|i'?ll|i'?d|going to|gonna|plan(?:ning)? to|want(?:ing)? to"
    r"|thinking (?:of|about)|considering|maybe|used to|(?<!'ve )(?<!have )had"
    r"|don'?t|do not|didn'?t|did not|never|no longer|haven'?t|have not|not"
    r"|you said|you told|you think|you guessed|you mentioned|jarvis said|assistant said|according to you"
    r"|my (?:wife|husband|partner|girlfriend|boyfriend|brother|sister|dad|father|mom|mum|mother|friend|son|daughter|uncle|aunt|cousin)"
    r"|(?:he|she|they|we)\s+(?:\w+\s+)?(?:has|have|hold|holds|own|owns|bought|sold|got))\b", re.I)
# A question even without its question mark (voice notes are transcribed, and the mark is
# often lost): "Do I still have 40 TSLA at 210".
_QUESTION_START = re.compile(r"(?:^|[,;:.]\s*)(?:do|does|did|am|is|are|was|were|can|could|should|would|will|shall)"
                             r"\s+(?:i|my|we|you)\b|^\s*(?:how|what|why|when|which|where)\b", re.I)
_PRICE_BEFORE = re.compile(
    r"(?:(?<!\w)(?:at|@|for|paid|cost|costs|price|priced|average|avg|basis|entry)"
    r"(?:\s+(?:an?|the|my|of|average|avg|cost|price|basis|around|about|roughly))*\s*[$€£]?\s*|[$€£]\s*)$", re.I)
_PRICE_AFTER = re.compile(r"^\s*(?:(?:each|apiece|a share|per share|average|avg|dollars?|euros?|usd|eur)\b|/share|[$€])", re.I)
_FEE_NEAR = re.compile(r"\b(fees?|commission)\b", re.I)
# "Sold all my SPCX at 146.68": the share count is the ledger's whole holding.
_ALL_CUE = re.compile(r"\b(?:all|everything|entire|whole|every share)\b", re.I)
# The quote of a trade the owner approved on a card (propose_trade): his tap is the evidence.
_CARD_QUOTE = _owner.say("approved by «owner» on the card: ")
_CORRECTION_CUE = re.compile(r"\b(wrong|mistake|mistaken|incorrect|correct|correction|fix|actually|meant|instead"
                             r"|remove|delete|undo|cancel|void|scratch that|typo|error|not right|shouldn'?t)\b", re.I)
_CONFIRM_CUE = re.compile(r"\b(yes|yeah|yep|yup|confirm|confirmed|record|correct|right|true|go ahead|do it|ok|okay|sure|split|did)\b", re.I)
_DISMISS_CUE = re.compile(r"\b(no|nope|wrong|not|didn'?t|did not|never|dismiss|ignore|false|mistake|error|bogus)\b", re.I)
_TRADE_FIELDS = {"symbol", "side", "shares", "price", "currency", "fees", "traded_on", "quote"}


def set_notifier(fn):
    global _notifier
    _notifier = fn


def handle_command(arg: str = "") -> str:
    try:
        if arg.strip().lower() in ("stats", "record"):
            return record_text()
        return show_text(resolve_instrument(arg)["symbol"] if arg.strip() else "")
    except Exception as exc:
        return f"Error: portfolio unavailable ({exc})"


def resolve_instrument(symbol: str) -> dict:
    supplied = str(symbol or "").strip()
    existing = next((i for i in store.instruments() if i["symbol"].casefold() == supplied.casefold()), None)
    if existing:
        return existing
    upper = supplied.upper()
    # Exact ticker beats aliases; plain ASML requires his listing/currency.
    if supplied.lower() in AMBIGUOUS:
        raise ValueError(f"{supplied} has multiple share classes/listings; ask for the ticker and exchange")
    for ticker, (name, exchange, currency, key, aliases) in CATALOGUE.items():
        if upper == ticker or supplied.lower() in aliases:
            return {"symbol": ticker, "name": name, "exchange": exchange, "currency": currency, "price_key": key}
    raise ValueError(f"unknown or ambiguous instrument {supplied!r}; ask for its exact ticker and exchange")


def _alias_pattern(i: dict) -> str:
    aliases = CATALOGUE.get(i["symbol"], (None, None, None, None, [i["symbol"], i["name"]]))[4]
    return r"(?<!\w)(?:" + "|".join(re.escape(a) for a in sorted(set(aliases + [i["symbol"]]), key=len, reverse=True)) + r")(?!\w)"


def _verified(quote: str) -> str:
    """Use P1's matcher, then keep only a real full segment (never ellipsis padding)."""
    for segment in policy._SEGMENT_SPLIT.split(quote or ""):
        if policy.quote_backed(segment, policy.owner_words()):
            return segment.strip()
    # Replayed (5 October): given "that's wrong, it's 20 shares not 200, same price", JARVIS quoted his
    # own paraphrase, was refused, and asked the owner to retype it, when his words would have passed.
    # Live (28 October): his message was "Heres the details" and a broker screenshot. Refused, JARVIS
    # quoted his previous message, then single words of it, and told him the check "is not receiving
    # this turn's text at all" -- a fault invented for a refusal whose reason it was never shown.
    # The refusal now says what his message is, and that figures in a picture are not his words.
    said = " ".join(policy.owner_words())
    attached = re.search(r"\[Sent (image|document)", said)
    raise ValueError(
        _owner.say("quote is not in «owner's» current message, which is: \"") + _short(said, 160) + _owner.say("\". Only "
        "this message counts, not an earlier one. If it states the trade, call again with «his» words "
        "from it, copied exactly (for a correction, the part that gives the right figure). ")
        + (_owner.say("Figures shown only in an image or document «he» sent are not «his» words, so the ledger "
           "cannot take them from it: call propose_trade with the figures you read there, which "
           "puts them to «him» as one Approve/Deny card, and tell «him» so. ") if attached else
           _owner.say("If it does not state the trade, ask «him» to say it in one sentence (e.g. 'sold 10 NVDA at "
           "181.40'), or, if «his» message clearly means a trade, propose_trade puts your reading of "
           "it to «him» as one Approve/Deny card. "))
        + "This refusal is the check working as designed, not a fault, and says nothing about what "
        "the ledger already holds: portfolio show does.")


def _short(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def _known_instruments() -> list[dict]:
    known = {t: {"symbol": t, "name": v[0]} for t, v in CATALOGUE.items()}
    try:
        known.update({i["symbol"]: i for i in store.instruments()})
    except Exception:
        pass
    return list(known.values())


def _groups(q: str, instrument: dict) -> list[dict]:
    """Split his sentence into one group per holding it names.

    Each piece (cut at "and", commas, semicolons, full stops) belongs to the last holding
    named before it; pieces before the first one belong to that first one. Returns this
    instrument's groups, each with the heads (text before the first figure) of the groups
    before it, because "If I have 200 NVDA and 50 TTWO" makes both hypothetical and
    "I sold 10 NVDA and 100 TTWO" makes both sales."""
    own = re.compile(_alias_pattern(instrument), re.I)
    others = [re.compile(_alias_pattern(i), re.I) for i in _known_instruments() if i["symbol"] != instrument["symbol"]]

    def names_another(piece: str) -> bool:
        if any(o.search(piece) for o in others):
            return True
        return any(m.group() not in _NOT_TICKERS and not own.fullmatch(m.group())
                   for m in _TICKER_LIKE.finditer(piece))

    lead, groups, current = [], [], None
    for piece in (p.strip() for p in _PIECE_SPLIT.split(q)):
        if not piece:
            continue
        owner = "self" if own.search(piece) else "other" if names_another(piece) else None
        if owner is None:
            (current["pieces"] if current else lead).append(piece)
        else:
            current = {"owner": owner, "pieces": [piece]}
            groups.append(current)
    if groups and lead:
        groups[0]["pieces"] = lead + groups[0]["pieces"]
    out, heads = [], []
    for g in groups:
        text = ", ".join(g["pieces"])
        first = _NUMBER.search(_DATE_TEXT.sub(lambda m: " " * len(m.group()), text))
        head = text[:first.start()] if first else text
        if g["owner"] == "self":
            out.append({"text": text, "before": list(heads)})
        heads.append(head)
    return out


def _sides(text: str) -> set[str]:
    return {name for name, pattern in _SIDES.items() if pattern.search(text)}


def _figures(text: str) -> list[tuple[int, int, Decimal]]:
    masked = _DATE_TEXT.sub(lambda m: " " * len(m.group()), text)
    return [(m.start(), m.end(), _decimal(m.group())) for m in _NUMBER.finditer(masked)]


def _price_cued(text: str, start: int, end: int) -> bool:
    return bool(_PRICE_BEFORE.search(text[max(0, start - 40):start]) or _PRICE_AFTER.match(text[end:end + 20]))


def _read_group(group: dict, side: str, qty: Decimal, price: Decimal,
                held: Decimal | None = None) -> str | None:
    """The reason this group does not state the trade, or None when it does. `held`: the
    shares the ledger holds, for a sale of "all" of them."""
    text = group["text"]
    if "?" in text:
        return "a question is not a holding"
    figures = _figures(text)
    if side == "split":
        used = [f for f in figures if f[2] == qty]
        if not used:
            return "state the split multiplier explicitly (e.g. 'NVDA split 10 for 1')"
        end = used[0][1]
    else:
        prices = [f for f in figures if f[2] == price and _price_cued(text, f[0], f[1])]
        shares = [f for f in figures if f[2] == qty and not _PRICE_BEFORE.search(text[max(0, f[0] - 40):f[0]])]
        cue = _ALL_CUE.search(text) if side == "sell" and not shares and held is not None else None
        if cue and qty != held:
            return (_owner.say(f"«he» said «he» sold all of it, and the ledger holds {_plain(held)} shares: record a "
                    f"sale of {_plain(held)}"))
        if cue:
            shares = [(cue.start(), cue.end(), qty)]     # "all": the count is the ledger's
        pairs = [(s, p) for s in shares for p in prices if s != p]
        if not pairs:
            # Name what is missing, so the reply can say it rather than guess at a cause.
            missing = [f"{what} {_plain(value)}" for what, value, given in
                       (("share count", qty, bool(cue)), ("price", price, False))
                       if not given and not any(f[2] == value for f in figures)]
            if missing and cue:
                return (_owner.say(f"«his» words for this stock do not give the {' or the '.join(missing)}: \"all\" gives "
                        "the count («his» whole holding), and the price must be in what «he» says this message"))
            if missing:
                return (_owner.say(f"«his» words for this stock do not give the {' or the '.join(missing)}: the share "
                        "count and the price must both be in what «he» says this message"))
            return _owner.say("«his» words must give the price as a price (e.g. 'at 118', '$118', 'cost basis 118')")
        end = min(max(s[1], p[1]) for s, p in pairs)
    # A piece with a verb of its own ("but I have 200 NVDA") stands alone; one without
    # ("...and 50 TTWO at 160") is governed by the verb, and any "if", before it.
    own_sides = _sides(text)
    head = text[:end] if own_sides else " ".join(group["before"] + [text[:end]])
    if _NOT_A_HOLDING.search(head) or _QUESTION_START.search(head):
        return _owner.say("hypothetical, questioned, denied, past or someone else's figures are not «his» holding")
    found = own_sides or next((_sides(h) for h in reversed(group["before"]) if _sides(h)), set())
    if side == "opening":
        if found - {"opening", "buy"}:
            return _owner.say("«his» words describe a sale or split, not a holding")
    elif side not in found:
        if side in ("buy", "sell") and re.search(rf"\b{'buy' if side == 'buy' else 'sell'}\b", text, re.I):
            # "Sell all my SPCX at 146,68" (28 October): an order he cannot give here, or a sale
            # already made? Not read as a fact; his tap on a card can say which.
            return (_owner.say(f"«his» words say '{side}', not that «he» {'bought' if side == 'buy' else 'sold'} it: if "
                    "«he» means a trade «he» already made, propose_trade puts these figures to «him» as one "
                    "Approve/Deny card"))
        return _owner.say(f"«his» words must say «he» {'bought' if side == 'buy' else 'sold' if side == 'sell' else 'split'} it")
    return None


def verify_trade(row: dict, instrument: dict) -> str:
    q = _verified(str(row.get("quote") or ""))
    side = row.get("side", "opening")
    if side not in _SIDES:
        raise ValueError("side must be opening, buy, sell or split")
    groups = _groups(q, instrument)
    if not groups:
        raise ValueError("quote does not identify this instrument")
    qty = data.number(row.get("shares"))
    price = data.number(row.get("price", 0), zero=side == "split")
    held = None
    if side == "sell":
        rows = store.positions(instrument["symbol"])
        held = Decimal(rows[0]["shares"] or "0") if rows else None
    reasons = [_read_group(g, side, qty, price, held) for g in groups]
    if all(reasons):
        raise ValueError(reasons[0])
    clause = groups[reasons.index(None)]["text"]
    fees = data.number(row.get("fees", 0), zero=True)
    if fees:
        stated = any(f[2] == fees and (_FEE_NEAR.search(clause[max(0, f[0] - 25):f[0]]) or
                                       _FEE_NEAR.search(clause[f[1]:f[1] + 25]))
                     for f in _figures(clause))
        if not stated:
            raise ValueError(_owner.say("fees must be explicitly stated as fees in «his» quote"))
    currency = str(row.get("currency") or instrument["currency"]).upper()
    explicit = "EUR" if "€" in clause or re.search(r"\b(EUR|euros?)\b", clause, re.I) else (
        "USD" if "$" in clause or re.search(r"\b(USD|dollars?)\b", clause, re.I) else None)
    if explicit and currency != explicit:
        raise ValueError(_owner.say("trade currency conflicts with «his» quote"))
    if not explicit and currency != instrument["currency"]:
        raise ValueError("an unstated currency defaults to the instrument's own currency")
    return q


def _date_stated(day: str, q: str) -> bool:
    """Is the date of a backdated trade in his words? An ISO date, "12 September",
    "Sept 12", or "yesterday"."""
    if day in q:
        return True
    d, text = date.fromisoformat(day), q.lower()
    names = {d.strftime("%B").lower(), d.strftime("%b").lower()} | ({"sept"} if d.month == 9 else set())
    months = "|".join(sorted(names, key=len, reverse=True))
    if (re.search(rf"\b{d.day}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{months})\b", text)
            or re.search(rf"\b(?:{months})\.?\s+{d.day}(?:st|nd|rd|th)?\b", text)):
        return True
    return "yesterday" in text and d == date.fromisoformat(lifelog.local_date()) - timedelta(days=1)


def relevant(text: str) -> bool:
    if re.search(r"\b(my (?:position|portfolio|holding|thesis)|portfolio|record.*shares|bought.*shares|sold.*shares)\b", text, re.I):
        return True
    return any(re.search(_alias_pattern(i), text, re.I) for i in store.instruments())


def money(value) -> str:
    return "unavailable" if value is None else f"{Decimal(str(value)):,.2f}"


def _plain(value) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _effective_level(c: dict, day: str) -> Decimal:
    """The condition's level in the shares of `day`: his level, divided by every split
    recorded after he set it."""
    return Decimal(c["level"]) / store.split_factor(c["instrument"], c["local_date"], day)


def _condition_line(c: dict, today: str) -> str:
    what = c["level"] or c["criterion"]
    line = f"C{c['id']} {c['direction']}: {c['kind']} {what} {c['due_date']} [{c['state']}]"
    if c["kind"].startswith("price_") and c["level"]:
        level = _effective_level(c, today)
        if level != Decimal(c["level"]):
            line += _owner.say(f" (now {_plain(level)}, adjusted for a split since «he» set it)")
    return line + "."


def _split_words(s: dict) -> str:
    return (_owner.say(f"{s['instrument']}: the price feed reports a {s['numerator']}-for-{s['denominator']} split on "
            f"{s['split_date']} that is not in the ledger. Its value, P&L and price alerts are held at the "
            "last close before it until «owner» confirms it (portfolio confirm_split")
            + (f", or approval #{s['approval_id']}" if s["approval_id"] else "")
            + ") or says the feed is wrong (dismiss_split).")


def show_text(symbol: str = "", *, compact=False) -> str:
    rows = store.positions(symbol)
    if not rows:
        return _owner.say("No positions or tracked theses recorded. Ask «owner» for «his» actual holdings; never infer them from memory.")
    lines = ["Portfolio ledger (tracked positions; average-cost basis):"]
    total = Decimal(0)
    total_ok = True
    dates = set()
    for s in store.splits(symbol, state="pending"):
        lines.append(_split_words(s))
    for p in rows:
        shares = Decimal(p["shares"] or "0")
        if not shares:
            lines.append(f"{p['symbol']}: tracked, no shares held; realised {money(p['realised'] or '0')} {p['currency']}.")
            continue
        line = f"{p['symbol']}: {shares.normalize():f} shares; average {money(p['average_cost'])} {p['currency']}; "
        if p["mark_date"]:
            gain = Decimal(p['unrealised']) / Decimal(p['cost_basis']) * 100 if Decimal(p['cost_basis']) else None
            line += f"value {money(p['value'])}, P&L {money(p['unrealised'])} {p['currency']} ({money(gain)}% on recorded cost) as of close {p['mark_date']}."
            dates.add(p["mark_date"])
        else:
            line += "no completed close recorded; value/P&L unavailable."
        line += f" {data.BASE} value {money(p['value_base'])}, P&L {money(p['unrealised_base'])}."
        if p["value_base"] is None:
            total_ok = False
        else:
            total += Decimal(p["value_base"])
        if not compact and p["mark_source"]:
            line += f" Source: {p['mark_source']} {p['mark_url']}; FX {p['fx_date'] or p['mark_date']} (ECB reference)."
        lines.append(line)
        if not compact:
            trades = store.trade_rows(p["symbol"])
            live = [t for t in trades if not t["voided"]][-6:]
            if live:
                lines.append(f"  {p['symbol']} trades: " + "; ".join(
                    f"#{t['id']} {t['side']} {_plain(t['shares'])}"
                    + (f" at {_plain(t['price'])} {t['currency']}" if t["side"] != "split" else "")
                    + f" on {t['traded_on']}" for t in live)
                    + (f"; {sum(t['voided'] for t in trades)} cancelled" if any(t["voided"] for t in trades) else "") + ".")
    if total_ok and len(dates) == 1:
        lines.append(f"Tracked total: {data.BASE} {money(total)}, close {next(iter(dates))}.")
    elif any(Decimal(p["shares"] or "0") for p in rows):
        lines.append("Total unavailable: missing marks/FX, a held split, or mixed close dates.")
    lines.append(f"{data.BASE} figures use the ECB reference rate of each trade's date "
                 "(opening balances: the date recorded).")
    for t in store.current_theses(symbol, open_only=True):
        lines.append(f"T{t['id']} {t['instrument']} [{t['source']}, {t['status']}]: {t['why']} (horizon: {t['horizon'] or 'unspecified'}).")
        if not compact:
            from backend.tools import vault
            note=f"{t['instrument'].lower()}-thesis.md"
            if vault._resolve('topics',note).is_file():
                lines.append(f"Latest research note: topics/{note}; use vault_read to inspect its dated sources when reviewing the thesis.")
    today = lifelog.local_date()
    for c in store.conditions():
        if not symbol or c["instrument"] == symbol:
            lines.append(_condition_line(c, today))
    if compact:
        # Bounded by whole lines: never cut a value in the middle and present it as exact.
        kept, chars = [], 0
        for line in lines:
            if chars + len(line) + 1 > 1100:
                kept.append("More positions/theses exist; use portfolio show for the full ledger.")
                break
            kept.append(line)
            chars += len(line) + 1
        return "\n".join(kept)
    return "\n".join(lines)


def context_text(text: str, signals=None) -> str:
    try:
        if relevant(text) or (signals is not None and signals.get("portfolio")):
            return show_text(compact=True)
    except Exception as exc:
        log.warning("portfolio context unavailable: %s", exc)
        return "Portfolio ledger unavailable; do not invent holdings or calculations."
    return ""


async def _rates(day: str):
    # Callers carry on without a rate (a close is exact in its own currency), so a failure is
    # logged here: swallowed silently, it once hid a ledger with no rate at all for two weeks.
    try:
        rows = await data.get_fx(day)
        store.save_fx(rows)
    except Exception as exc:
        log.warning("portfolio: ECB rates for %s not stored (%s: %s)", day, type(exc).__name__, exc)
        raise


async def identify_instrument(symbol: str) -> dict:
    """Unknown *exact tickers* may be verified by exchange metadata, never name search."""
    try:
        return resolve_instrument(symbol)
    except ValueError:
        supplied = str(symbol or "").strip()
        if supplied.lower() in AMBIGUOUS or not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", supplied):
            raise
        import httpx
        from urllib.parse import quote as url_quote
        async with httpx.AsyncClient(timeout=12, headers=data.HEADERS) as client:
            response = await client.get("https://query1.finance.yahoo.com/v8/finance/chart/" + url_quote(supplied, safe=""),
                                        params={"range":"5d","interval":"1d"})
            response.raise_for_status()
            meta = response.json()["chart"]["result"][0]["meta"]
        exchanges = {"NMS":"NASDAQ","NGM":"NASDAQ","NCM":"NASDAQ","NYQ":"NYSE","PAR":"XPAR","AMS":"XAMS","GER":"XETR"}
        exchange = exchanges.get(meta.get("exchangeName"))
        if meta.get("symbol") != supplied or not exchange or meta.get("currency") not in ("USD","EUR","GBP","CHF"):
            raise ValueError("ticker/exchange/currency could not be verified; no holding recorded")
        return {"symbol":supplied,"exchange":exchange,"currency":meta["currency"],"price_key":supplied,
                "name":meta.get("longName") or meta.get("shortName") or supplied}


def verify_replacement(row: dict, instrument: dict, q: str, old: dict) -> None:
    """A trade replacing one he says is wrong. Live (3 October 2026): "that's wrong, it's 20
    shares not 200, same price" names neither the stock nor the price, so the ordinary check
    refused the replacement and the correction failed. What he leaves out comes from the
    cancelled trade, which was itself recorded from his words; what changed must be in this
    message. No hypothetical check here: "20 not 200" is exactly how a correction reads."""
    if instrument["symbol"] != old["instrument"]:
        verify_trade({**row, "quote": q}, instrument)      # a different stock: the full rule
        return
    if "?" in q:
        raise ValueError("a question is not a correction")
    numbers = [f[2] for f in _figures(q)]
    qty = data.number(row.get("shares"))
    price = data.number(row.get("price", 0), zero=row.get("side") == "split")
    fees = data.number(row.get("fees", 0), zero=True)
    for name, value, before in (("share count", qty, old["shares"]), ("price", price, old["price"]),
                                ("fees", fees, old["fees"])):
        if value != Decimal(before) and value not in numbers:
            raise ValueError(_owner.say(f"the corrected {name} must be in «his» words (or unchanged from trade #{old['id']})"))
    if qty != Decimal(old["shares"]) and price != Decimal(old["price"]) and qty == price and numbers.count(qty) < 2:
        raise ValueError(_owner.say("the corrected share count and price must both be in «his» words"))
    side = row.get("side") or old["side"]
    if side != old["side"] and side not in _sides(q) and not (side == "opening" and old["side"] == "buy"):
        raise ValueError(_owner.say(f"«his» words must say it was a {side}, not a {old['side']}"))
    currency = str(row.get("currency") or old["currency"]).upper()
    said = {"EUR": r"€|\b(?:eur|euros?)\b", "USD": r"\$|\b(?:usd|dollars?)\b"}.get(currency, re.escape(currency))
    if currency != old["currency"] and not re.search(said, q, re.I):
        raise ValueError(_owner.say("a changed currency must be in «his» words"))


async def _prepare_trade(raw, run, salt: str = "", replacing: dict | None = None,
                         from_card: bool = False) -> dict:
    """One trade, checked against his words and converted to the stock's own currency.
    `replacing`: the trade this one corrects (see verify_replacement). `from_card`: figures
    he approved on a card (propose_trade), so his words are not what is checked."""
    if not isinstance(raw, dict) or set(raw) - _TRADE_FIELDS:
        raise ValueError("each trade must be an object with the documented fields")
    if from_card and (replacing is not None or raw.get("side") == "split"):
        raise ValueError("a card records a buy, sale or holding, never a correction or split")
    if replacing is not None:
        raw = {"symbol": replacing["instrument"], "side": replacing["side"], "currency": replacing["currency"],
               "fees": replacing["fees"], "shares": replacing["shares"], "price": replacing["price"],
               **{k: v for k, v in raw.items() if v not in (None, "")}}
    i = await identify_instrument(raw.get("symbol", ""))
    if from_card:
        q = str(raw.get("quote") or "")
        if not q.startswith(_CARD_QUOTE):
            raise ValueError(_owner.say("only a trade «owner» approved on a card is recorded without «his» words"))
    elif replacing is not None:
        q = _verified(str(raw.get("quote") or ""))
        verify_replacement(raw, i, q, replacing)
    else:
        q = verify_trade(raw, i)
    stated_day = raw.get("traded_on")
    if replacing is not None and not stated_day:
        stated_day = replacing["traded_on"]          # same day as the trade it corrects
    day = date.fromisoformat(stated_day or lifelog.local_date()).isoformat()
    if day > lifelog.local_date():
        raise ValueError("future trades are not executed holdings")
    if (stated_day and day != lifelog.local_date() and not from_card and not _date_stated(day, q)
            and not (replacing is not None and day == replacing["traded_on"])):
        raise ValueError(_owner.say("a backdated trade needs its date in «his» words (e.g. '12 September' or 2026-09-12)"))
    currency = str(raw.get("currency") or i["currency"]).upper()
    conversion = Decimal(1)
    # Fetching FX can fail while a native-currency trade remains perfectly valid.
    if not store.fx_rate(i["currency"], day) or not store.fx_rate(currency, day):
        try:
            await _rates(day)
        except Exception:
            pass
    if currency != i["currency"]:
        own, native = store.fx_rate(currency, day), store.fx_rate(i["currency"], day)
        if own is None or native is None:
            raise ValueError("dated FX unavailable for the stated trade currency; nothing recorded")
        conversion = Decimal(own["rate"]) / Decimal(native["rate"])
    side = raw.get("side", "opening")
    price = str(data.number(raw.get("price", "0"), zero=side == "split"))
    qty = str(data.number(raw.get("shares")))
    fee = str(data.number(raw.get("fees", "0"), zero=True))
    if side == "split" and (Decimal(price) or Decimal(fee)):
        raise ValueError("a split changes share count only, with zero price and fees")
    canonical = [format(Decimal(v).normalize(),"f") for v in (qty,price,fee)]
    key = hashlib.sha256(json.dumps([run or policy.owner_words(), i["symbol"], side, canonical, currency, day, salt]).encode()).hexdigest()
    return {"instrument": i, "quote": q, "side": side, "shares": qty,
            "price": price, "currency": currency, "fees": fee,
            "native_price": str(Decimal(price)*conversion), "native_fees": str(Decimal(fee)*conversion),
            "conversion_rate": str(conversion), "traded_on": day, "dedup_key": key}


async def portfolio(action: str, symbol: str = "", trades: list | None = None,
                    why: str = "", source: str = "owner", quote: str = "", horizon: str = "",
                    thesis_id: int = 0, kind: str = "", direction: str = "wrong_if", level: str = "",
                    criterion: str = "", due_date: str = "", probability: str = "", status: str = "closed",
                    trade_id: int = 0, split_date: str = "") -> str:
    try:
        if action == "show":
            return show_text(resolve_instrument(symbol)["symbol"] if symbol else "")
        if action == "history":
            i = resolve_instrument(symbol)
            return json.dumps(store.history(i["symbol"]), ensure_ascii=False)
        if action == "record":
            return record_text()
        if action == "import_candidates":
            return await import_candidates()
        if action == "record_trade":
            if not isinstance(trades, list) or not 1 <= len(trades) <= 20:
                raise ValueError("trades must contain 1–20 entries, each with its own quote")
            run = operations.current_run_id.get()
            # Carrying out a card he approved (propose_trade): his tap stated the figures.
            approved = policy.current_caller.get() == policy.APPROVED
            prepared, already = [], []
            for raw in trades:
                same = _already_recorded(raw)
                if same is not None and (same["side"] == "opening" or approved):
                    already.append(same)            # restated holding: never a second opening
                    continue
                try:
                    prepared.append(await _prepare_trade(raw, run, from_card=approved))
                except ValueError:
                    # A redo whose words were in an earlier message. Words he says NOW that fail
                    # for another reason (fees not said as fees) may be a new trade: that error stands.
                    if same is None or _said_now(raw):
                        raise
                    already.append(same)
            ids = store.record_trades(prepared, run_id=run) if prepared else []
            have = ("Already in the ledger, not recorded again: "
                    + "; ".join(f"#{t['id']} {t['instrument']} {t['side']} {_plain(t['shares'])} at "
                                f"{_plain(t['price'])} {t['currency']} on {t['traded_on']}" for t in already)
                    + ".") if already else ""
            if not ids:
                return have + "\n" + show_text()
            return ("Confirmed: recorded and verified trades " + ", ".join(f"#{n}" for n in ids) + "."
                    + (" " + have if have else "") + "\n" + show_text())
        if action == "propose_trade":
            return await _propose_trades(trades)
        if action == "correct_trade":
            return await _correct_trade(int(trade_id or 0), quote, trades)
        if action in ("confirm_split", "dismiss_split"):
            return _decide_split(action, symbol, split_date, quote)
        if action in ("set_thesis", "revise_thesis", "close_thesis"):
            i = await identify_instrument(symbol)
            q = _verified(quote) if source == "owner" or action == "close_thesis" else ""
            if source not in ("owner", "my analysis"):
                raise ValueError("source must be owner or my analysis")
            if action == "close_thesis" and not re.search(r"\b(close|closed|broken|broke|abandon|abandoned|end|ended)\b", q, re.I):
                raise ValueError(_owner.say("closing a thesis requires «his» current words asking to close it or saying how it ended"))
            if action != "close_thesis" and not why.strip():
                raise ValueError("state the thesis reasons")
            if source == "owner" and action != "close_thesis" and policy.norm_words(why) not in policy.norm_words(q):
                raise ValueError(_owner.say("keep «his» reasons in «his» actual words; mark your own reading 'my analysis'"))
            if action != "set_thesis" and not thesis_id:
                raise ValueError("revise/close requires the current thesis_id")
            next_status = "open" if action == "set_thesis" else "revised" if action == "revise_thesis" else status
            if action == "close_thesis" and next_status not in ("closed", "broken", "abandoned"):
                raise ValueError("close status must be closed, broken or abandoned")
            if action=='close_thesis' and next_status in ('broken','abandoned'):
                pattern = r'\b(broken|broke)\b' if next_status=='broken' else r'\babandon(?:ed)?\b'
                if not re.search(pattern,q,re.I):
                    raise ValueError(_owner.say('the thesis ending must match «his» words'))
            tid = store.set_thesis(i, why or q, source, q, lifelog.short(horizon), next_status,
                                   thesis_id or None)
            if action == "close_thesis":
                from backend.storage import watches
                roots = {t["root_id"] or t["id"] for t in store.current_theses(i["symbol"]) if t["id"]==tid}
                for c in store.conditions():
                    t = store.connection().execute("SELECT * FROM theses WHERE id=?", (c["thesis_id"],)).fetchone()
                    if c["watch_id"] and (t["root_id"] or t["id"]) in roots:
                        watches.update(c["watch_id"], state=watches.CANCELLED)
            return f"Confirmed: thesis T{tid} saved and verified [{source}, {next_status}]."
        if action == "add_condition":
            q = _verified(quote)
            return add_condition(thesis_id, kind, direction, level, criterion, due_date, probability, q)
        raise ValueError(f"unknown portfolio action {action!r}")
    except Exception as exc:
        log.warning("portfolio %s: %s", action, exc)
        return f"Error: {exc}"


_CARD_SIDES = {"opening": "holding", "buy": "bought", "sell": "sold"}


async def _propose_trades(trades: list | None) -> str:
    """Put trades whose figures he did not state in words to him as one card: Approve records
    exactly these, Deny records nothing. Live (28 October 2026): he sold SPCX and sent the broker
    confirmation as a screenshot; the ledger rightly refused figures that were only in the
    picture, and it took him three more messages to type them. The figures still are not taken
    from the picture: they are shown to him, and his tap is what states them."""
    from backend.brain import attachments
    from backend.tools import approval_flow
    if policy.current_caller.get() != policy.LIVE:
        raise ValueError(_owner.say("a trade card is only put to «him» from a conversation with «him»"))
    if not isinstance(trades, list) or not 1 <= len(trades) <= 10:
        raise ValueError("trades must contain 1-10 entries")
    said = " ".join(policy.owner_words())
    sent_file = bool(re.search(r"\[Sent (image|document)", said)) or attachments.any_in_view()
    ready, already, lines = [], [], []
    for raw in trades:
        if not isinstance(raw, dict):
            raise ValueError("each trade must be an object with the documented fields")
        raw = {k: v for k, v in raw.items() if k != "quote"}
        i = await identify_instrument(raw.get("symbol", ""))
        if not sent_file and not re.search(_alias_pattern(i), said, re.I):
            raise ValueError(_owner.say(f"a card needs «his» figures in front of «him»: an image or document «he» sent, "
                             f"or «his» message naming {i['symbol']}"))
        same = _already_recorded(raw)
        if same is not None:
            already.append(same)
            continue
        side = raw.get("side", "opening")
        if side not in _CARD_SIDES:
            raise ValueError("a card records a buy, sale or holding, never a split or correction")
        trade = await _prepare_trade({**raw, "quote": _CARD_QUOTE}, None, from_card=True)
        if side == "sell":
            rows = store.positions(i["symbol"])
            held = Decimal(rows[0]["shares"] or "0") if rows else Decimal(0)
            if Decimal(trade["shares"]) > held:
                raise ValueError(_owner.say(f"the ledger holds {_plain(held)} {i['symbol']}, fewer than the "
                                 f"{_plain(trade['shares'])} sold: check the figures with «him»"))
        line = (f"{_CARD_SIDES[side]} {_plain(trade['shares'])} {i['symbol']} at {_plain(trade['price'])} "
                f"{trade['currency']}" + (f", fees {_plain(trade['fees'])}" if Decimal(trade['fees']) else "")
                + (f" on {trade['traded_on']}" if raw.get("traded_on")
                   else f" today ({trade['traded_on']})"))
        lines.append(line)
        ready.append({"symbol": i["symbol"], "side": side, "shares": trade["shares"], "price": trade["price"],
                      "currency": trade["currency"], "fees": trade["fees"], "traded_on": trade["traded_on"],
                      "quote": _CARD_QUOTE + line})
    have = ("Already in the ledger, no card needed: " + "; ".join(
        f"#{t['id']} {t['instrument']} {t['side']} {_plain(t['shares'])} at {_plain(t['price'])} "
        f"{t['currency']} on {t['traded_on']}" for t in already) + ".") if already else ""
    if not ready:
        return have + "\n" + show_text()
    label = "Record in your portfolio: " + "; ".join(lines) + "."
    why = ("The ledger only takes figures you state yourself. These are the ones I read"
           + (" from what you sent" if sent_file else " from your message")
           + ": check them, and your tap records exactly these. Deny records nothing.")
    row = await approval_flow.request(context="portfolio", tool="portfolio",
                                      args={"action": "record_trade", "trades": ready},
                                      risk=policy.OWN, why=why, label=label)
    if row is None:
        raise ValueError(_owner.say("the card could not be stored, so nothing was put to «him»; ask «him» to say "
                         "the trade in one sentence instead"))
    return (approval_flow.waiting_text(row, policy.LIVE)
            + _owner.say(f" Not recorded yet: the ledger changes only if «he» taps Approve on card #{row['id']}.")
            + (" " + have if have else ""))


def _said_now(raw) -> bool:
    try:
        _verified(str(raw.get("quote") or ""))
        return True
    except ValueError:
        return False


def _already_recorded(raw) -> dict | None:
    """The live trade with exactly these figures, if the ledger has one.

    Live (3 October 2026): asked after the fact to "do it now", JARVIS re-recorded five
    holdings from the previous message, the quote check refused it, and he told the owner the
    ledger was empty -- it held all five. Saying that the trade is already there is a read,
    not a write, so it needs no quote."""
    if not isinstance(raw, dict):
        return None
    try:
        i = resolve_instrument(raw.get("symbol", ""))
        side = raw.get("side", "opening")
        qty = data.number(raw.get("shares"))
        price = data.number(raw.get("price", 0), zero=side == "split")
        fees = data.number(raw.get("fees", 0), zero=True)
    except (ValueError, TypeError):
        return None
    currency = str(raw.get("currency") or i["currency"]).upper()
    # One opening per stock, whatever its date; a buy or sale is the same trade only on the same day.
    day = None if side == "opening" else (raw.get("traded_on") or lifelog.local_date())
    return next((t for t in store.trade_rows(i["symbol"]) if not t["voided"] and t["side"] == side
                 and Decimal(t["shares"]) == qty and Decimal(t["price"]) == price
                 and Decimal(t["fees"]) == fees and t["currency"] == currency
                 and (day is None or t["traded_on"] == day)), None)


async def _correct_trade(trade_id: int, quote: str, trades: list | None) -> str:
    """Cancel a recorded trade he says is wrong, and record what replaces it, together."""
    if not trade_id:
        raise ValueError("correct_trade needs trade_id: the number of the recorded trade (portfolio show lists them)")
    old = store.trade(trade_id)
    if old is None:
        raise ValueError(f"no recorded trade #{trade_id}")
    if old["voided"]:
        raise ValueError(f"trade #{trade_id} is already cancelled")
    q = _verified(quote)
    if not _CORRECTION_CUE.search(q):
        raise ValueError(_owner.say("cancelling a recorded trade needs «his» words saying it is wrong "
                         "(e.g. \"that's wrong, it was 20 shares\")"))
    if trades is not None and (not isinstance(trades, list) or len(trades) > 5):
        raise ValueError(_owner.say("trades holds at most 5 replacement entries, each with «his» quote"))
    run = operations.current_run_id.get()
    # Salted: a replacement with the same figures as the cancelled trade is a new fact.
    prepared = [await _prepare_trade({"quote": q, **raw} if isinstance(raw, dict) else raw, run,
                                     salt=f"replaces:{trade_id}", replacing=old) for raw in (trades or [])]
    ids = store.void_trade(trade_id, q, q, run_id=run, replacements=prepared)
    what = f"{old['side']} {_plain(old['shares'])} {old['instrument']}" + (
        f" at {_plain(old['price'])} {old['currency']}" if old["side"] != "split" else "")
    text = f"Confirmed: trade #{trade_id} ({what} on {old['traded_on']}) cancelled"
    if ids:
        text += "; recorded " + ", ".join(f"#{n}" for n in ids) + " in its place"
    return text + ".\n" + show_text(old["instrument"])


def _decide_split(action: str, symbol: str, split_date: str, quote: str) -> str:
    """Confirm or dismiss a split the feed reported. Authorised by his words in a live turn,
    or by the approval request he tapped (the gateway runs that as the approved caller)."""
    if not str(symbol or "").strip():
        # Live: a second call without the stock errored and JARVIS reported the (successful)
        # first one as failed. With only one stock concerned, the stock is not in doubt.
        concerned = {s["instrument"] for s in store.splits()
                     if s["state"] == "pending" or (split_date and s["split_date"] == split_date)}
        if len(concerned) != 1:
            raise ValueError("say which stock's split (symbol)")
        symbol = concerned.pop()
    i = resolve_instrument(symbol)
    approved = policy.current_caller.get() == policy.APPROVED
    if not approved:
        q = _verified(quote)
        cue = _CONFIRM_CUE if action == "confirm_split" else _DISMISS_CUE
        if not cue.search(q):
            raise ValueError(_owner.say("«his» words must confirm the split (or, to dismiss it, say the feed is wrong)"))
    else:
        q = _owner.say("approved by «owner» on Discord")
    known = store.splits(i["symbol"])
    chosen = [s for s in known if s["split_date"] == split_date] if split_date else \
        [s for s in known if s["state"] == "pending"][:1]
    if not chosen:
        raise ValueError(f"no split of {i['symbol']} is waiting" + (f" on {split_date}" if split_date else ""))
    s = chosen[0]
    if action == "confirm_split":
        if s["state"] == "confirmed":
            return f"Confirmed: the {i['symbol']} split on {s['split_date']} is already recorded (trade #{s['trade_id']})."
        before = store.positions(i["symbol"])[0]["shares"]
        tid = store.confirm_split(i["symbol"], s["split_date"], q, run_id=operations.current_run_id.get())
        after = store.positions(i["symbol"])[0]["shares"]
        return (f"Confirmed: recorded {i['symbol']}'s {s['numerator']}-for-{s['denominator']} split on {s['split_date']} "
                f"(trade #{tid}): {_plain(before)} shares are now {_plain(after)}, at the same total cost. Price "
                "conditions are read in post-split shares from that date.\n" + show_text(i["symbol"]))
    if s["state"] != "pending":
        raise ValueError(f"the {i['symbol']} split on {s['split_date']} is {s['state']}, not waiting")
    store.dismiss_split(i["symbol"], s["split_date"])
    return (f"Confirmed: dismissed the reported {i['symbol']} split on {s['split_date']}; its figures and alerts "
            "use the closes as they are.\n" + show_text(i["symbol"]))


def add_condition(thesis_id, kind, direction, level, criterion, due_date, probability, quote):
    from backend.storage import watches as ws, forecasts as fs
    from backend.brain import forecasts
    if kind not in ("price_below", "price_above", "dated_event", "metric") or direction not in ("wrong_if", "right_if"):
        raise ValueError("invalid condition kind/direction")
    current = next((t for t in store.current_theses(open_only=True) if t["id"] == thesis_id), None)
    if current is None:
        raise ValueError("condition requires a current open thesis")
    now = time.time()
    ts, day = lifelog.stamp(now)
    criterion = lifelog.short(criterion)
    if ((re.search(r"\bright if\b",quote,re.I) and direction!="right_if") or
            (re.search(r"\bwrong if\b",quote,re.I) and direction!="wrong_if")):
        raise ValueError(_owner.say("right_if/wrong_if must match «his» condition"))
    if kind.startswith("price_"):
        level = str(data.number(level))
        if Decimal(level) not in [_decimal(m.group()) for m in _NUMBER.finditer(quote)]:
            raise ValueError(_owner.say("the price level must appear in «his» quote"))
        direction_word = r"\b(below|under|less than)\b" if kind=="price_below" else r"\b(above|over|more than)\b"
        if not re.search(direction_word, quote, re.I):
            raise ValueError(_owner.say("the threshold direction must match «his» words"))
        instrument=resolve_instrument(current['instrument'])
        if ('€' in quote or re.search(r'\bEUR\b',quote,re.I)) and instrument['currency']!='EUR':
            raise ValueError('daily-close conditions use the instrument currency; ask for its native price level')
        if ('$' in quote or re.search(r'\bUSD\b',quote,re.I)) and instrument['currency']!='USD':
            raise ValueError('daily-close conditions use the instrument currency; ask for its native price level')
        criterion = criterion or f"{current['instrument']} closes {'below' if kind=='price_below' else 'above'} {level}"
    else:
        if len(criterion.strip()) < 10:
            raise ValueError("provide a concrete checkable criterion")
        due = date.fromisoformat(due_date)
        if policy.norm_words(criterion) not in policy.norm_words(quote):
            raise ValueError(_owner.say("the criterion must be copied from «his» condition quote"))
        if due_date not in quote and not (str(due.day) in quote and due.strftime("%B").lower() in quote.lower()):
            raise ValueError(_owner.say("due date must match the date «he» gave in «his» quote"))
        if due <= date.fromisoformat(day) or due > date.fromisoformat(day)+timedelta(days=365):
            raise ValueError("dated conditions must be due within the next year")
    ws._conn()                         # schemas ready BEFORE our atomic transaction
    fs._conn()
    forecast_cal = None
    if probability and not kind.startswith("price_"):
        p = forecasts.parse_probability(probability)
        if p is None or not forecasts.P_MIN < p < forecasts.P_MAX:
            raise ValueError("probability must be between 2% and 98%")
        forecast_cal = (p, forecasts.recalibrate(p, fs.query(state=fs.RESOLVED, domain="markets")))
    conn = store.connection()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM thesis_conditions WHERE thesis_id=? AND kind=? AND direction=? AND criterion=? AND due_date=?",
                                (thesis_id, kind, direction, criterion, due_date)).fetchone()
        if existing:
            return f"Confirmed: condition C{existing['id']} already recorded; its existing link is reused."
        cur = conn.execute("INSERT INTO thesis_conditions(thesis_id,kind,direction,level,criterion,due_date,ts,local_date)"
                           " VALUES (?,?,?,?,?,?,?,?)", (thesis_id, kind, direction, level or None, lifelog.short(criterion), due_date, ts, day))
        cid = cur.lastrowid
        wid = fid = None
        if probability and not kind.startswith("price_"):
            p, cal = forecast_cal
            settle = datetime.combine(due+timedelta(days=1), datetime.min.time(), data.LOCAL_TZ).timestamp()
            fid = fs.add(claim=criterion, criteria=criterion, domain="markets", raw_p=p, stated_p=cal["p"],
                         method="web", check_query=f"{current['instrument']} {criterion} {due_date}",
                         resolve_by=settle, source_quote=quote, now=ts, connection=conn)
            if not fid:
                raise ValueError("could not create linked forecast")
        else:
            due_ts = datetime.combine(due, datetime.min.time(), data.LOCAL_TZ).timestamp() if due_date else ts
            wid = ws.add(subject=current["instrument"], condition=criterion,
                         method="portfolio_close" if kind.startswith("price_") else "web",
                         query=f"{current['instrument']} {criterion} {due_date}", every_s=86400,
                         expires_at=due_ts+8*86400 if due_date else ts+10*365*86400,
                         origin_text=quote, now=ts, connection=conn)
            if not wid:
                raise ValueError("could not create linked watch")
            conn.execute("UPDATE watches SET next_check_at=? WHERE watch_id=?", (due_ts, wid))
        conn.execute("UPDATE thesis_conditions SET watch_id=?,forecast_id=? WHERE id=?", (wid, fid, cid))
        store.event(thesis_id, "condition_added", criterion, "owner", f"condition:{cid}", condition_id=cid, now=ts)
    return f"Confirmed: condition C{cid} saved and verified; linked {'forecast F'+str(fid) if fid else 'watch '+wid}."


async def import_candidates() -> str:
    """Read suggestions only. Neither mem0 nor old assistant prose can create a trade."""
    from backend.brain import memory
    facts = await memory.search_facts(_owner.say("«Owner» owns holds shares portfolio NVDA Take-Two positions"), top_k=10)
    lines = [_owner.say("Unconfirmed candidates — nothing imported. Ask «owner» to confirm the full shares, cost and currency in a new message:")]
    lines += ["User-attributed memory: " + lifelog.short(f["text"]) for f in facts
              if f.get("attributed_to") == "user" and not f.get("from_assistant", True)]
    conn = store.connection()
    for row in conn.execute("SELECT timestamp,user_text FROM exchanges WHERE "
                            "(user_text LIKE '%NVDA%' OR user_text LIKE '%Take-Two%' OR user_text LIKE '%shares%' OR user_text LIKE '%portfolio%') "
                            "ORDER BY timestamp DESC LIMIT 15"):
        lines.append(f"User message {lifelog.local_date(row['timestamp'])}: {lifelog.short(row['user_text'])}")
    return "\n".join(lines) if len(lines)>1 else "No user-attributed import candidates found. Nothing imported."


async def _tell(text: str) -> bool:
    if _notifier is None:
        return False
    try:
        return bool(await _notifier(text))
    except Exception:
        return False


def _price_hit(conn, c: dict) -> tuple[dict, Decimal] | None:
    """The first close since the condition was set that crosses its level (in that day's
    shares). Stops at a split the ledger has not recorded, and at a split-sized jump the
    split check has not yet covered."""
    symbol = c["instrument"]
    held = store.pending_split_day(symbol)
    checked = store.checked_through(symbol) or ""
    prev = None
    for m in conn.execute("SELECT * FROM marks WHERE instrument=? ORDER BY local_date", (symbol,)):
        if held and m["local_date"] >= held:
            break
        if m["local_date"] >= c["local_date"]:
            close = Decimal(m["close"])
            if prev is not None and m["local_date"] > checked:
                move = close / Decimal(prev["close"])
                if not _JUMP_LOW <= move <= _JUMP_HIGH:
                    break                  # could be a split: wait until the split check has run
            level = _effective_level(c, m["local_date"])
            if (close < level) if c["kind"] == "price_below" else (close > level):
                return dict(m), level
        prev = m
    return None


async def check_conditions(now: float):
    from backend.storage import watches as ws, forecasts as fs
    ws._conn()
    fs._conn()
    for c in store.active_conditions():
        conn = store.connection()
        reason = source = ""
        if c["kind"].startswith("price_"):
            found = _price_hit(conn, c)
            if not found:
                continue
            mark, level = found
            adjusted = f" (level {_plain(level)} after a split)" if level != Decimal(c["level"]) else ""
            reason = f"{c['criterion']}{adjusted}: close {mark['close']} {mark['currency']} on {mark['local_date']}"
            source = mark["source"] + " " + mark["url"]
        elif c["watch_id"]:
            w = ws.get(c["watch_id"])
            if w and w["state"] == ws.EXPIRED:
                _settle(conn, c, "not_seen", "condition_not_seen",
                        f"no sign it happened by {lifelog.local_date(w['expires_at'])}, when the check stopped",
                        "watch " + w["watch_id"], now)
                continue
            if not w or w["state"] != ws.FIRED:
                continue
            reason, source = w["last_verdict"], "watch " + w["watch_id"]
        else:
            f = fs.get(c["forecast_id"])
            if f and f["state"] in (fs.UNRESOLVABLE, fs.WITHDRAWN):
                _settle(conn, c, "unresolved", "condition_unresolved",
                        f"the linked forecast could not be settled ({f['state']})",
                        "forecast F" + str(f["forecast_id"]), now)
                continue
            if not f or f["state"] != fs.RESOLVED:
                continue
            reason = f["resolution_quote"]
            source = "forecast F" + str(f["forecast_id"])
            if not f["outcome"]:
                _settle(conn, c, "not_hit", "condition_not_hit", reason, source, now)
                continue
        text = f"Thesis T{c['thesis_id']} {c['direction']} condition hit: {reason}. Source: {source}"
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            won = conn.execute("UPDATE thesis_conditions SET state='hit' WHERE id=? AND state='open'", (c["id"],)).rowcount
            if not won:
                continue
            store.event(c["thesis_id"], "condition_hit", reason, source, f"hit:{c['id']}", condition_id=c["id"], now=now)
            price_alert = c["kind"].startswith("price_")
            if price_alert and not ws.begin_alert(c["watch_id"], text, next_state=ws.FIRED, next_check_at=now+86400,
                                                verdict=reason, connection=conn):
                raise ValueError("could not record thesis alert; condition transaction rolled back")
        if price_alert:
            ws.finish_alert(c["watch_id"], await _tell(text))


def _settle(conn, c: dict, state: str, kind: str, reason: str, source: str, now: float) -> None:
    """An open condition that will not be hit: say how it ended, once."""
    with conn:
        if conn.execute("UPDATE thesis_conditions SET state=? WHERE id=? AND state='open'",
                        (state, c["id"])).rowcount:
            store.event(c["thesis_id"], kind, reason, source, f"settled:{c['id']}", condition_id=c["id"], now=now)


# ---------------------------------------------------------------------------
# The loop: closes, splits, FX, conditions, reviews
# ---------------------------------------------------------------------------

async def run_loop() -> None:
    from backend.brain import pacing
    # Not `pausable`: the cycle checks the pause itself, so a paused close is recorded as
    # skipped (and fetched after /resume) instead of silently not happening.
    await pacing.run_periodic(name="portfolio", check_interval_s=_CHECK_S, on_cycle=cycle)


async def cycle(now: datetime | None = None):
    """Bounded, code-only close fetches. Its own loop; holds on /pause (checked here)."""
    from backend.brain import pause
    if _cycle_lock.locked():
        return
    async with _cycle_lock:
        now = now or datetime.now(data.LOCAL_TZ)
        stamp = now.timestamp()
        paused = pause.is_paused("work")
        try:
            await _fetch_closes(now, stamp, paused)
            if not paused:
                await _check_splits(now, stamp)
                await _ask_about_splits(stamp)
                await _repair_fx()
                await check_conditions(stamp)
                await reviews_cycle(now)
        except Exception as exc:
            log.warning("portfolio cycle: %s", exc)


async def _fetch_closes(now: datetime, stamp: float, paused: bool) -> None:
    from backend.brain import pause
    conn = store.connection()
    count = 0
    for i in store.instruments():
        start = max(date.fromisoformat(i["local_date"]), now.date()-timedelta(days=_LOOKBACK_DAYS))
        if start > now.date():
            continue
        # Newest first: tonight's close matters more than filling an old gap.
        for day in reversed(data.sessions(i, start.isoformat(), now.date().isoformat())):
            due = data.fetch_at(i, day)
            if due > now:
                continue
            if conn.execute("SELECT 1 FROM marks WHERE instrument=? AND local_date=?", (i["symbol"], day)).fetchone():
                continue
            fetched = conn.execute("SELECT * FROM portfolio_fetches WHERE instrument=? AND local_date=?", (i["symbol"], day)).fetchone()
            if fetched and fetched["state"] in ("retry", "missing") and fetched["next_at"] > stamp:
                continue
            if paused:
                if not (fetched and fetched["state"] == "paused"):
                    with conn:
                        conn.execute("INSERT INTO portfolio_fetches VALUES (?,?, 'paused',0,?,'skipped during /pause',?) "
                                     "ON CONFLICT(instrument,local_date) DO UPDATE SET state='paused',reason='skipped during /pause',ts=excluded.ts",
                                     (i['symbol'],day,stamp,stamp))
                    pause.record_skip("portfolio", f"{i['symbol']} close {day} (fetched after /resume)")
                continue
            if count >= _MAX_FETCHES_PER_CYCLE:
                return
            count += 1
            attempts = int(fetched["attempts"]) + 1 if fetched else 1
            state, reason, next_at = "done", "", stamp
            try:
                mark = await data.get_closes(i, day)
                if mark["day"] != day:
                    raise ValueError("source returned a different session")
                try:
                    await _rates(day)
                except Exception:
                    pass           # a USD close is still exact without conversion to the base currency
                store.save_mark(i["symbol"], mark, now=stamp)
            except Exception as exc:
                reason = lifelog.short(str(exc))
                if stamp <= due.timestamp() + _MISSING_AFTER_S:
                    state, next_at = "retry", stamp + _RETRY_S
                else:
                    state = "missing"
                    next_at = stamp + (_MISSING_RETRY_S if attempts < _SLOW_RETRY_AFTER else _SLOW_RETRY_S)
            with conn:
                conn.execute("INSERT INTO portfolio_fetches VALUES (?,?,?,?,?,?,?) ON CONFLICT(instrument,local_date) "
                             "DO UPDATE SET state=excluded.state,attempts=excluded.attempts,next_at=excluded.next_at,reason=excluded.reason,ts=excluded.ts",
                             (i["symbol"], day, state, attempts, next_at, reason, stamp))


async def _check_splits(now: datetime, stamp: float) -> None:
    """Once per new close of a held stock, ask the feed for splits since his first trade.
    At most one feed call per cycle."""
    conn = store.connection()
    today = now.date().isoformat()
    for i in store.instruments():
        symbol = i["symbol"]
        first = store.first_trade_day(symbol)
        latest = conn.execute("SELECT max(local_date) FROM marks WHERE instrument=?", (symbol,)).fetchone()[0]
        if not first or not latest or (store.checked_through(symbol) or "") >= latest:
            continue
        if _split_next_try.get(symbol, 0) > stamp:
            continue
        try:
            found = await data.get_splits(i, first)
        except Exception as exc:
            _split_next_try[symbol] = stamp + _SPLIT_RETRY_S
            log.info("portfolio: split check for %s failed (%s); price alerts on a split-sized move wait", symbol, exc)
            return
        for s in found:
            if first < s["day"] <= today and store.note_split(symbol, s, now=stamp):
                log.warning("portfolio: %s %s-for-%s split on %s is not in the ledger; its figures are held",
                            symbol, s["numerator"], s["denominator"], s["day"])
        store.split_checked(symbol, latest, now=stamp)
        return


async def _ask_about_splits(stamp: float) -> None:
    """Put each new split to him once, as one tap (Approve records it)."""
    from backend.tools import approval_flow
    for s in store.splits(state="pending"):
        if s["asked"] or s["next_ask_at"] > stamp:
            continue
        shares = store.positions(s["instrument"])[0]["shares"] or "0"
        after = Decimal(shares) * Decimal(s["ratio"])
        label = (f"Record {s['instrument']}'s {s['numerator']}-for-{s['denominator']} split on {s['split_date']} "
                 f"in your portfolio: {_plain(shares)} shares become {_plain(after)}, same total cost.")
        why = (f"The price feed ({s['source']}) reports this split and your ledger doesn't have it. Until it is "
               f"recorded, {s['instrument']}'s value, P&L and price alerts stay at the last close before it, so the "
               "split can't look like a crash. If the feed is wrong, tell me and I'll dismiss it.")
        row = await approval_flow.request(context="portfolio", tool="portfolio",
                                          args={"action": "confirm_split", "symbol": s["instrument"],
                                                "split_date": s["split_date"]},
                                          risk=policy.OWN, why=why, label=label)
        store.mark_split_asked(s["instrument"], s["split_date"], row["id"] if row else None,
                               retry_at=stamp + _SPLIT_ASK_RETRY_S)


async def _repair_fx() -> None:
    # Marks and trades survive FX outages. Repair at most one missing dated
    # conversion per cycle; never substitute today's rate for an older one.
    conn = store.connection()
    missing_fx = conn.execute("SELECT i.currency,t.traded_on AS day FROM live_trades t JOIN instruments i ON i.symbol=t.instrument "
                             "UNION SELECT i.currency,m.local_date FROM marks m JOIN instruments i ON i.symbol=m.instrument ORDER BY day DESC").fetchall()
    for currency, day in missing_fx:
        if currency != data.BASE and not store.fx_rate(currency,day,_conn=conn):
            try:
                await _rates(day)
            except Exception:
                pass
            break


def scheduled_items(now: float) -> list[tuple[float, str]]:
    out = []
    conn = store.connection()
    for s in store.splits(state="pending"):
        out.append((now, f"portfolio: {s['instrument']} {s['numerator']}-for-{s['denominator']} split on "
                         f"{s['split_date']} waiting for your yes" + (f" (approval #{s['approval_id']})" if s["approval_id"] else "")
                         + "; its figures are held until then"))
    for i in store.instruments():
        start = datetime.fromtimestamp(now, data.LOCAL_TZ).date()
        for day in data.sessions(i, start.isoformat(), (start+timedelta(days=10)).isoformat()):
            due = data.fetch_at(i, day).timestamp()
            if (not conn.execute("SELECT 1 FROM marks WHERE instrument=? AND local_date=?", (i["symbol"], day)).fetchone()
                    and not conn.execute("SELECT 1 FROM portfolio_fetches WHERE instrument=? AND local_date=? AND state IN ('missing','paused')", (i['symbol'],day)).fetchone()):
                out.append((max(now,due), f"portfolio close: {i['symbol']} ({day})"))
                break
    for r in conn.execute("SELECT * FROM portfolio_fetches WHERE state IN ('missing','retry','paused') ORDER BY local_date DESC LIMIT 5"):
        if r["state"] == "paused":
            out.append((now, f"portfolio close {r['instrument']} {r['local_date']} skipped during /pause; fetched after /resume"))
        else:
            out.append((r["next_at"], f"portfolio {r['state']} mark: {r['instrument']} {r['local_date']} ({r['reason']}); tried again then"))
    for r in conn.execute("SELECT * FROM portfolio_reviews WHERE state IN ('waiting','submitting','interrupted','incomplete','failed') ORDER BY ts LIMIT 5"):
        label = "admission uncertain; not repeated" if r["state"]=="submitting" else r["state"]
        out.append((now, f"portfolio thesis review C{r['condition_id']}: {label} {r['reason']}"))
    return out


def brief_line() -> str:
    rows = store.positions()
    if not any(Decimal(p["shares"] or "0") for p in rows):
        return ""
    held = "".join(f"Portfolio: {s['instrument']} figures on hold: the feed reports a {s['numerator']}-for-"
                   f"{s['denominator']} split on {s['split_date']} that isn't recorded yet"
                   + (f" (approval #{s['approval_id']})" if s["approval_id"] else "") + ". "
                   for s in store.splits(state="pending"))
    daily = store.daily()
    if not daily or daily[-1]["value_base"] is None:
        return held + "Portfolio: marks or dated FX incomplete; total unavailable."
    last = daily[-1]
    latest_trade = store.connection().execute('SELECT max(traded_on) FROM live_trades').fetchone()[0]
    if latest_trade and latest_trade > last['date']:
        return held + (f"Portfolio: latest close {last['date']} predates recorded trades/opening balances "
                       f"on {latest_trade}; current total awaits a compatible close.")
    hits = store.connection().execute("SELECT count(*) FROM thesis_events WHERE kind='condition_hit' AND local_date=?", (last["date"],)).fetchone()[0]
    prior_value = daily[-2]["value_base"] if len(daily)>1 else None
    percent = (Decimal(last["change_base"])/Decimal(prior_value)*100
               if last["change_base"] is not None and prior_value is not None and Decimal(prior_value)>0 else None)
    change_label = f"{money(percent)}%" if percent is not None else 'daily change unavailable'
    details=[]
    for p in rows:
        if p['mark_date']==last['date'] and p['cost_basis'] and Decimal(p['cost_basis']):
            details.append(f"{p['symbol']} {money(Decimal(p['unrealised'])/Decimal(p['cost_basis'])*100)}% on recorded cost")
    upcoming=[c for c in store.active_conditions() if c['due_date'] and c['due_date']>=last['date']]
    if upcoming:
        next_event=min(upcoming,key=lambda c:c['due_date'])
        details.append(f"{next_event['instrument']} check {next_event['due_date']}: {next_event['criterion'][:60]}")
    return held + (f"Portfolio close {last['date']}: {change_label} ({data.BASE} {money(last['value_base'])}); "
                   f"change in total P&L {data.BASE} {money(last['change_base'])}; {hits} thesis condition(s) hit. " + '; '.join(details[:4]))


def record_text() -> str:
    rows = store.positions()
    if not rows:
        return "No portfolio history yet."
    lines = [f"{p['symbol']}: realised {money(p['realised'] or '0')}, unrealised {money(p['unrealised'])} {p['currency']}" for p in rows]
    for p in rows:
        history = store.position_daily(p['symbol'])
        for month in sorted({r['date'][:7] for r in history}):
            last = [r for r in history if r['date'].startswith(month)][-1]
            lines.append(f"{p['symbol']} {month}, last recorded {last['date']}: cumulative realised {money(last['realised'])}, unrealised {money(last['unrealised'])} {p['currency']}.")
    daily = store.daily()
    for month in sorted({r["date"][:7] for r in daily}):
        records = [r for r in daily if r["date"].startswith(month)]
        changes = [Decimal(r["change_base"]) for r in records if r["change_base"] is not None]
        complete = all(r["change_base"] is not None for r in records)
        lines.append(f"{month}: {data.BASE} P&L change {money(sum(changes)) if complete else 'incomplete history'}; {len(records)} marked dates.")
    theses = store.current_theses()
    hits = store.connection().execute("SELECT count(*) FROM thesis_events WHERE kind='condition_hit'").fetchone()[0]
    closed = [t for t in theses if t["status"] in ("closed", "abandoned", "broken")]
    lines.append(f"Theses: {len(closed)} ended, {hits} conditions hit; " + ", ".join(f"{s} {sum(t['status']==s for t in closed)}" for s in ("closed","broken","abandoned")) + ".")
    from backend.storage import forecasts as fs
    from backend.brain import forecasts
    linked = {c['forecast_id'] for c in store.conditions() if c['forecast_id']}
    scored = [f for f in fs.query(state=fs.RESOLVED) if f['forecast_id'] in linked]
    score = forecasts.summarize(scored)
    lines.append(f"Linked forecasts: {score['n']} resolved" + (f", Brier {score['brier']:.3f} (0 is perfect)." if score['n'] else "."))
    lines.append(f"{len(daily)} marked sessions; {len(closed)} ended theses. Too early to tell without a longer observed record; these are descriptive totals.")
    return "\n".join(lines)


async def reviews_cycle(now: datetime):
    """One durable post-event research job, with uncertain admissions never retried."""
    from backend.brain import background
    from backend.brain import pause
    from backend.storage import jobs
    from backend.tools import vault
    if pause.is_paused('work'):
        return
    conn = store.connection()
    stamp, day = lifelog.stamp(now.timestamp())
    for c in store.conditions():
        if not c["due_date"] or now.date() <= date.fromisoformat(c["due_date"]):
            continue
        review = conn.execute("SELECT * FROM portfolio_reviews WHERE condition_id=?", (c["id"],)).fetchone()
        if review is not None:
            if review["state"]=="waiting" and stamp-float(review["ts"]) >= 1800:
                review = None              # no job was admitted; safe to retry a full slot later
        if review is not None:
            if review["state"] != "submitted":
                continue
            job = jobs.get(review["job_id"])
            if not job or job["state"] not in ("done", "failed", "cancelled", "interrupted"):
                continue
            state, reason, source = job['state'], job.get('error') or '', f"background job {review['job_id']}"
            if state == 'done':
                result = json.loads(job.get('payload') or '{}')
                target = vault._resolve('topics', c['instrument'].lower()+'-thesis.md')
                candidates = [target]
                folder, _, filename = str(result.get('note_file') or '').partition('/')
                if folder in vault.FOLDERS and filename:
                    candidates.append(vault._resolve(folder,filename))
                content = next((p.read_text(encoding='utf-8') for p in candidates if p.is_file()
                                and p.stat().st_mtime >= float(review['ts'])
                                and re.search(r'https?://\S+',p.read_text(encoding='utf-8'))), '')
                if content:
                    written = await vault.vault_write(target.name,content,'topics')
                    if written.startswith('Error:') or not target.is_file() or target.read_text(encoding='utf-8') != content:
                        state, reason = 'incomplete', 'Research returned but its cited thesis note could not be verified'
                    else:
                        reason, source = result.get('summary') or 'Cited review saved', 'topics/'+target.name
                else:
                    state, reason = 'incomplete', 'Research returned without a newly written note containing source URLs'
            with conn:
                conn.execute("UPDATE portfolio_reviews SET state=?,reason=? WHERE condition_id=? AND state='submitted'", (state,reason,c['id']))
                if state=='done':
                    store.event(c['thesis_id'],'reviewed',reason,source,f"review:{c['id']}",condition_id=c['id'],now=stamp)
                elif state=='incomplete':
                    store.event(c['thesis_id'],'review_incomplete',reason,source,f"review:{c['id']}",condition_id=c['id'],now=stamp)
            continue
        current = store.current_theses(c["instrument"], open_only=True)
        original=conn.execute('SELECT id,root_id FROM theses WHERE id=?',(c['thesis_id'],)).fetchone()
        root=original['root_id'] or original['id']
        if not any((t['root_id'] or t['id'])==root for t in current):
            continue
        # Record intent before starting a job. A crash here leaves an uncertain admission;
        # it is visible in activity and never creates a duplicate research pass.
        with conn:
            won = conn.execute("INSERT OR IGNORE INTO portfolio_reviews VALUES (?,'submitting',NULL,'',?,?)", (c["id"],stamp,day)).rowcount
            if not won:
                won = conn.execute("UPDATE portfolio_reviews SET state='submitting',ts=?,local_date=? WHERE condition_id=? AND state='waiting' AND ts<=?",
                                   (stamp,day,c['id'],stamp-1800)).rowcount
        if not won:
            continue
        brief = (_owner.say(f"Review {c['instrument']} after {c['due_date']}: {c['criterion']}. "
                 f"Ledger context:\n{show_text(c['instrument'])}\n"
                 f"Read primary company/exchange sources using web_browse and fetch_page. Write topics/{c['instrument'].lower()}-thesis.md "
                 "with explicit source URLs, what happened, «owner's» quoted reasons and a separate 'my analysis' section. "
                 "Do not change trades or any ledger facts. If evidence is unavailable, say that; do not invent a result. "
                 "Finish with a short message stating what changed and citing the URLs. Give descriptive findings; use the supplied code values and percentages without recomputing them."))
        with policy.job_scope("background"):
            task, error = await background._start(f"Thesis review {c['instrument']} C{c['id']}", brief, notify=True)
        with conn:
            conn.execute("UPDATE portfolio_reviews SET state=?,job_id=?,reason=? WHERE condition_id=?",
                         ("submitted" if task else "waiting", task.job_id if task else None, error, c["id"]))
        break                           # at most one new research job per cycle

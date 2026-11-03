"""backend/brain/instructions.py — tell him once.

When the owner corrects JARVIS or states how he wants something done from now on, the
correction used to become, at best, one mem0 fact among ~1,100 -- retrieved only if a
later turn happened to be semantically close -- or a line in the portrait rewrite, if
consolidation kept it. So the same mistake could come back a week later.

A standing instruction is typed and applied to every turn in its scope:

    standing_instruction(add|list|retire)   the tool, called in the turn he says it
    turn_note(channel)                      the rules for this turn, attached to his message
    handle_rules_command(arg)               "/rules" and "/rules drop N" on Discord

Captured by a tool call in the same turn, not a separate classifier: JARVIS sees the
whole conversation when deciding what the rule is, and can acknowledge it in the same
reply. `personality.is_correction_request` nudges him toward the tool when the wording
sounds like a correction, the same pattern the persistence check uses.

Measured, not assumed: saying a rule again that is already held increments its
`times_restated` -- he had to repeat himself, so it was not followed. That count is
the honest number for whether this works.
"""
from __future__ import annotations
from backend import owner as _owner

import datetime
import logging

from backend.storage import instructions as store

log = logging.getLogger(__name__)

_MAX_ACTIVE = 25
_MAX_RULE_CHARS = 300
_BLOCK_CHAR_CAP = 2400
_RESTATE_SIMILARITY = 0.6      # token Jaccard; a restatement, not a related topic
_REVIEW_AFTER_DAYS = 120       # not reaffirmed in this long -> ask whether it still holds

_SCOPE_LABEL = {"always": "", "discord": "On Discord: ", "desk": "At the desk: "}
_CHANNEL_SCOPES = {"discord": ("always", "discord"), "local": ("always", "desk")}


def _date(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _origin_quote() -> str:
    try:
        from backend.context.origin import current_origin
        o = current_origin.get()
        return (o.user_text if o else "")[:500]
    except Exception:
        return ""


def _line(r: dict, *, with_meta: bool = False) -> str:
    label = _SCOPE_LABEL.get(r["scope"], "")
    if label and r["rule"].lower().startswith(label.lower().rstrip(": ")):
        label = ""                       # the rule already says where it applies
    text = f"[r{r['rule_id']}] {label}{r['rule']}"
    if not with_meta:
        return text + f" (since {_date(r['created_at'])})"
    meta = [f"since {_date(r['created_at'])}"]
    if r["times_restated"]:
        meta.append(_owner.say(f"«he» had to repeat it {r['times_restated']}x"))
    age_days = (datetime.datetime.now().timestamp() - r["reaffirmed_at"]) / 86400
    if age_days > _REVIEW_AFTER_DAYS:
        meta.append(f"not reaffirmed in {int(age_days)} days -- worth asking if it still holds")
    return f"{text} ({'; '.join(meta)})"


def _parse_id(value) -> int | None:
    try:
        return int(str(value).strip().lstrip("rR"))
    except (TypeError, ValueError):
        return None


def _restated(rid: int, rule: str, quote: str) -> str:
    store.restate(rid, rule, quote)
    again = store.get(rid) or {"rule_id": rid, "times_restated": "?"}
    return (_owner.say(f"Confirmed: this is rule r{again['rule_id']}, which I already hold -- «he» has "
            f"now had to repeat it {again['times_restated']}x. Acknowledge that you had not "
            "been following it."))


async def standing_instruction(action: str, rule: str = "", scope: str = "always",
                               replaces: str = "", restates: str = "",
                               rule_id: str = "") -> str:
    """Add, list or retire a standing instruction. Returns a confirmed result or an Error."""
    try:
        if action == "list":
            rules = store.active()
            if not rules:
                return "No standing instructions."
            return "Standing instructions:\n" + "\n".join(
                f"- {_line(r, with_meta=True)}" for r in rules)

        if action == "retire":
            rid = _parse_id(rule_id)
            if rid is None or not store.end(rid, store.RETIRED):
                return f"Error: no active rule {rule_id!r}. Use action='list'."
            return f"Retired rule r{rid}; it no longer applies."

        if action != "add":
            return f"Error: unknown standing_instruction action {action!r}."

        rule = " ".join(rule.split())
        if not rule:
            return "Error: a rule needs its text."
        if len(rule) > _MAX_RULE_CHARS:
            return f"Error: keep a rule under {_MAX_RULE_CHARS} characters -- one instruction each."
        if scope not in store.SCOPES:
            return f"Error: scope must be one of {', '.join(store.SCOPES)}."
        quote = _origin_quote()

        def _active_id(ref: str) -> int | None:
            rid = _parse_id(ref)
            if rid is None or (store.get(rid) or {}).get("state") != store.ACTIVE:
                return None
            return rid

        if restates:
            rid = _active_id(restates)
            if rid is None:
                return f"Error: no active rule {restates!r}. Use action='list'."
            return _restated(rid, rule, quote)

        old_id = _active_id(replaces) if replaces else None
        if replaces and old_id is None:
            return f"Error: no active rule {replaces!r} to replace. Use action='list'."

        if old_id is None:
            for existing in store.active((scope,)):
                if store.similarity(existing["rule"], rule) >= _RESTATE_SIMILARITY:
                    return _restated(existing["rule_id"], rule, quote)

        if len(store.active()) >= _MAX_ACTIVE:
            return f"Error: already {_MAX_ACTIVE} standing rules -- retire one first."
        new_id = store.add(rule, scope, quote)
        saved = store.get(new_id) if new_id else None
        if saved is None or saved["state"] != store.ACTIVE:
            return "Error: the rule was NOT saved (storage unavailable)."
        if old_id is not None:
            store.end(old_id, store.SUPERSEDED, superseded_by=new_id)
            return f"Confirmed: rule r{new_id} saved and verified; it replaces r{old_id}."
        return f"Confirmed: rule r{new_id} saved and verified -- {_line(saved)}."
    except Exception as e:
        log.warning("instructions: standing_instruction failed: %s", e)
        return f"Error: standing instructions unavailable ({e})"


_NOTE_HEADER = (
    _owner.say("[Standing instructions from «owner» that apply to this reply -- «he» told you these "
    "once and expects them to stick. Follow them without mentioning them, unless «his» "
    "message above says otherwise. The [rN] ids are for standing_instruction.]")
)


def render_block(channel: str | None = None) -> str:
    """The rule lines that apply on `channel`. None (unknown) includes every scope."""
    try:
        scopes = _CHANNEL_SCOPES.get(channel) if channel else None
        rules = store.active(scopes)
    except Exception:
        return ""
    lines, used = [], 0
    for r in rules:
        line = f"- {_line(r)}"
        if used + len(line) > _BLOCK_CHAR_CAP:
            log.warning("instructions: rule note cap reached; %d rule(s) not shown",
                        len(rules) - len(lines))
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def turn_note(channel: str | None = None) -> str:
    """The note appended to this turn's outgoing user message, or "" with no rules.

    On the user message rather than in the system prompt because that is where the model
    actually follows it -- see the measurement recorded at the call site in chat().
    Never raises: a storage problem means a turn without rules, not a failed turn.
    """
    try:
        lines = render_block(channel)
    except Exception:
        return ""
    return f"{_NOTE_HEADER}\n{lines}" if lines else ""


def handle_rules_command(arg: str) -> str:
    """'/rules' lists; '/rules drop N' retires one. Sync, like /pace and /brief."""
    arg = (arg or "").strip()
    if not arg:
        rules = store.active()
        if not rules:
            return "I'm not holding any standing rules yet."
        return "Rules I'm holding myself to:\n" + "\n".join(
            f"- {_line(r, with_meta=True)}" for r in rules) + "\n\nDrop one with /rules drop <number>."
    parts = arg.split()
    if parts[0].lower() in ("drop", "retire", "remove") and len(parts) == 2:
        rid = _parse_id(parts[1])
        if rid is not None and store.end(rid, store.RETIRED):
            return f"Dropped rule r{rid}."
        return f"No active rule {parts[1]!r}. Send /rules to see them."
    return "Usage: /rules, or /rules drop <number>."

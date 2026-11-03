"""What kind of input the running turn came from, for channels with no context object of their
own (Discord carries it in discord_context). Set by main.py around the turn; a ContextVar so
concurrent work never sees another turn's value."""
from backend import localtime as _localtime
from backend import owner as _owner
from contextvars import ContextVar

voice: ContextVar[bool] = ContextVar("turn_input_voice", default=False)
# The phone will read the reply aloud (it asked to): answer briefly, in plain sentences.
spoken: ContextVar[bool] = ContextVar("turn_input_spoken", default=False)
# Phone slice 4: the Inbox message he is answering ({"id", "ts", "kind", "text"}), or None.
reply_to: ContextVar[dict | None] = ContextVar("turn_input_reply_to", default=None)


def render_reply(notice: dict | None) -> str:
    """The message being answered, as context for the turn. JARVIS's own words, not the owner's."""
    if not notice or not notice.get("text"):
        return ""
    import datetime
    from zoneinfo import ZoneInfo
    when = datetime.datetime.fromtimestamp(float(notice.get("ts") or 0), _localtime.ZONE)
    return (_owner.say(f"[«owner» is answering a message you sent «him» on your own ({notice.get('kind', 'notice')}, "
            f"{when:%a %d %b %H:%M}), from «his» phone's Inbox. Resolve 'this', 'that' and follow-up "
            "questions against it first; older memories are background, not a replacement. "
            "These are your words, not something «he» said.]\n") + str(notice["text"])[:6000])

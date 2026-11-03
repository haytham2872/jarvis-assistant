"""Phone slice 4: where JARVIS's own messages go -- Discord, the phone, or both.

Every module that messages the owner unprompted (chronos, research, watches, forecasts, goals,
background jobs, the planner, ...) was handed `discord_bridge.send_to_owner` directly. Now each
gets `deliver(kind)`: the message is stored first (storage/notices.py, the app's Inbox), then
sent according to his setting:

  discord  -- what happened before: a DM, and the Inbox only keeps a copy (the default);
  both     -- the DM, and the phone is told too;
  phone    -- no DM. The phone is told; what it cannot show at once waits in the Inbox.

"The phone is told" means a live event to an open app, and a push notification when one has
been confirmed on the phone (an Expo push token whose test notification the app reported
seeing). Expo Go on iOS may or may not deliver remote pushes -- Expo's docs only rule them out
on Android -- so nothing relies on it: the app also schedules notifications itself for the
things with a known time (mobile/upcoming.py).

A message counts as delivered when it reached at least one place he will see it. With
"phone", storage failing falls back to Discord, so a broken database never silences a
reminder. The callers' own fallbacks (park it for the next conversation turn) still apply
when this returns False.

A finer kind than the module's (a reminder versus the brief, both from chronos) is set by the
caller with `labelled("reminder")`, the llm_calls.labelled pattern.
"""
from __future__ import annotations
from backend import owner as _owner

import asyncio
import contextlib
import logging
from contextvars import ContextVar
from typing import Awaitable, Callable, Optional

from backend.storage import notices as store

log = logging.getLogger(__name__)

TITLES = {
    "reminder": "Reminder", "brief": "Morning brief", "check": "Scheduled check",
    "job": "Background work", "research": "Research", "watch": "Watch", "forecast": "Forecast",
    "goal": "Goal", "checkin": "Evening check-in", "review": "Weekly review", "plan": "Your plan",
    "portfolio": "Portfolio", "profile": "Profile research", "pause": "Paused work",
    "system": "JARVIS", "notice": "JARVIS",
}
_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_PUSH_TIMEOUT_S = 10.0
_PUSH_BODY = 178            # what a lock screen shows in a few lines

_kind: ContextVar[Optional[str]] = ContextVar("notice_kind", default=None)
_pending: set = set()       # push tasks still running (asyncio keeps only weak references)


@contextlib.contextmanager
def labelled(kind: str):
    """Messages sent inside this block are stored under `kind`."""
    token = _kind.set(kind)
    try:
        yield
    finally:
        _kind.reset(token)


def _phone_event(notice: dict) -> None:
    try:
        from backend.mobile.events import HUB
        HUB.publish({"type": "notice", "notice": notice})
    except Exception as exc:
        log.debug("notices: live event not published (%s)", exc)


def push_text(kind: str, text: str) -> tuple[str, str]:
    """(title, body) for a lock screen. A reminder shows its own words; anything else its start."""
    flat = " ".join((text or "").split())
    if kind == "reminder" and flat.lower().startswith("reminder:"):
        flat = flat[len("reminder:"):].strip()
    if len(flat) > _PUSH_BODY:
        flat = flat[:_PUSH_BODY - 1].rstrip() + "…"
    return TITLES.get(kind, "JARVIS"), flat


async def _post_push(messages: list[dict]) -> list[dict]:
    import httpx
    async with httpx.AsyncClient(timeout=_PUSH_TIMEOUT_S) as client:
        res = await client.post(_PUSH_URL, json=messages,
                                headers={"Accept": "application/json"})
        res.raise_for_status()
        return list((res.json() or {}).get("data") or [])


async def push(tokens: list[str], title: str, body: str, data: dict) -> int:
    """Send one notification to each token through Expo's push service. Returns how many it
    accepted. A token Expo calls DeviceNotRegistered is forgotten. Never raises."""
    if not tokens:
        return 0
    messages = [{"to": t, "title": title, "body": body, "data": data, "sound": "default"}
                for t in tokens]
    try:
        tickets = await _post_push(messages)
    except Exception as exc:
        log.info("notices: push not sent (%s)", exc)
        for t in tokens:
            store.push_result(t, False)
        return 0
    accepted = 0
    for token, ticket in zip(tokens, tickets):
        ok = (ticket or {}).get("status") == "ok"
        gone = ((ticket or {}).get("details") or {}).get("error") == "DeviceNotRegistered"
        store.push_result(token, ok, gone=gone)
        accepted += ok
    return accepted


def _push_later(notice: dict) -> None:
    tokens = store.tokens(confirmed_only=True)
    if not tokens:
        return
    title, body = push_text(notice["kind"], notice["text"])
    task = asyncio.ensure_future(push(tokens, title, body,
                                      {"notice_id": notice["id"], "kind": notice["kind"]}))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def send_test(token: str) -> int:
    """The test notification the app looks for before it trusts pushes on this phone."""
    return await push([token], "JARVIS", "Notifications from the server reach this phone.",
                      {"test": True, "token": token})


async def _route(kind: str, text: str, send: Callable[[], Awaitable[bool]],
                 key: Optional[str] = None) -> bool:
    kind = _kind.get() or kind
    where = store.route()
    notice_id = store.record(kind, text, key)
    stored = notice_id is not None
    sent = False
    if where != "phone" or not stored:         # with "phone", a broken store falls back to the DM
        try:
            sent = bool(await send())
        except Exception as exc:
            log.debug("notices: Discord send failed (%s)", exc)
            sent = False
        store.set_discord(notice_id, sent)
    if stored:
        notice = {"id": notice_id, "kind": kind, "text": text, "discord": sent if where != "phone" else None}
        _phone_event(notice)
        if where in ("phone", "both"):
            _push_later(notice)
    if where == "discord":
        return sent
    return sent or stored


def deliver(kind: str, send: Callable[[str], Awaitable[bool]]) -> Callable[[str], Awaitable[bool]]:
    """A notifier for a module: `await notifier(text)` stores, routes, and says if it landed."""
    async def notifier(text: str) -> bool:
        return await _route(kind, text, lambda: send(text))
    notifier.__name__ = f"deliver_{kind}"
    return notifier


def deliver_keyed(kind: str, send: Callable[[str, str], Awaitable[bool]]):
    """The same for a keyed sender (a goal's messages): one Inbox row per key."""
    async def notifier(text: str, key: str) -> bool:
        return await _route(kind, text, lambda: send(text, key), key=key)
    notifier.__name__ = f"deliver_keyed_{kind}"
    return notifier


def prompt_line() -> str:
    """Every turn while his own messages do not go to Discord only: where they go, so JARVIS
    does not promise a Discord DM that will never come. Empty in the default (nothing changes)."""
    where = store.route()
    if where == "both":
        return (_owner.say("[Delivery: the messages you send «him» on your own (reminders, the brief, results, "
                "alerts) go to Discord and to «his» phone's Inbox.]"))
    if where == "phone":
        return (_owner.say("[Delivery: the messages you send «him» on your own (reminders, the brief, results, "
                "alerts) go to «his» phone's Inbox, not Discord. Say you will message «him» or that it "
                "will be in «his» Inbox; never that you will DM «him» on Discord.]"))
    return ""

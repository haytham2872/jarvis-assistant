"""Spotify control + listening-pattern memory for JARVIS.

One tool, `spotify_action`, mirroring the gmail_action shape: play / pause /
resume / next / previous / queue / now_playing / recent / liked / top /
devices / search / volume / shuffle.

Auth — Authorization Code with PKCE (spotipy ``SpotifyPKCE``). PKCE needs only
a **client_id**, no client secret, so nothing sensitive lives in .env. The
client_id is read from ``spotify_credentials.json`` at the project root
(``{"client_id": "...", "redirect_uri": "http://127.0.0.1:8888/callback"}``),
falling back to a ``SPOTIFY_CLIENT_ID`` env var. First run opens the browser
for consent and captures the redirect on a local server (same UX as the Gmail
flow); the refresh token is cached to ``spotify_token.json`` and refreshed
automatically thereafter. Premium is required for playback control — the owner's
account is Premium.

Listening memory — every track JARVIS starts (and whatever is found playing on
a now_playing check) is appended to ``jarvis-vault/drafts/listening-log.md``
tagged with the timestamp **and JARVIS's mood at that moment** — the one piece
of context Spotify itself can never have. The raw log is high-churn and is
deliberately NOT semantically indexed (drafts/ is skipped by backfill, and we
write it by hand rather than through vault_write). JARVIS curates it into an
indexed ``topics/music-taste.md`` and uses that + the live mood to suggest.

Silent degradation: auth/network failures return a clear string, never raise.
The Spotify client is built lazily on first use, never at import.
"""

import os
import json
import time
import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_PROJECT_ROOT  = Path(__file__).parent.parent.parent
_CREDS_PATH    = _PROJECT_ROOT / "spotify_credentials.json"
_TOKEN_PATH    = _PROJECT_ROOT / "spotify_token.json"
_LISTEN_LOG    = _PROJECT_ROOT / "jarvis-vault" / "drafts" / "listening-log.md"

_DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"

# Everything JARVIS needs: read playback + library + history + top, modify playback.
_SCOPES = (
    "user-read-playback-state user-modify-playback-state "
    "user-read-currently-playing user-read-recently-played "
    "user-library-read user-top-read"
)

_LISTEN_LOG_CAP = 300   # keep the last N log lines — append-only otherwise

_sp = None                 # lazily-built spotipy.Spotify
_last_logged_track = None   # dedupe consecutive identical plays


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _client_id_and_redirect() -> tuple[str, str]:
    """Return (client_id, redirect_uri). Prefers spotify_credentials.json; falls
    back to the SPOTIFY_CLIENT_ID env var. Raises with guidance if unset."""
    client_id = ""
    redirect  = _DEFAULT_REDIRECT
    if _CREDS_PATH.exists():
        try:
            data = json.loads(_CREDS_PATH.read_text(encoding="utf-8"))
            client_id = (data.get("client_id") or "").strip()
            redirect  = (data.get("redirect_uri") or _DEFAULT_REDIRECT).strip()
        except Exception as e:
            log.warning("spotify: could not parse %s: %s", _CREDS_PATH.name, e)
    if not client_id:
        client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    if not client_id:
        raise RuntimeError(
            "No Spotify client_id. Create an app at "
            "https://developer.spotify.com/dashboard, add the redirect URI "
            f"'{redirect}', then put {{\"client_id\": \"...\"}} in "
            f"{_CREDS_PATH.name} at the project root."
        )
    return client_id, redirect


def _get_sp():
    """Build (once) and return the authenticated spotipy client."""
    global _sp
    if _sp is not None:
        return _sp
    import spotipy
    from spotipy.oauth2 import SpotifyPKCE
    from spotipy.cache_handler import CacheFileHandler

    client_id, redirect = _client_id_and_redirect()
    auth = SpotifyPKCE(
        client_id     = client_id,
        redirect_uri  = redirect,
        scope         = _SCOPES,
        open_browser  = True,
        cache_handler = CacheFileHandler(cache_path=str(_TOKEN_PATH)),
    )
    if not _TOKEN_PATH.exists():
        log.info("spotify: opening browser for OAuth consent…")
    _sp = spotipy.Spotify(auth_manager=auth, requests_timeout=10)
    log.info("Spotify client ready.")
    return _sp


# ---------------------------------------------------------------------------
# Listening-pattern memory
# ---------------------------------------------------------------------------

def _mood_tag() -> str:
    """'(mood: contemplative 0.41)' — JARVIS's mood right now, or '' if unknown."""
    try:
        from backend.brain.mood import get_mood
        m = get_mood()
        if m is None:
            return ""
        return f" (mood: {m.tone} {m.intensity:.2f})"
    except Exception:
        return ""


def _log_listen(track_id: str, label: str) -> None:
    """Append one dated, mood-tagged listen line to the raw vault log.

    Deduped on consecutive identical track ids (now_playing polled repeatedly
    shouldn't spam the log). Capped to the last _LISTEN_LOG_CAP lines. Written
    by hand (not vault_write) so it stays out of the semantic index — the raw
    log is churn; the curated topics/music-taste.md is what gets indexed.
    """
    global _last_logged_track
    if not track_id or track_id == _last_logged_track:
        return
    _last_logged_track = track_id
    try:
        _LISTEN_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M")
        line  = f"- {stamp} — {label}{_mood_tag()}\n"
        existing = _LISTEN_LOG.read_text(encoding="utf-8") if _LISTEN_LOG.exists() else ""
        if not existing:
            existing = "# Listening log (raw, mood-tagged — curate into topics/music-taste.md)\n\n"
        lines = existing.splitlines(keepends=True)
        header, body = lines[:2], [l for l in lines[2:] if l.strip()]
        body.append(line)
        body = body[-_LISTEN_LOG_CAP:]
        _LISTEN_LOG.write_text("".join(header) + "".join(body), encoding="utf-8")
    except Exception as e:
        log.debug("spotify: listen log append failed: %s", e)


def _fmt_track(t: dict) -> str:
    """'Song — Artist1, Artist2' from a Spotify track object."""
    if not t:
        return "(unknown track)"
    name = t.get("name", "?")
    artists = ", ".join(a.get("name", "?") for a in t.get("artists", []))
    return f"{name} — {artists}" if artists else name


# ---------------------------------------------------------------------------
# Playback helpers
# ---------------------------------------------------------------------------

def _resolve_device(sp, requested: str = "") -> str | None:
    """Return a device_id to target. If `requested` names a device, match it;
    else prefer the active device; else the sole available device; else None."""
    try:
        devices = sp.devices().get("devices", [])
    except Exception:
        return None
    if not devices:
        return None
    if requested:
        for d in devices:
            if requested.lower() in d.get("name", "").lower():
                return d.get("id")
    for d in devices:
        if d.get("is_active"):
            return d.get("id")
    return devices[0].get("id") if len(devices) == 1 else None


def _start_track(sp, uri: str, label: str, track_id: str, device: str) -> str:
    """Start a single track, picking a device if none is active. Logs the play."""
    device_id = _resolve_device(sp, device)
    try:
        sp.start_playback(device_id=device_id, uris=[uri])
    except Exception as e:
        if "NO_ACTIVE_DEVICE" in str(e) or "No active device" in str(e):
            return ("No active Spotify device. Open Spotify on your phone or "
                    "desktop (or say which device), then ask again.")
        raise
    _log_listen(track_id, label)
    return f"Playing: {label}"


# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------

_ACTIONS = (
    "play", "pause", "resume", "next", "skip", "previous", "queue",
    "now_playing", "recent", "liked", "top", "devices", "search",
    "volume", "shuffle",
)


async def spotify_action(
    action: str,
    query: str = "",
    uri: str = "",
    device: str = "",
    limit: int = 10,
    volume: int = -1,
    time_range: str = "medium_term",
) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _do, action, query, uri, device, limit, volume, time_range
    )


def _do(action, query, uri, device, limit, volume, time_range) -> str:
    action = (action or "").strip().lower()
    if action == "skip":
        action = "next"
    if action not in _ACTIONS and action != "next":
        return f"Unknown action: {action}. Valid: {', '.join(_ACTIONS)}"

    try:
        sp = _get_sp()
    except Exception as e:
        return f"Spotify not set up: {e}"

    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 10

    try:
        # ----- playback control -----
        if action in ("play", "resume"):
            if action == "play" and (query or uri):
                if not uri:
                    res = sp.search(q=query, type="track", limit=1)
                    items = res.get("tracks", {}).get("items", [])
                    if not items:
                        return f"No track found for '{query}'."
                    track = items[0]
                else:
                    track = sp.track(uri)
                return _start_track(sp, track["uri"], _fmt_track(track),
                                    track.get("id", ""), device)
            # bare resume
            device_id = _resolve_device(sp, device)
            try:
                sp.start_playback(device_id=device_id)
            except Exception as e:
                if "NO_ACTIVE_DEVICE" in str(e) or "No active device" in str(e):
                    return "No active Spotify device to resume on."
                raise
            cur = sp.currently_playing()
            label = _fmt_track(cur.get("item")) if cur else ""
            if cur and cur.get("item"):
                _log_listen(cur["item"].get("id", ""), label)
            return f"Resumed{(': ' + label) if label else ''}."

        if action == "pause":
            sp.pause_playback(device_id=_resolve_device(sp, device))
            return "Paused."

        if action == "next":
            sp.next_track(device_id=_resolve_device(sp, device))
            time.sleep(0.3)  # let Spotify advance before reading the new track
            cur = sp.currently_playing()
            if cur and cur.get("item"):
                label = _fmt_track(cur["item"])
                _log_listen(cur["item"].get("id", ""), label)
                return f"Skipped to: {label}"
            return "Skipped."

        if action == "previous":
            sp.previous_track(device_id=_resolve_device(sp, device))
            return "Went back to the previous track."

        if action == "queue":
            if not (query or uri):
                return "queue needs a query or uri."
            if not uri:
                res = sp.search(q=query, type="track", limit=1)
                items = res.get("tracks", {}).get("items", [])
                if not items:
                    return f"No track found for '{query}'."
                track = items[0]
            else:
                track = sp.track(uri)
            sp.add_to_queue(track["uri"], device_id=_resolve_device(sp, device))
            return f"Queued: {_fmt_track(track)}"

        if action == "volume":
            if not 0 <= volume <= 100:
                return "volume needs a value 0–100."
            sp.volume(volume, device_id=_resolve_device(sp, device))
            return f"Volume set to {volume}%."

        if action == "shuffle":
            state = query.strip().lower() not in ("off", "false", "no", "0")
            sp.shuffle(state, device_id=_resolve_device(sp, device))
            return f"Shuffle {'on' if state else 'off'}."

        # ----- reads -----
        if action == "now_playing":
            cur = sp.current_playback()
            if not cur or not cur.get("item"):
                return "Nothing is playing right now."
            label = _fmt_track(cur["item"])
            _log_listen(cur["item"].get("id", ""), label)
            state = "playing" if cur.get("is_playing") else "paused"
            dev = cur.get("device", {}).get("name", "?")
            return f"Now {state}: {label} (on {dev})."

        if action == "recent":
            res = sp.current_user_recently_played(limit=limit)
            items = res.get("items", [])
            if not items:
                return "No recent listening history."
            lines = [f"{i}. {_fmt_track(it.get('track'))}"
                     for i, it in enumerate(items, 1)]
            return "Recently played:\n" + "\n".join(lines)

        if action == "liked":
            res = sp.current_user_saved_tracks(limit=limit)
            items = res.get("items", [])
            if not items:
                return "No liked songs found."
            total = res.get("total", len(items))
            lines = [f"{i}. {_fmt_track(it.get('track'))}"
                     for i, it in enumerate(items, 1)]
            return f"Liked songs ({total} total, showing {len(lines)}):\n" + "\n".join(lines)

        if action == "top":
            tr = time_range if time_range in ("short_term", "medium_term", "long_term") else "medium_term"
            res = sp.current_user_top_tracks(limit=limit, time_range=tr)
            items = res.get("items", [])
            if not items:
                return "No top tracks yet."
            window = {"short_term": "last 4 weeks", "medium_term": "last 6 months",
                      "long_term": "all time"}[tr]
            lines = [f"{i}. {_fmt_track(t)}" for i, t in enumerate(items, 1)]
            return f"Top tracks ({window}):\n" + "\n".join(lines)

        if action == "devices":
            devices = sp.devices().get("devices", [])
            if not devices:
                return "No Spotify devices available. Open Spotify somewhere."
            lines = [f"- {d.get('name','?')} ({d.get('type','?')})"
                     f"{' [active]' if d.get('is_active') else ''}" for d in devices]
            return "Available devices:\n" + "\n".join(lines)

        if action == "search":
            res = sp.search(q=query, type="track", limit=min(limit, 10))
            items = res.get("tracks", {}).get("items", [])
            if not items:
                return f"No results for '{query}'."
            lines = [f"{i}. {_fmt_track(t)}  [uri: {t.get('uri')}]"
                     for i, t in enumerate(items, 1)]
            return f"Search results for '{query}':\n" + "\n".join(lines)

        return f"Unknown action: {action}"

    except Exception as e:
        msg = str(e)
        if "Premium" in msg or "premium" in msg:
            return "That needs Spotify Premium (playback control)."
        log.error("spotify action '%s' failed: %s", action, e)
        return f"Spotify error: {e}"

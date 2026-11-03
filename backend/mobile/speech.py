"""Speech for the phone: an answer as MP3 bytes the iPhone plays as they are.

backend/audio/tts.py cannot be imported on the server (it imports sounddevice at
module top, and the server has no audio device), so the phone path has its own small
module. Both engines return MP3, so nothing is decoded or re-encoded here.

Engines (docs/archive/MOBILE-APP-PLAN.md section 6, measured 13-14 October):
- Google Chirp 3 HD, when GOOGLE_TTS_API_KEY is set: ~0.7-0.9 s a sentence from the
  desk, free up to 1M characters a month. The key goes in the X-Goog-Api-Key header,
  never in a URL.
- edge-tts otherwise. Microsoft has refused it (403) since 13 October; kept only as the
  key-less path, and the phone's own voice covers its failures.
MOBILE_TTS_ENGINE=chirp|edge overrides the choice.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re

SYNTH_TIMEOUT_S = 15.0
CHIRP_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
DEFAULT_CHIRP_VOICE = "en-US-Chirp3-HD-Fenrir"   # The owner's pick by ear, 14 October
_EDGE_VOICE_RE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural$")
_CHIRP_VOICE_RE = re.compile(r"^([a-z]{2}-[A-Z]{2})-Chirp3-HD-[A-Z][a-z]+$")


class SpeechError(Exception):
    pass


class BadVoice(SpeechError):
    """The voice name is not one the engine could have (a caller error, not an outage)."""


def engine() -> str:
    forced = os.getenv("MOBILE_TTS_ENGINE", "").strip().lower()
    if forced in ("chirp", "edge"):
        return forced
    return "chirp" if os.getenv("GOOGLE_TTS_API_KEY", "").strip() else "edge"


def valid_voice(voice: str) -> bool:
    return bool(_EDGE_VOICE_RE.match(voice))


async def synthesize(text: str, voice: str = "") -> tuple[bytes, str]:
    """(MP3, engine name) for `text` from the configured engine. Raises SpeechError."""
    name = engine()
    audio = await (chirp_mp3(text, voice) if name == "chirp" else edge_mp3(text, voice))
    return audio, name


async def chirp_mp3(text: str, voice: str = "") -> bytes:
    """MP3 for `text` from Google Chirp 3 HD."""
    key = os.getenv("GOOGLE_TTS_API_KEY", "").strip()
    if not key:
        raise SpeechError("GOOGLE_TTS_API_KEY is not set")
    if not key.isascii():
        # Found on the server 14 October: pasting the key through a browser terminal turned
        # its hyphens into look-alike dashes, and the request died as UnicodeEncodeError.
        raise SpeechError("GOOGLE_TTS_API_KEY contains a character that is not plain ASCII "
                          "(often a hyphen changed into a dash by copy-paste)")
    voice = voice or os.getenv("MOBILE_TTS_VOICE", DEFAULT_CHIRP_VOICE)
    match = _CHIRP_VOICE_RE.match(voice)
    if not match:
        raise BadVoice(f"not a Chirp 3 HD voice name: {voice!r}")
    from backend.providers import gateway

    body = {"input": {"text": text}, "voice": {"languageCode": match.group(1), "name": voice},
            "audioConfig": {"audioEncoding": "MP3"}}
    try:
        resp = await gateway.get_http_client().post(
            CHIRP_URL, json=body, headers={"X-Goog-Api-Key": key}, timeout=SYNTH_TIMEOUT_S)
    except Exception as exc:
        raise SpeechError(f"Chirp request failed: {type(exc).__name__}") from None
    if resp.status_code != 200:
        try:
            message = resp.json().get("error", {}).get("message", "")
        except ValueError:
            message = ""
        raise SpeechError(f"Chirp answered {resp.status_code}: {message[:200]}")
    try:
        audio = base64.b64decode(resp.json()["audioContent"])
    except (ValueError, KeyError, TypeError):
        raise SpeechError("Chirp returned no audio") from None
    if not audio:
        raise SpeechError("Chirp returned no audio")
    return audio


async def edge_mp3(text: str, voice: str = "") -> bytes:
    """MP3 for `text` from edge-tts. Raises SpeechError on failure, timeout or no audio."""
    voice = voice or os.getenv("TTS_VOICE", "en-US-GuyNeural")
    if not valid_voice(voice):
        raise BadVoice(f"not an edge-tts voice name: {voice!r}")

    async def run() -> bytes:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice=voice, rate=os.getenv("TTS_RATE", "+8%"),
                                           pitch=os.getenv("TTS_PITCH", "-5Hz"))
        chunks = [c["data"] async for c in communicate.stream() if c["type"] == "audio"]
        return b"".join(chunks)

    try:
        audio = await asyncio.wait_for(run(), timeout=SYNTH_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise SpeechError(f"edge-tts gave no answer in {SYNTH_TIMEOUT_S:.0f}s") from None
    except Exception as exc:
        raise SpeechError(f"edge-tts failed: {type(exc).__name__}: {exc}") from None
    if not audio:
        raise SpeechError("edge-tts returned no audio")
    return audio

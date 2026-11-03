"""What the phone says aloud for an answer (docs/archive/MOBILE-APP-PLAN.md, M3).

Only the final answer is spoken: the text the phone shows once the turn is done, after
JARVIS's claim and skill checks, never the draft that streamed while he worked. It is read
back from the server's records (the request's stored answer, or the stored exchange), not
from anything the phone sends, so the phone cannot make him say anything else.

The spoken version is not the Markdown: formatting goes, a link is read by its text, code
and tables are left on the screen with one sentence saying so. Every word read is the
answer's own: a very long answer is read from the start up to READ_MAX_CHARS, then "the
rest is on your screen". (Until 15 October a long answer was read as a model-written
summary; the owner heard sentences that appeared nowhere on his screen and asked for the
answer itself.) It is cut into clips of a sentence or
a few, each with its language: a clip that is mostly French is read by Google's French
Fenrir, anything else by the English Fenrir the owner chose by ear. Both read both
languages intelligibly (each sample read back by Parakeet at 0-14% word error, 15
October); what differs is the accent.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from collections import OrderedDict

log = logging.getLogger(__name__)

READ_MAX_CHARS = 4000          # about four and a half minutes; past it, the opening and a pointer
FIRST_CLIP_MIN = 60            # a very short first sentence takes the next one with it
CLIP_TARGET = 280              # later clips: whole sentences up to about this
CLIP_HARD_MAX = 450            # one sentence longer than this is cut at a comma

DEFAULT_VOICE_EN = "en-US-Chirp3-HD-Fenrir"
DEFAULT_VOICE_FR = "fr-FR-Chirp3-HD-Fenrir"

ON_SCREEN_CODE = "The code is on your screen."
ON_SCREEN_TABLE = "The table is on your screen."
REST_ON_SCREEN = "The rest is on your screen."

# ------------------------------------------------------------------ Markdown to speech

_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+")
_QUOTE = re.compile(r"^\s*>\s?")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_FOOTER = re.compile(r"^\s*·\s")                 # "· followed: <skill>", added at delivery
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://([^/\s)]+)\S*")
_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC = re.compile(r"(?<![\w*])[*_](?=\S)([^*_\n]+?)(?<=\S)[*_](?![\w*])")
_STRIKE = re.compile(r"~~(.+?)~~")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002700-\U000027BF\U0001F900-\U0001F9FF️]")
_END = re.compile(r"[.!?…:;]$")


def _inline(text: str) -> str:
    text = _IMAGE.sub(lambda m: m.group(1), text)
    text = _LINK.sub(lambda m: m.group(1), text)
    text = _URL.sub(lambda m: "a link to " + m.group(1).removeprefix("www."), text)
    text = _BOLD.sub(lambda m: m.group(2), text)
    text = _STRIKE.sub(lambda m: m.group(1), text)
    text = _ITALIC.sub(lambda m: m.group(1), text)
    text = _CODE_SPAN.sub(lambda m: m.group(1) if len(m.group(1)) <= 40 else "the code on your screen",
                          text)
    text = _EMOJI.sub("", text)
    return text.replace(" → ", ", ").replace("→", ", ").replace("&", " and ").replace(" ~", " about ")


def _sentence(line: str) -> str:
    line = line.strip()
    return line if not line or _END.search(line) else line + "."


def spoken_text(answer: str) -> str:
    """The answer as it should be heard, before any summary."""
    parts: list[str] = []
    in_fence = said_code = said_table = in_table = False
    for raw in (answer or "").replace("\r\n", "\n").split("\n"):
        if _FENCE.match(raw):
            if not in_fence and not said_code:
                parts.append(ON_SCREEN_CODE)
                said_code = True
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _TABLE_ROW.match(raw):
            if not in_table and not said_table:
                parts.append(ON_SCREEN_TABLE)
                said_table = True
            in_table = True
            continue
        in_table = False
        if _FOOTER.match(raw) or _RULE.match(raw) or not raw.strip():
            continue
        line = _QUOTE.sub("", raw)
        structured = bool(_HEADING.match(line) or _BULLET.match(line))
        line = _BULLET.sub("", _HEADING.sub("", line))
        line = _inline(line)
        parts.append(_sentence(line) if structured else line.strip())
    text = " ".join(p for p in parts if p)
    return re.sub(r"\s+([,.;:!?])", r"\1", re.sub(r"\s{2,}", " ", text)).strip()


# ------------------------------------------------------------------ sentences, clips, language

_ABBREV = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "approx", "no", "fig",
           "m", "mme", "cf", "p"}
_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[\"“'(]?[A-ZÀ-ÖØ-Þ0-9])")

_FR = set("le la les un une des du de est et à au aux pour que qui pas ne je tu il elle nous "
          "vous ils elles ton ta tes mon ma mes son sa ses ce cette ces dans sur avec mais ou "
          "donc car j c l d n qu s t y en se lui leur très plus moins été être avoir fait "
          "aujourd'hui demain hier ça oui non merci bonjour".split())
_EN = set("the a an is are was were be been and to of for that this these those you your "
          "it its in on with but or so if i my me we our they their he she his her at by "
          "from as have has had will would can could should not do does did today tomorrow "
          "yes no thanks hello".split())
_ACCENT = re.compile(r"[éèêëàâîïôûùçœ]", re.I)


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for piece in _SPLIT.split(text):
        if out and out[-1].split() and out[-1].split()[-1].rstrip(".").lower() in _ABBREV:
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return [s.strip() for s in out if s.strip()]


def language(text: str, default: str = "en") -> str:
    """'fr' or 'en' from function words and accents; a tie keeps `default`."""
    words = re.findall(r"[a-zàâçéèêëîïôûùüÿœ']+", text.lower())
    fr = sum(w in _FR or w.split("'")[0] in _FR for w in words) + 0.5 * len(_ACCENT.findall(text))
    en = sum(w in _EN for w in words)
    return "fr" if fr > en else "en" if en > fr else default


def _cut(sentence: str) -> list[str]:
    """A sentence too long for one clip, cut at commas or semicolons near CLIP_TARGET."""
    if len(sentence) <= CLIP_HARD_MAX:
        return [sentence]
    out, cur = [], ""
    for bit in re.split(r"(?<=[,;])\s+", sentence):
        if cur and len(cur) + 1 + len(bit) > CLIP_TARGET:
            out.append(cur)
            cur = bit
        else:
            cur = f"{cur} {bit}".strip()
    return out + ([cur] if cur else [])


def clips(text: str) -> list[dict]:
    """[{seq, text, lang}]: the first clip short so the first sound comes fast, later clips
    whole sentences up to CLIP_TARGET; sentences of different languages never share a clip."""
    lang, units = "en", []
    for s in sentences(text):
        lang = language(s, lang)
        units += [(piece, lang) for piece in _cut(s)]
    out: list[dict] = []
    for piece, lang in units:
        last = out[-1] if out else None
        if last and last["lang"] == lang and (
                len(last["text"]) < FIRST_CLIP_MIN if last["seq"] == 0 else
                len(last["text"]) + 1 + len(piece) <= CLIP_TARGET):
            last["text"] = f"{last['text']} {piece}"
        else:
            out.append({"seq": len(out), "text": piece, "lang": lang})
    return out


def voice_for(lang: str) -> str:
    if lang == "fr":
        return os.getenv("MOBILE_TTS_VOICE_FR", "").strip() or DEFAULT_VOICE_FR
    return os.getenv("MOBILE_TTS_VOICE", "").strip() or DEFAULT_VOICE_EN


# ------------------------------------------------------------------ the plan of one answer

_plans: OrderedDict[tuple[str, str], dict] = OrderedDict()
_PLAN_CACHE = 64


def revision(answer: str) -> str:
    return hashlib.sha256((answer or "").encode("utf-8")).hexdigest()[:16]


def _opening(text: str, limit: int = READ_MAX_CHARS) -> str:
    """Whole sentences from the start, up to `limit` characters."""
    out = ""
    for s in sentences(text):
        if out and len(out) + 1 + len(s) > limit:
            break
        out = f"{out} {s}".strip()
    return out


def read_text(answer: str) -> tuple[str, bool]:
    """What is read aloud for `answer`, and whether it was cut short: every word the answer's
    own, a very long one from the start with a pointer to the screen. Shared by the app's
    clips and the Action Button Shortcut (/ask), so both read the same thing."""
    text = spoken_text(answer)
    if len(text) <= READ_MAX_CHARS:                # "cut short": the plan key keeps "summarised"
        return text, False
    return f"{_opening(text)} {REST_ON_SCREEN}", True


async def plan(source: str, answer: str) -> dict:
    """{source, revision, summarised, clips}. The same answer gets the same plan (cached),
    so the clips the phone asks for match the plan it was given."""
    rev = revision(answer)
    key = (source, rev)
    if key in _plans:
        _plans.move_to_end(key)
        return _plans[key]
    text, summarised = read_text(answer)
    result = {"source": source, "revision": rev, "summarised": summarised, "clips": clips(text)}
    _plans[key] = result
    while len(_plans) > _PLAN_CACHE:
        _plans.popitem(last=False)
    return result


# ------------------------------------------------------------------ one clip's audio

class OverAllowance(Exception):
    """This month's free voice characters would run out: the phone reads with its own voice."""


_audio: OrderedDict[tuple[str, int], tuple[bytes, str]] = OrderedDict()
_AUDIO_CACHE = 48
_inflight: dict[tuple[str, int], asyncio.Future] = {}


def monthly_limit() -> int:
    try:
        return int(os.getenv("MOBILE_TTS_MONTHLY_CHARS", "") or 900_000)
    except ValueError:
        return 900_000


async def clip_audio(rev: str, clip: dict) -> tuple[bytes, str]:
    """(MP3, engine) for one clip; cached, and two requests for the same clip share one call.
    Raises OverAllowance, or speech.SpeechError when the voice fails."""
    key = (rev, int(clip["seq"]))
    if key in _audio:
        _audio.move_to_end(key)
        return _audio[key]
    if key in _inflight:
        return await asyncio.shield(_inflight[key])
    fut = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        result = await _synthesize(clip)
        _audio[key] = result
        while len(_audio) > _AUDIO_CACHE:
            _audio.popitem(last=False)
        fut.set_result(result)
        return result
    except asyncio.CancelledError:
        fut.cancel()
        raise
    except Exception as exc:
        fut.set_exception(exc)
        fut.exception()           # retrieved: no "never retrieved" warning when nobody waits
        raise
    finally:
        _inflight.pop(key, None)


async def _synthesize(clip: dict) -> tuple[bytes, str]:
    from backend.mobile import speech
    from backend.storage import speech_usage
    text = clip["text"]
    if speech.engine() != "chirp":
        return await speech.synthesize(text)
    if not speech_usage.reserve(len(text), monthly_limit()):
        raise OverAllowance("this month's free voice allowance is used up")
    try:
        return await speech.chirp_mp3(text, voice_for(clip.get("lang", "en"))), "chirp"
    except Exception:
        speech_usage.refund(len(text))     # a failed request is not billed; a cancelled one may be
        raise

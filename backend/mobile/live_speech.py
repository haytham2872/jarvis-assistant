"""A heard phone turn speaks while it is still being written (docs/archive/MOBILE-APP-PLAN.md, M3).

Until now nothing was spoken until the whole turn and all its checks had finished. For a turn
the phone will read aloud (turn_input.spoken), each sentence chat() writes is published as a
`speech_clip` event the moment it is complete, and its synthesis starts at once; the phone
fetches the audio from GET /mobile/v1/speech/live. Three rules keep that honest:

- A sentence that matches a claim pattern (engine.speakable) is not spoken, and neither is
  anything after it: the reply check has not run yet. It is spoken from the checked final
  answer at the end.
- If a tool starts and nothing has been said, one short line written here (FILLERS), never by
  the model, covers the wait. Never a second one.
- At the end, finish() speaks whatever of the checked final answer was not already said
  (from the first sentence not yet said onward), then publishes `speech_end`. Every
  speech_clip and the speech_end are published before run_finished, so the phone decides
  at run_finished whether the turn was live.

Total speech is capped like a replayed answer (READ_MAX_CHARS, then REST_ON_SCREEN). One clip
is one sentence (or a few short ones): Chirp takes about 0.4 s for a sentence, 2.4 s for 320
characters. The audio is cached by spoken.clip_audio under the revision f"live-{request_id}".
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Callable

from backend.mobile import spoken
from backend.mobile.events import HUB
from backend.tasks import spawn

log = logging.getLogger(__name__)

WAIT_S = 20.0          # how long a request for a clip waits for a synthesis still in flight
KEEP = 8               # turns remembered

# What is said while a tool runs and nothing has been said yet: (English, French).
DEFAULT_FILLER = ("One moment.", "Un instant.")
FILLERS: dict[str, tuple[str, str]] = {
    "web_browse": ("Let me look that up.", "Je regarde ça."),
    "web_research": ("Let me look into that.", "Je me renseigne."),
    "fetch_page": ("Let me read that page.", "Je lis cette page."),
    "gmail_action": ("Checking your mail.", "Je regarde tes mails."),
    "calendar_action": ("Checking your calendar.", "Je regarde ton agenda."),
    "week_plan": ("Checking your week.", "Je regarde ta semaine."),
    "memory_search": ("Let me remember.", "Laisse-moi me souvenir."),
    "portfolio": ("Checking your portfolio.", "Je regarde ton portefeuille."),
    "spotify_action": ("On it.", "Je m'en occupe."),
    "notion_action": ("Checking Notion.", "Je regarde dans Notion."),
}


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


class LiveSpeech:
    def __init__(self, request_id: str, user_text: str = "",
                 publish: Callable[[dict], object] | None = None) -> None:
        self.request_id = request_id
        self.rev = f"live-{request_id}"
        self._lang = spoken.language(user_text or "")
        self._publish = publish
        self._clips: list[dict] = []
        self._tasks: dict[int, asyncio.Task] = {}
        self._said: set[str] = set()       # normalized sentences already published
        self._chars = 0
        self._held = False                 # a claim sentence: nothing more is spoken live
        self._capped = False
        self._tool_seen = False
        self._finished = False

    # ---------------------------------------------------------------- publishing

    def _pub(self, event: dict) -> None:
        (self._publish or HUB.publish)({**event, "request_id": self.request_id})

    def _clip(self, text: str, lang: str) -> None:
        clip = {"seq": len(self._clips), "text": text, "lang": lang}
        self._clips.append(clip)
        self._chars += len(text)
        task = spawn(spoken.clip_audio(self.rev, clip), name=f"live-speech-{self.request_id}-{clip['seq']}")
        task.add_done_callback(lambda t: t.cancelled() or t.exception())   # retrieved: no warning
        self._tasks[clip["seq"]] = task
        # "clip", not "seq": HUB.publish overwrites "seq" with its own envelope number.
        self._pub({"type": "speech_clip", "clip": clip["seq"], "text": text, "lang": lang})

    def _say(self, text: str) -> None:
        """Publish `text` as clips, unless the cap is reached (then the pointer, once)."""
        for c in spoken.clips(text):
            if self._capped:
                return
            if self._chars + len(c["text"]) > spoken.READ_MAX_CHARS:
                self._capped = True
                self._clip(spoken.REST_ON_SCREEN, "en")
                return
            self._clip(c["text"], c["lang"])
        self._said.update(_norm(s) for s in spoken.sentences(text))

    # ---------------------------------------------------------------- the turn

    async def run(self, queue: asyncio.Queue) -> None:
        """Consume the sentences chat() writes until the end marker (None)."""
        from backend.brain.engine import speakable
        while True:
            item = await queue.get()
            if item is None:
                return
            if self._held or self._capped:
                continue
            text = spoken.spoken_text(item)
            if not text:
                continue
            if not speakable(item):
                self._held = True
                continue
            self._say(text)

    def on_event(self, msg: dict) -> None:
        """The first tool call of the turn, with nothing said yet, gets one short line."""
        if msg.get("type") != "tool_call" or self._tool_seen:
            return
        self._tool_seen = True
        if self._clips:
            return
        line = FILLERS.get(str(msg.get("tool") or ""), DEFAULT_FILLER)[1 if self._lang == "fr" else 0]
        self._clip(line, self._lang)

    def finish(self, answer: str | None, completed: bool) -> None:
        """Speak what the checked final answer says that was not said, then speech_end."""
        if self._finished:
            return
        self._finished = True
        if completed and answer and not self._capped:
            final = spoken.sentences(spoken.read_text(answer)[0])
            todo = next((i for i, s in enumerate(final) if _norm(s) not in self._said), len(final))
            if todo < len(final):
                self._say(" ".join(final[todo:]))
        self._pub({"type": "speech_end", "clips": len(self._clips)})

    async def clip_bytes(self, seq: int) -> tuple[bytes, str]:
        """(MP3, engine) of one clip, waiting for a synthesis in flight. KeyError: no such
        clip. spoken.OverAllowance / speech.SpeechError / TimeoutError as clip_audio's."""
        task = self._tasks[seq]
        return await asyncio.wait_for(asyncio.shield(task), WAIT_S)


# ------------------------------------------------------------------ the last few turns

_turns: OrderedDict[str, LiveSpeech] = OrderedDict()


def start(request_id: str, user_text: str = "") -> LiveSpeech:
    ls = _turns[request_id] = LiveSpeech(request_id, user_text)
    _turns.move_to_end(request_id)
    while len(_turns) > KEEP:
        _turns.popitem(last=False)
    return ls


def get(request_id: str | None) -> LiveSpeech | None:
    return _turns.get(request_id) if request_id else None

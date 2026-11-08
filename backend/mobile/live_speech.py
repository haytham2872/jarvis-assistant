"""A heard phone turn speaks while it is still being written (docs/archive/MOBILE-APP-PLAN.md, M3).

Until now nothing was spoken until the whole turn and all its checks had finished. For a turn
the phone will read aloud (turn_input.spoken), each sentence chat() writes is published as a
`speech_clip` event the moment it is complete, and its synthesis starts at once; the phone
fetches the audio from GET /mobile/v1/speech/live. Three rules keep that honest:

- A sentence that matches a claim pattern (engine.speakable) is not spoken, and neither is
  anything after it: the reply check has not run yet. It is spoken from the checked final
  answer at the end.
- While JARVIS works and nothing is being said, short lines written here, never by the model,
  cover the wait: one for a tool (FILLERS), one when it is still thinking early on
  (THINKING), one in a long silence (STILL). Several variants each, the ones said lately
  avoided, at most MAX_FILLERS a turn.
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
import random
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable

from backend.mobile import spoken
from backend.mobile.events import HUB
from backend.tasks import spawn

log = logging.getLogger(__name__)

WAIT_S = 20.0          # how long a request for a clip waits for a synthesis still in flight
KEEP = 8               # turns remembered

# What is said while JARVIS works and nothing is being said: (English, French) variants.
DEFAULT_FILLERS = [("One moment.", "Un instant."), ("Give me a second.", "Une seconde."),
                   ("Let me check.", "Je vérifie."), ("On it.", "Je m'en occupe.")]
FILLERS: dict[str, list[tuple[str, str]]] = {
    "web_browse": [("Let me look that up.", "Je regarde ça."),
                   ("Let me check online.", "Je vérifie en ligne."),
                   ("Looking it up now.", "Je cherche."),
                   ("Let me see what's out there.", "Voyons ce qu'on trouve.")],
    "web_research": [("Let me dig into that.", "Je creuse la question."),
                     ("Let me look into it properly.", "Je me renseigne sérieusement.")],
    "fetch_page": [("Let me read that page.", "Je lis cette page."), ("Reading it now.", "Je lis ça.")],
    "gmail_action": [("Checking your mail.", "Je regarde tes mails."),
                     ("Let me look in your inbox.", "Je regarde ta boîte mail."),
                     ("One sec, going through your email.", "Une seconde, je parcours tes mails.")],
    "calendar_action": [("Checking your calendar.", "Je regarde ton agenda."),
                        ("Let me look at your schedule.", "Je regarde ton planning."),
                        ("One sec, opening your calendar.", "Une seconde, j'ouvre ton agenda.")],
    "week_plan": [("Checking your week.", "Je regarde ta semaine."),
                  ("Let me look at your plan.", "Je regarde ton plan de la semaine.")],
    "memory_search": [("Let me remember.", "Laisse-moi me souvenir."),
                      ("Hang on, it's coming back to me.", "Attends, ça me revient."),
                      ("Let me think back.", "Je repense à ça.")],
    "portfolio": [("Checking your portfolio.", "Je regarde ton portefeuille."),
                  ("Let me pull up your positions.", "J'ouvre tes positions.")],
    "spotify_action": [("On it.", "Je m'en occupe."), ("Sure, one second.", "D'accord, une seconde.")],
    "notion_action": [("Checking Notion.", "Je regarde dans Notion."),
                      ("Let me look in Notion.", "Je regarde ça dans Notion.")],
}
# Nothing said THINK_AFTER_S into the turn and no tool yet: JARVIS is thinking it through.
THINKING = [("Hmm, let me think.", "Hmm, laisse-moi réfléchir."), ("Let me see.", "Voyons voir."),
            ("Let me think about that.", "Je réfléchis."), ("Okay, give me a second.", "D'accord, une seconde."),
            ("Right, let me work that out.", "Bon, voyons ça.")]
# Nothing said for STILL_AFTER_S while the turn still runs.
STILL = [("Still on it.", "Je suis toujours dessus."), ("Almost there.", "J'y suis presque."),
         ("Bear with me, nearly done.", "Encore un instant, j'ai presque fini."),
         ("This one's taking a moment.", "Celle-là prend un peu de temps."),
         ("Just a little longer.", "Encore un peu.")]
THINK_AFTER_S = 2.5     # the server's first word comes ~2 s in (p50): only a slow start gets one
STILL_AFTER_S = 7.0
TOOL_GAP_S = 3.0        # a tool call gets its line only after this long without speech
MAX_FILLERS = 3         # lines of this kind per turn, in all
TICK_S = 0.25

_recent: deque[str] = deque(maxlen=6)       # lines said lately, across turns
_rng = random.Random()


def pick(options: list[tuple[str, str]], lang: str) -> str:
    """One line in the owner's language, avoiding the ones said lately while another is left."""
    lines = [o[1 if lang == "fr" else 0] for o in options]
    fresh = [line for line in lines if line not in _recent] or lines
    line = _rng.choice(fresh)
    _recent.append(line)
    return line


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


class LiveSpeech:
    def __init__(self, request_id: str, user_text: str = "",
                 publish: Callable[[dict], object] | None = None, now=time.monotonic) -> None:
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
        self._now = now
        self._started = now()
        self._last_said: float | None = None     # when the last clip went out
        self._fillers = 0
        self._tools_said: set[str] = set()
        self._running = False

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
        self._last_said = self._now()

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
        self._running = True
        watch = spawn(self._watch(), name=f"live-speech-watch-{self.request_id}")
        try:
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
        finally:
            self._running = False
            watch.cancel()

    def _filler(self, options: list[tuple[str, str]]) -> None:
        self._fillers += 1
        self._clip(pick(options, self._lang), self._lang)

    def tick(self) -> None:
        """While the turn runs: a thinking line early, a still-working line in a long silence."""
        if not self._running or self._finished or self._capped or self._fillers >= MAX_FILLERS:
            return
        now = self._now()
        if self._last_said is None:
            if not self._tool_seen and now - self._started >= THINK_AFTER_S:
                self._filler(THINKING)
            elif now - self._started >= STILL_AFTER_S:
                self._filler(STILL)
        elif now - self._last_said >= STILL_AFTER_S:
            self._filler(STILL)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            self.tick()

    def on_event(self, msg: dict) -> None:
        """A tool call after a while with nothing said gets a line for that tool: once per
        tool, at most MAX_FILLERS lines a turn in all."""
        if msg.get("type") != "tool_call":
            return
        tool = str(msg.get("tool") or "")
        self._tool_seen = True
        if self._finished or self._capped or self._fillers >= MAX_FILLERS or tool in self._tools_said:
            return
        if self._last_said is not None and self._now() - self._last_said < TOOL_GAP_S:
            return
        self._tools_said.add(tool)
        self._filler(FILLERS.get(tool, DEFAULT_FILLERS))

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

"""Who JARVIS works for: the owner's name and pronouns, from the settings.

Prompts, tool descriptions and messages write the owner as markers, filled in by `say()`:

    «owner» «Owner» «OWNER» «owner's» «Owner's»   the name ("the user" when none is set)
    «he» «He» «him» «his» «His» «himself»   the pronouns, for he, she or they
    «theirs»                                the standalone possessive ("is «theirs»")
    «tz»                                    the owner's time zone (JARVIS_TIMEZONE, backend/localtime.py)

With "they", the verb right after «he» is put in the plural ("«he» wants" reads "they
want"); write «he» before a verb in the present tense or a modal, never "«he»'s".

Settings, read once at import (from the environment, or the project's .env when the
environment does not have them; only these two keys are taken from .env here):
    JARVIS_OWNER_NAME       e.g. "Sam"; empty means "the user"
    JARVIS_OWNER_PRONOUNS   he, she or they (default they)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_KEYS = ("JARVIS_OWNER_NAME", "JARVIS_OWNER_PRONOUNS")


def _setting(key: str) -> str:
    if key in os.environ:
        return os.environ[key].strip()
    try:
        from dotenv import dotenv_values
        return (dotenv_values(Path(__file__).resolve().parent.parent / ".env").get(key) or "").strip()
    except Exception:
        return ""


NAME = _setting("JARVIS_OWNER_NAME")
PRONOUNS = _setting("JARVIS_OWNER_PRONOUNS").lower() or "they"
if PRONOUNS not in ("he", "she", "they"):
    PRONOUNS = "they"

_FORMS = {
    "he": {"he": "he", "him": "him", "his": "his", "himself": "himself", "theirs": "his"},
    "she": {"he": "she", "him": "her", "his": "her", "himself": "herself", "theirs": "hers"},
    "they": {"he": "they", "him": "them", "his": "their", "himself": "themselves", "theirs": "theirs"},
}

# "they" takes the plural verb. Irregular forms first; the rest drop their -s.
_IRREGULAR = {"is": "are", "was": "were", "has": "have", "does": "do", "isn't": "aren't",
              "wasn't": "weren't", "hasn't": "haven't", "doesn't": "don't", "goes": "go"}
_ADVERBS = r"(?:only|also|just|still|already|never|often|always|ever|then|even|not|\w+ly)"
_THEY_VERB = re.compile(r"«(he|He)»((?:\s+" + _ADVERBS + r")*)\s+([A-Za-z']+)")
_MARKER = re.compile(r"«(owner's|Owner's|owner|Owner|OWNER|he|He|him|his|His|himself|theirs|tz)»")


def _plural(verb: str) -> str:
    low = verb.lower()
    if low in _IRREGULAR:
        return _IRREGULAR[low]
    if re.search(r"[^aeiou]ies$", low):
        return verb[:-3] + "y"
    if re.search(r"(ss|sh|ch|x|z|o)es$", low):
        return verb[:-2]
    if low.endswith("s") and not low.endswith("ss") and len(low) > 3:
        return verb[:-1]
    return verb


def name() -> str:
    return NAME or "the user"


def say(text, *, name_: str | None = None, pronouns: str | None = None):
    """`text` with the owner markers filled in. Strings, and lists, tuples and dicts of
    them (tool schemas), are filled in; anything else comes back as it was."""
    if isinstance(text, dict):
        return {k: say(v, name_=name_, pronouns=pronouns) for k, v in text.items()}
    if isinstance(text, (list, tuple)):
        return type(text)(say(v, name_=name_, pronouns=pronouns) for v in text)
    if not isinstance(text, str) or "«" not in text:
        return text
    who = (NAME if name_ is None else name_) or "the user"
    forms = _FORMS[pronouns or PRONOUNS]
    if (pronouns or PRONOUNS) == "they":
        text = _THEY_VERB.sub(lambda m: f"«{m.group(1)}»{m.group(2)} {_plural(m.group(3))}", text)

    def fill(m: re.Match) -> str:
        k = m.group(1)
        if k == "tz":
            from backend import localtime
            return localtime.TIMEZONE
        if k == "OWNER":
            return who.upper()
        if k.lower().startswith("owner"):
            out = who + ("'s" if k.endswith("'s") else "")
            return out[0].upper() + out[1:] if k[0] == "O" else out
        out = forms[k.lower()]
        return out[0].upper() + out[1:] if k[0].isupper() else out

    return _MARKER.sub(fill, text)

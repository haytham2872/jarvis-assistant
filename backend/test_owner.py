"""The owner's name and pronouns: filled in everywhere, never left as a marker."""
import ast
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

from backend import owner

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("pronouns,expected", [
    ("he", "Ask him. He wants his notes; it is his. He is out."),
    ("she", "Ask her. She wants her notes; it is hers. She is out."),
    ("they", "Ask them. They want their notes; it is theirs. They are out."),
])
def test_pronouns(pronouns, expected):
    text = "Ask «him». «He» wants «his» notes; it is «theirs». «He» is out."
    assert owner.say(text, name_="Sam", pronouns=pronouns) == expected


@pytest.mark.parametrize("verb,plural", [
    ("asks", "ask"), ("has", "have"), ("does", "do"), ("doesn't", "don't"), ("is", "are"),
    ("was", "were"), ("tries", "try"), ("watches", "watch"), ("said", "said"), ("can", "can"),
])
def test_they_takes_the_plural_verb(verb, plural):
    assert owner.say(f"«he» {verb} it", name_="Sam", pronouns="they") == f"they {plural} it"


@pytest.mark.parametrize("text,expected", [
    ("«he» only asks", "they only ask"), ("«he» explicitly asks", "they explicitly ask"),
    ("if «he» ever has one", "if they ever have one"), ("«he» does not want", "they do not want"),
])
def test_an_adverb_between_pronoun_and_verb(text, expected):
    assert owner.say(text, pronouns="they") == expected


def test_the_name_and_its_default():
    text = "«Owner» said it to «owner». «Owner's» note, «owner's» words, «OWNER» SAID"
    assert owner.say(text, name_="Sam") == "Sam said it to Sam. Sam's note, Sam's words, SAM SAID"
    assert owner.say(text, name_="") == (
        "The user said it to the user. The user's note, the user's words, THE USER SAID")


def test_structures_are_filled_in():
    out = owner.say({"a": ["«owner»", ("«his»",)], "n": 3}, name_="Sam", pronouns="she")
    assert out == {"a": ["Sam", ("her",)], "n": 3}


def _says(node, parents) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "say":
                return True
    return False


def test_every_marker_in_the_code_is_filled_in_by_say():
    """A marker in a string that never passes through say() would reach the model as
    «he» or «owner». Every string literal holding one must sit inside a say(...) call."""
    bad = []
    for path in (ROOT / "backend").rglob("*.py"):
        if path.name.startswith("test_") or path.name == "owner.py":
            continue
        src = path.read_text(encoding="utf-8")
        if "«" not in src:
            continue
        tree = ast.parse(src)
        parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        docs = {n.body[0].value for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and "«" in n.value:
                if n not in docs and not _says(n, parents):
                    bad.append(f"{path.relative_to(ROOT)}:{n.lineno}")
    assert not bad, bad


_RENDER = r"""
import json, sys
sys.path.insert(0, ".")
from backend.brain import personality
from backend.tools import registry
from backend.brain import background, consolidation, outward_check, turn_signals
texts = [personality._INVARIANT_CORE, json.dumps(registry.TOOLS),
         consolidation._PROMPT, outward_check.PROMPT, json.dumps(turn_signals.QUESTIONS),
         background._BG_SYSTEM, background._SCHEDULED_SYSTEM]
print(json.dumps(texts))
"""


def _render(name: str, pronouns: str) -> str:
    env = {**os.environ, "JARVIS_OWNER_NAME": name, "JARVIS_OWNER_PRONOUNS": pronouns,
           "JARVIS_JEV": "0"}
    out = subprocess.run([sys.executable, "-c", _RENDER], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    return "\n".join(json.loads(out.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize("name,pronouns", [("Alex", "they"), ("", "she")])
def test_another_owner_reads_cleanly(name, pronouns):
    """The prompt, the tool schemas and the other prompts for someone else: no marker
    left, the test owner's name nowhere, and no "they" with a singular verb."""
    text = _render(name, pronouns)
    assert "«" not in text and "»" not in text
    assert not re.search(r"\bSam\b", text)
    assert (name or "the user") in text
    if pronouns == "they":
        wrong = re.findall(r"\b[Tt]hey (?:is|was|has|does|doesn't|wants|asks|says|tells|sends)\b", text)
        assert not wrong, wrong

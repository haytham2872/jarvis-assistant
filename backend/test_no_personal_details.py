"""No personal detail of the original owner in the tracked files.

This repository is ported from a private one that is full of its owner's name, email and
servers (docs/PORTING.md). A port that forgets to translate one line would publish it, so
every tracked file is scanned here, on every CI run.

The terms are stored as SHA-256 hashes: listing them in plain text would publish them too.
To block another term (a word, or a whole email address), add the hash of its lowercase
form:

    python -c "import hashlib; print(hashlib.sha256(b'term').hexdigest())"

A word matches whole (letters, digits and underscores), so a GitHub username that merely
starts with a blocked name is not a match.
"""
import hashlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BLOCKED = {
    "d30ce5733c1ee807a3398ab27483e79ba65aaf7f4666cc3fe0c40ab3e9d3cf50",  # a first name
    "994b6444377d8db4c8afac52ac9ea1a6356bdd1bfd92abd1912d0bf2e090d5ae",  # a surname
    "8545ba1ddab89cc7b4befbe9d32eebbd22e1acd025e4e3b702f51eeebf757368",  # an email
    "bae69be6cff7387abcff5d6716d6458d4973ff779eb0609f17cdbf44968ec999",  # an email
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_WORD = re.compile(r"\w+")
_MAX_BYTES = 20_000_000


def _hash(term: str) -> str:
    return hashlib.sha256(term.lower().encode()).hexdigest()


def offending_terms(text: str) -> set[str]:
    """The blocked terms in `text`, as they appear."""
    # Emails from the few chunks holding an "@": the email pattern run over a 17 MB
    # file is several seconds, over these chunks nothing.
    found = {m for chunk in text.split() if "@" in chunk for m in _EMAIL.findall(chunk)
             if _hash(m) in BLOCKED}
    found |= {w for w in set(_WORD.findall(text)) if _hash(w) in BLOCKED}
    return found


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                         check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def test_the_scan_finds_a_blocked_word_and_email_but_not_a_longer_word():
    extra = {_hash("zqblockedname"), _hash("zq.person@example.org")}
    BLOCKED.update(extra)
    try:
        text = "Hi Zqblockedname, mail zq.person@example.org; zqblockedname2872 is fine"
        assert offending_terms(text) == {"Zqblockedname", "zq.person@example.org"}
        assert offending_terms("zqblockedname2872 wrote this") == set()
    finally:
        BLOCKED.difference_update(extra)


def test_no_tracked_file_holds_a_personal_detail():
    hits = {}
    for rel in _tracked_files():
        terms = offending_terms(rel)
        path = ROOT / rel
        try:
            if path.stat().st_size <= _MAX_BYTES:
                data = path.read_bytes()
                if b"\0" not in data[:8000]:
                    terms |= offending_terms(data.decode("utf-8", errors="ignore"))
        except OSError:
            continue
        if terms:
            hits[rel] = len(terms)
    # Only the files are named: printing the terms in a CI log would publish them.
    assert not hits, f"personal details in: {sorted(hits)}"

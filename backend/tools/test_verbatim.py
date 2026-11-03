"""H4: `vault_send` delivers a note exactly as saved.

Roadmap acceptance: "`vault_send` output matches the file byte-for-byte (a test hashes
both)". The hash is taken over what actually reached the (fake) Discord channel: the
concatenated message contents inline, or the attachment's bytes for a long note.
"""
import asyncio
import hashlib
import types

import pytest

from backend.brain import engine, discord_bridge
from backend.storage import delivery
from backend.tools import outcomes, vault, verbatim


def run(coro):
    return asyncio.run(coro)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def tmp_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    for folder in vault.FOLDERS + ("skills",):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    return tmp_path


# A note with the things a retelling or a lossy splitter would change: CRLF endings,
# blank-line runs, markdown, trailing spaces, non-ASCII.
_NOTE = ("# NVDA thesis\r\n\r\n\r\n**Stance:** holds -- *for now*.  \r\n"
         "- datacenter +117% y/y\r\n- export controls: Émile's note\r\n") * 3


class _Channel:
    def __init__(self):
        self.id = 42
        self.sent = []

    async def send(self, content=None, file=None):
        body = file.fp.read().decode("utf-8") if file is not None else None
        self.sent.append((content, body))
        return types.SimpleNamespace(id=len(self.sent), channel=self)


@pytest.fixture
def bridge(monkeypatch):
    channel = _Channel()
    monkeypatch.setattr(discord_bridge, "_client", object())
    monkeypatch.setattr(discord_bridge, "_ALLOWED_ID", "1")
    token = discord_bridge.current_channel.set(channel)
    yield channel
    discord_bridge.current_channel.reset(token)


# --- reading ------------------------------------------------------------------------------

def test_read_exact_keeps_the_bytes(tmp_vault):
    (tmp_vault / "topics" / "nvda.md").write_bytes(_NOTE.encode("utf-8"))
    label, text = verbatim.read_exact("nvda.md", "topics")
    assert label == "topics/nvda.md" and _sha(text) == _sha(_NOTE)   # \r\n survives


def test_root_and_skills_are_readable(tmp_vault):
    (tmp_vault / "queue.md").write_bytes(b"[open] a\n")
    (tmp_vault / "skills" / "s.md").write_bytes(b"step 1\n")
    assert verbatim.read_exact("queue.md", "root")[1] == "[open] a\n"
    assert verbatim.read_exact("s.md", "skills")[1] == "step 1\n"
    with pytest.raises(ValueError):
        verbatim.read_exact("../secrets.env", "root")


# --- splitting ------------------------------------------------------------------------------

def test_split_exact_loses_nothing_and_cuts_at_lines():
    text = "".join(f"line {i} " + "x" * (i % 90) + "\n\n" for i in range(400))
    parts = discord_bridge.split_exact(text)
    assert "".join(parts) == text and len(parts) > 1
    assert all(len(p) <= 2000 and p.strip() for p in parts)
    assert all(p.endswith("\n") for p in parts[:-1])


def test_split_exact_cuts_an_overlong_line():
    text = "y" * 5000
    parts = discord_bridge.split_exact(text)
    assert "".join(parts) == text and all(len(p) <= 1900 for p in parts)


# --- sending ------------------------------------------------------------------------------

def test_a_short_note_arrives_inline_byte_for_byte(tmp_vault, bridge):
    (tmp_vault / "topics" / "nvda.md").write_bytes(_NOTE.encode("utf-8"))
    verbatim.set_sender(discord_bridge.send_verbatim)
    try:
        out = run(verbatim.vault_send("nvda.md", "topics"))
    finally:
        verbatim.set_sender(None)
    assert out.startswith("Sent topics/nvda.md") and not outcomes.is_failure("vault_send", out)
    assert _sha("".join(c for c, _ in bridge.sent)) == _sha(_NOTE)


def test_a_long_note_arrives_as_an_identical_file(tmp_vault, bridge):
    long_note = _NOTE * 60
    (tmp_vault / "topics" / "long.md").write_bytes(long_note.encode("utf-8"))
    verbatim.set_sender(discord_bridge.send_verbatim)
    try:
        out = run(verbatim.vault_send("long.md", "topics"))
    finally:
        verbatim.set_sender(None)
    assert "attached file long.md" in out
    ((caption, body),) = bridge.sent
    assert caption == "topics/long.md, exactly as saved:" and _sha(body) == _sha(long_note)


def test_each_part_gets_a_delivery_receipt(tmp_vault, bridge):
    (tmp_vault / "topics" / "nvda.md").write_bytes(_NOTE.encode("utf-8"))
    ok, _how = run(discord_bridge.send_verbatim("topics/nvda.md", _NOTE))
    conn = delivery._conn()
    rows = conn.execute("SELECT state FROM delivery_parts WHERE intent_id LIKE 'verbatim:%'").fetchall()
    assert ok and rows and all(r[0] == "sent" for r in rows)


def test_a_failed_send_is_not_reported_as_sent(tmp_vault):
    (tmp_vault / "topics" / "nvda.md").write_text("x\n", encoding="utf-8")

    async def failing(label, text):
        return False, "part 1 uncertain"
    verbatim.set_sender(failing)
    try:
        out = run(verbatim.vault_send("nvda.md", "topics"))
    finally:
        verbatim.set_sender(None)
    assert out.startswith("Error:") and not not outcomes.is_failure("vault_send", out)


def test_missing_note_and_no_bridge_are_failures(tmp_vault):
    assert outcomes.is_failure("vault_send", run(verbatim.vault_send("nope.md", "topics")))
    (tmp_vault / "topics" / "a.md").write_text("x\n", encoding="utf-8")
    verbatim.set_sender(None)
    out = run(verbatim.vault_send("a.md", "topics"))
    assert out.startswith("Error: Discord is not connected")


# --- wiring -------------------------------------------------------------------------------

def test_the_tool_is_registered_and_dispatched():
    from backend.tools.registry import TOOLS
    assert any(t["name"] == "vault_send" for t in TOOLS)
    assert engine.TOOL_DISPATCH["vault_send"] is verbatim.vault_send


def test_saying_the_note_was_sent_needs_a_successful_vault_send():
    reply = "Done, I've sent you the note as it stands."
    assert engine._unbacked_claims(reply, set(), 0)
    assert engine._unbacked_claims(reply, {"vault_send"}, 1) == []
    assert engine._unbacked_claims("Want me to send you the note?", set(), 0) == []


def test_split_exact_keeps_blank_lines_that_straddle_a_cut():
    """The cut can fall inside a run of blank lines; none of them may be dropped."""
    text = "a" * 1897 + "\n" * 6 + "b" * 10
    parts = discord_bridge.split_exact(text)
    assert "".join(parts) == text and len(parts) == 2

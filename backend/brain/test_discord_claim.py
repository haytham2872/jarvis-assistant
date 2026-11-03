"""/claim: a new install's first owner, through the real on_message."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from backend.brain import discord_bridge as bridge
from backend.brain import discord_claim as claim


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_bytes(b"OPENROUTER_API_KEY=sk-x\r\nDISCORD_BOT_TOKEN=t\r\n"
                    b"DISCORD_ALLOWED_USER_ID=\r\nJARVIS_OWNER_NAME=Sam\r\n")
    monkeypatch.setattr(claim, "ENV_FILE", env)
    monkeypatch.setattr(bridge, "_ALLOWED_ID", "")
    claim._reset_for_tests()
    fake_discord = SimpleNamespace(
        Intents=SimpleNamespace(default=lambda: SimpleNamespace(message_content=False)),
        Client=type("Client", (), {"__init__": lambda self, intents: None}),
    )
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    client = bridge._build_client()
    client.user = SimpleNamespace(id=77)
    turns = []

    async def handler(text):
        turns.append(text)
        return "answered"
    monkeypatch.setattr(bridge, "_turn_handler", handler)
    monkeypatch.setattr(bridge, "_claim_ingress", lambda _: True)
    yield client, env, turns
    claim._reset_for_tests()


def dm(author, content, *, guild=None):
    sent = []

    async def send(text):
        sent.append(text)

    msg = SimpleNamespace(author=SimpleNamespace(id=author, __str__=lambda s: "u"),
                          content=content, guild=guild, attachments=[], id=1,
                          channel=SimpleNamespace(id=5, send=send))
    return msg, sent


def test_the_right_code_in_a_dm_makes_the_sender_the_owner(world):
    client, env, _ = world
    code = claim.open_claim()
    msg, sent = dm(4242424242, f"/claim {code.lower()}")
    run(client.on_message(msg))
    assert bridge._ALLOWED_ID == "4242424242"
    assert env.read_bytes() == (b"OPENROUTER_API_KEY=sk-x\r\nDISCORD_BOT_TOKEN=t\r\n"
                                b"DISCORD_ALLOWED_USER_ID=4242424242\r\nJARVIS_OWNER_NAME=Sam\r\n")
    assert len(sent) == 1 and "owner" in sent[0] and ".env" in sent[0]


def test_a_wrong_code_gets_no_reply_and_five_switch_claiming_off(world):
    client, env, _ = world
    code = claim.open_claim()
    before = env.read_bytes()
    for i in range(claim.MAX_TRIES):
        msg, sent = dm(9, "/claim WRONGONE")
        run(client.on_message(msg))
        assert sent == [] and bridge._ALLOWED_ID == ""
    msg, sent = dm(9, f"/claim {code}")
    run(client.on_message(msg))
    assert sent == [] and bridge._ALLOWED_ID == "" and env.read_bytes() == before


def test_a_claim_outside_a_dm_never_counts(world):
    """The code would be visible to everyone in a server channel."""
    client, _, _ = world
    code = claim.open_claim()
    msg, sent = dm(9, f"/claim {code}", guild=SimpleNamespace(id=1))
    run(client.on_message(msg))
    assert sent == [] and bridge._ALLOWED_ID == ""


def test_the_code_works_once(world):
    client, _, turns = world
    code = claim.open_claim()
    run(client.on_message(dm(1111111, f"/claim {code}")[0]))
    msg, sent = dm(2222222, f"/claim {code}")
    run(client.on_message(msg))
    assert bridge._ALLOWED_ID == "1111111" and sent == [] and turns == []


def test_without_a_code_nothing_can_be_claimed(world):
    """A process started with an owner opens no code."""
    client, _, _ = world
    msg, sent = dm(9, "/claim ")
    run(client.on_message(msg))
    assert not claim.try_claim("9", "/claim None", in_dm=True)
    assert sent == [] and bridge._ALLOWED_ID == ""


def test_once_claimed_strangers_are_still_ignored(world):
    client, _, turns = world
    run(client.on_message(dm(1111111, f"/claim {claim.open_claim()}")[0]))
    run(client.on_message(dm(9, "hello")[0]))
    assert turns == []
    assert bridge._ALLOWED_ID == "1111111"


def test_an_unwritable_env_still_claims_for_this_run_and_says_so(world, monkeypatch):
    client, _, _ = world
    monkeypatch.setattr(claim, "save_owner", lambda _id: False)
    msg, sent = dm(4242424242, f"/claim {claim.open_claim()}")
    run(client.on_message(msg))
    assert bridge._ALLOWED_ID == "4242424242"
    assert "DISCORD_ALLOWED_USER_ID=4242424242" in sent[0]


def test_start_logs_the_code_when_there_is_no_owner(monkeypatch, caplog):
    monkeypatch.setattr(bridge, "_BOT_TOKEN", "t")
    monkeypatch.setattr(bridge, "_ALLOWED_ID", "")
    claim._reset_for_tests()

    def boom():
        raise RuntimeError("no client in tests")
    monkeypatch.setattr(bridge, "_build_client", boom)
    with caplog.at_level("WARNING"):
        run(bridge.start())
    assert f"/claim {claim.open_claim()}" in caplog.text
    claim._reset_for_tests()


@pytest.mark.parametrize("before,after", [
    ("A=1\nDISCORD_ALLOWED_USER_ID=\nB=2\n", "A=1\nDISCORD_ALLOWED_USER_ID=55555\nB=2\n"),
    ("A=1\n# DISCORD_ALLOWED_USER_ID=\n\nB=2\n", "A=1\nDISCORD_ALLOWED_USER_ID=55555\n\nB=2\n"),
    ("A=1\nB=2", "A=1\nB=2\nDISCORD_ALLOWED_USER_ID=55555\n"),
    ("", "DISCORD_ALLOWED_USER_ID=55555\n"),
])
def test_save_owner_replaces_or_adds_one_line(tmp_path, before, after):
    env = tmp_path / ".env"
    env.write_bytes(before.encode())
    assert claim.save_owner("55555", env)
    assert env.read_bytes().decode() == after


def test_save_owner_refuses_anything_but_a_numeric_id(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    assert not claim.save_owner("55555\nOPENROUTER_API_KEY=evil", env)
    assert env.read_text() == "A=1\n"


def test_the_code_is_eight_unambiguous_characters():
    claim._reset_for_tests()
    code = claim.open_claim()
    assert len(code) == 8 and set(code) <= set(claim._ALPHABET)
    assert not set("01IOL") & set(claim._ALPHABET)
    assert claim.open_claim() == code
    claim._reset_for_tests()


def test_an_empty_or_partial_code_never_claims(world):
    client, _, _ = world
    code = claim.open_claim()
    for text in ("/claim", f"/claim {code[:4]}", f"/claim {code}X"):
        msg, sent = dm(9, text)
        run(client.on_message(msg))
        assert sent == [] and bridge._ALLOWED_ID == "", text


def test_try_claim_succeeds_only_once_even_without_the_bridge():
    claim._reset_for_tests()
    code = claim.open_claim()
    assert claim.try_claim("1", f"/claim {code}", in_dm=True)
    assert not claim.try_claim("2", f"/claim {code}", in_dm=True)
    claim._reset_for_tests()

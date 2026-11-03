"""scripts/migrate_skills.py on the server's three real June skills (4 October 2026)."""
import importlib.util
from pathlib import Path

import pytest

from backend.brain import skills
from backend.brain.test_skills import CREATIVE_JUNE, STOCK_JUNE
from backend.storage import skills as store
from backend.tools import vault

TOPIC_JUNE = """---
name: stock-research-to-topic
description: Research a stock ticker and create a structured analysis note in the topics vault folder.
category: research
requires_tools: [web_browse, vault_write]
confidence: 0.76
status: active
created: 2026-06-10
uses: 49
---

## Procedure
1. Search for current stock price.
"""


def _script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "migrate_skills.py"
    spec = importlib.util.spec_from_file_location("migrate_skills", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_VAULT", tmp_path)
    d = tmp_path / "skills"
    d.mkdir()
    for name, text in (("creative-write-on-demand", CREATIVE_JUNE),
                       ("structured-financial-research", STOCK_JUNE),
                       ("stock-research-to-topic", TOPIC_JUNE)):
        (d / f"{name}.md").write_bytes(text.encode("utf-8"))
    return d


def test_the_plan_merges_the_two_stock_skills_and_rewrites_the_creative_one(server):
    mig = _script()
    what = {name: w for name, w, _ in mig.plan()}
    assert what["stock-research"].startswith("new: replaces")
    assert what["stock-research-to-topic"].startswith("retired")
    assert what["structured-financial-research"].startswith("retired")
    assert what["creative-write-on-demand"].startswith("rewritten")


def test_a_dry_run_changes_nothing(server):
    before = {p.name: p.read_bytes() for p in server.iterdir()}
    _script().plan()
    assert {p.name: p.read_bytes() for p in server.iterdir()} == before


def test_apply_leaves_two_current_skills_and_keeps_every_old_text(server):
    mig = _script()
    mig.apply(mig.plan())
    assert [s.name for s in skills.active_skills()] == ["creative-write-on-demand", "stock-research"]
    stock = skills.load("stock-research")
    assert stock.created == "2026-06-10" and stock.source == "migrated"
    assert set(stock.tools) <= {t for t in skills._known_tools()}
    assert "## Checks" in stock.body and "portfolio" in stock.body
    for old in ("stock-research-to-topic", "structured-financial-research"):
        s = skills.load(old)
        assert s.status == "retired" and s.extra.get("superseded_by") == "stock-research"
    assert store.revisions("creative-write-on-demand")[0]["content"] == CREATIVE_JUNE
    assert store.revisions("stock-research-to-topic")[0]["content"] == TOPIC_JUNE
    for p in server.iterdir():
        assert "success_rate" not in p.read_text(encoding="utf-8")
    assert all(len(s.description) <= skills.CATALOG_LINE for s in skills.active_skills())


def test_running_it_twice_changes_nothing_the_second_time(server):
    mig = _script()
    mig.apply(mig.plan())
    assert mig.plan() == []


def test_a_june_skill_it_does_not_know_only_loses_its_counters(server):
    (server / "weekly-email.md").write_text(STOCK_JUNE.replace("structured-financial-research", "weekly-email"),
                                           encoding="utf-8")
    mig = _script()
    changes = {name: (w, s) for name, w, s in mig.plan()}
    what, new = changes["weekly-email"]
    assert what == "counters dropped" and new.body == skills.parse(
        (server / "weekly-email.md").read_text(encoding="utf-8")).body


def test_it_stops_before_changing_a_file_whose_old_text_cannot_be_kept(server, monkeypatch):
    mig = _script()
    changes = mig.plan()
    monkeypatch.setattr(store, "add_revision", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        mig.apply(changes)
    assert (server / "creative-write-on-demand.md").read_text(encoding="utf-8") == CREATIVE_JUNE


def test_an_earlier_migration_is_brought_up_to_the_current_text(server):
    mig = _script()
    mig.apply(mig.plan())
    path = server / "stock-research.md"
    path.write_text(path.read_text(encoding="utf-8").replace("\n- Stopping to ask him for a position", "\n- Old line"),
                    encoding="utf-8")
    assert [(n, w) for n, w, _ in mig.plan()] == [("stock-research", "updated to the current migrated text")]
    mig.apply(mig.plan())
    assert "Stopping to ask him for a position" in path.read_text(encoding="utf-8")
    assert mig.plan() == []


def test_a_stock_research_skill_he_has_changed_is_left_alone(server):
    mig = _script()
    mig.apply(mig.plan())
    s = skills.load("stock-research")
    s.source, s.body = "taught", s.body + "\n- His own extra step."
    skills._write(s)
    assert mig.plan() == []

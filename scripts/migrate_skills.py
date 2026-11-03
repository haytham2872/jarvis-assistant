"""One-off: bring the June 2026 skills into the October format. Run once on the server.

    .venv/bin/python scripts/migrate_skills.py           # shows what it would do
    .venv/bin/python scripts/migrate_skills.py --apply   # does it

What it does (4 October 2026):
  * Every skill still in the June format loses the old counters (confidence, uses,
    success_rate...): they counted turns the skill had been pasted into, not uses, so the
    usage record starts again from zero in SQLite. `requires_tools` becomes `tools`.
  * stock-research-to-topic and structured-financial-research did the same job and date
    from before the portfolio ledger, web_research and news search. They are retired and
    replaced by one skill, stock-research, written for today's tools.
  * creative-write-on-demand keeps its procedure, with a description narrow enough that it
    is only opened on an actual invitation to write.

Nothing is deleted: each file's old text is kept as a revision in the runtime database,
and the retired skills stay in jarvis-vault/skills/. Running it twice changes nothing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.brain import skills  # noqa: E402
from backend.storage import skills as store  # noqa: E402

MERGED = ("stock-research-to-topic", "structured-financial-research")

STOCK_RESEARCH = dict(
    name="stock-research",
    description=("Use when Sam asks to research, look into or dig into a stock, ETF or fund, "
                 "what's going on with one, or whether his thesis on one still holds: dated figures, "
                 "the week's news, bull and bear case, what it means for his position."),
    when_to_use=("He asks to research, analyse or check a listed asset (\"research TTWO\", \"what's "
                 "going on with Nvidia\", \"is my Tesla thesis still good\"). Not for a quick price "
                 "(\"what's NVDA at?\"): one search answers that. For a long sourced report he is "
                 "happy to wait for, use web_research instead."),
    steps=[
        "If he may hold it, read his position and thesis first with portfolio (action \"show\", "
        "symbol the ticker): his reasons and \"wrong if\" conditions are what the research has to answer.",
        "Get the current figures from a live page: price with its date and time, market cap, P/E, "
        "52-week range and the next earnings date. Note the source and time of each figure.",
        "Search the last week's news with web_browse (kind \"news\", recent_days 7) and read the "
        "one or two pieces that matter in full with fetch_page.",
        "Cover four angles with one search each, called together: results and competitors; "
        "regulation and structural factors; macro and sentiment; analyst views and price targets.",
        "Write the bull case and the bear case with the same care, in numbers rather than adjectives.",
        "If he holds it, say what the findings mean for his thesis: which reasons still hold, which "
        "are weaker, and whether a \"wrong if\" condition is close. Take his P&L from portfolio; "
        "never work it out yourself.",
        "Save the analysis with vault_write to topics/, named like the earlier notes on that ticker "
        "(otherwise YYYY-MM-DD_<ticker>-stock-research.md), with the sections Snapshot, News, Bull "
        "case, Bear case, Analyst views, Next catalysts and For his position, linking the earlier "
        "notes on the ticker and its thesis note.",
        "Tell him the three or four things that matter, with their dates, and where the note is.",
    ],
    pitfalls=[
        "A figure without its date and time: cached pages can be hours old.",
        "Mixing share classes or listings (GOOG and GOOGL; which exchange a European listing is on).",
        "A token bear case when the news is good, or a token bull case when it is bad.",
        "Stopping to ask him for a position the ledger doesn't hold: no recorded position means "
        "research it as a candidate, and say it isn't in the ledger.",
        "Adding a \"not financial advice\" line: he doesn't want it.",
    ],
    checks=[
        "Every figure in the reply has its source and date.",
        "The reply has both a bull case and a bear case.",
        "If he holds the asset, the reply says what the findings mean for his thesis.",
    ],
    tools=["portfolio", "web_browse", "fetch_page", "vault_write"],
)

CREATIVE = dict(
    name="creative-write-on-demand",
    description=("When Sam invites you to write something of your own with no set topic "
                 "(\"write something if you want\", \"surprise me with a piece\"), write an "
                 "original short piece and keep it in creative/."),
    when_to_use=("Only on an open invitation to write freely. Not when he asks for a specific text "
                 "(an email, a post, a summary, a message), and never on your own in the middle "
                 "of other work."),
    steps=[
        "Take the invitation as it is: don't ask him for a topic.",
        "Pick a form that suits the moment: a prose fragment, a poem, a very short story or a meditation.",
        "Draw on what is alive right now: your mood, what you have been talking about lately, "
        "something you keep wondering about.",
        "Write it in plain prose without Markdown symbols: it may be read aloud.",
        "Save it with vault_write to creative/ under a new kebab-case filename, linking related "
        "notes with [[wiki-links]]; never overwrite an earlier piece.",
        "Give him the piece in your reply, and say it is saved only once vault_write has confirmed it.",
    ],
    pitfalls=[
        "Overwriting an earlier piece: pick a new filename.",
        "Asking what he would like: the invitation is the brief.",
        "Markdown symbols in the text.",
    ],
    checks=["The piece is original and in plain text.", "It was saved before the reply says so."],
    tools=["vault_write"],
)


def _is_legacy(text: str) -> bool:
    head = text.split("\n---", 1)[0]
    return any(line.split(":", 1)[0].strip() in skills._LEGACY_KEYS
               for line in head.splitlines()[1:])


def _skill_from(spec: dict, *, created: str, source: str) -> skills.Skill:
    today = skills._today()
    return skills.Skill(
        name=spec["name"], description=spec["description"],
        body=skills.build_body(spec["when_to_use"], spec["steps"], spec["pitfalls"], spec["checks"]),
        status=skills.ACTIVE, source=source, tools=list(spec["tools"]),
        created=created or today, updated=today)


def plan() -> list[tuple[str, str, object]]:
    """[(name, what, new Skill or None)] -- the changes, without making any."""
    d = skills.skills_dir()
    texts = {p.stem: p.read_text(encoding="utf-8") for p in sorted(d.glob("*.md"))} if d.exists() else {}
    changes = []
    old_stock = [n for n in MERGED if n in texts and skills.parse(texts[n], n).active]
    if old_stock and "stock-research" not in texts:
        created = min(skills.parse(texts[n], n).created or skills._today() for n in old_stock)
        changes.append(("stock-research", "new: replaces " + " and ".join(old_stock),
                        _skill_from(STOCK_RESEARCH, created=created, source="migrated")))
        for n in old_stock:
            s = skills.parse(texts[n], n)
            s.status, s.updated = skills.RETIRED, skills._today()
            s.extra = {**s.extra, "superseded_by": "stock-research"}
            changes.append((n, "retired: merged into stock-research", s))
    # A stock-research written by an earlier run of this script, and not edited since (an
    # edit through the skill tool changes its source), is brought up to the current text.
    if "stock-research" in texts:
        current = skills.parse(texts["stock-research"], "stock-research")
        fresh = _skill_from(STOCK_RESEARCH, created=current.created, source="migrated")
        if current.source == "migrated" and current.body != fresh.body:
            fresh.status = current.status
            changes.append(("stock-research", "updated to the current migrated text", fresh))
    for name, text in texts.items():
        if any(c[0] == name for c in changes) or not _is_legacy(text):
            continue
        old = skills.parse(text, name)
        if name == CREATIVE["name"]:
            new = _skill_from(CREATIVE, created=old.created, source=old.source)
            new.status = old.status
            changes.append((name, "rewritten: narrower description, Checks added, counters dropped", new))
        else:
            old.updated = skills._today()
            changes.append((name, "counters dropped", old))
    return changes


def apply(changes) -> list[str]:
    done = []
    for name, what, new in changes:
        path = skills.skills_dir() / f"{name}.md"
        if path.exists():
            if store.add_revision(name, "migrate", path.read_bytes().decode("utf-8"),
                                  actor="system", reason=what) is None:
                raise SystemExit(f"Could not keep the old text of {name}; nothing more was changed.")
        skills._write(new)
        done.append(f"{name}: {what}")
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="make the changes (default: only show them)")
    args = ap.parse_args()
    changes = plan()
    if not changes:
        print("Nothing to migrate.")
        return
    for name, what, _ in changes:
        print(f"{'' if args.apply else 'would change '}{name}: {what}")
    if args.apply:
        apply(changes)
        print(f"Done: {len(changes)} change(s). Old versions are kept as revisions.")
    else:
        print("Dry run. Add --apply to make these changes.")


if __name__ == "__main__":
    main()

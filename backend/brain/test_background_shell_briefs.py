"""Background briefs that need a shell are refused before a job starts."""
import asyncio

import pytest

from backend.brain import background



# ---------------------------------------------------------------------------
# Briefs that need a shell (live, 2026-09-22: two git-command tasks failed the same way)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("task", [
    "Run git log on the repo and summarise the last week of commits",
    "use the terminal to check disk usage",
    "Check `git status` and git diff for uncommitted work",
    "execute a shell command to list the backend folder",
])
def test_a_brief_that_needs_a_shell_is_refused_before_it_starts(task):
    before = dict(background._tasks)
    r = asyncio.run(background.run_in_background(task))
    assert r.startswith("Error:") and "evolution.md" in r and "code_graph" in r
    assert background._tasks == before


@pytest.mark.parametrize("task", [
    "Summarise what the git history says changed this week (read self/evolution.md)",
    "Research how terminal velocity works for skydivers",
])
def test_mentioning_git_or_a_terminal_is_not_a_command(task):
    assert not background._NEEDS_SHELL.search(task)

# Porting from the private repository

This project is the public copy of a private JARVIS that one person uses every day. Features
and fixes land there first; this file says how they come here, and records what has.

The private repository and its history never become public. What comes over is the change,
translated so that it works for any owner.

## The routine

1. **Every private commit is marked when it is made**, with one trailer line:
   - `Port: yes`: worth having here.
   - `Port: no (<reason>)`: private servers, personal data, or only useful to its owner.
   - `Port: later (<reason>)`: worth having, but it needs a design for other owners first.

   Commits without a trailer (made before this routine) are decided by reading them.
2. **Porting is done in batches**, when asked: say after a feature is finished, or weekly.
   Commits that only make sense together are ported together.
3. **A batch** starts after the watermark below, takes the commits marked `yes` (and any
   unmarked ones judged worth it), ports each as described next, and ends by updating the
   log and the watermark in the same commit as the last port.
4. **Nothing is pushed without the owner's yes.** The pushed branch is `public`.

## Branches

- `public` (pushed as `main` on GitHub): an orphan branch with no private history. Ports
  are committed here.
- `main` (local only): the history of how the public copy was made, which holds personal
  details and must never be pushed. Each port is cherry-picked onto it as well, so the two
  trees stay identical (`git rev-parse main^{tree} public^{tree}` prints one hash twice).

## How to bring a commit over

From this repository, with the private one as a temporary remote:

```bash
git remote add private "<path to the private repo>" && git fetch private
git cherry-pick -n <sha>          # a three-way merge, nothing committed yet
```

- **Resolve conflicts hunk by hunk.** Never `git checkout --theirs <file>`: it takes the
  whole private file, with every personal detail in it, not only the conflicting lines.
- **Read the staged diff in full** and translate it with the table below.
- For a commit that touches files that do not exist here, or exist under another name, take
  only the relevant files: `git diff <sha>^ <sha> -- <files> | git apply -3 --index`, after
  renaming paths as the table says.
- The commit message says what changed in this project's terms and ends with
  `Port of private <short sha>.`
- Remove the remote when done: `git remote remove private`.

## Translation

| In the private repo | Here |
|---|---|
| The owner's name, "he", "his" in prompts, tool descriptions, messages and the self-model | `«owner»`, `«he»`, `«his»`, `«him»`... filled in by `owner.say()` (`backend/owner.py`) |
| A fixed time zone (Europe/Paris) in code | `localtime.ZONE`; `«tz»` in prompt text. Europe/Paris stays only as a test fixture or an example |
| Euro amounts as the portfolio's currency | the base currency (`JARVIS_BASE_CURRENCY`) |
| `backend/brain/claude.py`, `test_claude_*.py`, log prefix `claude:` | `backend/brain/engine.py`, `test_engine_*.py`, `engine:` |
| The "Active self-model" sections of CLAUDE.md | `docs/SELF_MODEL.md` (same headings) |
| The rest of the private CLAUDE.md (dated notes, measurements, incidents) | not copied; `CLAUDE.md` here keeps only the working rules. A good incident can become an entry in `docs/engineering-log.md`, without personal details |
| Server names, IP addresses, Tailscale names, cloud project, account ids, vault content, real emails | never; use placeholders (`host.ts.net`, `anna@gmail.com`) |
| Image and package pins (Qdrant, SearXNG, qdrant-client) | this project's own pins: Qdrant and its client move together, one minor version at a time |
| `graphify-out/graph.json` | never taken from the port: rebuild here with `python -m graphify update .` |

Files that exist only here: `docs/SETUP.md`, `setup.sh`, `deploy/jarvis.service`,
`docs/SELF_MODEL.md`, `docs/engineering-log.md`, `/claim` (`backend/brain/discord_claim.py`),
`backend/owner.py`, `backend/test_install_files.py`, `backend/test_no_personal_details.py`.
A ported change that should affect them is applied to them by hand.

## Checks before committing a port

- The test suite passes: `pytest backend/`.
- No personal detail: `backend/test_no_personal_details.py` scans every tracked file for
  the private owner's name and emails (stored as hashes) and runs in CI on every push.
- A search of the added lines for anything the table above should have translated:
  `git diff --cached -U0 | grep '^+' | grep -n -i -E "paris|eur\b|claude\.py|ts\.net|tailscale|gcp"`
  (each hit is read: a test fixture or an example is fine).
- Before a push, gitleaks on the `public` branch finds nothing:
  `gitleaks git --log-opts="public" .`

## Watermark

Last private commit reviewed: `8ea7467`

## Log

The private repository was copied at `b77c08e` (3 November 2026). Every private commit
since then is listed, newest first.

| Private | What | Here |
|---|---|---|
| `8ea7467` | Queue [hold] status and weekly-unique brief items, the tool-facts block in consolidation, the portrait edited by typed patches and fading, the phone's Portrait screen | ported (the portrait is `people/owner.md` with its history in `people/owner-history.md`; its sections are "Situation", "Who they are", "How to be with them" so they fit any owner) |
| `b322329` | Discord supervisor, commands shared by Discord and the phone, /pause spares scheduled tasks, research v2 (note kinds, subject matching, rests and retirement, a daily budget, findings to the Inbox, thumbs feedback) | ported (the private server's measurements and incident dates are not copied) |
| `d3a4b9a` | Implementer agent: move the worktree onto the working branch | not ported: private dev tooling |
| `3f2874b` | Track skills and agents, ruff in the guard, a server-logs skill | not ported: private dev tooling |
| `9a56b04` | Code graph rebuilt | not ported: the graph is rebuilt here, never copied |
| `32d97ff` | Research follows what the owner reads: note opens, a 30-day stop, a monthly brief line | ported |
| `23cf246` | CLAUDE.md: how development runs (planner, workers, guard, dashboard) | not ported: private CLAUDE.md dev notes |
| `d43c40e` | CLAUDE.md: the Port: trailer and the jarvis-port skill | not ported: private CLAUDE.md dev notes (the routine is this file) |
| `d565cd5` | The suite in 2 minutes: one slow test fixed, parallel by default, testmon | ported (CLAUDE.md's test section rewritten here) |
| `a6925e7` | Qdrant and its client pinned to the versions the private server runs (1.19) | not ported: this project keeps its own pins (Qdrant 1.17.1 with its client) |
| `171b835` | OpenRouter only, and fixes found while making this copy | nothing to port: it came from here |

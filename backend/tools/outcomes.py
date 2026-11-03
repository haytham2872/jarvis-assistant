"""A04: typed tool outcomes, so a failed action cannot be recorded as a success.

The roadmap's confirmed finding: *"`_is_error_result` misses failure strings from vault,
Gmail, Calendar, and search … Failed actions can appear successful; later features can
consume errors as evidence."*

`_is_error_result` only recognised the `Error:` / `Unknown tool:` prefixes. Every adapter
also has its own failure vocabulary that predates that convention, and none of it
matched. Surveyed from the actual source:

    vault      "Not found: topics/x.md"      "Unknown folder 'foo'. Valid folders: …"
    gmail      "Gmail auth failed: …"        "Gmail error: …"      "Unknown action: …"
    calendar   "Calendar auth failed: …"     "Calendar error: …"   "Unknown action: …"
    browser    "Could not fetch …"           "SEARCH FAILED …"
    notion     "Unknown notion_action action: …"
    spotify    "Spotify not set up: …"       "Unknown action: …"

All of those returned `ok=True`. The A02 receipt layer caught it live on its first real
run: a `vault_read` of a missing file was recorded `ok`.

**Patterns are keyed per tool, not applied globally.** A global prefix list would
misclassify a legitimate result that merely *quotes* one of these strings — a vault note
about error handling, or a web page containing "Gmail error:". Only `gmail_action`'s own
output is checked against gmail's vocabulary.

**Unrecognised text is `ok`, deliberately.** The roadmap is explicit: *"Adapter
normalization must not turn unknown text into false success"* — and equally must not
invent failure. An adapter returning something this module has never seen is the
adapter's problem to fix at source; guessing here would make the classification
unpredictable. It also warns, once per (tool, shape), so new failure vocabulary surfaces
instead of hiding.

This is the *transitional* layer the roadmap sanctions ("Initially wrap existing
implementations rather than rewrite each integration … Then replace ambiguous strings at
their source incrementally"). It is not meant to be permanent: `_PATTERNS` shrinking over
time, as adapters return structured outcomes themselves, is the success condition.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

OK = "ok"
FAILED = "failed"
UNKNOWN = "unknown"
# P1: the call was not made because it needs the owner's approval first. Only the tool
# gateway produces this (`classify` never does): the action neither happened nor failed.
PENDING = "pending"

# Applies to every tool: the in-band convention adapters were meant to follow.
#
# A plain "Error:" prefix is not enough -- LIVE probing showed adapters wrap their own
# errors and so never produce it: vault returns "Error reading from vault: …" and
# "Error writing to vault: …", memory_search returns "Error searching memory: …". This
# pattern accepts a short qualifying phrase between "Error" and the colon.
# `Error\b` (a word boundary) stops it matching prose like
# "Errors in the code are:".
_GLOBAL_FAILURE_RE = re.compile(r"^Error\b[^:\n]{0,40}:")
_GLOBAL_FAILURE_PREFIXES = ("Unknown tool:",)

# Fetched web content is wrapped in an untrusted-data banner BEFORE being returned, so a
# failure string ends up in the middle of the result and no prefix match can see it.
# Stripping the known banner first is exact -- it is a fixed constant, not a guess.
_UNTRUSTED_BANNER_MARK = "the text below was fetched from the internet"
# Email content carries its own banner (P1, tools/gmail.py); stripped the same way.
_UNTRUSTED_EMAIL_MARK = "written by other people and arrived by email"

# Per-tool failure prefixes, surveyed from each adapter's own source. Prefix matching,
# not substring: `vault_read` returns "Not found: …" as the whole result, whereas a note
# whose body merely mentions "Not found:" is legitimate content.
_PATTERNS: dict[str, tuple[str, ...]] = {
    "vault_read":        ("Not found:", "Unknown folder"),
    "vault_send":        ("Not found:", "Unknown folder"),
    "vault_write":       ("Unknown folder",),
    "vault_share":       ("Not found:", "Unknown folder"),
    "vault_list":        ("Unknown folder",),
    "vault_write_queue": ("Unknown folder",),
    "gmail_action":      ("Gmail auth failed:", "Gmail error:", "Unknown action:"),
    "calendar_action":   ("Calendar auth failed:", "Calendar error:", "Unknown action:"),
    "spotify_action":    ("Spotify not set up:", "Spotify error:", "Unknown action:"),
    "notion_action":     ("Unknown notion_action action:",),
    "web_browse":        ("Could not fetch", "SEARCH FAILED"),
    "fetch_page":        ("Could not fetch", "Non-HTML content"),
    "code_graph":        ("graphify is not installed",),
}

# Results that LOOK like failures but are legitimate: an empty inbox is a successful
# query, and being free in a time range is a successful availability check. Checked
# before the failure patterns so a future pattern cannot swallow them.
_NOT_FAILURES: dict[str, tuple[str, ...]] = {
    "gmail_action":    ("Inbox is empty.",),
    "calendar_action": ("You are free",),
}

_warned: set = set()


def classify(tool_name: str, result) -> str:
    """`ok | failed` for one tool result.

    `unknown` is never returned from here — it describes a remote action whose outcome
    could not be established (a cancelled or timed-out external write), which only the
    caller knows about. See `engine._exec_tool`, which records `unknown` on cancellation.
    """
    if not isinstance(result, str):
        return OK                       # non-string results are structured successes

    text = _unwrap(result).lstrip()
    if _GLOBAL_FAILURE_RE.match(text) or text.startswith(_GLOBAL_FAILURE_PREFIXES):
        return FAILED

    for allowed in _NOT_FAILURES.get(tool_name, ()):
        if text.startswith(allowed):
            return OK

    for prefix in _PATTERNS.get(tool_name, ()):
        if text.startswith(prefix):
            return FAILED

    return OK


def _unwrap(result: str) -> str:
    """Strip the untrusted-web-content banner so the real result can be classified.

    `web_browse`/`fetch_page` return `_wrap_untrusted(content)`, which prepends a fixed
    warning block. Live probing found a dead URL returning the banner followed by
    "Could not fetch …" -- classified `ok`, because the failure text was no longer at the
    start. Only the known banner is removed; nothing else is reinterpreted.
    """
    head = result[:300]
    if _UNTRUSTED_BANNER_MARK not in head and _UNTRUSTED_EMAIL_MARK not in head:
        return result
    end = result.find("]\n\n")
    return result[end + 3:] if end != -1 else result


def is_failure(tool_name: str, result) -> bool:
    return classify(tool_name, result) == FAILED


def note_unrecognised(tool_name: str, result) -> None:
    """Warn once per (tool, leading phrase) when a result matches nothing known.

    Not a classification — everything unrecognised stays `ok`. This exists so new
    failure vocabulary introduced by an adapter surfaces in the log instead of silently
    being counted as success, which is exactly how the current gap went unnoticed.
    """
    if not isinstance(result, str) or not result.strip():
        return
    shape = " ".join(result.strip().split()[:3])[:60]
    key = (tool_name, shape)
    if key in _warned:
        return
    _warned.add(key)
    log.debug("outcomes: unclassified result shape from %s: %r", tool_name, shape)


def known_tools() -> set:
    """Tools with adapter-specific failure vocabulary recorded here."""
    return set(_PATTERNS)

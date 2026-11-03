"""Tool-call markup a model wrote as text, taken out of an answer.

On a step offered no tools (the last step of a background job, the forced synthesis of a
live turn), DeepSeek V4.1 Flash sometimes writes the call it still wanted to make as plain
text in its own markup:

    Saving the note now:

    <｜DSML｜ calls>
    <｜DSML｜ invoke name="vault_write">
    <｜DSML｜ parameter name="path" string="true">notes/x.md</｜DSML｜ parameter>
    ...
    </｜DSML｜ invoke>
    </｜DSML｜ calls>

Reproduced live (2026-10-15): 3 of 4 tool-less final steps after a tool call did this, and
the phone showed the markup at the top of a background job's result. Nothing ran: the call
is only text. So the block goes, and so does the lead-in line announcing it ("Saving the
note now:"), which would otherwise claim a save that never happened.
"""
from __future__ import annotations

import re

_BAR = r"[｜|]"
# One whole block, or an opened block running to the end (cut off at the output limit).
_BLOCK = re.compile(
    rf"<{_BAR}DSML{_BAR}\s*(?:function_)?calls\s*>.*?(?:</{_BAR}DSML{_BAR}\s*(?:function_)?calls\s*>|\Z)",
    re.DOTALL)
# DeepSeek's older special-token form of the same thing.
_OLD_BLOCK = re.compile(r"<｜tool▁calls▁begin｜>.*?(?:<｜tool▁calls▁end｜>|\Z)", re.DOTALL)
_STRAY = re.compile(rf"</?{_BAR}DSML{_BAR}[^>]*>|<｜tool▁[^｜]*｜>")
# A short line ending with a colon right before the block: "Saving note to the vault:".
_LEAD_IN = re.compile(r"(?:^|\n)[ \t]*(?:-{3,}|\*{3,})?[ \t]*[^\n]{0,160}:[ \t]*\n*\Z")


def has_markup(text: str) -> bool:
    return bool(text) and bool(_STRAY.search(text))


def strip(text: str) -> str:
    """The answer without any tool-call markup; unchanged when there is none."""
    if not has_markup(text):
        return text
    out, pos = [], 0
    for m in sorted(list(_BLOCK.finditer(text)) + list(_OLD_BLOCK.finditer(text)),
                    key=lambda m: m.start()):
        if m.start() < pos:
            continue
        before = text[pos:m.start()]
        before = _LEAD_IN.sub("", before.rstrip()) if before.strip() else before
        out.append(before)
        pos = m.end()
    out.append(text[pos:])
    cleaned = _STRAY.sub("", "".join(out))
    cleaned = re.sub(r"\n(?:[ \t]*(?:-{3,}|\*{3,})[ \t]*\n?)+\s*\Z", "\n", cleaned)   # a dangling rule
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

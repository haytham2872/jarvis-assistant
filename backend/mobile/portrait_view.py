"""Read-only view of the owner's portrait and the lines that have faded out of it."""
import logging

from backend.brain import portrait
from backend.tools import vault

log = logging.getLogger(__name__)


def _read(name: str) -> str:
    path = vault._VAULT / "people" / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _entries(body: list[str]) -> list[dict]:
    out = []
    for line in body:
        e = portrait._entry(line)
        if e:
            out.append({"text": e[0], "as_of": e[1]})
    return out


def view() -> dict:
    out = {"migrated": False, "sections": [], "faded": [], "fading_days": portrait.AGE_OUT_DAYS}
    try:
        md = _read("owner.md")
        if portrait.is_v2(md):
            out["migrated"] = True
            out["sections"] = [{"title": h[3:].strip(), "entries": _entries(body)}
                               for h, body in portrait.parse(md)[1]]
        faded = [{"faded_on": h[3:].strip(), "entries": _entries(body)}
                 for h, body in portrait.parse(_read("owner-history.md"))[1]]
        out["faded"] = sorted((g for g in faded if g["entries"]),
                              key=lambda g: g["faded_on"], reverse=True)
    except Exception:
        log.warning("portrait view unreadable", exc_info=True)
        return {"migrated": False, "sections": [], "faded": [], "fading_days": portrait.AGE_OUT_DAYS}
    return out

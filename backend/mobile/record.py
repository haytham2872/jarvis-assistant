"""Next plan, slice 5: the same scored record as Discord, including an honest thin record."""
from __future__ import annotations
from backend import localtime as _localtime

import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.brain import forecasts as F
from backend.storage import forecasts as store

log = logging.getLogger(__name__)
LOCAL_TZ = _localtime.ZONE


def view() -> dict:
    """One storage snapshot; unavailable must never masquerade as no forecasts."""
    empty = {"available": False, "summary": {"n": 0}, "summary_gut": {"n": 0}, "bands": [], "bands_gut": [],
           "by_month": [], "by_domain": [], "settled": [], "open": [], "void_count": 0, "withdrawn_count": 0,
           "early": True, "threshold": F._EARLY_RECORD, "text": "", "caution": "", "notes": []}
    out = dict(empty)
    try:
        rows = store.query(strict=True)
        for row in rows:
            required = ("raw_p", "stated_p", "resolve_by", "created_at") + (("resolved_at",) if row["state"] == store.RESOLVED else ())
            if not all(math.isfinite(row[k]) for k in required) or not all(0 <= row[k] <= 1 for k in ("raw_p", "stated_p")):
                raise ValueError("Invalid forecast probability or date")
            if row["state"] == store.RESOLVED and row["outcome"] not in (0, 1):
                raise ValueError("Invalid settled forecast outcome")
        settled = [r for r in rows if r["state"] == store.RESOLVED]
        text = F.record_text(rows=rows)
        out.update(available=True, summary=F.summarize(settled), summary_gut=F.summarize(settled, "raw_p"),
                   bands=F.bands(settled), bands_gut=F.bands(settled, "raw_p"), text=text,
                   early=len(settled) < F._EARLY_RECORD,
                   caution=next((line for line in text.splitlines() if line.startswith("Too early to tell:")), ""),
                   void_count=sum(r["state"] == store.UNRESOLVABLE for r in rows),
                   withdrawn_count=sum(r["state"] == store.WITHDRAWN for r in rows))
        # Keep the fields needed for an honest call sheet; queries are sources, not invented URLs.
        fields = ("claim", "criteria", "domain", "raw_p", "stated_p", "outcome", "resolved_at", "resolver",
                  "resolution_quote", "check_query", "resolve_by", "method", "created_at", "source_quote")
        def card(row):
            return {"id": row["forecast_id"], **{k: row[k] for k in fields}}
        out["settled"] = [card(r) for r in sorted(settled, key=lambda r: (r["resolved_at"] or 0, r["forecast_id"]), reverse=True)]
        out["open"] = [card(r) for r in sorted((r for r in rows if r["state"] == store.OPEN),
                                              key=lambda r: (r["resolve_by"], r["forecast_id"]))]
        months = {}
        for row in settled:
            month = datetime.fromtimestamp(row["resolved_at"], LOCAL_TZ).strftime("%Y-%m")
            months.setdefault(month, []).append(row)
        def aggregate(group):
            return {"n": len(group), "brier": F.summarize(group)["brier"],
                    "brier_gut": F.summarize(group, "raw_p")["brier"]}
        out["by_month"] = [{"month": m, **aggregate(months[m])} for m in sorted(months)]
        out["by_domain"] = [{"domain": d, **aggregate([r for r in settled if r["domain"] == d])}
                            for d in F.DOMAINS if any(r["domain"] == d for r in settled)]
        return out
    except Exception as exc:
        log.warning("mobile record: record could not be read or scored (%s)", exc, exc_info=True)
        # Discard partial calculations: a missing portion cannot look like a complete score.
        return {**empty, "text": "The forecast record could not be read just now.",
                "notes": ["The forecast record could not be read just now. Refresh to try again."]}


def hub() -> dict:
    record = view()
    if not record["available"]:
        return {"notes": record["notes"]}
    return {"record": {"settled": record["summary"]["n"], "brier": record["summary"].get("brier"),
                       "early": record["early"]}}

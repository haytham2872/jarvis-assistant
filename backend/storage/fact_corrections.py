"""User-authored corrections that supersede retrieved personal assertions.

This store is intentionally append-only. It does not mutate Mem0, Qdrant, or
vault files; read paths must honor it before showing old assertions to a model.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

from backend.storage import db

log = logging.getLogger(__name__)


def _normal(text: str) -> str:
    return " ".join(re.findall(r"[\w]+", (text or "").casefold()))


def add(user_id: str, claim: str, correction: str,
        terms: list[str] | None = None) -> str | None:
    claim, correction = claim.strip(), correction.strip()
    cleaned = [t.strip() for t in (terms or [])
               if len(_normal(t)) >= 5 and not _normal(t).isdigit()][:12]
    if not user_id or not claim or not correction:
        return None
    conn = db.connect()
    if conn is None:
        return None
    correction_id = str(uuid.uuid4())
    try:
        with conn:
            conn.execute(
                "INSERT INTO fact_corrections "
                "(id, user_id, claim, correction, terms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (correction_id, user_id, claim, correction,
                 json.dumps(cleaned), time.time()),
            )
        return correction_id
    except Exception as exc:
        log.warning("Fact correction could not be saved: %s", exc)
        return None


def list_for_user(user_id: str, limit: int = 100) -> list[dict]:
    conn = db.connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM fact_corrections WHERE user_id=? "
            "ORDER BY created_at DESC LIMIT ?", (user_id, limit),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["terms"] = json.loads(item["terms"])
            except (TypeError, ValueError):
                item["terms"] = []
            out.append(item)
        return out
    except Exception as exc:
        log.warning("Fact corrections could not be read: %s", exc)
        return []


def suppresses(text: str, corrections: list[dict]) -> bool:
    candidate = _normal(text)
    if not candidate:
        return False
    for entry in corrections:
        claim = _normal(entry.get("claim", ""))
        if len(claim) >= 12 and (claim in candidate or
                                 (len(candidate) >= 0.7 * len(claim) and
                                  candidate in claim)):
            return True
        for term in entry.get("terms", []):
            needle = _normal(term)
            if len(needle) >= 5 and f" {needle} " in f" {candidate} ":
                return True
    return False

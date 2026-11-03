"""Shadow promise-check verdicts (G3, B1): one row per future commitment found in a reply.
Never shown to the owner; read by measurement scripts."""
from __future__ import annotations

import logging

from backend.storage import db, lifelog

log = logging.getLogger(__name__)


def record(*, run_id: str | None, channel: str, kind: str, verdict: str, backing: str,
           sentence: str) -> None:
    conn = db.connect()
    if conn is None:
        return
    now, day = lifelog.stamp()
    try:
        with conn:
            conn.execute(
                "INSERT INTO promise_checks (run_id, channel, kind, verdict, backing, sentence,"
                " checked_at, local_date) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, channel, kind, verdict, backing, lifelog.short(sentence), now, day))
    except Exception as e:
        log.debug("promises: not recorded (%s)", e)

"""Live events for the phone: ordered, bounded, resumable (docs/archive/MOBILE-APP-PLAN.md section 5).

Every event gets a sequence number within this process's epoch. A phone that reconnects
asks for the events after the last sequence it saw; when its epoch is from an earlier
process, or the events it missed have left the buffer, it gets told to fetch a snapshot
instead (the durable records are the truth; these events are only the live view).

Token deltas and thinking are here and nowhere else, so they are lost on restart by design;
the final answer is also stored in mobile_requests.
"""
from __future__ import annotations

import asyncio
import collections
import time
import uuid

BUFFER = 2_000           # events kept for reconnects
SUBSCRIBER_QUEUE = 1_000  # a phone this far behind is dropped and resyncs from a snapshot

EPOCH = uuid.uuid4().hex[:12]


class Hub:
    def __init__(self, buffer: int = BUFFER) -> None:
        self.epoch = EPOCH
        self._seq = 0
        self._events: collections.deque[dict] = collections.deque(maxlen=buffer)
        self._subscribers: set[asyncio.Queue] = set()

    @property
    def seq(self) -> int:
        return self._seq

    def publish(self, event: dict) -> dict:
        self._seq += 1
        evt = {**event, "seq": self._seq, "epoch": self.epoch, "at": time.time()}
        self._events.append(evt)
        for q in list(self._subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                self._subscribers.discard(q)     # too slow: it resyncs from a snapshot
        return evt

    def since(self, epoch: str, after: int) -> list[dict] | None:
        """The events after `after`, or None when the phone must fetch a snapshot."""
        if epoch != self.epoch:
            return None
        if after > self._seq:
            return None
        if not self._events:
            return [] if after == self._seq else None
        oldest = self._events[0]["seq"]
        if after < oldest - 1:
            return None
        return [e for e in self._events if e["seq"] > after]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def is_subscribed(self, q: asyncio.Queue) -> bool:
        return q in self._subscribers


HUB = Hub()

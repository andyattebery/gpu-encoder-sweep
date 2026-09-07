"""sweep/hub/queue.py -- the queue's interface, and the in-memory queue M1 runs on. The Redis one (streams per host,
consumer groups, XAUTOCLAIM on restart) is M2's; this one has the same contract so the app never knows which."""
from collections import deque
from typing import NamedTuple, Protocol

from sweep.hub.refusals import Refusal


class Entry(NamedTuple):
    entry_id: str
    host: str
    run_id: str
    plan: dict


class Queue(Protocol):
    def enqueue(self, host, run_id, plan) -> str: ...
    def claim(self, host) -> Entry | None: ...       # the host's own claimed-unacked entry first, else the next queued
    def ack(self, host, entry_id) -> None: ...       # only the entry the host holds; anything else is refused
    def pending(self, host) -> list[Entry]: ...      # queued, plus the claimed-unacked one


class FakeQueue:
    def __init__(self):
        self._queued = {}      # host -> deque of Entry
        self._claimed = {}     # host -> the one Entry claimed and not yet acked
        self._next = 0

    def enqueue(self, host, run_id, plan):
        self._next += 1
        entry = Entry(str(self._next), host, run_id, plan)
        self._queued.setdefault(host, deque()).append(entry)
        return entry.entry_id

    def claim(self, host):
        if self._claimed.get(host) is not None:
            return self._claimed[host]
        queued = self._queued.get(host)
        if not queued:
            return None
        self._claimed[host] = queued.popleft()
        return self._claimed[host]

    def ack(self, host, entry_id):
        held = self._claimed.get(host)
        if held is None or held.entry_id != entry_id:
            raise Refusal(f"entry {entry_id!r} is not claimed by {host}", "claim before ack")
        self._claimed[host] = None

    def pending(self, host):
        held = self._claimed.get(host)
        return ([held] if held is not None else []) + list(self._queued.get(host, ()))

"""sweep/hub/queue.py -- the queue's contract, the in-memory queue the tests run on, and the Redis one the hub runs on.

The hub is the only Redis client. A stream per host holds the entries (a run to claim, or a publish job), a consumer
group hands each out once, XAUTOCLAIM gives a restarted agent its own unacked entry back first, heartbeats are keys
with a TTL, and a run's events go out on a pub/sub channel for `watch`. FakeQueue keeps the same contract in memory,
with a clock that tests advance, so every hub test runs in-process; tests/integration proves RedisQueue against a real
Redis under `make integration`. `redis` is imported only when RedisQueue is built, so the cli extra never needs it.
"""
import json
import time
from collections import deque
from typing import Iterator, NamedTuple, Protocol

from sweep.hub.refusals import Refusal


class Entry(NamedTuple):
    entry_id: str
    host: str
    run_id: str | None      # None for a publish job
    plan: dict              # the claim body, or the publish job
    kind: str = "run"       # run | publish


class Queue(Protocol):
    def enqueue(self, host, run_id, plan, kind="run") -> str: ...
    def claim(self, host, block_s=0) -> Entry | None: ...      # the host's own claimed-unacked entry first, else the next queued, else None after block_s
    def ack(self, host, entry_id) -> None: ...                 # only the entry the host holds; anything else is refused
    def pending(self, host) -> list[Entry]: ...                # queued, plus the claimed-unacked one
    def beat(self, host, payload, ttl_s) -> None: ...          # the agent's heartbeat, gone after ttl_s
    def pulse(self, host) -> dict | None: ...                  # the last heartbeat, or None once it expired: silent, not zero
    def publish(self, channel, payload) -> None: ...           # an event on a run's channel
    def listen(self, channel, timeout_s) -> Iterator[dict]: ...   # subscribed at once; ends at a payload with final: true, or at the timeout


class FakeQueue:
    def __init__(self, clock=time.monotonic):
        self._queued = {}      # host -> deque of Entry
        self._claimed = {}     # host -> the one Entry claimed and not yet acked
        self._next = 0
        self._clock = clock
        self._beats = {}       # host -> (payload, expires_at)
        self._channels = {}    # channel -> [payload, ...]

    def enqueue(self, host, run_id, plan, kind="run"):
        self._next += 1
        entry = Entry(str(self._next), host, run_id, plan, kind)
        self._queued.setdefault(host, deque()).append(entry)
        return entry.entry_id

    def claim(self, host, block_s=0):
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

    def beat(self, host, payload, ttl_s):
        self._beats[host] = (dict(payload), self._clock() + ttl_s)

    def pulse(self, host):
        beat = self._beats.get(host)
        if beat is None or self._clock() > beat[1]:
            return None
        return dict(beat[0])

    def publish(self, channel, payload):
        self._channels.setdefault(channel, []).append(dict(payload))

    def listen(self, channel, timeout_s):
        published = self._channels.setdefault(channel, [])
        return _replay(published, len(published))


def _replay(published, start):
    i = start
    while i < len(published):
        payload = published[i]
        i += 1
        yield payload
        if payload.get("final"):
            return


class RedisQueue:
    GROUP = "agents"

    def __init__(self, url):
        import redis   # the hub extra; the cli never needs it
        self._redis = redis
        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._groups = set()

    # ---- keys
    @staticmethod
    def _stream(host):
        return f"harness:queue:{host}"

    def _group(self, stream):
        if stream not in self._groups:
            try:
                self._r.xgroup_create(stream, self.GROUP, id="0", mkstream=True)
            except self._redis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
            self._groups.add(stream)
        return self.GROUP

    @staticmethod
    def _entry(host, entry_id, fields):
        body = json.loads(fields["entry"])
        return Entry(entry_id, host, body["run_id"], body["plan"], body.get("kind", "run"))

    # ---- the contract
    def enqueue(self, host, run_id, plan, kind="run"):
        stream = self._stream(host)
        self._group(stream)
        return self._r.xadd(stream, {"entry": json.dumps({"run_id": run_id, "kind": kind, "plan": plan})}, maxlen=1000, approximate=True)

    def claim(self, host, block_s=0):
        stream = self._stream(host)
        group = self._group(stream)
        own = self._r.xautoclaim(stream, group, host, min_idle_time=0, start_id="0-0", count=1)
        claimed = own[1] if len(own) > 1 else []
        if claimed:
            entry_id, fields = claimed[0]
            return self._entry(host, entry_id, fields)
        block = int(block_s * 1000) if block_s else None   # block=0 would wait forever
        read = self._r.xreadgroup(group, host, {stream: ">"}, count=1, block=block)
        for _, entries in read or []:
            for entry_id, fields in entries:
                return self._entry(host, entry_id, fields)
        return None

    def ack(self, host, entry_id):
        stream = self._stream(host)
        group = self._group(stream)
        held = self._r.xpending_range(stream, group, min=entry_id, max=entry_id, count=1, consumername=host)
        if not held:
            raise Refusal(f"entry {entry_id!r} is not claimed by {host}", "claim before ack")
        self._r.xack(stream, group, entry_id)
        self._r.xdel(stream, entry_id)

    def pending(self, host):
        stream = self._stream(host)
        return [self._entry(host, entry_id, fields) for entry_id, fields in self._r.xrange(stream)]

    def beat(self, host, payload, ttl_s):
        self._r.set(f"harness:heartbeat:{host}", json.dumps(payload), ex=int(ttl_s))

    def pulse(self, host):
        text = self._r.get(f"harness:heartbeat:{host}")
        return None if text is None else json.loads(text)

    def publish(self, channel, payload):
        self._r.publish(f"harness:events:{channel}", json.dumps(payload))

    def listen(self, channel, timeout_s):
        pubsub = self._r.pubsub()
        pubsub.subscribe(f"harness:events:{channel}")
        pubsub.get_message(timeout=5)             # the subscription's confirmation, so nothing published after this call is missed
        return _stream_until_final(pubsub, time.monotonic() + timeout_s)

    def flush_for_tests(self):
        """Everything in the database the URL names; the integration suite owns that database."""
        self._r.flushdb()
        self._groups.clear()


def _stream_until_final(pubsub, deadline):
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining)
            if message is None:
                continue
            payload = json.loads(message["data"])
            yield payload
            if payload.get("final"):
                return
    finally:
        pubsub.close()

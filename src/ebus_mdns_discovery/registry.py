"""Memory-bounded lifecycle for discovered services.

Every discovered ``(service_type, interface, instance_name)`` becomes one
retained v1 ``Record`` on the MQTT bus. This registry keeps BOTH the in-memory
entry set AND the retained-topic tree bounded on a long-lived, memory-constrained
panel:

* ``mark_active`` (re)publishes an ``active`` retained record for a service;
* ``mark_removed`` tombstones it (a retained ``state=removed`` record). Removal
  is driven by avahi's authoritative ``ItemRemove`` (a real DNS-SD goodbye or an
  mDNS TTL expiry), so this registry does not itself infer staleness;
* ``sweep`` clears each tombstone's retained topic (an empty retained payload,
  the only thing that deletes the message from the broker) after a bounded
  ``tombstone_linger`` and evicts the entry;
* a hard LRU ``max_records`` cap bounds the keyspace, evicting already-tombstoned
  entries or dropping the newest arrival rather than ever orphaning a topic;
* ``flush`` clears everything, for an avahi reconnect (the fresh ItemNew stream
  rebuilds the view).

Both removal signals are emitted on purpose: the ``removed`` tombstone tells
live/reconnecting consumers what went away, and the later empty-retained publish
stops the broker hoarding it so the topic tree cannot grow without bound.
"""

from __future__ import annotations

import dataclasses
import heapq
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from ebus_service_discovery.record import (
    DEFAULT_TOPIC_BASE,
    Address,
    Record,
    RecordState,
)

from ebus_mdns_discovery.browser import Observation

logger = logging.getLogger("mdns-discovery")

DEFAULT_TOMBSTONE_LINGER_SECONDS = 900
DEFAULT_MAX_RECORDS = 512

# publish(topic, payload_bytes, retain)
PublishFn = Callable[[str, bytes, bool], None]
_ViewKey = tuple[str, str, str]


class Phase(Enum):
    ACTIVE = "active"
    TOMBSTONED = "tombstoned"


@dataclass
class Entry:
    record: Record
    first_seen: datetime
    last_seen: datetime
    phase: Phase
    clear_due: datetime | None = None
    clear_token: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Registry:
    def __init__(
        self,
        publish: PublishFn,
        *,
        base: str = DEFAULT_TOPIC_BASE,
        ttl_seconds: int | None = None,
        tombstone_linger_seconds: int = DEFAULT_TOMBSTONE_LINGER_SECONDS,
        max_records: int = DEFAULT_MAX_RECORDS,
        now_fn: Callable[[], datetime] = _utcnow,
    ):
        self._publish = publish
        self._base = base
        # None = advertise no ttl. Freshness is a bus-level property ($state), not
        # per-record: an event-driven publisher confirms a stable service once, so a
        # stamped ttl would flag every stable record stale. Kept configurable.
        self._ttl = ttl_seconds
        self._linger = tombstone_linger_seconds
        self._max = max(1, max_records)  # a non-positive cap would thrash-evict
        self._now = now_fn
        self._entries: OrderedDict[_ViewKey, Entry] = OrderedDict()
        # min-heap of (clear_due_epoch, clear_token, key); clear_token identifies
        # the authoritative schedule so re-activated entries' stale heap items are
        # lazily discarded on pop.
        self._clear_heap: list[tuple[float, int, _ViewKey]] = []
        self._token = 0

    # -- events (driven by the avahi browser) --------------------------------

    def mark_active(self, obs: Observation, now: datetime | None = None) -> None:
        """A service is present (avahi ItemNew/resolve): (re)publish it active."""
        now = now or self._now()
        entry = self._entries.get(obs.key)
        if entry is None:
            if not self._ensure_capacity():
                # Genuine overflow: more live services than the cap allows. Drop
                # this one rather than evict a present service. Raise MDNSD_MAX_RECORDS.
                logger.warning("reason=discoveryCapReached,max=%s,dropped=%s", self._max, obs.key)
                return
            record = self._build_record(obs, now)
            entry = Entry(record=record, first_seen=now, last_seen=now, phase=Phase.ACTIVE)
            self._entries[obs.key] = entry
        else:
            record = self._build_record(obs, now)
            record.first_seen = entry.first_seen
            entry.record = record
            entry.last_seen = now
            entry.phase = Phase.ACTIVE
            entry.clear_due = None  # re-activated: supersede any pending clear
        self._entries.move_to_end(obs.key)
        self._publish_record(entry.record)

    def mark_removed(self, key: _ViewKey, now: datetime | None = None) -> None:
        """A service went away (avahi ItemRemove): tombstone it. Idempotent."""
        now = now or self._now()
        entry = self._entries.get(key)
        if entry is None or entry.phase is Phase.TOMBSTONED:
            return
        entry.phase = Phase.TOMBSTONED
        entry.record.state = RecordState.REMOVED
        entry.record.removed_at = now
        self._publish_record(entry.record)
        self._schedule_clear(key, entry, now + timedelta(seconds=self._linger))

    def flush(self) -> None:
        """Clear every retained topic and drop all entries. Used on an avahi
        reconnect: the fresh ItemNew stream rebuilds the view, so nothing stale
        is carried across the gap."""
        for key in list(self._entries):
            self._clear_and_evict(key)
        self._clear_heap.clear()

    def reconcile(self, seed_records: Iterable[Record], now: datetime | None = None) -> None:
        """Adopt the broker's pre-existing retained records (a tested capability;
        not wired into the service, whose flush-on-reconnect handles restarts).
        Does not republish; adopted tombstones get their pending clear rescheduled."""
        now = now or self._now()
        for seed in seed_records:
            key = (seed.service_type, seed.interface, seed.instance_name)
            if key in self._entries:
                continue
            record = dataclasses.replace(seed)  # own a private copy (we mutate on tombstone)
            first_seen = record.first_seen or now
            last_seen = record.last_seen or now
            if record.state is RecordState.REMOVED:
                entry = Entry(
                    record=record,
                    first_seen=first_seen,
                    last_seen=last_seen,
                    phase=Phase.TOMBSTONED,
                )
                self._entries[key] = entry
                base_time = record.removed_at or now
                self._schedule_clear(key, entry, base_time + timedelta(seconds=self._linger))
            else:
                self._entries[key] = Entry(
                    record=record,
                    first_seen=first_seen,
                    last_seen=last_seen,
                    phase=Phase.ACTIVE,
                )

    # -- periodic sweep ------------------------------------------------------

    def sweep(self, now: datetime | None = None) -> None:
        """Clear+evict any tombstones whose linger has elapsed. (Removal itself
        is avahi's job now, so there is no TTL/absence backstop here.)"""
        now = now or self._now()
        now_epoch = now.timestamp()
        while self._clear_heap and self._clear_heap[0][0] <= now_epoch:
            _due, token, key = heapq.heappop(self._clear_heap)
            entry = self._entries.get(key)
            if entry is not None and entry.phase is Phase.TOMBSTONED and entry.clear_token == token:
                self._clear_and_evict(key)

    def seconds_until_next_clear(self, now: datetime | None = None) -> float | None:
        if not self._clear_heap:
            return None
        now = now or self._now()
        return max(0.0, self._clear_heap[0][0] - now.timestamp())

    def snapshot(self) -> list[Entry]:
        return list(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    # -- internals -----------------------------------------------------------

    def _next_token(self) -> int:
        self._token += 1
        return self._token

    def _build_record(self, obs: Observation, now: datetime) -> Record:
        return Record(
            service_type=obs.service_type,
            instance_name=obs.instance_name,
            hostname=obs.hostname,
            interface=obs.interface,
            port=obs.port,
            addresses=[Address.parse(a) for a in obs.addresses],
            txt=dict(obs.txt),
            state=RecordState.ACTIVE,
            first_seen=now,
            last_seen=now,
            ttl_seconds=self._ttl,
        )

    def _schedule_clear(self, key: _ViewKey, entry: Entry, clear_due: datetime) -> None:
        entry.clear_due = clear_due
        entry.clear_token = self._next_token()
        heapq.heappush(self._clear_heap, (clear_due.timestamp(), entry.clear_token, key))

    def _clear_and_evict(self, key: _ViewKey) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        # Empty retained payload deletes the message from the broker's store.
        self._publish(entry.record.topic(self._base), b"", True)

    def _ensure_capacity(self) -> bool:
        """Make room for one new key by evicting an already-tombstoned entry.
        Returns False if the live set genuinely fills the cap (caller drops the
        new arrival), so a present service is never evicted to make room."""
        while len(self._entries) >= self._max:
            victim = self._pick_eviction_victim()
            if victim is None:
                return False
            self._clear_and_evict(victim)
        return True

    def _pick_eviction_victim(self) -> _ViewKey | None:
        for key, entry in self._entries.items():
            if entry.phase is Phase.TOMBSTONED:
                return key
        return None

    def _publish_record(self, record: Record) -> None:
        self._publish(record.topic(self._base), record.to_json().encode("utf-8"), True)

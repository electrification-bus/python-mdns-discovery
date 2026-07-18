import json
from datetime import datetime, timedelta, timezone

from ebus_service_discovery.record import Address, Record, RecordState

from ebus_mdns_discovery.browser import Observation
from ebus_mdns_discovery.registry import Registry

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
BASE = "local/mdns/discovery/v1"


class Clock:
    def __init__(self, start=T0):
        self.t = start

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)


class Pub:
    def __init__(self):
        self.calls = []  # (topic, payload, retain)

    def __call__(self, topic, payload, retain):
        self.calls.append((topic, payload, retain))

    def clear(self):
        self.calls = []

    def states(self):
        return [json.loads(p)["state"] for _t, p, _r in self.calls if p]

    def clears_for(self, topic):
        return [c for c in self.calls if c[0] == topic and c[1] == b""]


def _obs(
    service_type="_x._tcp",
    interface="eth0",
    instance="Dev 1",
    hostname="h.local",
    port=80,
    addresses=("192.168.1.10",),
    txt=None,
):
    return Observation(
        service_type=service_type,
        interface=interface,
        instance_name=instance,
        hostname=hostname,
        port=port,
        addresses=list(addresses),
        txt=dict(txt or {}),
    )


def _registry(pub, clock, *, ttl=300, linger=900, max_records=512):
    return Registry(
        pub,
        base=BASE,
        ttl_seconds=ttl,
        tombstone_linger_seconds=linger,
        max_records=max_records,
        now_fn=clock.now,
    )


def _topic(instance="Dev%201", service="_x._tcp", iface="eth0"):
    return f"{BASE}/{service}/{iface}/{instance}"


# -- publish / refresh -------------------------------------------------------


def test_mark_active_publishes_one_active_retained_record():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock)
    r.mark_active(_obs())
    assert len(pub.calls) == 1
    topic, payload, retain = pub.calls[0]
    assert topic == _topic()
    assert retain is True
    rec = json.loads(payload)
    assert (
        rec["state"] == "active" and rec["schema_version"] == 1 and rec["instance_name"] == "Dev 1"
    )


def test_default_ttl_none_omits_ttl_from_wire():
    # Freshness is bus-level ($state) now, not per-record: with ttl None the
    # record carries no ttl_seconds, so consumers never compute is_stale on it.
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock, ttl=None)
    r.mark_active(_obs())
    rec = json.loads(pub.calls[0][1])
    assert "ttl_seconds" not in rec


def test_re_mark_active_republishes_and_keeps_first_seen():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock)
    r.mark_active(_obs())
    clock.advance(300)
    pub.clear()
    r.mark_active(_obs(addresses=("192.168.1.10", "192.168.1.99")))
    assert len(pub.calls) == 1
    rec = json.loads(pub.calls[0][1])
    assert rec["state"] == "active"
    assert rec["first_seen"] != rec["last_seen"]  # first_seen kept, last_seen advanced
    assert [a["address"] for a in rec["addresses"]] == ["192.168.1.10", "192.168.1.99"]


# -- removal (avahi ItemRemove) ---------------------------------------------


def test_mark_removed_tombstones():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock)
    r.mark_active(_obs())
    pub.clear()
    r.mark_removed(_obs().key)
    assert pub.states() == ["removed"]
    rec = json.loads(pub.calls[0][1])
    assert rec["state"] == "removed" and "removed_at" in rec


def test_mark_removed_unknown_or_double_is_noop():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock)
    r.mark_removed(("_x._tcp", "eth0", "ghost"))  # never seen
    r.mark_active(_obs())
    pub.clear()
    r.mark_removed(_obs().key)
    r.mark_removed(_obs().key)  # already tombstoned
    assert pub.states() == ["removed"]  # only one removed publish


# -- tombstone -> clear -> evict --------------------------------------------


def test_linger_elapses_then_clears_and_evicts():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock, linger=900)
    r.mark_active(_obs())
    r.mark_removed(_obs().key)
    pub.clear()
    clock.advance(900 + 1)
    r.sweep()
    assert len(pub.calls) == 1
    assert pub.calls[0] == (_topic(), b"", True)  # empty retained clear
    assert len(r) == 0


def test_clear_not_emitted_before_linger():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock, linger=900)
    r.mark_active(_obs())
    r.mark_removed(_obs().key)
    pub.clear()
    clock.advance(899)
    r.sweep()
    assert pub.calls == []
    assert len(r) == 1


def test_reactivation_supersedes_pending_clear():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock, linger=10)
    r.mark_active(_obs())
    r.mark_removed(_obs().key)  # tombstoned, clear_due = T0 + 10
    clock.advance(5)
    pub.clear()
    r.mark_active(_obs())  # re-activated before the clear fires
    assert pub.states() == ["active"]
    clock.advance(10)  # past the ORIGINAL clear_due
    pub.clear()
    r.sweep()
    assert pub.clears_for(_topic()) == []  # stale heap item discarded
    assert len(r) == 1


# -- capacity cap ------------------------------------------------------------


def test_cap_evicts_a_tombstoned_entry_to_make_room():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock, max_records=2)
    r.mark_active(_obs(instance="A"))
    r.mark_removed(("_x._tcp", "eth0", "A"))  # A tombstoned
    r.mark_active(_obs(instance="B"))
    pub.clear()
    r.mark_active(_obs(instance="C"))  # at cap -> evict the tombstoned A
    assert len(pub.clears_for(_topic(instance="A"))) == 1  # A cleared, not orphaned
    assert {e.record.instance_name for e in r.snapshot()} == {"B", "C"}


def test_cap_full_of_live_services_drops_the_newest_without_a_removed_storm():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock, max_records=2)
    r.mark_active(_obs(instance="A"))
    r.mark_active(_obs(instance="B"))
    pub.clear()
    r.mark_active(_obs(instance="C"))  # both present -> C dropped, no eviction
    assert pub.calls == []  # nothing published/cleared for the dropped arrival
    assert {e.record.instance_name for e in r.snapshot()} == {"A", "B"}


def test_nonpositive_max_records_is_clamped_to_one():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock, max_records=0)  # clamped to 1, not thrash-to-empty
    r.mark_active(_obs(instance="A"))
    assert len(r) == 1


# -- flush (avahi reconnect) -------------------------------------------------


def test_flush_clears_every_retained_topic_and_empties_the_registry():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock)
    r.mark_active(_obs(instance="A"))
    r.mark_active(_obs(instance="B"))
    pub.clear()
    r.flush()
    assert len(r) == 0
    cleared = {t for (t, p, _r) in pub.calls if p == b""}
    assert cleared == {_topic(instance="A"), _topic(instance="B")}


# -- reconcile (tested capability; not wired into the service) ---------------


def _record(instance, state=RecordState.ACTIVE, removed_at=None):
    return Record(
        service_type="_x._tcp",
        instance_name=instance,
        hostname="h.local",
        interface="eth0",
        port=80,
        addresses=[Address.parse("192.168.1.10")],
        txt={},
        state=state,
        first_seen=T0,
        last_seen=T0,
        ttl_seconds=300,
        removed_at=removed_at,
    )


def test_reconcile_seeds_without_republishing_and_resumes_tombstone_clear():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock, linger=900)
    r.reconcile([_record("A"), _record("B", state=RecordState.REMOVED, removed_at=T0)])
    assert len(r) == 2
    assert pub.calls == []  # adoption does not republish
    clock.advance(900 + 1)
    r.sweep()
    # the adopted tombstone (B) gets its pending clear; A (never removed) stays
    assert pub.clears_for(_topic(instance="B")) == [(_topic(instance="B"), b"", True)]
    assert {e.record.instance_name for e in r.snapshot()} == {"A"}


def test_reconcile_does_not_mutate_caller_records():
    pub, clock = Pub(), Clock()
    r = _registry(pub, clock)
    seed = _record("A")
    r.reconcile([seed])
    r.mark_removed(("_x._tcp", "eth0", "A"))  # tombstones the adopted entry
    assert seed.state is RecordState.ACTIVE  # registry owns a private copy
    assert seed.removed_at is None

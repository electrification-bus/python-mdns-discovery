"""Object-lifecycle tests for AvahiBrowser.

The live D-Bus wiring is validated on a panel, but the pieces that stop avahi's
per-client object pool from leaking are pure bookkeeping and are covered here
with fake avahi objects (no live dbus): freeing an object also drops its signal
receivers, the per-type ServiceBrowser is refcounted and freed only when the
type is gone everywhere, and the concurrent resolver count is LRU-capped.
"""

from ebus_mdns_discovery.browser import (
    AVAHI_LOOKUP_RESULT_LOCAL,
    AvahiBrowser,
    _Tracked,
)


class FakeObj:
    def __init__(self):
        self.freed = False

    def Free(self):
        self.freed = True


class FakeMatch:
    def __init__(self):
        self.removed = False

    def remove(self):
        self.removed = True


def _tracked():
    t = _Tracked(FakeObj())
    t.matches = [FakeMatch(), FakeMatch()]
    return t


def _browser(**kw):
    return AvahiBrowser(on_active=lambda o: None, on_removed=lambda k: None, **kw)


def test_free_tracked_releases_object_and_matches():
    b = _browser()
    t = _tracked()
    matches = list(t.matches)
    b._free_tracked(t)
    assert t.obj.freed
    assert all(m.removed for m in matches)
    assert t.matches == []


def test_free_tracked_none_is_noop():
    b = _browser()
    b._free_tracked(None)  # must not raise


def test_evict_if_full_frees_lru_front():
    b = _browser(max_resolvers=3)
    trs = {}
    for k in ["r1", "r2", "r3"]:
        trs[k] = _tracked()
        b._resolvers[k] = trs[k]
    b._resolvers.move_to_end("r1")  # LRU order now r2, r3, r1
    b._evict_if_full()  # len 3 >= cap 3 -> evict the front (r2)
    assert "r2" not in b._resolvers
    assert trs["r2"].obj.freed
    assert list(b._resolvers.keys()) == ["r3", "r1"]
    assert not trs["r3"].obj.freed and not trs["r1"].obj.freed


def test_stop_frees_browsers_resolvers_and_type_browser():
    b = _browser()
    b._type_browser = _tracked()
    br = _tracked()
    b._service_browsers["_x._tcp"] = br
    res = _tracked()
    b._resolvers[(1, 0, "svc", "_x._tcp", "local")] = res
    tb_obj, br_obj, res_obj = b._type_browser.obj, br.obj, res.obj
    b.stop()
    assert tb_obj.freed and br_obj.freed and res_obj.freed
    assert b._service_browsers == {} and len(b._resolvers) == 0
    assert b._type_browser is None


def test_on_service_new_existing_moves_to_lru_tail():
    b = _browser()
    r1 = (1, 0, "svc1", "_x._tcp", "local")
    r2 = (1, 0, "svc2", "_x._tcp", "local")
    b._resolvers[r1] = _tracked()
    b._resolvers[r2] = _tracked()
    b._on_service_new(1, 0, "svc1", "_x._tcp", "local", 0)  # r1 already exists
    assert list(b._resolvers.keys())[-1] == r1  # bumped to the LRU tail, not recreated


def test_on_service_new_skips_out_of_scope_interface():
    # A filter that admits only eth0 must not create a resolver for a service on
    # another interface (so no record is ever produced for it).
    b = AvahiBrowser(
        on_active=lambda o: None,
        on_removed=lambda k: None,
        interface_in_scope=lambda name: name == "eth0",
    )
    b._iface_names[2] = "wlan0_ap"  # pre-seed so _iface_name skips the _server lookup
    b._on_service_new(2, 0, "svc", "_x._tcp", "local", 0)
    assert b._resolvers == {}  # out of scope -> no resolver created


def test_on_resolved_moves_to_tail_and_skips_local():
    seen = []
    b = AvahiBrowser(on_active=lambda o: seen.append(o), on_removed=lambda k: None)
    b._iface_names[1] = "eth0"  # pre-seed so _iface_name skips the _server lookup
    r1 = (1, 0, "svc1", "_x._tcp", "local")
    r2 = (1, 0, "svc2", "_x._tcp", "local")
    b._resolvers[r1] = _tracked()
    b._resolvers[r2] = _tracked()
    # non-local resolve: publishes and bumps r1 to the tail
    b._on_resolved(1, 0, "svc1", "_x._tcp", "local", "host", 0, "1.2.3.4", 80, [], 0)
    assert list(b._resolvers.keys())[-1] == r1
    assert len(seen) == 1 and seen[0].instance_name == "svc1"
    # local resolve: still bumps the LRU, but does NOT publish (ignore-local parity)
    b._on_resolved(
        1,
        0,
        "svc2",
        "_x._tcp",
        "local",
        "host",
        0,
        "1.2.3.4",
        80,
        [],
        AVAHI_LOOKUP_RESULT_LOCAL,
    )
    assert list(b._resolvers.keys())[-1] == r2
    assert len(seen) == 1  # unchanged

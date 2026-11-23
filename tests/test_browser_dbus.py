"""AvahiBrowser against a fake D-Bus binding."""

import logging
from types import SimpleNamespace

import pytest

from ebus_mdns_discovery import browser as browser_mod
from ebus_mdns_discovery.browser import AvahiBrowser


class FakeDBusException(Exception):
    pass


class FakeAvahi:
    """Stands in for the dbus module, the system bus and every avahi object."""

    DBusException = FakeDBusException

    def __init__(self):
        self.proxies = []  # (path, introspect)
        self.count = 0
        self.receivers = {}  # (interface, signal) -> handler
        self.resolver_error = None

    # the dbus module
    def SystemBus(self):
        return self

    def UInt32(self, value):
        return value

    def Interface(self, proxy, interface):
        return proxy

    # the bus
    def get_object(self, name, path, introspect=True):
        self.proxies.append((path, introspect))
        return SimpleNamespace(
            path=path,
            Free=lambda: None,
            ServiceTypeBrowserNew=self._new("ServiceTypeBrowser"),
            ServiceBrowserNew=self._new("ServiceBrowser"),
            ServiceResolverNew=self._new("ServiceResolver"),
        )

    def add_signal_receiver(self, handler, signal_name, dbus_interface, bus_name, path_keyword):
        assert (bus_name, path_keyword) == (browser_mod.AVAHI_DBUS_NAME, "path")
        key = (dbus_interface.rsplit(".", 1)[-1], signal_name)
        assert key not in self.receivers
        self.receivers[key] = handler
        return SimpleNamespace(remove=lambda: self.receivers.pop(key))

    def emit(self, interface, signal, path, *args):
        handler = self.receivers.get((interface, signal))
        if handler:
            handler(*args, path=path)

    def _new(self, kind):
        def create(*args):
            if kind == "ServiceResolver" and self.resolver_error:
                raise self.resolver_error
            self.count += 1
            return f"/Client1/{kind}{self.count}"

        return create


@pytest.fixture
def avahi(monkeypatch):
    fake = FakeAvahi()
    monkeypatch.setattr(browser_mod, "dbus", fake)
    return fake


def _started(on_active=lambda o: None, **kw):
    b = AvahiBrowser(on_active=on_active, on_removed=lambda k: None, **kw)
    b._iface_names[1] = "eth0"
    b.start()
    return b


def test_no_avahi_proxy_introspects(avahi):
    b = _started()
    b._on_type_new(1, 0, "_x._tcp", "local", 0)
    b._on_service_new(1, 0, "svc", "_x._tcp", "local", 0)
    kinds = [path.rstrip("0123456789") for path, _ in avahi.proxies]
    assert kinds == [
        "/",
        "/Client1/ServiceTypeBrowser",
        "/Client1/ServiceBrowser",
        "/Client1/ServiceResolver",
    ]
    assert not any(introspect for _, introspect in avahi.proxies)


def test_repeated_resolver_failures_are_sampled(avahi, caplog):
    avahi.resolver_error = FakeDBusException("Invalid service type")
    b = _started()
    for i in range(150):
        b._on_service_new(1, 0, f"svc{i}", "_bad._tcp", "local", 0)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "reason=resolverNewFailed,total=101,type=_bad._tcp,name=svc100" in warnings[1]


def _browse(avahi, *names):
    """Announce one service type and the named services through the signals; returns
    the resolver path of each."""
    avahi.emit(
        "ServiceTypeBrowser", "ItemNew", "/Client1/ServiceTypeBrowser1", 1, 0, "_x._tcp", "local", 0
    )
    paths = []
    for name in names:
        avahi.emit(
            "ServiceBrowser",
            "ItemNew",
            "/Client1/ServiceBrowser2",
            1,
            0,
            name,
            "_x._tcp",
            "local",
            0,
        )
        paths.append(f"/Client1/ServiceResolver{avahi.count}")
    return paths


def _found(avahi, path, name):
    avahi.emit(
        "ServiceResolver",
        "Found",
        path,
        1,
        0,
        name,
        "_x._tcp",
        "local",
        "h",
        0,
        "1.2.3.4",
        80,
        [],
        0,
    )


def test_receivers_do_not_grow_with_services(avahi):
    _started()
    before = len(avahi.receivers)
    _browse(avahi, *[f"svc{i}" for i in range(300)])
    assert before == len(avahi.receivers) == 5


def test_signals_reach_their_handlers(avahi):
    seen = []
    b = _started(on_active=seen.append)
    one, two = _browse(avahi, "one", "two")
    _found(avahi, two, "two")
    assert [o.instance_name for o in seen] == ["two"]
    avahi.emit("ServiceResolver", "Failure", one, "timeout")
    assert [k[2] for k in b._resolvers] == ["two"]
    avahi.emit(
        "ServiceBrowser",
        "ItemRemove",
        "/Client1/ServiceBrowser2",
        1,
        0,
        "two",
        "_x._tcp",
        "local",
        0,
    )
    assert not b._resolvers


def test_signal_from_an_evicted_resolver_is_ignored(avahi):
    seen = []
    _started(on_active=seen.append, max_resolvers=1)
    one, two = _browse(avahi, "one", "two")  # "two" evicts "one"
    _found(avahi, one, "one")
    assert seen == []
    _found(avahi, two, "two")
    assert [o.instance_name for o in seen] == ["two"]


def test_stop_removes_receivers_and_restart_adds_them_once(avahi):
    b = _started()
    _browse(avahi, "one")
    b.stop()
    assert avahi.receivers == {} and b._paths == {}
    b.start()
    assert len(avahi.receivers) == 5


def test_start_after_a_failed_start_does_not_add_receivers_twice(avahi):
    b = _started()
    b.start()  # no stop() in between
    assert len(avahi.receivers) == 5


def test_one_label_type_is_not_resolved(avahi):
    b = _started()
    b._on_service_new(1, 0, "_http", "_tcp", "local", 0)
    assert not b._resolvers
    assert len(avahi.proxies) == 2  # the server and the type browser

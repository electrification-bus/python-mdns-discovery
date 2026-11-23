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
            connect_to_signal=lambda *a: SimpleNamespace(remove=lambda: None),
            ServiceTypeBrowserNew=self._new("ServiceTypeBrowser"),
            ServiceBrowserNew=self._new("ServiceBrowser"),
            ServiceResolverNew=self._new("ServiceResolver"),
        )

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


def _started():
    b = AvahiBrowser(on_active=lambda o: None, on_removed=lambda k: None)
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


def test_one_label_type_is_not_resolved(avahi):
    b = _started()
    b._on_service_new(1, 0, "_http", "_tcp", "local", 0)
    assert not b._resolvers
    assert len(avahi.proxies) == 2  # the server and the type browser

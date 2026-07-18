"""Tests for the service glue. Importable off-panel because dbus/gi are guarded;
the D-Bus/GLib browse loop itself is validated on a panel."""

from ebus_mdns_discovery import service
from ebus_mdns_discovery.service import _make_publisher

# -- publish guard -----------------------------------------------------------


def test_publisher_swallows_publish_errors():
    class BadMqtt:
        def publish(self, *a, **k):
            raise ValueError("wildcard in topic")

    _make_publisher(BadMqtt())("t/+/x", b"data", True)  # must not raise


def test_publisher_none_logs_without_raising():
    _make_publisher(None)("t", b"data", True)  # --no-mqtt-please dry-run path


# -- startup guards ----------------------------------------------------------


def test_main_returns_1_when_dbus_unavailable(monkeypatch):
    monkeypatch.setattr(service, "dbus", None)
    assert service.main([]) == 1  # panel-only service; no dbus off-panel


def test_main_returns_1_on_missing_broker(monkeypatch):
    monkeypatch.setattr(service, "dbus", object())  # get past the dbus-available gate
    monkeypatch.setattr(service, "GLib", object())
    monkeypatch.delenv("MDNSD_MQTT_HOST", raising=False)
    monkeypatch.delenv("MDNSD_MQTT_PORT", raising=False)
    assert service.main([]) == 1


def test_main_returns_1_on_malformed_port(monkeypatch):
    monkeypatch.setattr(service, "dbus", object())
    monkeypatch.setattr(service, "GLib", object())
    monkeypatch.setenv("MDNSD_MQTT_HOST", "127.0.0.1")
    monkeypatch.setenv("MDNSD_MQTT_PORT", "notaport")
    assert service.main([]) == 1  # clean exit, not an uncaught ValueError


# -- startup retained-tree clear ---------------------------------------------


def test_is_live_retained_only_true_for_nonempty_payloads():
    assert service._is_live_retained(b'{"state":"active"}') is True
    assert service._is_live_retained(b"") is False  # already-cleared topic
    assert service._is_live_retained(b"   ") is False  # whitespace only
    assert service._is_live_retained(None) is False


def test_clear_topics_empty_retains_each_enumerated_topic():
    calls = []
    n = service._clear_topics(
        {"base/x/eth0/A", "base/y/eth0/B"}, lambda t, p, r: calls.append((t, p, r))
    )
    assert n == 2
    # every clear is an empty retained publish (the only payload that deletes a
    # retained message); one per enumerated topic, no others
    assert sorted(calls) == [
        ("base/x/eth0/A", b"", True),
        ("base/y/eth0/B", b"", True),
    ]


def test_clear_topics_on_empty_set_publishes_nothing():
    calls = []
    assert service._clear_topics(set(), lambda *a: calls.append(a)) == 0
    assert calls == []

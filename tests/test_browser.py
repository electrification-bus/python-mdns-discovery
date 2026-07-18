from ebus_mdns_discovery.browser import InstanceTracker, parse_dbus_txt

# avahi protocol constants (as they arrive in signals)
INET, INET6 = 0, 1


def _found(
    tracker,
    *,
    address,
    protocol,
    instance="Envoy 42",
    service="_enphase-envoy._tcp",
    interface="eth0",
    hostname="envoy.local",
    port=80,
    txt=None,
):
    return tracker.found(
        service_type=service,
        interface=interface,
        instance_name=instance,
        hostname=hostname,
        port=port,
        address=address,
        txt=txt or {},
        protocol=protocol,
    )


# -- parse_dbus_txt ----------------------------------------------------------


def test_parse_dbus_txt_decodes_byte_arrays():
    txt = parse_dbus_txt([b"serialnum=482", b"protovers=7", b"flag"])
    assert txt == {"serialnum": "482", "protovers": "7", "flag": ""}


def test_parse_dbus_txt_drops_empty_keys_and_keeps_value_equals_signs():
    assert parse_dbus_txt([b"=novalue", b"url=http://x:8123/a=b"]) == {"url": "http://x:8123/a=b"}


# -- InstanceTracker: address aggregation across protocols -------------------


def test_tracker_aggregates_ipv4_and_ipv6_into_one_observation():
    t = InstanceTracker()
    # avahi's Found carries the full txt on every protocol's resolve
    _found(t, address="192.168.1.10", protocol=INET, txt={"serialnum": "482"})
    obs = _found(t, address="fe80::1", protocol=INET6, txt={"serialnum": "482"})
    assert obs.key == ("_enphase-envoy._tcp", "eth0", "Envoy 42")
    assert obs.addresses == ["192.168.1.10", "fe80::1"]  # both families, sorted
    assert obs.txt == {"serialnum": "482"}
    assert obs.hostname == "envoy.local" and obs.port == 80


def test_tracker_dedups_repeat_addresses():
    t = InstanceTracker()
    _found(t, address="192.168.1.10", protocol=INET)
    obs = _found(t, address="192.168.1.10", protocol=INET)
    assert obs.addresses == ["192.168.1.10"]


def test_tracker_replaces_address_on_in_place_change():
    # avahi re-emits Found (same resolver, same protocol) with a new IP on a DHCP
    # renew, with NO ItemRemove. The old address must NOT accumulate.
    t = InstanceTracker()
    _found(t, address="192.168.1.50", protocol=INET)
    obs = _found(t, address="192.168.1.99", protocol=INET)  # renew, same protocol
    assert obs.addresses == ["192.168.1.99"]  # stale .50 dropped
    # a concurrent IPv6 is unaffected (cross-protocol union preserved)
    obs = _found(t, address="fe80::1", protocol=INET6)
    assert obs.addresses == ["192.168.1.99", "fe80::1"]


def test_tracker_host_port_txt_are_authoritative_including_clears():
    t = InstanceTracker()
    _found(
        t,
        address="192.168.1.10",
        protocol=INET,
        hostname="a.local",
        port=80,
        txt={"serialnum": "482"},
    )
    # a re-resolve reports a changed host/port and a CLEARED txt
    obs = _found(t, address="192.168.1.10", protocol=INET, hostname="b.local", port=443, txt={})
    assert obs.hostname == "b.local" and obs.port == 443
    assert obs.txt == {}  # cleared, not stale-retained


def test_tracker_removed_one_protocol_reduces_then_removes():
    t = InstanceTracker()
    _found(t, address="192.168.1.10", protocol=INET)
    _found(t, address="fe80::1", protocol=INET6)
    # IPv4 protocol goes away: still present on IPv6
    reduced = t.removed(
        service_type="_enphase-envoy._tcp",
        interface="eth0",
        instance_name="Envoy 42",
        protocol=INET,
    )
    assert reduced is not None and reduced.addresses == ["fe80::1"]
    # IPv6 goes away too: fully gone
    gone = t.removed(
        service_type="_enphase-envoy._tcp",
        interface="eth0",
        instance_name="Envoy 42",
        protocol=INET6,
    )
    assert gone is None


def test_tracker_removed_unknown_is_none():
    t = InstanceTracker()
    assert (
        t.removed(
            service_type="_x._tcp",
            interface="eth0",
            instance_name="ghost",
            protocol=INET,
        )
        is None
    )


def test_tracker_same_instance_on_two_interfaces_is_two_keys():
    t = InstanceTracker()
    a = _found(t, address="192.168.1.10", protocol=INET, interface="eth0", instance="X")
    b = _found(t, address="10.0.0.5", protocol=INET, interface="wlan0", instance="X")
    assert a.key != b.key
    assert a.addresses == ["192.168.1.10"] and b.addresses == ["10.0.0.5"]


def test_tracker_clear_resets():
    t = InstanceTracker()
    _found(t, address="192.168.1.10", protocol=INET)
    t.clear()
    assert (
        t.removed(
            service_type="_enphase-envoy._tcp",
            interface="eth0",
            instance_name="Envoy 42",
            protocol=INET,
        )
        is None
    )

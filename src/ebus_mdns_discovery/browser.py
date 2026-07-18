"""Browse DNS-SD services via the avahi D-Bus API (org.freedesktop.Avahi).

A ServiceTypeBrowser discovers every service type on the LAN; a per-type
ServiceBrowser then reports each instance via ``ItemNew`` / ``ItemRemove``; a
per-instance ServiceResolver turns an instance into host/address/port/txt. The
signals drive the registry directly, so removals are avahi's authoritative DNS-SD
goodbye / mDNS TTL expiry rather than an inferred one.

``AvahiBrowser`` is the avahi ``DiscoveryBackend`` (see ``backend.py``).
``InstanceTracker`` (the address aggregation) and the txt parsing are pure and
unit-tested; the D-Bus/GLib wiring is validated on a panel.

avahi enforces an ``objects-per-client-max`` (default 1024) across ALL browsers
and resolvers a single D-Bus client holds. Because this is one long-lived client,
resolvers (created per instance x interface x protocol, the dominant consumer on
a busy LAN) are LRU-capped, and every freed object also drops its D-Bus signal
receivers. Per-type ServiceBrowsers are kept for the whole browse session (freed
only in ``stop()``): freeing one when its type goes away would make its instances'
``ItemRemove``s unobservable and orphan their records (the type PTR and the
per-type PTR expire on independent TTLs, so the type removal can race ahead of the
instances'), and browsers are bounded by the number of distinct service types
(small) regardless.
"""

from __future__ import annotations

import contextlib
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

logger = logging.getLogger("mdns-discovery")

# The D-Bus binding is a panel-only dependency; keep the module importable (and
# InstanceTracker testable) without it.
try:
    import dbus
except ImportError:
    dbus = None

AVAHI_DBUS_NAME = "org.freedesktop.Avahi"
AVAHI_IF_UNSPEC = -1
AVAHI_PROTO_UNSPEC = -1
# avahi sets this lookup-result flag when the service is on the local host
# (the parity we want with the old avahi-browse --ignore-local).
AVAHI_LOOKUP_RESULT_LOCAL = 8
_S_SERVER = "org.freedesktop.Avahi.Server"
_S_TYPE_BROWSER = "org.freedesktop.Avahi.ServiceTypeBrowser"
_S_BROWSER = "org.freedesktop.Avahi.ServiceBrowser"
_S_RESOLVER = "org.freedesktop.Avahi.ServiceResolver"

_ViewKey = tuple[str, str, str]

# Cap the concurrent avahi ServiceResolvers this one D-Bus client holds. On a busy
# LAN with many/rotating instances and the per-(interface, protocol) fan-out,
# resolvers otherwise accumulate past the live-instance count until avahi rejects
# new ones with TooManyObjectsError. We LRU-evict the least-recently-resolved
# resolver at the cap. Presence is still tracked by the (session-lived)
# ServiceBrowser, so an evicted resolver only stops IP-update tracking for a stale
# instance, never its removal. Kept well under avahi's default 1024 to leave room
# for the per-type ServiceBrowsers.
DEFAULT_MAX_RESOLVERS = 512
DEFAULT_RESOLVER_EVICT_LOG_EVERY = 100


class _Tracked:
    """An avahi D-Bus object (a browser or resolver) plus its signal receivers,
    so freeing it releases BOTH the avahi object (``Free()``) and the client-side
    D-Bus SignalMatch receivers (which otherwise leak on the process-singleton
    bus)."""

    __slots__ = ("obj", "matches")

    def __init__(self, obj: object) -> None:
        self.obj = obj
        self.matches: list = []


@dataclass
class Observation:
    """One resolved DNS-SD instance on one interface, aggregated across the
    per-protocol resolves avahi reports for it."""

    service_type: str
    interface: str
    instance_name: str
    hostname: str
    port: int
    addresses: list[str] = field(default_factory=list)
    txt: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> _ViewKey:
        return (self.service_type, self.interface, self.instance_name)


def parse_dbus_txt(txt: Iterable) -> dict[str, str]:
    """avahi TXT is an array of byte-arrays, each ``key=value``. Decode to a dict."""
    out: dict[str, str] = {}
    for entry in txt:
        s = bytes(entry).decode("utf-8", "replace")
        if "=" in s:
            key, value = s.split("=", 1)
            if key:
                out[key] = value
        elif s:
            out[s] = ""
    return out


class InstanceTracker:
    """Aggregates avahi's per-``(interface, protocol)`` resolve/remove events into
    one Observation per ``(service_type, interface, instance_name)``, so a service
    heard on both IPv4 and IPv6 becomes a single record with both addresses."""

    def __init__(self) -> None:
        # key -> {"hostname", "port", "txt", "addrs": {protocol: set(address)}}
        self._state: dict[_ViewKey, dict] = {}

    def found(
        self,
        *,
        service_type: str,
        interface: str,
        instance_name: str,
        hostname: str,
        port: int,
        address: str,
        txt: dict[str, str],
        protocol: int,
    ) -> Observation:
        key = (service_type, interface, instance_name)
        st = self._state.setdefault(key, {"hostname": "", "port": 0, "txt": {}, "addrs": {}})
        # avahi's Found carries the full CURRENT host/port/txt, and one current
        # address per (interface, protocol) resolver. Assign, do NOT merge, so an
        # in-place IP change (DHCP renew, no ItemRemove) or a TXT clear supersedes
        # the old value instead of accumulating a stale one. The cross-protocol
        # union in _observation() still yields all address families.
        st["hostname"] = hostname
        st["port"] = port
        st["txt"] = txt
        if address:
            st["addrs"][protocol] = {address}
        return self._observation(key, st)

    def removed(
        self, *, service_type: str, interface: str, instance_name: str, protocol: int
    ) -> Observation | None:
        """Drop one protocol's addresses. Returns the reduced Observation if the
        instance is still present on another protocol, or None if fully gone."""
        key = (service_type, interface, instance_name)
        st = self._state.get(key)
        if st is None:
            return None
        st["addrs"].pop(protocol, None)
        if not st["addrs"]:
            del self._state[key]
            return None
        return self._observation(key, st)

    def clear(self) -> None:
        self._state.clear()

    @staticmethod
    def _observation(key: _ViewKey, st: dict) -> Observation:
        addresses = sorted({a for addrs in st["addrs"].values() for a in addrs})
        service_type, interface, instance_name = key
        return Observation(
            service_type=service_type,
            interface=interface,
            instance_name=instance_name,
            hostname=st["hostname"],
            port=st["port"],
            addresses=addresses,
            txt=dict(st["txt"]),
        )


class AvahiBrowser:
    """Live avahi D-Bus browse (the avahi ``DiscoveryBackend``). Wires
    ItemNew/ItemRemove/Found signals to the two callbacks: ``on_active(Observation)``
    and ``on_removed(key)``.

    ``interface_in_scope`` filters which interfaces are published: services whose
    interface is out of scope are never resolved, so no record is created for them.
    ``None`` (the default) means every interface is in scope (no filtering, and no
    per-instance interface-name lookup).

    Not unit-tested (needs a live avahi); validated on a panel. Requires the dbus
    binding and a running GLib main loop (set up by the service)."""

    def __init__(
        self,
        on_active: Callable[[Observation], None],
        on_removed: Callable[[_ViewKey], None],
        max_resolvers: int = DEFAULT_MAX_RESOLVERS,
        resolver_evict_log_every: int = DEFAULT_RESOLVER_EVICT_LOG_EVERY,
        interface_in_scope: Callable[[str], bool] | None = None,
    ) -> None:
        self._on_active = on_active
        self._on_removed = on_removed
        # Always create a resolver for the current ItemNew, so the effective floor
        # is 1 even if a caller passes 0 / a negative cap.
        self._max_resolvers = max(1, max_resolvers)
        self._evict_log_every = max(1, resolver_evict_log_every)
        # None means no filtering; avoid the per-instance interface-name lookup.
        self._interface_in_scope = interface_in_scope
        self._tracker = InstanceTracker()
        self._bus = None
        self._server = None
        self._type_browser: _Tracked | None = None
        # stype -> _Tracked(ServiceBrowser). Kept for the session (freed only in
        # stop()); see the module docstring for why they are not freed on
        # type-removal.
        self._service_browsers: dict[str, _Tracked] = {}
        # rkey -> _Tracked(ServiceResolver), in LRU order (front = stalest).
        self._resolvers: OrderedDict[tuple, _Tracked] = OrderedDict()
        self._iface_names: dict[int, str] = {}
        self._evicted = 0

    def start(self) -> None:
        if dbus is None:
            raise RuntimeError("dbus binding unavailable; AvahiBrowser requires a panel")
        self._bus = dbus.SystemBus()
        self._server = dbus.Interface(self._bus.get_object(AVAHI_DBUS_NAME, "/"), _S_SERVER)
        path = self._server.ServiceTypeBrowserNew(
            AVAHI_IF_UNSPEC, AVAHI_PROTO_UNSPEC, "", dbus.UInt32(0)
        )
        tb = dbus.Interface(self._bus.get_object(AVAHI_DBUS_NAME, path), _S_TYPE_BROWSER)
        self._type_browser = _Tracked(tb)
        self._type_browser.matches.append(tb.connect_to_signal("ItemNew", self._on_type_new))
        logger.info("reason=avahiBrowseStarted")

    def stop(self) -> None:
        """Free every browser/resolver (avahi object + its signal receivers) and
        reset. Safe to call before start(). The bus is a process singleton, so
        leaked SignalMatch receivers would otherwise accumulate on every avahi
        restart."""
        for tracked in list(self._resolvers.values()):
            self._free_tracked(tracked)
        for tracked in list(self._service_browsers.values()):
            self._free_tracked(tracked)
        self._free_tracked(self._type_browser)
        self._resolvers.clear()
        self._service_browsers.clear()
        self._type_browser = None
        self._server = None
        self._tracker.clear()
        self._iface_names.clear()

    def is_alive(self) -> bool:
        """True if the avahi Server answers a method call. The service's liveness
        watchdog uses this: a hung daemon still owns the bus name, so
        NameOwnerChanged never fires for it."""
        if self._server is None:
            return False
        try:
            self._server.GetVersionString()
            return True
        except Exception:
            return False

    # -- object lifecycle helpers -------------------------------------------

    def _free_tracked(self, tracked: _Tracked | None) -> None:
        """Release an avahi object and its D-Bus signal receivers."""
        if tracked is None:
            return
        for match in tracked.matches:
            with contextlib.suppress(Exception):
                match.remove()
        tracked.matches.clear()
        _free(tracked.obj)

    def _evict_if_full(self) -> None:
        """Keep the concurrent-resolver count under the cap by freeing the
        least-recently-resolved resolver (the front of the LRU OrderedDict)."""
        while self._resolvers and len(self._resolvers) >= self._max_resolvers:
            _rkey, tracked = self._resolvers.popitem(last=False)
            self._free_tracked(tracked)
            self._evicted += 1
            if self._evicted % self._evict_log_every == 1:
                logger.warning(
                    "reason=resolverCapEvict,cap=%d,total_evicted=%d",
                    self._max_resolvers,
                    self._evicted,
                )

    # -- signal handlers -----------------------------------------------------

    def _on_type_new(self, interface, protocol, stype, domain, flags) -> None:
        stype = str(stype)
        if stype in self._service_browsers:
            return
        try:
            path = self._server.ServiceBrowserNew(
                AVAHI_IF_UNSPEC, AVAHI_PROTO_UNSPEC, stype, "", dbus.UInt32(0)
            )
        except dbus.DBusException as e:
            logger.warning("reason=serviceBrowserNewFailed,type=%s,error=%s", stype, e)
            return
        browser = dbus.Interface(self._bus.get_object(AVAHI_DBUS_NAME, path), _S_BROWSER)
        tracked = _Tracked(browser)
        try:
            tracked.matches.append(browser.connect_to_signal("ItemNew", self._on_service_new))
            tracked.matches.append(browser.connect_to_signal("ItemRemove", self._on_service_remove))
        except Exception as e:
            # Wiring failed after ServiceBrowserNew created the avahi object; free
            # it (and any receiver attached so far) so it doesn't leak.
            logger.warning("reason=serviceBrowserWireFailed,type=%s,error=%s", stype, e)
            self._free_tracked(tracked)
            return
        self._service_browsers[stype] = tracked

    def _on_service_new(self, interface, protocol, name, stype, domain, flags) -> None:
        rkey = (int(interface), int(protocol), str(name), str(stype), str(domain))
        if rkey in self._resolvers:
            self._resolvers.move_to_end(rkey)  # mark recently seen for the LRU
            return
        # Interface scoping: never resolve (nor publish) a service on an
        # out-of-scope interface. Only consult the interface name when a filter is
        # configured, so the common no-filter case skips the D-Bus name lookup.
        if self._interface_in_scope is not None and not self._interface_in_scope(
            self._iface_name(interface)
        ):
            return
        self._evict_if_full()
        try:
            path = self._server.ServiceResolverNew(
                interface,
                protocol,
                name,
                stype,
                domain,
                AVAHI_PROTO_UNSPEC,
                dbus.UInt32(0),
            )
        except dbus.DBusException as e:
            logger.warning("reason=resolverNewFailed,name=%s,error=%s", name, e)
            return
        resolver = dbus.Interface(self._bus.get_object(AVAHI_DBUS_NAME, path), _S_RESOLVER)
        tracked = _Tracked(resolver)
        try:
            tracked.matches.append(resolver.connect_to_signal("Found", self._on_resolved))
            tracked.matches.append(
                resolver.connect_to_signal(
                    "Failure", lambda err, rk=rkey: self._on_resolve_failure(rk, err)
                )
            )
        except Exception as e:
            # Wiring failed after ServiceResolverNew created the avahi object; free
            # it (and any receiver attached so far) so it doesn't leak.
            logger.warning("reason=resolverWireFailed,name=%s,error=%s", name, e)
            self._free_tracked(tracked)
            return
        self._resolvers[rkey] = tracked

    def _on_resolve_failure(self, rkey, err) -> None:
        # Free the resolver so a never-resolving instance doesn't pin an avahi
        # object; avahi re-issues ItemNew if it re-announces.
        logger.debug("reason=resolveFailed,rkey=%s,err=%s", rkey, err)
        self._free_tracked(self._resolvers.pop(rkey, None))

    def _on_resolved(
        self,
        interface,
        protocol,
        name,
        stype,
        domain,
        host,
        aprotocol,
        address,
        port,
        txt,
        flags,
    ) -> None:
        rkey = (int(interface), int(protocol), str(name), str(stype), str(domain))
        if rkey in self._resolvers:
            self._resolvers.move_to_end(rkey)  # most-recently-resolved -> LRU tail
        if int(flags) & AVAHI_LOOKUP_RESULT_LOCAL:
            return  # parity with avahi-browse --ignore-local: skip our own host's services
        obs = self._tracker.found(
            service_type=str(stype),
            interface=self._iface_name(interface),
            instance_name=str(name),
            hostname=str(host),
            port=int(port),
            address=str(address),
            txt=parse_dbus_txt(txt),
            protocol=int(protocol),
        )
        self._on_active(obs)

    def _on_service_remove(self, interface, protocol, name, stype, domain, flags) -> None:
        rkey = (int(interface), int(protocol), str(name), str(stype), str(domain))
        self._free_tracked(self._resolvers.pop(rkey, None))
        iface_name = self._iface_name(interface)
        reduced = self._tracker.removed(
            service_type=str(stype),
            interface=iface_name,
            instance_name=str(name),
            protocol=int(protocol),
        )
        if reduced is None:
            self._on_removed((str(stype), iface_name, str(name)))
        else:
            self._on_active(reduced)  # still present on another protocol

    def _iface_name(self, index) -> str:
        index = int(index)
        if index not in self._iface_names:
            try:
                self._iface_names[index] = str(self._server.GetNetworkInterfaceNameByIndex(index))
            except dbus.DBusException:
                self._iface_names[index] = str(index)
        return self._iface_names[index]


def _free(obj) -> None:
    """Best-effort Free() on an avahi browser/resolver D-Bus object."""
    if obj is None:
        return
    with contextlib.suppress(Exception):
        obj.Free()

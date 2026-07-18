"""The mdns-discovery service: an event-driven avahi browse feeding the
memory-bounded registry, published to the local MQTT bus.

A GLib main loop consumes the discovery backend's ItemNew/ItemRemove/Found
signals (via AvahiBrowser) and drives the registry directly; a GLib timeout runs
the GC sweep; avahi coming or going (NameOwnerChanged) flushes and rebuilds the
view. Removal is avahi's authoritative DNS-SD goodbye / mDNS TTL expiry, so there
is no poll loop and no staleness inference here.

The publisher also maintains a Homie-borrowed $state liveness signal at
{base}/$state (init -> ready -> {disconnected | lost}, lost via the MQTT will),
so consumers know whether the retained tree is being maintained.

Configuration is a typed Config (env prefix MDNSD_, optional TOML file, CLI); see
config.py. The runtime never reads the environment directly.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import sys
import threading
import time

from ebus_mqtt_client import MqttClient

from ebus_mdns_discovery.browser import AVAHI_DBUS_NAME, AvahiBrowser
from ebus_mdns_discovery.config import DEFAULT_CLIENT_ID, Config, ConfigError, load
from ebus_mdns_discovery.registry import Registry

try:
    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib
except ImportError:  # off-panel (dev/CI): the service only runs on a panel
    dbus = None
    GLib = None

logger = logging.getLogger("mdns-discovery")

# The publisher's Homie-borrowed $state lifecycle values (a private liveness
# signal at {base}/$state, not a Homie device).
_STATE_INIT = "init"
_STATE_READY = "ready"
_STATE_DISCONNECTED = "disconnected"
_STATE_LOST = "lost"

# avahi-daemon liveness watchdog: a hung daemon still owns the bus name, so
# NameOwnerChanged never fires for it.
_SYSTEMD_BUS = "org.freedesktop.systemd1"
_SYSTEMD_PATH = "/org/freedesktop/systemd1"
_SYSTEMD_MANAGER = "org.freedesktop.systemd1.Manager"
AVAHI_DAEMON_SERVICE = "avahi-daemon.service"
_WATCHDOG_FAILURES_BEFORE_RESTART = 3


def _restart_avahi_daemon() -> None:
    logger.warning("reason=restartingAvahiDaemon")
    try:
        bus = dbus.SystemBus()
        manager = dbus.Interface(bus.get_object(_SYSTEMD_BUS, _SYSTEMD_PATH), _SYSTEMD_MANAGER)
        manager.RestartUnit(AVAHI_DAEMON_SERVICE, "replace")
    except dbus.DBusException as e:
        logger.warning("reason=avahiRestartFailed,error=%s", e)


def _make_publisher(mqttc: MqttClient | None):
    def publish(topic: str, payload: bytes, retain: bool) -> None:
        if mqttc is None:
            logger.info(
                "reason=wouldPublish,topic=%s,retain=%s,bytes=%s",
                topic,
                retain,
                len(payload),
            )
            return
        # Log-and-continue: one bad topic/record must not abort the loop.
        try:
            mqttc.publish(topic, payload, qos=1, retain=retain)
        except Exception:
            logger.exception("reason=publishFailed,topic=%s", topic)

    return publish


def _is_live_retained(payload) -> bool:
    """True if a retained message occupies this topic. An empty (or whitespace)
    payload is an already-cleared topic: nothing to delete."""
    return bool(payload) and bool(bytes(payload).strip())


def _enumerate_retained_topics(base, endpoint, port, client_id, quiet_seconds, max_seconds):
    """Return ``(topics, truncated)``: the set of topics under ``{base}/#`` that
    hold a retained message, and whether the drain hit its cap while records were
    still arriving. A throwaway client subscribes and reads the retained burst
    the broker sends after SUBACK. There is no end-of-retained marker, so we stop
    once no new record has arrived for ``quiet_seconds`` (so a large tree is not
    truncated by a fixed window), bounded by ``max_seconds``. ``topics`` and the
    last-seen timestamp are shared with the client's network thread, so a lock
    guards them: a wedged ``stop()`` could otherwise let a callback mutate the
    set while the caller iterates it."""
    topics: set[str] = set()
    lock = threading.Lock()
    last_msg = [None]  # monotonic ts of the most recent retained message

    def on_msg(topic, payload):
        if str(topic).rsplit("/", 1)[-1].startswith("$"):
            return  # never clear a $-attribute (our own $state lives at {base}/$state)
        if _is_live_retained(payload):
            with lock:
                topics.add(str(topic))
                last_msg[0] = time.monotonic()

    scanner = MqttClient(f"{client_id}-startup-scan", endpoint, port)
    scanner.subscribe(f"{base}/#", param=on_msg)
    scanner.start()
    start = time.monotonic()
    truncated = False
    try:
        while True:
            time.sleep(0.05)
            now = time.monotonic()
            with lock:
                quiet_since = last_msg[0] if last_msg[0] is not None else start
            if now - quiet_since >= quiet_seconds:
                break  # the burst has gone quiet: drain complete
            if now - start >= max_seconds:
                truncated = True  # still arriving at the cap
                break
    finally:
        scanner.stop()
    with lock:
        return set(topics), truncated


def _clear_topics(topics, publish) -> int:
    """Empty-retain every topic (the only payload that deletes a retained
    message). Pure over ``publish`` so it is unit-testable."""
    for topic in sorted(topics):
        publish(topic, b"", True)
    return len(topics)


def _make_backend(cfg: Config, *, on_active, on_removed):
    """Construct the discovery backend selected by ``cfg.backend``. Only avahi is
    implemented; the seam (see backend.py) keeps a second backend a contained add."""
    if cfg.backend == "avahi":
        return AvahiBrowser(
            on_active,
            on_removed,
            max_resolvers=cfg.max_resolvers,
            resolver_evict_log_every=cfg.resolver_evict_log_every,
            interface_in_scope=(cfg.interface_in_scope if cfg.interface_filtering else None),
        )
    raise ConfigError(
        f"unknown or unimplemented backend: {cfg.backend!r} (only 'avahi' is supported)"
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig()
    try:
        cfg = load(argv)
    except ConfigError as e:
        logger.error("reason=configError,error=%s", e)
        return 1
    logger.setLevel(getattr(logging, str(cfg.log_level).upper(), logging.INFO))

    if dbus is None or GLib is None:
        logger.error("reason=dbusOrGlibUnavailable,note=mdns-discovery is a panel service")
        return 1

    if cfg.backend != "avahi":
        logger.error(
            "reason=unknownBackend,backend=%s,note=only 'avahi' is supported",
            cfg.backend,
        )
        return 1

    base = cfg.topic_base
    gc_interval = cfg.gc_sweep_interval_seconds
    state_topic = f"{base}/$state"

    # The publisher's $state lifecycle. `current` is the last value we published;
    # the settle detector and the reconnect/reassert paths read it. `settle` holds
    # monotonic timestamps for the browse-settle heuristic.
    state = {"current": None}
    settle = {"last_change": 0.0, "started": 0.0}
    browse_active = [False]

    mqttc: MqttClient | None = None

    def publish_state(value: str) -> None:
        if mqttc is None:
            return
        state["current"] = value
        try:
            mqttc.publish(state_topic, value, qos=1, retain=True)
        except Exception:
            logger.exception("reason=publishStateFailed,state=%s", value)
        logger.info("reason=stateTransition,state=%s", value)

    if not cfg.no_mqtt:
        if not cfg.mqtt_host:
            logger.error(
                "reason=missingMqttConfig,note=set MDNSD_MQTT_HOST (or [mqtt].host / --mqtt-host)"
            )
            return 1
        endpoint = cfg.mqtt_host
        port = cfg.mqtt_port
        client_id = cfg.mqtt_client_id or DEFAULT_CLIENT_ID
        # LWT is set at construction (will_set runs in __init__): the broker
        # publishes $state=lost if we die ungracefully.
        mqttc = MqttClient(
            client_id,
            endpoint,
            port,
            lwt={
                "topic": state_topic,
                "payload": _STATE_LOST,
                "retain": True,
                "qos": 1,
            },
        )

        def on_broker_connect() -> None:
            # Fires on every _on_connect (paho invokes the callback regardless of
            # the CONNACK result), so skip a rejected connect. First real connect
            # -> init; a reconnect re-asserts the current state, overwriting a late
            # lost the broker may have fired for the dropped socket during the blip.
            if not mqttc.is_connected():
                return
            publish_state(state["current"] or _STATE_INIT)

        mqttc.on_connect_callback = on_broker_connect
        mqttc.start()
        # Wipe any records a previous process left retained on the broker before
        # the browse repopulates. flush() cannot reach them from an empty
        # registry, so we read the broker's own retained tree and clear it. The
        # enumerator excludes $-topics so it never clears our own $state. These
        # clears are queued before the browse publishes anything, so a still-present
        # service is re-published after its clear, not wiped by it.
        try:
            stale, truncated = _enumerate_retained_topics(
                base,
                endpoint,
                port,
                client_id,
                cfg.startup_clear_quiet_seconds,
                cfg.startup_clear_max_seconds,
            )
            cleared = _clear_topics(stale, _make_publisher(mqttc))
            if truncated:
                logger.warning(
                    "reason=startupClearTruncated,cleared=%s,"
                    "note=retained tree still arriving at cap; remainder clears next restart",
                    cleared,
                )
            else:
                logger.info("reason=startupClearedRetainedTree,topics=%s", cleared)
        except Exception:
            logger.exception("reason=startupClearFailed")

    registry = Registry(
        _make_publisher(mqttc),
        base=base,
        ttl_seconds=cfg.ttl_seconds,
        tombstone_linger_seconds=cfg.tombstone_linger_seconds,
        max_records=cfg.max_records,
    )

    def mark_active(obs) -> None:
        settle["last_change"] = time.monotonic()
        registry.mark_active(obs)

    def mark_removed(key) -> None:
        settle["last_change"] = time.monotonic()
        registry.mark_removed(key)

    # The signal-driven browse needs the GLib main loop wired into D-Bus before
    # the bus is created.
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    browser = _make_backend(cfg, on_active=mark_active, on_removed=mark_removed)

    watchdog_interval = cfg.avahi_watchdog_seconds
    settle_quiet = cfg.browse_settle_quiet_seconds
    settle_max = cfg.browse_settle_max_seconds
    reassert_secs = cfg.state_reassert_seconds
    retry_secs = 10

    def start_browse() -> bool:
        # stop -> flush -> start, so a reconnect carries nothing stale across the
        # gap. On failure (avahi may still be coming up) arm a bounded one-shot
        # retry so a failed start self-heals instead of leaving an empty view.
        browser.stop()
        registry.flush()
        try:
            browser.start()
        except Exception:
            browse_active[0] = False
            logger.exception("reason=avahiBrowseStartFailed,retryIn=%s", retry_secs)
            GLib.timeout_add_seconds(retry_secs, start_browse)
            return False
        browse_active[0] = True
        now = time.monotonic()
        settle["started"] = now
        settle["last_change"] = now
        return False  # one-shot when used as a timeout callback

    def on_avahi_owner(owner: str) -> None:
        # watch_name_owner fires once with the current owner, then on every change.
        if owner:
            logger.info("reason=avahiAppeared,restartingBrowse")
            publish_state(_STATE_INIT)  # the tree is (re)building
            start_browse()
        else:
            logger.warning("reason=avahiVanished,stoppingBrowse")
            browse_active[0] = False
            publish_state(_STATE_INIT)  # not maintained until avahi returns
            browser.stop()

    def reassert_ready() -> bool:
        # One-shot: republish ready to overwrite a late lost from a prior crashed
        # incarnation (the broker fires that will ~1.5x keepalive after the crash).
        if state["current"] == _STATE_READY:
            publish_state(_STATE_READY)
        return False

    def settle_tick() -> bool:
        # Publish ready once the initial browse burst goes quiet (or a cap elapses),
        # so ready implies a populated tree. Only while init and actively browsing.
        if state["current"] == _STATE_INIT and browse_active[0]:
            now = time.monotonic()
            if (now - settle["last_change"] >= settle_quiet) or (
                now - settle["started"] >= settle_max
            ):
                publish_state(_STATE_READY)
                GLib.timeout_add_seconds(reassert_secs, reassert_ready)
        return True

    def sweep_tick() -> bool:
        registry.sweep()
        return True  # keep the timeout repeating

    watchdog_failures = [0]

    def watchdog_tick() -> bool:
        if browser.is_alive():
            watchdog_failures[0] = 0
        else:
            watchdog_failures[0] += 1
            logger.warning("reason=avahiUnresponsive,count=%s", watchdog_failures[0])
            if watchdog_failures[0] >= _WATCHDOG_FAILURES_BEFORE_RESTART:
                _restart_avahi_daemon()
                watchdog_failures[0] = 0
        return True

    loop = GLib.MainLoop()

    def on_sigterm() -> bool:
        # Reach the finally so we can publish $state=disconnected before exit;
        # systemd's SIGTERM would otherwise kill us and leave the will (lost).
        logger.info("reason=sigterm,quitting")
        loop.quit()
        return False  # G_SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, on_sigterm)

    bus = dbus.SystemBus()
    bus.watch_name_owner(AVAHI_DBUS_NAME, on_avahi_owner)
    GLib.timeout_add_seconds(max(1, int(gc_interval)), sweep_tick)
    GLib.timeout_add_seconds(max(1, int(watchdog_interval)), watchdog_tick)
    GLib.timeout_add_seconds(1, settle_tick)

    logger.info("reason=starting,base=%s", base)
    try:
        loop.run()
    finally:
        # Deliberately NOT browser.stop() here: on process exit the OS closes the
        # D-Bus connection and avahi releases our browsers/resolvers, so its
        # blocking .Free() calls would only add shutdown latency (and could hang
        # past the systemd timeout if avahi were wedged). browser.stop() still runs
        # on the reconnect/vanish paths, where the cleanup actually matters.
        if mqttc is not None:
            # Detach the connect callback FIRST so a reconnect racing the shutdown
            # cannot re-assert 'ready' over our 'disconnected', and keep
            # state['current'] in sync with the wire. Best-effort: on a coordinated
            # reboot the broker may be gone, in which case the will (lost) fires.
            mqttc.on_connect_callback = None
            state["current"] = _STATE_DISCONNECTED
            with contextlib.suppress(Exception):
                mqttc.publish_and_flush(
                    state_topic, _STATE_DISCONNECTED, qos=1, retain=True, timeout=1.0
                )
            mqttc.stop()
    return 0


def run() -> None:
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        logger.exception("reason=mdnsDiscoveryFatal")
        sys.exit(1)


if __name__ == "__main__":
    run()

# ebus-mdns-discovery

[![PyPI version](https://img.shields.io/pypi/v/ebus-mdns-discovery.svg)](https://pypi.org/project/ebus-mdns-discovery/)
[![Python versions](https://img.shields.io/pypi/pyversions/ebus-mdns-discovery.svg)](https://pypi.org/project/ebus-mdns-discovery/)
[![CI](https://github.com/electrification-bus/python-mdns-discovery/actions/workflows/ci.yml/badge.svg)](https://github.com/electrification-bus/python-mdns-discovery/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

`mdns-discovery` browses the local network for DNS-SD/mDNS services and publishes each one it finds as a **retained MQTT record** on the configured broker, so any service on the LAN can look up "what is on the network, and how do I reach it" by subscribing instead of browsing itself.

It is the **browse-and-publish** side of discovery. The record model, its JSON Schema, the topic layout, tombstone/freshness semantics, a live-view `ServiceResolver`, and the `service-discovery` debug CLI all live in the companion library [`ebus-service-discovery`](https://github.com/electrification-bus/python-service-discovery) ([PyPI](https://pypi.org/project/ebus-service-discovery/)). That repo's README is the normative description of the wire contract; this package is only the publisher that produces those records.

Each discovered service instance becomes one retained record at `{base}/{service_type}/{interface}/{percent_encoded_instance}`, where `{base}` defaults to `local/mdns/discovery/v1`. Addresses are carried raw; scope/APIPA/reachability classification is derived client-side by the library, so consumers get correct IPv4/IPv6 reachability handling for free.

## Discovery backends

The publisher is built around a `DiscoveryBackend` seam: a backend browses the LAN and produces the observations the rest of the service turns into records. **avahi** is the only backend today (`AvahiBrowser`), selected by default (`MDNSD_BACKEND=avahi`); its D-Bus dependencies are the optional `avahi` extra. The seam leaves room for a pure-Python `zeroconf` backend (cross-platform dev, no system daemon) as a contained addition rather than a fork. The rest of the pipeline (the record model, the bounded registry, the `$state` liveness) is backend-agnostic.

## How the avahi backend works

Event-driven, on a GLib main loop, reacting to avahi's D-Bus signals:

1. **Browse.** A `ServiceTypeBrowser` discovers every service type on the LAN (no allowlist); a per-type `ServiceBrowser` reports each instance; a per-instance `ServiceResolver` turns an instance into host/addresses/port/txt. A service heard on both IPv4 and IPv6 is aggregated into one observation with both addresses. The host's own advertisements are skipped by avahi's `LOCAL` result flag (parity with `avahi-browse --ignore-local`).
2. **Model.** Build an `ebus_service_discovery.Record` per observation and publish it retained (`state=active`).
3. **Bound.** Removal is avahi's authoritative `ItemRemove` (a real DNS-SD goodbye or an mDNS TTL expiry): the record is **tombstoned** (`state=removed`), then after a bounded `tombstone_linger` a GC sweep **clears** its retained topic (an empty retained payload, the only thing that deletes the message from the broker) and evicts the entry. A hard LRU `max_records` cap bounds the keyspace.
4. **Reconcile on startup.** Before the browse repopulates, the service reads the broker's own retained tree under `{base}/#` and clears it, so records a previous process left behind are not orphaned.
5. **Liveness (`$state`).** The service maintains one retained topic `{base}/$state`, borrowing the Homie 5 device lifecycle: `init` while (re)building, `ready` once the initial browse settles, `disconnected` on a clean stop, and `lost` as the MQTT Last Will. Consumers gate their trust on `ready` (`ServiceResolver.bus_ready`).

## Install

```bash
# Linux with avahi (the panel/production target): pull in the avahi backend deps
pip install "ebus-mdns-discovery[avahi]"

# base install (any platform): the contract, registry, and MQTT wiring only
pip install ebus-mdns-discovery
```

The avahi backend needs the D-Bus binding and PyGObject (the `avahi` extra), plus a running `avahi-daemon`. In an OS image these are usually the system `python3-dbus` / `python3-pygobject` packages rather than the wheels.

## Run

```bash
MDNSD_MQTT_HOST=127.0.0.1 mdns-discovery
```

Only the broker host is required; everything else has a sane default. Add `--no-mqtt-please` to log what would be published instead of connecting (debugging).

## Configuration

Config is a single typed contract loaded with precedence `defaults < optional TOML file < environment (MDNSD_*) < CLI`. The runtime never reads the environment directly, which keeps the package free of any deployment-specific names: a systemd unit or container maps its own variables onto `MDNSD_*` in its launcher.

### MQTT broker

| Env | Default | Purpose |
|---|---|---|
| `MDNSD_MQTT_HOST` | (required) | broker host; the service fails loud if unset |
| `MDNSD_MQTT_PORT` | `1883` | broker port |
| `MDNSD_MQTT_CLIENT_ID` | `mdns_discovery` | MQTT client id |
| `MDNSD_MQTT_KEEPALIVE_SECONDS` | `60` | keepalive |
| `MDNSD_MQTT_USERNAME` / `MDNSD_MQTT_PASSWORD` | unset | reserved (anonymous if unset) |
| `MDNSD_MQTT_TLS` / `MDNSD_MQTT_CA` / `MDNSD_MQTT_CERT` / `MDNSD_MQTT_KEY` / `MDNSD_MQTT_INSECURE` / `MDNSD_MQTT_SERVER_NAME` | off | reserved TLS axis (surface frozen, not yet wired) |

### Publishing

| Env | Default | Purpose |
|---|---|---|
| `MDNSD_TOPIC_BASE` | `local/mdns/discovery/v1` | retained-topic root, including the contract-version segment |
| `MDNSD_BACKEND` | `avahi` | discovery backend (only `avahi` today) |

### Config file

Every setting can also come from a TOML file, pointed at by `MDNSD_CONFIG` or `--config` (default `/etc/mdns-discovery/config.toml`). The file is optional; environment variables override it, and CLI flags override both.

```toml
[mqtt]
host = "127.0.0.1"
port = 1883

[publish]
topic_base = "local/mdns/discovery/v1"

[interfaces]
allow = ["eth0", "eth1"]
deny  = ["wlan0_ap"]
glob  = true

[tuning]
max_records = 512
```

### Network interfaces in and out of scope

By default every interface avahi reports is published. To scope it, following the avahi model (deny wins, an empty allow-list means "all"):

```bash
MDNSD_ALLOW_INTERFACES=eth0,eth1          # publish only these
MDNSD_DENY_INTERFACES=wlan0_ap            # publish everything except this
MDNSD_ALLOW_INTERFACES="eth*,en*"         # globs (default on); exclude container churn:
MDNSD_DENY_INTERFACES="veth*,docker*,br-*"
```

Set `MDNSD_INTERFACE_GLOB=false` to require exact interface names. Names match the OS interface name (the same string used as the topic's `{interface}` segment).

### Tuning

Every knob has a working default; override only what you need.

| Env | Default | Purpose |
|---|---|---|
| `MDNSD_TTL_SECONDS` | unset | optional per-record ttl; unset because freshness is bus-level via `$state` |
| `MDNSD_TOMBSTONE_LINGER_SECONDS` | `900` | how long a tombstone lingers before its retained topic is cleared |
| `MDNSD_MAX_RECORDS` | `512` | hard LRU cap on tracked records (bounds the keyspace) |
| `MDNSD_GC_SWEEP_INTERVAL_SECONDS` | `60` | max wall time between GC sweeps |
| `MDNSD_STARTUP_CLEAR_QUIET_SECONDS` | `0.3` | end the startup retained-tree drain once idle this long |
| `MDNSD_STARTUP_CLEAR_MAX_SECONDS` | `5.0` | hard cap on the startup drain |
| `MDNSD_BROWSE_SETTLE_QUIET_SECONDS` | `2.0` | publish `$state=ready` once the browse burst is idle this long |
| `MDNSD_BROWSE_SETTLE_MAX_SECONDS` | `10.0` | hard cap before `$state=ready` fires anyway |
| `MDNSD_STATE_REASSERT_SECONDS` | `120` | re-assert `ready` this long after settling (beats a late `lost` will) |
| `MDNSD_AVAHI_WATCHDOG_SECONDS` | `60` | how often to probe avahi liveness |
| `MDNSD_MAX_RESOLVERS` | `512` | concurrent avahi resolver LRU cap |
| `MDNSD_RESOLVER_EVICT_LOG_EVERY` | `100` | sample rate for the resolver-cap-evict warning |
| `MDNSD_LOG_LEVEL` | `INFO` | log level |

## Layout

| Path | Role |
|---|---|
| `src/ebus_mdns_discovery/service.py` | the GLib main loop, avahi reconnect handling, MQTT lifecycle, and config |
| `src/ebus_mdns_discovery/config.py` | the typed `Config` dataclass and the layered loader |
| `src/ebus_mdns_discovery/backend.py` | the `DiscoveryBackend` protocol |
| `src/ebus_mdns_discovery/browser.py` | the avahi D-Bus browse (`AvahiBrowser`) + the pure `InstanceTracker` address aggregation |
| `src/ebus_mdns_discovery/registry.py` | the memory-bounded lifecycle: active/tombstone/clear/evict, LRU cap, GC sweep |

## Tests

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

The suite is offline and mock-based (fake avahi/dbus objects); the live D-Bus/GLib browse is validated on a real device.

## Requirements

- **Python 3.10+** (the package and its dependencies use 3.10 language features).
- [`ebus-service-discovery`](https://pypi.org/project/ebus-service-discovery/) and [`ebus-mqtt-client`](https://pypi.org/project/ebus-mqtt-client/) (installed automatically).
- An MQTT broker to publish to (`MDNSD_MQTT_HOST`).
- For the avahi backend (the `avahi` extra): the D-Bus binding and PyGObject, and a running `avahi-daemon`. In an OS image these are usually the system `python3-dbus` / `python3-pygobject` packages rather than the wheels.

## Releases

Released versions are published to [PyPI](https://pypi.org/project/ebus-mdns-discovery/); each is tagged `vX.Y.Z` in this repository and described in [CHANGELOG.md](CHANGELOG.md). The project follows [Semantic Versioning](https://semver.org/).

## Releasing

The version lives in exactly one place: `__version__` in `src/ebus_mdns_discovery/__init__.py`. `pyproject.toml` reads it dynamically, the `setup.py` legacy shim reads it by regex, and the publish workflow refuses to release a tag that disagrees with it. To cut a release:

1. Bump `__version__` in `src/ebus_mdns_discovery/__init__.py` (the only place).
2. Move the CHANGELOG's `[Unreleased]` entries under a new version heading.
3. Commit, then tag it `v`-prefixed to match: `git tag vX.Y.Z && git push --tags` (a plain `git push` does not trigger a release).

Pushing a `v*` tag runs the publish workflow, which verifies the tag equals `v$__version__`, builds the sdist and wheel, and publishes to PyPI via Trusted Publishing (OIDC, no stored token).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for Discussions, Issues, and pull requests. The daemon is intentionally vendor- and product-agnostic: it models generic DNS-SD discovery, and its configuration is a typed `Config` that a deployment maps its own names onto rather than one this package knows about. The record model and topic layout live in [`ebus-service-discovery`](https://github.com/electrification-bus/python-service-discovery); align changes to the wire contract there.

## License

[MIT License](LICENSE) — Copyright (c) 2026 Clark Communications Corporation

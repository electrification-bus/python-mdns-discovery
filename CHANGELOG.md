# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-18

### Added

- Initial public release. The `mdns-discovery` daemon browses the LAN for
  DNS-SD/mDNS services via avahi and publishes each as a retained v1 `Record` on
  the local MQTT bus, using the shared wire model from `ebus-service-discovery`.
- Bounded lifecycle: active/tombstone/clear/evict, an LRU `max_records` cap, a
  startup retained-tree reconcile, and a Homie-borrowed `{base}/$state` liveness
  signal (`init` -> `ready` -> `disconnected` | `lost`).
- Typed `Config` (a frozen dataclass) loaded from `defaults < optional TOML file
  < environment (MDNSD_*) < CLI`. The runtime modules never read the environment
  directly, so the package carries no deployment-specific names.
- Network-interface scoping (new): `allow_interfaces` / `deny_interfaces` /
  `interface_glob`, following the avahi model (deny wins, an empty allow-list
  means every interface is in scope). Off by default, so behavior is unchanged
  unless configured.
- A pluggable `DiscoveryBackend` seam. avahi is the only backend today (its D-Bus
  dependencies are the optional `avahi` extra); the seam leaves room for a
  pure-Python `zeroconf` backend without a fork.

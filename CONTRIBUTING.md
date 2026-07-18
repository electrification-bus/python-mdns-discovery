# Contributing to ebus-mdns-discovery

Thanks for your interest in contributing! `ebus-mdns-discovery` is the
browse-and-publish side of the eBus service-discovery bus: it browses the LAN for
DNS-SD/mDNS services and publishes each as a retained MQTT record, using the
shared wire model from
[`ebus-service-discovery`](https://github.com/electrification-bus/python-service-discovery).
It is intentionally **vendor- and product-agnostic** — it models generic DNS-SD
discovery, not any particular device or integration.

## How to contribute

### Discussions

Use [Discussions](https://github.com/electrification-bus/python-mdns-discovery/discussions) for:

- Open-ended questions about the daemon's design, scope, or intent.
- Proposed changes to what is published or to the config surface — worth aligning
  before writing code.
- A new discovery backend (e.g. `zeroconf`) or interface/service-type policy.
- Thinking out loud about a change before scoping it.

### Issues

Use [Issues](https://github.com/electrification-bus/python-mdns-discovery/issues) for actionable changes:

- Bug reports with reproduction steps.
- Concrete feature requests with a clear scope and a use case.
- Documentation gaps where a specific change is intended.

If you're not sure whether something is an Issue or a Discussion, start with a
Discussion — we can convert it later.

### Pull requests

Pull requests are welcome.

- For small fixes (typos, docstring tweaks, low-risk bug fixes with a test), open a PR directly.
- For substantive changes (new config, a new backend, changes to what is
  published), open a Discussion or Issue first so we can align on scope.
- **Stay generic.** No device-, vendor-, or deployment-specific logic. The
  configuration is a typed `Config`; a deployment maps its own env names onto
  `MDNSD_*` in its own launcher rather than teaching this package about them.
- **The wire contract is shared.** The record model and topic layout live in
  `ebus-service-discovery`; changes there affect every publisher and consumer.
- **Tests are required.** The suite is offline and mock-based (`pytest tests/`).
  The avahi D-Bus/GLib loop itself is validated on a panel, not in CI. New
  behavior needs a test; bug fixes need a regression test.
- **Keep comments to a minimum.** Write self-explanatory code; reserve comments
  for non-obvious *why* (a hidden constraint or a specific quirk).
- **The version lives in one place.** Bump `__version__` in
  `src/ebus_mdns_discovery/__init__.py` only — `pyproject.toml` reads it
  dynamically and `setup.py` reads it by regex. (The `setup.py` shim exists so
  legacy `setuptools<61` — pinned in some embedded builds — can build a wheel
  with correct metadata; its docstring explains why.)
- One commit per logical change is fine; we don't require squash or any particular branch naming.

## Releases

Releases to PyPI are automated via the [`Publish to PyPI`](.github/workflows/publish.yml)
GitHub Actions workflow, which runs on `v*` git tags using PyPI
[trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no stored
token). The workflow refuses to publish a tag whose version disagrees with
`__version__`. Move the `[Unreleased]` CHANGELOG entries under a dated version
heading in the same commit that bumps `__version__`, then tag `vX.Y.Z`.

## Maintenance posture

`ebus-mdns-discovery` is an active alpha project. Updates and maintenance,
including responses to issues filed on GitHub, will take place on an "as time and
resources permit" basis. It is maintained alongside
[`ebus-service-discovery`](https://github.com/electrification-bus/python-service-discovery),
[`ebus-mqtt-client`](https://github.com/electrification-bus/ebus-mqtt-client), and
the [Electrification Bus specification](https://github.com/electrification-bus/specification).

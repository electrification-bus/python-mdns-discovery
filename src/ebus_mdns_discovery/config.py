"""Typed configuration for the mdns-discovery daemon.

The runtime modules receive a frozen ``Config`` and never read the environment
themselves. ``load()`` is the single place env, file, and CLI names appear, with
precedence ``defaults < optional TOML file < environment (MDNSD_*) < CLI``, so the
same daemon runs from 12-factor env in a container or from a config file, and is
trivially constructed in tests.

The env prefix is ``MDNSD_``. A deployment that already exports its own variable
names (e.g. a systemd unit) translates them to ``MDNSD_*`` in its own launcher, so
this package carries no vendor-specific names.
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field, fields
from fnmatch import fnmatch
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no stdlib tomllib: optional backport
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

from ebus_service_discovery.record import DEFAULT_TOPIC_BASE

logger = logging.getLogger("mdns-discovery")

ENV_PREFIX = "MDNSD_"
DEFAULT_CONFIG_PATH = "/etc/mdns-discovery/config.toml"
DEFAULT_CLIENT_ID = "mdns_discovery"


class ConfigError(Exception):
    """A configuration value could not be parsed (a bad int, etc.)."""


@dataclass(frozen=True)
class Config:
    """The whole configuration surface, as one typed contract the runtime reads."""

    # -- MQTT broker (host required unless no_mqtt) ------------------------
    mqtt_host: str | None = None
    mqtt_port: int = 1883
    mqtt_client_id: str | None = None
    mqtt_keepalive_seconds: int = 60
    mqtt_username: str | None = None
    mqtt_password: str | None = field(default=None, repr=False)

    # -- MQTT TLS (reserved axis: surface frozen, not yet wired) -----------
    mqtt_tls: bool = False
    mqtt_ca: str | None = None
    mqtt_cert: str | None = None
    mqtt_key: str | None = None
    mqtt_insecure: bool = False
    mqtt_server_name: str | None = None

    # -- publish ----------------------------------------------------------
    topic_base: str = DEFAULT_TOPIC_BASE

    # -- discovery backend ------------------------------------------------
    backend: str = "avahi"

    # -- interface scoping (avahi model: deny wins, empty allow = all) ----
    allow_interfaces: tuple[str, ...] = ()
    deny_interfaces: tuple[str, ...] = ()
    interface_glob: bool = True

    # -- tuning -----------------------------------------------------------
    ttl_seconds: int | None = None
    tombstone_linger_seconds: float = 900.0
    max_records: int = 512
    gc_sweep_interval_seconds: float = 60.0
    startup_clear_quiet_seconds: float = 0.3
    startup_clear_max_seconds: float = 5.0
    browse_settle_quiet_seconds: float = 2.0
    browse_settle_max_seconds: float = 10.0
    state_reassert_seconds: float = 120.0
    avahi_watchdog_seconds: float = 60.0
    max_resolvers: int = 512
    resolver_evict_log_every: int = 100
    log_level: str = "INFO"

    # -- debug ------------------------------------------------------------
    no_mqtt: bool = False  # log what would publish instead of connecting

    def __post_init__(self) -> None:
        object.__setattr__(self, "topic_base", self.topic_base.rstrip("/"))
        object.__setattr__(self, "max_resolvers", max(1, self.max_resolvers))
        object.__setattr__(self, "max_records", max(1, self.max_records))

    @property
    def interface_filtering(self) -> bool:
        """True if any allow/deny is set (otherwise every interface is in scope)."""
        return bool(self.allow_interfaces or self.deny_interfaces)

    def interface_in_scope(self, iface: str) -> bool:
        """Deny wins; an empty allow-list means every interface is in scope."""
        if self.interface_glob:

            def matches(patterns: tuple[str, ...]) -> bool:
                return any(fnmatch(iface, p) for p in patterns)
        else:

            def matches(patterns: tuple[str, ...]) -> bool:
                return iface in patterns

        if matches(self.deny_interfaces):
            return False
        if not self.allow_interfaces:
            return True
        return matches(self.allow_interfaces)


# -- value coercion (env and file values arrive as strings / TOML scalars) ---


def _csv(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(t.strip() for t in value.split(",") if t.strip())
    return tuple(str(v) for v in value)  # a TOML array


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _opt_int(value: object) -> int | None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    return int(value)


_COERCE = {
    "mqtt_port": int,
    "mqtt_keepalive_seconds": int,
    "mqtt_tls": _bool,
    "mqtt_insecure": _bool,
    "allow_interfaces": _csv,
    "deny_interfaces": _csv,
    "interface_glob": _bool,
    "ttl_seconds": _opt_int,
    "tombstone_linger_seconds": float,
    "max_records": int,
    "gc_sweep_interval_seconds": float,
    "startup_clear_quiet_seconds": float,
    "startup_clear_max_seconds": float,
    "browse_settle_quiet_seconds": float,
    "browse_settle_max_seconds": float,
    "state_reassert_seconds": float,
    "avahi_watchdog_seconds": float,
    "max_resolvers": int,
    "resolver_evict_log_every": int,
    "no_mqtt": _bool,
}

_FIELD_NAMES = {f.name for f in fields(Config)}


def _coerce(name: str, raw: object) -> object:
    fn = _COERCE.get(name)
    if fn is None:
        return raw
    try:
        return fn(raw)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"invalid value for {name!r}: {raw!r} ({e})") from e


# -- layers ------------------------------------------------------------------


def _from_env(env: dict) -> dict:
    out: dict = {}
    for key, value in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX) :].lower()
        if name in _FIELD_NAMES:
            out[name] = _coerce(name, value)
    return out


def _from_file(path: Path | None) -> dict:
    if not path or not path.exists():
        return {}
    if tomllib is None:
        logger.warning("reason=configFileIgnored,path=%s,note=no tomllib/tomli available", path)
        return {}
    doc = tomllib.loads(path.read_text())
    flat: dict = {}
    mqtt = doc.get("mqtt", {})
    for k, v in mqtt.items():
        if k == "tls":
            continue
        flat[f"mqtt_{k}"] = v
    for k, v in mqtt.get("tls", {}).items():
        flat["mqtt_tls" if k == "enabled" else f"mqtt_{k}"] = v
    ifs = doc.get("interfaces", {})
    if "allow" in ifs:
        flat["allow_interfaces"] = ifs["allow"]
    if "deny" in ifs:
        flat["deny_interfaces"] = ifs["deny"]
    if "glob" in ifs:
        flat["interface_glob"] = ifs["glob"]
    if "topic_base" in doc.get("publish", {}):
        flat["topic_base"] = doc["publish"]["topic_base"]
    if "name" in doc.get("backend", {}):
        flat["backend"] = doc["backend"]["name"]
    if "level" in doc.get("log", {}):
        flat["log_level"] = doc["log"]["level"]
    flat.update(doc.get("tuning", {}))
    return {k: _coerce(k, v) for k, v in flat.items() if k in _FIELD_NAMES}


def _parse_cli(argv: list[str] | None) -> dict:
    p = argparse.ArgumentParser(prog="mdns-discovery")
    p.add_argument("--config", help="path to a TOML config file")
    p.add_argument("--mqtt-host")
    p.add_argument("--mqtt-port", type=int)
    p.add_argument("--topic-base")
    p.add_argument("--backend")
    p.add_argument("--log-level")
    p.add_argument(
        "--no-mqtt-please",
        dest="no_mqtt",
        action="store_true",
        default=None,
        help="log what would be published instead of connecting to MQTT (debug)",
    )
    ns = p.parse_args(argv)
    return {k: v for k, v in vars(ns).items() if v is not None}


def load(argv: list[str] | None = None, env: dict | None = None) -> Config:
    """Build a ``Config`` from ``defaults < TOML file < env (MDNSD_*) < CLI``.

    Raises ``ConfigError`` on an unparseable value. The required-broker check is
    left to the caller (the service), so a ``--no-mqtt-please`` dry run needs no
    broker.
    """
    env = os.environ if env is None else env
    cli = _parse_cli(argv)
    cfg_path = cli.pop("config", None) or env.get(f"{ENV_PREFIX}CONFIG") or DEFAULT_CONFIG_PATH
    path = Path(cfg_path) if cfg_path else None

    merged: dict = {}
    for layer in (_from_file(path), _from_env(env), cli):
        for k, v in layer.items():
            if v is not None:
                merged[k] = v
    return Config(**merged)

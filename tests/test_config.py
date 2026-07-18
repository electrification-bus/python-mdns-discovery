import pytest
from ebus_service_discovery.record import DEFAULT_TOPIC_BASE

from ebus_mdns_discovery.config import Config, ConfigError, load

# -- layered loading (defaults < file < env < cli) ---------------------------


def test_defaults_when_empty():
    cfg = load(argv=[], env={})
    assert cfg.mqtt_host is None
    assert cfg.mqtt_port == 1883
    assert cfg.backend == "avahi"
    assert cfg.topic_base == DEFAULT_TOPIC_BASE.rstrip("/")
    assert cfg.allow_interfaces == () and cfg.deny_interfaces == ()
    assert cfg.interface_filtering is False


def test_env_overrides_defaults():
    cfg = load(argv=[], env={"MDNSD_MQTT_HOST": "10.0.0.5", "MDNSD_MQTT_PORT": "1885"})
    assert cfg.mqtt_host == "10.0.0.5" and cfg.mqtt_port == 1885


def test_cli_overrides_env():
    cfg = load(argv=["--mqtt-host", "1.1.1.1"], env={"MDNSD_MQTT_HOST": "2.2.2.2"})
    assert cfg.mqtt_host == "1.1.1.1"


def test_unknown_env_keys_ignored():
    cfg = load(argv=[], env={"MDNSD_NOT_A_FIELD": "x", "UNRELATED": "y"})
    assert cfg.mqtt_port == 1883  # nothing blew up, defaults intact


# -- coercion ----------------------------------------------------------------


def test_csv_interfaces_parsed_and_trimmed():
    cfg = load(
        argv=[],
        env={"MDNSD_ALLOW_INTERFACES": "eth0, eth1 ,", "MDNSD_DENY_INTERFACES": "wlan0_ap"},
    )
    assert cfg.allow_interfaces == ("eth0", "eth1")
    assert cfg.deny_interfaces == ("wlan0_ap",)


def test_topic_base_trailing_slash_stripped():
    cfg = load(argv=[], env={"MDNSD_TOPIC_BASE": "local/mdns/x/"})
    assert cfg.topic_base == "local/mdns/x"


def test_ttl_optional_int():
    assert load(argv=[], env={}).ttl_seconds is None
    assert load(argv=[], env={"MDNSD_TTL_SECONDS": ""}).ttl_seconds is None
    assert load(argv=[], env={"MDNSD_TTL_SECONDS": "300"}).ttl_seconds == 300


def test_bool_env():
    assert load(argv=[], env={"MDNSD_MQTT_TLS": "true"}).mqtt_tls is True
    assert load(argv=[], env={"MDNSD_MQTT_TLS": "0"}).mqtt_tls is False


def test_bad_int_raises_config_error():
    with pytest.raises(ConfigError):
        load(argv=[], env={"MDNSD_MQTT_PORT": "notaport"})


def test_no_mqtt_flag():
    assert load(argv=["--no-mqtt-please"], env={}).no_mqtt is True
    assert load(argv=[], env={}).no_mqtt is False


def test_nonpositive_caps_clamped():
    cfg = load(argv=[], env={"MDNSD_MAX_RECORDS": "0", "MDNSD_MAX_RESOLVERS": "-5"})
    assert cfg.max_records == 1 and cfg.max_resolvers == 1


# -- interface_in_scope predicate (avahi model: deny wins, empty allow = all) --


def test_scope_empty_allow_is_all():
    cfg = Config()
    assert cfg.interface_in_scope("eth0") is True
    assert cfg.interface_in_scope("anything") is True


def test_scope_deny_wins_over_allow():
    cfg = Config(allow_interfaces=("eth0", "wlan0"), deny_interfaces=("wlan0",))
    assert cfg.interface_in_scope("eth0") is True
    assert cfg.interface_in_scope("wlan0") is False  # deny wins even though allowed
    assert cfg.interface_filtering is True


def test_scope_allow_only():
    cfg = Config(allow_interfaces=("eth0",))
    assert cfg.interface_in_scope("eth0") is True
    assert cfg.interface_in_scope("eth1") is False


def test_scope_globs_by_default():
    cfg = Config(allow_interfaces=("eth*", "en*"), deny_interfaces=("veth*",))
    assert cfg.interface_in_scope("eth0") is True
    assert cfg.interface_in_scope("en0") is True
    assert cfg.interface_in_scope("veth9") is False  # deny glob wins
    assert cfg.interface_in_scope("wlan0") is False  # not in allow


def test_scope_exact_when_glob_off():
    cfg = Config(allow_interfaces=("eth0",), interface_glob=False)
    assert cfg.interface_in_scope("eth0") is True
    assert cfg.interface_in_scope("eth00") is False  # exact, glob disabled

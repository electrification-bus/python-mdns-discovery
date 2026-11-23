import logging

from ebus_mdns_discovery._sampled_log import SampledWarning

LOGGER = logging.getLogger("mdns-discovery")


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_first_and_every_nth_are_warnings(caplog):
    caplog.set_level(logging.DEBUG, logger="mdns-discovery")
    warn = SampledWarning(LOGGER, "somethingFailed", every=3)
    for i in range(7):
        warn("name=%s", f"svc{i}")
    assert _warnings(caplog) == [
        "reason=somethingFailed,total=1,name=svc0",
        "reason=somethingFailed,total=4,name=svc3",
        "reason=somethingFailed,total=7,name=svc6",
    ]
    assert len(caplog.records) == 7


def test_every_one_warns_each_time(caplog):
    warn = SampledWarning(LOGGER, "somethingFailed", every=1)
    for _ in range(3):
        warn("x")
    assert len(_warnings(caplog)) == 3

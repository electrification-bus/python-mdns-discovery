"""mDNS/DNS-SD discovery service: browse the LAN via a discovery backend (avahi)
and publish each service as a retained v1 Record on the local MQTT bus, keeping
both the in-memory view and the retained-topic tree bounded.

The wire model and topic layout come from the shared ``ebus_service_discovery``
library; this package is only the publisher.
"""

from ebus_mdns_discovery.config import Config, load

__version__ = "0.1.0"

__all__ = ["Config", "load", "__version__"]

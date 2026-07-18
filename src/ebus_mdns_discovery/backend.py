"""The discovery-backend seam.

A backend browses the local network and drives the registry via two callbacks
supplied at construction: ``on_active(Observation)`` and ``on_removed(key)``.
avahi is the only backend today (``AvahiBrowser`` in ``browser.py``); the seam
exists so a second backend (e.g. a pure-Python ``zeroconf`` browser for
cross-platform dev) is a contained addition rather than a fork of the service.
The service selects the backend from ``Config.backend`` and drives its lifecycle.
"""

from __future__ import annotations

from typing import Protocol


class DiscoveryBackend(Protocol):
    """The lifecycle the service drives on any discovery backend. The observation
    callbacks are backend-specific and passed at construction, so the protocol is
    just start / stop / liveness."""

    def start(self) -> None:
        """Begin browsing. May raise if the backend's substrate is unavailable."""
        ...

    def stop(self) -> None:
        """Stop browsing and release all resources. Safe to call before start()."""
        ...

    def is_alive(self) -> bool:
        """True if the backend is responsive (the liveness watchdog uses this)."""
        ...

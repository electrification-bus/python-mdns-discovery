"""A warning that can fire once per discovered service, logged at a bounded rate."""

from __future__ import annotations

import logging

DEFAULT_LOG_EVERY = 100


class SampledWarning:
    """Logs the first occurrence and then every ``every``-th, with a running total.
    Every occurrence is also logged at DEBUG."""

    def __init__(self, logger: logging.Logger, reason: str, every: int = DEFAULT_LOG_EVERY) -> None:
        self._logger = logger
        self._reason = reason
        self._every = max(1, every)
        self.total = 0

    def __call__(self, detail: str, *args: object) -> None:
        self.total += 1
        sampled = (self.total - 1) % self._every == 0
        self._logger.log(
            logging.WARNING if sampled else logging.DEBUG,
            f"reason={self._reason},total=%d,{detail}",
            self.total,
            *args,
        )

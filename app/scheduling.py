"""Background sync. Scheduled runs avoid the top of the hour."""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, timezone

logger = logging.getLogger("fitness_coach")


def seconds_to_leave_top_of_hour(now: datetime) -> float:
    """Hevy asks scheduled clients not to call exactly at minute 00."""
    if now.minute == 0 and now.second < 45:
        return float(45 - now.second)
    return 0.0


class SyncScheduler:
    def __init__(
        self,
        trigger,
        interval_seconds: int,
        *,
        clock=None,
        jitter_seconds: int = 1800,
        initial_delay: float = 20,
        name: str = "hevy-sync",
    ):
        self.name = name
        self.trigger = trigger
        self.interval_seconds = interval_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.jitter_seconds = jitter_seconds
        self.initial_delay = initial_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        if self._stop.wait(self.initial_delay):
            return
        while not self._stop.is_set():
            pause = seconds_to_leave_top_of_hour(self.clock())
            if pause and self._stop.wait(pause):
                return
            try:
                self.trigger()
            except Exception:
                logger.error("scheduled sync failed")
            delay = self.interval_seconds + random.randint(0, self.jitter_seconds)
            if self._stop.wait(delay):
                return

"""
Pressure step sequencing for VasoTracker 2.

This engine drives scripted pressure protocols and is intentionally UI-agnostic.
It only depends on a callable that can set the pressure and an optional
callback to report step progress.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List


@dataclass(frozen=True)
class Step:
    target_mm_hg: float
    dwell_s: float


class StepsEngine:
    """Background runner that drives a sequence of pressure steps."""

    def __init__(
        self,
        set_pressure: Callable[[float], None],
        on_step_start: Callable[[int, Step], None] | None = None,
    ) -> None:
        self._set_pressure = set_pressure
        self._on_step_start = on_step_start or (lambda index, step: None)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def run(
        self,
        steps: Iterable[Step],
        start_delay_s: float = 0.0,
    ) -> None:
        """Start executing the provided sequence of steps."""
        self.stop()
        steps_list = list(steps)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            args=(steps_list, start_delay_s),
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop any active protocol."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None

    # Internal ------------------------------------------------------------------
    def _loop(self, steps: List[Step], start_delay: float) -> None:
        if start_delay > 0:
            self._sleep_with_cancellation(start_delay)
            if self._stop_event.is_set():
                return

        for index, step in enumerate(steps, start=1):
            if self._stop_event.is_set():
                break
            self._set_pressure(step.target_mm_hg)
            self._on_step_start(index, step)
            self._sleep_with_cancellation(step.dwell_s)

    def _sleep_with_cancellation(self, duration: float) -> None:
        end_time = time.time() + max(0.0, duration)
        while time.time() < end_time:
            if self._stop_event.wait(timeout=0.05):
                return


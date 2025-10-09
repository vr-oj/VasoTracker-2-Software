# #################################################
# # VasoTracker 2 - Blood Vessel Diameter Measurement Software
# #
# # Author: Calum Wilson, Matthew D Lee, and Chris Osborne
# # License: BSD 3-Clause License (See main file for details)
# # Website: www.vasostracker.com
# #
# #################################################

# Vendor-neutral pressure device abstractions. These classes allow
# VasoTracker to support multiple hardware backends (Arduino VasoMoto,
# NI-DAQ, software simulation, or a null device) without coupling the
# UI or data logging logic to any particular vendor API.

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple
import re
import threading
import time


class PressureDevice(Protocol):
    """Interface implemented by every concrete pressure backend."""

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def set_pressure(self, value_mmHg: float) -> None:
        ...

    def read_latest(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Return (p1_mmHg, p2_mmHg, setpoint_mmHg)."""


@dataclass
class NullPressureDevice:
    """No-op device used when no hardware is selected."""

    def __post_init__(self) -> None:
        self._latest: Tuple[Optional[float], Optional[float], Optional[float]] = (
            None,
            None,
            None,
        )

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def set_pressure(self, value_mmHg: float) -> None:
        self._latest = (self._latest[0], self._latest[1], float(value_mmHg))

    def read_latest(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        return self._latest


class SimPressureDevice:
    """Simple first-order plant that drifts toward setpoint (for offline tests)."""

    def __init__(self, update_hz: float = 20.0, tau_s: float = 0.8) -> None:
        self._sp = 0.0
        self._p1 = 0.0
        self._p2 = 0.0
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dt = 1.0 / max(1.0, update_hz)
        self._tau = max(1e-3, tau_s)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def set_pressure(self, value_mmHg: float) -> None:
        self._sp = max(0.0, float(value_mmHg))

    def _run(self) -> None:
        k = self._dt / self._tau
        while not self._stop_evt.is_set():
            self._p1 += k * (self._sp - self._p1)
            # Small mismatch to avoid perfect symmetry
            self._p2 += k * (self._sp - self._p2 * 0.98)
            time.sleep(self._dt)

    def read_latest(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        return (round(self._p1, 2), round(self._p2, 2), round(self._sp, 2))


class ArduinoPressureDevice:
    """
    Arduino-based implementation that speaks the minimal VasoMoto protocol.

    Serial protocol (ASCII, newline terminated):
      PC -> Arduino: 'PING\\n' | 'START\\n' | 'STOP\\n'
        Setpoint: 'SET:<mmHg>\\n' (modern) and '<mmHg>\\n' (legacy VasoMoto firmware)
      Arduino -> PC: 'P1:<v>,P2:<v>,SET:<v>\\n' (modern) or '<P1:<v>;P2:<v>>' (legacy)
    """

    def __init__(self, arduino, stream_rate_hz: float = 20.0) -> None:
        """
        Parameters
        ----------
        arduino:
            Instance exposing `sendData(text: str) -> None` and
            `readline(timeout: float) -> Optional[str]`.
            The utilities.VT_Arduino.Arduino class satisfies this interface.
        stream_rate_hz:
            Expected telemetry rate; used to set the poll timeout.
        """

        self.arduino = arduino
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._latest: Tuple[Optional[float], Optional[float], Optional[float]] = (
            None,
            None,
            None,
        )
        self._pattern = re.compile(
            r"P1:(-?\d+\.?\d*),\s*P2:(-?\d+\.?\d*),\s*SET:(-?\d+\.?\d*)"
        )
        self._pattern_legacy = re.compile(
            r"<\s*P1:(-?\d+\.?\d*)\s*[,;]\s*P2:(-?\d+\.?\d*)\s*(?:[,;]\s*SET:(-?\d+\.?\d*))?\s*>"
        )
        self._last_sent_set_mmHg: Optional[float] = None
        self._interval = 1.0 / max(1.0, stream_rate_hz)

    def start(self) -> None:
        if not getattr(self.arduino, "is_connected", False):
            return

        self._stop_evt.clear()
        try:
            self.arduino.sendData("PING\n")
        except Exception:
            pass

        try:
            self.arduino.sendData("START\n")
        except Exception:
            pass

        if self._thread and self._thread.is_alive():
            return

        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        try:
            self.arduino.sendData("STOP\n")
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def set_pressure(self, value_mmHg: float) -> None:
        if not getattr(self.arduino, "is_connected", False):
            return

        v = max(0.0, float(value_mmHg))
        self._last_sent_set_mmHg = v
        try:
            self.arduino.sendData(f"SET:{v:.2f}\n")
        except Exception:
            pass

    def _reader_loop(self) -> None:
        while not self._stop_evt.is_set():
            if not getattr(self.arduino, "is_connected", False):
                time.sleep(self._interval)
                continue
            try:
                line = self.arduino.readline(timeout=self._interval)
            except Exception:
                line = None
            if not line:
                continue
            s = line.strip()
            match = self._pattern.search(s)
            match_legacy = None
            sp: Optional[float] = None
            if match:
                p1 = float(match.group(1))
                p2 = float(match.group(2))
                sp = float(match.group(3))
            else:
                match_legacy = self._pattern_legacy.search(s)
                if match_legacy:
                    p1 = float(match_legacy.group(1))
                    p2 = float(match_legacy.group(2))
                    g3 = match_legacy.group(3)
                    sp = float(g3) if g3 is not None else None
            if match or match_legacy:
                if sp is None:
                    previous = self._latest[2]
                    sp = previous if previous is not None else self._last_sent_set_mmHg
                self._latest = (p1, p2, sp)

    def read_latest(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        return self._latest


class NIDaqPressureDevice:
    """Thin wrapper around NI-DAQ PyDAQmx tasks (optional backend)."""

    def __init__(self, ai_task=None, ao_task=None, scale: float = 1.0) -> None:
        self._latest: Tuple[Optional[float], Optional[float], Optional[float]] = (
            None,
            None,
            None,
        )
        self._ai = ai_task
        self._ao = ao_task
        self._scale = float(scale)

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def set_pressure(self, value_mmHg: float) -> None:
        try:
            from PyDAQmx import Task  # noqa: F401
        except Exception as exc:
            raise RuntimeError("PyDAQmx not available for NI-DAQ pressure control") from exc

        if self._ao is None:
            raise RuntimeError("Analog output task not configured for NI-DAQ pressure device")

        volts = float(value_mmHg) * self._scale
        self._ao.WriteAnalogScalarF64(True, 10.0, volts, None)
        self._latest = (self._latest[0], self._latest[1], float(value_mmHg))

    def read_latest(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        # Keeping async reads optional – callers may supply ai_task to refresh.
        return self._latest

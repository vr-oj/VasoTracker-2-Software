"""
Thin VasoMoto serial helper for the new thin-firmware architecture.

Responsibilities:
  * Maintain exclusive serial access with background RX/TX threads.
  * Parse telemetry lines that look like: `DATA T=<ms> P=<meas> P_SET=<applied>`.
  * Expose `set_pressure(mmHg)` that enqueues `SET P=<value>` writes.
  * Surface parsed telemetry and ACK notifications through a queue.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from typing import Dict, Optional, Tuple

try:
    import serial
except ImportError as exc:  # pragma: no cover
    raise ImportError("pyserial is required for Arduino communication.") from exc


DATA_RE = re.compile(
    r"^DATA\s+T=(\d+)\s+P=([\d\.\-NaN]+)\s+P_SET=([\d\.\-NaN]+)",
    re.IGNORECASE,
)
ACK_RE = re.compile(r"^ACK\s+SET\s+P=([\d\.\-]+)\s+T=(\d+)", re.IGNORECASE)


class VasoMotorPort:
    """Threaded serial helper that treats the Arduino as a thin I/O device."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 0.05) -> None:
        self._serial = serial.Serial(
            port=port,
            baudrate=baud,
            timeout=timeout,
            write_timeout=0.2,
            exclusive=True,
        )
        try:
            self._serial.setDTR(False)
            self._serial.setRTS(False)
        except Exception:
            pass

        self.rx_queue: "queue.Queue[Tuple[str, Dict[str, float]]]" = queue.Queue(maxsize=1000)
        self.state: Dict[str, float] = {"t_ms": float("nan"), "p": float("nan"), "p_set": float("nan")}
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._tx_thread = threading.Thread(target=self._tx_loop, daemon=True)
        self._tx_queue: "queue.Queue[str]" = queue.Queue()
        self._running = True

        self._rx_thread.start()
        self._tx_thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._running = False
        time.sleep(0.1)
        try:
            self._serial.close()
        except Exception:
            pass

    def set_pressure(self, mmhg: float) -> None:
        """Enqueue a new setpoint command."""
        self._tx_queue.put(f"SET P={mmhg:.1f}")

    def get_latest(self, timeout: Optional[float] = None) -> Optional[Tuple[str, Dict[str, float]]]:
        """Block until a telemetry or ACK event is available."""
        try:
            return self.rx_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------
    def _rx_loop(self) -> None:
        buf = b""
        while self._running:
            chunk = self._serial.read(512)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                data = self._parse_line(text)
                if data is None:
                    continue
                kind, payload = data
                try:
                    self.rx_queue.put_nowait((kind, payload))
                except queue.Full:
                    try:
                        self.rx_queue.get_nowait()
                        self.rx_queue.put_nowait((kind, payload))
                    except Exception:
                        pass

    def _tx_loop(self) -> None:
        while self._running:
            try:
                cmd = self._tx_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            payload = (cmd + "\n").encode("utf-8")
            try:
                self._serial.write(payload)
                self._serial.flush()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _parse_line(self, line: str) -> Optional[Tuple[str, Dict[str, float]]]:
        if match := DATA_RE.match(line):
            t_ms, p_raw, p_set_raw = match.groups()
            payload = {
                "t_ms": float(t_ms),
                "p": float(p_raw) if p_raw != "NaN" else float("nan"),
                "p_set": float(p_set_raw) if p_set_raw != "NaN" else float("nan"),
            }
            self.state.update(payload)
            return "DATA", payload
        if match := ACK_RE.match(line):
            payload = {
                "t_ms": float(match.group(2)),
                "p_set": float(match.group(1)),
            }
            return "ACK", payload
        return None


__all__ = ["VasoMotorPort"]

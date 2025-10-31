"""
Pressure adapter for VasoTracker 2.

This module wraps the low-level `VasoMotorPort` helper and presents a small
interface that the rest of the application (session controller, tests, etc.)
expect: `connect`, `close`, `read`, `_set_target`, and `query_setpoint`.

Telemetry is streamed asynchronously from the device.  Each `DATA` line is
converted into a `PressureReading` containing host timestamp, device time,
pressure, and applied setpoint.  `ACK` lines are used to surface setpoint
echoes so higher layers can confirm when a command has been applied.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

try:
    import serial  # type: ignore[import]
    from serial.tools import list_ports  # type: ignore[import]
except ImportError as exc:  # pragma: no cover - runtime dependency
    raise ImportError(
        "pyserial is required for Arduino communication. Install with `pip install pyserial`."
    ) from exc

try:  # Optional dependency; only used for busy-port diagnostics.
    import psutil  # type: ignore[import]
except ImportError:  # pragma: no cover - psutil is optional
    psutil = None

from .vasomotor_port import VasoMotorPort


@dataclass
class PressureReading:
    """Structured telemetry emitted by the pressure controller."""

    p1: Optional[float]
    p2: Optional[float]
    setpoint: Optional[float]
    raw: str
    t: float  # monotonic timestamp
    device_time_ms: Optional[float] = None


class PressureAdapter:
    """
    Threaded adapter that manages serial I/O with the pressure hardware.

    The adapter is re-entrant: calling `connect` repeatedly tears down and
    re-establishes the connection. Readings are published to an internal queue
    so consumers can poll without blocking the UI thread.
    """

    ACK_RE = re.compile(r"^ACK\s+SET\s+P=([\d\.\-]+)\s+T=(\d+)")

    def __init__(
        self,
        port: Optional[str] = None,
        baud: int = 115200,
        timeout: float = 0.2,
        queue_size: int = 1000,
        setpoint_callback: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._queue: "queue.Queue[PressureReading]" = queue.Queue(maxsize=queue_size)
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._vm_port: Optional[VasoMotorPort] = None
        self._setpoint_callback = setpoint_callback
        self._last_callback_value: Optional[float] = None
        self._last_command_value: Optional[float] = None
        self._last_close_ts: float = 0.0
        self.protocol: Optional[str] = None  # For compatibility with existing UI/tests.
        self._keepalive_interval_s = 0.8
        self._keepalive_evt = threading.Event()
        self._keepalive_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    @staticmethod
    def list_serial_ports() -> List[Tuple[str, str]]:
        """
        Enumerate available serial ports with friendly names.

        Returns:
            List of tuples: [(device_path, description), ...]
        """
        ports = []
        for port_info in list_ports.comports():
            description = " ".join(
                part
                for part in (
                    port_info.manufacturer or "",
                    port_info.description or "",
                    port_info.hwid or "",
                )
                if part
            ).strip()
            ports.append((port_info.device, description or port_info.device))
        return ports

    def connect(self) -> None:
        """Connect to the pressure controller, trying candidate ports as needed."""
        with self._lock:
            self.close()
            now = time.monotonic()
            if now - self._last_close_ts < 0.2:
                time.sleep(0.2 - (now - self._last_close_ts))

            candidates = (
                [self.port] if self.port else [device for device, _ in self.list_serial_ports()]
            )
            if not candidates:
                raise RuntimeError("No serial ports detected.")

            last_error: Optional[Exception] = None
            for device in candidates:
                if not device:
                    continue
                try:
                    self._vm_port = self._open_port(device)
                    self.port = device
                    self.protocol = "vasomotor"
                    self._start_reader()
                    self._start_keepalive()
                    return
                except Exception as exc:  # noqa: BLE001 - surface raw message
                    last_error = exc
                    self._vm_port = None
            message = self._humanize_serial_error(last_error)
            raise RuntimeError(
                f"Unable to connect to any serial port ({', '.join(candidates)}). Last error: {message}"
            )

    def reconnect(self) -> None:
        """Convenience wrapper that simply calls `connect`."""
        self.connect()

    def close(self) -> None:
        """Tear down the serial connection and stop background threads."""
        with self._lock:
            self._stop_reader()
            self._stop_keepalive()
            port = self._vm_port
            self._vm_port = None
            if port is not None:
                try:
                    port.close()
                except Exception:  # noqa: BLE001 - best effort
                    pass
            self.protocol = None
            self._last_close_ts = time.monotonic()

    def read(self, timeout: float = 0.0) -> Optional[PressureReading]:
        """
        Retrieve the next telemetry reading.

        Args:
            timeout: Maximum seconds to wait. Zero means non-blocking.
        """
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def iter_readings(self) -> Iterable[PressureReading]:
        """Yield readings as they become available (blocking generator)."""
        while True:
            reading = self.read(timeout=None)
            if reading is None:
                return
            yield reading

    # ------------------------------------------------------------------ #
    # Command helpers                                                    #
    # ------------------------------------------------------------------ #
    def _set_target(self, target_mm_hg: float) -> None:
        """Queue a new pressure setpoint command."""
        value = float(target_mm_hg)
        with self._lock:
            if not self._vm_port:
                return
            self._last_command_value = value
            self._vm_port.set_pressure(value)

    def set_pressure(self, target_mm_hg: float) -> None:
        """Legacy entrypoint retained to catch direct device usage."""
        raise RuntimeError(
            "PressureAdapter.set_pressure() is no longer available. "
            "Route setpoint changes through SessionController.apply_setpoint()."
        )

    def query_setpoint(self) -> None:
        """
        Request the device to re-emit its applied setpoint.

        The VasoMoto firmware continuously emits DATA lines, so we simply
        re-issue the last command (if available) to prompt an ACK.
        """
        with self._lock:
            if not self._vm_port or self._last_command_value is None:
                return
            self._vm_port.tx_q.put(f"SET P={self._last_command_value:.1f}")

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #
    def _open_port(self, device: str) -> VasoMotorPort:
        try:
            return VasoMotorPort(
                device,
                baud=self.baud,
                read_timeout=max(0.02, min(self.timeout, 0.2)),
                write_timeout=0.2,
            )
        except serial.SerialException as exc:
            owner = self._guess_port_owner(device)
            if owner:
                raise serial.SerialException(
                    f"Port {device} appears busy (likely used by {owner})."
                ) from exc
            raise

    def _start_reader(self) -> None:
        self._stop_event.clear()
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _stop_reader(self) -> None:
        self._stop_event.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=0.5)
        self._reader_thread = None
        with self._queue.mutex:
            self._queue.queue.clear()

    def _start_keepalive(self) -> None:
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return
        self._keepalive_evt.clear()
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive_thread.start()

    def _stop_keepalive(self) -> None:
        self._keepalive_evt.set()
        thread = self._keepalive_thread
        if thread and thread.is_alive():
            thread.join(timeout=0.5)
        self._keepalive_thread = None

    def _reader_loop(self) -> None:
        while not self._stop_event.is_set():
            port = self._vm_port
            if port is None:
                time.sleep(0.05)
                continue
            try:
                event = port.rx_q.get(timeout=0.1)
            except queue.Empty:
                continue
            kind = event[0]
            if kind == "DATA":
                payload = event[1]
                if not isinstance(payload, dict):
                    continue
                raw = str(payload.get("raw", ""))
                pressure = self._safe_float(payload.get("p"))
                setpoint = self._safe_float(payload.get("p_set"))
                device_time = self._safe_float(payload.get("t_ms"))
                reading = PressureReading(
                    p1=pressure,
                    p2=None,
                    setpoint=setpoint,
                    raw=raw,
                    t=time.perf_counter(),
                    device_time_ms=device_time,
                )
                self._publish(reading)
                if setpoint is not None:
                    self._handle_setpoint_feedback(setpoint)
            elif kind == "ACK":
                raw_ack = str(event[1])
                match = self.ACK_RE.match(raw_ack)
                if match:
                    try:
                        value = float(match.group(1))
                        self._handle_setpoint_feedback(value)
                    except ValueError:
                        continue

    def _keepalive_loop(self) -> None:
        while not self._keepalive_evt.wait(self._keepalive_interval_s):
            with self._lock:
                port = self._vm_port
                value = self._last_command_value
                if port is None or value is None:
                    continue
                try:
                    port.tx_q.put(f"SET P={value:.1f}")
                except Exception:
                    continue

    def _publish(self, reading: PressureReading) -> None:
        """Push a reading onto the queue, dropping the oldest on overflow."""
        try:
            self._queue.put_nowait(reading)
        except queue.Full:
            try:
                _ = self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(reading)
            except queue.Full:
                pass

    def _handle_setpoint_feedback(self, value: float) -> None:
        """Update internal caches and notify listeners of a new target."""
        numeric = float(value)
        self._last_callback_value = self._notify_setpoint_once(
            numeric, self._last_callback_value
        )

    def _notify_setpoint_once(self, value: float, last_value: Optional[float]) -> Optional[float]:
        if self._setpoint_callback is None:
            return last_value
        if last_value is not None and abs(value - last_value) < 1e-3:
            return last_value
        try:
            self._setpoint_callback(value)
        except Exception:
            return last_value
        return value

    @staticmethod
    def _safe_float(value: object) -> Optional[float]:
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric != numeric:  # NaN check
            return None
        return numeric

    @staticmethod
    def _humanize_serial_error(exc: Optional[Exception]) -> str:
        if exc is None:
            return "unknown error"
        return f"{exc.__class__.__name__}: {exc}"

    @staticmethod
    def _guess_port_owner(device: str) -> Optional[str]:
        """
        Attempt to identify the process currently holding the requested port.

        Returns a short name (e.g. 'Arduino IDE') or None if detection is not
        possible. Requires `psutil` on the host environment.
        """
        if psutil is None:  # pragma: no cover - optional dependency
            return None
        device_lower = device.lower()
        hint = None
        for proc in psutil.process_iter(attrs=["pid", "name", "cmdline"]):
            try:
                open_files = proc.open_files()
            except (psutil.AccessDenied, psutil.ZombieProcess):
                continue
            for opened in open_files:
                if opened.path.lower().startswith(device_lower):
                    hint = proc.info.get("name") or "another application"
                    break
            if hint:
                break
        return hint

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

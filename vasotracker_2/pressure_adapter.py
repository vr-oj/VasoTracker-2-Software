"""
Pressure adapter for VasoTracker 2.

This module encapsulates all serial-port handling for the pressure controller.
It supports legacy `<NN>` framing and the newer CSV telemetry, automatically
detecting the correct protocol during the preflight handshake.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

import serial
from serial.tools import list_ports

try:  # Optional dependency; only used for busy-port diagnostics.
    import psutil
except ImportError:  # pragma: no cover - psutil is optional
    psutil = None


@dataclass
class PressureReading:
    """Structured telemetry emitted by the pressure controller."""

    p1: Optional[float]
    p2: Optional[float]
    setpoint: Optional[float]
    raw: str
    t: float  # monotonic timestamp


class PressureAdapter:
    """
    Threaded adapter that manages serial I/O with the pressure hardware.

    The adapter is re-entrant: calling `connect` repeatedly tears down and
    re-establishes the connection. Readings are published to an internal queue
    so consumers can poll without blocking the UI thread.
    """

    LEGACY_REGEX = re.compile(
        r"<\s*P1:(-?\d+\.?\d*)\s*[,;]\s*P2:(-?\d+\.?\d*)"
        r"(?:\s*[,;]\s*SET:(-?\d+\.?\d*))?\s*>",
        re.IGNORECASE,
    )
    CSV_REGEX = re.compile(
        r"P1:(-?\d+\.?\d*)[,;]\s*P2:(-?\d+\.?\d*)"
        r"(?:[,;]\s*SET:(-?\d+\.?\d*))?",
        re.IGNORECASE,
    )

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
        self._last_setpoint: Optional[float] = None
        self._lock = threading.RLock()
        self.protocol: Optional[str] = None  # "legacy" or "csv"
        self._serial: Optional[serial.Serial] = None
        self._setpoint_callback = setpoint_callback
        self._last_notified_setpoint: Optional[float] = None

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

    # Public API -----------------------------------------------------------------
    def connect(self) -> None:
        """Connect to the pressure controller, auto-detecting the protocol."""
        with self._lock:
            self.close()
            candidates = (
                [self.port] if self.port else [device for device, _ in self.list_serial_ports()]
            )
            if not candidates:
                raise RuntimeError("No serial ports detected.")

            last_error: Optional[Exception] = None
            for device in candidates:
                try:
                    self._serial = self._open_serial(device)
                    self.port = device
                    self._detect_protocol()
                    self._start_reader()
                    return
                except Exception as exc:  # noqa: BLE001 - we want to capture any failure
                    last_error = exc
                    self._serial = None
            raise RuntimeError(
                f"Unable to connect to any serial port ({', '.join(candidates)}). "
                f"Last error: {self._humanize_serial_error(last_error)}"
            )

    def reconnect(self) -> None:
        """Convenience wrapper that simply calls `connect`."""
        self.connect()

    def close(self) -> None:
        """Tear down the serial connection and stop background threads."""
        with self._lock:
            self._stop_reader()
            if self._serial:
                try:
                    self._serial.close()
                except Exception:  # noqa: BLE001 - best effort
                    pass
            self._serial = None
            self.protocol = None

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

    def set_pressure(self, target_mm_hg: float) -> None:
        """
        Update the pressure setpoint. Both command dialects are sent so either
        protocol will respond.
        """
        self._last_setpoint = float(target_mm_hg)
        self._last_notified_setpoint = float(target_mm_hg)
        integer = int(round(target_mm_hg))
        commands = (
            f"SET:{target_mm_hg:.2f}",
            f"<{integer}>",
            f"P {integer}",
            f"P:{integer}",
            f"P={integer}",
            f"SET P {integer}",
            f"SET_PRESSURE {integer}",
            f"sp {integer}",
            f"SP {integer}",
        )
        with self._lock:
            if not self._serial:
                return
            for command in commands:
                payload = f"{command}\n".encode("ascii", errors="ignore")
                try:
                    self._serial.write(payload)
                except Exception:  # noqa: BLE001 - ignore transient write failures
                    continue

    # Internal helpers -----------------------------------------------------------
    def _open_serial(self, device: str) -> serial.Serial:
        """Open the serial port with sane defaults."""
        kwargs = dict(
            baudrate=self.baud,
            timeout=self.timeout,
            write_timeout=0.5,
        )
        try:
            return serial.Serial(device, **kwargs)
        except PermissionError as exc:
            owner = self._guess_port_owner(device)
            if owner:
                raise PermissionError(
                    f"Port {device} appears busy (in use by {owner})."
                ) from exc
            raise

    def _detect_protocol(self) -> None:
        """Probe the connected device to determine telemetry format."""
        assert self._serial is not None
        try:
            self._serial.reset_input_buffer()
        except Exception:
            pass

        try:
            self._serial.write(b"?\n")
        except Exception:
            pass

        deadline = time.time() + 1.5
        while time.time() < deadline:
            line = self._readline()
            if not line:
                continue
            if self.LEGACY_REGEX.search(line):
                self.protocol = "legacy"
                return
            if self.CSV_REGEX.search(line):
                self.protocol = "csv"
                return

        # Default to legacy if no conclusive telemetry is observed.
        self.protocol = "legacy"

    def _start_reader(self) -> None:
        """Launch the background telemetry reader thread."""
        self._stop_event.clear()
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _stop_reader(self) -> None:
        """Stop the background reader thread."""
        self._stop_event.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=0.5)
        self._reader_thread = None
        with self._queue.mutex:
            self._queue.queue.clear()

    def _reader_loop(self) -> None:
        """Continuously read telemetry lines and publish structured readings."""
        assert self._serial is not None
        while not self._stop_event.is_set():
            line = self._readline()
            if not line:
                continue
            timestamp = time.perf_counter()
            match = self.CSV_REGEX.search(line) or self.LEGACY_REGEX.search(line)
            p1 = p2 = setpoint = None
            explicit_setpoint: Optional[float] = None
            if match:
                try:
                    p1 = float(match.group(1))
                except (TypeError, ValueError):
                    p1 = None
                try:
                    p2 = float(match.group(2))
                except (TypeError, ValueError):
                    p2 = None
                if match.lastindex and match.group(match.lastindex):
                    try:
                        explicit_setpoint = float(match.group(match.lastindex))
                    except ValueError:
                        explicit_setpoint = None
                if explicit_setpoint is not None:
                    setpoint = explicit_setpoint
            else:
                explicit_setpoint = self._extract_setpoint_echo(line)
                if explicit_setpoint is not None:
                    setpoint = explicit_setpoint
            if setpoint is None:
                setpoint = self._last_setpoint
            reading = PressureReading(p1=p1, p2=p2, setpoint=setpoint, raw=line, t=timestamp)
            if explicit_setpoint is not None:
                self._handle_setpoint_feedback(explicit_setpoint)
            self._publish(reading)

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

    def _extract_setpoint_echo(self, line: str) -> Optional[float]:
        """Parse device-emitted setpoint echoes like 'SP:60.0'."""
        text = line.strip()
        if not text:
            return None
        upper = text.upper()
        prefix = None
        for candidate in ("SP:", "SETPOINT:", "TARGET:"):
            if upper.startswith(candidate):
                prefix = candidate
                break
        if prefix is None:
            return None
        try:
            return float(text[len(prefix) :].strip())
        except ValueError:
            return None

    def _handle_setpoint_feedback(self, value: float) -> None:
        """Update internal caches and notify listeners of a new target."""
        numeric = float(value)
        self._last_setpoint = numeric
        if (
            self._last_notified_setpoint is not None
            and abs(self._last_notified_setpoint - numeric) < 1e-3
        ):
            return
        self._last_notified_setpoint = numeric
        if not self._setpoint_callback:
            return
        try:
            self._setpoint_callback(numeric)
        except Exception:
            pass

    def _readline(self) -> str:
        """Best-effort UTF-8 decode of a single telemetry line."""
        with self._lock:
            if not self._serial:
                return ""
            try:
                data = self._serial.readline() or b""
            except Exception:
                return ""
        return data.decode(errors="replace").strip()

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


# #################################################
# # VasoTracker 2 - Blood Vessel Diameter Measurement Software
# #
# # Author: Calum Wilson, Matthew D Lee, and Chris Osborne
# # License: BSD 3-Clause License (See main file for details)
# # Website: www.vasostracker.com
# #
# #################################################

"""
Serial client for controlling a VasoMoto Arduino sketch without modifying it.

The client discovers the Arduino, streams telemetry lines, and exposes a
`set_pressure(mmHg)` helper that tries a handful of common command formats
until one is accepted.  It can be imported directly by the VasoTracker app or
used as a small standalone CLI for manual testing.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:  # pragma: no cover - runtime dependency
    raise ImportError(
        "pyserial is required for Arduino communication. Install with `pip install pyserial`."
    ) from exc

# Common fallback baud rates used by Arduino sketches.
COMMON_BAUDS: List[int] = [115200, 57600, 38400, 19200, 9600]

# Command templates that cover the most frequently used VasoMoto formats.
COMMAND_STYLES: List[str] = [
    "<{v}>\n",
    "P {v}\n",
    "P:{v}\n",
    "P={v}\n",
    "SET P {v}\n",
    "SET_PRESSURE {v}\n",
    "p {v}\n",
    "sp {v}\n",
    "SP {v}\n",
]


@dataclass
class Telemetry:
    """Represents a single telemetry update from the Arduino."""

    raw_line: str
    fields: Dict[str, Any]


class VasoMotoClient:
    """
    High-level serial helper that auto-detects the Arduino and streams data.

    Parameters
    ----------
    port:
        Optional serial device name. Leave unset to auto-detect.
    baud:
        Optional baud rate. If not provided the client cycles through
        `COMMON_BAUDS` until it sees printable telemetry.
    read_timeout:
        Timeout in seconds for each serial read call.
    eol:
        End-of-line bytes passed to `read_until`. Defaults to '\\n'.
    max_pressure_mmHg:
        Clamp applied to requested pressures for safety.
    device_units_scale/device_units_offset:
        Optional scale/offset applied before sending commands when the sketch
        expects raw DAC values instead of mmHg.
    """

    def __init__(
        self,
        port: Optional[str] = None,
        baud: Optional[int] = None,
        read_timeout: float = 0.3,
        eol: bytes = b"\n",
        max_pressure_mmHg: float = 200.0,
        device_units_scale: float = 1.0,
        device_units_offset: float = 0.0,
    ) -> None:
        self.port = port
        self.baud = baud
        self.read_timeout = read_timeout
        self.eol = eol
        self.max_pressure = max_pressure_mmHg
        self.scale = device_units_scale
        self.offset = device_units_offset
        self.ser: Optional[serial.SerialBase] = None
        self._queue: "queue.Queue[Telemetry]" = queue.Queue(maxsize=1000)
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.last_fields: Dict[str, Any] = {}
        self.last_line: str = ""
        self.connected = False

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------
    @staticmethod
    def discover_ports() -> List[Tuple[str, str]]:
        """Return a list of available serial ports."""
        ports = []
        for port in list_ports.comports():
            info = f"{port.manufacturer or ''} {port.description or ''}".strip()
            ports.append((port.device, info))
        return ports

    def _try_open(self, port: str, baud: int) -> bool:
        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baud,
                timeout=self.read_timeout,
                write_timeout=0.5,
                exclusive=True,
            )
            # Allow bootloaders that toggle reset on DTR to finish.
            time.sleep(0.6)
            return True
        except Exception:
            self.ser = None
            return False

    def connect(self) -> None:
        """Attempt to open the serial connection, cycling ports/bauds as needed."""
        ports = self.discover_ports() if self.port is None else [(self.port, "")]
        if not ports:
            raise RuntimeError("No serial ports found. Is the Arduino connected?")

        for device, _ in ports:
            baud_candidates = [self.baud] if self.baud else COMMON_BAUDS
            for baud in baud_candidates:
                if baud is None:
                    continue
                if not self._try_open(device, baud):
                    continue
                if self._looks_alive():
                    self.port = device
                    self.baud = baud
                    self.connected = True
                    self._start_reader()
                    return
                self._cleanup_serial()

        raise RuntimeError("Could not connect: tried available ports and baud rates.")

    def _cleanup_serial(self) -> None:
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def _looks_alive(self) -> bool:
        """Check whether opening the port yields readable telemetry lines."""
        if not self.ser:
            return False
        lines = 0
        start = time.time()
        while time.time() - start < 2.0:
            line = self._readline()
            if line:
                lines += 1
                if lines >= 2:
                    return True
        return False

    def _start_reader(self) -> None:
        self._stop_event.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    # ------------------------------------------------------------------
    # Reading and parsing
    # ------------------------------------------------------------------
    def _readline(self) -> Optional[str]:
        if not self.ser:
            return None
        try:
            data = self.ser.read_until(self.eol)
        except Exception:
            return None
        if not data:
            return None
        try:
            return data.decode(errors="replace").strip()
        except Exception:
            return None

    def _parse_fields(self, line: str) -> Dict[str, Any]:
        stripped = line.strip()

        # JSON payloads
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

        # key:value or key=value pairs
        token_source = stripped
        if token_source.startswith("<") and token_source.endswith(">"):
            token_source = token_source[1:-1]
        token_source = token_source.replace(";", " ").replace("|", " ")
        tokens = re.split(r"[,\t ]+", token_source)
        kv: Dict[str, Any] = {}
        for token in tokens:
            if ":" in token:
                key, value = token.split(":", 1)
            elif "=" in token:
                key, value = token.split("=", 1)
            else:
                continue
            key = key.strip().lower()
            value = value.strip()
            if not key:
                continue
            try:
                kv[key] = float(value)
            except ValueError:
                kv[key] = value
        if kv:
            return kv

        # CSV-like payloads fallback
        csv_tokens = [token for token in tokens if token]
        if csv_tokens:
            parsed: Dict[str, Any] = {}
            for index, token in enumerate(csv_tokens):
                try:
                    parsed[f"f{index}"] = float(token)
                except ValueError:
                    parsed[f"f{index}"] = token
            return parsed

        return {}

    def _reader_loop(self) -> None:
        while not self._stop_event.is_set():
            line = self._readline()
            if not line:
                continue
            fields = self._parse_fields(line)
            self.last_line = line
            if fields:
                self.last_fields = fields
            telemetry = Telemetry(raw_line=line, fields=fields)
            try:
                self._queue.put_nowait(telemetry)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(telemetry)
                except Exception:
                    pass

    def get_latest(self, timeout: float = 0.0) -> Optional[Telemetry]:
        """Return the most recent telemetry line (blocking up to `timeout`)."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Command helpers
    # ------------------------------------------------------------------
    def _send_line(self, text: str) -> None:
        if not self.ser:
            raise RuntimeError("Serial port is not open.")
        self.ser.write(text.encode())

    def send_raw(self, payload: str) -> None:
        """
        Send a raw command string to the device. Automatically appends a trailing
        newline if one is not present so sketches that expect line-based commands
        continue to work as expected.
        """
        if not payload.endswith("\n"):
            payload += "\n"
        self._send_line(payload)

    def _to_device_units(self, value_mmHg: float) -> float:
        return value_mmHg * self.scale + self.offset

    def set_pressure(
        self,
        target_mmHg: float,
        *,
        style: str = "auto",
        wait_for_effect: float = 1.0,
    ) -> Tuple[bool, str]:
        """
        Try to set pressure without knowing the sketch's exact command format.

        Returns (success, message). Success is inferred from ACK-like telemetry
        or observing the reported pressure move toward the requested setpoint.
        """
        if target_mmHg < 0:
            target_mmHg = 0.0
        if target_mmHg > self.max_pressure:
            target_mmHg = self.max_pressure

        device_value = self._to_device_units(target_mmHg)
        baseline = self._estimate_pressure(self.last_fields)

        styles = COMMAND_STYLES if style == "auto" else [style]
        attempts = []

        for template in styles:
            payload = template.format(v=int(round(device_value)))
            attempts.append(payload.strip())
            try:
                self._send_line(payload)
            except Exception:
                continue

            ack_received = False
            pressure_changed = False
            start_time = time.time()

            while time.time() - start_time < wait_for_effect:
                telemetry = self.get_latest(timeout=wait_for_effect)
                if not telemetry:
                    continue
                lowercase_line = telemetry.raw_line.lower()
                if any(
                    token in lowercase_line for token in ("ack", "ok", "set", "pressure", "target")
                ):
                    ack_received = True
                estimate = self._estimate_pressure(telemetry.fields)
                if estimate is not None and baseline is not None:
                    if abs(estimate - target_mmHg) <= 5.0 or abs(estimate - baseline) >= 3.0:
                        pressure_changed = True
                if ack_received or pressure_changed:
                    return True, f"Command accepted via '{payload.strip()}'"

        return False, f"No obvious ACK/change detected. Tried: {attempts}"

    def _estimate_pressure(self, fields: Dict[str, Any]) -> Optional[float]:
        """Best-effort attempt to pull a pressure value out of parsed telemetry."""
        if not fields:
            return None

        for key, value in fields.items():
            if any(tag in key for tag in ("press", "mmhg", "p_mm", "p")):
                try:
                    return float(value)
                except Exception:
                    continue

        for key in sorted(fields.keys()):
            value = fields[key]
            if isinstance(value, (int, float)):
                return float(value)

        return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._stop_event.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=0.3)
            self._reader_thread = None
        self._cleanup_serial()
        self.connected = False


def _cli() -> None:
    """Simple command-line interface for quick manual testing."""
    import argparse

    parser = argparse.ArgumentParser(description="VasoMoto Arduino serial client")
    parser.add_argument("--port", default=None, help="Serial port name (e.g. COM3, /dev/ttyACM0)")
    parser.add_argument("--baud", type=int, default=None, help="Baud rate. Auto-detected by default.")
    parser.add_argument("--set", type=float, default=None, help="Target pressure in mmHg.")
    args = parser.parse_args()

    print("Available ports:")
    for device, description in VasoMotoClient.discover_ports():
        print(f"  {device:>18}  {description}")

    client = VasoMotoClient(port=args.port, baud=args.baud)
    client.connect()
    print(f"Connected on {client.port} @ {client.baud}")

    if args.set is not None:
        ok, msg = client.set_pressure(args.set)
        print("set_pressure:", ok, msg)

    print("Reading telemetry (Ctrl+C to exit)...")
    try:
        while True:
            telemetry = client.get_latest(timeout=1.0)
            if telemetry:
                print(telemetry.raw_line)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


if __name__ == "__main__":
    _cli()

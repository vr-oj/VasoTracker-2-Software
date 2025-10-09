# #################################################
# # VasoTracker 2 - Blood Vessel Diameter Measurement Software
# #
# # Author: Calum Wilson, Matthew D Lee, and Chris Osborne
# # License: BSD 3-Clause License (See main file for details)
# # Website: www.vasostracker.com
# #
# #################################################

"""
Cross-platform Arduino serial helper used by the pressure device layer.

The previous implementation relied on Windows-specific discovery logic and
device-specific framing. This module now provides a lightweight wrapper over
`pyserial` so that higher level code can speak the VasoMoto text protocol without
pulling in UI dependencies or platform checks.
"""

from __future__ import annotations

from contextlib import contextmanager
import re
from typing import Generator, Optional

try:
    import serial
except ImportError as exc:  # pragma: no cover - handled at runtime
    raise ImportError(
        "pyserial is required for Arduino communication. Install with `pip install pyserial`."
    ) from exc


class Arduino:
    """
    Minimal serial wrapper compatible with the `ArduinoPressureDevice`.

    Parameters
    ----------
    port:
        Serial port string (e.g. 'COM5', '/dev/ttyACM0', or 'loop://').
    baud:
        Baud rate used by the Arduino sketch (default: 115200).
    timeout:
        Default read timeout in seconds (overridden per-call by `readline`).
    auto_connect:
        If True, open the serial port immediately.
    """

    def __init__(
        self,
        port: str,
        baud: int = 115_200,
        timeout: float = 0.1,
        auto_connect: bool = True,
    ) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.serial: Optional[serial.SerialBase] = None
        self._last_error: Optional[Exception] = None
        self._error_notified: bool = False
        self._last_set_mmHg: Optional[float] = None

        if auto_connect:
            self.connect()

    def connect(self) -> None:
        """Open the serial port if it is not already open."""
        if self.serial and self.serial.is_open:
            return

        try:
            if self.port.startswith(("loop://", "spy://", "rfc2217://")):
                self.serial = serial.serial_for_url(
                    self.port, baudrate=self.baud, timeout=self.timeout
                )
            else:
                self.serial = serial.Serial(
                    self.port, baudrate=self.baud, timeout=self.timeout
                )
        except Exception as exc:
            self._handle_connection_failure(exc)
            return

        self._last_error = None
        self._error_notified = False

        # Flush any stale bytes to start with a clean buffer.
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

    def close(self) -> None:
        if self.serial and self.serial.is_open:
            self.serial.close()

    def __enter__(self) -> "Arduino":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @property
    def is_connected(self) -> bool:
        return bool(self.serial and self.serial.is_open)

    @property
    def last_error(self) -> Optional[Exception]:
        return self._last_error

    @property
    def error_notified(self) -> bool:
        return self._error_notified

    def sendData(self, text: str) -> None:
        """
        Send ASCII text to the Arduino and bridge modern commands to the
        legacy angle-bracket protocol expected by older firmware.
        """
        if not self.serial or not self.serial.is_open:
            return

        if not text.endswith("\n"):
            text += "\n"

        try:
            payload = text.encode("utf-8")
            self.serial.write(payload)
            self.serial.flush()
        except Exception as exc:
            print("Serial write error:", exc)
            return

        match = re.search(
            r"(?:^|\b)(?:SET|SP|P|PRESSURE)\s*[:= ]\s*(-?\d+(?:\.\d+)?)",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return

        try:
            value = float(match.group(1))
            legacy = f"<{int(round(value))}>"
            self.serial.write(legacy.encode("utf-8"))
            self.serial.flush()
            self._last_set_mmHg = value
        except Exception as exc:
            print("Legacy bridge write error:", exc)

    def readline(self, timeout: float = 0.1) -> Optional[str]:
        """
        Read a single line from the serial connection.

        Parameters
        ----------
        timeout:
            Temporary timeout override in seconds.

        Returns
        -------
        str | None
            The decoded ASCII line without trailing newline, or None if nothing
            was received before the timeout.
        """
        if not self.serial or not self.serial.is_open:
            raise RuntimeError("Serial port is not open. Call `connect()` first.")

        previous_timeout = self.serial.timeout
        self.serial.timeout = timeout
        try:
            line = self.serial.readline()
        finally:
            self.serial.timeout = previous_timeout

        if not line:
            return None
        return line.decode("ascii", errors="ignore")

    def _handle_connection_failure(self, exc: Exception) -> None:
        self.serial = None
        self._last_error = exc
        message = (
            f"Unable to open serial port '{self.port}':\n{exc}\n\n"
            "Tip: close Arduino Serial Monitor or any app using the port."
        )
        try:
            # Lazy import keeps this utility usable in headless contexts.
            from tkinter import messagebox

            messagebox.showerror("Arduino connection failed", message)
            self._error_notified = True
        except Exception:
            print("Arduino connection failed:", message)
            self._error_notified = True


@contextmanager
def arduino_session(
    port: str, baud: int = 115_200, timeout: float = 0.1
) -> Generator[Arduino, None, None]:
    """Convenience context manager for short-lived interactions."""
    device = Arduino(port=port, baud=baud, timeout=timeout, auto_connect=True)
    try:
        yield device
    finally:
        device.close()

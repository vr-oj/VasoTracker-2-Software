"""
Low-level serial port helper for the VasoMoto pressure controller.

This module encapsulates the raw serial connection, exposing a pair of
background threads that handle RX and TX independently.  Consumers push
outbound commands via `tx_q` and receive parsed telemetry/acks through
`rx_q`.

The device speaks a very small ASCII protocol:

    Host -> Device:  "SET P=<mmHg>\n"
    Device -> Host:  "ACK SET P=<int> T=<ms>\n"
    Device -> Host:  "DATA T=<ms> P=<float> P_SET=<int>\n"

Only one process should hold the serial handle at a time, therefore the
port is opened with `exclusive=True` when supported by the platform.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Dict, Tuple, Union, Any

import serial


DATA_PREFIX = "DATA"
ACK_PREFIX = "ACK"


class VasoMotorPort:
    """
    Thin wrapper around `serial.Serial` with dedicated RX/TX workers.

    Parameters
    ----------
    port:
        Serial device string (e.g. 'COM5' or '/dev/ttyACM0').
    baud:
        Baud rate used when talking to the device (defaults to 115200).
    read_timeout:
        Timeout in seconds for each serial read. Small values keep the
        RX loop responsive to shutdown requests.
    write_timeout:
        Timeout in seconds for write operations.
    queue_size:
        Maximum items the RX queue will buffer before dropping the oldest.
    """

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        *,
        read_timeout: float = 0.05,
        write_timeout: float = 0.2,
        queue_size: int = 1000,
    ) -> None:
        self.ser = serial.Serial(
            port,
            baudrate=baud,
            timeout=read_timeout,
            write_timeout=write_timeout,
            exclusive=True,
        )
        try:
            self.ser.setDTR(False)
            self.ser.setRTS(False)
        except Exception:
            pass
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        try:
            self.ser.reset_output_buffer()
        except Exception:
            pass

        self.rx_q: "queue.Queue[Union[Tuple[str, Dict[str, Any]], Tuple[str, str]]]" = queue.Queue(maxsize=queue_size)
        self.tx_q: "queue.Queue[str]" = queue.Queue()
        self.state: Dict[str, float] = {"t_ms": float("nan"), "p": float("nan"), "p_set": float("nan")}
        self.running = True

        self._rx_thread = threading.Thread(target=self._rx, daemon=True)
        self._tx_thread = threading.Thread(target=self._tx, daemon=True)
        self._rx_thread.start()
        self._tx_thread.start()

    # ------------------------------------------------------------------ #
    # Serial worker threads                                              #
    # ------------------------------------------------------------------ #
    def _rx(self) -> None:
        buffer = b""
        while self.running:
            try:
                chunk = self.ser.read(512)
            except Exception:
                break
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if text.startswith(DATA_PREFIX):
                    parsed = self._parse_data(text)
                    if parsed:
                        # Keep the most recent values cached in case callers poll state.
                        self.state.update(parsed)
                        payload = dict(self.state)
                        payload["raw"] = text
                        self._publish(("DATA", payload))
                elif text.startswith(ACK_PREFIX):
                    self._publish(("ACK", text))

    def _tx(self) -> None:
        while self.running:
            try:
                cmd = self.tx_q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.ser.write((cmd + "\n").encode("utf-8"))
                self.ser.flush()
            except Exception:
                continue

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    def _publish(self, item: Union[Tuple[str, Dict[str, Any]], Tuple[str, str]]) -> None:
        try:
            self.rx_q.put_nowait(item)
        except queue.Full:
            try:
                _ = self.rx_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.rx_q.put_nowait(item)
            except queue.Full:
                pass

    @staticmethod
    def _parse_data(text: str) -> Union[Dict[str, Any], None]:
        parsed: Dict[str, Any] = {}
        parts = text.split()
        if len(parts) < 4:
            return None
        for token in parts[1:]:
            if "=" not in token:
                continue
            key, raw = token.split("=", 1)
            key = key.strip().lower()
            raw = raw.strip()
            if key == "t":
                try:
                    parsed["t_ms"] = float(raw)
                except ValueError:
                    continue
            elif key == "p":
                if raw == "NaN":
                    parsed["p"] = float("nan")
                else:
                    try:
                        parsed["p"] = float(raw)
                    except ValueError:
                        continue
            elif key == "p_set":
                if raw == "NaN":
                    parsed["p_set"] = float("nan")
                else:
                    try:
                        parsed["p_set"] = float(raw)
                    except ValueError:
                        continue
        return parsed if parsed else None

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def set_pressure(self, mmhg: float) -> None:
        """Queue a new pressure target in mmHg."""
        self.tx_q.put(f"SET P={float(mmhg):.1f}")

    def close(self) -> None:
        """Stop worker threads and close the serial port."""
        self.running = False
        time.sleep(0.2)  # Allow threads to exit gracefully.
        try:
            self.ser.close()
        except Exception:
            pass

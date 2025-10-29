from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Optional

from pathlib import Path
import sys
import types

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _StubVideoCapture:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._open = True

    def isOpened(self) -> bool:
        return True

    def read(self) -> tuple[bool, None]:
        return False, None

    def set(self, *args: Any, **kwargs: Any) -> None:
        return None

    def release(self) -> None:
        self._open = False


class _StubVideoWriter:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._open = True

    def isOpened(self) -> bool:
        return True

    def write(self, *args: Any, **kwargs: Any) -> None:
        return None

    def release(self) -> None:
        self._open = False


if "cv2" not in sys.modules:
    sys.modules["cv2"] = types.SimpleNamespace(
        VideoCapture=_StubVideoCapture,
        VideoWriter=_StubVideoWriter,
        VideoWriter_fourcc=lambda *args: 0,
        resize=lambda image, size: image,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
    )


class _StubSerial:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.timeout = kwargs.get("timeout", 0.1)

    def write(self, payload: bytes) -> None:
        return None

    def close(self) -> None:
        return None

    def readline(self) -> bytes:
        return b""

    def reset_input_buffer(self) -> None:
        return None

    def reset_output_buffer(self) -> None:
        return None


if "serial" not in sys.modules:
    list_ports = types.SimpleNamespace(comports=lambda: [])
    serial_module = types.SimpleNamespace(
        Serial=_StubSerial,
        SerialBase=_StubSerial,
        SerialException=Exception,
        tools=types.SimpleNamespace(list_ports=list_ports),
    )
    sys.modules["serial"] = serial_module
    sys.modules["serial.tools"] = serial_module.tools
    sys.modules["serial.tools.list_ports"] = list_ports

import pytest

from vasotracker_2 import session_controller as sc_module
from vasotracker_2 import setpoint_bus
from vasotracker_2.pressure_adapter import PressureReading
from vasotracker_2.session_controller import SessionConfig, SessionController


class DummyCameraAdapter:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._frames: queue.Queue[Any] = queue.Queue()

    def open(self) -> None:
        return None

    def read(self, timeout: float = 0.0) -> Any:
        return None

    def close(self) -> None:
        return None


class DummyWriter:
    def __init__(
        self,
        folder: str,
        frame_queue_size: int = 500,
        telemetry_queue_size: int = 2000,
    ) -> None:
        self.folder = folder
        self.telemetry_rows: list[list[Optional[float]]] = []

    def start(self, *args: Any, **kwargs: Any) -> None:
        return None

    def push_frame(self, *args: Any, **kwargs: Any) -> None:
        return None

    def push_telemetry(
        self,
        t: float,
        frame_no: Optional[int],
        p1: Optional[float],
        p2: Optional[float],
        setpoint: Optional[float],
        note: str = "",
    ) -> None:
        self.telemetry_rows.append([t, frame_no, p1, p2, setpoint, note])

    def close(self) -> None:
        return None

    def write_metadata(self, metadata: dict, filename: str = "metadata.json") -> None:
        return None


class DummyPressureAdapter:
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
        self._queue: queue.Queue[PressureReading] = queue.Queue()
        self.setpoint_callback = setpoint_callback
        self.last_command: Optional[float] = None
        self.protocol: Optional[str] = "csv"
        self.queries: list[str] = []

    def connect(self) -> None:
        return None

    def close(self) -> None:
        return None

    def read(self, timeout: float = 0.0) -> Optional[PressureReading]:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def _set_target(self, target_mm_hg: float) -> None:
        self.last_command = float(target_mm_hg)

    def query_setpoint(self) -> None:
        self.queries.append("query")

    def queue_reading(self, reading: PressureReading) -> None:
        self._queue.put(reading)

    def emit_setpoint(self, value: float) -> None:
        if self.setpoint_callback:
            self.setpoint_callback(value)


def _wait_for_rows(writer: DummyWriter, expected: int, timeout: float = 1.0) -> None:
    deadline = time.time() + timeout
    while len(writer.telemetry_rows) < expected:
        if time.time() > deadline:
            raise AssertionError("Timed out waiting for telemetry rows.")
        time.sleep(0.01)


def test_setpoint_logged_each_step(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sc_module, "CameraAdapter", DummyCameraAdapter)
    monkeypatch.setattr(sc_module, "Writer", DummyWriter)
    monkeypatch.setattr(sc_module, "PressureAdapter", DummyPressureAdapter)

    cfg = SessionConfig(base_folder=str(tmp_path))
    session = SessionController(cfg)
    writer: DummyWriter = session.writer  # type: ignore[assignment]
    pressure: DummyPressureAdapter = session.pressure  # type: ignore[assignment]

    session._pump_stop.clear()
    pump = threading.Thread(target=session._pump_loop, daemon=True)
    pump.start()

    try:
        commanded = [20, 40, 60, 80, 100]
        for index, value in enumerate(commanded, start=1):
            session.apply_setpoint(value)
            reading = PressureReading(
                p1=42.0,
                p2=43.0,
                setpoint=None,
                raw=f"P1:{value},P2:{value}",
                t=time.perf_counter(),
            )
            pressure.queue_reading(reading)
            _wait_for_rows(writer, index)

        # Simulate device-driven change and telemetry echo.
        pressure.emit_setpoint(65.0)
        assert session.current_target_mmHg == pytest.approx(65.0, abs=1e-6)

        reading = PressureReading(
            p1=46.0,
            p2=47.0,
            setpoint=65.0,
            raw="SP:65.0",
            t=time.perf_counter(),
        )
        pressure.queue_reading(reading)
        _wait_for_rows(writer, len(commanded) + 1)

        assert setpoint_bus.notify_setpoint(90.0, "test_bus") is True
        assert session.current_target_mmHg == pytest.approx(90.0, abs=1e-6)
    finally:
        session._pump_stop.set()
        pump.join(timeout=1.0)
        setpoint_bus.clear_setpoint_handler(session._ingest_external_setpoint)  # type: ignore[attr-defined]
        setpoint_bus.clear_setpoint_query(session._handle_refresh_request)  # type: ignore[attr-defined]

    recorded = [row[4] for row in writer.telemetry_rows]
    assert recorded[: len(commanded)] == commanded
    assert recorded[-1] == pytest.approx(65.0, abs=1e-6)
    assert len(set(recorded)) == len(commanded) + 1


def test_echo_last_writer_wins(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sc_module, "CameraAdapter", DummyCameraAdapter)
    monkeypatch.setattr(sc_module, "Writer", DummyWriter)
    monkeypatch.setattr(sc_module, "PressureAdapter", DummyPressureAdapter)

    cfg = SessionConfig(base_folder=str(tmp_path))
    session = SessionController(cfg)
    pressure: DummyPressureAdapter = session.pressure  # type: ignore[assignment]

    session.apply_setpoint(60.0, source="GUI")
    pressure.emit_setpoint(59.8)
    assert session.current_target_mmHg == pytest.approx(59.8, abs=1e-6)

    setpoint_bus.clear_setpoint_handler(session._ingest_external_setpoint)  # type: ignore[attr-defined]
    setpoint_bus.clear_setpoint_query(session._handle_refresh_request)  # type: ignore[attr-defined]


def test_setpoint_jitter_debounced(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sc_module, "CameraAdapter", DummyCameraAdapter)
    monkeypatch.setattr(sc_module, "Writer", DummyWriter)
    monkeypatch.setattr(sc_module, "PressureAdapter", DummyPressureAdapter)

    cfg = SessionConfig(base_folder=str(tmp_path))
    session = SessionController(cfg)
    pressure: DummyPressureAdapter = session.pressure  # type: ignore[assignment]

    session.apply_setpoint(40.0, source="GUI")
    pressure.emit_setpoint(40.0)
    baseline_events = len(session._setpoint_events)

    for delta in (0.2, -0.1, 0.15):
        pressure.emit_setpoint(40.0 + delta)

    assert len(session._setpoint_events) == baseline_events

    setpoint_bus.clear_setpoint_handler(session._ingest_external_setpoint)  # type: ignore[attr-defined]
    setpoint_bus.clear_setpoint_query(session._handle_refresh_request)  # type: ignore[attr-defined]


def test_notify_without_session_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sc_module, "CameraAdapter", DummyCameraAdapter)
    monkeypatch.setattr(sc_module, "Writer", DummyWriter)
    monkeypatch.setattr(sc_module, "PressureAdapter", DummyPressureAdapter)

    cfg = SessionConfig(base_folder=str(tmp_path))
    session = SessionController(cfg)
    setpoint_bus.clear_setpoint_handler(session._ingest_external_setpoint)  # type: ignore[attr-defined]
    setpoint_bus.clear_setpoint_query(session._handle_refresh_request)  # type: ignore[attr-defined]

    assert setpoint_bus.notify_setpoint(55.0, "test") is False

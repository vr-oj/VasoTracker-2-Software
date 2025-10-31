"""
High-level session controller for VasoTracker 2.

This orchestrates camera capture, pressure telemetry, disk writing, and scripted
pressure steps. The design keeps hardware work off the UI thread and exposes a
simple state machine that UI layers (Tkinter, PyQt, CLI) can consume.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections import deque
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

try:  # Support running as a script or imported package.
    from .camera_adapter import CameraAdapter
    from .pressure_adapter import PressureAdapter
    from .steps_engine import Step, StepsEngine
    from .writer import Writer
    from .setpoint_bus import (
        register_setpoint_handler,
        clear_setpoint_handler,
        broadcast_setpoint,
        register_setpoint_query,
        clear_setpoint_query,
    )
except ImportError as exc:  # pragma: no cover - fallback for direct script execution
    if __package__:
        raise
    from camera_adapter import CameraAdapter
    from pressure_adapter import PressureAdapter
    from steps_engine import Step, StepsEngine
    from writer import Writer
    from setpoint_bus import (
        register_setpoint_handler,
        clear_setpoint_handler,
        broadcast_setpoint,
        register_setpoint_query,
        clear_setpoint_query,
    )

logger = logging.getLogger(__name__)


class SessionState(Enum):
    IDLE = auto()
    PREFLIGHT = auto()
    READY = auto()
    RECORDING = auto()
    PAUSED = auto()
    STOPPING = auto()


@dataclass
class SessionConfig:
    base_folder: str
    animal_id: str = ""
    vessel_id: str = ""
    operator: str = ""
    camera_index: int = 0
    camera_size: Optional[Tuple[int, int]] = None
    camera_fps: Optional[int] = None
    serial_port: Optional[str] = None
    serial_baud: int = 115200
    acquisition_profile: str = ""
    hardware_profile: str = ""
    pressure_profile: str = ""
    extra_metadata: Dict[str, str] = field(default_factory=dict)


class SessionController:
    """Central orchestrator coordinating camera, pressure, and disk writing."""

    PREVIEW_TIMEOUT = 1.0
    PREFLIGHT_TEST_SECONDS = 3.0

    def __init__(self, cfg: SessionConfig) -> None:
        self.cfg = cfg
        self.state = SessionState.IDLE

        self.session_folder = self._create_session_folder()
        self._frame_size: Optional[Tuple[int, int]] = None
        self._preflight_report: Dict[str, object] = {}
        self._setpoint_lock = threading.Lock()
        self._current_target_mmHg: float = 0.0
        self._setpoint_events: Deque[Tuple[float, str, float]] = deque(maxlen=512)
        self._last_gui_ts: float = 0.0
        self._last_echo_ts: float = 0.0
        self._last_query_ts: float = 0.0
        self._last_command_value: Optional[float] = None
        self._debounce_threshold = 0.5
        self._debounce_window_s = 1.0
        self._echo_watchdog_s = 2.5

        self.camera = CameraAdapter(
            index=cfg.camera_index,
            size=cfg.camera_size,
            fps=cfg.camera_fps,
        )
        self.pressure = PressureAdapter(
            port=cfg.serial_port,
            baud=cfg.serial_baud,
            setpoint_callback=self._on_setpoint_echo,
        )
        self.writer = Writer(str(self.session_folder))
        self.steps_engine = StepsEngine(self.apply_setpoint, on_step_start=self._on_step_start)

        self._pump_thread: Optional[threading.Thread] = None
        self._pump_stop = threading.Event()

        register_setpoint_handler(self._ingest_external_setpoint)
        register_setpoint_query(self._handle_refresh_request)
        self._record_setpoint_event(self._current_target_mmHg, "init", timestamp=time.time())

        self._update_metadata_file()

    # Public API -----------------------------------------------------------------
    def preflight(self) -> Dict[str, object]:
        """
        Run the preflight sequence.

        Returns a dictionary with details about the hardware and disk checks.
        Raises on failure.
        """
        if self.state not in (SessionState.IDLE, SessionState.READY):
            raise RuntimeError(f"Cannot run preflight while state={self.state.name}")

        self.state = SessionState.PREFLIGHT
        report: Dict[str, object] = {}
        try:
            report["camera"] = self._preflight_camera()
            report["pressure"] = self._preflight_pressure()
            report["disk"] = self._preflight_disk_writer()
            self._preflight_report = report
            self.state = SessionState.READY
            self._update_metadata_file()
            return report
        except Exception:
            # Reset hardware before re-raising to keep future retries clean.
            self._safe_shutdown_hardware()
            self.state = SessionState.IDLE
            raise

    def start_recording(self, video_name: str = "video.avi", csv_name: str = "telemetry.csv") -> None:
        """Start streaming frames/telemetry to disk and move to RECORDING state."""
        if self.state != SessionState.READY:
            raise RuntimeError("Session is not ready. Run preflight() before start_recording().")

        self.writer.start(
            video_name=video_name,
            csv_name=csv_name,
            fps_hint=self.cfg.camera_fps or 30,
            size=self._frame_size,
        )

        self._issue_setpoint_query("start_recording")

        self._pump_stop.clear()
        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump_thread.start()

        self.state = SessionState.RECORDING
        self._update_metadata_file()

    def stop_recording(self) -> None:
        """Stop capture, close hardware, and return to IDLE."""
        if self.state not in (SessionState.RECORDING, SessionState.READY, SessionState.PAUSED):
            return

        self.state = SessionState.STOPPING
        self.steps_engine.stop()
        self._pump_stop.set()
        if self._pump_thread and self._pump_thread.is_alive():
            self._pump_thread.join(timeout=1.0)
        self._pump_thread = None

        self.writer.close()
        self._safe_shutdown_hardware()
        self.state = SessionState.IDLE
        self._update_metadata_file()

    def pause_recording(self) -> None:
        """Pause the pump loop without tearing down hardware."""
        if self.state != SessionState.RECORDING:
            return
        self.state = SessionState.PAUSED
        self._pump_stop.set()
        if self._pump_thread and self._pump_thread.is_alive():
            self._pump_thread.join(timeout=0.5)
        self._pump_thread = None
        self._update_metadata_file()

    def resume_recording(self) -> None:
        """Resume from PAUSED state."""
        if self.state != SessionState.PAUSED:
            return
        self._pump_stop.clear()
        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump_thread.start()
        self.state = SessionState.RECORDING
        self._update_metadata_file()

    def start_steps(self, steps: Tuple[Step, ...], start_delay_s: float = 0.0) -> None:
        if self.state not in (SessionState.RECORDING, SessionState.PAUSED):
            raise RuntimeError("Steps can only be started while recording or paused.")
        self.steps_engine.run(steps, start_delay_s=start_delay_s)

    def stop_steps(self) -> None:
        self.steps_engine.stop()

    @property
    def preflight_report(self) -> Dict[str, object]:
        return dict(self._preflight_report)

    def apply_setpoint(self, mmHg: float, *, source: str = "command", _from_bus: bool = False) -> None:
        """Set the hardware target and mirror it for downstream consumers."""
        value = float(mmHg)
        now = time.time()
        self.pressure._set_target(value)
        self.current_target_mmHg = value
        if source not in ("device", "device_echo", "echo"):
            self._last_gui_ts = now
            self._last_command_value = value
        self._record_setpoint_event(value, source or "command", timestamp=now)

    @property
    def current_target_mmHg(self) -> float:
        with self._setpoint_lock:
            return self._current_target_mmHg

    @current_target_mmHg.setter
    def current_target_mmHg(self, value: float) -> None:
        with self._setpoint_lock:
            self._current_target_mmHg = float(value)

    def _on_setpoint_echo(self, value: float) -> None:
        """Mirror device-reported targets without issuing a new command."""
        now = time.time()
        numeric = float(value)
        prior = self.current_target_mmHg
        if (
            abs(numeric - prior) < self._debounce_threshold
            and (now - self._last_echo_ts) < self._debounce_window_s
        ):
            self._last_echo_ts = now
            return

        event_source = "device"
        if self._last_gui_ts and (now - self._last_gui_ts) <= 0.5:
            event_source = "echo"
            if (
                self._last_command_value is not None
                and abs(numeric - self._last_command_value) > 1.0
            ):
                logger.warning(
                    "Setpoint echo %.2f mmHg diverges from last command %.2f mmHg",
                    numeric,
                    self._last_command_value,
                )

        self.current_target_mmHg = numeric
        self._last_echo_ts = now
        self._record_setpoint_event(numeric, event_source, timestamp=now)
        if event_source == "echo":
            self._last_command_value = numeric

    def _ingest_external_setpoint(self, value: float, source: str) -> None:
        """Proxy for setpoint_bus to route commands through this controller."""
        self.apply_setpoint(value, source=source, _from_bus=True)

    def _record_setpoint_event(self, value: float, source: str, *, timestamp: Optional[float] = None) -> None:
        ts = timestamp if timestamp is not None else time.time()
        self._setpoint_events.append((ts, source, float(value)))
        try:
            broadcast_setpoint(value, source)
        except Exception:
            pass

    def _handle_refresh_request(self) -> None:
        self._issue_setpoint_query("ui-refresh")

    def _issue_setpoint_query(self, reason: str = "") -> None:
        """Ask the adapter to report its current setpoint."""
        if not hasattr(self.pressure, "query_setpoint"):
            return
        try:
            self.pressure.query_setpoint()  # type: ignore[attr-defined]
            self._last_query_ts = time.time()
            if reason:
                logger.debug("Issued setpoint query (%s)", reason)
        except Exception as exc:
            logger.debug("Setpoint query failed: %s", exc)

    def _maybe_query_device_setpoint(self, now: float) -> None:
        """Watchdog to keep device/app setpoints in sync."""
        if now - self._last_echo_ts <= self._echo_watchdog_s:
            return
        if now - self._last_query_ts <= 2.0:
            return
        self._issue_setpoint_query("watchdog")

    # Internal helpers -----------------------------------------------------------
    def _preflight_camera(self) -> Dict[str, object]:
        self.camera.open()
        preview = self.camera.read(timeout=self.PREVIEW_TIMEOUT)
        if not preview or not preview.ok or preview.image is None:
            raise RuntimeError("Camera did not produce a frame during preflight.")
        frame = preview.image
        self._frame_size = (frame.shape[1], frame.shape[0])
        info = {
            "index": self.cfg.camera_index,
            "size": self._frame_size,
            "fps_request": self.cfg.camera_fps,
        }
        return info

    def _preflight_pressure(self) -> Dict[str, object]:
        self.pressure.connect()
        self._issue_setpoint_query("preflight")
        reading = self.pressure.read(timeout=0.5)
        info = {
            "port": self.pressure.port,
            "protocol": self.pressure.protocol,
            "initial_reading": asdict(reading) if reading else None,
        }
        return info

    def _preflight_disk_writer(self) -> Dict[str, object]:
        """
        Perform a lightweight write test using a temporary writer inside the
        session folder. The files are removed afterwards.
        """
        test_dir = self.session_folder / "_preflight_check"
        test_dir.mkdir(exist_ok=True)
        writer = Writer(str(test_dir))
        writer.start(
            video_name="test.avi",
            csv_name="test.csv",
            fps_hint=self.cfg.camera_fps or 30,
            size=self._frame_size,
        )

        start_time = time.perf_counter()
        frame_count = 0
        telem_count = 0
        while time.perf_counter() - start_time < self.PREFLIGHT_TEST_SECONDS:
            frame = self.camera.read(timeout=0.05)
            if frame and frame.ok and frame.image is not None:
                writer.push_frame(frame.t, frame.frame_no, frame.image)
                frame_count += 1
            reading = self.pressure.read(timeout=0.0)
            if reading:
                writer.push_telemetry(
                    reading.t,
                    reading.device_time_ms,
                    reading.p1,
                    reading.setpoint if reading.setpoint is not None else self.current_target_mmHg,
                )
                telem_count += 1

        writer.close()
        shutil.rmtree(test_dir, ignore_errors=True)
        return {
            "frames_written": frame_count,
            "telemetry_written": telem_count,
            "duration_s": self.PREFLIGHT_TEST_SECONDS,
        }

    def _create_session_folder(self) -> Path:
        base = Path(self.cfg.base_folder).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        parts = [self._slugify(part) for part in (self.cfg.animal_id, self.cfg.vessel_id, self.cfg.operator)]
        parts = [part for part in parts if part]
        prefix = f"{timestamp}__{'__'.join(parts)}" if parts else timestamp

        # Determine next session number.
        existing = sorted(base.glob(f"{prefix}__session*"))
        next_index = 1
        for folder in existing:
            try:
                suffix = folder.name.split("__session")[-1]
                index = int(suffix)
                next_index = max(next_index, index + 1)
            except ValueError:
                continue

        session_folder = base / f"{prefix}__session{next_index:02d}"
        session_folder.mkdir(parents=True, exist_ok=True)
        return session_folder

    def _update_metadata_file(self) -> None:
        metadata = {
            **asdict(self.cfg),
            "session_folder": str(self.session_folder),
            "state": self.state.name,
            "preflight": self._preflight_report,
            "setpoint_policy": "last-writer-wins",
            "setpoint_events": [
                {"t": ts, "value": val, "source": src}
                for ts, src, val in self._setpoint_events
            ],
        }
        self.writer.write_metadata(metadata)

    def _pump_loop(self) -> None:
        """Background loop that forwards frames/telemetry to the writer."""
        while not self._pump_stop.is_set():
            frame = self.camera.read(timeout=0.05)
            if frame and frame.ok and frame.image is not None:
                self.writer.push_frame(frame.t, frame.frame_no, frame.image)
            reading = self.pressure.read(timeout=0.0)
            if reading:
                self.writer.push_telemetry(
                    reading.t,
                    reading.device_time_ms,
                    reading.p1,
                    reading.setpoint if reading.setpoint is not None else self.current_target_mmHg,
                )
            self._maybe_query_device_setpoint(time.time())

    def _on_step_start(self, index: int, step: Step) -> None:
        self.writer.push_telemetry(
            time.perf_counter(),
            None,
            None,
            self.current_target_mmHg,
            note=f"step_{index}_start",
        )

    def _safe_shutdown_hardware(self) -> None:
        try:
            self.camera.close()
        except Exception:
            pass
        try:
            self.pressure.close()
        except Exception:
            pass

    @staticmethod
    def _slugify(value: str) -> str:
        value = value.strip().replace(" ", "_")
        allowed = "".join(ch for ch in value if ch.isalnum() or ch in ("_", "-", "."))
        return allowed

    def __del__(self) -> None:
        try:
            clear_setpoint_handler(self._ingest_external_setpoint)
        except Exception:
            pass
        try:
            clear_setpoint_query(self._handle_refresh_request)
        except Exception:
            pass

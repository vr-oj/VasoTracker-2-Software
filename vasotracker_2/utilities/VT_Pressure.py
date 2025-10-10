# #################################################
# # VasoTracker 2 - Blood Vessel Diameter Measurement Software
# #
# # Author: Calum Wilson, Matthew D Lee, and Chris Osborne
# # License: BSD 3-Clause License (See main file for details)
# # Website: www.vasostracker.com
# #
# #################################################

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Dict, List, Optional, Tuple
import math
import time
import tkinter.messagebox as tmb

from .pressure_devices import (
    ArduinoPressureDevice,
    NullPressureDevice,
    NIDaqPressureDevice,
    PressureDevice,
    SimPressureDevice,
)
from .VT_Arduino import Arduino
from .arduino_async_worker import ArduinoSerialWorker
from .arduino_link_monitor import LinkMonitor


def is_pydaqmx_available() -> bool:
    try:
        import PyDAQmx  # noqa: F401

        return True
    except Exception:
        return False


def _safe_mean(values) -> Optional[float]:
    filtered = [v for v in values if v is not None and not math.isnan(v)]
    if not filtered:
        return None
    return sum(filtered) / len(filtered)


@dataclass
class DeviceContext:
    """Book-keeping for the active hardware backend."""

    device: PressureDevice
    type_name: str
    arduino: Optional[Arduino] = None
    worker: Optional[ArduinoSerialWorker] = None
    monitor: Optional[LinkMonitor] = None
    nidaq_task: Optional["PyDAQmx.Task"] = None  # type: ignore[name-defined]


class PressureController:
    """
    High-level pressure controller that abstracts away hardware differences.

    The controller orchestrates protocol execution, updates UI state, and routes
    pressure commands/telemetry through a selected `PressureDevice` instance.
    """

    def __init__(self, model, view) -> None:
        self.model = model
        self.view = view

        self._device_ctx = DeviceContext(NullPressureDevice(), "none")
        self._latest: Tuple[Optional[float], Optional[float], Optional[float]] = (
            None,
            None,
            None,
        )

        # Pressure protocol state
        self.start_pressure: Optional[float] = None
        self.stop_pressure: Optional[float] = None
        self.pressure_interval: Optional[float] = None
        self.pressure_time_interval: Optional[float] = None
        self.pressure_start_time: Optional[float] = None
        self.next_pressure_update_time: float = 0.0
        self.multiplier: int = 1
        self.protocol_completed: bool = False
        self.stop_protocol_on_completion: bool = True
        self.completed: bool = False
        self.last_update_time: Optional[float] = None
        self.update_threshold: float = 1.0
        self.set_pressure: float = 0.0
        self._status_reset_job: Optional[str] = None
        self._default_status_text = self._capture_status_text()

        # Configure from persisted settings
        self.configure_from_state(start_immediately=False)

    # ------------------------------------------------------------------
    # Device configuration
    # ------------------------------------------------------------------
    def _capture_status_text(self) -> str:
        status_bar = getattr(self.view, "status_bar", None)
        if status_bar is None:
            return ""
        try:
            return str(status_bar.cget("text"))
        except Exception:
            return ""

    def _notify_status(self, message: str, *, persist: bool = False, log: bool = True) -> None:
        if log:
            print(f"[PressureController] {message}")

        status_bar = getattr(self.view, "status_bar", None)
        if status_bar is None:
            return

        try:
            status_bar.configure(text=message)
        except Exception:
            return

        if persist or not self._default_status_text:
            self._status_reset_job = None
            return

        if self._status_reset_job is not None:
            try:
                status_bar.after_cancel(self._status_reset_job)
            except Exception:
                pass
            self._status_reset_job = None

        def _reset_status() -> None:
            try:
                status_bar.configure(text=self._default_status_text)
            except Exception:
                pass
            finally:
                self._status_reset_job = None

        self._status_reset_job = status_bar.after(8000, _reset_status)

    @staticmethod
    def _summarise_exception(exc: Exception) -> str:
        text = str(exc).strip()
        return text if text else exc.__class__.__name__

    def _discover_serial_ports(self) -> List[Tuple[str, str]]:
        try:
            from serial.tools import list_ports
        except Exception:
            return []

        ports: List[Tuple[str, str]] = []
        for info in list_ports.comports():
            description = " ".join(
                part for part in (info.manufacturer, info.description, info.hwid) if part
            ).strip()
            ports.append((info.device, description or info.device))
        return ports

    def list_serial_ports(self) -> List[Tuple[str, str]]:
        """Return a list of (device, description) tuples for available serial ports."""
        return self._discover_serial_ports()

    def notify_status(self, message: str, *, persist: bool = False, log: bool = True) -> None:
        """Public helper for UI components that need to surface controller status messages."""
        self._notify_status(message, persist=persist, log=log)

    def _dispatch_to_ui(self, func: Callable[[], None]) -> None:
        """Execute `func` on the UI thread if possible."""
        status_bar = getattr(self.view, "status_bar", None)
        if status_bar is not None:
            try:
                status_bar.after(0, func)
                return
            except Exception:
                pass
        after = getattr(self.view, "after", None)
        if callable(after):
            try:
                after(0, func)
                return
            except Exception:
                pass
        func()

    def _notify_status_async(self, message: str, *, persist: bool = False, log: bool = True) -> None:
        """Thread-safe wrapper around `_notify_status`."""
        self._dispatch_to_ui(lambda m=message, p=persist, l=log: self._notify_status(m, persist=p, log=l))

    def configure_from_state(self, start_immediately: bool = False) -> None:
        """Reconfigure the active device based on toolbar/settings state."""
        settings = getattr(self.model.state.toolbar, "pressure_device", None)
        if settings is None:
            # Fallback to previous servo settings to avoid crashing on partial upgrades.
            settings = getattr(self.model.state.toolbar, "servo", None)
        if settings is None:
            self._set_device(NullPressureDevice(), "none")
            return

        device_type = str(settings.device_type.get() if hasattr(settings, "device_type") else "").strip().lower()

        if device_type in ("arduino", "vasomoto"):
            port = getattr(settings, "port", None)
            baud = getattr(settings, "baud", None)
            try:
                port_value = port.get() if port is not None else ""
                baud_value = int(baud.get()) if baud is not None else 115200
            except Exception:
                port_value = ""
                baud_value = 115200
            self._setup_arduino_device(port_value, baud_value, start_immediately=start_immediately)
        elif device_type in ("sim", "simulation"):
            device = SimPressureDevice()
            self._set_device(device, "sim")
            if start_immediately:
                device.start()
        elif device_type in ("ni", "ni-daq", "nidaq"):
            device = self._setup_nidaq_device(settings)
            self._set_device(device, "nidaq")
            if start_immediately:
                device.start()
        else:
            self._set_device(NullPressureDevice(), "none")

    def _set_device(
        self,
        device: PressureDevice,
        type_name: str,
        *,
        arduino: Optional[Arduino] = None,
        worker: Optional[ArduinoSerialWorker] = None,
        monitor: Optional[LinkMonitor] = None,
        nidaq_task: Optional["PyDAQmx.Task"] = None,  # type: ignore[name-defined]
    ) -> None:
        ctx = self._device_ctx
        try:
            ctx.device.stop()
        except Exception:
            pass
        if ctx.worker is not None:
            try:
                ctx.worker.stop()
            except Exception:
                pass
        if ctx.arduino is not None:
            try:
                ctx.arduino.close()
            except Exception:
                pass
        if ctx.nidaq_task is not None:
            try:
                ctx.nidaq_task.StopTask()
            except Exception:
                pass
            try:
                ctx.nidaq_task.ClearTask()
            except Exception:
                pass

        self._device_ctx = DeviceContext(
            device=device,
            type_name=type_name,
            arduino=arduino,
            worker=worker,
            monitor=monitor,
            nidaq_task=nidaq_task,
        )

        toolbar = getattr(self.view, "toolbar", None)
        if toolbar is not None:
            try:
                if type_name == "none":
                    toolbar.pressure_protocol_settings.set_lock_state()
                    toolbar.pressure_control_settings.set_lock_state()
                else:
                    toolbar.pressure_protocol_settings.set_unlock_state()
                    toolbar.pressure_control_settings.set_unlock_state()
            except Exception:
                pass

    def _setup_arduino_device(self, port: str, baud: int, start_immediately: bool = False) -> None:
        requested = (port or "").strip()
        auto_detect = requested.lower() in ("", "auto", "autodetect", "detect")
        discovered = self._discover_serial_ports()

        if not discovered and auto_detect:
            self._set_device(NullPressureDevice(), "none")
            self._notify_status(
                "No serial ports detected. Connect the Arduino and try again.",
                persist=True,
            )
            return

        monitor = LinkMonitor(expected_rx_hz=5.0, warmup_s=1.2, stale_s=2.5)
        device = ArduinoPressureDevice(worker=None)
        last_status: Dict[str, Optional[str]] = {"event": None}

        def update_port_variable(device_name: str) -> None:
            def setter() -> None:
                try:
                    self.model.state.toolbar.pressure_device.port.set(device_name)
                except Exception:
                    pass

            self._dispatch_to_ui(setter)

        def status_callback(event: str, info: dict) -> None:
            port_name = info.get("port") or (requested if requested else "auto")
            # Reduce chatter for repeated states.
            if event == last_status["event"] and event not in ("error", "stale", "healthy"):
                return
            last_status["event"] = event

            if event == "connecting":
                self._notify_status_async(f"Connecting to Arduino on {port_name}...", log=False)
            elif event == "open":
                self._notify_status_async(f"Serial port {port_name} opened.", log=False)
                actual_port = info.get("port")
                if actual_port:
                    update_port_variable(actual_port)
            elif event == "syncing":
                self._notify_status_async(
                    f"Waiting for Arduino telemetry ({port_name})...", log=False
                )
            elif event == "healthy":
                hz = info.get("rx_hz")
                if hz:
                    self._notify_status_async(
                        f"Arduino telemetry active ({hz:.1f} Hz).",
                        log=False,
                    )
                else:
                    self._notify_status_async("Arduino telemetry active.", log=False)
            elif event == "stale":
                age = info.get("age")
                if age:
                    self._notify_status_async(
                        f"Arduino telemetry stale ({age:.1f}s gap).",
                        log=False,
                    )
                else:
                    self._notify_status_async("Arduino telemetry stale.", log=False)
            elif event == "error":
                message = info.get("message") or "Unknown error"
                self._notify_status_async(
                    f"Arduino error on {port_name}: {message}",
                    persist=True,
                )
            elif event == "closed":
                self._notify_status_async(f"Arduino connection closed ({port_name}).", log=False)

        worker = ArduinoSerialWorker(
            port=None if auto_detect else requested,
            baud=baud,
            monitor=monitor,
            line_callback=device.handle_line,
            status_callback=status_callback,
        )
        device.bind_worker(worker)
        self._set_device(device, "arduino", worker=worker, monitor=monitor)

        if not discovered and not auto_detect:
            self._notify_status_async(
                f"Serial port {requested} not detected. The worker will keep retrying.",
                persist=True,
            )

        if start_immediately:
            try:
                device.start()
            except Exception as exc:
                self._notify_status_async(
                    f"Failed to start Arduino telemetry: {self._summarise_exception(exc)}",
                    persist=True,
                )
                print("Failed to start Arduino pressure device:", exc)

    def _setup_nidaq_device(self, settings) -> PressureDevice:
        if not is_pydaqmx_available():
            tmb.showinfo(
                "NI-DAQ unavailable",
                "PyDAQmx is not installed; reverting to Null pressure device.",
            )
            null_device = NullPressureDevice()
            self._set_device(null_device, "none")
            return null_device

        try:
            import PyDAQmx
        except Exception as exc:  # pragma: no cover - defensive
            print("Failed to import PyDAQmx:", exc)
            null_device = NullPressureDevice()
            self._set_device(null_device, "none")
            return null_device

        device_name = getattr(settings, "ni_device", getattr(settings, "device", None))
        ao_channel = getattr(settings, "ni_ao_channel", getattr(settings, "ao_channel", None))
        try:
            device_value = device_name.get() if device_name is not None else ""
            channel_value = ao_channel.get() if ao_channel is not None else ""
        except Exception:
            device_value = ""
            channel_value = ""

        if not device_value or not channel_value:
            tmb.showinfo(
                "NI-DAQ configuration",
                "Please select both a device and an analog output channel for NI-DAQ control.",
            )
            null_device = NullPressureDevice()
            self._set_device(null_device, "none")
            return null_device

        nidaq_task = PyDAQmx.Task()
        try:
            nidaq_task.CreateAOVoltageChan(
                f"/{device_value}/{channel_value}",
                "",
                -10.0,
                10.0,
                PyDAQmx.DAQmx_Val_Volts,
                None,
            )
            nidaq_task.StartTask()
        except Exception as exc:
            print("Failed to initialise NI-DAQ task:", exc)
            tmb.showinfo(
                "NI-DAQ connection failed",
                "Cannot connect to NI device. Please verify the hardware configuration.",
            )
            try:
                nidaq_task.ClearTask()
            except Exception:
                pass
            null_device = NullPressureDevice()
            self._set_device(null_device, "none")
            return null_device

        scale = getattr(settings, "ni_scale", None)
        try:
            scale_value = float(scale.get()) if scale is not None else 0.01
        except Exception:
            scale_value = 0.01

        device = NIDaqPressureDevice(ai_task=None, ao_task=nidaq_task, scale=scale_value)
        self._set_device(device, "nidaq", nidaq_task=nidaq_task)
        return device

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self) -> None:
        try:
            self._device_ctx.device.start()
        except Exception as exc:
            print("Pressure device start failed:", exc)

    def stop(self) -> None:
        try:
            self._device_ctx.device.stop()
        except Exception as exc:
            print("Pressure device stop failed:", exc)

    def adjust_pressure(self, pressure_value_mmHg: float, update_table: bool = True) -> None:
        value = max(0.0, min(200.0, float(pressure_value_mmHg)))

        pressure_protocol_settings = self.model.state.toolbar.pressure_protocol
        pressure_protocol_settings.set_pressure.set(value)

        try:
            self._device_ctx.device.set_pressure(value)
        except Exception as exc:
            print("set_pressure failed:", exc)

        if update_table:
            try:
                self.model.state.table.label.set(f"Set pressure = {value} mmHg")
                self.model.add_table_row()
            except Exception:
                pass

    def poll_latest(self) -> None:
        self._latest = self._device_ctx.device.read_latest()
        p1, p2, sp = self._latest
        tb = self.model.state.toolbar

        avg = _safe_mean([p1, p2])
        try:
            if avg is not None:
                tb.data_acq.pressure.set(round(avg, 2))
            if p1 is not None and hasattr(tb.data_acq, "pressure1"):
                tb.data_acq.pressure1.set(round(p1, 2))
            if p2 is not None and hasattr(tb.data_acq, "pressure2"):
                tb.data_acq.pressure2.set(round(p2, 2))
        except Exception:
            pass

        if sp is not None:
            tb.pressure_protocol.set_pressure.set(round(sp, 2))

    def get_latest(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        return self._latest

    def active_device_type(self) -> str:
        """Return the lowercase name of the currently active pressure device."""
        return str(self._device_ctx.type_name or "").lower()

    def link_monitor(self) -> Optional[LinkMonitor]:
        """Expose the Arduino link monitor (if available) for UI badges."""
        return self._device_ctx.monitor

    # ------------------------------------------------------------------
    # Protocol handling (largely retained from previous implementation)
    # ------------------------------------------------------------------
    def end_protocol(self) -> None:
        try:
            self.view.toolbar.pressure_control_settings.toggle_protocol_button()
        except Exception as exc:
            print(f"Error in end_protocol: {exc}")

    def update_intvl(self) -> None:
        current_time = time.time()

        if (
            self.last_update_time is not None
            and (current_time - self.last_update_time) < self.update_threshold
        ):
            return

        pressure_protocol_settings = self.model.state.toolbar.pressure_protocol
        if pressure_protocol_settings.pressure_protocol_flag.get() == 0:
            if self.protocol_completed:
                self.reset_protocol()
            return

        if self.pressure_start_time is None:
            self.initialize_pressure_protocol(pressure_protocol_settings)

        elapsed_seconds = current_time - self.pressure_start_time

        time_to_update_secs = (
            self.multiplier * self.pressure_time_interval - int(elapsed_seconds)
        )
        self.model.state.toolbar.data_acq.countdown.set(
            str(timedelta(seconds=max(time_to_update_secs, 0)))
        )

        if elapsed_seconds >= self.next_pressure_update_time:
            self.update_pressure()
            self.next_pressure_update_time += self.pressure_time_interval

        self.last_update_time = current_time

    def initialize_pressure_protocol(self, settings) -> None:
        self.start_pressure = settings.pressure_start.get()
        self.stop_pressure = settings.pressure_stop.get()
        self.pressure_interval = settings.pressure_intvl.get()
        self.pressure_time_interval = settings.time_intvl.get()
        self.pressure_start_time = time.time()
        self.next_pressure_update_time = self.pressure_time_interval
        self.multiplier = 1
        self.protocol_completed = False

        self.set_pressure = self.start_pressure
        self.adjust_pressure(self.set_pressure)

    def update_pressure(self) -> None:
        self.stop_protocol_on_completion = True
        self.completed = False

        if self.set_pressure < self.stop_pressure:
            self.set_pressure += self.pressure_interval
            self.adjust_pressure(self.set_pressure)
            self.multiplier += 1
        else:
            if not self.model.state.toolbar.pressure_protocol.hold_pressure.get():
                self.set_pressure = self.start_pressure
                self.adjust_pressure(self.set_pressure)
            self.multiplier = 1
            self.completed = True
            self.model.state.toolbar.pressure_protocol.pressure_protocol_flag.set(0)
            self.reset_protocol()

        if self.completed:
            self.end_protocol()

    def reset_protocol(self) -> None:
        settings = self.model.state.toolbar.pressure_protocol
        self.start_pressure = settings.pressure_start.get()
        self.stop_pressure = settings.pressure_stop.get()
        self.pressure_interval = settings.pressure_intvl.get()
        self.pressure_time_interval = settings.time_intvl.get()
        self.set_pressure = settings.set_pressure.get()
        self.pressure_start_time = None
        self.multiplier = 1
        self.next_pressure_update_time = 0
        self.protocol_completed = False

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
from typing import Optional, Tuple
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

        # Configure from persisted settings
        self.configure_from_state(start_immediately=False)

    # ------------------------------------------------------------------
    # Device configuration
    # ------------------------------------------------------------------
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
        nidaq_task: Optional["PyDAQmx.Task"] = None,  # type: ignore[name-defined]
    ) -> None:
        ctx = self._device_ctx
        try:
            ctx.device.stop()
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
            nidaq_task=nidaq_task,
        )

        if type_name == "none":
            self.view.toolbar.pressure_protocol_settings.set_lock_state()
        else:
            self.view.toolbar.pressure_protocol_settings.set_unlock_state()

    def _setup_arduino_device(self, port: str, baud: int, start_immediately: bool = False) -> None:
        if not port:
            self._set_device(NullPressureDevice(), "none")
            return
        try:
            arduino = Arduino(port=port, baud=baud)
        except Exception as exc:
            tmb.showinfo(
                "Arduino connection failed",
                f"Unable to open serial port '{port}':\n{exc}",
            )
            self._set_device(NullPressureDevice(), "none")
            return

        device = ArduinoPressureDevice(arduino)
        self._set_device(device, "arduino", arduino=arduino)
        if start_immediately:
            try:
                device.start()
            except Exception as exc:
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

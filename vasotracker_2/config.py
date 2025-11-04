##################################################
## VasoTracker 2 - Blood Vessel Diameter Measurement Software
##
## Author: Calum Wilson, Matthew D Lee, and Chris Osborne
## License: BSD 3-Clause License (See main file for details)
## Website: www.vasostracker.com
##
##################################################


from dataclasses import dataclass, field, asdict, fields, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union
import toml
import dacite
import os
from tkinter import TclError

if TYPE_CHECKING:
    from vt_mvc import VtState


def _safe_set(var, value) -> None:
    if var is None:
        return
    try:
        var.set(value)
    except TclError:
        pass
    except Exception:
        pass


class Configurator:
    def set_values(self, state: "VtState"):
        pass

    @classmethod
    def from_state(cls, state: "VtState"):
        return cls()


@dataclass
class AcquisitionSettings(Configurator):
    camera: str = ""
    pixel_world_scale: float = 1.0
    exposure: int = 50
    pixel_clock: int = 10
    recording_interval: float = 300.0
    refresh_min_interval: float = 0.01
    refresh_faster_interval: float = 0.001

    def set_values(self, state: "VtState"):
        acq = state.toolbar.acq
        if self.camera:
            _safe_set(acq.camera, self.camera)
        _safe_set(acq.scale, self.pixel_world_scale)
        _safe_set(acq.exposure, self.exposure)
        _safe_set(acq.pixel_clock, self.pixel_clock)
        _safe_set(acq.rec_interval, self.recording_interval)

    @classmethod
    def from_state(cls, state: "VtState"):
        acq = state.toolbar.acq
        camera = ""
        try:
            camera = acq.camera.get()
        except Exception:
            camera = ""
        scale = acq.scale.get()
        exposure = acq.exposure.get()
        pixel_clock = acq.pixel_clock.get()
        recording_interval = acq.rec_interval.get()
        return cls(
            camera=camera,
            pixel_world_scale=scale,
            exposure=exposure,
            pixel_clock=pixel_clock,
            recording_interval=recording_interval,
        )


@dataclass
class AnalysisSettings(Configurator):
    num_lines: int = 10
    smooth: int = 21
    integration: int = 20
    threshold: float = 5.5
    num_threads: int = 1

    def set_values(self, state: "VtState"):
        ana = state.toolbar.analysis
        _safe_set(ana.num_lines, self.num_lines)
        _safe_set(ana.smooth_factor, self.smooth)
        _safe_set(ana.integration_factor, self.integration)
        _safe_set(ana.thresh_factor, self.threshold)

    @classmethod
    def from_state(cls, state: "VtState"):
        ana = state.toolbar.analysis
        num_lines = ana.num_lines.get()
        smooth = ana.smooth_factor.get()
        integration = ana.smooth_factor.get()
        threshold = ana.thresh_factor.get()
        return cls(
            num_lines=num_lines,
            smooth=smooth,
            integration=integration,
            threshold=threshold,
        )


@dataclass
class SourceSettings(Configurator):
    file_fps: float = 1.0

    def set_values(self, state: "VtState"):
        _safe_set(state.toolbar.source.file_fps, self.file_fps)

    @classmethod
    def from_state(cls, state: "VtState"):
        source = state.toolbar.source
        try:
            fps = float(source.file_fps.get())
        except Exception:
            fps = 1.0
        return cls(file_fps=fps)


@dataclass
class GraphAxisSettings(Configurator):
    x_min: float = -1200.0
    x_max: float = 0.0
    y_min1: float = 50.0
    y_max1: float = 250.0
    y_min2: float = 25.0
    y_max2: float = 200.0

    def set_values(self, state: "VtState"):
        g = state.toolbar.graph
        _safe_set(g.x_min, self.x_min)
        _safe_set(g.x_max, self.x_max)
        _safe_set(g.y_min_od, self.y_min1)
        _safe_set(g.y_max_od, self.y_max1)
        _safe_set(g.y_min_id, self.y_min2)
        _safe_set(g.y_max_id, self.y_max2)
    @classmethod
    def from_state(cls, state: "VtState"):
        g = state.toolbar.graph
        return cls(
            x_min=g.x_min.get(),
            x_max=g.x_max.get(),
            y_min1=g.y_min_od.get(),
            y_max1=g.y_max_od.get(),
            y_min2=g.y_min_id.get(),
            y_max2=g.y_max_od.get(),
        )


@dataclass
class MemorySettings(Configurator):
    num_plot_points: int = 500000
    num_data_points: int = 500000

    def set_values(self, state: "VtState"):
        state.measure.max_len = self.num_data_points

    @classmethod
    def from_state(cls, state: "VtState"):
        if state.measure.max_len is not None:
            return cls(
                num_data_points=state.measure.max_len
            )
        return cls()
    

@dataclass
class PressureHardwareSettings(Configurator):
    device: str = "Arduino"
    port: str = "COM5"
    baud: int = 115200
    ni_device: str = "Dev1"
    ni_ao_channel: str = "ao1"
    ni_scale: float = 0.01

    def set_values(self, state: "VtState"):
        hardware = state.toolbar.pressure_device
        _safe_set(hardware.device_type, self.device)
        _safe_set(hardware.port, self.port)
        _safe_set(hardware.baud, self.baud)
        _safe_set(hardware.ni_device, self.ni_device)
        _safe_set(hardware.ni_ao_channel, self.ni_ao_channel)
        _safe_set(hardware.ni_scale, self.ni_scale)

    @classmethod
    def from_state(cls, state: "VtState"):
        hardware = state.toolbar.pressure_device
        return cls(
            device=hardware.device_type.get(),
            port=hardware.port.get(),
            baud=hardware.baud.get(),
            ni_device=hardware.ni_device.get(),
            ni_ao_channel=hardware.ni_ao_channel.get(),
            ni_scale=hardware.ni_scale.get(),
        )


@dataclass
class PressureControlSettings(Configurator):
    default_pressure: float = 20.0
    time_interval: float = 300.0
    start_pressure: float = 20.0
    stop_pressure: float = 100.0
    pressure_interval: float = 20.0

    def set_values(self, state: "VtState"):
        p = state.toolbar.pressure_protocol
        _safe_set(p.pressure_start, str(self.start_pressure))
        _safe_set(p.pressure_stop, str(self.stop_pressure))
        _safe_set(p.pressure_intvl, str(self.pressure_interval))
        _safe_set(p.time_intvl, str(self.time_interval))
        _safe_set(p.set_pressure, str(self.default_pressure))

    @classmethod
    def from_state(cls, state: "VtState"):
        p = state.toolbar.pressure_protocol
        def _to_float(var, fallback: float) -> float:
            if var is None:
                return fallback
            try:
                value = var.get()
            except Exception:
                return fallback
            if isinstance(value, str):
                value = value.strip()
                if value == "":
                    return fallback
            try:
                return float(value)
            except (TypeError, ValueError):
                return fallback

        start_p = _to_float(p.pressure_start, 0.0)
        stop_p = _to_float(p.pressure_stop, start_p)
        p_interval = _to_float(p.pressure_intvl, 0.0)
        t_interval = _to_float(p.time_intvl, 0.0)
        default_pressure = _to_float(p.set_pressure, start_p)
        return cls(
            default_pressure=default_pressure,
            time_interval=t_interval,
            start_pressure=start_p,
            stop_pressure=stop_p,
            pressure_interval=p_interval,
        )

@dataclass
class TisDcamSettings:
    property_gain: int = 240


@dataclass
class ProxyCameraSettings:
    #initialdir = os.getcwd()
    path_template: str = "\\SampleData\\TEST{:04d}.tif" #f'{initialdir}' + "\\SampleData\\TEST{:d}.tif" #
    max_frame: int = 300

@dataclass
class RegistrationSettings:
    #initialdir = os.getcwd()
    register_flag: int = 0
    neveragain_flag: int = 0

@dataclass
class Config(Configurator):
    acquisition: AcquisitionSettings = field(default_factory=AcquisitionSettings)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    source: SourceSettings = field(default_factory=SourceSettings)
    pressure: PressureHardwareSettings = field(default_factory=PressureHardwareSettings)
    graph_axes: GraphAxisSettings = field(default_factory=GraphAxisSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    pressure_control: PressureControlSettings = field(
        default_factory=PressureControlSettings
    )
    TIS_DCAM: TisDcamSettings = field(default_factory=TisDcamSettings)
    proxy_camera: ProxyCameraSettings = field(default_factory=ProxyCameraSettings)
    registration: RegistrationSettings = field(default_factory=RegistrationSettings)

    path: Optional[str] = None

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "Config":
        data = toml.load(path)
        if "pressure" not in data:
            servo_data = data.pop("servo", {}) or {}
            pressure_device = "NI-DAQ" if servo_data.get("device") else "Arduino"
            data["pressure"] = {
                "device": pressure_device,
                "port": "COM5" if pressure_device == "Arduino" else "",
                "baud": 115_200,
                "ni_device": servo_data.get("device", "Dev1"),
                "ni_ao_channel": servo_data.get("ao_channel", "ao1"),
                "ni_scale": 0.01,
            }
        result = dacite.from_dict(data_class=cls, data=data)
        result.path = str(path)
        return result

    def save(self, override_path: Optional[Union[str, Path]] = None):
        path = self.path
        if override_path is not None:
            path = override_path
        data = asdict(self)
        if self.path is not None:
            del data["path"]
        with open(path, "w") as f:
            toml.dump(data, f)

    def set_values(self, state: "VtState"):
        class_fields = fields(self)
        for f in class_fields:
            # NOTE(cmo): Check is_dataclass first, because the Union[str, None]
            # breaks older Python issubclass
            if is_dataclass(f.type) and issubclass(f.type, Configurator):
                item: Configurator = getattr(self, f.name)
                item.set_values(state)

    @classmethod
    def from_state(cls, state: "VtState"):
        attrs = {}
        class_fields = fields(cls)
        for f in class_fields:
            # NOTE(cmo): Check is_dataclass first, because the Union[str, None]
            # breaks older Python issubclass
            if is_dataclass(f.type) and issubclass(f.type, Configurator):
                attrs[f.name] = f.type.from_state(state)
        return cls(**attrs)

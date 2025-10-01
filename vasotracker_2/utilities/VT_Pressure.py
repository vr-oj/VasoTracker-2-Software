##################################################
## VasoTracker 2 - Blood Vessel Diameter Measurement Software
##
## Author: Calum Wilson, Matthew D Lee, and Chris Osborne
## License: BSD 3-Clause License (See main file for details)
## Website: www.vasostracker.com
##
##################################################


## We found the following to be useful:
## https://www.safaribooksonline.com/library/view/python-cookbook/0596001673/ch09s07.html
## http://code.activestate.com/recipes/82965-threads-tkinter-and-asynchronous-io/
## https://www.physics.utoronto.ca/~phy326/python/Live_Plot.py
## http://forum.arduino.cc/index.php?topic=225329.msg1810764#msg1810764
## https://stackoverflow.com/questions/9917280/using-draw-in-pil-tkinter
## https://stackoverflow.com/questions/37334106/opening-image-on-canvas-cropping-the-image-and-update-the-canvas

import sys
import os
import time
from datetime import timedelta
import tkinter.messagebox as tmb

#########################################################################################
# Calum trying to sort out the National Instruments problem....
#########################################################################################
'''
def get_resource_path(relative_path):
    """Get the path to a resource, whether it's bundled with PyInstaller or not."""
    base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
    return os.path.join(base_path, relative_path)



# If running as an exe, then get the included nidaqmax.h file location
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    # running in a PyInstaller bundle
    header_dir = os.path.join(sys._MEIPASS, 'include')
else:
    # running in a normal Python environment
    # This is wrong, but it won't matter because I set the DAQmxConfig.py to check the header_dir last.
    header_dir = os.path.join(os.getcwd(), 'include')
print("header_dir = ", header_dir)

os.environ['NIDAQMX_INCLUDE_PATH'] = header_dir
print("NIDAQMX_INCLUDE_PATH set to:", os.environ['NIDAQMX_INCLUDE_PATH'])

from PyDAQmx.DAQmxConfig import is_pydaqmx_installed
print(f"PyDAQmx installed: {is_pydaqmx_installed()}")

'''
try:
    import PyDAQmx  # type: ignore
    from PyDAQmx import *  # noqa: F401,F403
    _pydaqmx_available = True
except Exception as exc:
    PyDAQmx = None  # type: ignore
    _pydaqmx_available = False
    print(f"PyDAQmx import failed: {exc}")


def is_pydaqmx_available():
    return _pydaqmx_available


#########################################################################################
# End of Calum trying to sort out the National Instruments problem....
#########################################################################################






class PressureController:
    def __init__(self, model, view, pydaqmx_available):
        self.model = model
        self.view = view
        self.pydaqmx_available = pydaqmx_available
        self.task = None
        self.arduino = None
        self.current_device_type = (
            self.model.state.toolbar.servo.device_type.get() or "NI"
        )
        #self.initialize_pressure_system()
        self.start_pressure = None
        self.stop_pressure = None
        self.pressure_interval = None
        self.pressure_time_interval = None
        self.set_pressure = None
        self.pressure_start_time = None
        self.multiplier = 1
        self.last_update_time = None
        self.update_threshold = 1  # Minimum time interval in seconds between updates
        self.on_option_changed()

    def end_protocol(self):
        try:
            self.view.toolbar.pressure_control_settings.toggle_protocol_button()
        except Exception as e:
            print(f"Error in end_protocol: {e}")



    def initialize_pressure_system(self):
        self.on_option_changed()

    def set_arduino(self, arduino):
        self.arduino = arduino
        self.on_option_changed()

    def on_device_type_changed(self):
        device_type = self.model.state.toolbar.servo.device_type.get() or "NI"
        if device_type != self.current_device_type:
            if device_type != "NI":
                self._clear_task()
        self.current_device_type = device_type
        self.on_option_changed()

    def on_option_changed(self, *args):
        servo_settings = self.model.state.toolbar.servo
        device_type = servo_settings.device_type.get() or "NI"

        if device_type == "NI":
            device = servo_settings.device.get()
            ao_channel = servo_settings.ao_channel.get()

            if not self.pydaqmx_available:
                print("PyDAQmx is not available; cannot control NI hardware.")
                self._update_manual_control_state(False)
                self._lock_pressure_protocol_settings()
                return

            if device and ao_channel:
                if self.set_dev():
                    self._update_manual_control_state(True)
                    self._unlock_pressure_protocol_settings()
                else:
                    self._update_manual_control_state(False)
                    self._lock_pressure_protocol_settings()
            else:
                self._update_manual_control_state(False)
                self._lock_pressure_protocol_settings()
        elif device_type == "Arduino":
            if self.arduino:
                self._update_manual_control_state(True)
                self._unlock_pressure_protocol_settings()
            else:
                print("Arduino controller not initialised.")
                self._update_manual_control_state(False)
        else:
            self._update_manual_control_state(False)

    def update_intvl(self):
        current_time = time.time()

        # Check if sufficient time has elapsed since the last update
        if self.last_update_time is not None and (current_time - self.last_update_time) < self.update_threshold:
            return  # Exit if not enough time has passed

        pressure_protocol_settings = self.model.state.toolbar.pressure_protocol
        if pressure_protocol_settings.pressure_protocol_flag.get() == 0:
            if self.protocol_completed:
                self.reset_protocol()  # Reset protocol for next run
            return  # Exit if the protocol is not active

        if self.pressure_start_time is None:
            self.initialize_pressure_protocol(pressure_protocol_settings)

        elapsed_seconds = current_time - self.pressure_start_time

        time_to_update_secs = self.multiplier * self.pressure_time_interval - int(elapsed_seconds)
        self.model.state.toolbar.data_acq.countdown.set(str(timedelta(seconds=time_to_update_secs)))

        if elapsed_seconds >= self.next_pressure_update_time:
            self.update_pressure()
            self.next_pressure_update_time += self.pressure_time_interval# * self.multiplier

        self.last_update_time = current_time

    def initialize_pressure_protocol(self, settings):
        self.start_pressure = settings.pressure_start.get()
        self.stop_pressure = settings.pressure_stop.get()
        self.pressure_interval = settings.pressure_intvl.get()
        self.pressure_time_interval = settings.time_intvl.get()
        self.pressure_start_time = time.time()
        self.next_pressure_update_time = self.pressure_time_interval
        self.multiplier = 1
        self.protocol_completed = False

        # Immediately set pressure to start_pressure when the protocol begins
        self.set_pressure = self.start_pressure
        self.adjust_pressure(self.set_pressure)


    def update_pressure(self):
        self.stop_protocol_on_completion = True
        self.completed = False
        if self.set_pressure < self.stop_pressure:
            self.set_pressure += self.pressure_interval
            self.adjust_pressure(self.set_pressure)
            self.multiplier += 1
        else:
            

            if not self.model.state.toolbar.pressure_protocol.hold_pressure.get():# Reset to start pressure or stop protocol
                self.set_pressure = self.start_pressure
                self.adjust_pressure(self.set_pressure)
            self.multiplier = 1  # Reset the multiplier
            self.completed = True
            self.model.state.toolbar.pressure_protocol.pressure_protocol_flag.set(0)
            self.reset_protocol()  # Reset protocol for next run

        if self.completed:
            self.end_protocol()



    def reset_protocol(self):
        # Reset all protocol control variables
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



    def set_dev(self):
        if not self.pydaqmx_available:
            return False
        time.sleep(2)
        servo_settings = self.model.state.toolbar.servo
        device = servo_settings.device.get()
        ao_channel = servo_settings.ao_channel.get()



        # Clear any existing task to avoid conflicts
        if self.task is not None:
            self.task.ClearTask()

        try:
            self.task = PyDAQmx.Task()
            self.task.CreateAOVoltageChan(f"/{device}/{ao_channel}", "", -10.0, 10.0, PyDAQmx.DAQmx_Val_Volts, None)
            self.task.StartTask()
            return True  # Device successfully set
        except Exception as e:
            print("Failed to connect to NI device:", e)
            # Temporarily remove the trace callback if necessary
            try:
                servo_settings.device.trace_remove(...)
            except:
                pass
            try:
                servo_settings.ao_channel.trace_remove(...)
            except:
                pass
            tmb.showinfo("Warning", "Cannot connect to NI device:\n - Ensure the device is connected via USB.\n - Check the device name in the NI Device Monitor Software.")

            servo_settings.device.set("")
            servo_settings.ao_channel.set("")
            servo_settings.device.trace_add("write", ...)
            servo_settings.ao_channel.trace_add("write", ...)
            return False  # Failed to set the device
            
        

    def adjust_pressure(self, pressure_value, update_table=True):
        # Validate and adjust pressure value to be within the acceptable range
        pressure_value = max(min(200, pressure_value), 0)

        pressure_protocol_settings = self.model.state.toolbar.pressure_protocol
        pressure_protocol_settings.set_pressure.set(pressure_value)

        device_type = self.model.state.toolbar.servo.device_type.get() or "NI"

        if device_type == "NI":
            if not self.pydaqmx_available or self.task is None:
                print("Cannot set pressure: NI hardware is not ready.")
                return
            try:
                self.task.WriteAnalogScalarF64(1, 10.0, pressure_value / 100, None)
            except Exception as e:
                print("Exception occurred while setting pressure:", e)
        elif device_type == "Arduino":
            if not self.arduino:
                print("Cannot set pressure: Arduino controller is not available.")
                return
            try:
                self.arduino.sendData(int(round(pressure_value)))
            except Exception as e:
                print(f"Exception occurred while sending pressure to Arduino: {e}")
        else:
            print(f"Unsupported device type '{device_type}' for pressure control.")
            return

        # Optionally update the table
        # If update_table is True, this will update the UI to reflect the new pressure
        if update_table:
            # Assuming this method updates a UI element to show the current pressure
            self.model.state.table.label.set(f"Set pressure = {pressure_value} mmHg")
            self.model.add_table_row()

    def _update_manual_control_state(self, enabled: bool):
        try:
            controls = self.view.toolbar.pressure_control_settings
        except AttributeError:
            return

        try:
            if enabled:
                controls.enable_buttons()
            else:
                controls.disable_buttons()
        except Exception:
            pass

    def _unlock_pressure_protocol_settings(self):
        try:
            self.view.toolbar.pressure_protocol_settings.set_unlock_state()
        except Exception:
            pass

    def _lock_pressure_protocol_settings(self):
        try:
            self.view.toolbar.pressure_protocol_settings.set_lock_state()
        except Exception:
            pass

    def _clear_task(self):
        if self.task is not None:
            try:
                self.task.ClearTask()
            except Exception:
                pass
            finally:
                self.task = None

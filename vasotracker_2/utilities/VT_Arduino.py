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

from __future__ import division
import numpy as np

# Tkinter imports
import tkinter as tk
from tkinter import *
import tkinter.simpledialog as tkSimpleDialog
import tkinter.messagebox as tmb
import tkinter.filedialog as tkFileDialog
from tkinter import ttk
from PIL import Image, ImageTk  # convert cv2 image to tkinter

E = tk.E
W = tk.W
N = tk.N
S = tk.S
ypadding = 1.5  # ypadding just to save time - used for both x and y

# Other imports
import os
import sys
import time
import datetime
import threading
import random
import queue
from typing import List

import cv2
import csv
from skimage import io
import skimage
from skimage import measure
import serial
try:
    from serial import SerialException
except ImportError:  # pragma: no cover - SerialException missing if pyserial absent
    SerialException = Exception  # type: ignore
try:
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - serial.tools may be unavailable during linting
    list_ports = None
import webbrowser

import colorama

# Add MicroManager to path
"""
import sys
MM_PATH = os.path.join('C:', os.path.sep, 'Program Files','Micro-Manager-1.4')
sys.path.append(MM_PATH)
os.environ['PATH'] = MM_PATH + ';' + os.environ['PATH']
try:
    import MMCorePy
except:
    tmb.showinfo("Warning", "You need to install umanager")
"""
"""
import sys
sys.path.append('C:\Program Files\Micro-Manager-1.4')
import MMCorePy
"""
# import PyQt5
# matplotlib imports
import matplotlib

# matplotlib.use('Qt5Agg')
# matplotlib.use('Qt4Agg', warn=True)
# import matplotlib.backends.tkagg as tkagg
# from matplotlib.figure import Figure
# from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2TkAgg
import matplotlib.pyplot as plt

# from matplotlib.backends import backend_qt4agg
from matplotlib import pyplot


class Arduino:
    def __init__(self, PORTS):
        # Open the serial ports
        self.PORTS: List[serial.Serial] = []
        self.measured_pressure_1 = None
        self.measured_pressure_2 = None
        self.measured_pressure_avg = None
        self.measured_temperature = None

        self._initialise_serial_ports()

    def _initialise_serial_ports(self):
        """Discover and open any Arduino-compatible serial ports."""
        candidate_ports = self._discover_arduino_ports()

        if not candidate_ports:
            print("No Arduino devices detected on available serial ports.")
            return

        for port_info in candidate_ports:
            port = self._open_serial_port(port_info.device)
            if port is not None:
                self.PORTS.append(port)

        if not self.PORTS:
            print("Failed to open any detected Arduino serial ports.")

    def _discover_arduino_ports(self):
        """Return a list of serial ports that look like Arduino boards."""
        if list_ports is None:
            print("pyserial's list_ports module is unavailable; cannot auto-detect Arduino devices.")
            return []

        known_vids = {0x2341, 0x2A03, 0x1A86, 0x10C4, 0x16C0}
        arduino_ports = []
        all_ports = list(list_ports.comports())

        for port in all_ports:
            description = (port.description or "").lower()
            manufacturer = (port.manufacturer or "").lower()
            vid = port.vid

            if vid in known_vids:
                arduino_ports.append(port)
                continue

            keywords = ("arduino", "ch340", "cp210", "usb-serial", "usb serial")
            if any(keyword in description for keyword in keywords):
                arduino_ports.append(port)
                continue
            if any(keyword in manufacturer for keyword in keywords):
                arduino_ports.append(port)

        if not arduino_ports and len(all_ports) == 1:
            # Fallback: if there's only one serial device, assume it's the Arduino
            arduino_ports = all_ports

        return arduino_ports

    def _open_serial_port(self, device):
        try:
            return serial.Serial(device, baudrate=9600, dsrdtr=True, timeout=1)
        except SerialException as exc:
            print(f"Unable to open serial device {device}: {exc}")
        except Exception as exc:  # pragma: no cover - unexpected
            print(f"Unexpected error opening serial device {device}: {exc}")
        return None

    def getports(self):
        return self.PORTS

    def getData(self):
        num_buffers = max(2, len(self.PORTS))
        data = [[] for _ in range(num_buffers)]

        for i, GLOBAL_PORT in enumerate(self.PORTS):
            try:
                if hasattr(GLOBAL_PORT, "reset_input_buffer"):
                    GLOBAL_PORT.reset_input_buffer()
                    GLOBAL_PORT.reset_output_buffer()
                else:
                    GLOBAL_PORT.flushInput()
                    GLOBAL_PORT.flushOutput()
                GLOBAL_PORT.write(b".")  # Note the b prefix for bytes

                startMarker = ord("<")
                endMarker = ord(">")

                ck = bytearray()

                # Wait for the start character
                while True:
                    x = GLOBAL_PORT.read()
                    if not x:
                        raise TimeoutError("Timed out waiting for Arduino start marker")
                    if x[0] == startMarker:
                        break

                # Save data until the end marker is found
                while True:
                    x = GLOBAL_PORT.read()
                    if not x:
                        raise TimeoutError("Timed out waiting for Arduino end marker")
                    if x[0] == endMarker:
                        break
                    if x[0] != startMarker:
                        ck.extend(x)

                decoded = ck.decode("utf-8", errors="ignore")
                data[i].append(decoded)
            except Exception:
                decoded = "Nodata:0;Nodata2:0"
                data[i].append(decoded)
        
        return data

    def sortdata(self,temppres):
    
        # Initialize variables
        temp = np.nan
        pres1 = np.nan
        pres2 = np.nan

        # Loop through the data from the two Arduinos (tempres contains dummy data if < 2 connected)
        for data in temppres:
            if len(data) > 0:

                # Split the data by Arduino
                val = data[0].strip('\n\r').split(';')
                val = val[:-1]
                val = [el.split(':') for el in val]

                # Get the temperature value
                if val[0][0] == "T1":
                    try:
                        self.measured_temperature = float(val[0][1])
                    except:

                        self.measured_temperature = np.nan
                    #set_temp = float(val[1][1])

                # Get the pressure value
                elif val[0][0] == "P1":
                    try:
                        self.measured_pressure_1  = float(val[0][1])
                        self.measured_pressure_2 = float(val[1][1])
                        self.measured_pressure_avg = np.average([self.measured_pressure_1, self.measured_pressure_2], axis=None)

                    except:
                        self.measured_pressure_1 = np.nan
                        self.measured_pressure_2 = np.nan
                        self.measured_pressure_avg = np.nan

                else:
                    pass
            else:
                pass

        return  self.measured_pressure_1, self.measured_pressure_2, self.measured_pressure_avg, self.measured_temperature


    def sendData(self, pressure):
        print(pressure)
        msg = f"<{pressure}>"  # Do we need the newline?

        #print("message = ", msg)
        x = msg.encode("ascii")
        for i, GLOBAL_PORT in enumerate(self.PORTS):
            GLOBAL_PORT.flushInput()
            GLOBAL_PORT.flushOutput() 

            try:
                GLOBAL_PORT.write(x)  # Send the encoded message
            except Exception as e:
                print(f"Error sending data: {e}")

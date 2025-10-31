"""
Camera adapter for VasoTracker 2.

Encapsulates camera discovery and threaded frame capture so UI code can poll
frames without blocking. Any heavy lifting (decoding, timestamping) happens in
the background thread.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import cv2


@dataclass
class Frame:
    """Container for captured frames with metadata."""

    t: float
    frame_no: int
    image: Optional["cv2.Mat"]
    ok: bool


class CameraAdapter:
    """Thread-safe wrapper around OpenCV VideoCapture."""

    def __init__(
        self,
        index: int = 0,
        size: Optional[Tuple[int, int]] = None,
        fps: Optional[int] = None,
        queue_size: int = 200,
    ) -> None:
        self.index = index
        self.size = size
        self.fps = fps
        self._queue: "queue.Queue[Frame]" = queue.Queue(maxsize=queue_size)
        self._capture: Optional[cv2.VideoCapture] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._frame_counter = 0
        self._backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY

    # Discovery -----------------------------------------------------------------
    @staticmethod
    def list_devices(max_index: int = 8) -> Tuple[Tuple[int, str], ...]:
        """
        Probe connected cameras and return their indices with friendly labels.
        """
        devices = []
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        for i in range(max_index):
            cap = cv2.VideoCapture(i, backend)
            if not cap.isOpened():
                cap.release()
                continue
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            name = f"Camera {i} ({width}x{height})"
            devices.append((i, name))
            cap.release()
        return tuple(devices)

    # Lifecycle -----------------------------------------------------------------
    def open(self) -> None:
        """Open the selected camera and start the reader thread."""
        with self._lock:
            self.close()
            capture = cv2.VideoCapture(self.index, self._backend)
            if not capture.isOpened():
                capture.release()
                raise RuntimeError(f"Unable to open camera index {self.index}")

            if self.size:
                width, height = self.size
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            if self.fps:
                capture.set(cv2.CAP_PROP_FPS, self.fps)

            self._capture = capture
            self._start_reader()

    def close(self) -> None:
        """Stop capture and release camera resources."""
        with self._lock:
            self._stop_reader()
            if self._capture:
                self._capture.release()
            self._capture = None

    # IO ------------------------------------------------------------------------
    def read(self, timeout: float = 0.0) -> Optional[Frame]:
        """
        Retrieve the next frame.

        Args:
            timeout: Maximum seconds to wait. Zero is non-blocking.
        """
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # Background worker ---------------------------------------------------------
    def _start_reader(self) -> None:
        self._stop_event.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _stop_reader(self) -> None:
        self._stop_event.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=0.5)
        self._reader_thread = None
        with self._queue.mutex:
            self._queue.queue.clear()

    def _reader_loop(self) -> None:
        assert self._capture is not None
        while not self._stop_event.is_set():
            ok, frame = self._capture.read()
            timestamp = time.perf_counter()
            self._frame_counter += 1
            wrapped = Frame(t=timestamp, frame_no=self._frame_counter, image=frame, ok=ok)
            self._publish(wrapped)

    def _publish(self, frame: Frame) -> None:
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            try:
                _ = self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                pass


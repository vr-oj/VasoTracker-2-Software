"""
Asynchronous disk writer for VasoTracker 2 sessions.

Frames and telemetry are queued from the capture threads and written to disk
from a dedicated worker thread to avoid blocking the UI.
"""

from __future__ import annotations

import csv
import json
import queue
import threading
import logging
from pathlib import Path
from typing import Optional

import cv2


class Writer:
    """Threaded queue-based writer for frames and telemetry."""

    def __init__(
        self,
        folder: str,
        frame_queue_size: int = 500,
        telemetry_queue_size: int = 2000,
    ) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._frames: "queue.Queue[tuple]" = queue.Queue(maxsize=frame_queue_size)
        self._telemetry: "queue.Queue[list]" = queue.Queue(maxsize=telemetry_queue_size)

        self._video_writer: Optional[cv2.VideoWriter] = None
        self._video_path: Optional[Path] = None
        self._video_tmp_path: Optional[Path] = None
        self._csv_file: Optional[open] = None
        self._csv_writer: Optional[csv.writer] = None
        self._csv_path: Optional[Path] = None
        self._csv_tmp_path: Optional[Path] = None
        self._fps_hint = 30
        self._frame_size: Optional[tuple[int, int]] = None
        self._last_written_setpoint: Optional[float] = None
        self._stale_setpoint_warned = False

    # Lifecycle -----------------------------------------------------------------
    def start(
        self,
        video_name: str = "video.avi",
        csv_name: str = "telemetry.csv",
        fps_hint: int = 30,
        size: Optional[tuple[int, int]] = None,
        fourcc: str = "MJPG",
    ) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Writer already running.")

        self._fps_hint = fps_hint
        self._frame_size = size

        if size:
            self._video_path = self.folder / video_name
            self._video_tmp_path = self.folder / f"{video_name}.tmp"
            fourcc_code = cv2.VideoWriter_fourcc(*fourcc)
            self._video_writer = cv2.VideoWriter(
                str(self._video_tmp_path),
                fourcc_code,
                fps_hint,
                size,
            )
            if not self._video_writer.isOpened():
                raise RuntimeError(f"Unable to open video writer for {video_name}")

        self._csv_path = self.folder / csv_name
        self._csv_tmp_path = self.folder / f"{csv_name}.tmp"
        self._csv_file = open(self._csv_tmp_path, mode="w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(["Time (s)", "Device Time (ms)", "Pressure 1 (mmHg)", "Set Pressure (mmHg)", "Note"])
        self._csv_file.flush()

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self, timeout: float = 1.0) -> None:
        """Stop the writer and finalise files atomically."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

        self._drain_queues()

        if self._video_writer:
            self._video_writer.release()
            self._video_writer = None
        if self._video_tmp_path and self._video_tmp_path.exists():
            if self._video_path:
                self._atomic_replace(self._video_tmp_path, self._video_path)
            else:
                self._video_tmp_path.unlink(missing_ok=True)

        if self._csv_file:
            self._csv_file.flush()
            self._csv_file.close()
            self._csv_file = None
        if self._csv_tmp_path and self._csv_tmp_path.exists():
            if self._csv_path:
                self._atomic_replace(self._csv_tmp_path, self._csv_path)
            else:
                self._csv_tmp_path.unlink(missing_ok=True)

        self._csv_writer = None
        self._video_path = None
        self._video_tmp_path = None
        self._csv_path = None
        self._csv_tmp_path = None

    # Queuing -------------------------------------------------------------------
    def push_frame(self, t: float, frame_no: int, frame) -> None:
        """Queue a frame for writing."""
        payload = (t, frame_no, frame)
        try:
            self._frames.put_nowait(payload)
        except queue.Full:
            try:
                _ = self._frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frames.put_nowait(payload)
            except queue.Full:
                pass

    def push_telemetry(
        self,
        time_s: float,
        device_time_ms: Optional[float],
        pressure_mmHg: Optional[float],
        setpoint_mmHg: Optional[float],
        note: str = "",
    ) -> None:
        """Queue a telemetry row for writing."""
        if (
            note
            and note.startswith("step_")
            and setpoint_mmHg is not None
            and self._last_written_setpoint is not None
            and abs(setpoint_mmHg - self._last_written_setpoint) < 1e-3
            and not self._stale_setpoint_warned
        ):
            logging.warning(
                "Setpoint marker %s arrived but CSV value did not change (%.1f mmHg).",
                note,
                setpoint_mmHg,
            )
            self._stale_setpoint_warned = True

        payload = [time_s, device_time_ms, pressure_mmHg, setpoint_mmHg, note]
        try:
            self._telemetry.put_nowait(payload)
        except queue.Full:
            try:
                _ = self._telemetry.get_nowait()
            except queue.Empty:
                pass
            try:
                self._telemetry.put_nowait(payload)
            except queue.Full:
                pass
        if setpoint_mmHg is not None:
            if (
                self._last_written_setpoint is None
                or abs(setpoint_mmHg - self._last_written_setpoint) >= 1e-3
            ):
                self._stale_setpoint_warned = False
            self._last_written_setpoint = setpoint_mmHg

    def write_metadata(self, metadata: dict, filename: str = "metadata.json") -> None:
        """
        Persist session metadata alongside recordings. Writes atomically via a
        temporary file rename.
        """
        target = self.folder / filename
        tmp = self.folder / f"{filename}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2)
        self._atomic_replace(tmp, target)

    # Internal ------------------------------------------------------------------
    def _loop(self) -> None:
        """
        Worker loop that flushes frame and telemetry queues until stop is
        signaled and both queues are empty.
        """
        while not self._stop_event.is_set() or not self._queues_empty():
            wrote = False
            try:
                t, frame_no, frame = self._frames.get(timeout=0.05)
                if self._video_writer is not None and frame is not None:
                    if self._frame_size and (
                        frame.shape[1], frame.shape[0]
                    ) != self._frame_size:
                        frame = cv2.resize(frame, self._frame_size)
                    self._video_writer.write(frame)
                    wrote = True
            except queue.Empty:
                pass

            try:
                row = self._telemetry.get_nowait()
                if self._csv_writer:
                    self._csv_writer.writerow(row)
                    wrote = True
            except queue.Empty:
                pass

            if wrote and self._csv_file:
                self._csv_file.flush()

    def _queues_empty(self) -> bool:
        return self._frames.empty() and self._telemetry.empty()

    def _drain_queues(self) -> None:
        """Flush any remaining items synchronously (used during shutdown)."""
        while not self._frames.empty():
            try:
                t, frame_no, frame = self._frames.get_nowait()
                if self._video_writer is not None and frame is not None:
                    if self._frame_size and (
                        frame.shape[1], frame.shape[0]
                    ) != self._frame_size:
                        frame = cv2.resize(frame, self._frame_size)
                    self._video_writer.write(frame)
            except queue.Empty:
                break

        while not self._telemetry.empty():
            try:
                row = self._telemetry.get_nowait()
                if self._csv_writer:
                    self._csv_writer.writerow(row)
            except queue.Empty:
                break

        if self._csv_file:
            self._csv_file.flush()

    @staticmethod
    def _atomic_replace(tmp_path: Path, final_path: Path) -> None:
        tmp_path.replace(final_path)

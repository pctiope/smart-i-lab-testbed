from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import cv2
import numpy as np


def _safe_url_for_logging(rtsp_url: str) -> str:
    try:
        parts = urlparse(rtsp_url)
        if parts.username or parts.password:
            host = parts.hostname or "?"
            port = f":{parts.port}" if parts.port else ""
            path = parts.path or ""
            return f"{parts.scheme}://***@{host}{port}{path}"
        return rtsp_url
    except Exception:
        return "<unparseable rtsp url>"


def _make_placeholder_jpeg(message: str) -> bytes:
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    cv2.putText(
        frame,
        message,
        (40, 200),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (200, 200, 200),
        2,
        cv2.LINE_AA,
    )
    success, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    if not success:
        return b""
    return buffer.tobytes()


class RtspFrameGrabber:
    """Background thread that keeps the latest decoded frame from an RTSP feed.

    Reconnects with bounded exponential backoff on read failure. Single-frame
    buffer under a lock; readers grab the most recent frame, MJPEG-encode, and
    stream out. Multiple HTTP clients share the same frame buffer.
    """

    def __init__(self, rtsp_url: str, jpeg_quality: int = 70) -> None:
        if not rtsp_url:
            raise ValueError("rtsp_url is required")
        self._rtsp_url = rtsp_url
        self._jpeg_quality = int(jpeg_quality)
        self._frame_lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._latest_frame_time: float = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._connection_lock = threading.Lock()

    @property
    def public_url(self) -> str:
        return _safe_url_for_logging(self._rtsp_url)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def latest_frame_age(self) -> float | None:
        if self._latest_frame_time == 0.0:
            return None
        return max(0.0, time.time() - self._latest_frame_time)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="rtsp-grabber", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _set_connected(self, value: bool) -> None:
        with self._connection_lock:
            self._connected = value

    def _open_capture(self) -> cv2.VideoCapture | None:
        cap = cv2.VideoCapture(self._rtsp_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            cap = self._open_capture()
            if cap is None:
                self._set_connected(False)
                if self._stop_event.wait(timeout=backoff):
                    return
                backoff = min(backoff * 2.0, 30.0)
                continue
            self._set_connected(True)
            backoff = 1.0
            try:
                while not self._stop_event.is_set():
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        break
                    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self._jpeg_quality]
                    success, buffer = cv2.imencode(".jpg", frame, encode_params)
                    if not success:
                        continue
                    jpeg_bytes = buffer.tobytes()
                    with self._frame_lock:
                        self._latest_jpeg = jpeg_bytes
                        self._latest_frame_time = time.time()
            finally:
                cap.release()
            self._set_connected(False)
            if self._stop_event.wait(timeout=backoff):
                return
            backoff = min(backoff * 2.0, 30.0)

    def latest_jpeg(self) -> bytes | None:
        with self._frame_lock:
            return self._latest_jpeg

    def mjpeg_iterator(self, target_fps: float = 10.0) -> Iterator[bytes]:
        boundary = b"--frame"
        delay = max(0.02, 1.0 / max(target_fps, 1.0))
        last_sent_time: float = 0.0
        last_sent_id: int = 0
        placeholder = self._make_placeholder_jpeg()
        while not self._stop_event.is_set():
            with self._frame_lock:
                jpeg = self._latest_jpeg
                frame_time = self._latest_frame_time
            if jpeg is None:
                jpeg = placeholder
                frame_time = time.time()
            if id(jpeg) != last_sent_id or (frame_time - last_sent_time) > delay:
                last_sent_id = id(jpeg)
                last_sent_time = frame_time
                header = (
                    boundary
                    + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg)).encode("ascii")
                    + b"\r\n\r\n"
                )
                yield header + jpeg + b"\r\n"
            time.sleep(delay)

    def _make_placeholder_jpeg(self) -> bytes:
        return _make_placeholder_jpeg("Connecting to RTSP...")


class FileBackedMjpegGrabber:
    """MJPEG source backed by a latest-frame JPEG written by the YOLO tracker."""

    def __init__(self, jpeg_path: str | Path, max_age_sec: float = 30.0) -> None:
        if not jpeg_path:
            raise ValueError("jpeg_path is required")
        self._jpeg_path = Path(jpeg_path)
        self._max_age_sec = float(max_age_sec)
        self._frame_lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._latest_mtime_ns: int | None = None
        self._stop_event = threading.Event()
        self._placeholder_jpeg = _make_placeholder_jpeg("Waiting for YOLO frames...")

    @property
    def public_url(self) -> str:
        return str(self._jpeg_path)

    @property
    def connected(self) -> bool:
        age = self.latest_frame_age
        return age is not None and age <= self._max_age_sec

    @property
    def latest_frame_age(self) -> float | None:
        try:
            mtime = self._jpeg_path.stat().st_mtime
        except OSError:
            return None
        return max(0.0, time.time() - mtime)

    def start(self) -> None:
        self._stop_event.clear()

    def stop(self) -> None:
        self._stop_event.set()

    def latest_jpeg(self) -> bytes | None:
        try:
            stat_result = self._jpeg_path.stat()
        except OSError:
            return None
        if max(0.0, time.time() - stat_result.st_mtime) > self._max_age_sec:
            return None
        with self._frame_lock:
            if self._latest_mtime_ns != stat_result.st_mtime_ns:
                try:
                    self._latest_jpeg = self._jpeg_path.read_bytes()
                    self._latest_mtime_ns = stat_result.st_mtime_ns
                except OSError:
                    return None
            return self._latest_jpeg

    def mjpeg_iterator(self, target_fps: float = 10.0) -> Iterator[bytes]:
        boundary = b"--frame"
        delay = max(0.02, 1.0 / max(target_fps, 1.0))
        last_sent_key = object()
        while not self._stop_event.is_set():
            jpeg = self.latest_jpeg()
            frame_key = self._latest_mtime_ns if jpeg is not None else None
            if frame_key != last_sent_key:
                if jpeg is None:
                    jpeg = self._placeholder_jpeg
                header = (
                    boundary
                    + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg)).encode("ascii")
                    + b"\r\n\r\n"
                )
                last_sent_key = frame_key
                yield header + jpeg + b"\r\n"
            time.sleep(delay)


class DisabledFrameGrabber:
    """MJPEG-compatible placeholder for offline replay packages without RTSP."""

    def __init__(self, message: str = "RTSP disabled") -> None:
        self._message = message
        self._latest_jpeg = self._make_placeholder_jpeg()

    @property
    def public_url(self) -> str:
        return "disabled"

    @property
    def connected(self) -> bool:
        return False

    @property
    def latest_frame_age(self) -> float | None:
        return None

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def latest_jpeg(self) -> bytes | None:
        return self._latest_jpeg

    def mjpeg_iterator(self, target_fps: float = 10.0) -> Iterator[bytes]:
        boundary = b"--frame"
        delay = max(0.02, 1.0 / max(target_fps, 1.0))
        jpeg = self._latest_jpeg
        header = (
            boundary
            + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(jpeg)).encode("ascii")
            + b"\r\n\r\n"
        )
        while True:
            yield header + jpeg + b"\r\n"
            time.sleep(delay)

    def _make_placeholder_jpeg(self) -> bytes:
        return _make_placeholder_jpeg(self._message)



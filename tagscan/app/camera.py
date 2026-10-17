"""Frame sources: the device camera (Qt Multimedia) and a folder of photos for testing.

The camera backend hands out frames on its own capture thread. ``CameraSource`` converts them
right there (mapped planes -> OpenCV, no Qt image conversion, no GPU round trip) and keeps
only the newest one in a one-slot mailbox for the GUI thread. A plain queued signal/slot
would instead park every frame in the GUI event queue; on a tablet that cannot keep up with
30 fps of 1920x1440 that queue, and the process memory with it, grows without bound.
"""
from __future__ import annotations

import glob
import logging
import os
import threading
import time

import cv2
import numpy as np
from PySide6.QtCore import QObject, Signal, QTimer, Qt
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import (QCamera, QMediaCaptureSession, QMediaDevices, QVideoSink, QVideoFrame,
                                  QVideoFrameFormat)

log = logging.getLogger("tagscan.camera")
PF = QVideoFrameFormat.PixelFormat


# ---------------------------------------------------------------------------- image helpers
def qimage_to_bgr(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format.Format_BGR888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), np.uint8, count=bpl * h).reshape(h, bpl)[:, : w * 3].reshape(h, w, 3)
    return buf.copy()               # the QImage dies with this function: never hand out a view


def bgr_to_qimage(bgr: np.ndarray) -> QImage:
    bgr = np.ascontiguousarray(bgr)
    h, w = bgr.shape[:2]
    return QImage(bgr.data, w, h, 3 * w, QImage.Format.Format_BGR888).copy()


# ---------------------------------------------------------------------------- frame conversion
# Qt names its packed RGB video formats by byte order (BGRA8888 = bytes B,G,R,A), unlike QImage.
_SEMI = {PF.Format_NV12: cv2.COLOR_YUV2BGR_NV12, PF.Format_NV21: cv2.COLOR_YUV2BGR_NV21}
_PLANAR = {PF.Format_YUV420P: cv2.COLOR_YUV2BGR_I420, PF.Format_YV12: cv2.COLOR_YUV2BGR_YV12}
_PACKED422 = {PF.Format_YUYV: cv2.COLOR_YUV2BGR_YUYV, PF.Format_UYVY: cv2.COLOR_YUV2BGR_UYVY}
_RGB32_CV = {PF.Format_BGRA8888: cv2.COLOR_BGRA2BGR, PF.Format_BGRX8888: cv2.COLOR_BGRA2BGR,
             PF.Format_BGRA8888_Premultiplied: cv2.COLOR_BGRA2BGR,
             PF.Format_RGBA8888: cv2.COLOR_RGBA2BGR, PF.Format_RGBX8888: cv2.COLOR_RGBA2BGR}
_RGB32_ORDER = {PF.Format_ARGB8888: (3, 2, 1), PF.Format_XRGB8888: (3, 2, 1),
                PF.Format_ARGB8888_Premultiplied: (3, 2, 1),
                PF.Format_ABGR8888: (1, 2, 3), PF.Format_XBGR8888: (1, 2, 3)}
NATIVE_FORMATS = (set(_SEMI) | set(_PLANAR) | set(_PACKED422) | set(_RGB32_CV) | set(_RGB32_ORDER)
                  | {PF.Format_Y8, PF.Format_Jpeg})


def _plane(frame: QVideoFrame, i: int, rows: int, cols: int):
    """One mapped plane as a (rows, cols) uint8 view honouring the line stride."""
    bpl = frame.bytesPerLine(i)
    buf = np.frombuffer(frame.bits(i), np.uint8)
    if bpl < cols or buf.size < bpl * (rows - 1) + cols:
        return None
    return np.lib.stride_tricks.as_strided(buf, shape=(rows, cols), strides=(bpl, 1))


def _convert_mapped(frame: QVideoFrame, pf, w: int, h: int):
    if pf in _SEMI:
        y, uv = _plane(frame, 0, h, w), _plane(frame, 1, h // 2, w)
        if y is None or uv is None:
            return None
        return cv2.cvtColor(np.vstack((y, uv)), _SEMI[pf])
    if pf in _PLANAR:
        planes = [_plane(frame, 0, h, w), _plane(frame, 1, h // 2, w // 2), _plane(frame, 2, h // 2, w // 2)]
        if any(p is None for p in planes):
            return None
        yuv = np.concatenate([p.reshape(-1) for p in planes]).reshape(h * 3 // 2, w)
        return cv2.cvtColor(yuv, _PLANAR[pf])
    if pf in _PACKED422:
        p = _plane(frame, 0, h, 2 * w)
        return None if p is None else cv2.cvtColor(np.ascontiguousarray(p).reshape(h, w, 2), _PACKED422[pf])
    if pf in _RGB32_CV:
        p = _plane(frame, 0, h, 4 * w)
        return None if p is None else cv2.cvtColor(np.ascontiguousarray(p).reshape(h, w, 4), _RGB32_CV[pf])
    if pf in _RGB32_ORDER:
        p = _plane(frame, 0, h, 4 * w)
        return None if p is None else np.ascontiguousarray(np.ascontiguousarray(p).reshape(h, w, 4)[:, :, list(_RGB32_ORDER[pf])])
    if pf == PF.Format_Y8:
        p = _plane(frame, 0, h, w)
        return None if p is None else cv2.cvtColor(np.ascontiguousarray(p), cv2.COLOR_GRAY2BGR)
    if pf == PF.Format_Jpeg:
        data = np.frombuffer(frame.bits(0), np.uint8, count=frame.mappedBytes(0))
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    return None


def _degrees(rot) -> int:
    try:
        return int(rot.value)
    except Exception:
        try:
            return int(rot)
        except Exception:
            return 0


def surface_transform(frame: QVideoFrame) -> tuple[int, bool, bool]:
    """(clockwise degrees, mirror horizontally, flip vertically) from the surface format. Qt
    applies these in QVideoFrame.toImage() and when presenting, in exactly this order:
    mirror, then rotate, then the bottom-to-top scan-line flip (checked against toImage)."""
    fmt = frame.surfaceFormat()
    flip = False
    try:
        flip = fmt.scanLineDirection() == QVideoFrameFormat.Direction.BottomToTop
    except Exception:
        pass
    return _degrees(fmt.rotation()) % 360, bool(fmt.isMirrored()), flip


def presentation_transform(frame: QVideoFrame) -> tuple[int, bool, bool]:
    """Per-frame hints (Qt >= 6.7: QVideoFrame.rotation()/mirrored()). toImage() ignores them,
    the video sinks apply them when presenting, so we do too."""
    angle = _degrees(frame.rotation()) if hasattr(frame, "rotation") else 0
    mirror = bool(frame.mirrored()) if hasattr(frame, "mirrored") else False
    return angle % 360, mirror, False


def frame_transform(frame: QVideoFrame) -> tuple[tuple[int, bool, bool], tuple[int, bool, bool]]:
    return surface_transform(frame), presentation_transform(frame)


def apply_transform(bgr: np.ndarray, angle: int, mirror: bool, flip: bool = False) -> np.ndarray:
    if mirror:
        bgr = cv2.flip(bgr, 1)
    if angle == 90:
        bgr = cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
    elif angle == 180:
        bgr = cv2.rotate(bgr, cv2.ROTATE_180)
    elif angle == 270:
        bgr = cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if flip:
        bgr = cv2.flip(bgr, 0)
    return bgr


def frame_to_bgr(frame: QVideoFrame) -> np.ndarray | None:
    """QVideoFrame -> BGR array. Maps the planes and converts with OpenCV; falls back to
    QVideoFrame.toImage() (slow: Qt tries a GPU round trip) for formats not handled here."""
    pf = frame.pixelFormat()
    w, h = frame.width(), frame.height()
    if pf in NATIVE_FORMATS and w % 2 == 0 and h % 2 == 0 and frame.map(QVideoFrame.MapMode.ReadOnly):
        try:
            bgr = _convert_mapped(frame, pf, w, h)
        except Exception:
            log.exception("mapped frame conversion failed (%s), using toImage", pf)
            bgr = None
        finally:
            frame.unmap()
        if bgr is not None:
            return apply_transform(apply_transform(bgr, *surface_transform(frame)), *presentation_transform(frame))
    img = frame.toImage()                      # applies the surface transform itself
    return None if img.isNull() else apply_transform(qimage_to_bgr(img), *presentation_transform(frame))


# ---------------------------------------------------------------------------- sources
class FrameSource(QObject):
    frame = Signal(object)          # numpy BGR array
    status = Signal(str)

    def start(self) -> None: ...
    def stop(self) -> None: ...


class CameraSource(FrameSource):
    """Live frames from a QCamera. Picks a rear camera when there is one.

    ``videoFrameChanged`` is connected *directly*, so ``_on_sink_frame`` runs on whatever thread
    the backend produces frames on and does the conversion there. The result lands in a one-slot
    mailbox; the GUI thread is woken by a payload-free queued signal (at most one in flight) and
    takes the newest frame. Frames that arrive while the GUI still holds the previous one are
    dropped, never queued. Conversions are also capped at ``max_fps``.
    """
    _wake = Signal()

    def __init__(self, parent=None, max_width: int = 1920, max_fps: float = 20.0):
        super().__init__(parent)
        self.session = QMediaCaptureSession(self)
        self.sink = QVideoSink(self)
        self.session.setVideoSink(self.sink)
        self.camera: QCamera | None = None
        self.max_width = max_width
        self.min_interval = 1.0 / max(1.0, max_fps)
        self.format_text = ""
        self._lock = threading.Lock()
        self._latest = None
        self._wake_pending = False
        self._last_conv = 0.0
        self._logged = False
        self.dropped = 0            # frames skipped because the GUI had not taken the previous one
        self.delivered = 0
        self._wake.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self.sink.videoFrameChanged.connect(self._on_sink_frame, Qt.ConnectionType.DirectConnection)

    @staticmethod
    def devices():
        return QMediaDevices.videoInputs()

    @staticmethod
    def preferred_index(devices) -> int:
        for i, d in enumerate(devices):
            desc = d.description().lower()
            if any(k in desc for k in ("back", "rear", "world", "rück")):
                return i
        for i, d in enumerate(devices):
            if d.position() == d.Position.BackFace:
                return i
        return 0 if devices else -1

    def select(self, index: int) -> str:
        devices = self.devices()
        if not devices:
            self.status.emit("no camera found")
            return ""
        index = max(0, min(index, len(devices) - 1))
        dev = devices[index]
        if self.camera:
            self.camera.stop()
            self.camera.deleteLater()
        with self._lock:
            self._latest = None
        self.camera = QCamera(dev, self)
        fmt = self._pick_format(dev)
        if fmt is not None:
            self.camera.setCameraFormat(fmt)
        self.camera.errorOccurred.connect(lambda err, msg: self.status.emit(f"camera error: {msg}"))
        self.session.setCamera(self.camera)
        self.camera.start()
        f = self.camera.cameraFormat()
        res = f.resolution()
        self.format_text = f"{res.width()}x{res.height()} {_pf_name(f.pixelFormat())} {f.maxFrameRate():.0f} fps"
        log.info("camera %r: %s (of %d formats)", dev.description(), self.format_text, len(dev.videoFormats()))
        self.status.emit(f"{dev.description()} · {self.format_text}")
        return dev.description()

    def _pick_format(self, dev):
        """Largest frame up to max_width; among equals prefer >= 15 fps, a format converted
        natively here (no JPEG decode), then the higher frame rate."""
        best, best_key = None, None
        for f in dev.videoFormats():
            r = f.resolution()
            if r.width() > self.max_width or r.width() < 640:
                continue
            pf = f.pixelFormat()
            fps = f.maxFrameRate()
            key = (fps >= 15, r.width() * r.height(), pf in NATIVE_FORMATS and pf != PF.Format_Jpeg, fps)
            if best_key is None or key > best_key:
                best, best_key = f, key
        return best

    def start(self) -> None:
        if self.camera is None:
            self.select(self.preferred_index(self.devices()))
        elif not self.camera.isActive():
            self.camera.start()

    def stop(self) -> None:
        if self.camera:
            self.camera.stop()
        with self._lock:
            self._latest = None

    # -- capture thread ----------------------------------------------------------------------
    def _on_sink_frame(self, frame: QVideoFrame) -> None:
        now = time.perf_counter()           # not monotonic(): 15.6 ms ticks on Windows before Python 3.13
        if not frame.isValid() or now - self._last_conv < self.min_interval:
            return
        with self._lock:
            if self._latest is not None:
                self.dropped += 1
                return
        try:
            bgr = frame_to_bgr(frame)
        except Exception:
            log.exception("frame conversion failed")
            return
        if bgr is None:
            return
        if not self._logged:
            self._logged = True
            native = frame.pixelFormat() in NATIVE_FORMATS
            log.info("frames: %s %dx%d, transform %s, %s path, converting on %s thread",
                     _pf_name(frame.pixelFormat()), frame.width(), frame.height(), frame_transform(frame),
                     "mapped" if native else "toImage",
                     "the GUI" if threading.current_thread() is threading.main_thread() else "a capture")
        self._last_conv = now
        with self._lock:
            self._latest = bgr
            wake, self._wake_pending = not self._wake_pending, True
        if wake:
            self._wake.emit()

    # -- GUI thread --------------------------------------------------------------------------
    def _deliver(self) -> None:
        with self._lock:
            bgr, self._latest, self._wake_pending = self._latest, None, False
        if bgr is not None:
            self.delivered += 1
            self.frame.emit(bgr)


def _pf_name(pf) -> str:
    try:
        return pf.name.replace("Format_", "")
    except Exception:
        return str(pf)


class FolderSource(FrameSource):
    """Replays photos from a folder, one every ``interval`` ms, each repeated ``repeat`` times
    so the two-read vote can happen. For testing the engine without a camera."""

    def __init__(self, folder: str, interval_ms: int = 700, repeat: int = 3, loop: bool = False, parent=None):
        super().__init__(parent)
        exts = (".jpg", ".jpeg", ".png", ".bmp")
        self.files = sorted(f for f in glob.glob(os.path.join(folder, "*")) if f.lower().endswith(exts))
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self._tick)
        self.repeat = repeat
        self.loop = loop
        self._i = 0
        self._n = 0
        self._cur = None
        self.paused = False        # the window pauses replay while a card waits for the user

    def start(self) -> None:
        if not self.files:
            self.status.emit("no images in folder")
            return
        self.status.emit(f"photos: {len(self.files)} files")
        self.timer.start()
        self._tick()

    def stop(self) -> None:
        self.timer.stop()

    def _tick(self) -> None:
        if self.paused:
            return
        if self._cur is None or self._n >= self.repeat:
            if self._i >= len(self.files):
                if not self.loop:
                    self.timer.stop()
                    self.status.emit("photos: done")
                    return
                self._i = 0
            self._cur = cv2.imread(self.files[self._i])
            self.status.emit(f"photo {self._i + 1}/{len(self.files)}: {os.path.basename(self.files[self._i])}")
            self._i += 1
            self._n = 0
        self._n += 1
        if self._cur is not None:
            self.frame.emit(self._cur)

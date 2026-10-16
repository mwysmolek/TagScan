"""Frame sources: the device camera (Qt Multimedia) and a folder of photos for testing."""
from __future__ import annotations

import glob
import os

import numpy as np
from PySide6.QtCore import QObject, Signal, QTimer, Qt
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoSink, QVideoFrame


def qimage_to_bgr(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), np.uint8, count=bpl * h).reshape(h, bpl)[:, : w * 3].reshape(h, w, 3)
    return np.ascontiguousarray(buf[:, :, ::-1])


def bgr_to_qimage(bgr: np.ndarray) -> QImage:
    rgb = np.ascontiguousarray(bgr[:, :, ::-1])
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


class FrameSource(QObject):
    frame = Signal(object)          # numpy BGR array
    status = Signal(str)

    def start(self) -> None: ...
    def stop(self) -> None: ...


class CameraSource(FrameSource):
    """Live frames from a QCamera. Picks a rear camera when there is one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = QMediaCaptureSession(self)
        self.sink = QVideoSink(self)
        self.session.setVideoSink(self.sink)
        self.camera: QCamera | None = None
        self.sink.videoFrameChanged.connect(self._on_frame)
        self._busy = False
        self.max_width = 1920

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
        self.camera = QCamera(dev, self)
        fmt = self._pick_format(dev)
        if fmt is not None:
            self.camera.setCameraFormat(fmt)
        self.camera.errorOccurred.connect(lambda err, msg: self.status.emit(f"camera error: {msg}"))
        self.session.setCamera(self.camera)
        self.camera.start()
        res = self.camera.cameraFormat().resolution()
        self.status.emit(f"{dev.description()} {res.width()}x{res.height()}")
        return dev.description()

    def _pick_format(self, dev):
        best, best_key = None, None
        for f in dev.videoFormats():
            r = f.resolution()
            if r.width() > self.max_width or r.width() < 640:
                continue
            key = (r.width() * r.height(), f.maxFrameRate())
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

    def _on_frame(self, frame: QVideoFrame) -> None:
        if self._busy or not frame.isValid():
            return
        self._busy = True
        try:
            img = frame.toImage()
            if img.isNull():
                return
            # the sink reports the needed rotation separately on some backends
            try:
                rot = frame.rotation()
                angle = int(rot) if isinstance(rot, int) else int(rot.value)
            except Exception:
                angle = 0
            bgr = qimage_to_bgr(img)
            if angle in (90, 180, 270):
                bgr = np.ascontiguousarray(np.rot90(bgr, -angle // 90))
            self.frame.emit(bgr)
        finally:
            self._busy = False


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
        import cv2
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

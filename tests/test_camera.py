"""Frame conversion and the camera mailbox, with synthetic QVideoFrames (no camera needed)."""
import os
import threading
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, QCoreApplication
from PySide6.QtWidgets import QApplication
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat

from tagscan.app.camera import (CameraSource, NATIVE_FORMATS, bgr_to_qimage, frame_to_bgr, qimage_to_bgr,
                                frame_transform)

PF = QVideoFrameFormat.PixelFormat


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _test_bgr(w=96, h=64):
    rng = np.random.default_rng(1)
    img = rng.integers(0, 256, (h, w, 3), np.uint8)
    img = __import__("cv2").GaussianBlur(img, (7, 7), 0)     # smooth: chroma subsampling then costs little
    return img


def _fill(frame: QVideoFrame, pf, bgr) -> bool:
    """Write ``bgr`` into a mapped frame in the frame's own pixel format."""
    import cv2
    h, w = bgr.shape[:2]
    if not frame.map(QVideoFrame.MapMode.WriteOnly):
        return False
    try:
        def plane(i):
            return np.frombuffer(frame.bits(i), np.uint8), frame.bytesPerLine(i)

        def put(i, rows, data):          # data: (rows, cols) -> plane i honouring the stride
            buf, bpl = plane(i)
            cols = data.shape[1]
            view = np.lib.stride_tricks.as_strided(buf, shape=(rows, cols), strides=(bpl, 1))
            view[:] = data

        if pf in (PF.Format_NV12, PF.Format_NV21):
            i420 = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)        # (h*3/2, w): Y, U(h/4 rows), V
            y, u, v = i420[:h], i420[h:h + h // 4].reshape(h // 2, w // 2), i420[h + h // 4:].reshape(h // 2, w // 2)
            uv = np.empty((h // 2, w), np.uint8)
            a, b = (u, v) if pf == PF.Format_NV12 else (v, u)
            uv[:, 0::2], uv[:, 1::2] = a, b
            put(0, h, y)
            put(1, h // 2, uv)
        elif pf in (PF.Format_YUV420P, PF.Format_YV12):
            i420 = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
            y, u, v = i420[:h], i420[h:h + h // 4].reshape(h // 2, w // 2), i420[h + h // 4:].reshape(h // 2, w // 2)
            put(0, h, y)
            p1, p2 = (u, v) if pf == PF.Format_YUV420P else (v, u)
            put(1, h // 2, p1)
            put(2, h // 2, p2)
        elif pf in (PF.Format_YUYV, PF.Format_UYVY):
            yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV)
            y, u, v = yuv[:, :, 0], yuv[:, 0::2, 1], yuv[:, 0::2, 2]
            packed = np.empty((h, w, 2), np.uint8)
            if pf == PF.Format_YUYV:
                packed[:, :, 0] = y
                packed[:, 0::2, 1], packed[:, 1::2, 1] = u, v
            else:
                packed[:, :, 1] = y
                packed[:, 0::2, 0], packed[:, 1::2, 0] = u, v
            put(0, h, packed.reshape(h, 2 * w))
        elif pf == PF.Format_Y8:
            put(0, h, cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
        elif pf == PF.Format_Jpeg:
            ok, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            buf, _ = plane(0)
            assert enc.size <= buf.size
            buf[:enc.size] = enc.reshape(-1)
        else:                       # 32-bit packed: Qt's names give the byte order
            order = {PF.Format_BGRA8888: "BGRA", PF.Format_BGRX8888: "BGRX", PF.Format_BGRA8888_Premultiplied: "BGRA",
                     PF.Format_RGBA8888: "RGBA", PF.Format_RGBX8888: "RGBX",
                     PF.Format_ARGB8888: "ARGB", PF.Format_XRGB8888: "XRGB", PF.Format_ARGB8888_Premultiplied: "ARGB",
                     PF.Format_ABGR8888: "ABGR", PF.Format_XBGR8888: "XBGR"}[pf]
            chan = {"B": bgr[:, :, 0], "G": bgr[:, :, 1], "R": bgr[:, :, 2]}
            px = np.empty((h, w, 4), np.uint8)
            for k, c in enumerate(order):
                px[:, :, k] = chan.get(c, 255)
            put(0, h, px.reshape(h, 4 * w))
    finally:
        frame.unmap()
    return True


@pytest.mark.parametrize("pf", sorted(NATIVE_FORMATS, key=lambda p: p.name))
def test_frame_to_bgr_matches_qt(app, pf):
    bgr = _test_bgr()
    h, w = bgr.shape[:2]
    frame = QVideoFrame(QVideoFrameFormat(QSize(w, h), pf))
    assert frame.isValid() and _fill(frame, pf, bgr)
    out = frame_to_bgr(frame)
    assert out is not None and out.shape == (h, w, 3) and out.dtype == np.uint8
    ref = qimage_to_bgr(frame.toImage())            # Qt's own conversion is the reference
    tol = 2 if pf in (PF.Format_Y8,) or "8888" in pf.name else 12   # YUV paths round differently
    diff = np.abs(out.astype(int) - ref.astype(int))
    assert diff.max() <= tol, f"{pf.name}: max diff {diff.max()} vs Qt"
    # and both are close to the source picture (chroma subsampling / JPEG are lossy)
    assert np.abs(out.astype(int) - bgr.astype(int)).mean() < 6 or pf == PF.Format_Y8


def _rgb32_frame(bgr, fmt):
    h, w = bgr.shape[:2]
    frame = QVideoFrame(fmt)
    assert _fill(frame, PF.Format_BGRA8888, bgr)
    return frame


def _rotation_enum(deg):
    from PySide6.QtMultimedia import QtVideo
    return {0: QtVideo.Rotation.None_, 90: QtVideo.Rotation.Clockwise90,
            180: QtVideo.Rotation.Clockwise180, 270: QtVideo.Rotation.Clockwise270}[deg]


@pytest.mark.parametrize("deg", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("flip", [False, True])
def test_surface_transform_matches_qt(app, deg, mirror, flip):
    """Rotation, mirroring and scan-line direction from the surface format come out exactly as
    Qt's own toImage() applies them (SURFACE_ORDER: changed in Qt 6.11.3)."""
    bgr = _test_bgr(64, 32)
    fmt = QVideoFrameFormat(QSize(64, 32), PF.Format_BGRA8888)
    fmt.setRotation(_rotation_enum(deg))
    fmt.setMirrored(mirror)
    fmt.setScanLineDirection(QVideoFrameFormat.Direction.BottomToTop if flip else QVideoFrameFormat.Direction.TopToBottom)
    frame = _rgb32_frame(bgr, fmt)
    assert frame_transform(frame)[0] == (deg, mirror, flip)
    out, ref = frame_to_bgr(frame), qimage_to_bgr(frame.toImage())
    assert out.shape == ref.shape and np.array_equal(out, ref)
    assert out.shape[:2] == ((64, 32) if deg in (90, 270) else (32, 64))


def test_presentation_hints_applied_on_top(app):
    """Per-frame rotation/mirror hints are not in toImage(); we apply them like a video sink."""
    bgr = _test_bgr(64, 32)
    frame = _rgb32_frame(bgr, QVideoFrameFormat(QSize(64, 32), PF.Format_BGRA8888))
    if not hasattr(frame, "setRotation"):
        pytest.skip("PySide6 < 6.7")
    frame.setRotation(_rotation_enum(90))
    frame.setMirrored(True)
    assert frame_transform(frame) == ((0, False, False), (90, True, False))
    out = frame_to_bgr(frame)
    import cv2
    expect = cv2.rotate(cv2.flip(qimage_to_bgr(frame.toImage()), 1), cv2.ROTATE_90_CLOCKWISE)
    assert out.shape == (64, 32, 3) and np.array_equal(out, expect)


def test_qimage_roundtrip(app):
    bgr = _test_bgr(50, 30)
    img = bgr_to_qimage(bgr)
    assert img.width() == 50 and img.height() == 30
    back = qimage_to_bgr(img)
    assert np.array_equal(back, bgr)
    assert back.flags["OWNDATA"] or back.base is None or not np.shares_memory(back, img.constBits() if False else back)


def test_mailbox_drops_instead_of_queueing(app):
    """Flood the capture-thread slot faster than the GUI consumes: the GUI gets at most one frame
    per wake, nothing piles up, and the extra frames are counted as dropped."""
    src = CameraSource(max_fps=1e9)          # no rate cap: exercise the mailbox itself
    received = []

    def slow_consumer(bgr):
        received.append(bgr.shape)
        time.sleep(0.01)                       # a slow GUI
    src.frame.connect(slow_consumer)

    bgr = _test_bgr(64, 48)
    frames = []
    for _ in range(4):
        f = QVideoFrame(QVideoFrameFormat(QSize(64, 48), PF.Format_NV12))
        assert _fill(f, PF.Format_NV12, bgr)
        frames.append(f)
    n = 300

    def producer():
        for i in range(n):
            src._on_sink_frame(frames[i % 4])  # the backend thread calling the direct slot
    t = threading.Thread(target=producer)
    t.start()
    deadline = time.time() + 5
    while (t.is_alive() or src._latest is not None) and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.001)
    t.join()
    QCoreApplication.processEvents()
    assert received and all(s == (48, 64, 3) for s in received)
    assert src.delivered == len(received)
    assert src.dropped > 0, "no frame was dropped: is the rate cap swallowing calls (coarse clock)?"
    assert src.delivered + src.dropped <= n
    assert src.delivered < n // 2, "frames were queued rather than dropped"
    assert src._latest is None and not src._wake_pending

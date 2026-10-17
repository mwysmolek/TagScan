"""The aim-box flow end to end through the real window and the real OCR engine, offscreen:
frames are pushed in as the camera would, decisions are read back from the store."""
import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QSettings
from PySide6.QtWidgets import QApplication

from tagscan.catalog import Catalog, Item
from tagscan.profile import Profile

pytestmark = pytest.mark.skipif(os.environ.get("TAGSCAN_NO_OCR") == "1", reason="OCR disabled")


def _pump(cond, timeout=40.0, step=None):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if step:
            step()
        QCoreApplication.processEvents()
        time.sleep(0.02)
        if cond():
            return True
    return False


def _label_frame(number, w=1920, h=1440):
    """A full-size frame with a label inside the default aim box (60 % wide, centred)."""
    import cv2
    from tagscan.app.main import synthetic_label
    import tempfile
    d = tempfile.mkdtemp()
    synthetic_label(os.path.join(d, "l.png"), number)
    lab = cv2.imread(os.path.join(d, "l.png"))                  # 900x600, label at 150..750 x 150..450
    lab = lab[140:460, 130:770]                                 # just the label: 640x320
    frame = np.full((h, w, 3), 150, np.uint8)
    x0, y0 = (w - lab.shape[1]) // 2, (h - lab.shape[0]) // 2
    frame[y0:y0 + lab.shape[0], x0:x0 + lab.shape[1]] = lab
    return frame


def _textless_frame(w=1920, h=1440, seed=3):
    """Something with contrast but no text in the box (a machine surface)."""
    rng = np.random.default_rng(seed)
    frame = np.full((h, w, 3), 160, np.uint8)
    for _ in range(12):
        x, y = rng.integers(400, 1400), rng.integers(450, 900)
        frame[y:y + 40, x:x + 120] = rng.integers(20, 90)
    return frame


def _qr_frame(text, base, size=260):
    """``base`` with a QR code in the top-left corner, well outside the aim box."""
    import cv2
    import zxingcpp
    if hasattr(zxingcpp, "create_barcode"):
        img = np.array(zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode), scale=6))
    else:
        img = np.array(zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, text, width=size, height=size))
    img = cv2.resize(img, (size, size), interpolation=cv2.INTER_NEAREST)
    frame = base.copy()
    frame[40:40 + size, 40:40 + size] = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return frame


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_box_flow_accept_unreadable_undo(app, tmp_path):
    from tagscan.app.mainwindow import MainWindow
    from tagscan.app.camera import CameraSource
    cat = Catalog(items={"123456": Item("123456", "123456", "item one"), "234567": Item("234567", "234567", "item two")})
    prof = Profile()
    prof.auto_confirm_seconds = 0.2
    prof.ocr_interval = 0.0
    prof.ocr_fail_passes = 3
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    win = MainWindow(profile=prof, catalog=cat, data_dir=str(tmp_path / "data"), settings=settings)
    win.show()
    assert _pump(lambda: isinstance(win.source, CameraSource), 10), "camera source not set up"
    assert win._box_active
    assert _pump(lambda: "ready" in win.engine_lbl.text() or "unavailable" in win.engine_lbl.text(), 60)
    assert "ready" in win.engine_lbl.text(), win.engine_lbl.text()

    # 1. a label held steady in the box is read from the crop and auto-accepted
    frame = _label_frame("123456")
    assert _pump(lambda: win.store.count() >= 1, 40, step=lambda: win._on_frame(frame)), win.card.head.text()
    scan = win.store.recent(1)[0]
    assert scan.number == "123456" and scan.source == "OCR" and scan.in_list
    row = win.store.db.execute("SELECT outcome, ocr_number, crop, roi, photo FROM reads ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "accepted" and row[1] == "123456" and row[2] and '"w"' in row[3] and row[4].startswith("photos/")
    assert (tmp_path / "data" / "training" / "crops" / row[2]).exists()
    assert win._need_change                                    # the same picture is not read again

    # 2. textless content held steady: after three empty passes the "type it" screen appears
    blank = _textless_frame()
    assert _pump(lambda: win.card.state == "input", 40, step=lambda: win._on_frame(blank)), win.card.head.text()
    assert "Could not read" in win.card.head.text() and not win.card.pic.isHidden()
    win.card._skip()                                             # Back
    QCoreApplication.processEvents()
    assert win.card.state == "idle"
    assert win.store.db.execute("SELECT outcome FROM reads ORDER BY id DESC LIMIT 1").fetchone()[0] == "back"

    # 2b. the type-it screen again, then the operator aims at a QR *outside* the box: the
    #     whole-frame code scan resolves it and dismisses the screen
    blank2 = _textless_frame(seed=5)
    assert _pump(lambda: win.card.state == "input", 40, step=lambda: win._on_frame(blank2)), win.card.head.text()
    qr_frame = _qr_frame("234567", blank2)
    assert _pump(lambda: win.card.state == "accept", 20, step=lambda: win._on_frame(qr_frame)), win.card.head.text()
    assert win.card.pending["number"] == "234567" and win.card.pending["source"] == "QR"
    win.card._ok("confirmed")
    QCoreApplication.processEvents()
    scan = win.store.recent(1)[0]
    assert scan.number == "234567" and scan.source == "QR"
    row = win.store.db.execute("SELECT outcome, source, crop FROM reads ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "confirmed" and row[1] == "QR" and row[2] == ""      # whole-frame hit: no crop
    win.worker.scanner.suppress("234567", 0.0)                           # forget it for step 3

    # 3. another label: accept, Undo -> type-it with the wrong read shown, retype the right number
    frame2 = _label_frame("234567")
    assert _pump(lambda: win.card.state == "review", 40, step=lambda: win._on_frame(frame2)), win.card.head.text()
    assert win.card.pending["number"] == "234567" and "already scanned" in win.card.head.text()
    win.card._skip()                                             # Skip: stays unrecorded as a scan
    QCoreApplication.processEvents()
    win.store.db.execute("DELETE FROM scans WHERE number='234567'")
    win.store.db.commit()
    win.worker.scanner.suppress("234567", 0.0)
    win._need_change = False
    win._stab.reset()
    assert _pump(lambda: win.card.state == "accept", 40, step=lambda: win._on_frame(frame2)), win.card.head.text()
    assert win.card.pending["number"] == "234567"
    win.card._undo()
    QCoreApplication.processEvents()
    assert win.card.state == "input" and "Wrong read" in win.card.head.text() and win.card.number.text() == "234567"
    assert win.store.db.execute("SELECT outcome FROM reads ORDER BY id DESC LIMIT 1").fetchone()[0] == "undone"
    for k in "123456":
        win.card.pad._press(k)
    win.card.pad._press("✓")
    QCoreApplication.processEvents()
    scan = win.store.recent(1)[0]
    assert scan.number == "123456" and scan.source == "MANUAL" and scan.note == "undo"
    row = win.store.db.execute("SELECT outcome, ocr_number, final_number, crop FROM reads ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "typed" and row[1] == "234567" and row[2] == "123456" and row[3]
    assert win.store.read_counts() == {"accepted": 1, "back": 1, "confirmed": 1, "skipped": 1, "undone": 1, "typed": 1}

    # 4. a tap on the picture moves the box there
    win.view.set_frame(__import__("tagscan.app.camera", fromlist=["bgr_to_qimage"]).bgr_to_qimage(frame2[::4, ::4]), 1920, 1440)
    win._tapped(0.25, 0.75)
    win._on_frame(frame2)
    assert win._box.x < 1920 * 0.25 < win._box.x + win._box.w and win._box.y < 1440 * 0.75 < win._box.y + win._box.h
    win.close()

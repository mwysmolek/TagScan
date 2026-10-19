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
    prof.scan_trigger = "auto"
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    win = MainWindow(profile=prof, catalog=cat, data_dir=str(tmp_path / "data"), settings=settings)
    win.show()
    assert _pump(lambda: isinstance(win.source, CameraSource), 10), "camera source not set up"
    assert win._box_active
    assert _pump(lambda: "ready" in win.engine_lbl.text() or "unavailable" in win.engine_lbl.text(), 60)
    assert "ready" in win.engine_lbl.text(), win.engine_lbl.text()

    # 0. nobody set yet: the card says so; a badge code anywhere in the picture sets the operator
    assert "operator" in win.card.head.text().lower() and win.operator_text == ""
    badge = _qr_frame("OP: Anna", _textless_frame(seed=9))
    assert _pump(lambda: win.operator_text == "Anna", 20, step=lambda: win._on_frame(badge)), win.op_btn.text()
    assert win.op_btn.text() == "Operator: Anna" and "operator" not in win.card.head.text().lower()
    win.set_location("Hall 2")

    # 1. a label held steady in the box is read from the crop and auto-accepted
    frame = _label_frame("123456")
    assert _pump(lambda: win.store.count() >= 1, 40, step=lambda: win._on_frame(frame)), win.card.head.text()
    scan = win.store.recent(1)[0]
    assert scan.number == "123456" and scan.source == "OCR" and scan.in_list
    assert scan.operator == "Anna" and scan.location == "Hall 2"
    assert scan.photo.startswith("Hall 2/") and scan.photo.endswith("_123456.jpg")
    assert (tmp_path / "data" / "photos" / scan.photo).exists()
    row = win.store.db.execute("SELECT outcome, ocr_number, crop, roi, photo, operator FROM reads ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "accepted" and row[1] == "123456" and row[2] and '"w"' in row[3] and row[4] == f"photos/{scan.photo}"
    assert row[5] == "Anna"
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
    assert win.store.read_counts() == {"accepted": 1, "back": 1, "confirmed": 1, "discarded": 1, "undone": 1, "typed": 1}

    # 4. a tap on the picture moves the box there
    win.view.set_frame(__import__("tagscan.app.camera", fromlist=["bgr_to_qimage"]).bgr_to_qimage(frame2[::4, ::4]), 1920, 1440)
    win._tapped(0.25, 0.75)
    win._on_frame(frame2)
    assert win._box.x < 1920 * 0.25 < win._box.x + win._box.w and win._box.y < 1440 * 0.75 < win._box.y + win._box.h
    win.close()


def test_tap_to_scan_flow(app, tmp_path):
    """Default mode: nothing is decoded until a trigger; then a result or the type-it screen."""
    from tagscan.app.mainwindow import MainWindow
    from tagscan.app.camera import CameraSource
    cat = Catalog(items={"123456": Item("123456", "123456", "item one")})
    prof = Profile()
    prof.auto_confirm_seconds = 0.2
    prof.ocr_interval = 0.0
    prof.trigger_window = 1.5
    assert prof.scan_trigger == "tap"
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    win = MainWindow(profile=prof, catalog=cat, data_dir=str(tmp_path / "data"), settings=settings)
    win.show()
    assert _pump(lambda: isinstance(win.source, CameraSource), 10)
    assert _pump(lambda: "ready" in win.engine_lbl.text(), 60), win.engine_lbl.text()
    win.set_operator("456cd")
    assert not win.card.scan_btn.isHidden()

    # 1. a label in the box, no trigger: nothing happens
    frame = _label_frame("123456")
    t0 = time.time()
    _pump(lambda: time.time() - t0 > 1.2, 3, step=lambda: win._on_frame(frame))
    assert win.store.count() == 0 and win.card.state == "idle"

    # 2. SCAN: read within the window, auto-accepted
    win.card.scan_btn.click()
    assert win._armed
    assert _pump(lambda: win.store.count() == 1, 20, step=lambda: win._on_frame(frame)), win.card.head.text()
    assert win.store.recent(1)[0].number == "123456" and not win._armed

    # 3. a blank box + trigger: the window runs out -> type-it screen; Discard -> idle
    blank = _textless_frame(seed=4)
    win._trigger()
    assert _pump(lambda: win.card.state == "input", 20, step=lambda: win._on_frame(blank)), win.card.head.text()
    win.card._skip()
    QCoreApplication.processEvents()
    assert win.card.state == "idle" and win.store.db.execute("SELECT outcome FROM reads ORDER BY id DESC LIMIT 1").fetchone()[0] == "back"

    # 4. a tap on the picture aims and scans in one go
    from tagscan.app.camera import bgr_to_qimage
    win.view.set_frame(bgr_to_qimage(frame[::4, ::4]), 1920, 1440)
    win.worker.scanner.suppress("123456", 0.0)
    win._tapped(0.5, 0.5)
    assert win._armed
    assert _pump(lambda: win.card.state == "review", 20, step=lambda: win._on_frame(frame)), win.card.head.text()
    assert "already scanned" in win.card.head.text()
    win.card._skip()                                             # Discard the orange card
    QCoreApplication.processEvents()
    assert win.store.db.execute("SELECT outcome FROM reads ORDER BY id DESC LIMIT 1").fetchone()[0] == "discarded"

    # 4b. a number that is not in the list asks for a description and is stored with it
    asked = []
    win._ask_description = lambda number, guess: (asked.append((number, guess)), "new rolling cart")[1]
    for k in "999999":
        win.card.pad._press(k)
    win.card.pad._press("✓")
    QCoreApplication.processEvents()
    s = win.store.recent(1)[0]
    assert asked == [("999999", "")] and s.number == "999999" and not s.in_list
    assert s.description == "new rolling cart" and "new item" in s.note and s.operator == "456cd"
    win.store.delete(s.id)

    # 5. Undo last scan removes the stored scan and its photo
    photo = win.store.recent(1)[0].photo
    assert (tmp_path / "data" / "photos" / photo).exists()
    win._undo_last()
    assert win.store.count() == 0 and not (tmp_path / "data" / "photos" / photo).exists()
    assert win.store.db.execute("SELECT outcome, final_number FROM reads ORDER BY id DESC LIMIT 1").fetchone() == ("deleted", "123456")

    # 6. a new inventory archives everything and the window keeps working on the fresh store
    reads_before = sum(win.store.read_counts().values())
    assert reads_before > 0
    dest = win.store.archive()
    win._refresh_history()
    assert win.store.count() == 0 and win.store.read_counts() == {} and (dest / "scans.sqlite").exists()
    assert win.operator_text == "456cd" and "0 scans" in win.count_lbl.text()
    win.close()

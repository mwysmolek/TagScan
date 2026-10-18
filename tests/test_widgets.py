"""The touch widgets, offscreen."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLineEdit
from PySide6.QtTest import QTest

from tagscan.app.widgets import LocationPicker, NumberPad, OperatorPicker, ResultCard
from tagscan.catalog import Item


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_number_pad_edits_attached_field(app):
    edit = QLineEdit()
    pad = NumberPad(extra=("-", " ", "clear"))
    pad.attach(edit)
    fired = []
    pad.entered.connect(lambda: fired.append(1))
    for k in ("2", "8", "0", "⌫", "1", "9", "-", " "):
        pad._press(k)
    assert edit.text() == "2819- "
    pad._press("✓")
    assert fired == [1]
    pad._press("clear")
    assert edit.text() == ""


def test_card_pad_visibility_and_manual_entry(app):
    card = ResultCard()
    card.show()
    got = []
    card.confirmed.connect(lambda *a: got.append(a))
    card.lookup = lambda n: (n == "1001", "Cart" if n == "1001" else "", [])
    assert card.state == "idle" and not card.pad.isHidden() and not card.scan_btn.isHidden() and not card.scan_btn_left.isHidden()
    card.set_accept("1001", "Cart", "QR", 60.0)
    assert card.pad.isHidden() and not card.fix_btn.isHidden() and not card.discard_btn.isHidden()
    assert card.scan_btn.isHidden() and card.scan_btn_left.isHidden()
    card._undo()                                      # Correct
    assert card.state == "idle" and not card.pad.isHidden()
    discarded = []
    card.discarded.connect(lambda n: discarded.append(n))
    card.set_accept("1001", "Cart", "QR", 60.0)
    card._skip()                                      # Discard on the green card
    assert discarded == ["1001"] and card.state == "idle"
    card.set_review("4711", "", "OCR", False, "not in list", nearest=[Item("1001", "1001", "Cart")])
    assert not card.pad.isHidden() and not card.discard_btn.isHidden() and card.fix_btn.isHidden()
    card._skip()
    assert discarded == ["1001", "4711"]
    for k in "1001":
        card.pad._press(k)
    card.pad._press("✓")
    assert got == [("1001", "MANUAL", True, "Cart", "", "typed")] and card.manual.text() == ""
    # a hardware keyboard still works: digits go to the number field, Enter adds
    QTest.keyClicks(card, "4711")
    QTest.keyClick(card, Qt.Key.Key_Return)
    assert got[-1][:3] == ("4711", "MANUAL", False)
    # outcomes: countdown -> accepted, OK -> confirmed, suggestion -> corrected
    card.set_accept("1001", "Cart", "QR", 60.0)
    card._remaining = 0.0
    card._tick()
    assert got[-1][-1] == "accepted" and card.state == "idle"
    card.set_review("4711", "", "OCR", False, "not in list", nearest=[Item("1001", "1001", "Cart")])
    card._ok()
    assert got[-1][:2] == ("4711", "OCR") and got[-1][-1] == "confirmed"
    card.set_review("4711", "", "OCR", False, "not in list", nearest=[Item("1001", "1001", "Cart")])
    card.alts_lay.itemAt(0).widget().click()
    assert got[-1][0] == "1001" and got[-1][-1] == "corrected"


def test_card_input_state(app):
    from PySide6.QtGui import QImage
    card = ResultCard()
    card.show()
    got, backed = [], []
    card.confirmed.connect(lambda *a: got.append(a))
    card.backed.connect(lambda n: backed.append(n))
    card.lookup = lambda n: (n == "1001", "Cart" if n == "1001" else "", [])
    img = QImage(300, 120, QImage.Format.Format_RGB888)
    img.fill(0xff8800)
    card.set_input(img, prior="", reason="unreadable", nearest=[Item("1001", "1001", "Cart")])
    assert card.state == "input" and not card.pic.isHidden() and not card.discard_btn.isHidden() and card.ok_btn.isHidden()
    card._ok()                                   # nothing to confirm on this screen
    assert card.state == "input" and not got
    card._skip()                                 # Discard
    assert backed == [""] and card.state == "idle" and card.pic.isHidden()
    card.set_input(None, prior="4717", reason="undo")
    assert card.pic.isHidden() and "Wrong read" in card.head.text()
    for k in "4711":
        card.pad._press(k)
    card.pad._press("✓")
    assert got[-1] == ("4711", "MANUAL", False, "", "undo", "typed")
    card.set_input(img, prior="", reason="unreadable", nearest=[Item("1001", "1001", "Cart")])
    card.alts_lay.itemAt(0).widget().click()
    assert got[-1] == ("1001", "MANUAL", True, "Cart", "unreadable", "corrected")


def test_location_picker_choice_and_keyboard(app):
    dlg = LocationPicker(None, "Hall 2", choices=["Hall 2", "Hall 10", "Lab A"], recent=["Lab A"])
    buttons = [b for b in dlg.findChildren(type(dlg.pad._key("x", 1))) if b.text() in ("Hall 2", "Hall 10", "Lab A")]
    assert [b.text() for b in buttons] == ["Lab A", "Hall 2", "Hall 10"]       # recent first, no duplicates
    QTimer.singleShot(0, lambda: buttons[2].click())
    assert dlg.exec() == dlg.DialogCode.Accepted and dlg.value() == "Hall 10"

    dlg = LocationPicker(None, "", choices=[], recent=[])
    for k in ("b", "ü", "r", "o", " ", "2", "0", "4"):                        # letters, umlauts, space, digits
        dlg.pad._press(k)
    assert dlg.edit.text() == "büro 204"
    QTimer.singleShot(0, lambda: dlg.pad._press("✓"))
    assert dlg.exec() == dlg.DialogCode.Accepted and dlg.value() == "büro 204"


def test_operator_picker_alnum_pad_and_pattern(app):
    dlg = OperatorPicker(None, "", recent=["123ab"], pattern=r"\d{3}[a-z]{2}")
    assert dlg.windowTitle() == "Operator" and not dlg.ok.isEnabled()
    for k in ("4", "5", "6", "c", "d"):                   # digits and letters on the one pad
        dlg.pad._press(k)
    assert dlg.edit.text() == "456cd" and dlg.ok.isEnabled() and dlg.valid()
    dlg.pad._press("⌫")
    assert dlg.edit.text() == "456c" and not dlg.ok.isEnabled() and "valid" in dlg.hint.text()
    dlg.accept()                                          # refused while invalid
    assert dlg.result() != dlg.DialogCode.Accepted
    dlg.pad._press("d")
    QTimer.singleShot(0, lambda: dlg.pad._press("✓"))
    assert dlg.exec() == dlg.DialogCode.Accepted and dlg.value() == "456cd"
    # a recent code is one tap
    dlg = OperatorPicker(None, "", recent=["123ab"], pattern="")
    buttons = [b for b in dlg.findChildren(type(dlg.pad._key("x", 1))) if b.text() == "123ab"]
    QTimer.singleShot(0, lambda: buttons[0].click())
    assert dlg.exec() == dlg.DialogCode.Accepted and dlg.value() == "123ab"
    # without a pattern anything non-empty goes
    dlg = OperatorPicker(None, "x1")
    assert dlg.ok.isEnabled() and dlg.valid()


def test_history_strip_undo_last(app):
    from tagscan.app.widgets import HistoryStrip
    from tagscan.store import Scan
    strip = HistoryStrip()
    fired = []
    strip.undo_last_requested.connect(lambda: fired.append(1))
    strip.set_scans([])
    assert not strip.undo_btn.isEnabled()
    strip.set_scans([Scan(1, "2026-10-17 10:00:00", "1001", "Cart", "Hall 2", "QR", True, "", "dev")])
    assert strip.undo_btn.isEnabled()
    strip.undo_btn.click()
    assert fired == [1]


def test_light_theme(app):
    from PySide6.QtGui import QPalette
    from tagscan.app.theme import apply_light_theme
    apply_light_theme(app)
    pal = app.palette()
    assert pal.color(QPalette.ColorRole.Text).lightness() < 80 and pal.color(QPalette.ColorRole.Base).lightness() > 240
    assert pal.color(QPalette.ColorRole.ButtonText).lightness() < 80 and pal.color(QPalette.ColorRole.Window).lightness() > 230


def test_window_layout_puts_controls_at_the_edges(app, tmp_path):
    """The thumb columns hold the decision controls; the centre panel holds the information."""
    from PySide6.QtCore import QSettings
    from tagscan.app.mainwindow import MainWindow
    from tagscan.profile import Profile
    from tagscan.catalog import Catalog
    win = MainWindow(profile=Profile(), catalog=Catalog(), data_dir=str(tmp_path / "data"),
                     settings=QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    win.resize(1280, 800)
    win.show()
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.processEvents()
    c = win.card
    centre_x = win.width() / 2

    def x(b):
        return b.mapTo(win, b.rect().center()).x()

    def y(b):
        return b.mapTo(win, b.rect().center()).y()
    # idle: SCAN at both edges, pad on the right, zoom and undo on the left
    for b in (c.scan_btn_left, win.history.undo_btn, win.zoom_in):
        assert x(b) < centre_x * 0.4, b.text()
    for b in (c.scan_btn, c.pad):
        assert x(b) > centre_x * 1.6
    assert abs(y(c.scan_btn_left) - win.height() / 2) < win.height() * 0.3
    assert abs(y(c.scan_btn) - win.height() / 2) < win.height() * 0.3
    # a card: DISCARD left, OK and Correct right, all at thumb height
    c.set_accept("1001", "Cart", "QR", 60.0)
    QCoreApplication.processEvents()
    assert x(c.discard_btn) < centre_x * 0.4 and x(c.ok_btn) > centre_x * 1.6 and x(c.fix_btn) > centre_x * 1.6
    assert abs(y(c.discard_btn) - win.height() / 2) < win.height() * 0.3 and abs(y(c.ok_btn) - win.height() / 2) < win.height() * 0.3
    # the result panel is below the picture, in the middle
    assert y(c) > y(win.view) and abs(x(c) - centre_x) < centre_x * 0.3
    assert c.scan_btn.minimumHeight() >= 100 and c.discard_btn.minimumHeight() >= 100 and c.ok_btn.minimumHeight() >= 100
    win.close()

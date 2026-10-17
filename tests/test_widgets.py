"""The touch widgets, offscreen."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLineEdit
from PySide6.QtTest import QTest

from tagscan.app.widgets import LocationPicker, NumberPad, ResultCard
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
    assert card.state == "idle" and not card.pad.isHidden()
    card.set_accept("1001", "Cart", "QR", 60.0)
    assert card.pad.isHidden() and not card.undo_btn.isHidden()
    card._undo()
    assert card.state == "idle" and not card.pad.isHidden()
    card.set_review("4711", "", "OCR", False, "not in list", nearest=[Item("1001", "1001", "Cart")])
    assert not card.pad.isHidden() and not card.skip_btn.isHidden()
    card._skip()
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
    assert card.state == "input" and not card.pic.isHidden() and card.skip_btn.text() == "Back" and card.ok_btn.isHidden()
    card._ok()                                   # nothing to confirm on this screen
    assert card.state == "input" and not got
    card._skip()                                 # Back
    assert backed == [""] and card.state == "idle" and card.pic.isHidden() and card.skip_btn.text() == "Skip"
    card.set_input(None, prior="4717", reason="undo")
    assert card.pic.isHidden() and "Wrong read" in card.head.text()
    for k in "4711":
        card.pad._press(k)
    card.pad._press("✓")
    assert got[-1] == ("4711", "MANUAL", False, "", "undo", "typed")
    card.set_input(img, prior="", reason="unreadable", nearest=[Item("1001", "1001", "Cart")])
    card.alts_lay.itemAt(0).widget().click()
    assert got[-1] == ("1001", "MANUAL", True, "Cart", "unreadable", "corrected")


def test_location_picker_choice_and_pad(app):
    dlg = LocationPicker(None, "Hall 2", choices=["Hall 2", "Hall 10", "Lab A"], recent=["Lab A"])
    buttons = [b for b in dlg.findChildren(type(dlg.pad._key("x", 1))) if b.text() in ("Hall 2", "Hall 10", "Lab A")]
    assert [b.text() for b in buttons] == ["Lab A", "Hall 2", "Hall 10"]       # recent first, no duplicates
    QTimer.singleShot(0, lambda: buttons[2].click())
    assert dlg.exec() == dlg.DialogCode.Accepted and dlg.value() == "Hall 10"

    dlg = LocationPicker(None, "", choices=[], recent=[])
    for k in ("2", "0", "4"):
        dlg.pad._press(k)
    QTimer.singleShot(0, lambda: dlg.pad._press("✓"))
    assert dlg.exec() == dlg.DialogCode.Accepted and dlg.value() == "204"

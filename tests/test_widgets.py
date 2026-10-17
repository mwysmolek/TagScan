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
    card.lookup = lambda n: (n == "28019", "Servicewagen" if n == "28019" else "", [])
    assert card.state == "idle" and not card.pad.isHidden()
    card.set_accept("28019", "Servicewagen", "QR", 60.0)
    assert card.pad.isHidden() and not card.undo_btn.isHidden()
    card._undo()
    assert card.state == "idle" and not card.pad.isHidden()
    card.set_review("4711", "", "OCR", False, "not in list", nearest=[Item("28019", "28019", "Servicewagen")])
    assert not card.pad.isHidden() and not card.skip_btn.isHidden()
    card._skip()
    for k in "28019":
        card.pad._press(k)
    card.pad._press("✓")
    assert got == [("28019", "MANUAL", True, "Servicewagen", "")] and card.manual.text() == ""
    # a hardware keyboard still works: digits go to the number field, Enter adds
    QTest.keyClicks(card, "4711")
    QTest.keyClick(card, Qt.Key.Key_Return)
    assert got[-1][:3] == ("4711", "MANUAL", False)


def test_location_picker_choice_and_pad(app):
    dlg = LocationPicker(None, "Halle 2", choices=["Halle 2", "Halle 10", "Labor 6"], recent=["Labor 6"])
    buttons = [b for b in dlg.findChildren(type(dlg.pad._key("x", 1))) if b.text() in ("Halle 2", "Halle 10", "Labor 6")]
    assert [b.text() for b in buttons] == ["Labor 6", "Halle 2", "Halle 10"]       # recent first, no duplicates
    QTimer.singleShot(0, lambda: buttons[2].click())
    assert dlg.exec() == dlg.DialogCode.Accepted and dlg.value() == "Halle 10"

    dlg = LocationPicker(None, "", choices=[], recent=[])
    for k in ("2", "0", "4"):
        dlg.pad._press(k)
    QTimer.singleShot(0, lambda: dlg.pad._press("✓"))
    assert dlg.exec() == dlg.DialogCode.Accepted and dlg.value() == "204"

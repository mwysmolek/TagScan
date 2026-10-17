"""Touch-friendly widgets: the camera view with overlays, the result card with a number pad,
the location picker and the history strip. Everything can be driven with a finger."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer, QPointF, QRectF, QEvent, QPoint
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QProgressBar,
                               QPushButton, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

GREEN = QColor(46, 160, 67)
ORANGE = QColor(240, 140, 0)
BLUE = QColor(60, 140, 230)
GREY = QColor(200, 200, 200)
TOUCH = 48      # minimum height of anything a finger has to hit


def _remove_all(layout, keep_first: int = 0, keep_last: int = 0) -> None:
    """Take widgets out of a layout and delete them later. They are detached from their parent
    first: a child with a pending deleteLater whose parent is destroyed from Python crashes
    PySide6 6.11; deferring (rather than deleting now) matters when the widget being removed
    is the button whose click triggered the removal."""
    while layout.count() > keep_first + keep_last:
        item = layout.takeAt(keep_first)
        w = item.widget() if item else None
        if w:
            w.hide()
            w.setParent(None)
            w.deleteLater()


class VideoView(QWidget):
    """Shows the latest (already downscaled) frame, scaled to fit, with coloured quads on top.
    A tap emits ``tapped``; a pinch emits ``pinched(scale)``."""
    tapped = Signal()
    pinched = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._img: QImage | None = None
        self._src = (1, 1)                  # size of the frame the quads refer to
        self._quads: list[tuple[list, QColor]] = []
        self._quad_age = QTimer(self)
        self._quad_age.setSingleShot(True)
        self._quad_age.timeout.connect(self._clear_quads)
        self._text = ""
        self._flash: QColor | None = None
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self._press: QPoint | None = None
        self.setStyleSheet("background:#111;")
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
        self.grabGesture(Qt.GestureType.PinchGesture)

    def set_frame(self, img: QImage, src_w: int | None = None, src_h: int | None = None) -> None:
        """``img`` is what gets painted; ``src_w x src_h`` is the frame size the quads are in."""
        self._img = img
        self._src = (src_w or img.width(), src_h or img.height())
        self.update()

    def set_quads(self, quads: list[tuple[list, QColor]], keep_ms: int = 700) -> None:
        self._quads = quads
        self._quad_age.start(keep_ms)
        self.update()

    def set_text(self, text: str) -> None:
        self._text = text
        self.update()

    def flash(self, color: QColor, ms: int = 350) -> None:
        self._flash = color
        self._flash_timer.start(ms)
        self.update()

    def _clear_quads(self):
        self._quads = []
        self.update()

    def _end_flash(self):
        self._flash = None
        self.update()

    # -- input -------------------------------------------------------------------------------
    def mousePressEvent(self, ev):
        self._press = ev.position().toPoint()

    def mouseReleaseEvent(self, ev):
        if self._press is not None and (ev.position().toPoint() - self._press).manhattanLength() < 24:
            self.tapped.emit()
        self._press = None

    def event(self, ev):
        if ev.type() == QEvent.Type.Gesture:
            try:
                g = ev.gesture(Qt.GestureType.PinchGesture)
                if g is not None:
                    f = float(g.scaleFactor())
                    if f > 0 and abs(f - 1.0) > 1e-3:
                        self.pinched.emit(f)
                    self._press = None          # a pinch is not a tap
                    return True
            except Exception:
                pass
        return super().event(ev)

    # -- painting ----------------------------------------------------------------------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(17, 17, 17))
        if self._img is None or self._img.isNull():
            p.setPen(GREY)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text or "no camera")
            return
        sw, sh = self._src
        s = min(self.width() / sw, self.height() / sh)
        dw, dh = sw * s, sh * s
        ox, oy = (self.width() - dw) / 2, (self.height() - dh) / 2
        target = QRectF(ox, oy, dw, dh)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(target, self._img)
        for quad, color in self._quads:
            p.setPen(QPen(color, 3))
            p.drawPolygon(QPolygonF([QPointF(ox + x * s, oy + y * s) for x, y in quad]))
        if self._flash:
            p.setPen(QPen(self._flash, 14))
            p.drawRect(target.adjusted(7, 7, -7, -7))
        if self._text:
            p.setPen(Qt.GlobalColor.white)
            f = QFont()
            f.setPointSize(11)
            p.setFont(f)
            p.fillRect(QRectF(ox, oy + dh - 28, dw, 28), QColor(0, 0, 0, 120))
            p.drawText(QRectF(ox + 8, oy + dh - 28, dw - 16, 28), Qt.AlignmentFlag.AlignVCenter, self._text)


class NumberPad(QWidget):
    """Digits, backspace and enter as finger-sized buttons that edit an attached QLineEdit.
    ``entered`` fires on the tick key. Extra keys (e.g. "-", " ") can be added for locations."""
    entered = Signal()

    def __init__(self, parent=None, extra: tuple[str, ...] = (), height: int = TOUCH):
        super().__init__(parent)
        self.target: QLineEdit | None = None
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(6)
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "⌫", "0", "✓"]
        for i, k in enumerate(keys):
            g.addWidget(self._key(k, height), i // 3, i % 3)
        if extra:
            row = QHBoxLayout()
            row.setSpacing(6)
            for k in extra:
                row.addWidget(self._key(k, height))
            g.addLayout(row, 4, 0, 1, 3)

    def _key(self, k: str, height: int) -> QPushButton:
        label = {" ": "space", "⌫": "⌫", "✓": "✓"}.get(k, k)
        b = QPushButton(label)
        b.setMinimumHeight(height)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)          # never steal the keyboard focus
        style = "font-size:20px;border-radius:8px;"
        if k == "✓":
            style += "background:#2ea043;color:white;font-weight:600;"
        elif k == "⌫":
            style += "background:#eee;"
        b.setStyleSheet(style)
        b.clicked.connect(lambda _=False, k=k: self._press(k))
        return b

    def attach(self, edit: QLineEdit) -> None:
        self.target = edit

    def _press(self, k: str) -> None:
        if self.target is None:
            return
        if k == "⌫":
            self.target.backspace()
        elif k == "✓":
            self.entered.emit()
        elif k == "clear":
            self.target.clear()
        else:
            self.target.insert(k)


class ResultCard(QFrame):
    """Idle -> (seen) -> accept with countdown | review with buttons -> idle.
    The number pad under the card types a number without a keyboard."""
    confirmed = Signal(str, str, bool, str, str)   # number, source, in_list, description, note
    skipped = Signal(str)                          # number
    undone = Signal(str)                           # number
    state_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("card")
        self.setMinimumWidth(340)
        self.state = "idle"
        self.pending: dict | None = None
        self._seconds = 3.0
        self._remaining = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(8)
        self.head = QLabel("Point the camera at a label")
        self.head.setWordWrap(True)
        self.head.setStyleSheet("font-size:15px;color:#555;")
        self.number = QLabel("")
        self.number.setStyleSheet("font-size:44px;font-weight:700;")
        self.number.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setStyleSheet("font-size:17px;")
        self.meta = QLabel("")
        self.meta.setWordWrap(True)
        self.meta.setStyleSheet("font-size:12px;color:#777;")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        self.bar.hide()

        self.alts = QWidget()
        self.alts_lay = QGridLayout(self.alts)
        self.alts_lay.setContentsMargins(0, 0, 0, 0)
        self.alts_lay.setSpacing(6)

        btns = QHBoxLayout()
        self.ok_btn = QPushButton("OK  ✓")
        self.ok_btn.setMinimumHeight(60)
        self.ok_btn.setStyleSheet("font-size:20px;font-weight:600;background:#2ea043;color:white;border-radius:8px;")
        self.skip_btn = QPushButton("Skip")
        self.skip_btn.setMinimumHeight(60)
        self.skip_btn.setStyleSheet("font-size:18px;border-radius:8px;")
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.setMinimumHeight(60)
        self.undo_btn.setStyleSheet("font-size:18px;background:#f08c00;color:white;border-radius:8px;")
        btns.addWidget(self.ok_btn, 2)
        btns.addWidget(self.skip_btn, 1)
        btns.addWidget(self.undo_btn, 1)

        manual = QHBoxLayout()
        self.manual = QLineEdit()
        self.manual.setPlaceholderText("number …")
        self.manual.setMinimumHeight(TOUCH)
        self.manual.setStyleSheet("font-size:20px;")
        self.manual_btn = QPushButton("Add")
        self.manual_btn.setMinimumHeight(TOUCH)
        self.manual_btn.setStyleSheet("font-size:16px;")
        manual.addWidget(self.manual, 1)
        manual.addWidget(self.manual_btn)
        self.pad = NumberPad(height=44)
        self.pad.attach(self.manual)
        self.pad.entered.connect(self._manual)

        lay.addWidget(self.head)
        lay.addWidget(self.number)
        lay.addWidget(self.desc)
        lay.addWidget(self.meta)
        lay.addWidget(self.bar)
        lay.addWidget(self.alts)
        lay.addStretch(1)
        lay.addLayout(btns)
        lay.addLayout(manual)
        lay.addWidget(self.pad)

        self.ok_btn.clicked.connect(self._ok)
        self.skip_btn.clicked.connect(self._skip)
        self.undo_btn.clicked.connect(self._undo)
        self.manual_btn.clicked.connect(self._manual)
        self.manual.returnPressed.connect(self._manual)
        self.lookup = None          # callable number -> (in_list, description, nearest[list of Item])
        self.set_idle()

    # ------------------------------------------------------------------ states
    def set_idle(self, msg: str = "Point the camera at a label"):
        self.state = "idle"
        self.pending = None
        self._timer.stop()
        self.state_changed.emit("idle")
        self.head.setText(msg)
        self.number.setText("")
        self.desc.setText("")
        self.meta.setText("")
        self.bar.hide()
        self._clear_alts()
        self.ok_btn.hide()
        self.skip_btn.hide()
        self.undo_btn.hide()
        self.pad.show()
        self.setStyleSheet("QFrame#card{background:#fafafa;border:1px solid #ddd;border-radius:10px;}")

    def set_seen(self, number: str, votes: int):
        if self.state != "idle":
            return
        self.head.setText(f"reading … ({votes})")
        self.number.setText(number)
        self.number.setStyleSheet("font-size:44px;font-weight:700;color:#999;")

    def set_accept(self, number, description, source, seconds: float, note: str = ""):
        self.state = "accept"
        self.state_changed.emit("accept")
        self.pending = dict(number=number, description=description, source=source, in_list=True, note=note)
        self.head.setText(f"Found  ·  {source}")
        self.number.setText(number)
        self.number.setStyleSheet("font-size:44px;font-weight:700;color:#1a7f37;")
        self.desc.setText(description)
        self.meta.setText(note)
        self._clear_alts()
        self.bar.show()
        self._seconds = max(0.05, seconds)
        self._remaining = self._seconds
        self.bar.setValue(1000)
        self.ok_btn.show()
        self.skip_btn.hide()
        self.undo_btn.show()
        self.pad.hide()
        self.setStyleSheet("QFrame#card{background:#e9f7ec;border:2px solid #2ea043;border-radius:10px;}")
        self._timer.start()

    def set_review(self, number, description, source, in_list, reason, nearest=()):
        self.state = "review"
        self.state_changed.emit("review")
        self.pending = dict(number=number, description=description, source=source, in_list=in_list, note=reason)
        self.head.setText(f"Check  ·  {source}  ·  {reason}")
        self.number.setText(number or "?")
        self.number.setStyleSheet("font-size:44px;font-weight:700;color:#b35c00;")
        self.desc.setText(description if in_list else "not in the list")
        self.meta.setText("tap a suggestion, OK to keep as read, or type the number")
        self.bar.hide()
        self._timer.stop()
        self._clear_alts()
        for i, it in enumerate(list(nearest)[:4]):
            b = QPushButton(f"{it.number}\n{it.description[:34]}")
            b.setMinimumHeight(56)
            b.setStyleSheet("text-align:left;font-size:13px;border-radius:6px;")
            b.clicked.connect(lambda _=False, it=it: self._choose(it))
            self.alts_lay.addWidget(b, i // 2, i % 2)
        self.ok_btn.show()
        self.skip_btn.show()
        self.undo_btn.hide()
        self.pad.show()
        self.setStyleSheet("QFrame#card{background:#fff4e5;border:2px solid #f08c00;border-radius:10px;}")

    # ------------------------------------------------------------------ actions
    def _tick(self):
        self._remaining -= 0.05
        self.bar.setValue(int(max(0.0, self._remaining / self._seconds) * 1000))
        if self._remaining <= 0:
            self._timer.stop()
            self._ok()

    def _ok(self):
        if not self.pending:
            return
        p = self.pending
        self.set_idle()
        self.confirmed.emit(p["number"], p["source"], p["in_list"], p["description"], p.get("note", ""))

    def _skip(self):
        n = self.pending["number"] if self.pending else ""
        self.set_idle()
        self.skipped.emit(n)

    def _undo(self):
        n = self.pending["number"] if self.pending else ""
        self.set_idle("undone")
        self.undone.emit(n)

    def _choose(self, it):
        if not self.pending:
            return
        self.pending.update(number=it.key, description=it.description, in_list=True, note="corrected")
        self._ok()

    def _manual(self):
        text = self.manual.text().strip()
        if not text:
            return
        self.manual.clear()
        in_list, description = False, ""
        if self.lookup:
            in_list, description, _ = self.lookup(text)
        self.set_idle()
        self.confirmed.emit(text, "MANUAL", in_list, description, "")

    def _clear_alts(self):
        _remove_all(self.alts_lay)

    def keyPressEvent(self, ev):
        key, text = ev.key(), ev.text()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.state in ("accept", "review"):
            self._ok()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.manual.text().strip():
            self._manual()
        elif key == Qt.Key.Key_Escape and self.state == "accept":
            self._undo()
        elif key == Qt.Key.Key_Escape and self.state == "review":
            self._skip()
        elif text.isdigit():                          # a hardware keyboard types into the number field
            self.manual.insert(text)
        elif key == Qt.Key.Key_Backspace:
            self.manual.backspace()
        else:
            super().keyPressEvent(ev)


class LocationPicker(QDialog):
    """Pick the scanning location with a finger: recent and list locations as big buttons, a
    number pad for room numbers, and a text field for the rare case a keyboard is at hand."""

    def __init__(self, parent=None, current: str = "", choices=(), recent=()):
        super().__init__(parent)
        self.setWindowTitle("Location")
        self.setModal(True)
        self.resize(940, 640)
        root = QHBoxLayout(self)
        root.setSpacing(14)

        # left: the choices
        left = QVBoxLayout()
        title = QLabel("Tap a location")
        title.setStyleSheet("font-size:15px;color:#555;")
        left.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setSpacing(8)
        grid.setContentsMargins(0, 0, 0, 0)
        seen: list[str] = []
        for r in list(recent) + list(choices):
            r = str(r).strip()
            if r and r not in seen:
                seen.append(r)
        for i, name in enumerate(seen):
            b = QPushButton(name)
            b.setMinimumHeight(56)
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            style = "font-size:17px;border-radius:8px;text-align:left;padding:0 12px;"
            if name in recent:
                style += "border:2px solid #3c8ce6;"
            if name == current:
                style += "background:#e3effc;"
            b.setStyleSheet(style)
            b.clicked.connect(lambda _=False, n=name: self._choose(n))
            grid.addWidget(b, i // 2, i % 2)
        grid.setRowStretch(max(1, (len(seen) + 1) // 2), 1)
        scroll.setWidget(inner)
        left.addWidget(scroll, 1)
        if not seen:
            empty = QLabel("No locations yet. Type one on the right, or give the list a\n"
                           "location column (profile: list.location_column) to see them here.")
            empty.setStyleSheet("color:#777;")
            left.addWidget(empty)
        root.addLayout(left, 3)

        # right: type it
        right = QVBoxLayout()
        self.edit = QLineEdit(current)
        self.edit.setPlaceholderText("room / hall")
        self.edit.setMinimumHeight(56)
        self.edit.setStyleSheet("font-size:24px;")
        right.addWidget(self.edit)
        self.pad = NumberPad(extra=("-", "/", " ", "clear"))
        self.pad.attach(self.edit)
        self.pad.entered.connect(self.accept)
        right.addWidget(self.pad)
        hint = QLabel("Or scan a location code: a QR code reading  LOC: <name>  switches the location "
                      "without touching the screen.")
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size:12px;color:#777;")
        right.addWidget(hint)
        right.addStretch(1)
        row = QHBoxLayout()
        ok = QPushButton("OK  ✓")
        ok.setMinimumHeight(60)
        ok.setStyleSheet("font-size:20px;font-weight:600;background:#2ea043;color:white;border-radius:8px;")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.setMinimumHeight(60)
        cancel.setStyleSheet("font-size:18px;border-radius:8px;")
        cancel.clicked.connect(self.reject)
        row.addWidget(ok, 2)
        row.addWidget(cancel, 1)
        right.addLayout(row)
        root.addLayout(right, 2)
        self.edit.returnPressed.connect(self.accept)

    def _choose(self, name: str) -> None:
        self.edit.setText(name)
        self.accept()

    def value(self) -> str:
        return self.edit.text().strip()

    @classmethod
    def pick(cls, parent, current: str, choices=(), recent=()) -> str | None:
        dlg = cls(parent, current, choices, recent)
        return dlg.value() if dlg.exec() == QDialog.DialogCode.Accepted else None


class HistoryStrip(QWidget):
    delete_requested = Signal(int)
    relocate_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(6, 4, 6, 4)
        self.lay.setSpacing(6)
        self.setFixedHeight(70)
        self.title = QLabel("recent")
        self.title.setStyleSheet("color:#888;")
        self.lay.addWidget(self.title)
        self.lay.addStretch(1)

    def set_scans(self, scans) -> None:
        _remove_all(self.lay, keep_first=1, keep_last=1)
        for i, s in enumerate(scans):
            b = QToolButton()
            b.setText(f"{s.number}\n{(s.location or '-')[:14]}")
            b.setMinimumSize(100, 58)
            color = "#2ea043" if s.in_list else "#f08c00"
            b.setStyleSheet(f"QToolButton{{border:2px solid {color};border-radius:8px;font-size:12px;}}")
            b.setToolTip(f"{s.ts}  {s.source}\n{s.description}")
            b.clicked.connect(lambda _=False, sid=s.id, btn=b: self._menu(sid, btn))
            self.lay.insertWidget(1 + i, b)

    def _menu(self, sid: int, btn: QToolButton):
        m = QMenu(self)
        m.setStyleSheet("QMenu{font-size:17px;} QMenu::item{padding:14px 28px;}")
        a1 = m.addAction("Delete this scan")
        a2 = m.addAction("Change location …")
        act = m.exec(btn.mapToGlobal(btn.rect().topLeft()))
        if act == a1:
            self.delete_requested.emit(sid)
        elif act == a2:
            self.relocate_requested.emit(sid)

"""Touch-friendly widgets: the camera view with overlays, the result card with a number pad,
the location picker and the history strip. Everything can be driven with a finger."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer, QPointF, QRectF, QEvent, QPoint
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QProgressBar,
                               QPushButton, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

GREEN = QColor(46, 160, 67)
ORANGE = QColor(240, 140, 0)
BLUE = QColor(60, 140, 230)
GREY = QColor(200, 200, 200)
RED = QColor(209, 36, 47)
TOUCH = 48      # minimum height of anything a finger has to hit
TEXT = "#1b1f24"
BTN_OK = "font-size:20px;font-weight:600;background:#2ea043;color:white;border-radius:8px;"
BTN_DISCARD = "font-size:18px;font-weight:600;background:#d1242f;color:white;border-radius:8px;"
BTN_PLAIN = f"font-size:18px;color:{TEXT};background:#e9ecf1;border:1px solid #c5c9d0;border-radius:8px;"


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
    """Shows the latest (already downscaled) frame, scaled to fit, with coloured quads and the
    aim box on top. A tap emits ``tapped(fx, fy)`` with the tapped point as fractions of the
    frame (-1, -1 outside the picture); a pinch emits ``pinched(scale)``."""
    tapped = Signal(float, float)
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
        self._box = None                     # (x, y, w, h) in frame coordinates
        self._box_color = QColor(255, 255, 255, 220)
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

    def set_box(self, box, color: QColor | None = None) -> None:
        """The aim box as an object with x, y, w, h in frame coordinates (None hides it)."""
        self._box = (box.x, box.y, box.w, box.h) if box is not None else None
        if color is not None:
            self._box_color = color
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
            self.tapped.emit(*self.frame_point(ev.position().x(), ev.position().y()))
        self._press = None

    def _geometry(self):
        """(scale, ox, oy, dw, dh) of the drawn picture inside the widget."""
        sw, sh = self._src
        s = min(self.width() / sw, self.height() / sh)
        dw, dh = sw * s, sh * s
        return s, (self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh

    def frame_point(self, wx: float, wy: float) -> tuple[float, float]:
        """Widget point -> fractions of the frame, (-1, -1) when outside the picture."""
        if self._img is None:
            return -1.0, -1.0
        s, ox, oy, dw, dh = self._geometry()
        fx, fy = (wx - ox) / dw, (wy - oy) / dh
        return (fx, fy) if 0 <= fx <= 1 and 0 <= fy <= 1 else (-1.0, -1.0)

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
        s, ox, oy, dw, dh = self._geometry()
        target = QRectF(ox, oy, dw, dh)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(target, self._img)
        if self._box is not None:
            bx, by, bw, bh = self._box
            r = QRectF(ox + bx * s, oy + by * s, bw * s, bh * s)
            # dim everything outside the box, then corner brackets
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawRect(QRectF(ox, oy, dw, r.top() - oy))
            p.drawRect(QRectF(ox, r.bottom(), dw, oy + dh - r.bottom()))
            p.drawRect(QRectF(ox, r.top(), r.left() - ox, r.height()))
            p.drawRect(QRectF(r.right(), r.top(), ox + dw - r.right(), r.height()))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(self._box_color, 4))
            arm = max(12.0, min(r.width(), r.height()) * 0.2)
            for cx, dx in ((r.left(), 1), (r.right(), -1)):
                for cy, dy in ((r.top(), 1), (r.bottom(), -1)):
                    p.drawLine(QPointF(cx, cy), QPointF(cx + dx * arm, cy))
                    p.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * arm))
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


# the on-screen keyboard: digits, QWERTZ letters, umlauts, a few separators, space
FULL_ROWS = (("1", "2", "3", "4", "5", "6", "7", "8", "9", "0"),
             ("q", "w", "e", "r", "t", "z", "u", "i", "o", "p"),
             ("a", "s", "d", "f", "g", "h", "j", "k", "l", "ä"),
             ("y", "x", "c", "v", "b", "n", "m", "ö", "ü", "ß"),
             ("-", ".", "/", " ", "clear", "⌫", "✓"))
ALNUM_ROWS = FULL_ROWS


class NumberPad(QWidget):
    """Finger-sized keys that edit an attached QLineEdit. Default: a 3x4 number pad with
    backspace and enter; ``extra`` adds a row (e.g. "-", " "); ``rows`` replaces the layout
    altogether (see ``ALNUM_ROWS`` for digits plus a QWERTZ letter block, for codes such as
    ``123ab``). ``entered`` fires on the tick key."""
    entered = Signal()

    def __init__(self, parent=None, extra: tuple[str, ...] = (), height: int = TOUCH, rows=None):
        super().__init__(parent)
        self.target: QLineEdit | None = None
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(6)
        if rows is None:
            rows = (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"), ("⌫", "0", "✓"))
            if extra:
                rows = rows + (tuple(extra),)
        ncol = max(len(r) for r in rows)
        for r, keys in enumerate(rows):
            if len(keys) == ncol:
                for c, k in enumerate(keys):
                    g.addWidget(self._key(k, height), r, c)
            else:                                   # a shorter row spans the full width
                row = QHBoxLayout()
                row.setSpacing(6)
                for k in keys:
                    row.addWidget(self._key(k, height))
                g.addLayout(row, r, 0, 1, ncol)

    def _key(self, k: str, height: int) -> QPushButton:
        label = {" ": "space", "⌫": "⌫", "✓": "✓"}.get(k, k)
        b = QPushButton(label)
        b.setMinimumHeight(height)
        b.setMinimumWidth(height * (4 if k == " " else 1))
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)          # never steal the keyboard focus
        style = f"font-size:20px;border-radius:8px;padding:0 4px;color:{TEXT};background:#ffffff;border:1px solid #c5c9d0;"
        if k == "✓":
            style += "background:#2ea043;color:white;font-weight:600;border:none;"
        elif k in ("⌫", "clear"):
            style += "background:#e5e7eb;font-size:16px;"
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
    confirmed = Signal(str, str, bool, str, str, str)   # number, source, in_list, description, note, outcome
    discarded = Signal(str)                        # number (Discard on a green or orange card)
    undone = Signal(str)                           # number (Correct on a green card: wrong read, type it)
    backed = Signal(str)                           # Discard on the "type it" screen (prior number)
    scan_requested = Signal()                      # the big SCAN button
    state_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("card")
        self.setMinimumWidth(340)
        self.state = "idle"
        self.idle_message = "Point the camera at a label"      # the window may replace it (e.g. "set the operator")
        self.scan_enabled = True                               # the SCAN button (tap-to-scan mode)
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
        self.number.setStyleSheet(f"font-size:44px;font-weight:700;color:{TEXT};")
        self.number.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setStyleSheet(f"font-size:17px;color:{TEXT};")
        self.meta = QLabel("")
        self.meta.setWordWrap(True)
        self.meta.setStyleSheet("font-size:12px;color:#777;")
        self.scan_btn = QPushButton("SCAN")
        self.scan_btn.setMinimumHeight(72)
        self.scan_btn.setStyleSheet("font-size:26px;font-weight:700;background:#2ea043;color:white;border-radius:10px;letter-spacing:2px;")
        self.scan_btn.setToolTip("Read the box now (or tap the picture, or press Space)")
        self._logo: QPixmap | None = None
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        self.bar.hide()
        self.pic = QLabel()                      # the box content, so the operator can read it
        self.pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pic.setStyleSheet("background:#222;border-radius:6px;")
        self.pic.setMinimumHeight(120)
        self.pic.hide()

        self.alts = QWidget()
        self.alts_lay = QGridLayout(self.alts)
        self.alts_lay.setContentsMargins(0, 0, 0, 0)
        self.alts_lay.setSpacing(6)

        btns = QHBoxLayout()
        self.ok_btn = QPushButton("OK  ✓")
        self.ok_btn.setMinimumHeight(60)
        self.ok_btn.setStyleSheet(BTN_OK)
        self.fix_btn = QPushButton("Correct")              # green card: the read is wrong, type the right one
        self.fix_btn.setMinimumHeight(60)
        self.fix_btn.setStyleSheet("font-size:18px;font-weight:600;background:#f08c00;color:white;border-radius:8px;")
        self.discard_btn = QPushButton("Discard  ✗")       # every card: drop what was captured
        self.discard_btn.setMinimumHeight(60)
        self.discard_btn.setStyleSheet(BTN_DISCARD)
        btns.addWidget(self.ok_btn, 2)
        btns.addWidget(self.fix_btn, 1)
        btns.addWidget(self.discard_btn, 1)

        manual = QHBoxLayout()
        self.manual = QLineEdit()
        self.manual.setPlaceholderText("number …")
        self.manual.setMinimumHeight(TOUCH)
        self.manual.setStyleSheet(f"font-size:20px;color:{TEXT};background:#ffffff;")
        self.manual_btn = QPushButton("Add")
        self.manual_btn.setMinimumHeight(TOUCH)
        self.manual_btn.setStyleSheet(f"font-size:16px;color:{TEXT};")
        manual.addWidget(self.manual, 1)
        manual.addWidget(self.manual_btn)
        self.pad = NumberPad(height=44)
        self.pad.attach(self.manual)
        self.pad.entered.connect(self._manual)

        lay.addWidget(self.scan_btn)
        lay.addWidget(self.head)
        lay.addWidget(self.number)
        lay.addWidget(self.desc)
        lay.addWidget(self.meta)
        lay.addWidget(self.bar)
        lay.addWidget(self.pic)
        lay.addWidget(self.alts)
        lay.addStretch(1)
        lay.addLayout(btns)
        lay.addLayout(manual)
        lay.addWidget(self.pad)

        self.ok_btn.clicked.connect(self._ok)
        self.discard_btn.clicked.connect(self._skip)
        self.fix_btn.clicked.connect(self._undo)
        self.manual_btn.clicked.connect(self._manual)
        self.manual.returnPressed.connect(self._manual)
        self.scan_btn.clicked.connect(self.scan_requested.emit)
        self.lookup = None          # callable number -> (in_list, description, nearest[list of Item])
        self.set_idle()

    # ------------------------------------------------------------------ states
    def set_idle(self, msg: str | None = None):
        self.state = "idle"
        self.pending = None
        self._timer.stop()
        self.state_changed.emit("idle")
        self.head.setText(self.idle_message if msg is None else msg)
        self.number.setText("")
        self.desc.setText("")
        self.meta.setText("")
        self.bar.hide()
        self._clear_alts()
        self.ok_btn.hide()
        self.discard_btn.hide()
        self.fix_btn.hide()
        self.scan_btn.setVisible(self.scan_enabled)
        self.pad.show()
        self._show_logo()
        self.setStyleSheet("QFrame#card{background:#fafafa;border:1px solid #ddd;border-radius:10px;}")

    def set_logo(self, pixmap: QPixmap | None) -> None:
        self._logo = pixmap if pixmap is not None and not pixmap.isNull() else None
        if self.state == "idle":
            self._show_logo()

    def _show_logo(self) -> None:
        if self._logo is None:
            self.pic.hide()
            self.pic.clear()
            return
        self.pic.setStyleSheet("background:transparent;")
        self.pic.setPixmap(self._logo.scaled(150, 150, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation))
        self.pic.show()

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
        self.pic.hide()
        self.scan_btn.hide()
        self.bar.show()
        self._seconds = max(0.05, seconds)
        self._remaining = self._seconds
        self.bar.setValue(1000)
        self.ok_btn.show()
        self.fix_btn.show()
        self.discard_btn.show()
        self.pad.hide()
        self.setStyleSheet("QFrame#card{background:#e9f7ec;border:2px solid #2ea043;border-radius:10px;}")
        self._timer.start()

    def set_review(self, number, description, source, in_list, reason, nearest=(), image=None):
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
        self.scan_btn.hide()
        self._show_pic(image)
        self._set_alts(nearest)
        self.ok_btn.show()
        self.fix_btn.hide()
        self.discard_btn.show()
        self.pad.show()
        self.setStyleSheet("QFrame#card{background:#fff4e5;border:2px solid #f08c00;border-radius:10px;}")

    def set_input(self, image, prior: str = "", reason: str = "unreadable", nearest=()):
        """The "type it" screen: OCR gave up on a steady box, or the operator undid a read.
        Shows the box content, suggestions, and the pad; Back returns to idle."""
        self.state = "input"
        self.state_changed.emit("input")
        self.pending = dict(number=prior, description="", source="MANUAL", in_list=False, note=reason)
        if reason == "undo":
            self.head.setText("Wrong read  ·  type the right number")
        else:
            self.head.setText("Could not read the label  ·  type the number")
        self.number.setText(prior or "")
        self.number.setStyleSheet("font-size:44px;font-weight:700;color:#999;")
        self.desc.setText("")
        self.meta.setText("tap a suggestion or type the number on the pad; Back if there is nothing to add")
        self.bar.hide()
        self._timer.stop()
        self.scan_btn.hide()
        self._show_pic(image)
        self._set_alts(nearest)
        self.ok_btn.hide()
        self.fix_btn.hide()
        self.discard_btn.show()
        self.pad.show()
        self.manual.setFocus()
        self.setStyleSheet("QFrame#card{background:#eef3fb;border:2px solid #3c8ce6;border-radius:10px;}")

    def _show_pic(self, image) -> None:
        if image is None or image.isNull():
            self.pic.hide()
            self.pic.clear()
            return
        self.pic.setStyleSheet("background:#222;border-radius:6px;")
        w = max(200, self.width() - 40)
        pm = QPixmap.fromImage(image).scaled(w, 220, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation)
        self.pic.setPixmap(pm)
        self.pic.show()

    def _set_alts(self, nearest) -> None:
        self._clear_alts()
        for i, it in enumerate(list(nearest)[:4]):
            b = QPushButton(f"{it.number}\n{it.description[:34]}")
            b.setMinimumHeight(56)
            b.setStyleSheet(f"text-align:left;font-size:13px;border-radius:6px;color:{TEXT};background:#ffffff;border:1px solid #c5c9d0;")
            b.clicked.connect(lambda _=False, it=it: self._choose(it))
            self.alts_lay.addWidget(b, i // 2, i % 2)

    # ------------------------------------------------------------------ actions
    def _tick(self):
        self._remaining -= 0.05
        self.bar.setValue(int(max(0.0, self._remaining / self._seconds) * 1000))
        if self._remaining <= 0:
            self._timer.stop()
            self._ok("accepted")

    def _ok(self, outcome: str = ""):
        if not self.pending or self.state == "input":
            return
        p = self.pending
        outcome = outcome or ("accepted" if self.state == "accept" else "confirmed")
        self.set_idle()
        self.confirmed.emit(p["number"], p["source"], p["in_list"], p["description"], p.get("note", ""), outcome)

    def _skip(self):
        """Discard: drop what was captured (green or orange card), or leave the type-it screen."""
        n = self.pending["number"] if self.pending else ""
        was_input = self.state == "input"
        self.set_idle()
        (self.backed if was_input else self.discarded).emit(n)

    def _undo(self):
        n = self.pending["number"] if self.pending else ""
        self.set_idle("undone")
        self.undone.emit(n)

    def _choose(self, it):
        if not self.pending:
            return
        note = self.pending.get("note", "") if self.state == "input" else "corrected"
        self.pending.update(number=it.key, description=it.description, in_list=True, note=note, source="MANUAL"
                            if self.state == "input" else self.pending["source"])
        p = self.pending
        self.set_idle()
        self.confirmed.emit(p["number"], p["source"], p["in_list"], p["description"], p["note"], "corrected")

    def _manual(self):
        text = self.manual.text().strip()
        if not text:
            return
        self.manual.clear()
        in_list, description = False, ""
        if self.lookup:
            in_list, description, _ = self.lookup(text)
        note = self.pending.get("note", "") if (self.pending and self.state == "input") else ""
        self.set_idle()
        self.confirmed.emit(text, "MANUAL", in_list, description, note, "typed")

    def _clear_alts(self):
        _remove_all(self.alts_lay)

    def keyPressEvent(self, ev):
        key, text = ev.key(), ev.text()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.state in ("accept", "review"):
            self._ok("confirmed")
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.manual.text().strip():
            self._manual()
        elif key == Qt.Key.Key_Escape and self.state in ("accept", "review", "input"):
            self._skip()
        elif text.isdigit():                          # a hardware keyboard types into the number field
            self.manual.insert(text)
        elif key == Qt.Key.Key_Backspace:
            self.manual.backspace()
        else:
            super().keyPressEvent(ev)


class TextPicker(QDialog):
    """Pick or type a short text (the location, the operator) with a finger: choices and recent
    values as big buttons, and a full on-screen keyboard (digits, letters, umlauts, space).
    An optional ``pattern`` (regex) refuses anything else."""

    def __init__(self, parent=None, current: str = "", choices=(), recent=(), title: str = "Location",
                 prompt: str = "Where are you scanning?  Tap a location or type it",
                 placeholder: str = "room / hall",
                 code_hint: str = "Or scan a location code: a QR code reading  LOC: <name>  switches the "
                                  "location without touching the screen.",
                 pattern: str = ""):
        super().__init__(parent)
        import re
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(1040, 700)
        self._rx = re.compile(pattern, re.I) if pattern else None
        v = QVBoxLayout(self)
        v.setSpacing(10)
        lbl = QLabel(prompt)
        lbl.setStyleSheet("font-size:16px;color:#555;")
        v.addWidget(lbl)
        self.edit = QLineEdit(current)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setMinimumHeight(60)
        self.edit.setStyleSheet(f"font-size:26px;color:{TEXT};background:#ffffff;")
        v.addWidget(self.edit)

        seen: list[str] = []
        for r in list(recent) + list(choices):
            r = str(r).strip()
            if r and r not in seen:
                seen.append(r)
        if seen:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setMaximumHeight(190)
            inner = QWidget()
            grid = QGridLayout(inner)
            grid.setSpacing(8)
            grid.setContentsMargins(0, 0, 0, 0)
            cols = 4
            for i, name in enumerate(seen):
                b = QPushButton(name)
                b.setMinimumHeight(54)
                b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                style = f"font-size:16px;border-radius:8px;text-align:left;padding:0 12px;color:{TEXT};background:#ffffff;border:1px solid #c5c9d0;"
                if name in recent:
                    style += "border:2px solid #3c8ce6;"
                if name == current:
                    style += "background:#e3effc;"
                b.setStyleSheet(style)
                b.clicked.connect(lambda _=False, n=name: self._choose(n))
                grid.addWidget(b, i // cols, i % cols)
            grid.setRowStretch((len(seen) + cols - 1) // cols, 1)
            scroll.setWidget(inner)
            v.addWidget(scroll)

        self.pad = NumberPad(rows=FULL_ROWS, height=46)
        self.pad.attach(self.edit)
        self.pad.entered.connect(self.accept)
        v.addWidget(self.pad)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("font-size:12px;color:#b35c00;")
        v.addWidget(self.hint)
        hint = QLabel(code_hint)
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size:12px;color:#777;")
        v.addWidget(hint)
        v.addStretch(1)
        row = QHBoxLayout()
        self.ok = QPushButton("OK  ✓")
        self.ok.setMinimumHeight(60)
        self.ok.setStyleSheet(BTN_OK)
        self.ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.setMinimumHeight(60)
        cancel.setStyleSheet(BTN_PLAIN)
        cancel.clicked.connect(self.reject)
        row.addWidget(self.ok, 2)
        row.addWidget(cancel, 1)
        v.addLayout(row)
        self.edit.textChanged.connect(self._validate)
        self.edit.returnPressed.connect(self.accept)
        self._validate(self.edit.text())

    def valid(self, text: str | None = None) -> bool:
        text = (self.edit.text() if text is None else text).strip()
        if self._rx is None:
            return True
        return bool(text) and self._rx.fullmatch(text) is not None

    def _validate(self, text: str) -> None:
        ok = self.valid(text)
        self.ok.setEnabled(ok)
        self.hint.setText("" if ok or not text.strip() else "that does not look like a valid code")

    def accept(self) -> None:
        if self.valid():
            super().accept()

    def _choose(self, name: str) -> None:
        self.edit.setText(name)
        if self.valid():
            super().accept()

    def value(self) -> str:
        return self.edit.text().strip()

    @classmethod
    def pick(cls, parent, current: str, choices=(), recent=(), **kw) -> str | None:
        dlg = cls(parent, current, choices, recent, **kw)
        return dlg.value() if dlg.exec() == QDialog.DialogCode.Accepted else None


class LocationPicker(TextPicker):
    """The location picker (TextPicker with its default texts)."""


class OperatorPicker(TextPicker):
    """Who is scanning: the person types their own short code (e.g. ``123ab``); recent codes
    are one tap; ``pattern`` keeps typos out."""

    def __init__(self, parent=None, current: str = "", choices=(), recent=(), pattern: str = ""):
        super().__init__(parent, current, choices, recent, title="Operator",
                         prompt="Who is scanning?  Enter your code",
                         placeholder="your code, e.g. 123ab",
                         code_hint="Or scan your badge: a QR code reading  OP: <code>  sets the operator "
                                   "without touching the screen.",
                         pattern=pattern)
        self.edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.edit.setStyleSheet(f"font-size:28px;letter-spacing:2px;color:{TEXT};background:#ffffff;")

    def valid(self, text: str | None = None) -> bool:
        text = (self.edit.text() if text is None else text).strip()
        return bool(text) and super().valid(text)

    @classmethod
    def pick(cls, parent, current: str, choices=(), recent=(), pattern: str = "") -> str | None:
        dlg = cls(parent, current, choices, recent, pattern)
        return dlg.value() if dlg.exec() == QDialog.DialogCode.Accepted else None


class HistoryStrip(QWidget):
    delete_requested = Signal(int)
    relocate_requested = Signal(int)
    undo_last_requested = Signal()                 # the "Undo last scan" button

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
        self.undo_btn = QPushButton("Undo last scan")
        self.undo_btn.setMinimumHeight(54)
        self.undo_btn.setStyleSheet(BTN_DISCARD.replace("font-size:18px", "font-size:15px"))
        self.undo_btn.clicked.connect(self.undo_last_requested.emit)
        self.lay.addWidget(self.undo_btn)

    def set_scans(self, scans) -> None:
        _remove_all(self.lay, keep_first=1, keep_last=2)
        self.undo_btn.setEnabled(bool(scans))
        for i, s in enumerate(scans):
            b = QToolButton()
            b.setText(f"{s.number}\n{(s.location or '-')[:14]}")
            b.setMinimumSize(100, 58)
            color = "#2ea043" if s.in_list else "#f08c00"
            b.setStyleSheet(f"QToolButton{{border:2px solid {color};border-radius:8px;font-size:12px;color:{TEXT};background:#ffffff;}}")
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

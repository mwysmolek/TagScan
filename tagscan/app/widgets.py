"""Touch-friendly widgets: the camera view with overlays, the result card, the history strip."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer, QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF, QBrush
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget, QMenu, QGridLayout)

GREEN = QColor(46, 160, 67)
ORANGE = QColor(240, 140, 0)
BLUE = QColor(60, 140, 230)
GREY = QColor(200, 200, 200)


class VideoView(QWidget):
    """Shows the latest frame, scaled to fit, with coloured quads on top."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._img: QImage | None = None
        self._quads: list[tuple[list, QColor]] = []
        self._quad_age = QTimer(self)
        self._quad_age.setSingleShot(True)
        self._quad_age.timeout.connect(self._clear_quads)
        self._text = ""
        self._flash: QColor | None = None
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self.setStyleSheet("background:#111;")

    def set_frame(self, img: QImage) -> None:
        self._img = img
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

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(17, 17, 17))
        if self._img is None or self._img.isNull():
            p.setPen(GREY)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text or "no camera")
            return
        iw, ih = self._img.width(), self._img.height()
        s = min(self.width() / iw, self.height() / ih)
        dw, dh = iw * s, ih * s
        ox, oy = (self.width() - dw) / 2, (self.height() - dh) / 2
        target = QRectF(ox, oy, dw, dh)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(target, self._img)
        for quad, color in self._quads:
            pen = QPen(color, 3)
            p.setPen(pen)
            poly = QPolygonF([QPointF(ox + x * s, oy + y * s) for x, y in quad])
            p.drawPolygon(poly)
        if self._flash:
            pen = QPen(self._flash, 14)
            p.setPen(pen)
            p.drawRect(target.adjusted(7, 7, -7, -7))
        if self._text:
            p.setPen(Qt.GlobalColor.white)
            f = QFont()
            f.setPointSize(11)
            p.setFont(f)
            p.fillRect(QRectF(ox, oy + dh - 28, dw, 28), QColor(0, 0, 0, 120))
            p.drawText(QRectF(ox + 8, oy + dh - 28, dw - 16, 28), Qt.AlignmentFlag.AlignVCenter, self._text)


class ResultCard(QFrame):
    """Idle -> (seen) -> accept with countdown | review with buttons -> idle."""
    confirmed = Signal(str, str, bool, str, str)   # number, source, in_list, description, note
    skipped = Signal(str)                          # number
    undone = Signal(str)                           # number
    state_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("card")
        self.setMinimumWidth(330)
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
        self.ok_btn.setMinimumHeight(56)
        self.ok_btn.setStyleSheet("font-size:20px;font-weight:600;background:#2ea043;color:white;border-radius:8px;")
        self.skip_btn = QPushButton("Skip")
        self.skip_btn.setMinimumHeight(56)
        self.skip_btn.setStyleSheet("font-size:18px;border-radius:8px;")
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.setMinimumHeight(56)
        self.undo_btn.setStyleSheet("font-size:18px;background:#f08c00;color:white;border-radius:8px;")
        btns.addWidget(self.ok_btn, 2)
        btns.addWidget(self.skip_btn, 1)
        btns.addWidget(self.undo_btn, 1)

        manual = QHBoxLayout()
        self.manual = QLineEdit()
        self.manual.setPlaceholderText("type a number …")
        self.manual.setMinimumHeight(44)
        self.manual.setStyleSheet("font-size:18px;")
        self.manual_btn = QPushButton("Add")
        self.manual_btn.setMinimumHeight(44)
        self.manual_btn.setStyleSheet("font-size:16px;")
        manual.addWidget(self.manual, 1)
        manual.addWidget(self.manual_btn)

        lay.addWidget(self.head)
        lay.addWidget(self.number)
        lay.addWidget(self.desc)
        lay.addWidget(self.meta)
        lay.addWidget(self.bar)
        lay.addWidget(self.alts)
        lay.addStretch(1)
        lay.addLayout(btns)
        lay.addLayout(manual)

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
            b.setMinimumHeight(52)
            b.setStyleSheet("text-align:left;font-size:13px;border-radius:6px;")
            b.clicked.connect(lambda _=False, it=it: self._choose(it))
            self.alts_lay.addWidget(b, i // 2, i % 2)
        self.ok_btn.show()
        self.skip_btn.show()
        self.undo_btn.hide()
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
        while self.alts_lay.count():
            w = self.alts_lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.state in ("accept", "review"):
            self._ok()
        elif ev.key() == Qt.Key.Key_Escape and self.state == "accept":
            self._undo()
        elif ev.key() == Qt.Key.Key_Escape and self.state == "review":
            self._skip()
        else:
            super().keyPressEvent(ev)


class HistoryStrip(QWidget):
    delete_requested = Signal(int)
    relocate_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(6, 4, 6, 4)
        self.lay.setSpacing(6)
        self.setFixedHeight(66)
        self.title = QLabel("recent")
        self.title.setStyleSheet("color:#888;")
        self.lay.addWidget(self.title)
        self.lay.addStretch(1)

    def set_scans(self, scans) -> None:
        while self.lay.count() > 2:
            w = self.lay.takeAt(1).widget()
            if w:
                w.deleteLater()
        for i, s in enumerate(scans):
            b = QToolButton()
            b.setText(f"{s.number}\n{(s.location or '-')[:14]}")
            b.setMinimumSize(96, 54)
            color = "#2ea043" if s.in_list else "#f08c00"
            b.setStyleSheet(f"QToolButton{{border:2px solid {color};border-radius:8px;font-size:12px;}}")
            b.setToolTip(f"{s.ts}  {s.source}\n{s.description}")
            b.clicked.connect(lambda _=False, sid=s.id, btn=b: self._menu(sid, btn))
            self.lay.insertWidget(1 + i, b)

    def _menu(self, sid: int, btn: QToolButton):
        m = QMenu(self)
        m.setStyleSheet("QMenu{font-size:16px;} QMenu::item{padding:10px 24px;}")
        a1 = m.addAction("Delete this scan")
        a2 = m.addAction("Change location …")
        act = m.exec(btn.mapToGlobal(btn.rect().topLeft()))
        if act == a1:
            self.delete_requested.emit(sid)
        elif act == a2:
            self.relocate_requested.emit(sid)

"""The main window: camera on the left, result card on the right, recent scans below."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QThread, Signal, QMutex, QWaitCondition, QSettings, QStandardPaths, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton, QSlider, QToolBar, QVBoxLayout, QWidget, QStatusBar)

from .. import __version__
from ..catalog import Catalog
from ..engine import Scanner
from ..profile import Profile
from ..store import Store
from .camera import CameraSource, FolderSource, bgr_to_qimage
from .widgets import GREEN, ORANGE, BLUE, HistoryStrip, ResultCard, VideoView

log = logging.getLogger("tagscan")


class _FrameQueue:
    """Latest-frame mailbox shared by a producer (GUI) and one consumer thread."""

    def __init__(self):
        self.mutex = QMutex()
        self.cond = QWaitCondition()
        self.frame = None
        self.force = False
        self.stop = False

    def put(self, frame, force=False):
        self.mutex.lock()
        self.frame = frame
        self.force = self.force or force
        self.cond.wakeOne()
        self.mutex.unlock()

    def take(self):
        self.mutex.lock()
        while self.frame is None and not self.stop:
            self.cond.wait(self.mutex)
        frame, force, stop = self.frame, self.force, self.stop
        self.frame, self.force = None, False
        self.mutex.unlock()
        return (None, False) if stop else (frame, force)

    def close(self):
        self.mutex.lock()
        self.stop = True
        self.cond.wakeAll()
        self.mutex.unlock()


class CodeWorker(QThread):
    """Decodes QR/barcodes on every frame it gets (a few ms each)."""
    result = Signal(object, object, object)

    def __init__(self, scanner_holder, parent=None):
        super().__init__(parent)
        self.holder = scanner_holder
        self.q = _FrameQueue()

    def run(self):
        while True:
            frame, _ = self.q.take()
            if frame is None:
                return
            sc = self.holder.get("scanner")
            if sc is None:
                continue
            try:
                obs, dec = sc.process_codes(frame)
            except Exception:
                log.exception("code decode failed")
                continue
            if obs or dec.status != "none":
                self.result.emit(obs, dec, frame)


class OcrWorker(QThread):
    """Owns the Scanner (RapidOCR lives here); reads printed text, throttled by the profile."""
    ready = Signal(str)
    result = Signal(object, object, object)
    timing = Signal(float)

    def __init__(self, profile: Profile, catalog: Catalog, holder: dict, parent=None):
        super().__init__(parent)
        self.profile, self.catalog, self.holder = profile, catalog, holder
        self.q = _FrameQueue()

    def run(self):
        t = time.time()
        sc = Scanner(self.profile, self.catalog)
        self.holder["scanner"] = sc
        try:
            sc.warmup()
        except Exception as ex:
            log.exception("OCR warmup failed")
            self.ready.emit(f"OCR unavailable: {ex}")
        else:
            self.ready.emit(f"engine ready ({time.time() - t:.1f}s)")
        while True:
            frame, force = self.q.take()
            if frame is None:
                return
            try:
                obs, dec, ran = sc.process_text(frame, force=force)
            except Exception:
                log.exception("OCR failed")
                continue
            if ran:
                self.timing.emit(sc.stats["ocr_last"])
                self.result.emit(obs, dec, frame)
            else:
                # throttled: sleep until the next slot instead of spinning on new frames
                self.msleep(max(10, int(self.profile.ocr_interval * 1000 / 4)))


class ScanWorker:
    """Facade over the two threads, so the window talks to one object."""
    def __init__(self, profile: Profile, catalog: Catalog, parent=None):
        self.profile, self.catalog = profile, catalog
        self.holder: dict = {}
        self.ocr = OcrWorker(profile, catalog, self.holder, parent)
        self.codes = CodeWorker(self.holder, parent)
        self.ready = self.ocr.ready
        self.timing = self.ocr.timing

    @property
    def scanner(self) -> Scanner | None:
        return self.holder.get("scanner")

    def start(self):
        self.ocr.start()
        self.codes.start()

    def connect_result(self, slot):
        self.ocr.result.connect(slot)
        self.codes.result.connect(slot)

    def submit(self, frame, force: bool = False) -> None:
        self.codes.q.put(frame)
        self.ocr.q.put(frame, force)

    def stop(self) -> None:
        self.codes.q.close()
        self.ocr.q.close()
        self.codes.wait(3000)
        self.ocr.wait(3000)

    def set_catalog(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.ocr.catalog = catalog
        if self.scanner:
            self.scanner.catalog = catalog

    def suppress(self, number: str, seconds: float = 8.0) -> None:
        if self.scanner and number:
            self.scanner.suppress(number, seconds)

    def reset(self) -> None:
        if self.scanner:
            self.scanner.reset()


class MainWindow(QMainWindow):
    def __init__(self, profile: Profile | None = None, catalog: Catalog | None = None,
                 data_dir: str | None = None, source=None, settings: QSettings | None = None):
        super().__init__()
        self.setWindowTitle(f"kayEi TagScan {__version__}")
        self.settings = settings or QSettings("kayEi", "TagScan")
        self.data_dir = Path(data_dir or self.settings.value(
            "data_dir", os.path.join(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation), "TagScan")))
        self.store = Store(self.data_dir)
        self.profile = profile or Profile()
        self.catalog = catalog or Catalog()
        self.source = None
        self._zoom = 1.0
        self._last_submit = 0.0
        self._last_frame = None
        self._busy_decision = False

        self._build_ui()
        self.worker = ScanWorker(self.profile, self.catalog, self)
        self.worker.ready.connect(self._engine_ready)
        self.worker.timing.connect(self._ocr_timing)
        self.worker.connect_result(self._on_result)
        self.worker.start()

        if source is not None:
            self.set_source(source)
        else:
            self._restore_list()
            QTimer.singleShot(200, self.use_camera)
        self.resize(1280, 800)

    # ------------------------------------------------------------------ ui
    def _build_ui(self):
        tb = QToolBar("main")
        tb.setMovable(False)
        tb.setStyleSheet("QToolBar{spacing:8px;padding:4px;} QToolButton,QPushButton{font-size:14px;min-height:36px;padding:4px 10px;}")
        self.addToolBar(tb)

        self.list_btn = QPushButton("List …")
        self.list_btn.clicked.connect(self.choose_list)
        tb.addWidget(self.list_btn)
        self.sheet_box = QComboBox()
        self.sheet_box.setMinimumWidth(150)
        self.sheet_box.currentTextChanged.connect(self._sheet_changed)
        tb.addWidget(self.sheet_box)
        tb.addSeparator()
        tb.addWidget(QLabel(" Location: "))
        self.location = QLineEdit(self.settings.value("location", ""))
        self.location.setPlaceholderText("room / hall")
        self.location.setMinimumWidth(160)
        self.location.setStyleSheet("font-size:15px;min-height:34px;")
        self.location.textChanged.connect(lambda t: self.settings.setValue("location", t))
        tb.addWidget(self.location)
        tb.addSeparator()
        self.camera_box = QComboBox()
        self.camera_box.setMinimumWidth(180)
        tb.addWidget(self.camera_box)
        self.cam_btn = QPushButton("Camera")
        self.cam_btn.clicked.connect(self.use_camera)
        tb.addWidget(self.cam_btn)
        self.photos_btn = QPushButton("Photos …")
        self.photos_btn.clicked.connect(self.choose_photos)
        tb.addWidget(self.photos_btn)
        tb.addSeparator()
        tb.addWidget(QLabel(" Zoom "))
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(10, 30)
        self.zoom.setValue(int(float(self.settings.value("zoom", 10))))
        self.zoom.setFixedWidth(140)
        self.zoom.valueChanged.connect(self._zoom_changed)
        self._zoom_changed(self.zoom.value())
        tb.addWidget(self.zoom)
        tb.addSeparator()
        self.export_btn = QPushButton("Export Excel")
        self.export_btn.clicked.connect(self.export)
        tb.addWidget(self.export_btn)

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(6, 6, 6, 6)
        h = QHBoxLayout()
        self.view = VideoView()
        self.card = ResultCard()
        self.card.lookup = self._lookup
        self.card.confirmed.connect(self._confirmed)
        self.card.skipped.connect(self._skipped)
        self.card.undone.connect(self._undone)
        self.card.state_changed.connect(self._card_state)
        h.addWidget(self.view, 3)
        h.addWidget(self.card, 1)
        v.addLayout(h, 1)
        self.history = HistoryStrip()
        self.history.delete_requested.connect(self._delete_scan)
        self.history.relocate_requested.connect(self._relocate_scan)
        v.addWidget(self.history)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.engine_lbl = QLabel("starting engine …")
        self.timing_lbl = QLabel("")
        self.timing_lbl.setStyleSheet("color:#777;")
        self.count_lbl = QLabel("")
        self.statusBar().addWidget(self.engine_lbl, 1)
        self.statusBar().addPermanentWidget(self.timing_lbl)
        self.statusBar().addPermanentWidget(self.count_lbl)
        self._refresh_history()

        force = QAction("force OCR now", self)
        force.setShortcut(QKeySequence(Qt.Key.Key_Space))
        force.triggered.connect(lambda: self._last_frame is not None and self.worker.submit(self._last_frame, force=True))
        self.addAction(force)

    # ------------------------------------------------------------------ list / profile
    def _restore_list(self):
        path = self.settings.value("list_path", "")
        if path and Path(path).exists():
            self.load_list(path, self.settings.value("sheet", None))

    def choose_list(self):
        start = self.settings.value("list_path", "") or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Choose the list of known numbers", start,
                                              "Lists (*.xlsx *.xlsm *.csv *.txt);;All files (*)")
        if path:
            self.load_list(path, None)

    def load_list(self, path: str, sheet: str | None):
        prof = Profile.find_near(path)
        if prof:
            self.profile = prof
            self.worker.profile = prof
            self.worker.ocr.profile = prof
            sc = self.worker.scanner
            if sc:
                sc.profile = prof
                sc.parser.__init__(prof)
                sc.ocr.parser = sc.parser
        spec = self.profile.list
        if sheet:
            spec.sheet = sheet
        self.sheet_box.blockSignals(True)
        self.sheet_box.clear()
        if path.lower().endswith((".xlsx", ".xlsm")):
            names = Catalog.sheet_names(path)
            self.sheet_box.addItems(names)
            if spec.sheet in names:
                self.sheet_box.setCurrentText(spec.sheet)
            else:
                spec.sheet = names[0] if names else None
            self.sheet_box.show()
        else:
            self.sheet_box.hide()
        self.sheet_box.blockSignals(False)
        try:
            self.catalog = Catalog.load(path, spec)
        except Exception as ex:
            log.exception("list load failed")
            QMessageBox.warning(self, "List", f"Could not read the list:\n{ex}")
            return
        self.worker.set_catalog(self.catalog)
        self.settings.setValue("list_path", path)
        self.settings.setValue("sheet", spec.sheet or "")
        self.list_btn.setText(f"List: {Path(path).name}")
        self.engine_lbl.setText(f"{len(self.catalog)} items · profile {self.profile.name}"
                                + ("" if prof else " (no tagscan_profile.json next to the list: generic profile)"))
        self._refresh_history()

    def _sheet_changed(self, name: str):
        path = self.settings.value("list_path", "")
        if name and path:
            self.load_list(path, name)

    def _lookup(self, number: str):
        it = self.catalog.get(number)
        return (it is not None, it.description if it else "", self.catalog.nearest(number, 4))

    # ------------------------------------------------------------------ sources
    def set_source(self, source):
        if self.source is not None:
            self.source.stop()
            self.source.frame.disconnect(self._on_frame)
            self.source.status.disconnect(self._source_status)
            self.source.deleteLater()
        self.source = source
        source.frame.connect(self._on_frame)
        source.status.connect(self._source_status)
        self.card.set_idle()
        source.start()

    def use_camera(self):
        cam = CameraSource(self)
        devices = cam.devices()
        self.camera_box.blockSignals(True)
        self.camera_box.clear()
        self.camera_box.addItems([d.description() for d in devices])
        idx = self.settings.value("camera_index", None)
        idx = int(idx) if idx not in (None, "") else cam.preferred_index(devices)
        if 0 <= idx < len(devices):
            self.camera_box.setCurrentIndex(idx)
        self.camera_box.blockSignals(False)
        try:
            self.camera_box.currentIndexChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.camera_box.currentIndexChanged.connect(self._camera_changed)
        self.set_source(cam)
        if devices:
            cam.select(idx)
        else:
            self.view.set_text("no camera found: use Photos … to test with images")

    def _camera_changed(self, idx: int):
        if isinstance(self.source, CameraSource) and idx >= 0:
            self.settings.setValue("camera_index", idx)
            self.source.select(idx)

    def choose_photos(self):
        start = self.settings.value("photos_dir", "") or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Folder with label photos", start)
        if folder:
            self.settings.setValue("photos_dir", folder)
            self.set_source(FolderSource(folder, parent=self))

    def _card_state(self, state: str):
        if isinstance(self.source, FolderSource):
            self.source.paused = state != "idle"

    def _source_status(self, text: str):
        self.view.set_text(text)

    def _zoom_changed(self, v: int):
        self._zoom = v / 10.0
        self.settings.setValue("zoom", v)

    # ------------------------------------------------------------------ frames
    def _on_frame(self, bgr: np.ndarray):
        if self._zoom > 1.01:
            h, w = bgr.shape[:2]
            cw, ch = int(w / self._zoom), int(h / self._zoom)
            x0, y0 = (w - cw) // 2, (h - ch) // 2
            bgr = np.ascontiguousarray(bgr[y0:y0 + ch, x0:x0 + cw])
        self._last_frame = bgr
        self.view.set_frame(bgr_to_qimage(bgr))
        now = time.time()
        if now - self._last_submit >= 0.04 and self.card.state == "idle":
            self._last_submit = now
            self.worker.submit(bgr)

    def _ocr_timing(self, seconds: float):
        self._ocr_times = (getattr(self, "_ocr_times", []) + [seconds])[-20:]
        avg = sum(self._ocr_times) / len(self._ocr_times)
        self.timing_lbl.setText(f"OCR {seconds:.2f}s (avg {avg:.2f}s)")

    def _engine_ready(self, msg: str):
        self.engine_lbl.setText(msg + (f" · {len(self.catalog)} items" if len(self.catalog) else " · no list loaded"))

    def _on_result(self, obs, dec, frame):
        quads = []
        for o in obs:
            color = GREEN if o.in_list else ORANGE
            if o.source == "OCR" and dec.status in ("none", "seen"):
                color = BLUE
            quads += [(q, color) for q in o.boxes]
        if quads:
            self.view.set_quads(quads)
        if self.card.state != "idle":
            return
        if dec.status == "seen":
            self.card.set_seen(dec.observation.number, dec.votes)
        elif dec.status in ("accept", "review"):
            o = dec.observation
            it = self.catalog.get(o.number)
            desc = it.description if it else ""
            earlier = self.store.seen(o.number)
            self._pending_frame = frame
            if dec.status == "accept" and not earlier:
                self.card.set_accept(o.number, desc, o.source, self.profile.auto_confirm_seconds)
                self.view.flash(GREEN)
            else:
                reason = dec.message
                if earlier:
                    e = earlier[-1]
                    reason = f"already scanned {e.ts[5:16]} at {e.location or '-'}"
                self.card.set_review(o.number, desc, o.source, o.in_list, reason,
                                     nearest=[] if o.in_list else self.catalog.nearest(o.number, 4))
                self.view.flash(ORANGE)
            self.card.setFocus()

    # ------------------------------------------------------------------ decisions
    def _confirmed(self, number, source, in_list, description, note):
        if source == "MANUAL":
            frame = self._last_frame
        else:
            frame = getattr(self, "_pending_frame", None)
        scan = self.store.add(number, description, self.location.text().strip(), source, in_list,
                              image=frame, note=note)
        log.info("scan %s %s %s in_list=%s", scan.id, number, source, in_list)
        self.worker.suppress(number)
        self.worker.reset()
        self._refresh_history()
        self.view.flash(GREEN if in_list else ORANGE, 500)

    def _skipped(self, number):
        self.worker.suppress(number, 6.0)
        self.worker.reset()

    def _undone(self, number):
        self.worker.suppress(number, 10.0)
        self.worker.reset()

    def _refresh_history(self):
        self.history.set_scans(self.store.recent(10))
        n = self.store.count()
        self.count_lbl.setText(f"{n} scans · {self.data_dir}")

    def _delete_scan(self, sid: int):
        self.store.delete(sid)
        self._refresh_history()

    def _relocate_scan(self, sid: int):
        text, ok = QInputDialog.getText(self, "Location", "New location for this scan:", text=self.location.text())
        if ok:
            self.store.update(sid, location=text.strip())
            self._refresh_history()

    def export(self):
        default = str(self.data_dir / self.store.default_export_name())
        path, _ = QFileDialog.getSaveFileName(self, "Export scans", default, "Excel (*.xlsx)")
        if not path:
            return
        try:
            out = self.store.export_xlsx(path, self.catalog)
        except Exception as ex:
            log.exception("export failed")
            QMessageBox.warning(self, "Export", f"Export failed:\n{ex}")
            return
        self.statusBar().showMessage(f"exported {out}", 8000)

    def closeEvent(self, ev):
        if self.source is not None:
            self.source.stop()
        self.worker.stop()
        super().closeEvent(ev)

"""The main window: camera on the left, result card on the right, recent scans below."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import Qt, QThread, Signal, QMutex, QWaitCondition, QSettings, QStandardPaths, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPushButton, QSizePolicy, QToolBar, QVBoxLayout, QWidget, QStatusBar)

from .. import __version__
from ..catalog import Catalog
from ..engine import Scanner
from ..profile import Profile
from ..store import Store
from .aim import Box, Stability, box_rect, crop_box
from .assets import asset_path
from . import printing
from ..labels import render_label
from .camera import CameraSource, FolderSource, bgr_to_qimage
from .sysinfo import rss_mb
from .widgets import (GREEN, ORANGE, BLUE, DescriptionPicker, HistoryStrip, LocationPicker, OperatorPicker,
                      ResultCard, VideoView, TOUCH)

log = logging.getLogger("tagscan")

PREVIEW_MAX = 1024          # the preview is never painted larger than this (or the view) on a side
ZOOM_MIN, ZOOM_MAX = 1.0, 3.0
FULL_CODES_INTERVAL = 0.35  # with the aim box active, the whole frame is still scanned for codes this often
WHITE = QColor(255, 255, 255, 220)


@dataclass
class Job:
    """What a decoder works on: a frame or the aim box crop, with its position in the frame.
    A crop job carries the whole frame too (``full``), so the code decoder can scan it for a
    code outside the box on its own cadence without competing for the mailbox."""
    bgr: np.ndarray
    origin: tuple = (0, 0)
    box: "Box | None" = None
    full: "np.ndarray | None" = None


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
    """Decodes QR/barcodes on the jobs it gets (tens of ms each on a tablet). A crop job that
    yields nothing is followed, every ``FULL_CODES_INTERVAL``, by a scan of the whole frame."""
    result = Signal(object, object, object)

    def __init__(self, scanner_holder, parent=None):
        super().__init__(parent)
        self.holder = scanner_holder
        self.q = _FrameQueue()
        self._last_full = 0.0

    def run(self):
        while True:
            job, _ = self.q.take()
            if job is None:
                return
            sc = self.holder.get("scanner")
            if sc is None:
                continue
            try:
                obs, dec = sc.process_codes(job.bgr)
                if obs or dec.status != "none":
                    self.result.emit(obs, dec, job)
                    continue
                now = time.perf_counter()
                if job.full is not None and now - self._last_full >= FULL_CODES_INTERVAL:
                    self._last_full = now
                    obs, dec = sc.process_codes(job.full)
                    if obs or dec.status != "none":
                        self.result.emit(obs, dec, Job(job.full))
            except Exception:
                log.exception("code decode failed")


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
            job, force = self.q.take()
            if job is None:
                return
            try:
                obs, dec, ran = sc.process_text(job.bgr, force=force)
            except Exception:
                log.exception("OCR failed")
                continue
            if ran:
                self.timing.emit(sc.stats["ocr_last"])
                self.result.emit(obs, dec, job)
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
        self._last_code = 0.0

    @property
    def scanner(self) -> Scanner | None:
        return self.holder.get("scanner")

    def start(self):
        self.ocr.start()
        self.codes.start()

    def connect_result(self, slot):
        self.ocr.result.connect(slot)
        self.codes.result.connect(slot)

    def submit(self, job: Job, force: bool = False) -> None:
        """Whole-frame mode: codes at most every ``code_interval``, OCR throttled by the OCR
        thread itself (``ocr_interval``). Both mailboxes keep only the newest job."""
        self.submit_codes(job, force)
        self.ocr.q.put(job, force)

    def submit_codes(self, job: Job, force: bool = False) -> None:
        now = time.perf_counter()
        if force or now - self._last_code >= self.profile.code_interval:
            self._last_code = now
            self.codes.q.put(job)

    def submit_text(self, job: Job, force: bool = False) -> None:
        self.ocr.q.put(job, force)

    def stop(self) -> None:
        self.codes.q.close()
        self.ocr.q.close()
        self.codes.wait(3000)
        self.ocr.wait(3000)

    def set_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.ocr.profile = profile
        if self.scanner:
            self.scanner.apply_profile(profile)

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
        self.location_text = str(self.settings.value("location", "") or "")
        self.operator_text = str(self.settings.value("operator", "") or "")
        self._zoom = 1.0
        self._last_frame = None
        self._frames = 0
        self._stats_ticks = 0
        # the aim box (live camera only): the operator holds the label inside it
        self._box_active = False
        self._box: Box | None = None
        self._box_center = (0.5, 0.5)
        self._stab = Stability()
        self._box_fail = 0              # empty OCR passes on the current steady box content
        self._need_change = False       # after a decision: wait until something new is in the box
        self._last_box_ocr = 0.0
        self._armed_until = 0.0         # tap-to-scan: the box is read until this time
        self._armed = False
        self._last_crop = None
        self._last_obs: list = []
        self._ctx: dict = {}            # what the pending card was read from (for the training record)

        self._build_ui()
        self.worker = ScanWorker(self.profile, self.catalog, self)
        self.worker.ready.connect(self._engine_ready)
        self.worker.timing.connect(self._ocr_timing)
        self.worker.connect_result(self._on_result)
        self.worker.start()

        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start(1000)

        if source is not None:
            self.set_source(source)
        else:
            self._restore_list()
            QTimer.singleShot(200, self.use_camera)
        self.resize(1280, 800)

    # ------------------------------------------------------------------ ui
    def _build_ui(self):
        style = (f"QToolBar{{spacing:8px;padding:4px;}} QToolButton,QPushButton{{font-size:15px;min-height:{TOUCH}px;padding:4px 14px;}}"
                 f"QComboBox{{font-size:15px;min-height:{TOUCH}px;}} QComboBox QAbstractItemView::item{{min-height:40px;}}")

        tb = QToolBar("list")
        tb.setMovable(False)
        tb.setStyleSheet(style)
        self.addToolBar(tb)
        logo = QPixmap(str(asset_path("logo.png")))
        if not logo.isNull():
            self.setWindowIcon(QIcon(logo))
            mark = QLabel()
            mark.setPixmap(logo.scaled(44, 44, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            mark.setToolTip(f"kayEi TagScan {__version__}")
            tb.addWidget(mark)
            tb.addSeparator()
        self.list_btn = QPushButton("List …")
        self.list_btn.setToolTip("One list file, or a folder of lists: one file per Kostenstelle")
        self.list_btn.clicked.connect(self.choose_list)
        tb.addWidget(self.list_btn)
        self.lists_btn = QPushButton("Lists folder …")
        self.lists_btn.setToolTip("Load every list in a folder; each file name is its Kostenstelle")
        self.lists_btn.clicked.connect(self.choose_lists_folder)
        tb.addWidget(self.lists_btn)
        self.sheet_box = QComboBox()
        self.sheet_box.setMinimumWidth(170)
        self.sheet_box.currentTextChanged.connect(self._sheet_changed)
        tb.addWidget(self.sheet_box)
        tb.addSeparator()
        self.loc_btn = QPushButton()
        self.loc_btn.setMinimumWidth(240)
        self.loc_btn.setStyleSheet("font-weight:600;text-align:left;")
        self.loc_btn.clicked.connect(self.pick_location)
        tb.addWidget(self.loc_btn)
        self.op_btn = QPushButton()
        self.op_btn.setMinimumWidth(200)
        self.op_btn.clicked.connect(self.pick_operator)
        tb.addWidget(self.op_btn)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.export_btn = QPushButton("Export Excel")
        self.export_btn.clicked.connect(self.export)
        tb.addWidget(self.export_btn)
        self.new_btn = QPushButton("New inventory …")
        self.new_btn.clicked.connect(self.new_inventory)
        tb.addWidget(self.new_btn)

        self.addToolBarBreak()
        tb2 = QToolBar("camera")
        tb2.setMovable(False)
        tb2.setStyleSheet(style)
        self.addToolBar(tb2)
        self.camera_box = QComboBox()
        self.camera_box.setMinimumWidth(200)
        tb2.addWidget(self.camera_box)
        self.cam_btn = QPushButton("Camera")
        self.cam_btn.clicked.connect(self.use_camera)
        tb2.addWidget(self.cam_btn)
        self.photos_btn = QPushButton("Photos …")
        self.photos_btn.clicked.connect(self.choose_photos)
        tb2.addWidget(self.photos_btn)
        tb2.addSeparator()
        self.read_btn = QPushButton("Read whole picture")
        self.read_btn.setToolTip("OCR on the whole frame, for a label that does not fit the box")
        self.read_btn.clicked.connect(self._force_ocr)
        tb2.addWidget(self.read_btn)

        # Landscape tablet held in both hands: the thumbs rest at the left and right edge, about
        # mid-height. Everything a thumb must hit lives in two edge columns, vertically centred;
        # the picture and the result panel sit in the middle; setup stays in the toolbars.
        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(6, 6, 6, 6)
        h = QHBoxLayout()
        h.setSpacing(10)
        self.view = VideoView()
        self.view.tapped.connect(self._tapped)
        self.view.pinched.connect(lambda f: self._set_zoom(self._zoom * f))
        self.card = ResultCard()
        self.card.lookup = self._lookup
        self.card.confirmed.connect(self._confirmed)
        self.card.discarded.connect(self._discarded)
        self.card.undone.connect(self._undone)
        self.card.backed.connect(self._backed)
        self.card.scan_requested.connect(self._trigger)
        self.card.print_requested.connect(self._print_last)
        self.card.state_changed.connect(self._card_state)
        self._printable: dict | None = None     # the last confirm without a code: number + description
        if not logo.isNull():
            self.card.set_logo(logo)
        self.history = HistoryStrip()
        self.history.delete_requested.connect(self._delete_scan)
        self.history.relocate_requested.connect(self._relocate_scan)
        self.history.undo_last_requested.connect(self._undo_last)
        self.history.print_requested.connect(self._print_scan)

        # left thumb column: zoom, SCAN / DISCARD, undo last scan
        left = QWidget()
        left.setFixedWidth(180)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(10)
        self.zoom_in = QPushButton("Zoom  +")
        self.zoom_out = QPushButton("Zoom  −")
        self.zoom_lbl = QLabel("1×")
        self.zoom_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_lbl.setStyleSheet("font-size:16px;color:#555;")
        for b, d in ((self.zoom_in, +0.25), (self.zoom_out, -0.25)):
            b.setMinimumHeight(56)
            b.setStyleSheet("font-size:18px;")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, d=d: self._zoom_step(d))
        lv.addWidget(self.zoom_in)
        lv.addWidget(self.zoom_lbl)
        lv.addWidget(self.zoom_out)
        lv.addStretch(1)
        lv.addWidget(self.card.print_btn)
        lv.addWidget(self.card.scan_btn_left)
        lv.addWidget(self.card.discard_btn)
        lv.addStretch(1)
        self.history.undo_btn.setMinimumHeight(64)
        lv.addWidget(self.history.undo_btn)

        # centre: picture over the result panel
        centre = QVBoxLayout()
        centre.setSpacing(8)
        centre.addWidget(self.view, 1)
        centre.addWidget(self.card)

        # right thumb column: SCAN / OK + Correct, the number pad
        right = QWidget()
        right.setFixedWidth(200)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(10)
        rv.addStretch(1)
        rv.addWidget(self.card.scan_btn)
        rv.addWidget(self.card.ok_btn)
        rv.addWidget(self.card.fix_btn)
        manual = QHBoxLayout()
        manual.setSpacing(6)
        manual.addWidget(self.card.manual, 1)
        manual.addWidget(self.card.manual_btn)
        rv.addLayout(manual)
        rv.addWidget(self.card.pad)
        rv.addStretch(1)

        h.addWidget(left)
        h.addLayout(centre, 1)
        h.addWidget(right)
        v.addLayout(h, 1)
        v.addWidget(self.history)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.engine_lbl = QLabel("starting engine …")
        self.timing_lbl = QLabel("")
        self.timing_lbl.setStyleSheet("color:#777;")
        self.stats_lbl = QLabel("")
        self.stats_lbl.setStyleSheet("color:#777;")
        self.count_lbl = QLabel("")
        self.statusBar().addWidget(self.engine_lbl, 1)
        self.statusBar().addPermanentWidget(self.timing_lbl)
        self.statusBar().addPermanentWidget(self.stats_lbl)
        self.statusBar().addPermanentWidget(self.count_lbl)
        self._refresh_history()
        self._set_zoom(float(self.settings.value("zoom", 10)) / 10.0, save=False)
        self._show_location()
        self._show_operator()

        force = QAction("scan now", self)
        force.setShortcut(QKeySequence(Qt.Key.Key_Space))
        force.triggered.connect(self._trigger)
        self.addAction(force)

    # ------------------------------------------------------------------ list / profile
    def _restore_list(self):
        folder = self.settings.value("lists_dir", "")
        if folder and Path(folder).is_dir() and Catalog.list_files(folder):
            self.load_lists([str(p) for p in Catalog.list_files(folder)], self.settings.value("sheet", None), folder)
            return
        path = self.settings.value("list_path", "")
        if path and Path(path).exists():
            self.load_list(path, self.settings.value("sheet", None))

    def choose_list(self):
        start = self.settings.value("list_path", "") or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Choose the list of known numbers", start,
                                              "Lists (*.xlsx *.xlsm *.csv *.txt);;All files (*)")
        if path:
            self.settings.setValue("lists_dir", "")
            self.load_list(path, None)

    def choose_lists_folder(self):
        start = self.settings.value("lists_dir", "") or self.settings.value("list_path", "") or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Folder with one list per Kostenstelle", start)
        if not folder:
            return
        files = Catalog.list_files(folder)
        if not files:
            QMessageBox.warning(self, "Lists", f"No list files (xlsx, csv) in\n{folder}")
            return
        self.load_lists([str(p) for p in files], None, folder)

    def load_lists(self, paths: list, sheet: str | None, folder: str = ""):
        """Several lists, the file name being the Kostenstelle (cost centre) of its items."""
        self.load_list(paths[0], sheet, paths=paths, folder=folder)

    def load_list(self, path: str, sheet: str | None, paths: list | None = None, folder: str = ""):
        prof = Profile.find_near(path)
        if prof:
            self.profile = prof
            self.worker.set_profile(prof)
            self._box_active = isinstance(self.source, CameraSource) and self.profile.aim_box
            if not self._box_active:
                self.view.set_box(None)
            self.card.scan_enabled = self._tap_mode()
            if self.card.state == "idle":
                self.card.set_idle()
        spec = self.profile.list
        if sheet:
            spec.sheet = sheet
        paths = list(paths or [path])
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
            self.catalog = Catalog.load_many(paths, spec) if len(paths) > 1 else Catalog.load(path, spec)
        except Exception as ex:
            log.exception("list load failed")
            QMessageBox.warning(self, "List", f"Could not read the list:\n{ex}")
            return
        self.worker.set_catalog(self.catalog)
        self.settings.setValue("list_path", path)
        self.settings.setValue("lists_dir", folder or "")
        self.settings.setValue("sheet", spec.sheet or "")
        ksts = self.catalog.kst_names()
        if len(paths) > 1:
            self.list_btn.setText(f"Lists: {len(paths)} Kostenstellen")
            what = f"{len(self.catalog)} items in {len(paths)} lists ({', '.join(ksts[:6])}{' …' if len(ksts) > 6 else ''})"
            if self.catalog.duplicates:
                what += f", {self.catalog.duplicates} numbers in more than one list"
        else:
            self.list_btn.setText(f"List: {Path(path).name}")
            what = f"{len(self.catalog)} items"
        self.engine_lbl.setText(f"{what} · profile {self.profile.name}"
                                + ("" if prof else " (no tagscan_profile.json next to the list: generic profile)"))
        log.info("lists loaded: %s", what)
        self._refresh_history()

    def _sheet_changed(self, name: str):
        path = self.settings.value("list_path", "")
        folder = self.settings.value("lists_dir", "")
        if name and folder and Path(folder).is_dir():
            self.load_lists([str(p) for p in Catalog.list_files(folder)], name, folder)
        elif name and path:
            self.load_list(path, name)

    def _lookup(self, number: str):
        it = self.catalog.get(number)
        return (it is not None, it.description if it else "", self.catalog.nearest(number, 4))

    # ------------------------------------------------------------------ location
    def _recent_locations(self) -> list[str]:
        v = self.settings.value("recent_locations", [])
        if isinstance(v, str):
            v = [v] if v else []
        return [str(x) for x in (v or []) if str(x).strip()]

    def _show_location(self):
        self.loc_btn.setText(f"Location: {self.location_text or '—  (tap to set)'}")

    def set_location(self, text: str, source: str = "") -> None:
        text = (text or "").strip()
        self.location_text = text
        self.settings.setValue("location", text)
        if text:
            recent = [text] + [r for r in self._recent_locations() if r != text]
            self.settings.setValue("recent_locations", recent[:12])
        self._show_location()
        if source == "code":
            self.statusBar().showMessage(f"location set by code: {text}", 4000)
            self.view.set_text(f"location: {text}")
            self.view.flash(BLUE, 600)
        log.info("location %r (%s)", text, source or "picked")

    def pick_location(self):
        val = LocationPicker.pick(self, self.location_text, self.catalog.locations(), self._recent_locations())
        if val is not None:
            self.set_location(val)

    # ------------------------------------------------------------------ operator
    def _recent_operators(self) -> list[str]:
        v = self.settings.value("recent_operators", [])
        if isinstance(v, str):
            v = [v] if v else []
        return [str(x) for x in (v or []) if str(x).strip()]

    def _show_operator(self):
        if self.operator_text:
            self.op_btn.setText(f"Operator: {self.operator_text}")
            self.op_btn.setStyleSheet("font-weight:600;text-align:left;")
        else:
            self.op_btn.setText("Operator: —  (tap: enter your code)")
            self.op_btn.setStyleSheet("font-weight:600;text-align:left;border:2px solid #f08c00;")
        self.card.idle_message = ("Point the camera at a label" if self.operator_text
                                  else "Set the operator first (button above), then point the camera at a label")
        if self.card.state == "idle":
            self.card.head.setText(self.card.idle_message)

    def set_operator(self, text: str, source: str = "") -> None:
        text = (text or "").strip()
        self.operator_text = text
        self.settings.setValue("operator", text)
        if text:
            recent = [text] + [r for r in self._recent_operators() if r != text]
            self.settings.setValue("recent_operators", recent[:12])
        self._show_operator()
        if source == "code":
            self.statusBar().showMessage(f"operator set by badge: {text}", 4000)
            self.view.set_text(f"operator: {text}")
            self.view.flash(BLUE, 600)
        log.info("operator %r (%s)", text, source or "picked")

    def pick_operator(self):
        val = OperatorPicker.pick(self, self.operator_text, list(self.profile.operators), self._recent_operators(),
                                  pattern=self.profile.operator_pattern)
        if val is not None:
            self.set_operator(val)

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
        self._box_active = isinstance(source, CameraSource) and self.profile.aim_box
        self._stab.reset()
        self._box_fail, self._need_change, self._armed = 0, False, False
        if not self._box_active:
            self.view.set_box(None)
        self.card.scan_enabled = self._tap_mode()
        self.card.set_idle()
        source.start()

    def _tap_mode(self) -> bool:
        return self._box_active and self.profile.scan_trigger != "auto"

    def use_camera(self):
        cam = CameraSource(self, max_width=self.profile.camera_max_width)
        devices = cam.devices()
        self.camera_box.blockSignals(True)
        self.camera_box.clear()
        self.camera_box.addItems([d.description() for d in devices])
        idx = self.settings.value("camera_index", None)
        idx = int(idx) if idx not in (None, "") else cam.preferred_index(devices)
        if 0 <= idx < len(devices):
            self.camera_box.setCurrentIndex(idx)
        self.camera_box.blockSignals(False)
        if getattr(self, "_camera_box_connected", False):
            self.camera_box.currentIndexChanged.disconnect(self._camera_changed)
        self.camera_box.currentIndexChanged.connect(self._camera_changed)
        self._camera_box_connected = True
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
        if self._box_active:
            color = {"accept": GREEN, "review": ORANGE, "input": ORANGE}.get(state, BLUE if self._armed else WHITE)
            self.view.set_box(self._box, color)

    def _source_status(self, text: str):
        self.view.set_text(text)

    # ------------------------------------------------------------------ zoom
    def _zoom_step(self, delta: float):
        self._set_zoom(round((self._zoom + delta) * 4) / 4)

    def _set_zoom(self, z: float, save: bool = True):
        z = max(ZOOM_MIN, min(ZOOM_MAX, float(z)))
        self._zoom = z
        self.zoom_lbl.setText(f"{z:.2f}×".replace(".00×", "×").replace("0×", "×") if z != 1.0 else "1×")
        if save:
            self.settings.setValue("zoom", int(round(z * 10)))

    # ------------------------------------------------------------------ frames
    def _on_frame(self, bgr: np.ndarray):
        self._frames += 1
        if self._box_active:
            self._last_frame = bgr
            self._show_preview(bgr)
            self._read_box(bgr)
            return
        # whole-frame mode (photos, or aim_box off): the zoom crops the centre of the frame
        if self._zoom > 1.01:
            h, w = bgr.shape[:2]
            cw, ch = int(w / self._zoom), int(h / self._zoom)
            x0, y0 = (w - cw) // 2, (h - ch) // 2
            bgr = np.ascontiguousarray(bgr[y0:y0 + ch, x0:x0 + cw])
        self._last_frame = bgr
        self._show_preview(bgr)
        if self.card.state == "idle":
            self.worker.submit(Job(bgr))

    def _read_box(self, bgr: np.ndarray) -> None:
        """Aim-box mode: codes from the box crop (and the whole frame now and then), OCR from
        the crop only while its content is steady, sharp and new. Full resolution, no zoom."""
        h, w = bgr.shape[:2]
        box = box_rect(w, h, self._zoom, self.profile.box_aspect, self.profile.box_width, self._box_center)
        if box != self._box:
            self._box = box
            self.view.set_box(box)
        crop, origin = crop_box(bgr, box)
        self._last_crop = crop
        st = self._stab.update(crop)
        if st.changed and self._need_change:
            self._need_change, self._box_fail = False, 0
        if self.card.state not in ("idle", "input"):
            return
        now = time.perf_counter()
        if self._tap_mode():
            # nothing is decoded until the operator asks; then the box (only the box) is read
            # for trigger_window seconds, ending in a result or the "type it" screen
            if not self._armed:
                return
            if now >= self._armed_until:
                self._armed = False
                if self.card.state == "idle":
                    self._ctx = self._context(Job(crop, origin, box), self._last_obs, reason="unreadable")
                    self._enter_input("unreadable", "", crop, self._suggestions(self._last_obs))
                return
            self.worker.submit_codes(Job(crop, origin, box))
            if self.card.state != "idle":
                return
            if st.content and st.sharp and now - self._last_box_ocr >= self.profile.ocr_interval:
                self._last_box_ocr = now
                self.worker.submit_text(Job(crop, origin, box), force=True)
            return
        # auto mode: codes from the crop at full resolution and (inside the worker) the whole frame
        # now and then; they keep running on the "type it" screen so a code can resolve a failed read
        self.worker.submit_codes(Job(crop, origin, box, full=bgr))
        if self.card.state != "idle":
            return
        if st.ready and not self._need_change and now - self._last_box_ocr >= self.profile.ocr_interval:
            self._last_box_ocr = now
            self._stab.mark_read()
            self.worker.submit_text(Job(crop, origin, box), force=True)
            self.view.set_box(box, BLUE)

    def _trigger(self):
        """SCAN / Space / a tap on the picture: read the box now (tap mode), or the whole frame
        when there is no box."""
        if not self._tap_mode():
            self._force_ocr()
            return
        if self.card.state == "input":
            self.card._skip()                       # the operator aims again: leave the type-it screen
        if self.card.state != "idle":
            return
        self._armed = True
        self._armed_until = time.perf_counter() + max(0.5, self.profile.trigger_window)
        self._last_box_ocr = 0.0
        self._last_obs = []
        self._box_fail = 0
        self._need_change = False
        self.view.set_box(self._box, BLUE)
        self.statusBar().showMessage("scanning the box …", int(self.profile.trigger_window * 1000))

    def _show_preview(self, bgr: np.ndarray) -> None:
        """Paint a view-sized copy: scaling 1920x1440 in QPainter on every frame is what kept
        the GUI thread busy; a 2-4 ms OpenCV resize here replaces it."""
        h, w = bgr.shape[:2]
        vw, vh = min(PREVIEW_MAX, max(320, self.view.width())), min(PREVIEW_MAX, max(240, self.view.height()))
        s = min(1.0, vw / w, vh / h)
        if s < 0.999:
            inter = cv2.INTER_AREA if s < 0.6 else cv2.INTER_LINEAR
            small = cv2.resize(bgr, (max(1, int(w * s)), max(1, int(h * s))), interpolation=inter)
        else:
            small = bgr
        self.view.set_frame(bgr_to_qimage(small), w, h)

    def _force_ocr(self):
        """Read the whole frame now (Space / the button): the fallback for a label that does not
        fit the box, and the only text path when the box is off."""
        if self._last_frame is not None:
            self.worker.submit(Job(self._last_frame), force=True)
            self.statusBar().showMessage("reading the whole picture …", 1500)

    def _tapped(self, fx: float, fy: float):
        """A tap on the picture moves the box there and scans; outside the picture (or without
        the box) it is the whole-frame read."""
        if self._box_active and fx >= 0:
            self._box_center = (fx, fy)
            self._stab.reset()
            self._box_fail, self._need_change, self._last_box_ocr = 0, False, 0.0
            if self.card.state == "input":
                self.card._skip()                   # the operator aims again instead
            if self._tap_mode():
                self._trigger()
            else:
                self.statusBar().showMessage("box moved", 1200)
        else:
            self._force_ocr()

    def _ocr_timing(self, seconds: float):
        self._ocr_times = (getattr(self, "_ocr_times", []) + [seconds])[-20:]
        avg = sum(self._ocr_times) / len(self._ocr_times)
        self.timing_lbl.setText(f"OCR {seconds:.2f}s (avg {avg:.2f}s)")

    def _engine_ready(self, msg: str):
        self.engine_lbl.setText(msg + (f" · {len(self.catalog)} items" if len(self.catalog) else " · no list loaded"))

    def _update_stats(self):
        fps, self._frames = self._frames, 0
        mem = rss_mb()
        parts = [f"{fps} fps"]
        if isinstance(self.source, CameraSource) and self.source.dropped:
            parts.append(f"{self.source.dropped} dropped")
        if mem >= 0:
            parts.append(f"{mem} MB")
        self.stats_lbl.setText(" · ".join(parts))
        self._stats_ticks += 1
        if self._stats_ticks % 60 == 0:
            log.info("stats: %s · OCR %s", self.stats_lbl.text(), self.timing_lbl.text() or "-")

    def _on_result(self, obs, dec, job: Job):
        ox, oy = job.origin
        quads = []
        for o in obs:
            color = GREEN if o.in_list else ORANGE
            if o.source in ("LOCATION", "OPERATOR"):
                color = BLUE
            elif o.source == "OCR" and dec.status in ("none", "seen"):
                color = BLUE
            quads += [([(x + ox, y + oy) for x, y in q], color) for q in o.boxes]
        if quads:
            self.view.set_quads(quads)
        if dec.status == "location":
            self.set_location(dec.observation.text, source="code")
            return
        if dec.status == "operator":
            self.set_operator(dec.observation.text, source="code")
            return
        if dec.status in ("seen", "accept", "review") and dec.observation is not None and dec.observation.number:
            sc = self.worker.scanner
            if sc is not None and sc.is_suppressed(dec.observation.number):
                return          # decoded before the confirm/skip that suppressed this number; stale
        if (self.card.state == "input" and dec.status in ("accept", "review") and dec.observation is not None
                and not self.card.manual.text().strip()):
            # a code, or a text read that was still in flight, resolves what the type-it screen
            # was opened for; the screen only stays if the operator has started typing
            self.card.set_idle()
            self._ctx = {}
        if self.card.state != "idle":
            return
        from_box = job.box is not None
        if from_box and any(o.source == "OCR" for o in obs):
            self._last_obs = obs
        if from_box and self.card.state == "idle" and not self._armed:
            self.view.set_box(self._box, WHITE)
        if dec.status == "seen":
            self._ctx = self._context(job, obs)
            self.card.set_seen(dec.observation.number, dec.votes)
            if self._armed:                 # a first vote: give the second one time to arrive
                self._armed_until = max(self._armed_until, time.perf_counter() + 1.5)
        elif dec.status in ("accept", "review"):
            self._box_fail = 0
            self._armed = False
            o = dec.observation
            it = self.catalog.get(o.number)
            desc = it.description if it else ""
            earlier = self.store.seen(o.number)
            self._ctx = self._context(job, obs)
            if dec.status == "accept" and not earlier:
                self.card.set_accept(o.number, desc, o.source, self.profile.auto_confirm_seconds)
                self.view.flash(GREEN)
            else:
                reason = dec.message
                if earlier:
                    e = earlier[-1]
                    reason = f"already scanned {e.ts[5:16]} at {e.location or '-'}"
                self.card.set_review(o.number, desc, o.source, o.in_list, reason,
                                     nearest=[] if o.in_list else self.catalog.nearest(o.number, 4),
                                     image=bgr_to_qimage(job.bgr) if from_box else None)
                self.view.flash(ORANGE)
            self.card.setFocus()
        elif (from_box and not self._tap_mode() and dec.message != "suppressed"
              and not any(o.source != "OCR" for o in obs)):
            # auto mode: an OCR pass on a steady, non-empty box that produced no usable number
            self._box_fail += 1
            if self._box_fail >= max(1, self.profile.ocr_fail_passes):
                self._ctx = self._context(job, obs, reason="unreadable")
                self._enter_input("unreadable", "", job.bgr, self._suggestions(obs))

    # ------------------------------------------------------------------ the "type it" screen
    def _context(self, job: Job | None, obs, reason: str = "") -> dict:
        """What the current card was read from: kept until the operator decides, then stored."""
        ocr = next((o for o in obs if o.source == "OCR"), None)
        code = next((o for o in obs if o.source in ("QR", "BARCODE")), None)
        o = ocr or code
        ox, oy = job.origin if job else (0, 0)
        from_box = job is not None and job.box is not None
        return dict(frame=self._last_frame if (from_box or job is None) else job.bgr,
                    crop=job.bgr if from_box else None,
                    roi=job.box.as_dict() if from_box else {},
                    ocr_number=o.number if o else "", ocr_text=o.text if o else "", ocr_level=o.level if o else "",
                    ocr_boxes=[[(x + ox, y + oy) for x, y in q] for q in o.boxes] if o else [],
                    source=o.source if o else "", reason=reason)

    def _suggestions(self, obs) -> list:
        """Buttons for the "type it" screen: list items among OCR's number-like tokens first,
        then the nearest list entries to what it read."""
        out, seen = [], set()
        ocr = next((o for o in obs if o.source == "OCR"), None)
        cands = ([ocr.number] if ocr and ocr.number else []) + (list(ocr.candidates) if ocr else [])
        for c in cands:
            it = self.catalog.get(c)
            if it and it.key not in seen:
                seen.add(it.key)
                out.append(it)
        for c in cands:
            for it in self.catalog.nearest(c, 4):
                if it.key not in seen:
                    seen.add(it.key)
                    out.append(it)
            if len(out) >= 4:
                break
        return out[:4]

    def _enter_input(self, reason: str, prior: str, image_bgr, nearest) -> None:
        self._box_fail = 0
        self._need_change = True
        self._stab.mark_read()
        self.card.set_input(bgr_to_qimage(image_bgr) if image_bgr is not None else None, prior, reason, nearest)
        self.card.setFocus()
        self.view.flash(ORANGE, 500)

    def _record(self, outcome: str, final_number: str = "", source: str = "", in_list: bool = False,
                photo: str = "", scan_id: int | None = None) -> None:
        ctx = self._ctx or {}
        try:
            self.store.add_read(outcome, final_number=final_number, source=source or ctx.get("source", ""),
                                ocr_number=ctx.get("ocr_number", ""), ocr_text=ctx.get("ocr_text", ""),
                                ocr_level=ctx.get("ocr_level", ""), ocr_boxes=ctx.get("ocr_boxes"),
                                roi=ctx.get("roi"), in_list=in_list, crop=ctx.get("crop"),
                                frame=None if photo else ctx.get("frame", self._last_frame), photo=photo,
                                location=self.location_text, scan_id=scan_id, operator=self.operator_text)
        except Exception:
            log.exception("training record failed")

    # ------------------------------------------------------------------ decisions
    def _recent_descriptions(self) -> list[str]:
        v = self.settings.value("recent_descriptions", [])
        if isinstance(v, str):
            v = [v] if v else []
        return [str(x) for x in (v or []) if str(x).strip()]

    def _ask_description(self, number: str, guess: str) -> str:
        """A number that is not in the list is a new item: ask what it is (Skip allowed)."""
        text = DescriptionPicker.ask(self, number, guess, self._recent_descriptions())
        if text:
            recent = [text] + [r for r in self._recent_descriptions() if r != text]
            self.settings.setValue("recent_descriptions", recent[:8])
        return text

    def _confirmed(self, number, source, in_list, description, note, outcome="confirmed"):
        ctx = self._ctx or {}
        frame = ctx.get("frame")
        if frame is None:
            frame = self._last_frame
        if not in_list and not description:
            sc = self.worker.scanner
            guess = sc.parser.describe(ctx.get("ocr_text", ""), number) if sc else ""
            description = self._ask_description(number, guess)
            note = (note + "; " if note else "") + "new item"
        scan = self.store.add(number, description, self.location_text, source, in_list, image=frame, note=note,
                              operator=self.operator_text, kst=self.catalog.kst_of(number))
        log.info("scan %s %s %s in_list=%s outcome=%s by %r at %r", scan.id, number, source, in_list, outcome,
                 self.operator_text, self.location_text)
        if not self.operator_text:
            self.statusBar().showMessage("stored without an operator: tap the Operator button", 6000)
        self._record(outcome, final_number=number, source=source, in_list=in_list, photo=scan.photo, scan_id=scan.id)
        self._ctx = {}
        self._need_change = True
        self._armed = False
        self._stab.mark_read()
        self._offer_label(scan)
        self.worker.suppress(number)
        self.worker.reset()
        self._refresh_history()
        self.view.flash(GREEN if in_list else ORANGE, 500)
        self._show_operator()

    # ------------------------------------------------------------------ QR labels
    def _offer_label(self, scan) -> None:
        """An item confirmed from printed text or typed by hand has no code: offer (or print) a
        QR label for it, so the next inventory scans it in a blink."""
        mode = self.profile.print_labels
        if mode == "off" or scan.source in ("QR", "BARCODE"):
            self._printable = None
            self.card.print_btn.hide()
            return
        self._printable = dict(id=scan.id, number=scan.number, description=scan.description)
        if mode == "auto":
            self._print_last()
        else:
            self.card.print_btn.setText(f"Print QR label  {scan.number}")
            self.card.print_btn.show()

    def _print_last(self) -> None:
        if self._printable:
            self._print(self._printable["number"], self._printable["description"], self._printable["id"])
        self.card.print_btn.hide()

    def _print_scan(self, sid: int) -> None:
        for s in self.store.recent(50):
            if s.id == sid:
                self._print(s.number, s.description, s.id)
                return

    def _print(self, number: str, description: str, scan_id: int | None = None) -> bool:
        try:
            label = render_label(self.profile, number, description)
        except Exception as ex:
            log.exception("label rendering failed")
            self.statusBar().showMessage(f"label failed: {ex}", 6000)
            return False
        info = printing.find_printer(self.profile.label_printer)
        if info is None:
            names = printing.printer_names()
            self.statusBar().showMessage(f"no printer named like '{self.profile.label_printer}' "
                                         f"(installed: {', '.join(names) or 'none'})", 8000)
            log.warning("label printer %r not found; installed: %s", self.profile.label_printer, names)
            return False
        ok = printing.print_label(label, info)
        self.statusBar().showMessage(f"label {number} sent to {info.printerName()}" if ok else "printing failed", 6000)
        if ok and scan_id is not None:
            row = self.store.db.execute("SELECT note FROM scans WHERE id=?", (scan_id,)).fetchone()
            note = (row[0] + "; " if row and row[0] else "") + "label printed"
            self.store.update(scan_id, note=note)
            self._refresh_history()
        return ok

    def _discarded(self, number):
        """Discard on a green or orange card: what was captured is wrong or unwanted."""
        self._record("discarded", final_number="", source=self._ctx.get("source", "") if self._ctx else "")
        self._ctx = {}
        self._need_change = True
        self._armed = False
        self._stab.mark_read()
        self.worker.suppress(number, 10.0)
        self.worker.reset()
        self.statusBar().showMessage("discarded", 2000)
        self._show_operator()

    def _undo_last(self):
        """Remove the most recent scan (the one that slipped through)."""
        last = self.store.recent(1)
        if not last:
            return
        s = last[0]
        self.store.delete(s.id)
        self._ctx = {}
        self.store.add_read("deleted", final_number=s.number, source=s.source, location=s.location,
                            operator=self.operator_text)
        self.worker.suppress(s.number, 10.0)
        self._refresh_history()
        self.statusBar().showMessage(f"removed scan {s.number}", 3000)
        log.info("scan %s %s removed by the operator", s.id, s.number)

    def _undone(self, number):
        """Undo on a green card: the read was wrong. Record it, then offer the pad with the
        box content so the right number is one entry away."""
        ctx = self._ctx or {}
        self._record("undone", final_number="", source=ctx.get("source", ""))
        self._armed = False
        self.worker.suppress(number, 10.0)
        self.worker.reset()
        image = ctx.get("crop")
        if image is None:
            image = ctx.get("frame")
        self._ctx = dict(ctx, reason="undo")
        self._enter_input("undo", number, image, self.catalog.nearest(number, 4) if number else [])

    def _backed(self, prior):
        self._record("back", final_number="", source=self._ctx.get("source", "") if self._ctx else "")
        self._ctx = {}
        self._need_change = True
        self._armed = False
        self._stab.mark_read()
        self.worker.reset()
        self._show_operator()

    def _refresh_history(self):
        self.history.set_scans(self.store.recent(10))
        n = self.store.count()
        reads = sum(self.store.read_counts().values())
        self.count_lbl.setText(f"{n} scans · {reads} reads kept for training · {self.data_dir}")

    def _delete_scan(self, sid: int):
        self.store.delete(sid)
        self._refresh_history()

    def _relocate_scan(self, sid: int):
        val = LocationPicker.pick(self, self.location_text, self.catalog.locations(), self._recent_locations())
        if val is not None:
            self.store.update(sid, location=val)
            self._refresh_history()

    def new_inventory(self):
        """Archive scans, photos, training data and exports under archive/<date_time>/ and start
        empty. List, operator and location stay."""
        n = self.store.count()
        dlg = QDialog(self)
        dlg.setWindowTitle("New inventory")
        dlg.setModal(True)
        dlg.resize(620, 360)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(12)
        msg = QLabel(f"Start a new inventory?\n\nThe current {n} scans, their photos, the training data and the "
                     f"exports are moved to\n{self.data_dir / 'archive'}\nand the app starts empty. "
                     f"List, operator and location are kept.")
        msg.setWordWrap(True)
        msg.setStyleSheet("font-size:15px;")
        lay.addWidget(msg)
        lay.addStretch(1)
        choice = {"v": None}

        def pick(v):
            choice["v"] = v
            dlg.accept()
        for text, val, style in (("Export Excel first, then start new", "export", "font-size:18px;font-weight:600;background:#2ea043;color:white;border-radius:10px;"),
                                 ("Start new without export", "new", "font-size:18px;font-weight:600;background:#f08c00;color:white;border-radius:10px;"),
                                 ("Cancel", None, "font-size:18px;border-radius:10px;")):
            b = QPushButton(text)
            b.setMinimumHeight(64)
            b.setStyleSheet(style)
            b.clicked.connect(lambda _=False, v=val: pick(v) if v else dlg.reject())
            lay.addWidget(b)
        if dlg.exec() != QDialog.DialogCode.Accepted or choice["v"] is None:
            return
        if choice["v"] == "export":
            try:
                out = self.store.export_all(self.data_dir / self.store.default_export_name(self.operator_text), self.catalog)
                log.info("exported %s before the new inventory", ", ".join(str(f) for f in out))
            except Exception as ex:
                log.exception("export before new inventory failed")
                QMessageBox.warning(self, "Export", f"Export failed, nothing archived:\n{ex}")
                return
        self.card.set_idle()
        self._ctx = {}
        dest = self.store.archive()
        self.worker.reset()
        self._refresh_history()
        self.statusBar().showMessage(f"new inventory started; the old one is in {dest}", 10000)
        log.info("new inventory: previous data archived to %s", dest)

    def export(self):
        default = str(self.data_dir / self.store.default_export_name(self.operator_text))
        path, _ = QFileDialog.getSaveFileName(self, "Export scans", default, "Excel (*.xlsx)")
        if not path:
            return
        try:
            files = self.store.export_all(path, self.catalog)
        except Exception as ex:
            log.exception("export failed")
            QMessageBox.warning(self, "Export", f"Export failed:\n{ex}")
            return
        more = f" + {len(files) - 1} per Kostenstelle" if len(files) > 1 else ""
        self.statusBar().showMessage(f"exported {files[0]}{more}", 8000)
        log.info("exported %s", ", ".join(str(f) for f in files))

    def closeEvent(self, ev):
        self._stats_timer.stop()
        if self.source is not None:
            self.source.stop()
        self.worker.stop()
        super().closeEvent(ev)

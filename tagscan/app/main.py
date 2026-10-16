"""Application entry point and the packaged self-test (``--smoke``)."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path


def _setup_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(data_dir / "tagscan.log", encoding="utf-8")]
                        + ([logging.StreamHandler(sys.stderr)] if sys.stderr else []))


def _data_dir() -> Path:
    from PySide6.QtCore import QStandardPaths
    env = os.environ.get("TAGSCAN_HOME")
    if env:
        return Path(env)
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)) / "TagScan"


def main(argv=None) -> int:
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv[:1])
    app.setApplicationName("TagScan")
    app.setOrganizationName("kayEi")
    data_dir = _data_dir()
    _setup_logging(data_dir)
    from .mainwindow import MainWindow
    win = MainWindow(data_dir=str(data_dir))
    win.show()
    return app.exec()


def synthetic_label(path: Path, number: str, brand: str = "ACME", keyword: str = "ASSET NO.") -> None:
    """Draw a black label with white text into a PNG (used by the smoke test and the unit tests)."""
    from PySide6.QtGui import QImage, QPainter, QColor, QFont, QGuiApplication
    from PySide6.QtCore import Qt, QRect
    if QGuiApplication.instance() is None:
        QGuiApplication(sys.argv[:1])
    img = QImage(900, 600, QImage.Format.Format_RGB888)
    img.fill(QColor(190, 190, 190))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(0, 0, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRect(150, 150, 600, 300), 20, 20)
    p.setPen(QColor(255, 255, 255))
    f = QFont("Arial")
    f.setBold(True)
    f.setPixelSize(56)
    p.setFont(f)
    p.drawText(QRect(150, 165, 600, 80), Qt.AlignmentFlag.AlignCenter, brand)
    p.drawText(QRect(150, 245, 600, 80), Qt.AlignmentFlag.AlignCenter, keyword)
    f.setPixelSize(72)
    p.setFont(f)
    p.drawText(QRect(150, 335, 600, 100), Qt.AlignmentFlag.AlignCenter, number)
    p.end()
    img.save(str(path))


def smoke() -> int:
    """Packaged self-test: synthetic label photo -> OCR -> accept -> stored -> exported."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer, QSettings
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv[:1])
    home = os.environ.get("TAGSCAN_HOME")
    tmp = Path(home) / "smoke" if home else Path(tempfile.mkdtemp(prefix="tagscan-smoke-"))
    if tmp.exists():
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    _setup_logging(tmp)
    log = logging.getLogger("tagscan.smoke")
    from ..catalog import Catalog, Item
    from ..profile import Profile
    from .camera import FolderSource
    from .mainwindow import MainWindow

    photos = tmp / "photos"
    photos.mkdir()
    synthetic_label(photos / "a.png", "123456")
    synthetic_label(photos / "b.png", "234567")
    cat = Catalog(items={"123456": Item("123456", "123456", "test item one"),
                         "234567": Item("234567", "234567", "test item two")})
    prof = Profile()
    prof.auto_confirm_seconds = 0.2
    prof.ocr_interval = 0.0
    settings = QSettings(str(tmp / "settings.ini"), QSettings.Format.IniFormat)
    win = MainWindow(profile=prof, catalog=cat, data_dir=str(tmp / "data"),
                     source=FolderSource(str(photos), interval_ms=400, repeat=4), settings=settings)
    win.show()
    rc = {"code": 1}

    def check():
        n = win.store.count()
        if n >= 2:
            out = win.store.export_xlsx(tmp / "out.xlsx", cat)
            log.info("SMOKE OK: %d scans, export %s (%d bytes)", n, out, out.stat().st_size)
            print("SMOKE OK")
            rc["code"] = 0
            app.quit()

    poll = QTimer()
    poll.timeout.connect(check)
    poll.start(300)
    QTimer.singleShot(90000, lambda: (log.error("SMOKE TIMEOUT: %d scans", win.store.count()), app.quit()))
    app.exec()
    win.close()
    return rc["code"]


if __name__ == "__main__":
    sys.exit(main())

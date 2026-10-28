"""Printing a label on a label printer through the system's printer driver (Brother P-touch
and alike): the driver knows the tape, cuts after the label and talks USB or Bluetooth.
Nothing printer-specific here, so it works with whatever printer Windows shows."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QMarginsF, QRectF, QSizeF
from PySide6.QtGui import QImage, QPageLayout, QPageSize, QPainter
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo

from ..labels import Label

log = logging.getLogger("tagscan.print")


def printer_names() -> list[str]:
    return [p.printerName() for p in QPrinterInfo.availablePrinters()]


def find_printer(name_part: str) -> QPrinterInfo | None:
    """The first printer whose name contains ``name_part`` (case-insensitive), else None."""
    want = (name_part or "").strip().lower()
    if not want:
        return None
    for p in QPrinterInfo.availablePrinters():
        if want in p.printerName().lower():
            return p
    return None


def _to_qimage(label: Label) -> QImage:
    rgb = label.image.convert("RGB")
    w, h = rgb.size
    data = rgb.tobytes()
    return QImage(data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def _setup(printer: QPrinter, label: Label) -> None:
    printer.setResolution(label.dpi)
    printer.setFullPage(True)
    # Label drivers (Brother P-touch among them) describe their paper the way the tape comes out of
    # the printer: as wide as the tape, as long as the label, printed in landscape. A width that is
    # not a tape width makes the driver fall back to whatever it printed last.
    size = QPageSize(QSizeF(label.tape_mm, label.width_mm), QPageSize.Unit.Millimeter, "label",
                     QPageSize.SizeMatchPolicy.ExactMatch)
    printer.setPageLayout(QPageLayout(size, QPageLayout.Orientation.Landscape, QMarginsF(0, 0, 0, 0),
                                      QPageLayout.Unit.Millimeter))
    printer.setCopyCount(1)


def _paint(printer: QPrinter, label: Label) -> bool:
    p = QPainter()
    if not p.begin(printer):
        return False
    try:
        page = printer.pageRect(QPrinter.Unit.DevicePixel)  # landscape: the label length by the tape
        img = _to_qimage(label)
        scale = page.width() / img.width()
        band = img.height() * scale  # the printable band, centred across the tape
        p.drawImage(QRectF(page.left(), page.top() + (page.height() - band) / 2, page.width(), band), img)
    finally:
        p.end()
    return True


def print_label(label: Label, printer_info: QPrinterInfo) -> bool:
    printer = QPrinter(printer_info, QPrinter.PrinterMode.HighResolution)
    _setup(printer, label)
    ok = _paint(printer, label)
    log.info("label %s on %r: %s", label.payload.replace("\n", " / "), printer_info.printerName(), "sent" if ok else "FAILED")
    return ok


def print_label_to_pdf(label: Label, path: str | Path) -> bool:
    """The same page, into a PDF: for testing the rendering and the page geometry without a printer."""
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))
    _setup(printer, label)
    return _paint(printer, label)

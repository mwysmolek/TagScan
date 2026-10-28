"""QR labels: rendered for the tape, readable by TagScan's own decoder, printable to a PDF."""
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tagscan.labels import qr_payload, render_label
from tagscan.profile import Profile
from tagscan.engine.parse import TextParser


def test_payload_follows_template():
    p = Profile()
    assert qr_payload(p, "123456", "Rolling cart") == "ACME\nASSET NO.\n123456\nRolling cart"
    assert qr_payload(p, "123456") == "ACME\nASSET NO.\n123456"
    p.label_brand_line, p.label_keyword_line = "Some Brand", "Inventar-Nr."
    p.code_number_regex = r"Inventar-?\s*Nr\.?\s*:?\s*\n?\s*(\d{5,7})"
    assert TextParser(p).parse_code(qr_payload(p, "6000031", "x")) == "6000031"     # the app reads its own label


@pytest.mark.parametrize("tape", [12.0, 24.0, 36.0])
def test_label_renders_and_decodes(tape):
    import zxingcpp
    p = Profile()
    label = render_label(p, "123456", "Rolling cart with three drawers and a very long name", tape_mm=tape)
    img = label.image
    assert img.mode == "1" and label.height_mm < tape == label.tape_mm and label.width_mm == p.label_length_mm
    assert img.size[1] == round(label.height_mm / 25.4 * p.label_dpi)
    arr = np.array(img.convert("L"))
    hits = zxingcpp.read_barcodes(arr)
    assert len(hits) == 1 and hits[0].text == label.payload and hits[0].format.name == "QRCode"
    assert TextParser(p).parse_code(hits[0].text) == "123456"
    assert arr[:, : arr.shape[0]].min() == 0 and arr[:, -arr.shape[0] // 4:].mean() > 200    # QR left, text right


def test_print_to_pdf_and_printer_lookup(tmp_path):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from tagscan.app import printing
    label = render_label(Profile(), "123456", "Rolling cart")
    out = tmp_path / "label.pdf"
    assert printing.print_label_to_pdf(label, out) and out.exists() and out.stat().st_size > 500
    assert printing.find_printer("") is None
    assert printing.find_printer("no such printer xyz") is None
    assert isinstance(printing.printer_names(), list)

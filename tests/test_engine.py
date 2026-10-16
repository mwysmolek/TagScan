import os
from pathlib import Path

import pytest

from tagscan.catalog import Catalog, normalize_number
from tagscan.engine.parse import TextParser
from tagscan.engine.textocr import _unrotate
from tagscan.profile import Profile, ListSpec


def test_normalize():
    assert normalize_number("28.462") == "28462"
    assert normalize_number(" 0104706 ") == "104706"
    assert normalize_number(104706.0) == "104706"
    assert normalize_number(None) == ""


def test_parser_keyword_and_brand():
    p = TextParser(Profile())
    r = p.parse("ACME\nASSET NO.\n123456")
    assert r.number == "123456" and r.level == "high" and r.brand and r.keyword
    r = p.parse("ASSET NO. 28.462\nMODEL X9")
    assert r.number == "28462" and r.level == "high"
    r = p.parse("random\n4711")
    assert r.number == "4711" and r.level == "low"
    assert p.parse("").number == ""


def test_parser_code_payload():
    p = TextParser(Profile())
    assert p.parse_code("Some Brand\nInventar-Nr.\n28019\nServicewagen") == "28019"
    assert p.parse_code("122145") == "122145"
    assert p.parse_code("12") == ""


def test_catalog_csv(tmp_path):
    f = tmp_path / "list.txt"
    f.write_text("Nr;Description\n28019;Servicewagen\n100016;Monitor\n", encoding="utf-8")
    cat = Catalog.load(f, ListSpec())
    assert len(cat) == 2 and "28.019" in cat and cat.get("100016").description == "Monitor"
    near = cat.nearest("100018", 1)
    assert near[0].key == "100016"


def test_unrotate_roundtrip():
    import numpy as np
    w0, h0 = 7, 4
    img = np.arange(w0 * h0).reshape(h0, w0)
    for k in range(4):
        rot = np.rot90(img, k)
        for y in range(rot.shape[0]):
            for x in range(rot.shape[1]):
                ox, oy = _unrotate(x, y, k, w0, h0)
                assert img[int(oy), int(ox)] == rot[y, x]


@pytest.mark.skipif(os.environ.get("TAGSCAN_NO_OCR") == "1", reason="OCR disabled")
def test_ocr_synthetic(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import cv2
    from tagscan.app.main import synthetic_label
    from tagscan.engine import Scanner
    from tagscan.catalog import Item
    synthetic_label(tmp_path / "a.png", "123456")
    cat = Catalog(items={"123456": Item("123456", "123456", "thing")})
    sc = Scanner(Profile(), cat)
    img = cv2.imread(str(tmp_path / "a.png"))
    for rot in (0, 1, 2, 3):
        frame = __import__("numpy").ascontiguousarray(__import__("numpy").rot90(img, rot))
        sc.reset()
        obs, dec, _ = sc.process(frame, force_ocr=True)
        assert obs and obs[0].number == "123456", f"rotation {rot}"

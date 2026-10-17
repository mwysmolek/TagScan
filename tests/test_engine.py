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


def test_location_code_regex():
    p = TextParser(Profile())
    assert p.parse_location("LOC: Hall 3") == "Hall 3"
    assert p.parse_location("ort=Labor 6\n") == "Labor 6"
    assert p.parse_location("STANDORT:\n204") == "204"
    assert p.parse_location("Inventar-Nr.\n28019") == ""
    assert p.parse_location("") == ""
    assert p.parse_code("LOC: 6200") == "6200"       # parse_code alone would read it as a number ...


def test_location_code_wins_in_pipeline():
    """... which is why the scanner checks for a location payload before the number."""
    import cv2
    import numpy as np
    import zxingcpp
    from tagscan.engine import Scanner
    from tagscan.engine.codes import formats_from_names
    from tagscan.catalog import Item

    def qr(text):
        if hasattr(zxingcpp, "create_barcode"):                 # zxing-cpp >= 3
            img = np.array(zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode), scale=8))
        else:
            img = np.array(zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, text, width=360, height=360))
        img = cv2.copyMakeBorder(img, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

    cat = Catalog(items={"6200": Item("6200", "6200", "the item")})
    sc = Scanner(Profile(), cat)
    assert formats_from_names(Profile().code_formats) is not None

    obs, dec = sc.process_codes(qr("LOC: 6200"))
    assert dec.status == "location" and dec.observation.text == "6200" and obs[0].source == "LOCATION"
    obs, dec = sc.process_codes(qr("LOC: 6200"))           # the same code again is quiet for a while
    assert dec.status == "none" and obs and obs[0].source == "LOCATION"

    obs, dec = sc.process_codes(qr("6200"))                 # a plain number is still an item
    assert dec.status == "accept" and dec.observation.number == "6200" and dec.observation.in_list
    obs, dec = sc.process_codes(qr("Some Brand\nInventar-Nr.\n4711\nthing"))
    assert dec.status == "review" and dec.observation.number == "4711"


def test_catalog_locations(tmp_path):
    f = tmp_path / "list.csv"
    f.write_text("Nr;Description;Ort\n28019;Servicewagen;Halle 10\n100016;Monitor;Halle 2\n100017;Maus;Halle 2\n100018;x;\n",
                 encoding="utf-8")
    cat = Catalog.load(f, ListSpec(description_columns=[2], location_column=3))
    assert cat.get("28019").location == "Halle 10"
    assert cat.locations() == ["Halle 2", "Halle 10"]          # natural order, distinct, blanks dropped
    assert Catalog.load(f, ListSpec()).locations() == []


def test_profile_roundtrip_and_old_profiles(tmp_path):
    p = Profile()
    p.save(tmp_path / "p.json")
    q = Profile.load(tmp_path / "p.json")
    assert q.ocr_rotations == [0, 90] and q.code_formats == p.code_formats and q.list.location_column == 0
    # a 0.1 profile without the new keys still loads and gets the defaults
    old = {"name": "old", "list": {"sheet": "S", "header_row": 3}}
    r = Profile.from_dict(old)
    assert r.name == "old" and r.list.sheet == "S" and r.code_interval == 0.12 and r.camera_max_width == 1920


def test_scanner_apply_profile():
    from tagscan.engine import Scanner
    sc = Scanner(Profile())
    p2 = Profile(name="two", ocr_rotations=[0, 90, 180, 270], ocr_max_side=640, code_formats=[])
    p2.keyword_patterns = ["SERIAL"]
    sc.apply_profile(p2)
    assert sc.ocr.rotations == (0, 90, 180, 270) and sc.ocr.max_side == 640 and sc.formats is None
    assert sc.parser.parse("SERIAL 123456").level == "high" and sc.ocr.parser is sc.parser

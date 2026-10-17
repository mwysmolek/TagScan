"""QR and barcode decoding with zxing-cpp."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

try:
    import zxingcpp
except ImportError:  # pragma: no cover
    zxingcpp = None


@dataclass
class CodeHit:
    format: str
    text: str
    number: str
    quad: list = field(default_factory=list)     # 4 (x, y) points in input coordinates


def formats_from_names(names) -> "object | None":
    """zxing-cpp format set from names like ["QRCode", "Code128"]; None = all formats."""
    if zxingcpp is None or not names:
        return None
    try:
        return zxingcpp.barcode_formats_from_str("|".join(names))
    except Exception:
        return None


def decode_codes(gray: np.ndarray, parser, max_side: int = 1280, formats=None) -> list[CodeHit]:
    if zxingcpp is None:
        return []
    h, w = gray.shape[:2]
    s = min(1.0, max_side / max(h, w))
    img = gray if s >= 1.0 else _resize(gray, s)
    hits = []
    kw = {"formats": formats} if formats is not None else {}
    for r in zxingcpp.read_barcodes(img, **kw):
        if not r.valid or not r.text:
            continue
        p = r.position
        quad = [(pt.x / s, pt.y / s) for pt in (p.top_left, p.top_right, p.bottom_right, p.bottom_left)]
        kind = "QR" if "QR" in r.format.name.upper() else "BARCODE"
        hits.append(CodeHit(format=kind, text=r.text, number=parser.parse_code(r.text), quad=quad))
    return hits


def _resize(img, s):
    import cv2
    return cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)

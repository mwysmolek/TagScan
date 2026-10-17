"""Printed-text reading with RapidOCR, trying the profile's orientations and keeping the best."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import numpy as np

from .parse import TextParser, Parsed


@dataclass
class TextHit:
    number: str = ""
    level: str = "none"
    text: str = ""
    conf: float = 0.0
    rotation: int = 0                      # degrees the frame was turned for the winning read
    boxes: list = field(default_factory=list)   # list of 4-point quads in input coordinates
    elapsed: float = 0.0
    passes: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.number)


def default_threads() -> int:
    """ONNX intra-op threads: half the logical CPUs, 1..4. Leaves room for the camera, the code
    decoder and the GUI on a fanless tablet; all cores would just throttle."""
    return max(1, min(4, (os.cpu_count() or 2) // 2))


class TextOCR:
    def __init__(self, parser: TextParser, max_side: int = 960, rotations=(0, 90), threads: int = 0):
        self.parser = parser
        self.max_side = max_side
        self.rotations = tuple(rotations)       # degrees
        self.threads = threads
        self._ocr = None

    def _engine(self):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            n = self.threads if self.threads > 0 else default_threads()
            try:
                self._ocr = RapidOCR(intra_op_num_threads=n, inter_op_num_threads=1)
            except Exception:           # an older/newer rapidocr without these kwargs
                self._ocr = RapidOCR()
        return self._ocr

    def warmup(self) -> None:
        self.read(np.full((64, 256, 3), 255, np.uint8), stop_in_list=None)

    def read(self, bgr: np.ndarray, stop_in_list=None, rotations=None) -> TextHit:
        """OCR the frame. Tries each rotation (degrees, default from the profile), stops early at
        a confident read (``stop_in_list`` is a callable number -> bool, e.g. the catalog's
        __contains__). RapidOCR's angle classifier flips upside-down lines itself, so 0 and 90
        already cover labels in all four orientations."""
        t0 = time.time()
        h, w = bgr.shape[:2]
        s = min(1.0, self.max_side / max(h, w))
        small = bgr if s >= 1.0 else _resize(bgr, s)
        engine = self._engine()
        best = TextHit()
        best_score = (-1,)
        passes = 0
        for deg in (self.rotations if rotations is None else rotations):
            k = (int(deg) // 90) % 4
            img = np.ascontiguousarray(np.rot90(small, k)) if k else small
            res, _ = engine(img)
            passes += 1
            if not res:
                if k == 0:
                    break           # the detector saw no text at all: other orientations won't help
                continue
            lines = [r[1] for r in res]
            confs = [float(r[2]) for r in res]
            text = "\n".join(lines)
            parsed: Parsed = self.parser.parse(text)
            in_list = bool(stop_in_list and parsed.number and stop_in_list(parsed.number))
            score = (int(in_list), int(parsed.level == "high"), int(bool(parsed.number)),
                     float(np.mean(confs)))
            if score > best_score:
                best_score = score
                rh, rw = img.shape[:2]
                boxes = [[_unrotate(x, y, k, small.shape[1], small.shape[0]) for x, y in r[0]] for r in res]
                best = TextHit(number=parsed.number, level=parsed.level, text=text,
                               conf=float(np.mean(confs)), rotation=k * 90,
                               boxes=[[(x / s, y / s) for x, y in q] for q in boxes])
            if in_list and parsed.level == "high":
                break
        best.elapsed = time.time() - t0
        best.passes = passes
        return best


def _resize(img, s):
    import cv2
    return cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)


def _unrotate(x: float, y: float, k: int, w0: int, h0: int):
    """Map a point in np.rot90(img, k) back into img (w0 x h0)."""
    for j in range(k, 0, -1):
        wj = w0 if (j - 1) % 2 == 0 else h0      # width of the image before rotation j
        x, y = wj - 1 - y, x
    return (x, y)

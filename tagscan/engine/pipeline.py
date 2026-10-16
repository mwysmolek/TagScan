"""Per-frame scheduling and the accept/review decision."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from ..catalog import Catalog
from ..profile import Profile
from .codes import decode_codes
from .parse import TextParser
from .textocr import TextOCR


@dataclass
class Observation:
    source: str                 # "QR", "BARCODE", "OCR"
    number: str
    text: str = ""
    level: str = "high"
    in_list: bool = False
    boxes: list = field(default_factory=list)
    elapsed: float = 0.0
    rotation: int = 0


@dataclass
class Decision:
    status: str = "none"        # none | seen | accept | review
    observation: Observation | None = None
    votes: int = 0
    message: str = ""


class Scanner:
    """Feed frames in, get observations and a decision out. Not thread-safe: one per worker."""

    def __init__(self, profile: Profile, catalog: Catalog | None = None):
        self.profile = profile
        self.catalog = catalog or Catalog()
        self.parser = TextParser(profile)
        self.ocr = TextOCR(self.parser, max_side=profile.ocr_max_side)
        self._last_ocr = 0.0
        self._streak_number = ""
        self._streak = 0
        self._suppressed: dict[str, float] = {}
        self.last_text_boxes: list = []
        self.stats = {"frames": 0, "ocr_passes": 0, "ocr_time": 0.0, "ocr_last": 0.0, "codes": 0}
        self._lock = threading.Lock()

    def warmup(self) -> None:
        self.ocr.warmup()

    def suppress(self, number: str, seconds: float = 8.0) -> None:
        """Ignore this number for a while (the item just confirmed is still in view)."""
        self._suppressed[number] = time.time() + seconds
        with self._lock:
            self._streak_number, self._streak = "", 0

    def reset(self) -> None:
        with self._lock:
            self._streak_number, self._streak = "", 0

    def _is_suppressed(self, number: str) -> bool:
        exp = self._suppressed.get(number)
        if exp is None:
            return False
        if exp < time.time():
            del self._suppressed[number]
            return False
        return True

    def process_codes(self, bgr: np.ndarray):
        """Codes only (fast, every frame). Returns (observations, decision)."""
        import cv2
        self.stats["frames"] += 1
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        obs: list[Observation] = []
        for hit in decode_codes(gray, self.parser):
            self.stats["codes"] += 1
            if not hit.number:
                continue
            obs.append(Observation(source=hit.format, number=hit.number, text=hit.text,
                                   in_list=hit.number in self.catalog, boxes=[hit.quad]))
        live = [o for o in obs if not self._is_suppressed(o.number)]
        if live:
            o = live[0]
            with self._lock:
                self._streak_number, self._streak = "", 0
            status = "accept" if o.in_list else "review"
            return obs, Decision(status=status, observation=o, votes=1,
                                 message="code" if o.in_list else "code, not in list")
        return obs, Decision(status="none")

    def process_text(self, bgr: np.ndarray, force: bool = False, now: float | None = None):
        """Printed text (slow, throttled by ocr_interval). Returns (observations, decision, ran)."""
        now = now or time.time()
        if not force and (now - self._last_ocr) < self.profile.ocr_interval:
            return [], Decision(status="none"), False
        self._last_ocr = now
        hit = self.ocr.read(bgr, stop_in_list=self.catalog.__contains__)
        self.stats["ocr_passes"] += hit.passes
        self.stats["ocr_time"] += hit.elapsed
        self.stats["ocr_last"] = hit.elapsed
        self.last_text_boxes = hit.boxes
        if not hit.ok:
            return [], Decision(status="none"), True
        o = Observation(source="OCR", number=hit.number, text=hit.text, level=hit.level,
                        in_list=hit.number in self.catalog, boxes=hit.boxes,
                        elapsed=hit.elapsed, rotation=hit.rotation)
        if self._is_suppressed(o.number):
            return [o], Decision(status="none", message="suppressed"), True
        with self._lock:
            if o.number == self._streak_number:
                self._streak += 1
            else:
                self._streak_number, self._streak = o.number, 1
            streak = self._streak
        need = self.profile.votes_needed
        if o.in_list and o.level == "high" and streak >= need:
            return [o], Decision("accept", o, streak, "text, in list"), True
        if streak >= need:
            why = "not in list" if not o.in_list else "weak context"
            return [o], Decision("review", o, streak, f"text, {why}"), True
        return [o], Decision("seen", o, streak, "text, confirming"), True

    def process(self, bgr: np.ndarray, force_ocr: bool = False, now: float | None = None):
        """Both stages in one call (bench, tests). Returns (observations, decision, ocr_ran)."""
        obs, dec = self.process_codes(bgr)
        if dec.status != "none":
            return obs, dec, False
        tobs, tdec, ran = self.process_text(bgr, force=force_ocr, now=now)
        return obs + tobs, tdec, ran

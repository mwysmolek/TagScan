"""The aim box: where in the frame the operator holds the label, the full-resolution crop of
it, and whether what is inside has settled enough to be worth an OCR pass. Pure numpy/OpenCV,
no Qt, so it is testable on its own."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    def quad(self) -> list:
        return [(self.x, self.y), (self.x + self.w, self.y), (self.x + self.w, self.y + self.h), (self.x, self.y + self.h)]

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


def box_rect(frame_w: int, frame_h: int, zoom: float = 1.0, aspect: float = 2.3, width_frac: float = 0.6,
             center: tuple[float, float] = (0.5, 0.5)) -> Box:
    """The box in frame pixels: ``width_frac`` of the frame width at zoom 1, shrinking with the
    zoom, label-shaped (``aspect`` = width / height), centred at ``center`` (fractions), kept
    inside the frame."""
    w = int(round(frame_w * width_frac / max(1.0, float(zoom))))
    w = max(64, min(w, frame_w))
    h = int(round(w / max(0.2, float(aspect))))
    h = max(32, min(h, frame_h))
    cx, cy = center
    x = int(round(cx * frame_w - w / 2))
    y = int(round(cy * frame_h - h / 2))
    x = max(0, min(x, frame_w - w))
    y = max(0, min(y, frame_h - h))
    return Box(x, y, w, h)


def crop_box(bgr: np.ndarray, box: Box, margin: float = 0.1) -> tuple[np.ndarray, tuple[int, int]]:
    """The box plus a margin, cut from the full-resolution frame. Returns (crop, (x0, y0))
    so results can be mapped back into frame coordinates."""
    h, w = bgr.shape[:2]
    mx, my = int(box.w * margin), int(box.h * margin)
    x0, y0 = max(0, box.x - mx), max(0, box.y - my)
    x1, y1 = min(w, box.x + box.w + mx), min(h, box.y + box.h + my)
    return np.ascontiguousarray(bgr[y0:y1, x0:x1]), (x0, y0)


@dataclass
class Status:
    still: bool = False         # the content has not moved for ``still_frames`` frames
    content: bool = False       # there is something in the box (contrast), not a blank wall
    sharp: bool = False         # not motion-blurred
    changed: bool = True        # different from what was in the box at the last read

    @property
    def ready(self) -> bool:
        return self.still and self.content and self.sharp


class Stability:
    """Cheap per-frame judgement of the box content (a 48x27 thumbnail and a small Laplacian):
    a few hundred microseconds, so it can run on every frame."""

    def __init__(self, still_frames: int = 2, max_diff: float = 6.0, min_contrast: float = 12.0,
                 min_sharpness: float = 30.0):
        self.still_frames = still_frames
        self.max_diff = max_diff
        self.min_contrast = min_contrast
        self.min_sharpness = min_sharpness
        self._prev = None
        self._at_read = None
        self._still = 0
        self.thumb = None

    @staticmethod
    def _thumb(bgr: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
        return cv2.resize(gray, (48, 27), interpolation=cv2.INTER_AREA).astype(np.float32)

    def update(self, bgr: np.ndarray) -> Status:
        if bgr.size == 0:
            return Status()
        thumb = self._thumb(bgr)
        st = Status()
        if self._prev is not None and self._prev.shape == thumb.shape:
            diff = float(np.mean(np.abs(thumb - self._prev)))
            self._still = self._still + 1 if diff < self.max_diff else 0
        else:
            self._still = 0
        self._prev = thumb
        self.thumb = thumb
        st.still = self._still >= self.still_frames
        st.content = float(thumb.std()) > self.min_contrast
        if st.content:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
            s = min(1.0, 240.0 / max(1, gray.shape[1]))
            small = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1.0 else gray
            st.sharp = float(cv2.Laplacian(small, cv2.CV_32F).var()) > self.min_sharpness
        st.changed = (self._at_read is None or self._at_read.shape != thumb.shape
                      or float(np.mean(np.abs(thumb - self._at_read))) > self.max_diff)
        return st

    def mark_read(self) -> None:
        """Remember what the box looked like at the last read; ``changed`` is measured against it."""
        self._at_read = self.thumb

    def reset(self) -> None:
        self._prev = None
        self._at_read = None
        self._still = 0

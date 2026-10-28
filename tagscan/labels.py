"""QR labels for items that have none: rendered as a 1-bit image sized for a tape, carrying the
same payload TagScan reads back (profile ``label_qr_template``). No Qt here; printing is in
``tagscan.app.printing``."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .profile import Profile


@dataclass
class Label:
    image: "object"               # PIL.Image, mode "1", black on white
    width_mm: float               # along the tape
    height_mm: float              # the printable band: the tape minus the margins
    tape_mm: float
    payload: str
    dpi: int


def qr_payload(profile: Profile, number: str, description: str = "") -> str:
    return profile.label_qr_template.format(brand=profile.label_brand_line, keyword=profile.label_keyword_line,
                                            number=number, description=(description or "").strip()).strip()


def _qr_bitmap(text: str) -> np.ndarray:
    """The QR as a 0/255 uint8 array, one pixel per module (no quiet zone)."""
    import zxingcpp
    if hasattr(zxingcpp, "create_barcode"):
        bc = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode, ec_level="M")
        return np.array(zxingcpp.write_barcode_to_image(bc, scale=1, add_quiet_zones=False))
    return np.array(zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, text, quiet_zone=0))


def render_label(profile: Profile, number: str, description: str = "", tape_mm: float | None = None,
                 dpi: int | None = None, length_mm: float | None = None) -> Label:
    """A label along the tape: the QR at the left, brand / keyword / number / description at the
    right. The printable height is the tape minus the margins the printers cannot print on."""
    from PIL import Image, ImageDraw, ImageFont
    tape_mm = tape_mm or profile.label_tape_mm
    dpi = dpi or profile.label_dpi
    px = dpi / 25.4
    margin_mm = 2.0 if tape_mm >= 18 else 1.0
    h = int(round((tape_mm - 2 * margin_mm) * px))
    length_mm = length_mm or profile.label_length_mm
    w = int(round(length_mm * px))
    img = Image.new("1", (w, h), 1)
    d = ImageDraw.Draw(img)
    payload = qr_payload(profile, number, description)
    qr = _qr_bitmap(payload)
    quiet = 2
    modules = qr.shape[0] + 2 * quiet
    scale = max(1, (h - 2) // modules)
    qr_px = modules * scale
    qr_img = Image.fromarray(np.pad(qr, quiet, constant_values=255)).resize((qr_px, qr_px), Image.NEAREST).convert("1")
    y0 = (h - qr_px) // 2
    img.paste(qr_img, (0, y0))
    x = qr_px + int(2 * px)
    avail = w - x - int(1 * px)

    def font(size):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    def fit(text, size, bold_min=8):
        while size > bold_min:
            f = font(size)
            if d.textlength(text, font=f) <= avail:
                return f
            size -= 1
        return font(bold_min)
    lines = [(profile.label_brand_line, 0.18, 1), (profile.label_keyword_line, 0.16, 1), (number, 0.40, 1)]
    desc = (description or "").strip().replace("\n", " ")
    if desc:
        lines.append((desc, 0.16, 1))
    total = sum(h * frac for _, frac, _ in lines)
    y = (h - total) / 2
    for text, frac, _ in lines:
        size = max(8, int(h * frac * 0.85))
        f = fit(text, size)
        if text is desc and d.textlength(text, font=f) > avail:
            while text and d.textlength(text + "…", font=f) > avail:
                text = text[:-1]
            text += "…"
        d.text((x, y), text, fill=0, font=f)
        y += h * frac
    return Label(image=img, width_mm=length_mm, height_mm=tape_mm - 2 * margin_mm, tape_mm=tape_mm, payload=payload,
                 dpi=dpi)

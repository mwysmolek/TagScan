"""A profile describes one family of labels and the list they are matched against.

The repository ships a generic example; site-specific words (brands, keywords, sheet names)
live in a private ``tagscan_profile.json`` next to the list file and are never committed here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

PROFILE_FILENAME = "tagscan_profile.json"


@dataclass
class ListSpec:
    sheet: str | None = None          # xlsx sheet name (None = first sheet)
    header_row: int = 1               # 1-based row holding the column titles
    number_column: int = 1            # 1-based column with the inventory number
    description_columns: list[int] = field(default_factory=lambda: [2])
    location_column: int = 0          # 1-based column with the item's location (0 = none); feeds the location picker
    delimiter: str | None = None      # csv/txt delimiter (None = sniff)


@dataclass
class Profile:
    name: str = "example"
    # regexes (case-insensitive) that identify the label family: a brand line ...
    brand_patterns: list[str] = field(default_factory=lambda: [r"ACME"])
    # ... and the keyword in front of the number, including OCR-mangled variants
    keyword_patterns: list[str] = field(default_factory=lambda: [r"INVENT", r"ASSET", r"INV\.?\s*NO"])
    number_digits: list[int] = field(default_factory=lambda: [4, 8])   # min, max digits
    allow_dotted_numbers: bool = True                                  # "28.462" -> 28462
    # applied to the text of a QR/barcode; group 1 is the number. Fallback: the number parser.
    code_number_regex: str = r"(?:Nr|No|Number)\.?\s*:?\s*\n?\s*(\d{4,8})"
    list: ListSpec = field(default_factory=ListSpec)
    auto_confirm_seconds: float = 3.0
    votes_needed: int = 2             # identical OCR reads before a text hit is accepted
    ocr_interval: float = 0.5         # seconds between OCR passes while scanning
    ocr_max_side: int = 960           # frames are downscaled to this before OCR
    # orientations tried per OCR pass (degrees). RapidOCR's line classifier handles upside-down
    # text, so 0 and 90 cover all four label orientations at half the cost of trying all four.
    ocr_rotations: list[int] = field(default_factory=lambda: [0, 90])
    ocr_threads: int = 0              # ONNX threads for OCR; 0 = half the logical CPUs (max 4)
    code_interval: float = 0.12       # seconds between QR/barcode decodes
    # zxing-cpp format names to look for; [] = every format (slower). Code 128 and QR are the usual ones.
    code_formats: list[str] = field(default_factory=lambda: ["QRCode", "DataMatrix", "Code128", "Code39", "EAN13", "EAN8", "ITF"])
    # a QR/barcode whose text matches this sets the scanning location instead of an item; group 1 = location
    location_code_regex: str = r"^\s*(?:LOC|ORT|LOCATION|STANDORT|RAUM|ROOM)\s*[:=]\s*(.+?)\s*$"
    camera_max_width: int = 1920      # largest camera frame width requested; more pixels cost CPU, not accuracy
    # who is scanning: names offered by the Operator picker, and the badge code that sets one (``OP: Anna``)
    operators: list[str] = field(default_factory=lambda: [])   # (not default_factory=list: the field named 'list' shadows it here)
    operator_code_regex: str = r"^\s*(?:OP|OPERATOR|USER|PERSON|MITARBEITER|NAME)\s*[:=]\s*(.+?)\s*$"
    # the aim box: the operator holds the label inside it; OCR and codes read its full-resolution crop
    aim_box: bool = True
    box_aspect: float = 2.3           # width / height of the box (label shape)
    box_width: float = 0.6            # box width as a fraction of the frame at zoom 1
    ocr_fail_passes: int = 3          # empty OCR passes on a steady box before the "type it" screen

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        d = dict(d)
        ls = d.pop("list", {}) or {}
        p = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        p.list = ListSpec(**{k: v for k, v in ls.items() if k in ListSpec.__dataclass_fields__})
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def find_near(cls, list_path: str | Path | None) -> "Profile | None":
        """The private profile that sits next to the list file, if any."""
        if not list_path:
            return None
        cand = Path(list_path).parent / PROFILE_FILENAME
        return cls.load(cand) if cand.exists() else None

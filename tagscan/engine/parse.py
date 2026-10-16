"""Find the inventory number in OCR text, driven by the profile's patterns."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..catalog import normalize_number
from ..profile import Profile


@dataclass
class Parsed:
    number: str = ""          # normalized, "" if none
    level: str = "none"       # "high": keyword/brand seen, "low": bare number, "none"
    keyword: bool = False
    brand: bool = False
    candidates: list[str] = field(default_factory=list)


class TextParser:
    def __init__(self, profile: Profile):
        self.brand = [re.compile(p, re.I) for p in profile.brand_patterns]
        self.keyword = [re.compile(p, re.I) for p in profile.keyword_patterns]
        lo, hi = profile.number_digits
        self.plain = re.compile(rf"(?<!\d)(\d{{{lo},{hi}}})(?!\d)")
        self.dotted = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{3})+)(?![\d.])") if profile.allow_dotted_numbers else None
        self.lo, self.hi = lo, hi
        self.code_regex = re.compile(profile.code_number_regex, re.I | re.S) if profile.code_number_regex else None

    def numbers_in(self, s: str) -> list[str]:
        out = []
        if self.dotted:
            for m in self.dotted.finditer(s):
                n = normalize_number(m.group(1))
                if self.lo <= len(n) <= self.hi:
                    out.append(n)
            s = self.dotted.sub(" ", s)
        out += [normalize_number(m.group(1)) for m in self.plain.finditer(s)]
        return [n for n in out if n]

    def has_brand(self, text: str) -> bool:
        return any(p.search(text) for p in self.brand)

    def has_keyword(self, text: str) -> bool:
        return any(p.search(text) for p in self.keyword)

    def parse(self, text: str) -> Parsed:
        if not text or not text.strip():
            return Parsed()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        brand = self.has_brand(text)
        keyword = self.has_keyword(text)
        candidates: list[str] = []
        number = ""

        kw_idx = next((i for i, ln in enumerate(lines) if self.has_keyword(ln)), -1)
        if kw_idx >= 0:
            same = self.numbers_in(lines[kw_idx])
            if same:
                number = same[-1]
            else:
                for j in range(kw_idx + 1, min(kw_idx + 3, len(lines))):
                    nxt = self.numbers_in(lines[j])
                    if nxt:
                        number = nxt[0]
                        break
        if not number:
            # a line that is nothing but a number, last one wins (number sits below the keyword)
            for ln in reversed(lines):
                cleaned = ln.replace(" ", "")
                nums = self.numbers_in(cleaned)
                if nums and re.fullmatch(r"[\d.]+", cleaned):
                    number = nums[0]
                    break
        if not number:
            nums = self.numbers_in(text)
            if nums:
                number = nums[0]
        for ln in lines:
            candidates += self.numbers_in(ln)
        level = "none" if not number else ("high" if (brand or keyword) else "low")
        return Parsed(number=number, level=level, keyword=keyword, brand=brand, candidates=candidates)

    def parse_code(self, text: str) -> str:
        """Inventory number from a QR/barcode payload."""
        t = (text or "").strip()
        if not t:
            return ""
        if re.fullmatch(r"[\d.\s]+", t):
            n = normalize_number(t)
            return n if self.lo <= len(n) <= self.hi else ""
        if self.code_regex:
            m = self.code_regex.search(t)
            if m:
                return normalize_number(m.group(1))
        return self.parse(t).number

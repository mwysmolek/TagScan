"""Bundled files (the logo), found both from source and inside the PyInstaller build."""
from __future__ import annotations

import sys
from pathlib import Path


def asset_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent))
    return base / "assets" / name

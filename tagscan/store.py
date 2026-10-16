"""Session storage: one SQLite file plus a photos folder per device, and the Excel export."""
from __future__ import annotations

import datetime as dt
import os
import socket
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .catalog import Catalog


@dataclass
class Scan:
    id: int
    ts: str
    number: str
    description: str
    location: str
    source: str
    in_list: bool
    photo: str
    device: str
    note: str = ""


class Store:
    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "photos").mkdir(exist_ok=True)
        self.device = os.environ.get("TAGSCAN_DEVICE") or socket.gethostname()
        self.db = sqlite3.connect(self.folder / "scans.sqlite", check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS scans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, number TEXT, description TEXT,
            location TEXT, source TEXT, in_list INTEGER, photo TEXT, device TEXT, note TEXT DEFAULT '')""")
        self.db.commit()

    # ------------------------------------------------------------------ writes
    def add(self, number: str, description: str, location: str, source: str, in_list: bool,
            image=None, note: str = "") -> Scan:
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        photo = ""
        if image is not None:
            import cv2
            safe = "".join(ch for ch in number if ch.isalnum()) or "x"
            photo = f"{safe}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            cv2.imwrite(str(self.folder / "photos" / photo), image, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        cur = self.db.execute("INSERT INTO scans(ts,number,description,location,source,in_list,photo,device,note)"
                              " VALUES(?,?,?,?,?,?,?,?,?)",
                              (ts, number, description, location, source, int(in_list), photo, self.device, note))
        self.db.commit()
        return Scan(cur.lastrowid, ts, number, description, location, source, in_list, photo, self.device, note)

    def delete(self, scan_id: int) -> None:
        row = self.db.execute("SELECT photo FROM scans WHERE id=?", (scan_id,)).fetchone()
        if row and row[0]:
            try:
                (self.folder / "photos" / row[0]).unlink()
            except OSError:
                pass
        self.db.execute("DELETE FROM scans WHERE id=?", (scan_id,))
        self.db.commit()

    def update(self, scan_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE scans SET {cols} WHERE id=?", (*fields.values(), scan_id))
        self.db.commit()

    # ------------------------------------------------------------------ reads
    def _rows(self, sql, args=()) -> list[Scan]:
        cur = self.db.execute(sql, args)
        return [Scan(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7], r[8], r[9]) for r in cur.fetchall()]

    def recent(self, n: int = 12) -> list[Scan]:
        return self._rows("SELECT id,ts,number,description,location,source,in_list,photo,device,note "
                          "FROM scans ORDER BY id DESC LIMIT ?", (n,))

    def all(self) -> list[Scan]:
        return self._rows("SELECT id,ts,number,description,location,source,in_list,photo,device,note "
                          "FROM scans ORDER BY id")

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM scans").fetchone()[0]

    def seen(self, number: str) -> list[Scan]:
        return self._rows("SELECT id,ts,number,description,location,source,in_list,photo,device,note "
                          "FROM scans WHERE number=? ORDER BY id", (number,))

    # ------------------------------------------------------------------ export
    def export_xlsx(self, path: str | Path, catalog: Catalog | None = None) -> Path:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = "Scans"
        head = ["Number", "Description", "Location", "Time", "Source", "In list", "Photo", "Device", "Note"]
        ws.append(head)
        for s in self.all():
            ws.append([s.number, s.description, s.location, s.ts, s.source, "yes" if s.in_list else "no",
                       s.photo, s.device, s.note])
        _style(ws, head, Font, PatternFill, get_column_letter)

        if catalog and len(catalog):
            found = {s.number for s in self.all()}
            ws2 = wb.create_sheet("List status")
            head2 = ["Number", "Description", "Status", "Location", "Time", "Device"]
            ws2.append(head2)
            last = {}
            for s in self.all():
                last[s.number] = s
            for it in catalog.items.values():
                s = last.get(it.key)
                ws2.append([it.number, it.description, "found" if s else "missing",
                            s.location if s else "", s.ts if s else "", s.device if s else ""])
            _style(ws2, head2, Font, PatternFill, get_column_letter)
            ws3 = wb.create_sheet("Not in list")
            head3 = ["Number", "Description", "Location", "Time", "Source", "Photo", "Device", "Note"]
            ws3.append(head3)
            for s in self.all():
                if not s.in_list:
                    ws3.append([s.number, s.description, s.location, s.ts, s.source, s.photo, s.device, s.note])
            _style(ws3, head3, Font, PatternFill, get_column_letter)
        path = Path(path)
        wb.save(path)
        return path

    def default_export_name(self) -> str:
        return f"TagScan_{self.device}_{dt.datetime.now().strftime('%Y-%m-%d_%H%M')}.xlsx"


def _style(ws, head, Font, PatternFill, get_column_letter):
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    widths = {"Number": 12, "Description": 48, "Location": 18, "Time": 20, "Source": 9, "In list": 8,
              "Photo": 30, "Device": 16, "Note": 30, "Status": 9}
    for i, h in enumerate(head, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(h, 14)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

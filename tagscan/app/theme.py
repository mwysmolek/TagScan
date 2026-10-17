"""One light look everywhere. Without this, Windows in dark mode hands Qt a dark palette:
white text lands on the app's light cards and buttons and disappears."""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

TEXT = "#1b1f24"
MUTED = "#6b7280"


def apply_light_theme(app) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: "#f3f4f6", QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: "#ffffff", QPalette.ColorRole.AlternateBase: "#f0f2f5",
        QPalette.ColorRole.Text: TEXT, QPalette.ColorRole.PlaceholderText: "#9aa0a6",
        QPalette.ColorRole.Button: "#e9ecf1", QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.Highlight: "#3c8ce6", QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: "#fffbe6", QPalette.ColorRole.ToolTipText: TEXT,
        QPalette.ColorRole.Link: "#1a5fb4", QPalette.ColorRole.Light: "#ffffff",
        QPalette.ColorRole.Midlight: "#e5e7eb", QPalette.ColorRole.Mid: "#c5c9d0",
        QPalette.ColorRole.Dark: "#9aa0a6", QPalette.ColorRole.Shadow: "#6b7280",
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor("#9aa0a6"))
    app.setPalette(pal)
    app.setStyleSheet(
        f"QWidget{{color:{TEXT};}}"
        f"QLineEdit{{color:{TEXT};background:#ffffff;border:1px solid #c5c9d0;border-radius:6px;padding:2px 8px;"
        f"selection-background-color:#3c8ce6;selection-color:#ffffff;}}"
        f"QComboBox{{color:{TEXT};background:#ffffff;border:1px solid #c5c9d0;border-radius:6px;padding:2px 8px;}}"
        f"QComboBox QAbstractItemView{{color:{TEXT};background:#ffffff;selection-background-color:#3c8ce6;selection-color:#ffffff;}}"
        f"QPushButton{{color:{TEXT};background:#e9ecf1;border:1px solid #c5c9d0;border-radius:8px;}}"
        f"QPushButton:pressed{{background:#d5dae2;}} QPushButton:disabled{{color:#9aa0a6;background:#f0f2f5;}}"
        f"QToolTip{{color:{TEXT};background:#fffbe6;border:1px solid #c5c9d0;}}"
        f"QStatusBar{{color:{MUTED};}} QMenu{{color:{TEXT};background:#ffffff;}} QMenu::item:selected{{background:#3c8ce6;color:#ffffff;}}"
        f"QProgressBar{{background:#e5e7eb;border:none;border-radius:5px;}} QProgressBar::chunk{{background:#2ea043;border-radius:5px;}}"
        f"QScrollArea{{background:transparent;}} QDialog{{background:#f3f4f6;}}"
    )

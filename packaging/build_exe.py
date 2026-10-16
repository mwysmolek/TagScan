"""Build the portable kayEi TagScan app with PyInstaller.

    pip install -r requirements.txt -r packaging/requirements-build.txt
    python packaging/build_exe.py            # -> dist/tagscan/  (zip that folder = the portable build)
Options: --console (debug build with a console window), --onefile.
Must run on the target OS; .github/workflows/build-windows.yml does it on a Windows runner.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
BUILD = ROOT / "build"
SEP = os.pathsep
NAME = "tagscan"


def version() -> str:
    text = (ROOT / "tagscan" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def make_icon() -> Path:
    from PIL import Image
    img = Image.open(ROOT / "assets" / "logo.png").convert("RGBA")
    BUILD.mkdir(exist_ok=True)
    if sys.platform == "darwin":
        icns = BUILD / "tagscan.icns"
        img.resize((1024, 1024), Image.LANCZOS).save(icns)
        return icns
    ico = BUILD / "tagscan.ico"
    img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ico


def version_file(ver: str) -> Path:
    nums = tuple((list(map(int, re.findall(r"\d+", ver))) + [0, 0, 0, 0])[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
          StringStruct('CompanyName', 'kayEi'),
          StringStruct('FileDescription', 'kayEi TagScan'),
          StringStruct('FileVersion', '{ver}'),
          StringStruct('InternalName', '{NAME}'),
          StringStruct('OriginalFilename', '{NAME}.exe'),
          StringStruct('ProductName', 'kayEi TagScan'),
          StringStruct('ProductVersion', '{ver}')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"""
    BUILD.mkdir(exist_ok=True)
    path = BUILD / "version_info.txt"
    path.write_text(text, encoding="utf-8")
    return path


def build(onefile: bool = False, console: bool = False) -> None:
    import PyInstaller.__main__
    excludes = ("PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
                "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtCharts", "PySide6.QtDataVisualization",
                "PySide6.QtBluetooth", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtTest",
                "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtNfc", "PySide6.QtRemoteObjects",
                "PySide6.QtScxml", "PySide6.QtSql", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
                "PySide6.QtUiTools", "PySide6.QtGraphs", "PySide6.QtHttpServer", "PySide6.QtSpatialAudio",
                "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtQuick3D", "PySide6.QtLocation",
                "matplotlib", "scipy", "pandas", "IPython", "pytest", "PyInstaller", "tkinter")
    args = [
        str(PKG / "tagscan_desktop.py"),
        "--name", NAME,
        "--onefile" if onefile else "--onedir",
        "--noconfirm", "--clean",
        "--icon", str(make_icon()),
        *(["--version-file", str(version_file(version()))] if sys.platform == "win32" else []),
        "--paths", str(ROOT),
        "--add-data", f"{ROOT / 'assets'}{SEP}assets",
        "--add-data", f"{ROOT / 'profiles'}{SEP}profiles",
        "--collect-all", "rapidocr_onnxruntime",
        "--collect-all", "onnxruntime",
        "--collect-binaries", "zxingcpp",
        "--hidden-import", "PySide6.QtMultimedia",
        "--hidden-import", "PySide6.QtNetwork",
        "--copy-metadata", "numpy", "--copy-metadata", "openpyxl",
        *[x for m in excludes for x in ("--exclude-module", m)],
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--console" if console else "--windowed",
    ]
    PyInstaller.__main__.run(args)


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(version())
        sys.exit(0)
    build(onefile="--onefile" in sys.argv, console="--console" in sys.argv)

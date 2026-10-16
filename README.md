<p align="center"><img src="assets/logo.png" width="120" alt="kayEi TagScan"></p>

# kayEi TagScan

Point a tablet camera at an inventory label and tick the item off a list. TagScan reads
**QR codes and barcodes** on every frame, and the **printed text** of the label when there is
no code, then looks the number up in your Excel or CSV list. Known items confirm themselves
with a short undo window; anything unsure stops and asks. Every confirmation stores number,
description, location, time, device and a photo. At the end you export one Excel per device.

Runs **standalone**: no server, no network, nothing installed. A portable zip for Windows
comes out of GitHub Actions; unzip anywhere and run `tagscan.exe`.

## Get it

* **Releases** on this repository: `kayEi-TagScan-portable-<version>.zip`. Unzip, run
  `tagscan.exe`. Windows SmartScreen warns once about the unsigned build: *More info → Run anyway*.
* Data lands in `Documents\TagScan` (`scans.sqlite`, `photos\`, `tagscan.log`, exports).

## Use it

1. **List …** choose the Excel/CSV with the known numbers (and the sheet). A
   `tagscan_profile.json` next to the list is picked up automatically and tells TagScan
   what your labels look like (brand words, keyword in front of the number, how many
   digits, which columns hold number and description). Without one a generic profile is used.
2. Type the **location** you are in.
3. Point the camera. Codes resolve instantly. For printed labels hold still for about a
   second; the number must be read twice and be in the list to auto-confirm.
4. Green card: **Found**, counts down, tap **Undo** if wrong. Orange card: **Check**, tap a
   suggestion, **OK**, or type the number. Items already scanned also stop for a check.
5. Recent scans sit at the bottom: tap one to delete it or change its location.
6. **Export Excel**: sheets *Scans*, *List status* (found / missing per list item) and
   *Not in list*.

**Photos …** replays a folder of photos through the same pipeline, for testing without a camera.
**Space** forces an OCR pass on the current frame. **Zoom** crops the centre of the frame.

## Profile

`profiles/example.json` documents every field. Copy it next to your list as
`tagscan_profile.json` and adapt the patterns. Regexes are case-insensitive; include the
OCR-mangled variants of your keyword (missing first letters are common).

## From source

```
pip install -r requirements.txt
python -m tagscan                      # the app
python -m tagscan bench PHOTOS_DIR --list list.xlsx [--profile p.json]   # engine on photos
python -m tagscan --smoke              # self-test
```

Build the portable app (on Windows): `pip install -r packaging/requirements-build.txt`,
`python packaging/build_exe.py`, zip `dist/tagscan`.

## How it works

* Codes: [zxing-cpp](https://github.com/zxing-cpp/zxing-cpp) on a downscaled grey frame, every frame.
* Text: [RapidOCR](https://github.com/RapidAI/RapidOCR) (ONNX, CPU) on the frame, tried in
  the four orientations with early exit, about 0.2 s per pass on a laptop CPU. A number is
  accepted when the profile's keyword or brand was seen next to it, it is in the list, and two
  consecutive reads agree. The item just confirmed is ignored for a few seconds so it does
  not re-trigger while still in view.
* Storage: SQLite + JPEG photos per device; merge several exports in Excel afterwards.

License: MIT.

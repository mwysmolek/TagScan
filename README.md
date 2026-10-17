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

Everything works with a finger; a keyboard is never required.

1. **List …** choose the Excel/CSV with the known numbers (and the sheet). A
   `tagscan_profile.json` next to the list is picked up automatically and tells TagScan
   what your labels look like (brand words, keyword in front of the number, how many
   digits, which columns hold number, description and location). Without one a generic
   profile is used.
2. **Location:** tap the location button and pick one: recent locations, every location
   named in the list (profile `list.location_column`), or a number pad for room numbers.
   Or skip the screen entirely and scan a **location code**: a QR code reading `LOC: Hall 3`
   switches the location (blue flash). Print one per room and stick it on the door.
3. Hold the label inside the **aim box** on the picture. Codes resolve instantly, anywhere
   in the picture. Printed labels are read from the box the moment its content holds still:
   the number must be read twice and be in the list to auto-confirm. Tap the picture to move
   the box there; **Read whole picture** (or Space) reads outside the box.
4. Green card: **Found**, counts down, tap **Undo** if wrong. Orange card: **Check**, tap a
   suggestion, **OK**, or type the number on the pad. Items already scanned also stop for a
   check. The pad under the card adds a number by hand at any time.
5. If three reads of a steady box come up empty, or you tap **Undo**, the card shows the box
   content with the pad: tap a suggestion or type the number. **Back** if there is nothing
   there.
6. Recent scans sit at the bottom: tap one to delete it or change its location.
7. **Export Excel**: sheets *Scans*, *List status* (found / missing per list item) and
   *Not in list*.

**Photos …** replays a folder of photos through the same pipeline, for testing without a camera.
**Zoom** (the − / + buttons or a pinch on the picture) shrinks the aim box: that is how to
read small or distant labels, since the box is always cut from the full-resolution frame.
With **Photos …** (no camera) the zoom crops the centre of each photo instead.

Every decision is kept in `scans.sqlite` (table *reads*) with the box crop under
`training\crops`, the frame, the text OCR saw and the number that was finally entered:
what was read right, what was corrected, what could not be read. That is the training
material for a label detector and for tuning the reader later.

The status bar shows the engine state, the OCR time per pass, the displayed frame rate and
the memory the app uses; include it in a bug report.

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

* Camera: the backend's frames are converted on the capture thread (mapped planes to OpenCV,
  no Qt image conversion, no GPU round trip) and only the newest one is handed to the GUI.
  Frames the GUI has not taken yet are dropped, never queued, so the app stays responsive
  and flat in memory however slow the tablet is. The preview is painted at view size. The
  camera is asked for at most `camera_max_width` (1920) pixels wide: the decoders downscale
  to 1280 (codes) and 960 (text) anyway, so a bigger frame costs CPU and RAM for nothing.
* Aim box: the operator is the label detector. The box (`aim_box`, `box_aspect`, `box_width`)
  is cut from the full-resolution frame, so the text keeps every pixel the camera has. A
  48x27 thumbnail of the box content decides whether it holds still, has contrast and is
  sharp; only then is it read, every `ocr_interval`, until a number is accepted or
  `ocr_fail_passes` passes came up empty and the pad is offered instead.
* Codes: [zxing-cpp](https://github.com/zxing-cpp/zxing-cpp) on the box crop about eight
  times a second and on the whole downscaled frame twice a second, limited to the formats in
  the profile (`code_formats`).
* Text: [RapidOCR](https://github.com/RapidAI/RapidOCR) (ONNX, CPU) on the frame, at most
  every `ocr_interval` seconds, on a bounded number of threads (`ocr_threads`, default half
  the CPUs) so the GUI and the camera keep theirs. Orientations 0° and 90° are tried with
  early exit; RapidOCR's line classifier handles upside-down text, which covers all four
  label orientations at half the cost (`ocr_rotations` to change). A number is accepted
  when the profile's keyword or brand was seen next to it, it is in the list, and two
  consecutive reads agree. The item just confirmed is ignored for a few seconds so it does
  not re-trigger while still in view.
* Location codes: a code whose text matches `location_code_regex` (`LOC:`, `ORT:`, `RAUM:`,
  `STANDORT:`, `ROOM:`, `LOCATION:` followed by the name) sets the location instead of
  being treated as an item.
* Storage: SQLite + JPEG photos per device; merge several exports in Excel afterwards.

License: MIT.

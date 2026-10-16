"""Command line: ``tagscan bench DIR`` runs the engine over photos, ``tagscan --smoke`` self-tests."""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "bench":
        return bench(argv[1:])
    if argv and argv[0] in ("--smoke", "smoke"):
        from .app.main import smoke
        return smoke()
    from .app.main import main as app_main
    return app_main(argv)


def _load(args):
    from .profile import Profile
    from .catalog import Catalog
    profile = Profile.load(args.profile) if args.profile else (Profile.find_near(args.list) or Profile())
    catalog = Catalog.load(args.list, profile.list) if args.list else Catalog()
    return profile, catalog


def bench(argv) -> int:
    import cv2
    from .engine import Scanner
    ap = argparse.ArgumentParser(prog="tagscan bench")
    ap.add_argument("dirs", nargs="+")
    ap.add_argument("--list", help="xlsx/csv list of known numbers")
    ap.add_argument("--profile", help="profile json (default: tagscan_profile.json next to the list)")
    ap.add_argument("--max-side", type=int, default=None)
    args = ap.parse_args(argv)
    profile, catalog = _load(args)
    if args.max_side:
        profile.ocr_max_side = args.max_side
    sc = Scanner(profile, catalog)
    sc.warmup()
    files = []
    for d in args.dirs:
        files += sorted(f for f in glob.glob(os.path.join(d, "*")) if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")))
    print(f"profile {profile.name}, list {len(catalog)} items, {len(files)} images")
    print(f"{'file':32} {'src':7} {'number':9} {'lvl':5} {'list':5} {'rot':4} {'pass':4} {'sec':5}")
    n_num = n_list = n_code = 0
    t_all = 0.0
    for f in files:
        img = cv2.imread(f)
        if img is None:
            continue
        sc.reset()
        obs, dec, _ = sc.process(img, force_ocr=True)
        o = next((x for x in obs if x.source != "OCR"), None) or (obs[0] if obs else None)
        t_all += sum(x.elapsed for x in obs) if obs else 0
        if o:
            n_num += 1
            n_list += o.in_list
            n_code += o.source != "OCR"
            hit = sc.ocr
            print(f"{os.path.basename(f)[:32]:32} {o.source:7} {o.number:9} {o.level:5} {str(o.in_list):5} "
                  f"{o.rotation:4} {'':4} {o.elapsed:5.2f}")
        else:
            print(f"{os.path.basename(f)[:32]:32} {'-':7} {'':9} {'':5} {'':5}")
    st = sc.stats
    print(f"\nimages {len(files)}  numbers {n_num}  in list {n_list}  via code {n_code}")
    if st["ocr_passes"]:
        print(f"ocr passes {st['ocr_passes']}  avg {st['ocr_time']/st['ocr_passes']:.2f}s/pass  "
              f"{st['ocr_time']/max(1,len(files)):.2f}s/image")
    return 0


if __name__ == "__main__":
    sys.exit(main())

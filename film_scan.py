#!/usr/bin/env python3
"""Scan a 35 mm strip on a Nikon LS-50 (SA-21 strip feeder) through SANE, with the
frame positions found automatically instead of nudged by hand.

    film_scan.py probe                       # is the scanner there, how many frames
    film_scan.py preview --frame 1           # low-res scan of one slot + found gaps
    film_scan.py calibrate                   # preview every slot, find each picture's crop
    film_scan.py scan --roll 3403 --out ~/Pictures/2026/film/3403 --ir

The feeder senses the frames when the strip goes in and offers each as a 37.8 mm
window, with the gap before the frame at its top. Film outside the window is behind
the holder's mask, so shifting the window shows black, not the next frame.
What varies is where the picture sits inside the window: on roll 3403 its start moved
from 2.1 mm to 1.0 mm over six frames. A 400 dpi preview of each slot is read row by
row along the strip; the gap (clear base) and the strip's leading edge (no film) are
bright and even across the film, pictures are not. Each frame is then scanned from the
end of its gap to the next gap or the end of the window.

Talks to the scanner directly over USB through ir/ls50.py (pyusb + numpy, see
requirements.txt); with --ir each frame's infrared channel is written next to it for
dust removal. Quit VueScan first: it holds the USB connection.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent / "ir"))
from ls50 import EXPOSURE_RGB, LS50, ScannerError, write_tiff  # noqa: E402

WINDOW_MM = 37.8333              # scan window along the strip (5958 pel at 4000 dpi)
FRAME_MM = 36.0
PREVIEW_DPI = 400
MARGIN_MM = 0.2                  # keep this much of the gap around the picture
GAP_SEARCH_MM = 5.0              # the leading gap must start within this of the window top
PEL_PER_MM = 4000 / 25.4         # -t/-y are in 4000 dpi pixels
MAX_SPREAD = 0.04                # relative std across a row that still counts as "even"


def row_profile(img: np.ndarray) -> tuple[list[float], list[float]]:
    """Mean brightness and relative spread (std/mean) per row along the strip, across the
    film inside the holder's side masks. img: (h, w, channels)."""
    w = img.shape[1]
    vals = img[:, w // 10:w - w // 10].astype(np.float64).mean(axis=2)
    means = vals.mean(axis=1)
    spreads = vals.std(axis=1) / np.maximum(means, 1.0)
    return means.tolist(), spreads.tolist()


@dataclass
class Band:
    start_mm: float
    end_mm: float

    @property
    def centre_mm(self) -> float:
        return (self.start_mm + self.end_mm) / 2


def find_bands(means: list[float], spreads: list[float], dpi: int = PREVIEW_DPI) -> list[Band]:
    """Bright, even runs of rows: frame gaps (clear base), the strip's leading edge or an
    empty holder (no film). Measured on roll 3403 at 400 dpi: no film has a relative
    spread of 0.000 across the row; bright walls and sky inside a frame 0.11-0.20."""
    lo, hi = min(means), max(means)
    threshold = lo + 0.75 * (hi - lo)
    mm = 25.4 / dpi
    bands, start = [], None
    for y, (m, s) in enumerate(zip(means + [0.0], spreads + [1e9])):
        even = m >= threshold and s <= MAX_SPREAD
        if even and start is None:
            start = y
        elif not even and start is not None:
            if (y - start) * mm >= 0.3:                  # ignore specks and scratches
                bands.append(Band(round(start * mm, 2), round(y * mm, 2)))
            start = None
    return bands


def preview(scanner: LS50, frame: int) -> tuple[list[Band], np.ndarray]:
    """Bands found in a 400 dpi preview of one slot, and the preview itself (RGB)."""
    rgb, _ = scanner.scan(frame, PREVIEW_DPI, ir=False, focus=False, progress=False)
    means, spreads = row_profile(rgb)
    return find_bands(means, spreads), rgb


def choose_crop(bands: list[Band]) -> tuple[float, float, bool]:
    """(top, bottom) in mm of the picture inside the slot, and whether a gap was found.

    The feeder puts the gap before each frame at the top of the window (0-2 mm on roll
    3403); the picture runs from its end to the next gap or the end of the window. Film
    outside the window is behind the holder's mask, so nothing is gained by shifting."""
    top, found = 0.0, False
    for b in bands:
        if b.start_mm <= GAP_SEARCH_MM:
            top, found = b.end_mm, True
            break
    bottom = next((b.start_mm for b in bands if b.start_mm > top + FRAME_MM / 2), WINDOW_MM)
    return (round(max(top - MARGIN_MM, 0.0), 2), round(min(bottom + MARGIN_MM, WINDOW_MM), 2),
            found)


def calibrate(scanner: LS50, save_previews: Path | None = None) -> list[dict]:
    n = scanner.info["frames"]
    if not n:
        raise ScannerError("no frames sensed: insert the strip in the feeder")
    plan, last_top = [], 0.0
    for f in range(1, n + 1):
        bands, img = preview(scanner, f)
        top, bottom, found = choose_crop(bands)
        if not found:                    # gap hidden (e.g. a frame shot right up to it):
            top = last_top               # the drift is ~0.25 mm per frame, keep the previous
        if save_previews:
            save_previews.mkdir(parents=True, exist_ok=True)
            write_tiff(save_previews / f"frame{f}.tif", img, PREVIEW_DPI)
        plan.append({"frame": f, "top_mm": top, "bottom_mm": bottom, "gap_found": found,
                     "bands_mm": [[b.start_mm, b.end_mm] for b in bands]})
        last_top = top
        print(f"frame {f}: picture {top:.2f}-{bottom:.2f} mm"
              + ("" if found else " (no gap found, kept the previous start)"))
    if not any(p["gap_found"] for p in plan):
        print("warning: no gap found in any slot; if the feeder lost track of the strip, "
              "take it out and insert it again", file=sys.stderr)
    return plan


def next_number(out: Path, roll: str) -> int:
    """Number for the first frame of this strip: one past the highest neg_<roll>_NNNN.tif in out."""
    nums = [int(f.stem.split("_")[-1]) for f in out.glob(f"neg_{roll}_[0-9][0-9][0-9][0-9].tif")]
    return max(nums, default=0) + 1


def scan(scanner: LS50, plan: list[dict], out: Path, roll: str, first: int, ir: bool,
         exposure: float, ir_exposure_ms: float | None, ir_separate: bool = False) -> None:
    exp = tuple(round(e * exposure) for e in EXPOSURE_RGB)
    exp_ir = None if ir_exposure_ms is None else round(ir_exposure_ms * 1e5)
    for p in plan:
        stem = f"neg_{roll}_{first + p['frame'] - 1:04d}"
        t0 = time.monotonic()
        rgb, irimg = scanner.scan(p["frame"], 4000, ir, exp, exp_ir, focus=True,
                                  top=round(p["top_mm"] * PEL_PER_MM),
                                  height=round((p["bottom_mm"] - p["top_mm"]) * PEL_PER_MM))
        if irimg is not None and not ir_separate:
            write_tiff(out / f"{stem}.tif", np.dstack([rgb, irimg]), 4000)
            what = f"{stem}.tif (RGBI)"
        else:
            write_tiff(out / f"{stem}.tif", rgb, 4000)
            what = f"{stem}.tif"
            if irimg is not None:
                write_tiff(out / f"{stem}_ir.tif", irimg, 4000)
                what += f" + {stem}_ir.tif"
        print(f"frame {p['frame']}: {what} "
              f"({rgb.shape[1]}x{rgb.shape[0]}, {time.monotonic() - t0:.0f} s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    pv = sub.add_parser("preview")
    pv.add_argument("--frame", type=int, default=1)
    pv.add_argument("-o", "--output", type=Path, help="save the preview (.tif)")
    cb = sub.add_parser("calibrate")
    cb.add_argument("--save-previews", type=Path)
    sc = sub.add_parser("scan")
    sc.add_argument("--roll", required=True)
    sc.add_argument("--out", type=Path, required=True)
    sc.add_argument("--first", type=int, default=None,
                    help="number of the strip's first frame (default: continue after the "
                         "highest neg_<roll>_NNNN.tif already in --out)")
    sc.add_argument("--ir", "--infrared", dest="ir", action="store_true",
                    help="also scan infrared, written as the fourth channel (RGBI, 64-bit TIFF)")
    sc.add_argument("--ir-separate", action="store_true",
                    help="with --ir: write infrared to its own _ir.tif instead")
    sc.add_argument("--exposure", type=float, default=1.0)
    sc.add_argument("--ir-exposure", type=float, default=None,
                    help="infrared exposure in ms (default 2.4)")
    sc.add_argument("--calibrate-only", action="store_true",
                    help="write the previews and plan, scan nothing")
    args = ap.parse_args()

    try:
        scanner = LS50()
    except ScannerError as e:
        sys.exit(f"film_scan: {e}")
    try:
        info = scanner.inquiry()
        if args.cmd == "probe":
            print(f"{info['product']}: {info['frames']} frame(s) sensed")
        elif args.cmd == "preview":
            bands, img = preview(scanner, args.frame)
            if args.output:
                write_tiff(args.output, img, PREVIEW_DPI)
            top, bottom, found = choose_crop(bands)
            print(json.dumps({"bands_mm": [[b.start_mm, b.end_mm] for b in bands],
                              "picture_mm": [top, bottom], "gap_found": found}))
        elif args.cmd == "calibrate":
            calibrate(scanner, args.save_previews)
        elif args.cmd == "scan":
            out = args.out.expanduser()
            out.mkdir(parents=True, exist_ok=True)
            t0 = time.monotonic()
            first = args.first if args.first is not None else next_number(out, args.roll)
            print(f"strip starts at frame {first:04d}")
            plan = calibrate(scanner, out / ".previews" / f"{first:04d}")
            (out / f"neg_{args.roll}_{first:04d}_plan.json").write_text(json.dumps(plan, indent=1))
            if not args.calibrate_only:
                scan(scanner, plan, out, args.roll, first, args.ir, args.exposure,
                     args.ir_exposure, args.ir_separate)
            print(f"done in {time.monotonic() - t0:.0f} s")
    except ScannerError as e:
        sys.exit(f"film_scan: {e}")
    finally:
        scanner.close()


if __name__ == "__main__":
    main()

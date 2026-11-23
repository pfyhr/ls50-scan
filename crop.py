#!/usr/bin/env python3
"""Trim the holder mask and the frame gap from full-resolution LS-50 scans.

The strip calibration crops each frame between gaps from a low-resolution preview, which
leaves three things at full resolution:

- the SA-21 mask at the left and right (~125 columns each, opaque),
- the mask's rounded window corners at the end of the slot, with streaks below them,
- a sliver (0.1-0.3 mm) of the gap and the film edge at the top.

The mask is opaque, so it is near zero in the infrared (and in all three colours); film
is not, however dense. The gap is clear film base or no film: brighter than the picture
and even across the row.

    crop.py FILE_OR_DIR [...]   trim neg_*.tif in place (verified read-back)
    crop.py --dry-run ...       only print the crops
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

MASK_LEVEL = 0.3     # below this fraction of the film's median signal: mask
COL_MASK_FRAC = 0.01  # a column is mask if more of its middle rows than this are mask
ROW_MASK_FRAC = 0.01  # an end row is mask (corner) if more of its width than this is mask
SIDE_MARGIN = 8       # extra columns trimmed past the mask edge (its blur)
END_MARGIN = 15       # extra rows trimmed past a corner
GAP_SPREAD = 0.15     # a gap row: relative spread (std/mean, worst channel) at most this
GAP_BRIGHT = 0.9      # ... and at least this fraction of the first row's brightness
GAP_EDGE = 25         # rows trimmed past the gap: the blurred film edge
MAX_GAP_ROWS = 300    # 1.9 mm at 4000 dpi; never trim more than this as gap


def find_crop(img: np.ndarray) -> tuple[int, int, int, int]:
    """(top, bottom, left, right) of the picture, as slice bounds, for an (h, w, 3|4)
    scan; uses the infrared (fourth channel) when present, else the mean of RGB."""
    h, w = img.shape[:2]
    rgb = img[..., :3]
    sig = (img[..., 3] if img.shape[2] > 3 else rgb.mean(2)).astype(np.float32)
    med = float(np.median(sig[h // 4:3 * h // 4, w // 4:3 * w // 4]))
    mask = sig < MASK_LEVEL * med

    colfrac = mask[h // 4:3 * h // 4].mean(0)
    clear = np.flatnonzero(colfrac <= COL_MASK_FRAC)
    left, right = int(clear[0]) + SIDE_MARGIN, int(clear[-1]) + 1 - SIDE_MARGIN

    rowfrac = mask[:, left:right].mean(1)
    top, bottom = 0, h
    while top < h // 4 and rowfrac[top] > ROW_MASK_FRAC:
        top += 1
    if top:
        top += END_MARGIN
    while bottom > 3 * h // 4 and rowfrac[bottom - 1] > ROW_MASK_FRAC:
        bottom -= 1
    if bottom < h:
        bottom -= END_MARGIN

    # gap at the top: rows as bright as the first one and even across the film
    band = rgb[top:top + MAX_GAP_ROWS, left:right].astype(np.float32)
    mean = band.mean(1)                                   # (rows, 3)
    spread = (band.std(1) / np.maximum(mean, 1)).max(1)
    bright = mean.mean(1)
    if spread[0] <= GAP_SPREAD and bright[0] > 1.2 * np.median(rgb[h // 2]):
        n = 0
        while n < len(bright) and spread[n] <= GAP_SPREAD and bright[n] >= GAP_BRIGHT * bright[0]:
            n += 1
        top += min(n + GAP_EDGE, MAX_GAP_ROWS)
    return top, bottom, left, right


def crop_file(path: Path, dry_run: bool) -> None:
    import tifffile
    sys.path.insert(0, str(Path(__file__).resolve().parent / "ir"))
    from ls50 import write_tiff

    img = tifffile.imread(path)
    top, bottom, left, right = find_crop(img)
    h, w = img.shape[:2]
    print(f"{path.name}: {w}x{h} -> {right - left}x{bottom - top} "
          f"(top {top}, bottom {h - bottom}, left {left}, right {w - right})")
    if dry_run or (top, bottom, left, right) == (0, h, 0, w):
        return
    out = np.ascontiguousarray(img[top:bottom, left:right])
    dpi = tifffile.TiffFile(path).pages[0].resolution[0] or 4000
    tmp = path.with_suffix(".crop.tmp")
    write_tiff(tmp, out, round(dpi))
    if not np.array_equal(tifffile.imread(tmp), out):
        tmp.unlink()
        raise ValueError(f"{path.name}: cropped file does not read back identical")
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    for p in args.paths:
        p = p.expanduser()
        files = sorted(p.glob("neg_*_[0-9][0-9][0-9][0-9].tif")) if p.is_dir() else [p]
        for f in files:
            crop_file(f, args.dry_run)


if __name__ == "__main__":
    main()

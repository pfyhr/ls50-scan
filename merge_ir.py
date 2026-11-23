#!/usr/bin/env python3
"""Merge neg_*.tif + neg_*_ir.tif pairs into one RGBI TIFF (64 bits per pixel).

Older scans (and --ir-separate) write the infrared channel to its own file. This writes
RGB + infrared as one 4-channel 16-bit TIFF in place of the RGB file, checks that it
reads back identical, and only then deletes the _ir.tif.

    merge_ir.py DIR [DIR ...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parent / "ir"))
from ls50 import write_tiff  # noqa: E402


def merge(rgb_path: Path, ir_path: Path) -> None:
    rgb = tifffile.imread(rgb_path)
    ir = tifffile.imread(ir_path)
    if rgb.ndim != 3 or rgb.shape[2] != 3 or ir.shape != rgb.shape[:2]:
        raise ValueError(f"{rgb_path.name}: shapes {rgb.shape} and {ir.shape} don't pair up")
    rgbi = np.dstack([rgb, ir])
    dpi = tifffile.TiffFile(rgb_path).pages[0].resolution[0] or 4000
    tmp = rgb_path.with_suffix(".rgbi.tmp")
    write_tiff(tmp, rgbi, round(dpi))
    if not np.array_equal(tifffile.imread(tmp), rgbi):
        tmp.unlink()
        raise ValueError(f"{rgb_path.name}: merged file does not read back identical")
    tmp.replace(rgb_path)
    ir_path.unlink()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dirs", nargs="+", type=Path)
    args = ap.parse_args()
    n = 0
    for d in args.dirs:
        for ir_path in sorted(d.expanduser().glob("neg_*_ir.tif")):
            rgb_path = ir_path.with_name(ir_path.name.replace("_ir.tif", ".tif"))
            if not rgb_path.exists():
                print(f"skip {ir_path.name}: no {rgb_path.name}", file=sys.stderr)
                continue
            merge(rgb_path, ir_path)
            print(f"{rgb_path.name}: RGBI")
            n += 1
    print(f"merged {n} frame(s)")


if __name__ == "__main__":
    main()

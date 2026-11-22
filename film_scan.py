#!/usr/bin/env python3
"""Scan a 35 mm strip on a Nikon LS-50 (SA-21 strip feeder) through SANE, with the
frame positions found automatically instead of nudged by hand.

    film_scan.py probe                       # is the scanner there, how many frames
    film_scan.py preview --frame 1           # low-res scan of one slot + found gaps
    film_scan.py calibrate                   # preview every slot, find each picture's crop
    film_scan.py scan --roll 3403 --out ~/Pictures/2026/film/3403

The feeder senses the frames when the strip goes in and offers each as a 37.8 mm
window, with the gap before the frame at its top. Film outside the window is behind
the holder's mask, so shifting the window (--subframe) shows black, not the next frame.
What varies is where the picture sits inside the window: on roll 3403 its start moved
from 2.1 mm to 1.0 mm over six frames. A 400 dpi preview of each slot is read row by
row along the strip; the gap (clear base) and the strip's leading edge (no film) are
bright and even across the film, pictures are not. Each frame is then scanned from the
end of its gap to the next gap or the end of the window.

Needs sane-backends (brew install sane-backends) and VueScan closed: it holds the USB
connection. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

WINDOW_MM = 37.8333              # scan window along the strip (5958 pel at 4000 dpi)
FRAME_MM = 36.0
PREVIEW_DPI = 400
MARGIN_MM = 0.2                  # keep this much of the gap around the picture
GAP_SEARCH_MM = 5.0              # the leading gap must start within this of the window top
PEL_PER_MM = 4000 / 25.4         # -t/-y are in 4000 dpi pixels
MAX_SPREAD = 0.04                # relative std across a row that still counts as "even"


class ScanError(RuntimeError):
    pass


def scanimage(*args: str, device: str | None = None, capture: bool = True) -> bytes:
    cmd = ["scanimage", *(["-d", device] if device else []), *args]
    p = subprocess.run(cmd, capture_output=capture)
    if p.returncode:
        raise ScanError(f"{' '.join(cmd)}: {(p.stderr or b'').decode().strip()}")
    return p.stdout or b""


def find_device() -> str:
    out = scanimage("-L").decode()
    m = re.search(r"device `(coolscan3:[^']+)' is a Nikon LS-50", out)
    if not m:
        raise ScanError(f"no LS-50 found (is VueScan closed?)\n{out.strip()}")
    return m.group(1)


def frame_count(device: str) -> int:
    """Frames the feeder sensed; 0 when no strip is loaded."""
    out = scanimage("--help", device=device).decode()
    m = re.search(r"--frame 1\.\.(\d+)", out)
    return int(m.group(1)) if m else 0


def read_pnm(data: bytes) -> tuple[int, int, int, bytes]:
    """(width, height, channels, pixels) of an 8-bit P5/P6 image."""
    tokens, pos = [], 0
    while len(tokens) < 4:
        m = re.compile(rb"\s*(#[^\n]*\n\s*)*(\S+)").match(data, pos)
        tokens.append(m.group(2))
        pos = m.end()
    magic, w, h, maxval = tokens[0], int(tokens[1]), int(tokens[2]), int(tokens[3])
    if maxval > 255 or magic not in (b"P5", b"P6"):
        raise ScanError(f"unexpected preview format {magic!r} maxval {maxval}")
    channels = 3 if magic == b"P6" else 1
    return w, h, channels, data[pos + 1:pos + 1 + w * h * channels]


def row_profile(w: int, h: int, ch: int, px: bytes) -> tuple[list[float], list[float]]:
    """Mean brightness and relative spread (std/mean) per row along the strip, across the
    film inside the holder's side masks."""
    x0, x1 = w // 10, w - w // 10
    means, spreads = [], []
    for y in range(h):
        row = px[(y * w + x0) * ch:(y * w + x1) * ch]
        vals = [sum(row[i:i + ch]) / ch for i in range(0, len(row), ch)]
        m = sum(vals) / len(vals)
        means.append(m)
        spreads.append((sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5 / max(m, 1.0))
    return means, spreads


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


def preview(device: str, frame: int, subframe: float = 0.0) -> tuple[list[Band], bytes]:
    """Bands found in a 400 dpi preview of one slot, and the preview itself (PNM)."""
    data = scanimage("--frame", str(frame), "--subframe", f"{subframe:.2f}",
                     "--resolution", str(PREVIEW_DPI), "--depth", "8", "--format=pnm",
                     device=device)
    means, spreads = row_profile(*read_pnm(data))
    return find_bands(means, spreads), data


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


def calibrate(device: str, save_previews: Path | None = None) -> list[dict]:
    n = frame_count(device)
    if not n:
        raise ScanError("no frames sensed: insert the strip in the feeder")
    plan, last_top = [], 0.0
    for f in range(1, n + 1):
        bands, data = preview(device, f)
        top, bottom, found = choose_crop(bands)
        if not found:                    # gap hidden (e.g. a frame shot right up to it):
            top = last_top               # the drift is ~0.25 mm per frame, keep the previous
        if save_previews:
            save_previews.mkdir(parents=True, exist_ok=True)
            (save_previews / f"frame{f}.pnm").write_bytes(data)
        plan.append({"frame": f, "top_mm": top, "bottom_mm": bottom, "gap_found": found,
                     "bands_mm": [[b.start_mm, b.end_mm] for b in bands]})
        last_top = top
        print(f"frame {f}: picture {top:.2f}-{bottom:.2f} mm"
              + ("" if found else " (no gap found, kept the previous start)"))
    return plan


def scan(device: str, plan: list[dict], out: Path, roll: str, infrared: bool, exposure: float) -> None:
    for p in plan:
        stem = f"neg_{roll}_{p['frame']:04d}"
        top = round(p["top_mm"] * PEL_PER_MM)
        height = round((p["bottom_mm"] - p["top_mm"]) * PEL_PER_MM)
        args = ["--frame", str(p["frame"]), "-t", str(top), "-y", str(height),
                "--resolution", "4000", "--depth", "14", "--exposure", f"{exposure:g}",
                "--autofocus=yes", "--format=tiff"]
        if infrared:                     # SANE reads infrared as a second pass of the batch
            scanimage(*args, "--infrared=yes", "--batch-count=2",
                      f"--batch={out}/{stem}-%d.tif", device=device, capture=False)
        else:
            scanimage(*args, "-o", str(out / f"{stem}.tif"), device=device, capture=False)
        print(f"frame {p['frame']}: {stem}{'-1/-2' if infrared else ''}.tif")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    pv = sub.add_parser("preview")
    pv.add_argument("--frame", type=int, default=1)
    pv.add_argument("--subframe", type=float, default=0.0)
    pv.add_argument("-o", "--output", type=Path, help="save the preview (.pnm)")
    cb = sub.add_parser("calibrate")
    cb.add_argument("--save-previews", type=Path)
    sc = sub.add_parser("scan")
    sc.add_argument("--roll", required=True)
    sc.add_argument("--out", type=Path, required=True)
    sc.add_argument("--infrared", action="store_true")
    sc.add_argument("--exposure", type=float, default=1.0)
    sc.add_argument("--calibrate-only", action="store_true",
                    help="write the previews and plan, scan nothing")
    args = ap.parse_args()

    try:
        device = find_device()
        if args.cmd == "probe":
            print(f"{device}: {frame_count(device)} frame(s) sensed")
        elif args.cmd == "preview":
            bands, data = preview(device, args.frame, args.subframe)
            if args.output:
                args.output.write_bytes(data)
            top, bottom, found = choose_crop(bands)
            print(json.dumps({"bands_mm": [[b.start_mm, b.end_mm] for b in bands],
                              "picture_mm": [top, bottom], "gap_found": found}))
        elif args.cmd == "calibrate":
            calibrate(device, args.save_previews)
        elif args.cmd == "scan":
            out = args.out.expanduser()
            out.mkdir(parents=True, exist_ok=True)
            plan = calibrate(device, out / ".previews")
            (out / f"neg_{args.roll}_plan.json").write_text(json.dumps(plan, indent=1))
            if not args.calibrate_only:
                scan(device, plan, out, args.roll, args.infrared, args.exposure)
    except ScanError as e:
        sys.exit(f"film_scan: {e}")


if __name__ == "__main__":
    main()

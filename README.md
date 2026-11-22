# ls50-scan

Command-line scanning of 35 mm strips on a Nikon Coolscan LS-50 ED (SA-21 strip
feeder) on macOS, without VueScan: frames cropped automatically from their gaps,
raw 14-bit output.

```sh
brew install sane-backends
python3 film_scan.py probe                     # scanner found? frames sensed?
python3 film_scan.py preview --frame 1         # one slot at 400 dpi: gap and picture position
python3 film_scan.py scan --roll 3403 --out ~/Pictures/film/3403
```

Quit VueScan first: it holds the scanner's USB connection.

## How it works

The SA-21 senses the frames when a strip is inserted and offers each as a 37.8 mm
window with the gap before the frame at its top. Film outside the window is behind the
holder's mask, so shifting the window (`--subframe`) shows black rather than the next
frame; what varies is where the picture sits inside it (on one test strip its start
moved from 2.1 mm to 1.0 mm over six frames).

`scan` previews each slot at 400 dpi and reads it row by row along the strip. The gap
(clear film base) and the strip's leading edge (no film) are bright and even across the
film; pictures are not, even where they show a white wall or sky. Each frame is then
scanned at 4000 dpi, 14-bit, from the end of its gap to the next gap or the end of the
window. Output: `neg_<roll>_000N.tif` (16-bit RGB, not inverted), the previews, and
`neg_<roll>_plan.json` with the crop of every frame.

Standard library only (Python 3.10+), driving SANE's `scanimage` with the `coolscan3`
backend.

## Status

| | |
|---|---|
| Frame detection and cropping | works (tested on one 6-frame colour negative strip) |
| 4000 dpi 14-bit RGB | works, ~1 min and 135 MB per frame |
| Infrared (dust removal) | works with our own USB driver `ir/ls50.py` (tested at 1000 dpi on one frame); not yet wired into `film_scan.py`. SANE can't do it: see `ir/PROTOCOL.md`. |

## Infrared

`ir/ls50.py` talks to the scanner directly over USB (pyusb on Homebrew's libusb) and
reads the infrared channel together with RGB:

```sh
python3 -m venv .venv && .venv/bin/pip install pyusb
.venv/bin/python ir/ls50.py scan --frame 3 --dpi 1000 --ir -o out/f3   # f3_rgb.pnm, f3_ir.pnm
```

16-bit PNM, raw. `--ir-exposure` sets the infrared exposure in ms (default 1.2;
2.4 puts clear film at ~60% of full scale). The protocol is described in
`ir/PROTOCOL.md`.

## License

MIT. SANE itself is GPL; this project calls `scanimage` as a separate program and
contains no SANE code. `ir/` is our own implementation of the scanner protocol.

# ls50-scan

Command-line scanning of 35 mm strips on a Nikon Coolscan LS-50 ED (SA-21 strip
feeder) on macOS, without VueScan or SANE: frames cropped automatically from their
gaps, raw 14-bit output, and the infrared channel for dust removal.

```sh
brew install libusb
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python film_scan.py probe                 # scanner found? frames sensed?
.venv/bin/python film_scan.py preview --frame 1     # one slot at 400 dpi: gap and picture position
.venv/bin/python film_scan.py scan --roll 3403 --out ~/Pictures/film/3403 --ir
```

Quit VueScan first: it holds the scanner's USB connection.

## How it works

The SA-21 senses the frames when a strip is inserted and offers each as a 37.8 mm
slot with the gap before the frame at its top. The scanner can't scan past the end of
a slot; what varies is where the picture sits inside it (on one test strip its start
moved from 2.1 mm to 1.0 mm over six frames).

`scan` previews each slot at 400 dpi and reads it row by row along the strip. The gap
(clear film base) and the strip's leading edge (no film) are bright and even across the
film; pictures are not, even where they show a white wall or sky. Each frame is then
scanned at 4000 dpi, 14-bit, from the end of its gap to the next gap or the end of the
slot. Output:

- `neg_<roll>_NNNN.tif`: 16-bit RGB, not inverted, uncompressed. With `--ir` it is RGBI
  instead: infrared, read in the same pass, is the fourth 16-bit channel (64 bits per
  pixel, like VueScan's raw RGBI files). Clear film is a flat field at ~60 % of full
  scale in the infrared; dust and scratches are dark specks. Colour dyes leave a faint
  (5-10 %) ghost of the picture. `--ir-separate` writes the infrared to its own
  `neg_<roll>_NNNN_ir.tif` instead; `merge_ir.py DIR` merges such pairs into RGBI.
- Each frame is then trimmed at full resolution (`crop.py`): the holder mask at the sides
  and its rounded window corners at the slot end, found in the infrared (or, without
  it, the colours) where the opaque mask reads near zero, and the last sliver of the
  gap at the top. `--no-trim` keeps the full scan; `crop.py DIR` trims earlier scans.
- `.previews/NNNN/frameN.tif` and `neg_<roll>_NNNN_plan.json` with the crop of every frame

Frame numbers continue across strips: each scan starts after the highest
`neg_<roll>_NNNN.tif` already in `--out` (or at `--first N`), so scan a roll strip by
strip into one folder.

`ir/ls50.py` is the scanner driver (pyusb + numpy): it speaks the LS-50's USB protocol
directly, described in `ir/PROTOCOL.md`. It can also be used on its own:

```sh
.venv/bin/python ir/ls50.py scan --frame 3 --dpi 4000 --ir --top 1.2 --height 36 -o out/f3
```

`--ir-exposure` sets the infrared exposure in ms (default 2.4; it doesn't need to change
with resolution).

## Why not SANE

SANE's `coolscan3` backend scans fine but has its infrared option compiled out, and
`coolscan2`'s infrared scan fails because it sends a set-window command the LS-50
refuses for the infrared channel. Details in `ir/PROTOCOL.md`.

## Status

| | |
|---|---|
| Frame detection and cropping | works (tested on one 6-frame colour negative strip) |
| 4000 dpi 14-bit RGB + infrared | works: ~85 s, 141 MB RGB + 47 MB IR per full slot |
| 400 dpi preview | ~20-25 s per slot |
| Dust removal from the infrared | not yet |

If every slot's preview reports "no gap found", the feeder has lost track of where the
strip is (seen once after a scan started while the strip was still moving): take the
strip out and insert it again.

## License

MIT. `ir/` is our own implementation of the scanner protocol; no SANE code is used.

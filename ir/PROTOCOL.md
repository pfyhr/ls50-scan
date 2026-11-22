# LS-50 ED USB protocol notes

Written from observation of the scanner and from reading the SANE coolscan2/coolscan3
sources for protocol facts; no SANE code is used here.

## Transport

One vendor-specific interface with two bulk endpoints (0x01 out, 0x82 in). Each command
is a SCSI-style command block sent over USB:

1. Write the command block (6 or 10 bytes) to 0x01.
2. Write the single byte `D0` and read one byte back: the phase. `01` = no data,
   go to status; `02` = the scanner wants the data-out bytes now; `03` = the scanner
   will send data-in bytes now.
3. Move the data, if any.
4. Read 8 status bytes. Byte 1 (low nibble) is the sense key, bytes 2 and 3 are
   ASC/ASCQ, byte 4 extra info. Key 0 = OK.

If the scanner rejects a command that carries data it answers phase `01` and reports
the error in the status, so check the sense of every command.

## Commands used

| Bytes | Purpose |
|---|---|
| `00 ×6` | Test unit ready. `02-04-xx` = busy (poll every 0.5 s), `02-3A-00` = no film |
| `12 00 00 00 24 00` | Standard inquiry (vendor, product, firmware) |
| `12 01 C1 00 len 00` | Scanner parameter page: max/min dpi (bytes 20-23), scan area width (36-39) and height (58-61) in 1/max-dpi units, number of frames the strip adapter sensed (75), focus range (76-79), bits per sample (82) |
| `15 10 00 00 14 00` + 20 bytes | Mode select: base unit for offsets, set to 4000 dpi |
| `2A 00 88 00 00 03 len3 00` + data | Frame boundaries: one start/end box per holder slot; slots are 1.5 in (6001 units) apart |
| `2A 00 03 00 ch 01 len3 00` + data | Tone curve for one channel (identity, 2^14 entries × 2 bytes) |
| `24 00 00 00 00 00 00 00 3A 80` + 58 bytes | Set window for one channel (below) |
| `25 01 00 00 00 ch 00 00 3A 00` | Get window (works for 1-3; the LS-50 rejects 9 with 05-24-00) |
| `E1 00 C1 00 00 00 00 00 0D 00` | Read focus position (bytes 1-4 of 13) |
| `E0 00 A0 00 00 00 00 00 09 00` + `00 x4 y4` | Autofocus at a point, then `C1` |
| `E0 00 C1 00 00 00 00 00 09 00` + `00 focus4 00×4` | Set focus, then `C1` |
| `C1 00 00 00 00 00` | Execute the preceding E0 operation |
| `E0 00 D1 …` (load) | Refused on the SA-21 (05-24-00) |
| `1B 00 00 00 n 00` + window ids | Start scan of the listed windows (`01 02 03` or `01 02 03 09`) |
| `28 00 87 00 00 00 00 00 len 00` | Read scanner status page; bytes 19-20 = padding bytes per line |
| `28 00 00 00 00 00 len3 00` | Read image data |

Channel (window) ids: 1 red, 2 green, 3 blue, 9 infrared.

## Window descriptor

8-byte header (6 zero bytes, then descriptor length `00 32`), then 50 bytes:

| Offset | Value |
|---|---|
| 0 | channel id |
| 2-5 | x and y resolution (dpi, 16 bit each) |
| 6-21 | x offset, y offset, width, height (32 bit each, 1/4000 in) |
| 25 | `05` |
| 26 | bits per sample (8 or 14) |
| 40 | multi-sample count − 1 in the high nibble |
| 41 | `80` + 1 for positive / 0 for negative |
| 42 | `01` normal scan (`20` auto-exposure, `40` auto-exposure + white balance) |
| 43-45 | `02 02 FF` (single sample, line-interleaved channels) |
| 46-49 | exposure in 10 ns units (1.2 ms = 120000) |

**The infrared window is only accepted with `80` in the last byte of the set-window
command block.** With `00` the LS-50 accepts the R/G/B windows but rejects window 9 with
sense 05-24-00 (invalid field in command block), whatever the exposure. That is why
SANE's coolscan2 fails (see below).

## Strip positioning (SA-21)

The strip adapter moves the film to a slot when a focus operation (set focus or
autofocus, then execute) runs after the boundaries and before the scan. Start a scan
without one and the film is still moving during the scan: the image comes out doubled
or shifted. Once this left the feeder's idea of the strip position off by exactly 1500
units (9.5 mm) for every slot, also as seen by SANE, until the strip was reinserted.
The driver therefore always sends a focus operation before scanning.

## Data

After `1B` with four windows the scanner answers sense `09-80-06-01` the first time:
read status page 0x87 to learn the padding, then send `1B` again. Wait until test unit
ready is clear, then read. Each line arrives as R, G, B, IR planes of `width` 16-bit
big-endian samples (14-bit values), followed by the padding (304 bytes at 1000 dpi:
4×986×2 + 304 = 8192). Reading many lines per `28` command works (about 1 MB at a time).

## Why SANE fails

- **coolscan2**: sends set window with `00` in the control byte for the LS-50, so window
  9 is refused (05-24-00); `1B` with four windows then fails with 05-2C-02 (invalid
  window combination). Its USB command path ignores the sense status, so the scan
  "starts" and the first read gets no data (05-2C-00, command sequence error).
- **coolscan3**: uses the right control byte, but its infrared option is compiled in
  only when `SANE_FRAME_RGBI` is defined, which standard `sane.h` does not, so the
  option is always inactive. scanimage could not save an RGBI frame anyway.

#!/usr/bin/env python3
"""Minimal user-space driver for the Nikon Coolscan LS-50 ED over USB, written to get
the infrared channel that SANE's coolscan2/coolscan3 backends do not deliver.

    ls50.py info                                  # inquiry: resolution, frames, focus range
    ls50.py scan --frame 3 --dpi 1000 --ir -o out/f3   # out/f3_rgb.pnm + out/f3_ir.pnm

Output is 16-bit PNM (P6 RGB, P5 infrared), raw (not inverted), 14-bit data scaled to
16 bits. See PROTOCOL.md for the command set. Needs pyusb and Homebrew's libusb; quit
VueScan first (it holds the USB connection).
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import usb.backend.libusb1
import usb.core
import usb.util

VENDOR, PRODUCT = 0x04B0, 0x4001
EP_OUT, EP_IN = 0x01, 0x82
PHASE_STATUS, PHASE_OUT, PHASE_IN = 0x01, 0x02, 0x03
CHANNELS = {"r": 1, "g": 2, "b": 3, "ir": 9}       # window identifiers
VERBOSE = bool(os.environ.get("LS50_DEBUG"))


def log(msg: str) -> None:
    if VERBOSE:
        print(f"[ls50] {msg}", file=sys.stderr)


class ScannerError(RuntimeError):
    pass


@dataclass
class Sense:
    key: int
    asc: int
    ascq: int
    info: int = 0

    @property
    def ok(self) -> bool:
        return self.key == 0

    def __str__(self) -> str:
        return f"{self.key:02x}-{self.asc:02x}-{self.ascq:02x}-{self.info:02x}"


def be(n: int, width: int) -> bytes:
    return n.to_bytes(width, "big")


class LS50:
    def __init__(self, timeout_ms: int = 30000):
        backend = usb.backend.libusb1.get_backend(
            find_library=lambda _: "/opt/homebrew/lib/libusb-1.0.dylib")
        self.dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT, backend=backend)
        if self.dev is None:
            raise ScannerError("LS-50 not found on USB (is it on, and VueScan closed?)")
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass                                    # already configured
        usb.util.claim_interface(self.dev, 0)
        self.timeout = timeout_ms
        self.info: dict = {}

    def close(self) -> None:
        usb.util.release_interface(self.dev, 0)
        usb.util.dispose_resources(self.dev)

    # -- transport ---------------------------------------------------------------
    def command(self, cdb: bytes, data_out: bytes = b"", n_in: int = 0) -> tuple[bytes, Sense]:
        """Send one command block, move its data (if the scanner asks for it) and
        collect the 8-byte status. Returns (data received, sense)."""
        self.dev.write(EP_OUT, cdb, self.timeout)
        self.dev.write(EP_OUT, b"\xd0", self.timeout)
        phase = bytes(self.dev.read(EP_IN, 1, self.timeout))[0]
        data = b""
        if phase == PHASE_OUT:
            if not data_out:
                raise ScannerError(f"cmd {cdb[0]:02x}: scanner wants data, none to send")
            self.dev.write(EP_OUT, data_out, self.timeout)
        elif phase == PHASE_IN:
            if not n_in:
                raise ScannerError(f"cmd {cdb[0]:02x}: scanner sends data, none expected")
            chunks, got = [], 0
            while got < n_in:
                c = bytes(self.dev.read(EP_IN, n_in - got, self.timeout))
                if not c:
                    break
                chunks.append(c)
                got += len(c)
            data = b"".join(chunks)
        elif phase != PHASE_STATUS:
            raise ScannerError(f"cmd {cdb[0]:02x}: unknown phase 0x{phase:02x}")
        st = bytes(self.dev.read(EP_IN, 8, self.timeout))
        sense = Sense(st[1] & 0x0F, st[2], st[3], st[4])
        log(f"cmd {cdb.hex(' ')} out={len(data_out)} in={len(data)}/{n_in} "
            f"phase={phase} sense={sense}")
        if (data_out or n_in) and phase == PHASE_STATUS:
            log(f"  (no data phase: the scanner refused the command, sense {sense})")
        return data, sense

    def checked(self, what: str, cdb: bytes, data_out: bytes = b"", n_in: int = 0) -> bytes:
        data, sense = self.command(cdb, data_out, n_in)
        if not sense.ok:
            raise ScannerError(f"{what} rejected, sense {sense}")
        return data

    def wait_ready(self, allow_no_film: bool = False, timeout_s: float = 120) -> Sense:
        """Test Unit Ready until the scanner is idle (02-04-xx means it is busy)."""
        t0 = time.monotonic()
        while True:
            _, s = self.command(bytes(6))
            if s.ok or (allow_no_film and (s.key, s.asc) == (2, 0x3A)):
                return s
            if (s.key, s.asc) == (2, 0x04) or s.key == 6:       # busy / unit attention
                if time.monotonic() - t0 > timeout_s:
                    raise ScannerError(f"scanner still busy after {timeout_s:.0f} s ({s})")
                time.sleep(0.5)
                continue
            if (s.key, s.asc) == (2, 0x3A):
                raise ScannerError("no film loaded")
            raise ScannerError(f"scanner not ready, sense {s}")

    # -- queries -------------------------------------------------------------------
    def inquiry(self) -> dict:
        self.wait_ready(allow_no_film=True)
        std = self.checked("inquiry", bytes([0x12, 0, 0, 0, 36, 0]), n_in=36)
        vendor, product = std[8:16].decode().strip(), std[16:32].decode().strip()
        if not product.startswith("LS-50"):
            raise ScannerError(f"unexpected device {vendor} {product}")
        head = self.checked("inquiry c1", bytes([0x12, 1, 0xC1, 0, 4, 0]), n_in=4)
        n = head[3] + 4
        p = self.checked("inquiry c1", bytes([0x12, 1, 0xC1, 0, n, 0]), n_in=n)
        u16 = lambda i: int.from_bytes(p[i:i + 2], "big")
        u32 = lambda i: int.from_bytes(p[i:i + 4], "big")
        self.info = {
            "product": f"{vendor} {product} {std[32:36].decode()}",
            "dpi_max": u16(20), "dpi_min": u16(22), "width": u32(36),
            "dpi_max_y": u16(42), "height": u32(58),
            "frames": p[75], "focus_range": (u16(76), u16(78)), "bits": p[82],
        }
        return self.info

    # -- scan setup ----------------------------------------------------------------
    def set_unit(self) -> None:
        """Mode select: measure offsets in 1/dpi_max inch."""
        params = bytes.fromhex("00 00 00 08 00 00 00 00 00 00 00 01 03 06 00 00") \
            + be(self.info["dpi_max"], 2) + b"\0\0"
        self.wait_ready()
        self.checked("mode select", bytes([0x15, 0x10, 0, 0, len(params), 0]), params)

    def frame_pitch(self) -> int:
        return int(self.info["dpi_max_y"] * 1.5 + 1)          # 1.5 in per holder slot

    def set_boundaries(self) -> None:
        """Tell the scanner where the holder's slots are (one window per frame)."""
        n, pitch, w = self.info["frames"], self.frame_pitch(), self.info["width"]
        body = be(4 + 16 * n, 2) + bytes([n, n])
        for i in range(n):
            body += be(pitch * i, 4) + be(0, 4) + be(pitch * (i + 1) - 1, 4) + be(w - 1, 4)
        self.wait_ready()
        self.checked("set boundary", bytes([0x2A, 0, 0x88, 0, 0, 3]) + be(len(body), 3) + b"\0",
                     body)

    def send_identity_lut(self, channel: int) -> None:
        n = 1 << self.info["bits"]
        body = b"".join(be(v, 2) for v in range(n))
        self.checked(f"lut {channel}", bytes([0x2A, 0, 3, 0, channel, 1]) + be(len(body), 3)
                     + b"\0", body)

    def set_window(self, channel: int, dpi: int, x: int, y: int, w: int, h: int,
                   bits: int, exposure: int, positive: bool = True) -> Sense:
        """Window descriptor for one channel (offsets/sizes in 1/dpi_max inch,
        exposure in units of 10 ns)."""
        d = bytes([channel, 0]) + be(dpi, 2) + be(dpi, 2) + be(x, 4) + be(y, 4) \
            + be(w, 4) + be(h, 4) + bytes([0, 0, 0, 0x05, bits]) + bytes(13) \
            + bytes([0x00, 0x80 | int(positive), 0x01, 0x02, 0x02, 0xFF]) + be(exposure, 4)
        assert len(d) == 50
        data = bytes(6) + be(len(d), 2) + d
        self.wait_ready()
        _, s = self.command(bytes([0x24, 0, 0, 0, 0, 0, 0]) + be(len(data), 2) + b"\x80", data)
        return s

    def get_window(self, channel: int) -> bytes:
        return self.checked("get window", bytes([0x25, 1, 0, 0, 0, channel, 0, 0, 0x3A, 0]),
                            n_in=0x3A)

    def execute(self) -> None:
        self.wait_ready()
        self.checked("execute", bytes([0xC1, 0, 0, 0, 0, 0]))

    def read_focus(self) -> int:
        self.wait_ready()
        r = self.checked("read focus", bytes([0xE1, 0, 0xC1, 0, 0, 0, 0, 0, 13, 0]), n_in=13)
        return int.from_bytes(r[1:5], "big")

    def autofocus(self, x: int, y: int) -> int:
        self.wait_ready()
        self.checked("autofocus", bytes([0xE0, 0, 0xA0, 0, 0, 0, 0, 0, 9, 0]),
                     b"\0" + be(x, 4) + be(y, 4))
        self.execute()
        return self.read_focus()

    def set_focus(self, focus: int) -> None:
        self.wait_ready()
        self.checked("set focus", bytes([0xE0, 0, 0xC1, 0, 0, 0, 0, 0, 9, 0]),
                     b"\0" + be(focus, 4) + bytes(4))
        self.execute()

    # -- scanning --------------------------------------------------------------------
    def scan(self, frame: int, dpi: int, ir: bool, exposure_rgb=(120000, 120000, 100000),
             exposure_ir: int | None = None, focus: bool = True, bits: int = 14,
             top: int = 0, height: int | None = None):
        """Scan one holder slot. Returns (width, height, {channel: list of rows}),
        each row a bytes object of 16-bit big-endian samples."""
        info = self.info or self.inquiry()
        if not 1 <= frame <= max(info["frames"], 1):
            raise ScannerError(f"frame {frame} not sensed (holder reports {info['frames']})")
        pitch = info["dpi_max"] // dpi
        dpi = info["dpi_max"] // pitch
        full_h = info["height"] - top if height is None else height
        lw, lh = info["width"] // pitch, full_h // pitch
        x0, y0 = 0, (frame - 1) * self.frame_pitch() + top
        chans = ["r", "g", "b"] + (["ir"] if ir else [])
        if exposure_ir is None:
            exposure_ir = exposure_rgb[0]

        self.wait_ready()
        self.set_unit()
        self.set_boundaries()
        if focus:
            f = self.autofocus(x0 + lw * pitch // 2, y0 + lh * pitch // 2)
            log(f"autofocus -> {f}")
        self.wait_ready()
        for c in chans:
            self.send_identity_lut(CHANNELS[c])
        for c, e in zip(chans, list(exposure_rgb) + [exposure_ir]):
            s = self.set_window(CHANNELS[c], dpi, x0, y0, lw * pitch, lh * pitch, bits, e)
            if not s.ok:
                raise ScannerError(f"set window for {c} rejected, sense {s}")
        ids = bytes(CHANNELS[c] for c in chans)
        self.wait_ready()
        _, s = self.command(bytes([0x1B, 0, 0, 0, len(ids), 0]), ids)
        padding = 0
        if (s.key, s.asc, s.ascq) == (9, 0x80, 0x06):
            padding = self.block_padding()
            _, s = self.command(bytes([0x1B, 0, 0, 0, len(ids), 0]), ids)
        if not s.ok:
            raise ScannerError(f"scan command rejected, sense {s}")

        line = len(chans) * lw * 2
        rows: dict[str, list[bytes]] = {c: [] for c in chans}
        lines_per_read = int(os.environ.get("LS50_LINES_PER_READ", 0)) or \
            max(1, (1 << 20) // (line + padding))
        y = 0
        while y < lh:
            n = min(lines_per_read, lh - y)
            want = n * (line + padding)
            self.wait_ready()
            data, s = self.command(bytes([0x28, 0, 0, 0, 0, 0]) + be(want, 3) + b"\0", n_in=want)
            if not s.ok or len(data) != want:
                raise ScannerError(f"read at line {y} failed: got {len(data)}/{want}, sense {s}")
            for k in range(n):
                off = k * (line + padding)
                for i, c in enumerate(chans):
                    rows[c].append(data[off + i * lw * 2: off + (i + 1) * lw * 2])
            y += n
            print(f"\r  line {y}/{lh}", end="", file=sys.stderr, flush=True)
        print(file=sys.stderr)
        return lw, lh, rows

    def block_padding(self) -> int:
        """After sense 09-80-06 to a scan command, the scanner states how many padding
        bytes follow each line (read data type 0x87)."""
        head = self.checked("read 87", bytes([0x28, 0, 0x87, 0, 0, 0, 0, 0, 6, 0]), n_in=6)
        n = head[5] + 6
        p = self.checked("read 87", bytes([0x28, 0, 0x87, 0, 0, 0, 0, 0, n, 0]), n_in=n)
        pad = int.from_bytes(p[19:21], "big")
        log(f"block padding {pad} (page {p.hex(' ')})")
        return pad


def write_pnm(path: Path, w: int, h: int, planes: list[list[bytes]], shift: int) -> None:
    """16-bit P5 (one plane) or P6 (three planes, interleaved per pixel)."""
    import array
    out = array.array("H")
    for y in range(h):
        rows = []
        for p in planes:
            a = array.array("H", p[y])
            if sys.byteorder == "little":
                a.byteswap()
            rows.append(a)
        if len(rows) == 1:
            line = rows[0]
        else:
            line = array.array("H", bytes(2 * w * len(rows)))
            for i, a in enumerate(rows):
                line[i::len(rows)] = a
        out.extend(line)
    if shift:
        out = array.array("H", (min(v << shift, 0xFFFF) for v in out))
    if sys.byteorder == "little":
        out.byteswap()
    with open(path, "wb") as f:
        f.write(f"P{'5' if len(planes) == 1 else '6'}\n{w} {h}\n65535\n".encode())
        f.write(out.tobytes())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    sc = sub.add_parser("scan")
    sc.add_argument("--frame", type=int, default=1)
    sc.add_argument("--dpi", type=int, default=1000)
    sc.add_argument("--ir", action="store_true", help="also read the infrared channel")
    sc.add_argument("--exposure", type=float, default=1.0,
                    help="scale the default RGB exposure (1.2/1.2/1.0 ms)")
    sc.add_argument("--ir-exposure", type=float, default=None,
                    help="infrared exposure in ms (default: same as red)")
    sc.add_argument("--no-focus", action="store_true")
    sc.add_argument("-o", "--out", type=Path, required=True, help="output prefix")
    args = ap.parse_args()

    s = LS50()
    try:
        info = s.inquiry()
        if args.cmd == "info":
            for k, v in info.items():
                print(f"{k:12} {v}")
            return
        e = args.exposure
        exp = (round(120000 * e), round(120000 * e), round(100000 * e))
        exp_ir = None if args.ir_exposure is None else round(args.ir_exposure * 1e5)
        w, h, rows = s.scan(args.frame, args.dpi, args.ir, exp, exp_ir, not args.no_focus)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        shift = 16 - 14
        rgb = Path(f"{args.out}_rgb.pnm")
        write_pnm(rgb, w, h, [rows["r"], rows["g"], rows["b"]], shift)
        print(rgb)
        if args.ir:
            irp = Path(f"{args.out}_ir.pnm")
            write_pnm(irp, w, h, [rows["ir"]], shift)
            print(irp)
    except ScannerError as e:
        sys.exit(f"ls50: {e}")
    finally:
        s.close()


if __name__ == "__main__":
    main()

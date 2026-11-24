#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Niek Ilmer <niek@beunie.nl>
"""Agfa Vision 35 film scanner tool for Linux (SCSI via SG_IO). Python 3 standard library only.

  scan calibrate              measure the film holder (true black) and a piece of blank film (white) and save
                              calibration.json in the current folder (one folder per roll works well)
  scan                        full scan -> next free N.tiff in the current folder, using ./calibration.json
  scan --out frame.tiff       same, explicit filename
  scan --mono [R|G|B]         black & white negative: one filter only (default R), about 2 min
  scan --preview              quick low-res greyscale preview (about 25 s) -> preview-N.tiff, no calibration needed
  scan --positive             8-bit positive for viewing (negative inverted + auto levels)

The output is flipped top-to-bottom by default (upright for film loaded the usual way); --no-flip, --flip-h and
--rotate adjust that.

How the scanner's signal works (measured, see docs/PROTOCOL.md): the scanner reports an inverted 8-bit signal s.
The film holder (no light) reads ~253 and clearer film reads lower, so light is linear in (s_black - s).  DN04 is
the same signal with about 2.04x the gain (it clips a bright channel); DN01 is the low-gain setting.  `calibrate`
measures black and blank film at the DN setting chosen for each channel, plus the lamp falloff across the film.

Default output (--output negpy): untagged 16-bit TIFF, linear transmittance relative to the blank film (blank film
= 0.75 of full scale, holder = 0), flat-fielded, NOT inverted, for converting negatives in NegPy or similar.  The
signal is encoded with density-linear (logarithmic) tables written into the scanner (WGMnT) and decoded exactly in
software, so shadows and highlights keep even density steps.

Protocol (decoded from a FotoLook 2.07 trace): ASCII commands + CRLF sent with WRITE(10), replies/image lines read
with READ(10), TEST UNIT READY polled between steps.  Only SCSI opcodes 12, 00, 03, 2A, 28 are ever sent, and
text commands are whitelisted (traced ones plus WGMnT tables, whose format was verified experimentally because the
trace logged those writes truncated).  No scanner firmware is ever written.
"""
__version__ = "0.1.0"
import argparse, ctypes, datetime, fcntl, glob, json, math, os, re, struct, sys, time, zlib

SG_IO = 0x2285
ALLOWED_OPS = {0x12, 0x00, 0x03, 0x2A, 0x28}
DXFER_NONE, DXFER_TO, DXFER_FROM = -1, -2, -3

# every text command that appears in the trace and that this tool may send
ALLOWED_TEXT = re.compile(
    r"^(APIN|OUT8|AF,R|MP|CP|GML8|IF15|INM|SPUP|NHD|AP50|AVOF|SDON|DN0[14]|BRT0|CNT0|SFT0|PSOF|PVOF|"
    r"F[RGB],R|GM[123]T8|MV0,R|MV345,R|"
    r"BXZ 0,0,4999,4999,13,13,V|BXZ 345,135,4757,3015,1,1,V)$")

IDENTITY = list(range(256))
PREVIEW_BXZ = "BXZ 0,0,4999,4999,13,13,V"
SCAN_BXZ = "BXZ 345,135,4757,3015,1,1,V"
PREVIEW_W, PREVIEW_LINES = 386, 385      # 385 samples + 1 constant pad byte (0x17)
SCAN_W, SCAN_LINES = 2882, 4413          # 2881 samples + 1 pad byte
# preview table (green, DN01): spread the signal range and saturate the holder (>=251) to 255 so it can be trimmed
PREVIEW_TABLE = [min(255, max(0, round(255 * (i - 150) / (251 - 150)))) for i in range(256)]
PREVIEW_TABLE[255] = 255

DN_GAIN = 2.04            # measured: DN04 has 2.04x the gain of DN01 (signal s = 255 - gain * light)
T_HI = 1.12               # legacy encoded ceiling (older calibrations/raw scans without a stored "t_hi")
T_HI_MAX = 1.3            # ceiling for new scans: room for the ~8% pixel noise of near-blank film
OUT_SCALE_NEW = 0.75      # new 16-bit scans: blank film -> 0.75 of full scale, so T_HI_MAX (1.3) -> 0.975
T_LO_MIN = 0.002          # density 2.7: absolute floor of the encoded range


OUT_SCALE = 0.85          # legacy output scale (raw scans made before "out_scale" was stored)
FILTERS = (("FR,R", "GM1T8"), ("FG,R", "GM2T8"), ("FB,R", "GM3T8"))
CAL_NAME = "calibration.json"


def t_lo(black, blank):
    """Encoded floor: about one raw level of light (denser film cannot be resolved), never above 0.02."""
    return min(0.02, max(T_LO_MIN, 1.0 / (black - blank)))


class ScsiError(Exception):
    pass


class SgIoHdr(ctypes.Structure):
    _fields_ = [
        ("interface_id", ctypes.c_int), ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte), ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort), ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p), ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p), ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint), ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p), ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte), ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte), ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort), ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint), ("info", ctypes.c_uint),
    ]


def find_scanner():
    """Return /dev/sgN of the Agfa Vision 35 LUN 0 (found via sysfs, no commands sent)."""
    for d in sorted(glob.glob("/sys/class/scsi_generic/sg*")):
        try:
            dev = os.path.join(d, "device")
            if open(dev + "/type").read().strip() != "6":
                continue
            vendor = open(dev + "/vendor").read().strip()
            model = open(dev + "/model").read().strip()
            hctl = os.path.basename(os.path.realpath(dev))
        except OSError:
            continue
        if vendor == "AGFA" and model == "VISION 35" and hctl.endswith(":0"):
            return "/dev/" + os.path.basename(d)
    return None


class Scanner:
    def __init__(self, dev, logpath):
        self.fd = os.open(dev, os.O_RDWR)
        self.log = open(logpath, "a", buffering=1)
        self.n = 0

    def cmd(self, cdb, data_out=None, in_len=0, timeout_ms=15000, quiet=False):
        cdb = bytes(cdb)
        if cdb[0] not in ALLOWED_OPS:
            raise ScsiError("opcode %02x not allowed" % cdb[0])
        self.n += 1
        sense = ctypes.create_string_buffer(32)
        cdbb = ctypes.create_string_buffer(cdb, len(cdb))
        if data_out is not None:
            buf = ctypes.create_string_buffer(bytes(data_out), len(data_out))
            direction, dlen = DXFER_TO, len(data_out)
        elif in_len:
            buf = ctypes.create_string_buffer(in_len)
            direction, dlen = DXFER_FROM, in_len
        else:
            buf = ctypes.create_string_buffer(1)
            direction, dlen = DXFER_NONE, 0
        h = SgIoHdr()
        h.interface_id = ord("S")
        h.dxfer_direction = direction
        h.cmd_len = len(cdb)
        h.mx_sb_len = 32
        h.dxfer_len = dlen
        h.dxferp = ctypes.addressof(buf)
        h.cmdp = ctypes.addressof(cdbb)
        h.sbp = ctypes.addressof(sense)
        h.timeout = timeout_ms
        fcntl.ioctl(self.fd, SG_IO, h)
        din = buf.raw[:dlen - h.resid] if direction == DXFER_FROM else b""
        sb = sense.raw[:h.sb_len_wr]
        if not quiet or h.status or (h.host_status and h.host_status != 1):
            line = "#%d %s | st=%02x host=%02x drv=%02x" % (
                self.n, " ".join("%02X" % b for b in cdb), h.status, h.host_status, h.driver_status)
            if data_out is not None:
                line += " | OUT %r" % (bytes(data_out)[:60],)
            if din:
                line += " | IN(%d) %s" % (len(din), " ".join("%02x" % b for b in din[:24]))
            if sb:
                line += " | SENSE %s" % sb.hex()
            self.log.write(line + "\n")
        return h.status, h.host_status, din, sb

    def _check(self, st, hs, sb):
        if hs or st:
            raise ScsiError("SCSI error status=%02x host=%02x sense=%s" % (st, hs, sb.hex()))

    def inquiry(self):
        st, hs, d, sb = self.cmd([0x12, 0, 0, 0, 36, 0], in_len=36)
        self._check(st, hs, sb)
        return d

    def tur(self):
        # a busy scanner does not answer selection -> DID_NO_CONNECT (host=01): that means "busy"
        st, hs, d, sb = self.cmd([0, 0, 0, 0, 0, 0], quiet=True)
        if hs:
            return False
        if st == 0:
            return True
        raise ScsiError("TEST UNIT READY CHECK CONDITION status=%02x sense=%s" % (st, sb.hex()))

    def wait_ready(self, timeout=60.0):
        t0 = time.time()
        while not self.tur():
            if time.time() - t0 > timeout:
                raise ScsiError("scanner busy for more than %.0f s" % timeout)
            time.sleep(0.05)

    def send(self, text):
        if not ALLOWED_TEXT.match(text):
            raise ScsiError("text command %r is not one of the traced commands" % text)
        data = text.encode("ascii") + b"\r\n"
        L = len(data)
        st, hs, d, sb = self.cmd([0x2A, 0, 0, 0, 0, 0, 0, L >> 8, L & 0xFF, 0], data_out=data)
        self._check(st, hs, sb)
        self.wait_ready()

    def send_table(self, n, values):
        """Write table n (1..3): 'WGMnT,v0,...,v255' + CRLF, 256 values 0..255 (format verified experimentally)."""
        if n not in (1, 2, 3) or len(values) != 256 or not all(isinstance(v, int) and 0 <= v <= 255 for v in values):
            raise ScsiError("bad WGM table")
        data = ("WGM%dT," % n + ",".join(str(v) for v in values)).encode("ascii") + b"\r\n"
        L = len(data)
        st, hs, d, sb = self.cmd([0x2A, 0, 0, 0, 0, 0, 0, L >> 8, L & 0xFF, 0], data_out=data)
        self._check(st, hs, sb)
        self.wait_ready()

    def read_status(self):
        st, hs, d, sb = self.cmd([0x28, 0, 1, 0, 0, 0, 0, 0, 1, 0], in_len=1)
        self._check(st, hs, sb)
        self.wait_ready()
        return d[0]

    def read_block(self):
        st, hs, h, sb = self.cmd([0x28, 0, 0, 0, 0, 0, 0, 0, 4, 0], in_len=4)
        self._check(st, hs, sb)
        n = (h[1] << 8) | h[2]
        st, hs, p, sb = self.cmd([0x28, 0, 0, 0, 0, 0, 0, h[1], h[2], 0], in_len=n)
        self._check(st, hs, sb)
        return bytes(h), p

    def _cmds(self, names):
        for c in names:
            self.send(c)

    def _status06(self, what):
        v = self.read_status()
        if v != 0x06:
            raise ScsiError("%s: expected status 06, got %02x" % (what, v))

    def read_lines(self, width, expect_lines, progress=None):
        lines, end = [], None
        for i in range(expect_lines + 50):
            h, p = self.read_block()
            if h[0] == 2 and len(p) == width and h[3] in (0x40, 0x42):
                lines.append(p)
            else:
                end = (h, p)
                break
            self.wait_ready()
            if progress and i % 200 == 0:
                progress(i, expect_lines)
        if end is None or end[1] != b"\x03":
            raise ScsiError("scan did not end with the expected end marker (got %d lines)" % len(lines))
        if len(lines) != expect_lines:
            raise ScsiError("expected %d lines, got %d" % (expect_lines, len(lines)))
        return lines

    # ---- sequences (as in the trace, plus WGM tables) ----
    def preview(self, progress=None):
        self._cmds(["APIN", "OUT8", "AF,R"]); self._status06("AF,R")
        self._cmds(["MP", "GML8", "IF15", "SPUP", "NHD", "AP50", "AVOF", "SDON", "DN01", "BRT0",
                    "CNT0", "SFT0"])
        self.send_table(1, PREVIEW_TABLE)
        self.send("FG,R"); self._status06("FG,R")
        self._cmds(["GM1T8", "MV0,R"]); self._status06("MV0,R")
        self.send(PREVIEW_BXZ)
        lines = self.read_lines(PREVIEW_W, PREVIEW_LINES, progress)
        self._cmds(["PSOF", "PVOF"])
        return lines

    def wide_scan(self, channels, dns):
        """Whole-frame step-13 pass per channel with neutral tables (used by calibration)."""
        self._cmds(["APIN", "OUT8", "AF,R"]); self._status06("AF,R")
        self._cmds(["MP", "GML8", "IF15", "SPUP", "NHD", "AP50", "AVOF", "SDON", dns[0], "BRT0", "CNT0", "SFT0"])
        for i in channels:
            self.send_table(i + 1, IDENTITY)
        out = []
        for k, i in enumerate(channels):
            f, g = FILTERS[i]
            self.send(dns[k])                      # DN is only latched by the NEXT filter command
            self.send(f); self._status06(f)
            self.send(g); self.send("MV0,R"); self._status06("MV0,R")
            self.send(PREVIEW_BXZ)
            out.append(self.read_lines(PREVIEW_W, PREVIEW_LINES))
        self._cmds(["PSOF", "PVOF"])
        return out

    def full_scan(self, channels, dns, tables, progress=None):
        self._cmds(["PSOF", "PVOF", "IF15", "INM", "APIN", "OUT8", "AF,R"]); self._status06("AF,R")
        self._cmds(["CP", "GML8", "IF15", "SPUP", "NHD", "AP50", "AVOF", "SDON", dns[0], "BRT0", "CNT0", "SFT0"])
        for k, i in enumerate(channels):
            self.send_table(i + 1, tables[k])
        planes = []
        for k, i in enumerate(channels):
            f, g = FILTERS[i]
            self.send(dns[k])                      # DN is only latched by the NEXT filter command
            self.send(f); self._status06(f)
            self.send(g); self.send("MV345,R"); self._status06("MV345,R")
            self.send(SCAN_BXZ)
            name = "RGB"[i]
            planes.append(self.read_lines(SCAN_W, SCAN_LINES,
                                          (lambda a, b, n=name: progress and progress(a, b, n))))
        self._cmds(["PSOF", "PVOF", "IF15", "INM"])
        return planes


# ---------------- calibration ----------------

def wide_arrays(lines_per_channel):
    """-> S[c][y][x] (x = line index along the film, y = sample index across it), pad byte dropped."""
    out = []
    for lines in lines_per_channel:
        n = len(lines)
        out.append([[lines[x][y] for x in range(n)] for y in range(len(lines[0]) - 1)])
    return out


def _median(v):
    v = sorted(v)
    return v[len(v) // 2] if len(v) % 2 else 0.5 * (v[len(v) // 2 - 1] + v[len(v) // 2])


def _whole_film_blank(S, holder):
    """If the whole film area is uniform apart from a smooth gradient (plane fit residual ~ noise), it is blank film:
    return the region dict (without holder points), else None."""
    nc, H, Wd = len(S), len(S[0]), len(S[0][0])
    gch = 1 if nc > 1 else 0
    bch = 2 if nc > 2 else gch
    colfrac = [sum(1 for y in range(H) if not holder[y][x]) / float(H) for x in range(Wd)]
    xs_f = [x for x in range(Wd) if colfrac[x] > 0.3]
    rowfrac = [sum(1 for x in range(Wd) if not holder[y][x]) / float(Wd) for y in range(H)]
    ys_f = [y for y in range(H) if rowfrac[y] > 0.3]
    if len(xs_f) < 120 or len(ys_f) < 120:
        return None
    x0, x1, y0, y1 = xs_f[0] + 8, xs_f[-1] - 8, ys_f[0] + 8, ys_f[-1] - 8
    if x1 - x0 < 100 or y1 - y0 < 100:
        return None
    for c in {gch, bch}:
        pts = []
        for by in range(y0, y1 - 10, 10):
            for bx in range(x0, x1 - 10, 10):
                v = sum(S[c][yy][xx] for yy in range(by, by + 10) for xx in range(bx, bx + 10)) / 100.0
                pts.append((bx, by, v))
        n = len(pts)
        mx = sum(p[0] for p in pts) / n; my = sum(p[1] for p in pts) / n; mv = sum(p[2] for p in pts) / n
        sxx = sum((p[0] - mx) ** 2 for p in pts); syy = sum((p[1] - my) ** 2 for p in pts)
        sxv = sum((p[0] - mx) * (p[2] - mv) for p in pts); syv = sum((p[1] - my) * (p[2] - mv) for p in pts)
        ax, ay = sxv / sxx, syv / syy
        res = [p[2] - (mv + ax * (p[0] - mx) + ay * (p[1] - my)) for p in pts]
        sd = (sum(r * r for r in res) / n) ** 0.5
        if sd > max(3.0, 0.03 * (253.0 - mv)):
            return None
    xs = list(range(x0, x1))
    bpts = [(y, x) for x in xs[::2] for y in range(y0, y1)]
    return {"blank_pts": bpts, "blank_cols": (x0, x1), "blank_xs": xs, "blank_rows": (y0, y1), "whole_film": True}


def detect_regions(S):
    """Find the film holder and a blank film strip in a wide scan taken with neutral tables at one DN.
    Returns dict with sample point lists; raises ValueError if the holder or blank film is not found."""
    nc, H, Wd = len(S), len(S[0]), len(S[0][0])
    hm = [[all(S[c][y][x] >= 252 for c in range(nc)) for x in range(Wd)] for y in range(H)]
    seen = [[False] * Wd for _ in range(H)]
    stack = []
    for y in range(H):
        for x in (0, Wd - 1):
            if hm[y][x] and not seen[y][x]:
                seen[y][x] = True; stack.append((y, x))
    for x in range(Wd):
        for y in (0, H - 1):
            if hm[y][x] and not seen[y][x]:
                seen[y][x] = True; stack.append((y, x))
    while stack:                                  # holder = near-black area connected to the border
        y, x = stack.pop()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            yy, xx = y + dy, x + dx
            if 0 <= yy < H and 0 <= xx < Wd and hm[yy][xx] and not seen[yy][xx]:
                seen[yy][xx] = True; stack.append((yy, xx))
    holder = seen
    n_holder = sum(sum(r) for r in holder)
    if n_holder < 0.02 * H * Wd:
        raise ValueError("film holder (black) not found in the wide scan: is the strip inside the holder?")
    hpts = []
    for y in range(3, H - 3, 2):
        for x in range(3, Wd - 3, 2):
            if holder[y][x] and all(holder[yy][xx] for yy in range(y - 3, y + 4, 3) for xx in range(x - 3, x + 4, 3)):
                hpts.append((y, x))
    if len(hpts) < 200:
        raise ValueError("film holder area too small to measure")
    # case 1: the whole film area is blank (nothing but smooth lamp falloff): use all of it
    fb = _whole_film_blank(S, holder)
    if fb:
        fb["holder_pts"] = hpts
        return fb
    # blank film: columns (lines) with a long stretch that is constant along the strip, and the most light
    gch = 1 if nc > 1 else 0
    bch = 2 if nc > 2 else gch
    BLACK = 253.0

    def tol_step(level):                           # allowed change between neighbouring 10-sample means
        return max(3.0, 0.06 * (BLACK - level))

    def tol_drift(level):                          # allowed total drift (lamp falloff across the film width)
        return max(6.0, 0.35 * (BLACK - level))
    cols = []
    for x in range(Wd):
        rows = [y for y in range(H) if not holder[y][x]]
        if len(rows) < 60:
            cols.append(None); continue
        y0, y1 = rows[0] + 6, rows[-1] - 6
        if y1 - y0 < 60:
            cols.append(None); continue
        segs = []                                  # (y_start, mean_g, mean_b) per 10-sample segment
        for ys in range(y0, y1 - 10, 10):
            segs.append((ys, sum(S[gch][y][x] for y in range(ys, ys + 10)) / 10.0,
                         sum(S[bch][y][x] for y in range(ys, ys + 10)) / 10.0))
        best = None                                # longest smooth run of segments
        i = 0
        while i < len(segs):
            j = i
            while (j + 1 < len(segs) and abs(segs[j + 1][1] - segs[j][1]) <= tol_step(segs[j][1])
                   and abs(segs[j + 1][1] - segs[i][1]) <= tol_drift(segs[i][1])
                   and abs(segs[j + 1][2] - segs[j][2]) <= tol_step(segs[j][2])):
                j += 1
            if best is None or j - i > best[1] - best[0]:
                best = (i, j)
            i = j + 1
        i, j = best
        if j - i + 1 >= 8:                         # at least ~80 samples long
            cols.append((segs[i][0], segs[j][0] + 10, sum(sg[1] for sg in segs[i:j + 1]) / (j - i + 1)))
        else:
            cols.append(None)
    # group neighbouring candidate columns with similar level into runs
    runs, x = [], 0
    while x < Wd:
        if cols[x]:
            x0 = x
            while x + 1 < Wd and cols[x + 1] and abs(cols[x + 1][2] - cols[x][2]) <= max(3.0, 0.1 * (BLACK - cols[x][2])):
                x += 1
            runs.append((x0, x)); x += 1
        else:
            x += 1
    runs = [(a, b) for a, b in runs if b - a + 1 >= 8]
    if not runs:
        raise ValueError("no blank film found: show a piece of unexposed film (e.g. the gap between frames) "
                         "as a band at least ~10 columns wide across most of the strip")
    # candidate bands, clearest first; a real gap between frames has SHARP edges on both sides (or borders the holder)
    def colmean(x, ya_, yb_):
        x = min(max(x, 0), Wd - 1)
        v = [S[gch][y][x] for y in range(ya_, yb_)]
        return sum(v) / len(v)
    cand_runs = []
    for a, b in runs:
        lvl = sum(cols[x][2] for x in range(a + 2, b - 1)) / max(1, (b - 1) - (a + 2))
        cand_runs.append((lvl, a, b))
    cand_runs.sort()
    chosen, why = None, []
    for lvl, a, b in cand_runs:
        ya_ = max(cols[x][0] for x in range(a + 2, b - 1)); yb_ = min(cols[x][1] for x in range(a + 2, b - 1))
        if yb_ - ya_ < 30:
            ya_ = min(cols[x][0] for x in range(a + 2, b - 1)); yb_ = max(cols[x][1] for x in range(a + 2, b - 1))
        need = max(8.0, 0.08 * (BLACK - lvl))
        left, right = colmean(a - 4, ya_, yb_), colmean(b + 4, ya_, yb_)
        if left - lvl >= need and right - lvl >= need:
            chosen = (lvl, a, b); break
        why.append("columns %d-%d (level %.0f): neighbours only %.0f / %.0f levels lighter-coded, need %.0f" % (
            a, b, lvl, left - lvl, right - lvl, need))
    if chosen is None:
        raise ValueError("no blank film found: the uniform areas in the wide scan have no sharp edges on both sides "
                         "(%s). Position the strip so that the clear gap between two frames, or the blank leader, "
                         "is visible as a band at least ~10 columns wide across the strip" % "; ".join(why))
    lvl, a, b = chosen
    # safeguard: blank film must be among the clearest areas of the frame, else this is probably flat picture
    allg = sorted(S[gch][y][x] for y in range(0, H, 3) for x in range(0, Wd, 3) if not holder[y][x])
    if lvl > allg[int(len(allg) * 0.25)] + 3:
        raise ValueError("the uniform band found (level %.0f) is not among the clearest areas of the frame "
                         "(25th percentile %.0f): probably no blank film is visible" % (lvl, allg[int(len(allg) * 0.25)]))
    xs = list(range(a + 2, b - 1))
    # row extent of the band: walk outwards from its clearest row until the (smoothed) signal jumps
    first = min(y for y in range(H) if any(not holder[y][x] for x in xs))
    last = max(y for y in range(H) if any(not holder[y][x] for x in xs))
    rm = {}
    for y in range(first, last + 1):
        vals = [S[gch][y][x] for x in xs if not holder[y][x]]
        if vals:
            rm[y] = sum(vals) / len(vals)

    def sm(y):
        v = [rm[k] for k in range(y - 2, y + 3) if k in rm]
        return sum(v) / len(v) if v else None
    ys_ok = [y for y in rm if sm(y) is not None]
    center = min(ys_ok, key=lambda y: sm(y))
    up = down = center
    while up - 1 in rm and sm(up - 1) is not None and abs(sm(up - 1) - sm(up)) <= max(5.0, 0.05 * (BLACK - sm(up))):
        up -= 1
    while down + 1 in rm and sm(down + 1) is not None and abs(sm(down + 1) - sm(down)) <= max(5.0, 0.05 * (BLACK - sm(down))):
        down += 1
    ya, yb = up + 3, down - 3
    if yb - ya < 60:
        raise ValueError("blank film band is too short along the strip to measure")
    bpts = [(y, x) for x in xs for y in range(ya, yb) if not holder[y][x]]
    return {"holder_pts": hpts, "blank_pts": bpts, "blank_cols": (a, b), "blank_xs": xs, "blank_rows": (ya, yb)}


def _robust_mean(v):
    """Mean of the samples within +-8 levels of the median: sub-level precision from thousands of 8-bit samples."""
    m = _median(v)
    keep = [x for x in v if abs(x - m) <= 8]
    return sum(keep) / len(keep)


def prof_level(p, k):
    """Blank-film level at preview row k (linear interpolation, clamped to the measured rows)."""
    v = p["vals"]
    t = min(max(k, p["k0"]), p["k1"]) - p["k0"]
    i = min(int(t), len(v) - 2)
    return v[i] + (v[i + 1] - v[i]) * (t - i) if len(v) > 1 else v[0]


def band_profile(S, reg, c, win=7):
    """Blank-film level along the film width: row means over the band columns, smoothed (moving average)."""
    ya, yb = reg["blank_rows"]
    xs = reg["blank_xs"]
    means = [_robust_mean([S[c][y][x] for x in xs]) for y in range(ya, yb)]
    vals = []
    for i in range(len(means)):
        seg = means[max(0, i - win):i + win + 1]
        vals.append(sum(seg) / len(seg))
    return {"k0": ya, "k1": yb - 1, "vals": vals}


def levels_at(S, reg):
    """Black (holder level), nominal blank level (the clearest row of the band) and the blank profile per channel."""
    black = [_robust_mean([S[c][y][x] for y, x in reg["holder_pts"]]) for c in range(len(S))]
    profs = [band_profile(S, reg, c) for c in range(len(S))]
    blank = [min(prof_level(p, k) for k in range(p["k0"] + 2, p["k1"] - 1)) for p in profs]
    return black, blank, profs


def row_factor(calc, i):
    """Flat-field factor for scan sample row i: nominal (clearest) blank light / blank light at this row."""
    p = calc.get("profile")
    if not p:
        return 1.0
    k = (135 + i) / 13.0                          # scan sample i is scanner y = 135+i; preview rows are every 13
    nom = calc["black"] - calc["blank"]
    return nom / max(calc["black"] - prof_level(p, k), 0.2 * nom)


def write_png_rgb(path, rows, w, h):
    raw = b"".join(b"\0" + r for r in rows)

    def k(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + k(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + k(b"IDAT", zlib.compress(raw, 3)) + k(b"IEND", b""))


def calibrate(sc, outdir, log=print):
    log("calibration: wide scan at DN01 ...")
    S1 = wide_arrays(sc.wide_scan((0, 1, 2), ["DN01"] * 3))
    os.makedirs(outdir, exist_ok=True)
    try:
        reg = detect_regions(S1)
    except ValueError as e:
        Hh, Ww = len(S1[0]), len(S1[0][0])
        write_png_rgb(os.path.join(outdir, "calibration-scan.png"),
                      [bytes(v for x in range(Ww) for v in (S1[0][y][x], S1[1][y][x], S1[2][y][x])) for y in range(Hh)], Ww, Hh)
        raise ScsiError("calibration failed: %s (the wide scan is saved as calibration-scan.png)" % e)
    black1, blank1, prof1 = levels_at(S1, reg)
    a, b = reg["blank_cols"]
    log("  holder (black) levels R,G,B at DN01: %s" % ", ".join("%.1f" % v for v in black1))
    log("  blank film levels R,G,B at DN01:     %s   (columns %d..%d of 385, scanner x %d..%d%s)" % (
        ", ".join("%.1f" % v for v in blank1), a, b, a * 13, b * 13,
        "; whole film area is blank" if reg.get("whole_film") else ""))
    for c in range(3):
        if black1[c] - blank1[c] < 15:
            raise ScsiError("calibration failed: blank film is not clearly lighter than the holder in %s "
                            "(black %.1f, blank %.1f)" % ("RGB"[c], black1[c], blank1[c]))
    # per channel: DN04 (2.04x gain, better precision) when the blank film still fits below the clip point
    use04 = [c for c in range(3) if (black1[c] - blank1[c]) * DN_GAIN * T_HI <= black1[c] - 6]
    cal = {c: {"dn": "DN01", "black": black1[c], "blank": blank1[c], "profile": prof1[c]} for c in range(3)}
    if use04:
        log("calibration: wide scan at DN04 for channels %s ..." % "".join("RGB"[c] for c in use04))
        S4 = wide_arrays(sc.wide_scan(use04, ["DN04"] * len(use04)))
        black4, blank4, prof4 = levels_at(S4, reg)
        for k, c in enumerate(use04):
            pred = black1[c] - (black1[c] - blank1[c]) * DN_GAIN
            log("  %s at DN04: black %.1f, blank %.1f (predicted from DN01: %.1f)" % ("RGB"[c], black4[k], blank4[k], pred))
            if abs(blank4[k] - pred) <= 10 and blank4[k] > 8 and black4[k] - blank4[k] > 30:
                cal[c] = {"dn": "DN04", "black": black4[k], "blank": blank4[k], "profile": prof4[k]}
    data = {"version": 2, "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "scanner": "AGFA VISION 35", "blank_columns_preview": [a, b],
            "channels": {"RGB"[c]: cal[c] for c in range(3)}}
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, CAL_NAME)
    json.dump(data, open(path, "w"), indent=1)
    # picture of the wide scan (raw signal) with the detected blank film tinted green, to eyeball the result
    bp = set(reg["blank_pts"])
    H, Wd = len(S1[0]), len(S1[0][0])
    rows = []
    for y in range(H):
        r = bytearray()
        for x in range(Wd):
            px = [S1[0][y][x], S1[1][y][x], S1[2][y][x]]
            if (y, x) in bp:
                px = [px[0] // 2, min(255, px[1] // 2 + 128), px[2] // 2]
            r += bytes(px)
        rows.append(bytes(r))
    write_png_rgb(os.path.join(outdir, "calibration-scan.png"), rows, Wd, H)
    for c in range(3):
        d = cal[c]
        p = d["profile"]
        ends = [d["black"] - prof_level(p, k) for k in (p["k0"] + 3, p["k1"] - 3)]
        log("  %s: %s  black %.1f  blank %.1f  light range %.1f  (falloff: film edges carry %.0f%% / %.0f%% of the centre's light)" % (
            "RGB"[c], d["dn"], d["black"], d["blank"], d["black"] - d["blank"],
            100 * ends[0] / (d["black"] - d["blank"]), 100 * ends[1] / (d["black"] - d["blank"])))
    log("saved %s (and calibration-scan.png, detected blank film tinted green)" % path)
    return data


def load_calibration(path, channels):
    try:
        data = json.load(open(path))
    except OSError:
        raise ScsiError("no calibration found (%s). Show a piece of blank film and run: scan calibrate" % path)
    cal = {}
    for i in channels:
        d = data["channels"]["RGB"[i]]
        if d["black"] - d["blank"] < 10:
            raise ScsiError("calibration for %s looks invalid; re-run: scan calibrate" % "RGB"[i])
        cal[i] = d
    return data, cal


# ---------------- tables ----------------

def build_table(black, blank, t_hi=T_HI):
    """256-entry table (index = scanner signal s) giving y = 255*ln(T/T_LO)/ln(T_HI/T_LO), where
    T = (black-s)/(black-blank) is the transmittance relative to blank film.  y increases with light."""
    span = black - blank
    lo = t_lo(black, blank)
    t = []
    for i in range(256):
        T = (black - i) / span
        if T <= lo:
            y = 0
        elif T >= t_hi:
            y = 255
        else:
            y = int(round(255 * math.log(T / lo) / math.log(t_hi / lo)))
        t.append(y)
    return t


def decode_map(black, blank, table, t_hi=T_HI):
    """Output byte v -> transmittance T relative to blank film, inverting the table as piecewise linear."""
    span = black - blank
    lo = t_lo(black, blank)
    res = []
    for v in range(256):
        if v >= 255:
            res.append(t_hi); continue
        if v <= 0:
            res.append(lo); continue
        x = None
        for i in range(255):
            a, b = table[i], table[i + 1]
            if a >= v >= b and a != b:
                x = i + (a - v) / (a - b); break
            if a == b == v:
                x = i + 0.5; break
        if x is None:
            x = black
        res.append(max(lo, min(t_hi, (black - x) / span)))
    return res


def rows_through(rows, nch, maps):
    """Apply one 256-entry map per channel to interleaved rows. A map is a list of ints (8-bit out) or of
    (lo, hi) byte pairs (16-bit little-endian out)."""
    sixteen = isinstance(maps[0][0], tuple)
    luts = []
    for m in maps:
        luts.append((bytes(p[0] for p in m), bytes(p[1] for p in m)) if sixteen else bytes(m))
    out = []
    for r in rows:
        if sixteen:
            b = bytearray(len(r) * 2)
            for c in range(nch):
                ch = r[c::nch]
                b[2 * c::2 * nch] = ch.translate(luts[c][0])
                b[2 * c + 1::2 * nch] = ch.translate(luts[c][1])
            out.append(bytes(b))
        elif nch == 1:
            out.append(r.translate(luts[0]))
        else:
            b = bytearray(len(r))
            for c in range(nch):
                b[c::nch] = r[c::nch].translate(luts[c])
            out.append(bytes(b))
    return out


_LUT_CACHE = {}


def decode_rows(rows, nch, channels, cal, row0, sixteen):
    """Decode y-byte rows to transmittance relative to the local blank film (flat-fielded per row).
    row0 = index of rows[0] in the uncropped scan.  16-bit LE (sixteen) or 8-bit display output."""
    dms, scales = [], []
    for i in channels:
        c = cal[i]
        th = c.get("t_hi", T_HI)
        dms.append(decode_map(c["black"], c["blank"], build_table(c["black"], c["blank"], th), th))
        scales.append(c.get("out_scale", OUT_SCALE))
    out = []
    for n, r in enumerate(rows):
        idx = row0 + n
        if sixteen:
            b = bytearray(len(r) * 2)
            for k, i in enumerate(channels):
                f = row_factor(cal[i], idx)
                key = (id(dms[k]), int(f * 400), 16, scales[k])
                lut = _LUT_CACHE.get(key)
                if lut is None:
                    pairs = [t16_map(T * (int(f * 400) / 400.0), scales[k]) for T in dms[k]]
                    lut = _LUT_CACHE[key] = (bytes(q[0] for q in pairs), bytes(q[1] for q in pairs))
                ch = r[k::nch]
                b[2 * k::2 * nch] = ch.translate(lut[0])
                b[2 * k + 1::2 * nch] = ch.translate(lut[1])
            out.append(bytes(b))
        else:
            b = bytearray(len(r))
            for k, i in enumerate(channels):
                f = row_factor(cal[i], idx)
                key = (id(dms[k]), int(f * 400), 8, scales[k])
                lut = _LUT_CACHE.get(key)
                if lut is None:
                    lut = _LUT_CACHE[key] = bytes(t8_map(T * (int(f * 400) / 400.0), scales[k]) for T in dms[k])
                b[k::nch] = r[k::nch].translate(lut)
            out.append(bytes(b))
    return out


def t16_map(T, scale=OUT_SCALE):
    v = max(0, min(65535, int(round(T * scale * 65535))))
    return (v & 0xFF, v >> 8)


def t8_map(T, scale=OUT_SCALE):     # display: gamma 2.2 of transmittance
    return max(0, min(255, int(round(255 * min(1.0, T * scale) ** (1 / 2.2)))))


# ---------------- image processing ----------------

def assemble(planes, width):
    """planes: per-colour lists of lines (width incl. pad byte). One scanner line is a vertical strip, so output
    x = line index, y = sample index.  Returns (w, h, rows) with interleaved samples."""
    n = len(planes[0])
    flat = [b"".join(p) for p in planes]
    h = width - 1                       # drop the trailing constant pad byte
    rows = []
    for y in range(h):
        if len(flat) == 1:
            rows.append(flat[0][y::width])
        else:
            r = bytearray(n * len(flat))
            for c, f in enumerate(flat):
                r[c::len(flat)] = f[y::width]
            rows.append(bytes(r))
    return n, h, rows


def holder_trim(w, h, rows, nch, value, thresh=0.9, margin=2):
    """Trim edge columns/rows in which more than `thresh` of the pixels equal `value` in all channels
    (the film holder).  Returns (x0, y0, x1, y1), x1/y1 exclusive."""
    x0, x1, y0, y1 = 0, w, 0, h

    def col_frac(x):
        c = 0
        for y in range(y0, y1):
            r = rows[y]
            if all(r[x * nch + k] == value for k in range(nch)):
                c += 1
        return c / max(1, y1 - y0)
    while x0 < x1 - 1 and col_frac(x0) > thresh:
        x0 += 1
    while x1 - 1 > x0 and col_frac(x1 - 1) > thresh:
        x1 -= 1

    def row_frac(y):
        r = rows[y]
        c = 0
        for x in range(x0, x1):
            if all(r[x * nch + k] == value for k in range(nch)):
                c += 1
        return c / max(1, x1 - x0)
    while y0 < y1 - 1 and row_frac(y0) > thresh:
        y0 += 1
    while y1 - 1 > y0 and row_frac(y1 - 1) > thresh:
        y1 -= 1
    if (x0, y0, x1, y1) != (0, 0, w, h):
        x0 = min(x0 + margin, x1 - 1); y0 = min(y0 + margin, y1 - 1)
        x1 = max(x1 - margin, x0 + 1); y1 = max(y1 - margin, y0 + 1)
    return x0, y0, x1, y1


def crop(rows, nch, box):
    x0, y0, x1, y1 = box
    return [r[x0 * nch:x1 * nch] for r in rows[y0:y1]]


def invert(rows):
    tbl = bytes(255 - i for i in range(256))
    return [r.translate(tbl) for r in rows]


def auto_levels(rows, nch, gamma=1.0, lo_pct=0.5, hi_pct=99.5):
    """Per-channel 8-bit stretch (0.5%..99.5% percentiles) with optional gamma; software only."""
    tables = []
    for c in range(nch):
        hist = [0] * 256
        for r in rows:
            ch = r[c::nch]
            for v in set(ch):
                hist[v] += ch.count(v)
        tot = sum(hist)
        lo = hi = None
        cum = 0
        for v in range(256):
            cum += hist[v]
            if lo is None and cum >= tot * lo_pct / 100:
                lo = v
            if hi is None and cum >= tot * hi_pct / 100:
                hi = v
        if hi <= lo:
            hi = min(255, lo + 1)
        tables.append(bytes(
            255 if v >= hi else (0 if v <= lo else min(255, max(0, int(round(255 * ((v - lo) / (hi - lo)) ** (1.0 / gamma))))))
            for v in range(256)))
    out = []
    for r in rows:
        if nch == 1:
            out.append(r.translate(tables[0]))
        else:
            b = bytearray(len(r))
            for c in range(nch):
                b[c::nch] = r[c::nch].translate(tables[c])
            out.append(bytes(b))
    return out


def orient(rows, w, h, ps, rot=0, flip_h=False, flip_v=False):
    """Lossless orientation. ps = bytes per pixel. Flips first, then rotates clockwise by rot (0/90/180/270)."""
    def rev_pixels(r):
        b = bytearray(len(r))
        for k in range(ps):
            b[k::ps] = r[k::ps][::-1]
        return bytes(b)
    if flip_v:
        rows = rows[::-1]
    if flip_h:
        rows = [rev_pixels(r) for r in rows]
    if rot == 180:
        rows = [rev_pixels(r) for r in rows[::-1]]
    elif rot in (90, 270):
        src = rows[::-1] if rot == 90 else rows
        cols = [b"".join(r[x * ps:(x + 1) * ps] for r in src) for x in range(w)]
        rows = cols if rot == 90 else cols[::-1]
        w, h = h, w
    return rows, w, h


def write_tiff(path, w, h, rows, nch, bps=8):
    """Uncompressed 8- or 16-bit TIFF (greyscale or RGB), little endian, single strip, untagged colour."""
    bpp = bps // 8
    assert len(rows) == h and all(len(r) == w * nch * bpp for r in rows)
    data_len = w * h * nch * bpp
    n_ent = 11
    ifd_off = 8
    extra_off = ifd_off + 2 + 12 * n_ent + 4
    bps_off = extra_off
    soft = b"vision35 scan.py\0"
    soft_off = bps_off + (6 if nch == 3 else 0)
    data_off = soft_off + len(soft)
    if data_off % 2:
        data_off += 1
    entries = [(256, 4, 1, w), (257, 4, 1, h),
               (258, 3, 3, bps_off) if nch == 3 else (258, 3, 1, bps),
               (259, 3, 1, 1), (262, 3, 1, 2 if nch == 3 else 1), (273, 4, 1, data_off), (277, 3, 1, nch),
               (278, 4, 1, h), (279, 4, 1, data_len), (284, 3, 1, 1), (305, 2, len(soft), soft_off)]
    entries.sort(key=lambda t: t[0])
    with open(path, "wb") as f:
        f.write(b"II*\0" + struct.pack("<I", ifd_off))
        f.write(struct.pack("<H", len(entries)))
        for tag, typ, cnt, val in entries:
            if typ == 3 and cnt == 1:
                f.write(struct.pack("<HHIHH", tag, typ, cnt, val, 0))
            else:
                f.write(struct.pack("<HHII", tag, typ, cnt, val))
        f.write(struct.pack("<I", 0))
        if nch == 3:
            f.write(struct.pack("<HHH", bps, bps, bps))
        f.write(soft)
        if f.tell() % 2:
            f.write(b"\0")
        assert f.tell() == data_off, (f.tell(), data_off)
        for r in rows:
            f.write(r)


def next_numbered(directory, prefix=""):
    best = 0
    for name in os.listdir(directory):
        m = re.fullmatch(re.escape(prefix) + r"(\d+)\.tiff?", name, re.I)
        if m:
            best = max(best, int(m.group(1)))
    return os.path.join(directory, "%s%d.tiff" % (prefix, best + 1))


def clip_report(rows, nch, names, los, his):
    """Fraction of pixels at the table floor/ceiling per channel (8-bit y rows)."""
    for c in range(nch):
        lo = hi = tot = 0
        for r in rows[::7]:
            ch = r[c::nch]
            lo += ch.count(0); hi += ch.count(255); tot += len(ch)
        print("  %s: %.2f%% denser than T=%.2f (floor), %.2f%% clearer than %.2fx blank (ceiling)" % (
            names[c], 100.0 * lo / tot, los[c], 100.0 * hi / tot, his[c]))
        if 100.0 * hi / tot > 0.5:
            print("     warning: %s has areas clearer than the calibrated blank film; recalibrate with the "
                  "clearest film available" % names[c])


def main():
    ap = argparse.ArgumentParser(prog="scan", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="scan " + __version__)
    ap.add_argument("command", nargs="?", choices=("scan", "calibrate"), default="scan")
    ap.add_argument("--preview", action="store_true", help="low-res greyscale preview instead of a full scan")
    ap.add_argument("--out", help="output file (default: next free N.tiff in --dir)")
    ap.add_argument("--dir", default=".", help="folder for output files and calibration.json (default: current folder)")
    ap.add_argument("--calibration", help="calibration file (default: <dir>/calibration.json)")
    ap.add_argument("--output", choices=("negpy", "display"), default="negpy",
                    help="negpy (default): 16-bit linear transmittance for NegPy; display: 8-bit gamma picture")
    ap.add_argument("--positive", action="store_true", help="shorthand for --output display --invert --auto-levels")
    ap.add_argument("--invert", action="store_true", help="(display) invert negative -> positive")
    ap.add_argument("--auto-levels", action="store_true", help="(display) per-channel stretch to brighten")
    ap.add_argument("--gamma", type=float, default=1.0, help="gamma used with --auto-levels")
    ap.add_argument("--mono", nargs="?", const="R", choices=("R", "G", "B"), metavar="CHANNEL",
                    help="black & white negative: scan ONE filter only (default R: most light, best SNR on the "
                         "B&W test frame; G was the noisiest). About 2 min instead of 4")
    ap.add_argument("--gray", action="store_true", help="with --mono: single-channel TIFF (default: 3 identical channels)")
    ap.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0, help="rotate clockwise (lossless)")
    ap.add_argument("--no-flip", action="store_true",
                    help="keep the scanner's native orientation (default: flipped top-to-bottom, which makes film "
                         "loaded the usual way come out upright)")
    ap.add_argument("--flip-h", action="store_true",
                    help="mirror left-to-right (needed if text in the picture reads backwards, i.e. the strip is in "
                         "the holder emulsion-side the other way)")
    ap.add_argument("--no-trim", action="store_true", help="do not trim the film holder")
    ap.add_argument("--device", help="sg device (default: auto-detect AGFA VISION 35)")
    ap.add_argument("--keep-raw", action="store_true", help="also save <out>.raw + <out>.json (for --from-scan)")
    ap.add_argument("--from-scan", metavar="FILE.raw", help="re-process a saved raw scan instead of scanning")
    a = ap.parse_args()

    if a.positive:
        a.output, a.invert, a.auto_levels = "display", True, True
    os.makedirs(os.path.expanduser("~/scanner_log"), exist_ok=True)
    logpath = os.path.expanduser("~/scanner_log/scan-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))

    def open_scanner():
        dev = a.device or find_scanner()
        if not dev:
            sys.exit("AGFA VISION 35 not found (check cabling, power-on order, SCSI ID and the sym53c8xx driver)")
        sc = Scanner(dev, logpath)
        iq = sc.inquiry()
        print("scanner: %s on %s" % (iq[8:36].decode("ascii", "replace"), dev), flush=True)
        return sc

    if a.command == "calibrate":
        sc = open_scanner()
        try:
            calibrate(sc, a.dir)
        except ScsiError as e:
            sys.exit("scanner error: %s\n(log: %s)" % (e, logpath))
        return

    calpath = a.calibration or os.path.join(a.dir, CAL_NAME)
    channels = (("R", "G", "B").index(a.mono),) if a.mono else (0, 1, 2)
    if a.mono and a.preview:
        sys.exit("--mono applies to full scans (the preview is already a single green channel)")
    cal = None
    planes = None
    sixteen = (a.output == "negpy") and not a.preview

    if a.from_scan:
        meta = json.load(open(a.from_scan[:-4] + ".json" if a.from_scan.endswith(".raw") else a.from_scan + ".json"))
        channels = tuple(meta["channels"])
        if len(channels) == 1 and not a.mono:
            a.mono = "RGB"[channels[0]]
        cal = {int(k): v for k, v in meta["calibration"].items()}
        raw = open(a.from_scan, "rb").read()
        planes = [[raw[c * SCAN_W * SCAN_LINES + i:c * SCAN_W * SCAN_LINES + i + SCAN_W]
                   for i in range(0, SCAN_W * SCAN_LINES, SCAN_W)] for c in range(len(channels))]
        width = SCAN_W
    else:
        sc = open_scanner()
        t0 = time.time()

        def progress(i, n, ch=""):
            print("  %s line %d/%d  %.0fs" % (ch, i, n, time.time() - t0), flush=True)
        try:
            if a.preview:
                planes, width = [sc.preview(progress)], PREVIEW_W
            else:
                data, cal = load_calibration(calpath, channels)
                print("calibration %s (%s): %s" % (calpath, data.get("created", "?"), ", ".join(
                    "%s %s black %.1f blank %.1f" % ("RGB"[i], cal[i]["dn"], cal[i]["black"], cal[i]["blank"]) for i in channels)))
                for i in channels:      # per-scan encoding parameters; stored with --keep-raw so the scan decodes exactly
                    cal[i]["t_hi"] = round(min(T_HI_MAX, (cal[i]["black"] - 3.0) / (cal[i]["black"] - cal[i]["blank"])), 4)
                    cal[i]["out_scale"] = OUT_SCALE_NEW
                tables = [build_table(cal[i]["black"], cal[i]["blank"], cal[i]["t_hi"]) for i in channels]
                dns = [cal[i]["dn"] for i in channels]
                planes, width = sc.full_scan(channels, dns, tables, progress), SCAN_W
        except ScsiError as e:
            sys.exit("error: %s\n(log: %s)" % (e, logpath))

    names = "".join("RGB"[i] for i in channels)
    nch = len(planes)
    w, h, rows = assemble(planes, width)
    value = 255 if a.preview else 0               # preview table saturates the holder to 255; scans floor it at 0
    row0 = 0
    if not a.no_trim:
        box = holder_trim(w, h, rows, nch, value)
        print("holder trim: x %d..%d, y %d..%d of %dx%d" % (box[0], box[2], box[1], box[3], w, h))
        rows = crop(rows, nch, box)
        row0 = box[1]
        w, h = box[2] - box[0], box[3] - box[1]

    bps = 8
    if a.preview:
        rows = auto_levels(rows, nch, 1.0)        # mild software stretch on top of the fixed preview table
    else:
        clip_report(rows, nch, names, [t_lo(cal[i]["black"], cal[i]["blank"]) for i in channels],
                    [cal[i].get("t_hi", T_HI) for i in channels])
        rows = decode_rows(rows, nch, channels, cal, row0, sixteen)
        if sixteen:
            bps = 16
        else:
            if a.invert:
                rows = invert(rows)
            if a.auto_levels:
                rows = auto_levels(rows, nch, a.gamma)
    flip_v = not a.no_flip
    if a.rotate or a.flip_h or flip_v:
        rows, w, h = orient(rows, w, h, nch * (bps // 8), a.rotate, a.flip_h, flip_v)
    if a.mono and not a.gray and nch == 1:
        bpp = bps // 8
        tri = []
        for r in rows:
            b = bytearray(len(r) * 3)
            for k in range(3):
                for j in range(bpp):
                    b[k * bpp + j::3 * bpp] = r[j::bpp]
            tri.append(bytes(b))
        rows, nch = tri, 3

    if a.out:
        out = a.out
    else:
        os.makedirs(a.dir, exist_ok=True)
        out = next_numbered(a.dir, "preview-" if a.preview else "")
    write_tiff(out, w, h, rows, nch, bps)
    if a.keep_raw and not a.from_scan and not a.preview:
        with open(out + ".raw", "wb") as f:
            for p in planes:
                f.write(b"".join(p))
        json.dump({"channels": list(channels), "calibration": {str(k): v for k, v in cal.items()}},
                  open(out + ".json", "w"))
    print("wrote %s (%dx%d, %s %d-bit%s)" % (out, w, h, "RGB" if nch == 3 else "grey", bps,
                                              ", linear transmittance" if sixteen else ""))


if __name__ == "__main__":
    main()

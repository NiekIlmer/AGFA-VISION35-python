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
  scan calibrate-sensor       once per scanner, with the film holder EMPTY: measure the sensor's fixed streak
                              pattern (green and blue) so every scan can correct it
  scan calibrate-dark         once per scanner, with the aperture COVERED (two layers of foil): measure the true
                              zero-light level at several exposures; scans use it as their black reference

The output is flipped top-to-bottom by default (upright for film loaded the usual way); --no-flip, --flip-h and
--rotate adjust that.

How the scanner's signal works (measured, see docs/PROTOCOL.md): the scanner reports an inverted 8-bit signal s.
The film holder (no light) reads ~253 and clearer film reads lower, so light is linear in (s_black - s).  DN04 is
DNnn is an exposure setting: light scales as 10^((nn-mm)/10) between DN mm and DN nn (DN04 = 2x DN01).  `calibrate`
measures black and blank film at the highest DN each channel can use, plus the lamp falloff across the film, and
`scan` then raises the exposure further when the frame itself is denser than blank film (auto-exposure).

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
import argparse, ctypes, datetime, fcntl, glob, json, math, os, re, shutil, struct, sys, time, zlib

SG_IO = 0x2285
ALLOWED_OPS = {0x12, 0x00, 0x03, 0x2A, 0x28}
DXFER_NONE, DXFER_TO, DXFER_FROM = -1, -2, -3

# every text command that appears in the trace and that this tool may send
ALLOWED_TEXT = re.compile(
    r"^(APIN|OUT8|AF,R|MP|CP|GML8|IF15|INM|SPUP|NHD|AP50|AVOF|SDON|DN(0[0-9]|1[0-2])|BRT0|CNT0|SFT0|PSOF|PVOF|"
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

DN_MAX = 12               # highest exposure step used: scan time per line is flat up to ~DN12 and grows beyond
EXPOSURE_HEADROOM = 1.25  # auto-exposure keeps the frame's clearest area x1.25 below saturation


def dn_number(dn):
    return int(dn[2:])


def dn_name(n):
    return "DN%02d" % n


def dn_factor(n_from, n_to):
    """Signal factor when going from DN n_from to DN n_to.  DNnn is a minimum-density setting of nn/10: the scanner
    lengthens its exposure by 10^(nn/10), so one step is x1.26 and DN04 is 10^0.3 = 1.995x DN01."""
    return 10.0 ** ((n_to - n_from) / 10.0)
T_HI = 1.12               # legacy encoded ceiling (older calibrations/raw scans without a stored "t_hi")
T_HI_MAX = 1.3            # ceiling for new scans: room for the ~8% pixel noise of near-blank film
OUT_SCALE_NEW = 0.75      # new 16-bit scans: blank film -> 0.75 of full scale, so T_HI_MAX (1.3) -> 0.975
T_LO_MIN = 0.0003         # density 3.5: absolute floor of the encoded range (the floor follows the exposure: 1/light)


OUT_SCALE = 0.85          # legacy output scale (raw scans made before "out_scale" was stored)
FILTERS = (("FR,R", "GM1T8"), ("FG,R", "GM2T8"), ("FB,R", "GM3T8"))
CAL_NAME = "calibration.json"
SENSOR_NAME = "sensor-profile.json"
SENSOR_BLACK = 253.1      # holder level assumed when measuring the sensor pattern (the pattern is a ratio)


def t_lo(black, blank):
    """Encoded floor: about one raw level of light (denser film cannot be resolved), never above 0.02."""
    return min(0.02, max(T_LO_MIN, 1.0 / (black - blank)))


class ScsiError(Exception):
    pass


def read_file(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


def load_json(path):
    with open(path) as f:
        return json.load(f)


def save_json(obj, path, **kw):
    with open(path, "w") as f:
        json.dump(obj, f, **kw)


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
            if read_file(dev + "/type").strip() != "6":
                continue
            vendor = read_file(dev + "/vendor").strip()
            model = read_file(dev + "/model").strip()
            hctl = os.path.basename(os.path.realpath(dev))
        except OSError:
            continue
        if vendor == "AGFA" and model == "VISION 35" and hctl.endswith(":0"):
            return "/dev/" + os.path.basename(d)
    return None


class Display:
    """Two-line live display.  Line 1: what is happening (stage, detail, progress, time).  Line 2: one progress bar
    whose sections are coloured by the colour being scanned (red, green, blue; grey for preparation and processing).
    Not live (output is not a terminal, or --plain): only the plain log lines are printed."""
    SPIN = {True: "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", False: "|/-\\"}
    EIGHTHS = " ▏▎▍▌▋▊▉"
    HUE = {"R": ("\x1b[91m", "\x1b[31m"), "G": ("\x1b[92m", "\x1b[32m"), "B": ("\x1b[94m", "\x1b[34m"),
           "grey": ("\x1b[97m", "\x1b[90m")}

    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.live, self.color, self.unicode, self.cols = False, True, True, None
        self._reset()

    def _reset(self):
        self.segs, self.cur = [], -1
        self.t0 = time.time()
        self.stage = self.ctx = self.what = self.chan = ""
        self.drawn = self.finished = self.hidden = False
        self.last, self.spin = 0.0, 0

    def configure(self, live, color=True, cols=None):
        self.live, self.color, self.cols = bool(live), bool(color), cols
        self.unicode = (getattr(self.out, "encoding", None) or "").lower().startswith("utf")

    def start(self, plan, stage=""):
        """plan: list of (label, kind, weight) with kind 'R', 'G', 'B' or 'grey'; weight = expected seconds."""
        self._reset()
        self.stage = stage
        self.segs = [{"label": l, "kind": k, "w": float(w), "p": 0.0} for l, k, w in plan]
        if self.live:
            self.out.write("\x1b[?25l")
            self.hidden = True
            self._render()

    def extend(self, plan):
        self.segs += [{"label": l, "kind": k, "w": float(w), "p": 0.0} for l, k, w in plan]

    def reweight(self, start, weights):
        for k, w in enumerate(weights):
            if start + k < len(self.segs):
                self.segs[start + k]["w"] = float(w)

    def next(self, label=None):
        """Finish the current section and move to the next one."""
        if 0 <= self.cur < len(self.segs):
            self.segs[self.cur]["p"] = 1.0
        self.cur += 1
        if self.cur < len(self.segs):
            self.segs[self.cur]["t0"] = time.time()
            if label is not None:
                self.segs[self.cur]["label"] = label
        self.tick(True)

    def _set(self, frac):
        if 0 <= self.cur < len(self.segs):
            self.segs[self.cur]["p"] = max(0.0, min(1.0, frac))
            self.segs[self.cur]["explicit"] = True

    @staticmethod
    def _eff(sg):
        """Progress of a section.  A grey section nobody reports on (start-up, autofocus, analysis) advances with
        time towards its expected duration, so the bar keeps moving; it never shows more than 95% by itself."""
        if sg["p"] < 1.0 and sg["kind"] == "grey" and not sg.get("explicit") and "t0" in sg:
            return min(0.95, (time.time() - sg["t0"]) / max(1.0, sg["w"]))
        return sg["p"]

    def event(self, what):
        self.what = what
        self.tick()

    def progress(self, frac, what=None):
        self._set(frac)
        if what is not None:
            self.what = what
        self.tick()

    def lines(self, i, n):
        self._set(i / float(max(1, n)))
        self.what = "line %d/%d" % (i, n)
        if self.live:
            self.tick()
        elif i % 200 == 0:
            self.out.write("  %s line %d/%d  %.0fs\n" % (self.chan, i, n, time.time() - self.t0))
            self.out.flush()

    def say(self, text="", end="\n"):
        """Print a message; a live display is kept at the bottom."""
        if self.live and self.drawn and not self.finished:
            self._erase()
            self.out.write(text + end)
            self._render()
        else:
            self.out.write(text + end)
        self.out.flush()

    def tick(self, force=False):
        if self.live and not self.finished and (force or time.time() - self.last >= 0.1):
            self._render()

    def finish(self, what="done"):
        for sg in self.segs:
            sg["p"] = 1.0
        self.stage, self.what, self.ctx = "Done", what, ""
        if self.live:
            self._render(final=True)
        self.finished = True
        self.close()

    def close(self):
        if self.hidden:
            self.out.write("\x1b[?25h")
            self.out.flush()
            self.hidden = False

    def _erase(self):
        self.out.write("\x1b[2A\r\x1b[J")
        self.drawn = False

    def _render(self, final=False):
        cols = self.cols or shutil.get_terminal_size((100, 24)).columns
        l1, l2 = self._line1(cols, final), self._line2(cols)
        if self.drawn:
            self.out.write("\x1b[2A")
        self.out.write("\r\x1b[2K" + l1 + "\n\r\x1b[2K" + l2 + "\n")
        self.out.flush()
        self.drawn = True
        self.last = time.time()
        self.spin += 1

    @staticmethod
    def fmt(sec):
        sec = int(max(0, sec))
        return "%d:%02d:%02d" % (sec // 3600, sec // 60 % 60, sec % 60) if sec >= 3600 else "%d:%02d" % (sec // 60, sec % 60)

    def overall(self):
        tot = sum(sg["w"] for sg in self.segs) or 1.0
        return sum(sg["w"] * self._eff(sg) for sg in self.segs) / tot

    def _line1(self, cols, final):
        frac, el = self.overall(), time.time() - self.t0
        right = "%3d%%  %s" % (round(100 * frac), self.fmt(el))
        if not final and 0.03 <= frac < 1.0:
            right += "  ~%s left" % self.fmt(el * (1 - frac) / frac)
        spin = ("✔" if self.unicode else "*") if final else self.SPIN[self.unicode][self.spin % len(self.SPIN[self.unicode])]
        text = ": ".join(t for t in (self.stage, self.ctx) if t)
        if self.what:
            text = (text + " — " if text else "") + self.what
        left = "%s %s" % (spin, text)
        room = cols - 1 - len(right) - 2
        if len(left) > room:
            left = left[:max(0, room - 1)] + ("…" if self.unicode else "~")
        pad = " " * max(1, cols - 1 - len(left) - len(right))
        if self.color:
            return "\x1b[1m" + left + "\x1b[0m" + pad + "\x1b[2m" + right + "\x1b[0m"
        return left + pad + right

    @staticmethod
    def alloc(weights, total):
        """Integer cells per section, proportional to the weights (largest remainder), at least one each."""
        s = sum(weights) or 1.0
        raw = [w * total / s for w in weights]
        base = [int(r) for r in raw]
        if total >= len(weights):
            base = [max(1, b) if w > 0 else b for b, w in zip(base, weights)]
        rem = total - sum(base)
        order = sorted(range(len(raw)), key=lambda i: raw[i] - int(raw[i]), reverse=True)
        k = 0
        while rem > 0 and order:
            base[order[k % len(order)]] += 1
            rem -= 1
            k += 1
        while rem < 0:
            i = max(range(len(base)), key=lambda j: base[j])
            base[i] -= 1
            rem += 1
        return base

    def _line2(self, cols):
        width = max(10, min(cols - 1, 120))
        full, empty = ("█", "░") if self.unicode else ("#", "-")
        if not self.segs:
            return empty * width
        out = []
        for idx, (sg, n) in enumerate(zip(self.segs, self.alloc([x["w"] for x in self.segs], width))):
            if n <= 0:
                continue
            filled = self._eff(sg) * n
            whole, part = int(filled), int((filled - int(filled)) * 8)
            bar = full * whole
            if self.unicode and part and whole < n:
                bar += self.EIGHTHS[part]
            bar += empty * (n - len(bar))
            if not self.color and idx > 0:
                bar = "|" + bar[1:]                      # no colours: mark the section boundaries instead
            on, off = self.HUE.get(sg["kind"], self.HUE["grey"])
            if self.color:
                k = whole + (1 if self.unicode and part and whole < n else 0)
                out.append(on + bar[:k] + "\x1b[0m" + off + bar[k:] + "\x1b[0m")
            else:
                out.append(bar)
        return "".join(out)


UI = Display()


def say(*args):
    UI.say(" ".join(str(a) for a in args))


CHAN_NAME = {"R": "red", "G": "green", "B": "blue"}
PREP_S, WIDE_PASS_S, PROC_S = 14.0, 17.0, 40.0          # rough seconds: scanner start-up, wide pass, processing
EXPO_K = (13.3, 6.6, 8.4)                                # ms of exposure per line at DN00 (R, G, B), measured


def est_pass_seconds(c, dn):
    """Rough time of a full-resolution pass (line time = max(17 ms, exposure time))."""
    return SCAN_LINES * max(0.017, EXPO_K[c] / 1000.0 * 10 ** (dn / 10.0))


def plan_wide(channels):
    return [("prep", "grey", PREP_S)] + [("pre-" + "RGB"[c], "RGB"[c], WIDE_PASS_S) for c in channels]


def plan_scan(channels, dns):
    return [("prep", "grey", PREP_S)] + [("RGB"[c], "RGB"[c], est_pass_seconds(c, d)) for c, d in zip(channels, dns)]


def friendly(cmd):
    """Plain-language description of a scanner command, for the status line."""
    if cmd.startswith("AF"):
        return "autofocusing"
    if cmd.startswith("MV"):
        return "moving the carriage"
    if len(cmd) == 4 and cmd[0] == "F" and cmd[2:] == ",R":
        return "selecting the %s filter" % CHAN_NAME[cmd[1]]
    if cmd.startswith("DN"):
        return "setting the exposure (%s)" % cmd
    if cmd.startswith("GM"):
        return "selecting the exposure table"
    if cmd.startswith("BXZ"):
        return "starting the scan"
    if cmd in ("PSOF", "PVOF"):
        return "finishing"
    if cmd in ("APIN", "OUT8"):
        return "initialising"
    return "configuring"


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
            UI.tick()
            time.sleep(0.05)

    def send(self, text):
        if not ALLOWED_TEXT.match(text):
            raise ScsiError("text command %r is not one of the traced commands" % text)
        UI.event(friendly(text))
        data = text.encode("ascii") + b"\r\n"
        L = len(data)
        st, hs, d, sb = self.cmd([0x2A, 0, 0, 0, 0, 0, 0, L >> 8, L & 0xFF, 0], data_out=data)
        self._check(st, hs, sb)
        self.wait_ready()

    def send_table(self, n, values):
        """Write table n (1..3): 'WGMnT,v0,...,v255' + CRLF, 256 values 0..255 (format verified experimentally)."""
        if n not in (1, 2, 3) or len(values) != 256 or not all(isinstance(v, int) and 0 <= v <= 255 for v in values):
            raise ScsiError("bad WGM table")
        UI.event("loading the exposure table")
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
            UI.lines(i + 1, expect_lines)
        if end is None or end[1] != b"\x03":
            raise ScsiError("scan did not end with the expected end marker (got %d lines)" % len(lines))
        if len(lines) != expect_lines:
            raise ScsiError("expected %d lines, got %d" % (expect_lines, len(lines)))
        return lines

    # ---- sequences (as in the trace, plus WGM tables) ----
    def preview(self, progress=None):
        UI.stage = "Preview"
        UI.next("prep")
        self._cmds(["APIN", "OUT8", "AF,R"]); self._status06("AF,R")
        self._cmds(["MP", "GML8", "IF15", "SPUP", "NHD", "AP50", "AVOF", "SDON", "DN01", "BRT0",
                    "CNT0", "SFT0"])
        self.send_table(1, PREVIEW_TABLE)
        self.send("FG,R"); self._status06("FG,R")
        self._cmds(["GM1T8", "MV0,R"]); self._status06("MV0,R")
        self.send(PREVIEW_BXZ)
        UI.next("preview")
        UI.chan, UI.ctx = "G", "green filter"
        lines = self.read_lines(PREVIEW_W, PREVIEW_LINES, progress)
        UI.ctx = ""
        self._cmds(["PSOF", "PVOF"])
        return lines

    def wide_scan(self, channels, dns, stage=None):
        """Whole-frame step-13 pass per channel with neutral tables (used by calibration)."""
        if stage:
            UI.stage = stage
        UI.next("prep")
        UI.ctx = ""
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
            UI.next()
            UI.chan, UI.ctx = "RGB"[i], "%s at %s" % (CHAN_NAME["RGB"[i]], dns[k])
            out.append(self.read_lines(PREVIEW_W, PREVIEW_LINES))
        UI.ctx = ""
        self._cmds(["PSOF", "PVOF"])
        return out

    def open_light_scan(self, channels, progress=None):
        """Full-window scan of the EMPTY holder at DN00 (lowest exposure) with neutral tables: sensor pattern data."""
        UI.stage = "Sensor calibration"
        UI.next("prep")
        self._cmds(["PSOF", "PVOF", "IF15", "INM", "APIN", "OUT8", "AF,R"]); self._status06("AF,R")
        self._cmds(["CP", "GML8", "IF15", "SPUP", "NHD", "AP50", "AVOF", "SDON", "DN00", "BRT0", "CNT0", "SFT0"])
        for i in channels:
            self.send_table(i + 1, IDENTITY)
        planes = []
        for i in channels:
            f, g = FILTERS[i]
            self.send("DN00")
            self.send(f); self._status06(f)
            self.send(g); self.send("MV345,R"); self._status06("MV345,R")
            self.send(SCAN_BXZ)
            UI.next()
            UI.chan, UI.ctx = "RGB"[i], "%s, empty holder at DN00" % CHAN_NAME["RGB"[i]]
            planes.append(self.read_lines(SCAN_W, SCAN_LINES))
        UI.ctx = ""
        self._cmds(["PSOF", "PVOF", "IF15", "INM"])
        return planes

    def full_scan(self, channels, dns, tables, progress=None):
        UI.stage = "Scanning"
        UI.next("prep")
        UI.ctx = ""
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
            UI.next()
            UI.chan = "RGB"[i]
            UI.ctx = "%s%s at %s" % (CHAN_NAME["RGB"[i]],
                                     " pass %d/%d" % (k + 1, len(channels)) if len(channels) > 1 else "", dns[k])
            planes.append(self.read_lines(SCAN_W, SCAN_LINES))
        UI.ctx = ""
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


def sensor_pattern(lines, black=SENSOR_BLACK, k=15, margin=100):
    """Multiplicative response pattern of the sensor along the line (one value per sample, ~1.0), from lines of an
    empty-holder scan: row means over the lines, divided by their own moving average (symmetric window, 2k+1 rows).
    The slow part (lamp falloff) is left to the calibration profile.  Returns (pattern, saturated_fraction)."""
    body = lines[margin:len(lines) - margin]
    n = len(body[0]) - 1
    acc = [0] * n
    sat = 0
    for ln in body:
        acc = [a + v for a, v in zip(acc, ln[:n])]
        sat += ln[:n].count(0) + ln[:n].count(1) + ln[:n].count(2)
    light = [black - a / len(body) for a in acc]
    pat = []
    for i in range(n):
        half = min(k, i, n - 1 - i)
        seg = light[i - half:i + half + 1]
        pat.append(light[i] / (sum(seg) / len(seg)))
    return pat, sat / float(len(body) * n)


def dark_level(levels, dn):
    """Dark (zero-light) level at DN number dn, linearly interpolated between the measured DN settings.
    levels: {'DN00': v, 'DN04': v, ...}."""
    pts = sorted((int(k[2:]), v) for k, v in levels.items())
    if dn <= pts[0][0]:
        return pts[0][1]
    for (a, va), (b, vb) in zip(pts, pts[1:]):
        if dn <= b:
            return va + (vb - va) * (dn - a) / float(b - a)
    return pts[-1][1]


def cal_black(c):
    """Black (zero-light) level used for the tables/decoding of a scan: the measured dark level when available."""
    return c.get("black_scan", c["black"])


def dark_levels_from_wide(S):
    """Per channel mean signal inside the aperture of a wide scan taken with something opaque (foil) in the holder."""
    out = []
    for c in range(len(S)):
        vals = [S[c][y][x] for y in range(25, 220) for x in range(40, 350)]
        out.append(sum(vals) / len(vals))
    return out


def sensor_profile_paths(explicit=None):
    paths = [explicit] if explicit else [
        os.path.join(".", SENSOR_NAME),
        os.path.join(os.path.expanduser("~"), ".config", "vision35", SENSOR_NAME),
        os.path.join("/usr/local/share/vision35", SENSOR_NAME)]
    return paths


def load_sensor_profile(explicit=None):
    """First existing sensor profile -> ({'G': [...], 'B': [...]}, path) or ({}, None)."""
    for path in sensor_profile_paths(explicit):
        try:
            data = load_json(path)
        except (OSError, ValueError):
            continue
        return {k: v["pattern"] for k, v in data.get("channels", {}).items() if v.get("pattern")}, path
    return {}, None


def load_dark_levels(explicit=None):
    """Dark levels per channel ({'R': {'DN00': v, ...}, ...}) from the first sensor profile that has them."""
    for path in sensor_profile_paths(explicit):
        try:
            data = load_json(path)
        except (OSError, ValueError):
            continue
        if data.get("dark"):
            return data["dark"], path
    return {}, None


def calibrate_dark(sc, path, log=say):
    log("dark calibration: scanning with the aperture COVERED (e.g. two layers of foil) at several exposures ...")
    levels = {"R": {}, "G": {}, "B": {}}
    for dn in ("DN00", "DN04", "DN08", "DN12"):
        S = wide_arrays(sc.wide_scan((0, 1, 2), [dn] * 3, "Dark calibration (%s)" % dn))
        means = dark_levels_from_wide(S)
        for c, m in enumerate(means):
            if m < 250.0:
                raise ScsiError("%s is not dark at %s (mean signal %.1f): is the aperture fully covered and light-tight?"
                                % ("RGB"[c], dn, m))
            levels["RGB"[c]][dn] = round(m, 3)
        log("  %s: dark level R %.2f  G %.2f  B %.2f" % (dn, means[0], means[1], means[2]))
    try:
        data = load_json(path)
    except (OSError, ValueError):
        data = {"version": 1, "scanner": "AGFA VISION 35", "channels": {}}
    data["dark"] = levels
    data["dark_created"] = datetime.datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    save_json(data, path)
    log("saved dark levels into %s" % path)
    UI.finish("dark levels saved")


def calibrate_sensor(sc, path, log=say):
    log("sensor calibration: scanning the EMPTY film holder at DN00 (green and blue; red saturates) ...")
    channels = (1, 2)
    planes = sc.open_light_scan(channels)
    UI.next("analysing")
    UI.chan, UI.ctx = "", ""
    UI.event("computing the sensor pattern")
    out = {}
    for c, lines in zip(channels, planes):
        sample = [v for ln in lines[200:4200:50] for v in ln[200:2700:9]]
        mean_s = sum(sample) / len(sample)
        if mean_s > 150:
            raise ScsiError("the aperture is not bright (mean signal %.0f): is a film still in the holder? "
                            "remove the film (keep the holder) and try again" % mean_s)
        pat, sat = sensor_pattern(lines)
        if sat > 0.01:
            raise ScsiError("%s saturates at the lowest exposure (%.1f%% of pixels); cannot measure it" % ("RGB"[c], 100 * sat))
        rms = (sum((v - 1.0) ** 2 for v in pat) / len(pat)) ** 0.5
        log("  %s: mean light %.0f, pattern rms %.2f%%, peak-to-peak %.1f%%" % (
            "RGB"[c], SENSOR_BLACK - mean_s, 100 * rms, 100 * (max(pat) - min(pat))))
        out["RGB"[c]] = {"dn": "DN00", "mean_light": round(SENSOR_BLACK - mean_s, 1),
                         "pattern": [round(v, 5) for v in pat]}
    data = {"version": 1, "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "scanner": "AGFA VISION 35", "samples": SCAN_W - 1,
            "note": "multiplicative sensor pattern along the line; red could not be measured", "channels": out}
    try:                                           # keep dark levels measured earlier with calibrate-dark
        old = load_json(path)
        if old.get("dark"):
            data["dark"] = old["dark"]
            data["dark_created"] = old.get("dark_created")
    except (OSError, ValueError):
        pass
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    save_json(data, path)
    log("saved %s" % path)
    UI.finish("sensor profile saved")


def row_factor(calc, i):
    """Flat-field factor for scan sample row i: nominal (clearest) blank light / blank light at this row."""
    p = calc.get("profile")
    f = 1.0
    if p:
        k = (135 + i) / 13.0                      # scan sample i is scanner y = 135+i; preview rows are every 13
        nom = calc["black"] - calc["blank"]
        f = nom / max(calc["black"] - prof_level(p, k), 0.2 * nom)
    pat = calc.get("pattern")                     # sensor pattern (empty-holder measurement), if available
    if pat and 0 <= i < len(pat):
        f /= pat[i]
    return f


def write_png_rgb(path, rows, w, h):
    raw = b"".join(b"\0" + r for r in rows)

    def k(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + k(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + k(b"IDAT", zlib.compress(raw, 3)) + k(b"IEND", b""))


def calibrate(sc, outdir, log=say):
    log("calibration: wide scan at DN01 ...")
    S1 = wide_arrays(sc.wide_scan((0, 1, 2), ["DN01"] * 3, "Calibration"))
    UI.next("analysing")
    UI.chan, UI.ctx = "", ""
    UI.event("looking for the film holder and the blank film")
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
    # per channel: the highest exposure step (DN) that keeps the blank film, with headroom, below saturation
    dns = []
    for c in range(3):
        L1 = black1[c] - blank1[c]
        n = 1
        while n < DN_MAX and L1 * dn_factor(1, n + 1) * T_HI_MAX <= black1[c] - 6:
            n += 1
        dns.append(n)
    cal = {c: {"dn": "DN01", "black": black1[c], "blank": blank1[c], "profile": prof1[c]} for c in range(3)}
    use = [c for c in range(3) if dns[c] > 1]
    if use:
        log("calibration: wide scan with more exposure for %s (%s) ..." % (
            "".join("RGB"[c] for c in use), ", ".join(dn_name(dns[c]) for c in use)))
        UI.extend(plan_wide(use))
        S4 = wide_arrays(sc.wide_scan(use, [dn_name(dns[c]) for c in use], "Calibration (more exposure)"))
        black4, blank4, prof4 = levels_at(S4, reg)
        for k, c in enumerate(use):
            pred = black1[c] - (black1[c] - blank1[c]) * dn_factor(1, dns[c])
            log("  %s at %s: black %.1f, blank %.1f (predicted from DN01: %.1f)" % (
                "RGB"[c], dn_name(dns[c]), black4[k], blank4[k], pred))
            if abs(blank4[k] - pred) <= 0.06 * (black1[c] - pred) + 3 and blank4[k] > 8 and black4[k] - blank4[k] > 30:
                cal[c] = {"dn": dn_name(dns[c]), "black": black4[k], "blank": blank4[k], "profile": prof4[k]}
            else:
                log("  %s: measurement does not match the gain law, staying at DN01" % "RGB"[c])
    data = {"version": 2, "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "scanner": "AGFA VISION 35", "blank_columns_preview": [a, b],
            "channels": {"RGB"[c]: cal[c] for c in range(3)}}
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, CAL_NAME)
    save_json(data, path, indent=1)
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
    UI.finish("calibration saved")
    return data


def cal_blank(c):
    """Blank-film level used for the tables/decoding of a scan: a 'virtual' level when the scan's DN differs from
    the calibrated one (the gain law scales the light), else the calibrated level."""
    return c.get("blank_scan", c["blank"])


def frame_clear_levels(S, blacks, exclude=None):
    """Light (black - s) of the clearest 0.1% of the picture inside the full-scan window, per channel, from a wide
    scan S[c][y][x] (neutral tables).  exclude = (a, b): wide-scan columns holding the blank film that was used for
    calibration (a leader offset into the window): they are left out, because blank film is not picture.  Ignored
    when the blank area covers most of the window (a fully blank frame)."""
    x0, x1 = int(345 / 13) + 2, int(4757 / 13) - 2
    y0, y1 = int(135 / 13) + 2, int(3015 / 13) - 2
    xs = list(range(x0, x1))
    if exclude is not None and (exclude[1] - exclude[0]) < 0.5 * len(xs):
        xs = [x for x in xs if not (exclude[0] - 2 <= x <= exclude[1] + 2)]
    out = []
    for c in range(len(S)):
        vals = sorted(S[c][y][x] for y in range(y0, y1) for x in xs)
        out.append(blacks[c] - vals[int(len(vals) * 0.001)])
    return out


def choose_exposure(dn_cal, light_cal, black, light_frame, headroom=EXPOSURE_HEADROOM, dn_max=DN_MAX):
    """Highest DN at which the frame's clearest area (x headroom) still stays below saturation.
    light_cal: blank-film light at the calibrated DN; light_frame: clearest-area light measured at the same DN."""
    n = dn_cal
    while n > 1 and light_frame * headroom * dn_factor(dn_cal, n) > black - 4.0:
        n -= 1
    while n < dn_max and light_frame * headroom * dn_factor(dn_cal, n + 1) <= black - 4.0:
        n += 1
    return n


def load_calibration(path, channels):
    try:
        data = load_json(path)
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


def decode_rows(rows, nch, channels, cal, row0, sixteen, report=None):
    """Decode y-byte rows to transmittance relative to the local blank film (flat-fielded per row).
    row0 = index of rows[0] in the uncropped scan.  16-bit LE (sixteen) or 8-bit display output."""
    dms, scales = [], []
    for i in channels:
        c = cal[i]
        th = c.get("t_hi", T_HI)
        dms.append(decode_map(cal_black(c), cal_blank(c), build_table(cal_black(c), cal_blank(c), th), th))
        scales.append(c.get("out_scale", OUT_SCALE))
    out = []
    for n, r in enumerate(rows):
        idx = row0 + n
        if report and n % 64 == 0:
            report(n / float(len(rows)))
        if sixteen:
            b = bytearray(len(r) * 2)
            for k, i in enumerate(channels):
                f = row_factor(cal[i], idx)
                key = (id(dms[k]), int(f * 4000), 16, scales[k])
                lut = _LUT_CACHE.get(key)
                if lut is None:
                    pairs = [t16_map(T * (int(f * 4000) / 4000.0), scales[k]) for T in dms[k]]
                    lut = _LUT_CACHE[key] = (bytes(q[0] for q in pairs), bytes(q[1] for q in pairs))
                ch = r[k::nch]
                b[2 * k::2 * nch] = ch.translate(lut[0])
                b[2 * k + 1::2 * nch] = ch.translate(lut[1])
            out.append(bytes(b))
        else:
            b = bytearray(len(r))
            for k, i in enumerate(channels):
                f = row_factor(cal[i], idx)
                key = (id(dms[k]), int(f * 4000), 8, scales[k])
                lut = _LUT_CACHE.get(key)
                if lut is None:
                    lut = _LUT_CACHE[key] = bytes(t8_map(T * (int(f * 4000) / 4000.0), scales[k]) for T in dms[k])
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
        say("  %s: %.2f%% denser than T=%.2f (floor), %.2f%% clearer than %.2fx blank (ceiling)" % (
            names[c], 100.0 * lo / tot, los[c], 100.0 * hi / tot, his[c]))
        if 100.0 * hi / tot > 0.5:
            say("     warning: %s has areas clearer than the calibrated blank film; recalibrate with the "
                  "clearest film available" % names[c])


def main():
    try:
        _main()
    finally:
        UI.close()                      # always give the terminal cursor back


def _main():
    ap = argparse.ArgumentParser(prog="scan", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="scan " + __version__)
    ap.add_argument("command", nargs="?", choices=("scan", "calibrate", "calibrate-sensor", "calibrate-dark"), default="scan")
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
    ap.add_argument("--no-auto-exposure", action="store_true",
                    help="scan at the calibrated exposure instead of measuring the frame first")
    ap.add_argument("--exposure", type=int, metavar="STEPS",
                    help="shift the exposure by STEPS DN steps (x1.26 each, may be negative) relative to the calibrated "
                         "one instead of auto-exposure")
    ap.add_argument("--sensor-profile", metavar="FILE",
                    help="sensor pattern file (default: ./%s, ~/.config/vision35/%s, /usr/local/share/vision35/%s); "
                         "also where `calibrate-sensor` writes (default ~/.config/vision35/%s)" % ((SENSOR_NAME,) * 4))
    ap.add_argument("--no-sensor-profile", action="store_true", help="do not apply the sensor pattern correction")
    ap.add_argument("--plain", action="store_true",
                    help="plain log lines instead of the live two-line status display (also used automatically "
                         "when the output is not a terminal; NO_COLOR turns the colours off)")
    ap.add_argument("--no-trim", action="store_true", help="do not trim the film holder")
    ap.add_argument("--device", help="sg device (default: auto-detect AGFA VISION 35)")
    ap.add_argument("--keep-raw", action="store_true", help="also save <out>.raw + <out>.json (for --from-scan)")
    ap.add_argument("--from-scan", metavar="FILE.raw", help="re-process a saved raw scan instead of scanning")
    a = ap.parse_args()

    UI.configure(live=sys.stdout.isatty() and not a.plain and os.environ.get("TERM") != "dumb",
                 color="NO_COLOR" not in os.environ)
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
        say("scanner: %s on %s" % (iq[8:36].decode("ascii", "replace"), dev))
        return sc

    if a.command == "calibrate-dark":
        sc = open_scanner()
        UI.start(sum([plan_wide((0, 1, 2)) for _ in range(4)], []), "Dark calibration")
        try:
            calibrate_dark(sc, a.sensor_profile or sensor_profile_paths()[1])
        except ScsiError as e:
            sys.exit("error: %s\n(log: %s)" % (e, logpath))
        return

    if a.command == "calibrate-sensor":
        sc = open_scanner()
        UI.start([("prep", "grey", PREP_S), ("G", "G", 75.0), ("B", "B", 110.0), ("analysing", "grey", 12.0)],
                 "Sensor calibration")
        try:
            calibrate_sensor(sc, a.sensor_profile or sensor_profile_paths()[1])
        except ScsiError as e:
            sys.exit("error: %s\n(log: %s)" % (e, logpath))
        return

    if a.command == "calibrate":
        sc = open_scanner()
        UI.start(plan_wide((0, 1, 2)) + [("analysing", "grey", 10.0)], "Calibration")
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
        UI.start([("process", "grey", PROC_S)], "Reprocessing")
        meta = load_json(a.from_scan[:-4] + ".json" if a.from_scan.endswith(".raw") else a.from_scan + ".json")
        channels = tuple(meta["channels"])
        if len(channels) == 1 and not a.mono:
            a.mono = "RGB"[channels[0]]
        cal = {int(k): v for k, v in meta["calibration"].items()}
        if not a.no_sensor_profile:               # a sensor profile measured later can be applied to an old scan
            patterns, _ = load_sensor_profile(a.sensor_profile)
            for i in channels:
                if "RGB"[i] in patterns and "pattern" not in cal[i]:
                    cal[i]["pattern"] = patterns["RGB"[i]]
        raw = read_file(a.from_scan, "rb")
        planes = [[raw[c * SCAN_W * SCAN_LINES + i:c * SCAN_W * SCAN_LINES + i + SCAN_W]
                   for i in range(0, SCAN_W * SCAN_LINES, SCAN_W)] for c in range(len(channels))]
        width = SCAN_W
    else:
        sc = open_scanner()
        try:
            if a.preview:
                UI.start([("prep", "grey", PREP_S), ("preview", "G", 25.0), ("process", "grey", 8.0)], "Preview")
                planes, width = [sc.preview()], PREVIEW_W
            else:
                data, cal = load_calibration(calpath, channels)
                say("calibration %s (%s): %s" % (calpath, data.get("created", "?"), ", ".join(
                    "%s %s black %.1f blank %.1f" % ("RGB"[i], cal[i]["dn"], cal[i]["black"], cal[i]["blank"]) for i in channels)))
                if not a.no_sensor_profile:
                    patterns, ppath = load_sensor_profile(a.sensor_profile)
                    got = [("RGB"[i]) for i in channels if "RGB"[i] in patterns]
                    for i in channels:
                        if "RGB"[i] in patterns:
                            cal[i]["pattern"] = patterns["RGB"[i]]
                    say("sensor pattern correction: %s" % (("%s (%s)" % ("".join(got), ppath)) if got else
                        "none (run `scan calibrate-sensor` with an empty holder to measure it)"))
                dn_scan = {i: dn_number(cal[i]["dn"]) for i in channels}
                if a.exposure is not None:
                    for i in channels:
                        dn_scan[i] = max(1, min(DN_MAX, dn_scan[i] + a.exposure))
                auto = a.exposure is None and not a.no_auto_exposure
                plan = ((plan_wide(channels) if auto else []) + plan_scan(channels, [dn_scan[i] for i in channels])
                        + [("process", "grey", PROC_S if len(channels) > 1 else 22.0)])
                UI.start(plan, "Pre-scan" if auto else "Scanning")
                if auto:
                    S = wide_arrays(sc.wide_scan(channels, [cal[i]["dn"] for i in channels], "Pre-scan: measuring the frame"))
                    lights = frame_clear_levels(S, [cal[i]["black"] for i in channels],
                                                tuple(data["blank_columns_preview"]) if data.get("blank_columns_preview") else None)
                    for k, i in enumerate(channels):
                        L_cal = cal[i]["black"] - cal[i]["blank"]
                        dn_scan[i] = choose_exposure(dn_number(cal[i]["dn"]), L_cal, cal[i]["black"], lights[k])
                        say("  %s: clearest area %.2f x blank film -> %s (calibrated %s)" % (
                            "RGB"[i], lights[k] / L_cal, dn_name(dn_scan[i]), cal[i]["dn"]))
                    UI.reweight(len(plan_wide(channels)) + 1, [est_pass_seconds(i, dn_scan[i]) for i in channels])
                dark = {}
                if not a.no_sensor_profile:
                    dark, dpath = load_dark_levels(a.sensor_profile)
                    say("dark levels: %s" % (("%s" % dpath) if dark else
                        "none (run `scan calibrate-dark` with the aperture covered; the holder reading is used)"))
                for i in channels:      # per-scan encoding parameters; stored with --keep-raw so the scan decodes exactly
                    n0 = dn_number(cal[i]["dn"])
                    if "RGB"[i] in dark:   # true zero-light level (foil), not the holder, whose reading stray light lowers
                        b_cal = dark_level(dark["RGB"[i]], n0); b_scan = dark_level(dark["RGB"[i]], dn_scan[i])
                    else:
                        b_cal = b_scan = cal[i]["black"]
                    L_virtual = (b_cal - cal[i]["blank"]) * dn_factor(n0, dn_scan[i])
                    cal[i]["dn_scan"] = dn_scan[i]
                    cal[i]["black_scan"] = round(b_scan, 3)
                    cal[i]["blank_scan"] = b_scan - L_virtual
                    cal[i]["t_hi"] = round(min(T_HI_MAX, (b_scan - 3.0) / L_virtual), 4)
                    cal[i]["out_scale"] = OUT_SCALE_NEW
                tables = [build_table(cal_black(cal[i]), cal_blank(cal[i]), cal[i]["t_hi"]) for i in channels]
                dns = [dn_name(dn_scan[i]) for i in channels]
                planes, width = sc.full_scan(channels, dns, tables), SCAN_W
        except ScsiError as e:
            sys.exit("error: %s\n(log: %s)" % (e, logpath))

    names = "".join("RGB"[i] for i in channels)
    nch = len(planes)
    UI.next("process")
    UI.stage, UI.ctx, UI.chan = "Processing", "", ""
    UI.progress(0.0, "assembling the picture")
    w, h, rows = assemble(planes, width)
    value = 255 if a.preview else 0               # preview table saturates the holder to 255; scans floor it at 0
    row0 = 0
    if not a.no_trim:
        UI.progress(0.12, "trimming the film holder")
        box = holder_trim(w, h, rows, nch, value)
        say("holder trim: x %d..%d, y %d..%d of %dx%d" % (box[0], box[2], box[1], box[3], w, h))
        rows = crop(rows, nch, box)
        row0 = box[1]
        w, h = box[2] - box[0], box[3] - box[1]

    bps = 8
    if a.preview:
        rows = auto_levels(rows, nch, 1.0)        # mild software stretch on top of the fixed preview table
    else:
        clip_report(rows, nch, names, [t_lo(cal_black(cal[i]), cal_blank(cal[i])) for i in channels],
                    [cal[i].get("t_hi", T_HI) for i in channels])
        UI.progress(0.2, "decoding to transmittance")
        rows = decode_rows(rows, nch, channels, cal, row0, sixteen,
                           lambda f: UI.progress(0.2 + 0.55 * f, "decoding to transmittance"))
        if sixteen:
            bps = 16
        else:
            if a.invert:
                rows = invert(rows)
            if a.auto_levels:
                rows = auto_levels(rows, nch, a.gamma)
    UI.progress(0.78, "orienting the picture")
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
    UI.progress(0.85, "writing %s" % os.path.basename(out))
    write_tiff(out, w, h, rows, nch, bps)
    if a.keep_raw and not a.from_scan and not a.preview:
        with open(out + ".raw", "wb") as f:
            for p in planes:
                f.write(b"".join(p))
        save_json({"channels": list(channels), "calibration": {str(k): v for k, v in cal.items()}}, out + ".json")
    UI.finish("saved %s" % os.path.basename(out))
    say("wrote %s (%dx%d, %s %d-bit%s)" % (out, w, h, "RGB" if nch == 3 else "grey", bps,
                                           ", linear transmittance" if sixteen else ""))


if __name__ == "__main__":
    main()

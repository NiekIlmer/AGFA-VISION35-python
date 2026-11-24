# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Niek Ilmer <niek@beunie.nl>
"""Hardware-free tests for scan.py.  Run:  python3 -m unittest discover -s tests -v"""
import importlib.util
import math
import os
import random
import struct
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("scan", os.path.join(ROOT, "scan.py"))
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


def synthetic_wide_scan(kind, seed=1):
    """Fake 385x385 wide scan (S[c][y][x], raw signal s: holder ~253, clearer film lower).
    kind: 'band' (picture + sharp-edged blank gap), 'picture' (no blank film), 'blank' (whole film blank),
    'smooth_edge' (picture whose right edge is smooth and dark: must NOT be taken as blank film)."""
    rnd = random.Random(seed)
    H = W = 385
    fx0, fx1, fy0, fy1 = 22, 372, 6, 238          # film area, holder around it
    light = (130.0, 55.0, 40.0)                    # blank film light per channel (R, G, B) at DN01

    def blank_level(c, y):                         # lamp falloff across the film width (rows)
        fall = 0.88 + 0.12 * (y - fy0) / float(fy1 - fy0)
        return 253.0 - light[c] * fall

    S = [[[253 for _ in range(W)] for _ in range(H)] for _ in range(3)]
    for y in range(fy0, fy1):
        for x in range(fx0, fx1):
            for c in range(3):
                if kind == "blank":
                    v = blank_level(c, y) + rnd.gauss(0, 3)
                else:
                    in_band = kind == "band" and 90 <= x <= 108
                    if in_band:
                        v = blank_level(c, y) + rnd.gauss(0, 3)
                    else:
                        # picture: smooth, strongly varying content, always denser (higher s) than the blank film
                        base = blank_level(c, y)
                        tex = 0.5 + 0.5 * math.sin(x / 9.0 + c) * math.cos(y / 7.0)
                        v = base + (253 - base) * (0.25 + 0.6 * tex) + rnd.gauss(0, 3)
                        if kind == "smooth_edge" and x > 290:
                            # dark (clear) smooth area that fades in gradually: not a gap between frames
                            k = min(1.0, (x - 290) / 25.0)
                            v = (1 - k) * v + k * (blank_level(c, y) + 4 + rnd.gauss(0, 3))
                S[c][y][x] = int(max(0, min(252, round(v))))
    return S


class TableTests(unittest.TestCase):
    CASES = ((253.2, 120.4), (253.1, 43.4), (253.5, 117.7), (253.3, 185.7))

    def test_table_shape(self):
        for black, blank in self.CASES:
            t = scan.build_table(black, blank, 1.3)
            self.assertEqual(len(t), 256)
            self.assertTrue(all(isinstance(v, int) and 0 <= v <= 255 for v in t))
            self.assertTrue(all(t[i] >= t[i + 1] for i in range(255)), "must not increase with signal s")

    def test_roundtrip_density_error(self):
        """Simulate the scanner applying the table (sub-index signal, linear interpolation) and decode it."""
        rnd = random.Random(5)
        for black, blank in self.CASES:
            for t_hi in (scan.T_HI, 1.3):
                t_hi = min(t_hi, (black - 3.0) / (black - blank))      # as scan does: the sensor cannot go below 0
                t = scan.build_table(black, blank, t_hi)
                dm = scan.decode_map(black, blank, t, t_hi)
                lo = scan.t_lo(black, blank)
                worst = 0.0
                for _ in range(4000):
                    T = math.exp(rnd.uniform(math.log(lo * 3), math.log(t_hi * 0.97)))
                    s = black - T * (black - blank)
                    i = int(s); f = s - i
                    y = int(round(t[i] + (t[min(i + 1, 255)] - t[i]) * f))
                    worst = max(worst, abs(math.log10(dm[y] / T)))
                self.assertLess(worst, 0.02, (black, blank, t_hi))

    def test_decode_endpoints(self):
        t = scan.build_table(253.0, 100.0, 1.3)
        dm = scan.decode_map(253.0, 100.0, t, 1.3)
        self.assertAlmostEqual(dm[255], 1.3)
        self.assertAlmostEqual(dm[0], scan.t_lo(253.0, 100.0))
        self.assertTrue(all(dm[i] <= dm[i + 1] + 1e-9 for i in range(255)))

    def test_floor_adapts_to_light_range(self):
        self.assertLess(scan.t_lo(253.0, 30.0), scan.t_lo(253.0, 180.0))
        self.assertLessEqual(scan.t_lo(253.0, 250.0), 0.02)
        self.assertGreaterEqual(scan.t_lo(253.0, 0.0), scan.T_LO_MIN)

    def test_16bit_scaling(self):
        lo, hi = scan.t16_map(1.0, 0.75)
        self.assertEqual(lo | (hi << 8), round(0.75 * 65535))
        lo, hi = scan.t16_map(5.0, 0.75)                       # clamps instead of wrapping
        self.assertEqual(lo | (hi << 8), 65535)


class DetectTests(unittest.TestCase):
    def test_finds_sharp_gap(self):
        reg = scan.detect_regions(synthetic_wide_scan("band"))
        a, b = reg["blank_cols"]
        self.assertTrue(88 <= a <= 94 and 104 <= b <= 110, (a, b))
        self.assertFalse(reg.get("whole_film"))
        S = synthetic_wide_scan("band")
        black, blank, profs = scan.levels_at(S, reg)
        for c, light in enumerate((130.0, 55.0, 40.0)):
            self.assertAlmostEqual(black[c], 253.0, delta=0.6)
            self.assertAlmostEqual(black[c] - blank[c], light, delta=0.1 * light)

    def test_flat_field_profile_follows_falloff(self):
        S = synthetic_wide_scan("band")
        reg = scan.detect_regions(S)
        black, blank, profs = scan.levels_at(S, reg)
        p = profs[1]
        lo_light = black[1] - scan.prof_level(p, p["k0"] + 2)
        hi_light = black[1] - scan.prof_level(p, p["k1"] - 2)
        self.assertGreater(hi_light, lo_light)                       # synthetic lamp is dimmer at the first rows
        self.assertAlmostEqual(lo_light / hi_light, 0.88 / 1.0, delta=0.05)

    def test_whole_film_blank(self):
        reg = scan.detect_regions(synthetic_wide_scan("blank"))
        self.assertTrue(reg.get("whole_film"))

    def test_picture_without_blank_film_is_rejected(self):
        with self.assertRaises(ValueError):
            scan.detect_regions(synthetic_wide_scan("picture"))

    def test_smooth_dark_picture_edge_is_not_blank_film(self):
        with self.assertRaises(ValueError):
            scan.detect_regions(synthetic_wide_scan("smooth_edge"))

    def test_no_holder_is_an_error(self):
        S = [[[120 for _ in range(385)] for _ in range(385)] for _ in range(3)]
        with self.assertRaises(ValueError):
            scan.detect_regions(S)


class ImageTests(unittest.TestCase):
    def test_orient_operations(self):
        w, h, ps = 3, 2, 2
        rows = [bytes(v for x in range(w) for v in (100 * y + 10 * x, 100 * y + 10 * x + 1)) for y in range(h)]

        def px(rs, ww, hh):
            return [[rs[y][x * ps] for x in range(ww)] for y in range(hh)]
        self.assertEqual(px(*scan.orient(rows, w, h, ps, flip_v=True)[:1], w, h), [[100, 110, 120], [0, 10, 20]])
        r, ww, hh = scan.orient(rows, w, h, ps, flip_h=True)
        self.assertEqual(px(r, ww, hh), [[20, 10, 0], [120, 110, 100]])
        r, ww, hh = scan.orient(rows, w, h, ps, rot=180)
        self.assertEqual(px(r, ww, hh), [[120, 110, 100], [20, 10, 0]])
        r, ww, hh = scan.orient(rows, w, h, ps, rot=90)
        self.assertEqual((ww, hh), (2, 3))
        self.assertEqual(px(r, ww, hh), [[100, 0], [110, 10], [120, 20]])
        r, ww, hh = scan.orient(rows, w, h, ps, rot=270)
        self.assertEqual(px(r, ww, hh), [[20, 120], [10, 110], [0, 100]])
        # pixel bytes stay together
        r, _, _ = scan.orient(rows, w, h, ps, flip_h=True)
        self.assertEqual(r[0][1], r[0][0] + 1)

    def test_holder_trim(self):
        w, h, nch = 20, 12, 3
        rows = []
        for y in range(h):
            r = bytearray()
            for x in range(w):
                holder = x < 3 or x >= 17 or y < 2 or y >= 10
                r += bytes((0, 0, 0) if holder else (50, 60, 70))
            rows.append(bytes(r))
        x0, y0, x1, y1 = scan.holder_trim(w, h, rows, nch, 0, margin=0)
        self.assertEqual((x0, y0, x1, y1), (3, 2, 17, 10))
        # an image without holder is untouched
        full = [bytes((50, 60, 70) * w) for _ in range(h)]
        self.assertEqual(scan.holder_trim(w, h, full, nch, 0), (0, 0, w, h))

    def test_assemble_transposes_lines(self):
        width = 5                                           # 4 samples + pad byte
        lines = [bytes([10 * x + y for y in range(4)] + [23]) for x in range(3)]
        w, h, rows = scan.assemble([lines], width)
        self.assertEqual((w, h), (3, 4))
        self.assertEqual(rows[2], bytes([2, 12, 22]))       # row y=2: sample 2 of each line

    def test_next_numbered(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(scan.next_numbered(d).endswith("/1.tiff"))
            for n in ("1.tiff", "2.tiff", "7.tiff", "preview-3.tiff", "notes.txt"):
                open(os.path.join(d, n), "w").close()
            self.assertTrue(scan.next_numbered(d).endswith("/8.tiff"))
            self.assertTrue(scan.next_numbered(d, "preview-").endswith("/preview-4.tiff"))

    def test_tiff_writer_roundtrip(self):
        w, h = 7, 4
        for nch, bps in ((3, 16), (1, 8), (3, 8)):
            bpp = bps // 8
            rows = [bytes((x * 13 + y * 7 + k) & 0xFF for x in range(w * nch * bpp) for k in (0,)) for y in range(h)]
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "t.tiff")
                scan.write_tiff(path, w, h, rows, nch, bps)
                with open(path, "rb") as fh:
                    b = fh.read()
            self.assertEqual(b[:4], b"II*\x00")
            off = struct.unpack("<I", b[4:8])[0]
            n = struct.unpack("<H", b[off:off + 2])[0]
            tags = {}
            for i in range(n):
                tag, typ, cnt, val = struct.unpack("<HHII", b[off + 2 + 12 * i:off + 14 + 12 * i])
                tags[tag] = (typ, cnt, val if not (typ == 3 and cnt == 1) else val & 0xFFFF)
            self.assertEqual(tags[256][2], w)
            self.assertEqual(tags[257][2], h)
            self.assertEqual(tags[277][2], nch)
            self.assertEqual(tags[262][2], 2 if nch == 3 else 1)
            self.assertEqual(tags[259][2], 1)                                   # uncompressed
            start, count = tags[273][2], tags[279][2]
            self.assertEqual(count, w * h * nch * bpp)
            self.assertEqual(b[start:start + count], b"".join(rows))
            self.assertEqual(len(b), start + count)

    def test_auto_levels_stretches(self):
        rows = [bytes(random.Random(i).randint(40, 90) for _ in range(30)) for i in range(20)]
        out = scan.auto_levels(rows, 1)
        flat = b"".join(out)
        self.assertLessEqual(min(flat), 5)
        self.assertGreaterEqual(max(flat), 250)


class DecodeTests(unittest.TestCase):
    def make_cal(self, **kw):
        c = {"dn": "DN01", "black": 253.0, "blank": 100.0, "t_hi": 1.3, "out_scale": 0.75,
             "profile": {"k0": 10, "k1": 230, "vals": [100.0] * 221}}
        c.update(kw)
        return c

    def test_decode_rows_16bit(self):
        cal = {0: self.make_cal()}
        tbl = scan.build_table(253.0, 100.0, 1.3)
        i_blank = int(round(100.0))                              # raw signal of blank film
        y_blank = tbl[i_blank]
        rows = [bytes([y_blank, 255, 0] * 4) for _ in range(5)]
        out = scan.decode_rows(rows, 1, (0,), cal, 0, True)
        self.assertEqual(len(out[0]), len(rows[0]) * 2)
        v = out[0][0] | (out[0][1] << 8)
        self.assertAlmostEqual(v / (0.75 * 65535), 1.0, delta=0.03)            # blank film -> 1.0
        self.assertEqual(out[0][2] | (out[0][3] << 8), round(1.3 * 0.75 * 65535))  # ceiling byte -> T_hi

    def test_row_factor_flat_field(self):
        vals = [100.0 + 0.2 * i for i in range(221)]            # light falls toward larger rows
        cal = self.make_cal(profile={"k0": 10, "k1": 230, "vals": vals})
        f_first = scan.row_factor(cal, 0)
        f_last = scan.row_factor(cal, 2880)
        self.assertGreaterEqual(f_first, 1.0)
        self.assertGreater(f_last, f_first)
        self.assertEqual(scan.row_factor({"black": 253.0, "blank": 100.0}, 5), 1.0)   # no profile -> no correction

    def test_legacy_calibration_defaults(self):
        c = {"black": 253.0, "blank": 100.0}
        self.assertEqual(c.get("t_hi", scan.T_HI), 1.12)
        self.assertEqual(c.get("out_scale", scan.OUT_SCALE), 0.85)


class SafetyTests(unittest.TestCase):
    def test_text_whitelist(self):
        ok = ["APIN", "OUT8", "AF,R", "MP", "CP", "GML8", "IF15", "INM", "SPUP", "NHD", "AP50", "AVOF", "SDON",
              "DN01", "DN04", "BRT0", "CNT0", "SFT0", "PSOF", "PVOF", "FR,R", "FG,R", "FB,R", "GM1T8", "GM2T8",
              "GM3T8", "MV0,R", "MV345,R", scan.PREVIEW_BXZ, scan.SCAN_BXZ]
        for t in ok:
            self.assertTrue(scan.ALLOWED_TEXT.match(t), t)
        for t in ["DN02", "MV100,R", "BXZ 0,0,100,100,1,1,V", "FORMAT", "FW", "WGM1T,1,2,3", "APIN;PSOF", "apin"]:
            self.assertFalse(scan.ALLOWED_TEXT.match(t), t)

    def test_only_safe_scsi_opcodes(self):
        self.assertEqual(scan.ALLOWED_OPS, {0x12, 0x00, 0x03, 0x2A, 0x28})
        sc = object.__new__(scan.Scanner)                     # no device: the guard must trigger before any I/O
        sc.n = 0
        for op in (0x3B, 0x04, 0x08, 0x1A, 0x3C, 0xC0):       # WRITE BUFFER, FORMAT, READ(6), MODE SENSE, ...
            with self.assertRaises(scan.ScsiError):
                sc.cmd([op, 0, 0, 0, 0, 0])

    def test_text_command_guard(self):
        sc = object.__new__(scan.Scanner)
        sc.n = 0
        with self.assertRaises(scan.ScsiError):
            sc.send("WGM1T,0,0,0")
        with self.assertRaises(scan.ScsiError):
            sc.send_table(4, list(range(256)))
        with self.assertRaises(scan.ScsiError):
            sc.send_table(1, list(range(255)))
        with self.assertRaises(scan.ScsiError):
            sc.send_table(1, [256] + list(range(255)))

    def test_calibration_loading_errors(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(scan.ScsiError) as cm:
                scan.load_calibration(os.path.join(d, "calibration.json"), (0, 1, 2))
            self.assertIn("scan calibrate", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Niek Ilmer <niek@beunie.nl>
"""Hardware-free tests for scan.py.  Run:  python3 -m unittest discover -s tests -v"""
import importlib.util
import math
import os
import random
import struct
import tempfile
import time
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


class ExposureTests(unittest.TestCase):
    def test_dn_gain_law(self):
        self.assertAlmostEqual(scan.dn_factor(1, 4), 2.0, delta=0.01)           # measured: DN04 = 2.0 x DN01
        self.assertAlmostEqual(scan.dn_factor(1, 6), 10 ** 0.5, places=6)       # measured ~3.2
        self.assertAlmostEqual(scan.dn_factor(6, 1) * scan.dn_factor(1, 6), 1.0)
        self.assertAlmostEqual(scan.dn_factor(3, 4), 10 ** 0.1)
        self.assertEqual(scan.dn_number("DN06"), 6)
        self.assertEqual(scan.dn_name(6), "DN06")

    def test_choose_exposure_limited_by_frame(self):
        black = 253.0
        # clearest area of the frame is only 0.25 x blank: plenty of room above the calibrated DN
        n = scan.choose_exposure(3, 100.0, black, 25.0)
        self.assertGreater(n, 3)
        self.assertLessEqual(25.0 * scan.EXPOSURE_HEADROOM * scan.dn_factor(3, n), black - 4.0)
        self.assertGreater(25.0 * scan.EXPOSURE_HEADROOM * scan.dn_factor(3, n + 1), black - 4.0) if n < scan.DN_MAX else None

    def test_choose_exposure_never_above_dn_max_or_below_one(self):
        self.assertEqual(scan.choose_exposure(3, 100.0, 253.0, 0.5), scan.DN_MAX)
        self.assertEqual(scan.choose_exposure(1, 100.0, 253.0, 400.0), 1)       # saturated even at DN01
        self.assertEqual(scan.choose_exposure(5, 100.0, 253.0, 400.0), 1)

    def test_choose_exposure_keeps_calibrated_dn_when_frame_is_clear(self):
        # a frame whose clearest area is as clear as the blank film (x1.3 headroom is already used up)
        n = scan.choose_exposure(4, 190.0, 253.0, 190.0)       # calibration would stop at DN04 for 190 units of light
        self.assertEqual(n, 4)

    def test_virtual_blank_roundtrip(self):
        """Scan at a DN different from the calibrated one: table and decode use the gain-scaled 'virtual' blank film."""
        rnd = random.Random(11)
        black, blank_cal, dn_cal = 253.0, 150.0, 2        # blank film light 103 at the calibrated DN02
        for dn_scan in (1, 2, 5, 9):
            L_virtual = (black - blank_cal) * scan.dn_factor(dn_cal, dn_scan)
            blank_scan = black - L_virtual
            t_hi = min(scan.T_HI_MAX, (black - 3.0) / L_virtual)
            tbl = scan.build_table(black, blank_scan, t_hi)
            dm = scan.decode_map(black, blank_scan, tbl, t_hi)
            lo = scan.t_lo(black, blank_scan)
            worst = 0.0
            for _ in range(3000):
                T = math.exp(rnd.uniform(math.log(lo * 4), math.log(t_hi * 0.95)))
                s = black - T * L_virtual                 # what the scanner reports at this DN
                i = int(s); f = s - i
                y = int(round(tbl[i] + (tbl[min(i + 1, 255)] - tbl[i]) * f))
                worst = max(worst, abs(math.log10(dm[y] / T)))
            self.assertLess(worst, 0.025, (dn_scan, worst))

    def test_higher_exposure_extends_the_dense_end(self):
        black, blank_cal = 253.0, 150.0
        lo_low = scan.t_lo(black, black - (black - blank_cal))
        lo_high = scan.t_lo(black, black - (black - blank_cal) * scan.dn_factor(1, 7))
        self.assertLess(lo_high, lo_low)                           # the floor of the encoded range gets denser

    def test_cal_blank_default(self):
        self.assertEqual(scan.cal_blank({"black": 253.0, "blank": 100.0}), 100.0)
        self.assertEqual(scan.cal_blank({"black": 253.0, "blank": 100.0, "blank_scan": 40.0}), 40.0)

    def test_frame_clear_levels_reads_only_the_scan_window(self):
        # wide scan with a very clear stripe OUTSIDE the scan window (columns 5..15) and a dimmer picture inside
        H = W = 385
        S = [[[200 for _ in range(W)] for _ in range(H)]]
        for y in range(H):
            for x in range(5, 16):
                S[0][y][x] = 30                               # blank leader outside the scan window
        light = scan.frame_clear_levels(S, [253.0])[0]
        self.assertAlmostEqual(light, 253.0 - 200, delta=1.0)  # not the stripe

    def test_frame_clear_levels_excludes_blank_leader_inside_the_window(self):
        # blank leader (very clear) at wide-scan columns 34..50, INSIDE the scan window; picture is dimmer
        H = W = 385
        S = [[[200 for _ in range(W)] for _ in range(H)]]
        for y in range(H):
            for x in range(34, 51):
                S[0][y][x] = 30
        with_leader = scan.frame_clear_levels(S, [253.0])[0]
        without = scan.frame_clear_levels(S, [253.0], (34, 50))[0]
        self.assertAlmostEqual(with_leader, 253.0 - 30, delta=1.0)    # fooled by the leader
        self.assertAlmostEqual(without, 253.0 - 200, delta=1.0)       # picture only
        # a fully blank frame: the 'blank area' covers most of the window, so nothing is excluded
        S2 = [[[60 for _ in range(W)] for _ in range(H)]]
        self.assertAlmostEqual(scan.frame_clear_levels(S2, [253.0], (30, 360))[0], 253.0 - 60, delta=1.0)


class SensorPatternTests(unittest.TestCase):
    def synthetic_lines(self, n=400, count=600, seed=3):
        rnd = random.Random(seed)
        true = [1.0 + 0.02 * math.sin(i * 0.9) + (0.03 if i == 150 else 0.0) for i in range(n)]
        lines = []
        for _ in range(count):
            ln = bytearray()
            for y in range(n):
                light = 200.0 * true[y] * (1 + rnd.gauss(0, 0.003))
                ln.append(max(0, min(255, int(round(253.1 - light)))))
            ln.append(23)                                     # pad byte
            lines.append(bytes(ln))
        return true, lines

    def test_pattern_recovers_multiplicative_response(self):
        true, lines = self.synthetic_lines()
        pat, sat = scan.sensor_pattern(lines)
        self.assertEqual(len(pat), 400)
        self.assertEqual(sat, 0.0)
        k = 15
        expect = []
        for i in range(400):
            half = min(k, i, 399 - i)
            seg = true[i - half:i + half + 1]
            expect.append(true[i] / (sum(seg) / len(seg)))
        err = max(abs(a - b) for a, b in zip(pat, expect))
        self.assertLess(err, 0.008)
        self.assertGreater(pat[150], 1.02)                    # the single-sample spike is found

    def test_saturated_scan_is_reported(self):
        lines = [bytes([0] * 400 + [23]) for _ in range(600)]
        pat, sat = scan.sensor_pattern(lines)
        self.assertEqual(sat, 1.0)

    def test_row_factor_applies_pattern(self):
        pat = [1.0] * 100
        pat[7] = 1.03
        c = {"black": 253.0, "blank": 100.0, "pattern": pat}
        self.assertAlmostEqual(scan.row_factor(c, 7), 1 / 1.03)
        self.assertAlmostEqual(scan.row_factor(c, 8), 1.0)
        self.assertAlmostEqual(scan.row_factor(c, 5000), 1.0)  # outside the pattern: no correction

    def test_load_sensor_profile(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sp.json")
            self.assertEqual(scan.load_sensor_profile(path), ({}, None))
            with open(path, "w") as fh:
                fh.write('{"channels": {"G": {"pattern": [1.0, 1.01]}, "B": {"pattern": [0.99, 1.0]}, "R": {}}}')
            prof, found = scan.load_sensor_profile(path)
            self.assertEqual(found, path)
            self.assertEqual(sorted(prof), ["B", "G"])             # red has no pattern
            self.assertEqual(prof["G"], [1.0, 1.01])


class DarkLevelTests(unittest.TestCase):
    def test_dark_level_interpolates_and_clamps(self):
        lv = {"DN00": 253.38, "DN04": 253.72, "DN08": 253.69, "DN12": 253.82}
        self.assertAlmostEqual(scan.dark_level(lv, 0), 253.38)
        self.assertAlmostEqual(scan.dark_level(lv, 2), (253.38 + 253.72) / 2)
        self.assertAlmostEqual(scan.dark_level(lv, 12), 253.82)
        self.assertAlmostEqual(scan.dark_level(lv, 20), 253.82)            # beyond the last measured step
        self.assertAlmostEqual(scan.dark_level({"DN04": 253.5}, 1), 253.5)  # a single measurement

    def test_cal_black_prefers_the_measured_dark_level(self):
        self.assertEqual(scan.cal_black({"black": 253.2}), 253.2)
        self.assertEqual(scan.cal_black({"black": 253.2, "black_scan": 253.74}), 253.74)

    def test_dark_levels_from_wide(self):
        S = [[[253 for _ in range(385)] for _ in range(385)] for _ in range(2)]
        for y in range(385):
            for x in range(385):
                S[1][y][x] = 254
        m = scan.dark_levels_from_wide(S)
        self.assertAlmostEqual(m[0], 253.0)
        self.assertAlmostEqual(m[1], 254.0)

    def test_load_dark_levels(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sp.json")
            self.assertEqual(scan.load_dark_levels(path), ({}, None))
            with open(path, "w") as fh:
                fh.write('{"channels": {}, "dark": {"G": {"DN00": 253.4, "DN04": 253.7}}}')
            dark, found = scan.load_dark_levels(path)
            self.assertEqual(found, path)
            self.assertEqual(dark["G"]["DN04"], 253.7)

    def test_floor_goes_denser_with_exposure(self):
        lo_dn1 = scan.t_lo(253.5, 253.5 - 220.0)                       # blank light 220 at DN01
        lo_dn10 = scan.t_lo(253.5, 253.5 - 220.0 * scan.dn_factor(1, 10))
        self.assertGreater(lo_dn1, lo_dn10)
        self.assertLess(lo_dn10, 0.002)                                  # the old 0.002 cap no longer limits it


ANSI = __import__("re").compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text):
    return ANSI.sub("", text)


class DisplayTests(unittest.TestCase):
    def make(self, live=True, color=True, cols=80):
        import io

        class Out(io.StringIO):
            encoding = "utf-8"                               # the display looks at the stream's encoding
        out = Out()
        d = scan.Display(out)
        d.configure(live=live, color=color, cols=cols)
        return d, out

    def plan(self):
        return [("prep", "grey", 10.0), ("R", "R", 30.0), ("G", "G", 30.0), ("B", "B", 30.0)]

    def test_alloc_sums_and_keeps_every_section(self):
        for weights in ([10, 30, 30, 30], [1, 1000, 1], [14.0, 75.0, 75.0, 75.0, 40.0], [5, 5]):
            for total in (20, 79, 120):
                cells = scan.Display.alloc(weights, total)
                self.assertEqual(sum(cells), total, (weights, total))
                self.assertTrue(all(c >= 1 for c in cells), (weights, total, cells))
        cells = scan.Display.alloc([10, 30, 30, 30], 100)
        self.assertEqual(cells, [10, 30, 30, 30])

    def test_two_lines_of_the_right_width(self):
        d, out = self.make(cols=80)
        d.start(self.plan(), "Scanning")
        d.next("prep")
        d.next()
        d.chan, d.ctx = "R", "red pass 1/3 at DN03"
        d.lines(50, 100)
        d._render()
        last = out.getvalue().split("\x1b[2A")[-1]
        rows = [strip_ansi(r) for r in last.replace("\r", "").split("\n") if strip_ansi(r) != ""]
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertLessEqual(len(r), 79)
        self.assertIn("Scanning", rows[0])
        self.assertIn("red pass 1/3 at DN03", rows[0])
        self.assertIn("line 50/100", rows[0])
        self.assertIn("%", rows[0])
        self.assertEqual(len(rows[1]), 79)

    def test_sections_are_coloured_by_channel(self):
        d, out = self.make(cols=100)
        d.start(self.plan(), "Scanning")
        d.next("prep")
        d.next()
        d.progress(0.5)
        d._render()
        bar = out.getvalue().split("\n")[-2]
        self.assertIn("\x1b[91m", bar)                         # red section (bright = filled)
        self.assertIn("\x1b[31m", bar)                         # same hue, dim = not yet scanned
        self.assertIn("\x1b[92m", bar)                         # green present (unfilled shows dim)
        self.assertIn("\x1b[34m", bar)                         # blue, dim
        plain = strip_ansi(bar).replace("\r", "")
        # the red section is half full: grey prep is complete, G and B empty
        self.assertEqual(plain.count("█"), 10 + 15)             # 10 prep cells + half of the 30 red cells at width 99?
        d.finish("done")

    def test_progress_bar_fraction_and_eta(self):
        d, out = self.make()
        d.start(self.plan(), "Scanning")
        d.next("prep")
        d.next()
        d.progress(1.0)
        self.assertAlmostEqual(d.overall(), (10 + 30) / 100.0)
        d.next()
        d.progress(0.5)
        self.assertAlmostEqual(d.overall(), (10 + 30 + 15) / 100.0)
        self.assertEqual(scan.Display.fmt(75), "1:15")
        self.assertEqual(scan.Display.fmt(3725), "1:02:05")

    def test_say_keeps_the_display_at_the_bottom(self):
        d, out = self.make()
        d.start(self.plan(), "Scanning")
        d.say("a note")
        text = out.getvalue()
        i = text.index("a note")
        self.assertIn("\x1b[2A\r\x1b[J", text[:i + 40])        # the old two lines were erased before the note
        self.assertTrue(text[i:].count("\n") >= 3)              # note + a fresh two-line display after it

    def test_finish_leaves_a_complete_display_and_normal_output_after(self):
        d, out = self.make()
        d.start(self.plan(), "Scanning")
        d.finish("saved 1.tiff")
        self.assertAlmostEqual(d.overall(), 1.0)
        n_before = out.getvalue().count("\x1b[2A")
        d.say("wrote 1.tiff")
        self.assertEqual(out.getvalue().count("\x1b[2A"), n_before)     # no erasing after the end
        self.assertTrue(out.getvalue().rstrip().endswith("wrote 1.tiff"))
        self.assertIn("\x1b[?25h", out.getvalue())                       # cursor restored

    def test_plain_mode_prints_only_log_lines(self):
        d, out = self.make(live=False)
        d.start(self.plan(), "Scanning")
        d.next("prep")
        d.next()
        d.chan = "R"
        for i in range(1, 401):
            d.lines(i, 400)
        d.say("done")
        text = out.getvalue()
        self.assertNotIn("\x1b", text)
        self.assertIn("R line 200/400", text)
        self.assertIn("R line 400/400", text)
        self.assertTrue(text.rstrip().endswith("done"))

    def test_no_color_mode(self):
        d, out = self.make(color=False, cols=60)
        d.start(self.plan(), "Scanning")
        d.next("prep")
        d.next()
        d.progress(0.5)
        d._render()
        bar = out.getvalue().split("\n")[-2]
        self.assertNotIn("\x1b[9", bar)                        # no colour codes in the bar
        self.assertIn("|", bar)                                  # section boundaries are marked instead

    def test_grey_sections_advance_with_time_until_reported(self):
        d, out = self.make()
        d.start([("prep", "grey", 20.0), ("R", "R", 60.0)], "Scanning")
        d.next("prep")
        seg = d.segs[0]
        seg["t0"] = time.time() - 10.0                            # halfway through its expected 20 s
        self.assertAlmostEqual(d._eff(seg), 0.5, delta=0.05)
        seg["t0"] = time.time() - 500.0
        self.assertEqual(d._eff(seg), 0.95)                       # never complete by itself
        d.progress(0.25)                                          # an explicit report takes over
        self.assertEqual(d._eff(seg), 0.25)
        d.next()
        self.assertEqual(d._eff(seg), 1.0)
        self.assertEqual(d._eff({"p": 0.3, "kind": "R", "w": 5.0, "t0": time.time() - 100}), 0.3)   # coloured: explicit only

    def test_finish_says_done(self):
        d, out = self.make()
        d.start(self.plan(), "Processing")
        d.finish("saved 3.tiff")
        self.assertIn("Done — saved 3.tiff", strip_ansi(out.getvalue()))

    def test_extend_and_reweight(self):
        d, out = self.make()
        d.start(self.plan(), "Scanning")
        d.extend([("analysing", "grey", 10.0)])
        self.assertEqual(len(d.segs), 5)
        d.reweight(1, [60.0, 60.0])
        self.assertEqual(d.segs[1]["w"], 60.0)
        self.assertEqual(d.segs[3]["w"], 30.0)                   # untouched

    def test_extra_next_calls_are_harmless(self):
        d, out = self.make()
        d.start([("prep", "grey", 5.0)], "x")
        for _ in range(4):
            d.next()
        d.progress(0.5)
        d.lines(1, 2)

    def test_friendly_status_text(self):
        self.assertEqual(scan.friendly("AF,R"), "autofocusing")
        self.assertEqual(scan.friendly("MV345,R"), "moving the carriage")
        self.assertEqual(scan.friendly("FG,R"), "selecting the green filter")
        self.assertIn("DN04", scan.friendly("DN04"))
        self.assertEqual(scan.friendly("BXZ 0,0,4999,4999,13,13,V"), "starting the scan")
        for cmd in ("APIN", "MP", "IF15", "SPUP", "PSOF"):
            self.assertTrue(scan.friendly(cmd))

    def test_plans_have_the_expected_sections(self):
        self.assertEqual([k for _, k, _ in scan.plan_wide((0, 1, 2))], ["grey", "R", "G", "B"])
        plan = scan.plan_scan((1,), [5])
        self.assertEqual([k for _, k, _ in plan], ["grey", "G"])
        self.assertGreater(scan.est_pass_seconds(0, 4), scan.est_pass_seconds(0, 1))    # red slows down with exposure
        self.assertAlmostEqual(scan.est_pass_seconds(1, 1), scan.SCAN_LINES * 0.017)    # green stays at the line-time floor


class SafetyTests(unittest.TestCase):
    def test_text_whitelist(self):
        ok = ["APIN", "OUT8", "AF,R", "MP", "CP", "GML8", "IF15", "INM", "SPUP", "NHD", "AP50", "AVOF", "SDON",
              "DN00", "DN01", "DN04", "DN02", "DN06", "DN09", "DN12", "BRT0", "CNT0", "SFT0", "PSOF", "PVOF", "FR,R", "FG,R", "FB,R", "GM1T8", "GM2T8",
              "GM3T8", "MV0,R", "MV345,R", scan.PREVIEW_BXZ, scan.SCAN_BXZ]
        for t in ok:
            self.assertTrue(scan.ALLOWED_TEXT.match(t), t)
        for t in ["DN13", "DN99", "DN1", "DN001", "MV100,R", "BXZ 0,0,100,100,1,1,V", "FORMAT", "FW", "WGM1T,1,2,3", "APIN;PSOF", "apin"]:
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

# scan: Agfa Vision 35 film scanner on Linux

A small, dependency-free tool to drive the **Agfa Vision 35** 35 mm film scanner (SCSI) from modern Linux. It was
written because the original software (FotoLook, 1995) only runs on Windows 3.x/9x and the scanner is supported by
no SANE backend. The protocol was worked out from a capture of FotoLook's SCSI traffic and from experiments on real
hardware (see [`docs/PROTOCOL.md`](docs/PROTOCOL.md)).

- One Python 3 file, standard library only, no compiled parts. Talks to the scanner through the Linux `sg` driver.
- **Calibrated, flat-fielded, 16-bit linear output** meant for converting negatives in
  [NegPy](https://github.com/marcinz606/NegPy) (or any tool that wants linear scans), plus a quick 8-bit positive.
- Per-roll calibration from a piece of blank film and the film holder.
- Colour and black & white, 4413 x 2881 pixels per frame.

> **Status: works for the author, on one scanner.** Tested with a Kodak colour negative, Harman Phoenix 200,
> Kodak Vision3 250D and one black & white negative. Many commands in the protocol are copied verbatim without
> being understood. The scanner is old and cannot be replaced: read [Safety](#safety).

## Requirements

- Linux with the `sg` driver, a SCSI host adapter the kernel supports, and the scanner on the bus
  ([`docs/HARDWARE.md`](docs/HARDWARE.md) has the bring-up checklist and what went wrong for us).
- Python 3 (developed and tested on 3.13). Nothing else.

## Install

```sh
sudo ./install.sh          # installs /usr/local/bin/scan  (sudo ./install.sh --uninstall to remove)
```

or run it in place: `python3 scan.py ...`. Your user needs read/write access to the scanner's `/dev/sgN`
(normally via the `scanner` group, see the hardware notes).

## Use

Make one folder per roll and work inside it:

```sh
mkdir roll-01 && cd roll-01

scan calibrate            # film holder + a piece of BLANK film in view -> calibration.json
scan                      # full colour scan -> 1.tiff   (next run -> 2.tiff, ...)
scan --mono               # black & white negative, one pass (default red filter), about 2 minutes
scan --preview            # quick 8-bit greyscale preview -> preview-1.tiff (no calibration needed)
scan --positive           # 8-bit positive for looking at (negative inverted, auto levels)
```

**Calibration needs blank film in view**, either the clear gap between two frames or the leader (a band at least
~10 columns / 2 mm wide across the strip), or a fully blank frame. If nothing suitable is found, `scan calibrate`
refuses and saves the wide scan as `calibration-scan.png` instead of guessing. Details in
[`docs/CALIBRATION.md`](docs/CALIBRATION.md).

A colour scan takes 4 to 7 minutes. `scan --help` lists everything:

| Option | |
|---|---|
| `--out FILE` / `--dir DIR` | output file, or folder for the auto-numbered files and `calibration.json` (default: current folder) |
| `--calibration FILE` | use another calibration file |
| `--mono [R\|G\|B]` | black & white: one filter only, default `R`; `--gray` writes a single-channel TIFF instead of 3 equal channels |
| `--output display` | 8-bit gamma picture instead of the 16-bit linear file; `--invert`, `--auto-levels`, `--gamma G` apply to it; `--positive` is a shorthand |
| `--no-flip`, `--flip-h`, `--rotate 90\|180\|270` | orientation (see below) |
| `--no-trim` | keep the film holder at the edges instead of trimming it |
| `--keep-raw`, `--from-scan N.tiff.raw` | save the raw scanner bytes with the exact parameters used, and decode them again later |
| `--device /dev/sgN` | skip auto-detection |

### Output

The default output is an **untagged 16-bit RGB TIFF with linear transmittance relative to the blank film**: blank film
is 0.75 of full scale, the holder is 0, lamp falloff is corrected, and the negative is **not** inverted and not
colour-balanced, so a converter can do its own film-base normalisation. After each scan `scan` prints how many
pixels sit at the floor/ceiling of the encoded range.

Pictures come out **flipped top-to-bottom by default**, which makes film loaded the usual way upright. If text in a
picture reads backwards, add `--flip-h`; use `--no-flip` for the scanner's native orientation.

### NegPy

Open the 16-bit TIFFs in NegPy as negatives (black & white files with its B&W mode). I have not tested NegPy
extensively with these files; file an issue with what you find.

## How it works, briefly

The scanner reports an **inverted** 8-bit signal (the film holder reads ~253, clearer film lower) and applies a
256-entry table that you can write. `scan` writes a logarithmic table per channel (equal byte steps are equal
density steps), uses a gain setting per channel chosen during calibration, and decodes the bytes to linear light in
software. Full explanation: [`docs/CALIBRATION.md`](docs/CALIBRATION.md) and [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Safety

- The scanner is sent **only** SCSI INQUIRY, TEST UNIT READY, REQUEST SENSE, WRITE(10) and READ(10), and only a
  fixed list of ASCII commands seen in the original software plus the table writes described above. Firmware,
  buffer, format and diagnostic commands are blocked in code and covered by tests.
- Table writes are the only configuration the tool changes, and they last until the scanner is power cycled.
- Even so, this is reverse-engineered, unofficial software for hardware that cannot be repaired or replaced. Use it at
  your own risk, and do not experiment with other commands on a scanner you care about. A READ(10) without a
  preceding command can reset the SCSI bus (see the protocol notes).
- Do not let the machine suspend during a scan. A suspend in the middle of a scan hangs it.

## Limitations

- Only the scan windows seen in the original software are used (one resolution, 4413 x 2881 per frame). Other
  windows are blocked on purpose; the whitelist is in `scan.py` if you want to explore.
- No infrared / dust channel, no dust removal, no multi-frame batch mode.
- Very dense negatives lose their densest highlights (a hardware limit).
- One scanner, four film stocks tested. The `--mono` default (red) comes from one B&W frame.

## Development

```sh
python3 -m unittest discover -s tests -v     # no hardware needed
python3 -m pyflakes scan.py tests/test_scan.py
```

`scan.py` is a single file by design. The tests use synthetic data to cover the encode/decode maths, calibration
detection (including false-positive rejection), orientation, TIFF writing and the command whitelist.

## Credits and legal

Copyright (C) 2026 Niek Ilmer <niek@beunie.nl>.

Not affiliated with or endorsed by Agfa, Nikon, Kodak, Harman or the NegPy project. All product names belong to their
owners. The protocol notes are the result of observing bus traffic for interoperability; no vendor software or
code is included in this repository. Released under the **GNU General Public License, version 2 only** (`GPL-2.0-only`, the same
licence as the Linux kernel; see [LICENSE](LICENSE)). Contributions are accepted under the same licence.

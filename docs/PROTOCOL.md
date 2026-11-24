# Agfa Vision 35 protocol notes

What is known about talking to the scanner over SCSI, as implemented by `scan.py`. Everything here was either
**observed** in a capture of the original Windows software (FotoLook 2.07, through a logging ASPI shim) or
**verified** by experiment on real hardware. Things that are guesses are marked **(guess)**.

No vendor code is reproduced here. The protocol was worked out from observed bus traffic for interoperability.

## Device

```
INQUIRY (12 00 00 00 24 00) ->
  06 00 01 02 1F 00 00 00  "AGFA    "  "VISION 35       "  "1.36"
```

- Peripheral device type 0x06 (scanner), SCSI-1, no sync, no wide, no command queueing.
- The device answers for **all 8 LUNs** (it ignores the LUN field). Use LUN 0 only. Linux logs harmless
  `M_REJECT received` messages while probing LUNs 1 to 7.
- INQUIRY VPD pages are not supported (page 0 lists only `0x1F`).
- Strings match a FotoLook device table that also lists `NIKON / LS-3510AF`, so the scanner is probably a
  rebadged Nikon of that family **(guess, unverified)**.

## Safe command set

`scan.py` only ever sends these SCSI opcodes: `12` INQUIRY, `00` TEST UNIT READY, `03` REQUEST SENSE,
`2A` WRITE(10), `28` READ(10). Everything else is blocked in code.

Observed on the real device (so you do not have to repeat it):

| CDB | Result |
|-----|--------|
| `1A` MODE SENSE(6), `1C` RECEIVE DIAGNOSTIC, `08` READ(6) | CHECK CONDITION, ILLEGAL REQUEST, invalid opcode (sense `05/20/00`) |
| `28` READ(10) sent **without** a preceding command (4 and 16 byte reads) | The first one may hang the bus: the host adapter logged a message-out error and reset the SCSI bus. The scanner recovered by itself. Only read after a command that produces data. |

## Transport

Commands are ASCII lines terminated with CR LF, sent with WRITE(10):

```
2A 00 00 00 00 00 00 LL LL 00      LL LL = big-endian length of the text incl. CR LF
data-out: "APIN\r\n"
```

After (almost) every WRITE the host polls TEST UNIT READY (`00 00 00 00 00 00`) until it succeeds.

- **While the scanner is busy** (moving, focusing, scanning) it does *not answer selection at all*. Linux reports
  `DID_NO_CONNECT` (host status `0x01`), Windows ASPI reports adapter status `0x11`. This is normal. Retry TUR
  every ~50 ms. It is never a CHECK CONDITION. A command that stays busy for more than a minute is a real problem.
- No sense data is returned in normal operation.

### Replies

```
28 00 01 00 00 00 00 00 01 00     READ(10), byte 2 = 0x01: 1-byte status
28 00 00 00 00 00 00 00 04 00     READ(10), byte 2 = 0x00: 4-byte header   02 LL LL X
28 00 00 00 00 00 00 LL LL 00     then LL LL bytes of payload
```

- Status byte `06` is returned after `AF,R`, the filter commands (`FR,R`/`FG,R`/`FB,R`) and `MV...,R`. `scan.py`
  requires it and stops on anything else.
- Header: `02`, 16-bit big-endian payload length, and a fourth byte (`40`, `42`, `C0` or `C2`) whose meaning is unknown.

## Scanning model

A scan is a sequence of **lines**. One line is a vertical strip: its samples run *across the film width* and
successive lines advance *along the film*.

```
BXZ x0,y0,x1,y1,xstep,ystep,V      select the window and start scanning
repeat:  READ header (02 LL LL X), READ payload (LL LL bytes), TUR
end:     header 02 00 01 C0 (or C2), then a 1-byte payload 03
```

- Payload = the samples of one line **plus one constant pad byte `0x17`** at the end.
- Coordinates are in scanner units. `V` presumably means vertical lines **(guess)**. Only these windows are used:

| Use | Command | Lines x payload |
|-----|---------|-----------------|
| Wide scan (preview, calibration) | `BXZ 0,0,4999,4999,13,13,V` | 385 x 386 bytes |
| Full scan | `BXZ 345,135,4757,3015,1,1,V` | 4413 x 2882 bytes (2881 samples) |
| (Original software pre-scan, not used by `scan.py`) | `BXZ 345,135,4757,3015,13,13,V` | 340 x 223 bytes |

- Before a scan the carriage is moved with `MV0,R` (wide scan) or `MV345,R` (full scan), then the status byte is read.
- One pass = one colour. A colour scan is three passes with `FR,R`, `FG,R`, `FB,R` (select the lamp filter/channel).
- Image data is **8 bit**.

## Commands

| Command | Meaning |
|---------|---------|
| `FR,R` `FG,R` `FB,R` | select red / green / blue pass (status byte `06`) |
| `AF,R` | autofocus, takes about 10 s (status `06`) |
| `MV0,R` `MV345,R` | move the carriage to a start position (status `06`) |
| `BXZ x0,y0,x1,y1,sx,sy,V` | scan window, see above |
| `DN01` `DN04` | analogue gain setting, see below |
| `GM1T8` `GM2T8` `GM3T8` | select table 1, 2 or 3 for the next pass |
| `WGMnT,v0,v1,...,v255` | write table *n* (1 to 3), see below |
| `APIN` `OUT8` `MP` `CP` `GML8` `IF15` `INM` `SPUP` `NHD` `AP50` `AVOF` `SDON` `BRT0` `CNT0` `SFT0` `PSOF` `PVOF` | sent verbatim in the order the original software uses; meaning unknown. `PSOF`/`PVOF` are sent after a scan, `APIN`/`OUT8` start the init sequence. |

Strings `DZ%d`, `BXD`, `INT`, `SFC%ld` also appear in the original program but were not seen on the wire.

### DN (gain)

`DN01` and `DN04` are the two values seen in the capture. Measured on three channels of the same film:

```
signal(DN01) = 255 - (255 - signal(DN04)) / 2.04
```

so **DN04 is the same signal with 2.04x the gain**, anchored at the holder level. DN04 gives more precision but
clips bright areas earlier. `DN` is only **latched by the next filter command** (`FR,R`/`FG,R`/`FB,R`). Sending it
after the filter command has no effect on that pass. The original software never changes it between passes, so
this never showed up in the capture.

### Signal polarity

The 8-bit value `s` is **inverted**: a completely dark area (the film holder) reads about **253**, and clearer
film reads lower. Light is linear in `(s_black - s)`; the holder level is the same in every channel and at both DN
values (253.0 to 253.7), which is how the true black is measured.

### Tables (`WGMnT`)

The scanner applies a 256-entry table to its signal before sending bytes: the table is indexed by the raw signal
`s` (0..255) and outputs the byte that is returned. Verified on hardware:

- Format: `WGM1T,` followed by exactly 256 integers 0..255 separated by commas, then CR LF (no trailing comma).
  The capture only logged the first 256 bytes of these writes, so the format was confirmed by experiment.
  Table *n* is used by the pass that selected it with `GMnT8`.
- Tables persist until the scanner is power cycled, so always write the table you want before scanning.
- The signal has **finer resolution than the 8-bit index**: a table that stretches an 89-level wide signal range
  across 0..255 returned 240 distinct output values with unit gaps. So the table is interpolated between entries
  (assumed linear). `scan.py` relies on this and inverts its own tables as piecewise linear when decoding.
- Writing a table is the only "configuration write" `scan.py` performs. No firmware, buffer or diagnostic
  commands are ever sent.

## Timing

Preview about 25 s. A wide scan of all three channels takes about a minute. A full colour scan takes 4 to 7
minutes (70 to 140 s per channel depending on DN, plus a ~10 s autofocus at the start). A mono scan takes about
2 minutes.

## Not known / worth exploring

- The meaning of most init commands and the 4th header byte.
- Other scan windows or resolutions (the code only uses windows seen in the capture).
- The infrared channel, if any, and dust handling.
- Whether other DN values exist.

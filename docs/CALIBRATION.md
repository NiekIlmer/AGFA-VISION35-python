# How calibration works

`scan calibrate` measures two references and writes them to `calibration.json` in the current folder:

- **Black**: the film holder, which lets no light through (about 253 on the scanner's inverted 8-bit scale).
- **White**: a piece of **blank film**, i.e. unexposed film base. For negatives this is the clearest film there is,
  so everything in a picture is a fraction of it.

Every scan afterwards uses the `calibration.json` in the folder it is run in (`--calibration FILE` selects another).
One folder per roll works well: calibrate once per roll, then scan the frames.

## What you have to show the scanner

Position the strip so that blank film is inside the scanner's field of view. Either:

- a **band of clear film** across the strip, at least ~10 columns wide (about 2 mm) *and* with sharp edges on both
  sides, such as the gap between two frames or the leader; or
- a **fully blank frame**, where the whole film area is blank.

If neither is found, `calibrate` stops with a message and saves the wide scan as `calibration-scan.png` so you can
see what the scanner saw. It does **not** guess: a smooth, dark part of a picture can look like blank film, so the
detector requires sharp edges (or an entirely uniform film area) and the band must be among the clearest areas
of the frame. After a successful run `calibration-scan.png` shows the detected area tinted green.

## What it does

1. One wide scan (`BXZ 0,0,4999,4999,13,13,V`) of all three channels at DN01 with neutral (identity) tables.
2. **Holder**: the near-black area connected to the border. Its robust mean is the black level per channel.
3. **Blank film**: either the whole uniform film area (a plane fit leaves only noise) or the clearest uniform band with
   sharp edges. The band's row extent is found by walking outward from its clearest row until the signal jumps.
4. **Flat field**: the blank band's level along the film width is stored as a smoothed profile. The lamp is a few
   percent (sometimes ~13 %) dimmer towards one edge, and every scan line runs across that gradient, so each row of the
   scan is divided by the profile when decoding. Blank film then reads the same everywhere.
5. **Gain**: for each channel, if the blank film leaves room (`light x 2.04 x 1.12 <= black - 6`) a second wide scan
   at DN04 measures the same references at 2.04x the gain, which roughly doubles the precision. Otherwise the channel
   stays at DN01. The DN04 numbers are checked against the prediction from DN01.

## What a scan does with it

For every channel the scanner is given a 256-entry table (`WGMnT`) that maps its signal to an output byte
**logarithmically in transmittance**, from a floor (about one raw level of light) up to 1.3x blank film (less for a
channel that cannot reach that at its gain). Equal steps in the byte are equal steps in density, so dense shadows are
not crushed and thin areas are not wasted. The 1.3x headroom matters: blank film has about 8 % pixel noise, so a
ceiling at 1.0 would clip half of the noise in clear areas.

The bytes are decoded back (exactly inverting the table) to transmittance relative to the local blank film, and written
as a **16-bit TIFF**: blank film = 0.75 of full scale, the holder = 0, not inverted, untagged colour. The report
after each scan lists the fraction of pixels at the floor and at the ceiling of the encoded range per channel.

## `calibration.json`

```json
{
 "version": 2,
 "created": "2026-01-01T12:00:00",
 "scanner": "AGFA VISION 35",
 "blank_columns_preview": [80, 97],
 "channels": {
  "R": {"dn": "DN01", "black": 253.2, "blank": 120.4,
        "profile": {"k0": 14, "k1": 230, "vals": [ ... smoothed blank level per preview row ... ]}},
  "G": {"dn": "DN04", "black": 253.2, "blank": 144.4, "profile": {"...": "..."}},
  "B": {"dn": "DN04", "black": 253.6, "blank": 172.7, "profile": {"...": "..."}}
 }
}
```

`blank` is the clearest row of the profile. `--keep-raw` saves the raw scanner bytes with a copy of the exact
parameters used (`N.tiff.raw` + `N.tiff.json`), so `scan --from-scan N.tiff.raw` can decode it again later, including
scans made with older versions of the tool.

## Limits

- **The lamp drifts** by a few percent over time. If you scan many frames, recalibrate now and then (a calibration
  takes about two and a half minutes).
- **Very dense film** is limited by the hardware: the dense end of a negative can sit within a raw level or two of the
  holder's black. On a very dense B&W negative about 18 % of the picture was at the floor in every channel.
- **Channel choice for B&W**: red had the most light and the best SNR on the one B&W frame tested; green was the
  noisiest. That is one frame and one film, so treat it as a default, not a law (`--mono R|G|B`).
- The black level comes from the film holder and is assumed to be the same everywhere on the holder.

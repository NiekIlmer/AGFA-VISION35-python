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
5. **Exposure**: for each channel, the highest DN (up to DN12) at which the blank film with 1.3x headroom still
   stays below saturation. A second wide scan with those per-channel DN values measures black, blank and the
   profile at the exposure the scans will use, and is checked against the prediction from DN01 and the gain law
   `10^(steps/10)`. If it does not match, the channel stays at DN01.

## Sensor pattern (`scan calibrate-sensor`)

With the holder **empty** the scanner sees nothing but lamp and optics, which separates its own behaviour from the
film's. Measured that way (green and blue at `DN00`, two repeats):

- random noise that changes from scan to scan is only about **0.3 %** of the light (about 0.65 raw levels);
- line-to-line lamp flicker is 0.01 to 0.02 %, and there is no fixed 2-D structure (no dust or specks on the optics);
- but there is a **fixed streak pattern along the line** (the same on every line): 0.67 % rms, up to 3 % at single
  samples, repeating to a correlation of 1.000. It is multiplicative and does not depend on exposure or film: fitting it to
  the pattern seen through a blank film gave a slope of 0.97 to 0.98.

`scan calibrate-sensor` stores it per sample (2881 values per channel) in
`~/.config/vision35/sensor-profile.json` (also looked up: `./sensor-profile.json`,
`/usr/local/share/vision35/sensor-profile.json`; `--sensor-profile FILE` selects another). Every scan divides each row by
it. Applied to a scan of the blank Phoenix frame it reduced the streaking from 0.77 % to 0.40 % (green) and from
0.73 % to 0.34 % (blue); what remains is the film's own lengthwise streaks. `--from-scan` applies a profile to old scans too.

**Red cannot be measured this way**: even at the lowest exposure (`DN00`) the empty aperture saturates it over about
half of the width, and the pattern is only partly shared between channels (correlation 0.76 between green and blue), so
red is left uncorrected. Repeat `calibrate-sensor` if the optics are cleaned or the scanner is serviced.

## Dark level (`scan calibrate-dark`)

The black level matters most at the dense end: a negative at density 2.3 is only about one raw level above black. With
the aperture covered by foil (two layers; creases don't matter) the scanner reads its true zero-light level at every
exposure setting: about 253.7 to 253.9, the same as the film holder, and flat from DN00 to DN12 (no dark current
build-up), with 0.3 to 0.4 levels of noise, which is essentially the 8-bit quantization. The film holder in a calibration
scan reads **0.4 to 0.9 levels lower** than that, because stray light from the bright aperture scatters into it. Using the
holder as black makes dense areas come out too dark.

`calibrate-dark` stores the dark level per channel at DN00, DN04, DN08 and DN12 in the sensor profile; scans interpolate it
for the exposure in use and take it as black instead of the holder reading. On the sunlit cat frame, the transmittance of
the densest pixels agreed noticeably better between exposures with it (dense-versus-mid-tone mismatch down from about 5%
to about 3%). Not yet corrected: a per-sample dark offset (0.1 to 0.2 levels rms, repeatable) and the scene-dependent
flare itself.

## Auto-exposure

A real picture is never as clear as blank film, so the blank film's exposure limit wastes counts in a dense frame.
Before the scan, `scan` makes a quick wide scan at the calibrated exposure and measures, per channel, the clearest
0.1 % of the picture **inside the scan window** (blank film outside the window does not count). It then raises DN
as far as keeps that clearest area, with 1.25x headroom, below saturation (never above DN12). The table and the
decode use a **virtual blank**: the calibrated blank-film light scaled by the gain law, so the result is still
transmittance relative to blank film regardless of the exposure used. The report says what was chosen
(`clearest area 0.31 x blank film -> DN08 (calibrated DN05)`).

- `--no-auto-exposure` scans at the calibrated exposure and skips the pre-scan (about 1.5 minutes shorter).
- `--exposure STEPS` shifts the exposure by STEPS (negative allowed) relative to the calibrated one.

Limit: a frame with very clear shadows is limited by them. On a sunlit B&W negative (clear base, shadows about 0.85 x
blank film) auto-exposure correctly stays at the calibrated exposure, and about 7 to 9 % of the picture (the sunlit
subject) sits at the dense floor. Scanning that frame with `--exposure +9` (DN10) resolved all but 0.3 % of it (density up
to about 3.2), at the price of clipping the clear areas (a third of the picture); a merge of the two exposures would
give both, and needs a small local alignment, since repeated scans can differ by one to two samples across the film.

Why it matters: the measurement error of the scanner falls roughly with 1/exposure (about 2.6 % of the signal at
DN01 against DN05, 1.2 % at DN03 on a blank frame), and extra exposure extends how dense a negative can be read
before the signal sinks into the raw quantization. It does not reduce film grain: on the blank film test, the
"noise" was the film's texture, 99 % reproducible between scans.

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

`blank` is the clearest row of the profile at that channel's calibrated DN. `--keep-raw` saves the raw scanner bytes
with a copy of the exact parameters used, including the exposure and virtual blank of that scan
(`dn_scan`, `blank_scan`; `N.tiff.raw` + `N.tiff.json`), so `scan --from-scan N.tiff.raw` can decode it again later, including
scans made with older versions of the tool.

## Limits

- **The lamp drifts** by a few percent over time (and the gain law is only good to about that, so the absolute scale of
  a scan taken several steps away from the calibrated exposure can be off by a few percent; a converter's film-base
  normalisation removes that). If you scan many frames, recalibrate now and then (a calibration
  takes about two and a half minutes).
- **Very dense film** is limited by the hardware: the dense end of a negative can sit within a raw level or two of the
  holder's black. On a very dense B&W negative about 18 % of the picture was at the floor in every channel.
- **Channel choice for B&W**: red had the most light and the best SNR on the one B&W frame tested; green was the
  noisiest. That is one frame and one film, so treat it as a default, not a law (`--mono R|G|B`).
- The black level comes from the film holder and is assumed to be the same everywhere on the holder.

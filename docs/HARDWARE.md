# Getting the scanner working on Linux

The Vision 35 is a SCSI scanner (50-pin, SCSI-1). You need a SCSI host adapter that Linux supports and a working
`sg` (SCSI generic) device. SANE is **not** needed and does not support this scanner (see below).

## What this was tested with

- A "PCI-SCSI200" card with an **LSI/NCR 53c810** (PCI `1000:0001`, rev 1), driven by the in-kernel `sym53c8xx`
  driver, on Debian 13 (kernel 6.12), in a PCIe motherboard with an ASMedia **ASM1083/1085 PCIe-to-PCI bridge**
  (`1b21:1080`) providing the legacy PCI slot.
- Scanner at SCSI ID 4 (the host adapter is ID 7).

Other adapters supported by the kernel (Adaptec `aic7xxx`/`aic79xx`, Symbios `sym53c8xx`, Tekram `dc395x`, ...)
should work the same way, but nothing else has been tried.

## Bring-up checklist

1. **Power the scanner on before the PC boots** (or rescan the bus after it is on:
   `echo "- - -" | sudo tee /sys/class/scsi_host/hostN/scan`). Pick a free SCSI ID (not 7), and make sure the bus is
   terminated at both ends (the scanner's switch, plus the host adapter's termination).
2. Check that the host adapter is claimed:
   ```
   lspci -nn | grep -i -E "scsi|storage"
   dmesg | grep -i sym
   lsscsi -g
   ```
   You should see `scanner  AGFA  VISION 35  1.36 /dev/sg0`.
   The scanner answers on all eight LUNs, so `lsscsi -g` lists it eight times (`sg0` to `sg7`). Only LUN 0 is
   meaningful. `scan` picks it automatically. Kernel lines such as `M_REJECT received` for LUNs 1 to 7 are harmless.
3. Permissions: `/dev/sgN` must be accessible to your user. On Debian the SANE udev rules normally tag it for
   the `scanner` group and the logged-in user. If not, install `contrib/70-agfa-vision35.rules` (untested here
   beyond a syntax check), add yourself to the `scanner` group and re-plug/reload udev.

## Troubleshooting what we hit

### `sym0: CACHE INCORRECTLY CONFIGURED. giving up ...`

The 53c810 behind the PCIe-to-PCI bridge initially failed this way. `dmesg` with `sym53c8xx verb=2` showed
`CACHE TEST FAILED: reg dstat-sstat2 readback ffffffff`, i.e. the driver could not read the chip's registers.
Symptoms we saw on the same card at the time:

- the chip's memory BAR (BAR1) read back with one address bit different from what the kernel had assigned
  (`0x9ff00000` instead of `0xdff00000`), so the chip answered outside the bridge's window;
- reading the chip's I/O registers gave all `0xff`, with occasional correct values;
- the PCI status register showed `<PERR+` (parity errors seen).

What fixed it: **reseating the card** and rebooting a few times; after that the stock driver attached normally
with sane BARs. We did not find a software fix. Setting the PCI cache line size does nothing (the register is
read-only on this chip/bridge). If you hit this, in order: reseat the card, try another slot, try another machine
or adapter. Check `lspci -vv -s <dev>` and compare the BARs with `setpci -s <dev> BASE_ADDRESS_1`. They should
match what `lspci` reports.

### The scanner does not respond for a while

While it is moving, focusing or scanning, the scanner does not answer SCSI selection at all (the kernel reports
`DID_NO_CONNECT`). This is normal; `scan` keeps polling. A SCSI bus reset in `dmesg` during a scan is not.

### Do not send arbitrary commands to it

See `docs/PROTOCOL.md`. In particular a READ(10) sent without a preceding command made the host adapter log a
message-out error and reset the bus. `scan` only uses the commands and sequences from the original software.

## Why not SANE?

`scanimage -L` may list the scanner through the `snapscan` backend, because `/etc/sane.d/snapscan.conf` contains
the wildcard `scsi AGFA * Scanner`. That is a false match: SnapScan is for Agfa flatbeds and uses different
commands. Do not scan through it. The `coolscan*` backends only accept `NIKON` INQUIRY strings.

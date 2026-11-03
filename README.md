# PREC: Polar recording with a Feather logger and a Bangle.js controller

```
 Polar H10  ──BLE (PMD data)──▶  Feather nRF52840 Sense  ──▶  SD card /prec/data/*.prc
                                  + Adalogger (SD, RTC)
                                        ▲
                                        │ BLE, password protected text commands
                                        │
                                   Bangle.js 2  (PREC app / "prec" module)
```

| Component | Where | Documentation |
|---|---|---|
| 0. PREC binary stream format | written by the firmware, read by `tools/prec_decode.py` | [docs/PREC_FORMAT.md](docs/PREC_FORMAT.md) |
| 1. Feather firmware | `firmware/prec_logger/` | [docs/FIRMWARE.md](docs/FIRMWARE.md), [docs/COMMANDS.md](docs/COMMANDS.md) |
| 2. Bangle.js 2 app and module: the Polar H10 read directly (speed models, cadence, recording) or through the feather, feather control, GPS time | `bangle/prec/` | [docs/BANGLE.md](docs/BANGLE.md) |
| 3. Laptop TUI: F1 home, F2 feather (sync + all feather commands), F3 bangle (apps, models, recordings), F4 data, F5 train | `tools/prec_tui.py`, library `tools/prec_link.py` | [docs/TUI.md](docs/TUI.md) |
| Data merging: pairs of recordings and .fit files, offsets, intervals, maps, merged numpy arrays | `tools/prec_merge.py`, `tools/prec_gui.py`, TUI tab F4 | [docs/MERGE.md](docs/MERGE.md) |
| Speed models and the run/walk/still/stretch classifier: training, export to the watch (compiled C + TFLite) and the feather | `tools/prec_train.py`, `tools/prec_arch/`, `tools/prec_class/`, `tools/prec_bangle.py`, TUI tab F5 | [docs/TRAIN.md](docs/TRAIN.md) |
| Other host tools | `tools/` | below |

## Quick start

From the laptop, `tools/prec_tui.py` does everything the watch can do (except
GPS time), plus **Sync data**, which copies all recordings to `~/prec/data`.


1. Flash the feather:
   `arduino-cli compile -b adafruit:nrf52:feather52840sense -u -p /dev/ttyACM0 firmware/prec_logger`
2. Install the Bangle app (see docs/BANGLE.md).
3. On the watch, run PREC:
   1. **GPS time**
   2. **Settings → Feather password** (feather default: `prec`)
   3. **Feather** (connects)
   4. **Set feather time**
   5. **Scan for Polar** (wear the strap so it is awake), and pick it as default
   6. **Data format**
   7. **Start logging**
   8. **Disconnect (keep logging)**

   Without the feather, **Polar H10** reads the strap directly and shows
   speed (with models from the PREC tools), heart rate, distance and cadence.
4. Later: **Feather** → **Stop logging**. Copy the `.prc` files from the SD
   card (or download them with `prec_cmd.py GET`) and decode them with
   `prec_decode.py`.

**Polar H10 limits.** The H10 streams only **ECG** (130 Hz) and
**accelerometer** (25–200 Hz, ±2/4/8 G) over the Polar Measurement Data
service. It has no gyroscope or magnetometer, so those options only work
with Polar devices that have them (e.g. Verity Sense). The feather reports
them as `not_supported` when logging from an H10.

## Host tools (`tools/`, Python 3, need `bleak` and `pyserial`)

### `prec_cmd.py`: feather commands from a script

```
prec_cmd.py [--serial PORT | --ble [--address ADDR]] [--password PW] [--timeout S] COMMAND...
```

* **Commands.** Each argument is one protocol line, e.g.
  `prec_cmd.py STATUS "FORMAT acc on rate=100"`. The default transport is the
  first USB serial port.
* **`--ble`.** Connects to the strongest `PREC-…` feather (or `--address`)
  and authenticates with `--password` (default `prec`).
* **Extra commands.**
  * `GET <file> [dest]` downloads one data file (binary SEND stream).
  * `SYNC [dir]` copies all new recordings (default `~/prec/data`).
  * `SETPASSWORD <new>` changes the password.
* **Exit status.** 1 if any command failed.

It is a thin wrapper around `prec_link.py` (see [docs/TUI.md](docs/TUI.md)).

### `prec_decode.py`: read `.prc` files

```
prec_decode.py FILE.prc              # session summary (all non-data words, sample counts and rates)
prec_decode.py FILE.prc --words      # every word
prec_decode.py FILE.prc --csv OUT/   # OUT/acc.csv, OUT/ecg.csv ...: polar_ns, rtc_ms, x, y, z (or value)
```

As a library:

| Function | Description |
|---|---|
| `read_words(path)` | yields `Word(offset, type, polar_ts, rtc_ms, payload)` |
| `word_name(type)` | name of a word type |
| `describe(word)` | one-line description of a word |
| `parse_session_start(payload)`, `parse_stream_start(payload)`, `parse_settings(bytes)` | payload parsers |
| `unpack_samples(data, nbytes, channels)`, `unpack_delta_frame(data, res_bits, channels)` | PMD sample decoding |
| `Decoder().feed(word)` | returns `[(measurement, polar_ns, rtc_ms, values)]` with per-sample timestamps, scaled by the stream FACTOR when present |

### `bangle_repl.py`: development helper for the Bangle

```
bangle_repl.py [-a ADDR] [-t SECONDS] [-r] [-u STORAGE_NAME FILE]... [-s SCREEN.png] [CODE]
```

* `-r` resets the interpreter first (needed before replacing a module that
  is already loaded).
* `-u` uploads files to Storage.
* `CODE` runs that JavaScript and prints the output for `-t` seconds.
* `-s` saves a screenshot.

## Arch Linux package

`aur/prec-tools/PKGBUILD` builds the package `prec-tools` from this checkout
(no download: it takes the files from two folders up):

```
cd aur/prec-tools && makepkg -si
```

It installs the tools, firmware sources and Bangle apps in
`/usr/lib/prec-tools`, the documentation in `/usr/share/doc/prec-tools`, and
these commands:

| Command | Runs |
|---|---|
| `prec-tui` | `tools/prec_tui.py` |
| `prec-cmd` | `tools/prec_cmd.py` |
| `prec-decode` | `tools/prec_decode.py` |
| `prec-merge` | `tools/prec_merge.py` |
| `prec-gui` | `tools/prec_gui.py` |
| `prec-train` | `tools/prec_train.py` |
| `prec-bangle` | `tools/prec_bangle.py` |
| `prec-bangle-repl` | `tools/bangle_repl.py` |

* **Dependencies.** Only the feather/TUI basics are required (python,
  numpy, pyserial, bleak, bluez). Data merging, training and firmware
  building are optional dependencies; `python-fitparse` comes from the AUR.
* **Firmware.** The installed firmware is read-only, so *Build model into
  feather* works on a copy in `~/.local/share/prec/firmware/prec_logger`
  (`paths.firmware` in `prec.conf`).
* **Version.** It follows `PREC_FW_VERSION` in `firmware/prec_logger/prec.h`.
* **Updating the AUR metadata.** After editing the PKGBUILD, run
  `makepkg --printsrcinfo > .SRCINFO`.


## Version and changes

The project version is in [VERSION](VERSION) (now 1.1.0); [CHANGELOG.md](CHANGELOG.md)
lists the changes of every version.

## Licence

MIT: anyone may use, copy, modify and distribute this project, also
commercially, as long as the copyright notice and the licence text stay
with it. See [LICENSE](LICENSE).

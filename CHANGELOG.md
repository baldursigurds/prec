# Changelog

All notable changes to the PREC project. The project version is in
[`VERSION`](VERSION) and follows [semantic versioning](https://semver.org/):
the third number for fixes, the second for new features, the first for
changes that break files, protocols or the command line. The parts keep
their own versions too, listed with each release:

* **firmware**: `PREC_FW_VERSION` in `firmware/prec_logger/prec.h`
* **watch app**: `version` in `bangle/prec/metadata.json` (with its own
  `bangle/prec/ChangeLog`)
* **PREC format**: `PREC_FORMAT_VERSION` in `firmware/prec_logger/prec.h`

New entries go at the top, under the version they will be released as.

## [1.1.0] - 2026-11-03

Firmware 1.0.0, watch app 0.04, PREC format 1 (unchanged).

### Added
- Classifier plot (F5 → *Predict on a pair* with a classifier): for the raw
  output and for every low-pass time constant a pair of graphs, the most
  likely class as a band and the four class probabilities (adding up to 1)
  as lines in the class colours. The actual classes stay at the top, as a
  band only.
- `classify_pair` returns the smoothed probabilities (`lowpass_prob`).
- Speed plot: the dots of the target-against-prediction scatter take the
  colour of the class interval their window lies in, blue outside the class
  intervals, with a legend counting them.
- `VERSION` and this changelog; the Arch package takes its version from
  `VERSION`.

### Changed
- Classifier plot: the acceleration and speed graph is lower, to leave
  room for the class graphs.

## [1.0.0] - 2026-11-03

The first version in git. Firmware 1.0.0, watch app 0.04, PREC format 1.

- **Feather firmware** (`firmware/prec_logger`): logs a Polar H10 (ECG,
  ACC) to SD card as PREC streams; text command protocol over BLE and USB
  with password authentication; Polar scan and known devices; resume after
  reset; built-in models (speed, classifier, run, walk) predicting speed,
  class and distance while logging; cadence (counted, or from a model's
  spectrum) with a low-pass; optional HEART_RATE and CADENCE words.
- **Watch app** `prec` (`bangle/prec`): the H10 read directly with the
  models on the watch, or the feather's data, on one data screen (speed
  with class colour, heart rate, distance, time, cadence, batteries);
  feather control; GPS time; recording of raw data, heart rate and
  cadence; settings for units, straps, models and filters; a module
  (`require("prec")`) for other apps.
- **Laptop tools** (`tools/`): the TUI (F1 home, F2 feather, F3 Bangle,
  F4 data, F5 train), decoding PREC files, merging with Garmin .fit files
  into pairs, class interval editing, cooking, training speed models and
  the run/walk/still/stretch classifier, export to the watch and the
  feather, prediction plots.
- **Packaging**: Arch Linux PKGBUILD (`aur/prec-tools`), MIT licence.

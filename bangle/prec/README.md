# PREC

Running speed from the Polar H10 chest strap, with models trained by the
PREC tools: either computed on the watch from the strap's accelerometer, or
read from the PREC feather logger (Adafruit Feather nRF52840 + Adalogger),
which this app also controls.

* **Polar H10**: connects to the default strap (the first time: scan and
  pick one) and shows the data screen. Back asks whether to stop.
* **Feather**: connects to the feather and opens its menu: Display (the
  data screen), status, clock, Polar straps, data format, logger settings,
  start / stop logging, disconnect (logging continues).
* **GPS time**: sets the watch clock from GPS.
* **Settings**: units (min/km, min/mi, km/h, mph, m/s), Polar straps,
  models (classifier, run, walk, any speed: they must take the same
  accelerometer input), recording (raw data, heart rate, cadence, file size),
  cadence axis, filters, feather password.

The data screen: status and clock; the speed with the class (green run,
yellow walk, red still, magenta stretch) and the unit; heart rate; distance,
running time and cadence (steps per minute); the batteries.

Models are uploaded from the PREC TUI (F3 > Models). Recordings are synced
to the computer with F3 > Sync recordings.

Other apps can use all of it through `require("prec")`; see `docs/BANGLE.md`
in the PREC project.

Licence: MIT (see LICENSE in the PREC project).

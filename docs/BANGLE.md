# Bangle.js 2 app and module (`bangle/prec`)

One app, **prec**, does everything on the watch:

* it reads the Polar H10 directly and computes speed, class, distance and
  cadence with models trained by the PREC tools (this was the separate
  *ppred* app until version 0.03);
* it controls the PREC feather and shows the feather's data;
* it sets the watch clock from GPS.

All of it lives in two modules, so any other app can use it.

| File | Installed as | Purpose |
|---|---|---|
| `lib.js` | `prec` | the module: settings, the data screen, models, the feather client, GPS time; `require("prec")` |
| `polar.js` | `precpol` | the Polar part: connection, models, cadence, recording (loaded by `prec.polar()`) |
| `app.js` | `prec.app.js` | the app (menus only) |
| `settings.js` | `prec.settings.js` | the settings menu (also reached from the Settings app) |
| `app.png` | | the icon (48 × 48, up to 16 colours) |
| `app-icon.js` | `prec.img` | launcher icon: `app.png` as an Espruino image (4 bits per pixel with a palette) |
| `make_icon.py` | | makes `app-icon.js` from `app.png` (run it after changing the picture) |
| `metadata.json` | | BangleApps App Loader description |
| | `prec.json` | settings, see below |
| | `prm_<id>`, `prm_<id>.tfl` | models uploaded from the TUI |
| | `pr<yymmddHHMMSS>.<n>` | recordings |

## Installing

* **From the TUI.** F3 → *Install prec app*. This uploads the app and also
  removes the old ppred app if it is there; its settings (units, filters,
  recording, buffers, the Polar) move into `prec.json`.
* **App Loader.** Copy `bangle/prec` into the `apps/` folder of a BangleApps
  checkout (or a personal fork with its own App Loader) and install PREC from
  there.

## The app

**Main menu**

* **Polar H10.**
  * Connects to the default strap and shows the data screen. The first time,
    when no strap is known yet, it scans and you pick one.
  * The watch computes the speed itself (see *Speed on the watch*). Without
    models it still shows heart rate, cadence and running time.
  * It reconnects by itself if the strap drops out.
  * *Back* asks "Stop?". Stopping ends the recording and disconnects.
* **Feather.**
  * Finds feathers advertising `PREC-…`, preferring the one used last time,
    otherwise the strongest signal. It connects and unlocks the feather with
    the password.
  * If the feather is already logging, the app says so. Then it opens the
    feather menu.
* **GPS time.**
  * Turns on the GPS, waits for a fix, sets the watch clock from GPS time
    and turns the GPS off. The screen shows the satellite count and the
    elapsed time.
  * *Back* cancels, and it gives up after 15 minutes. Outdoors with a clear
    sky a fix takes 30 s to a few minutes.
* **Settings.** See below; the same menu appears under Settings → Apps → PREC.

**The data screen** shows the same layout whether the data comes from the
Polar or from the feather:

```
 [widgets]
 running REC 1.2MB        00:31     status (small; warnings in red), clock
 ██                        min      class rectangle (run green, walk yellow,
 ██        5:39            ───        still red, stretch magenta), speed
 ██                        km         centred on the line of the unit fraction
     156           7.34             heart rate, distance (km or mi),
    ♥ bpm           km
    45:23          172              running time, cadence (steps per minute):
    time        steps/min             a 2 x 2 grid, one font size
     Polar 85%  Feather 3.95V       batteries (Feather only for the feather)
```

The app loads the widgets; the screen uses the area below them.

* The screen redraws once a second.
* `--` means there is no value: no recent prediction or heart rate, or no
  steps detected.
* *Time* counts from the start of the Polar session, or from the start of
  the feather's logging. *Dist* covers the same span.

**Feather menu** (title `LOGGING` while a session runs)

| Item | Feather command |
|---|---|
| *Log: <file>* (only while logging) | opens Status |
| Display | HR, polled every second, shown on the data screen. Back returns to the menu. |
| Status | STATUS: state, file, size, streams, Polar link, battery, clock |
| Set feather time | TIME, set from the watch clock (use *GPS time* first) |
| List data | LS |
| Scan for Polar | SCAN 10; picking a result makes it the default |
| Polar devices | DEVS; `*` marks the default; picking one makes it the default |
| Data format | QUERY (asks the Polar what it supports), then FORMAT per measurement: Enabled, Rate, Resolution, Range |
| Logger settings | SET: log heart rate, log cadence, cadence axis, cadence filter, class filter, speed filter |
| Start logging / Stop logging | LOG / STOP |
| Feather password | PASSWORD (sent encrypted); also stores the new password on the watch |
| Disconnect (keep logging) | BYE: the feather keeps logging until STOP, low battery, or the Polar switching off |

If the feather reports an event (Polar lost or back, session ended), the
watch buzzes and the menu is refreshed. If the connection drops unexpectedly
the app says "Connection lost".

**Settings** (`prec.json`)

| Item | Key | Default | Meaning |
|---|---|---|---|
| Units | `units` | `pace` | speed as min/km (`pace`), min/mi (`pacemi`), km/h, mph or m/s; distance in miles for min/mi and mph |
| Polar straps | `polars`, `polar` | | known straps (`[{id, name}]`) and the default. Pick one to make it the default or forget it; *Scan* finds new ones |
| Models | `models` | | which model files the Polar session uses: Classifier, Run speed, Walk speed, Any speed (or none). They must all take the same accelerometer input (rate and range); a model that does not fit the others gives an error, which can be overridden while switching several. A slot never set uses the old names `pclass`, `prun`, `pwalk`, `pmodel` if present |
| Record raw data | `record` | off | write the Polar's ACC data to the watch |
| Log heart rate | `logHr` | off | write HEART_RATE words |
| Log cadence | `logCad` | off | write a CADENCE word every second |
| File size | `chunk` | 0.5 MB | bytes per recording file (0.25, 0.5 or 1 MB) |
| Cadence axis | `cadAxis` | x | the accelerometer axis that points up and down (x on an H10 worn normally) |
| Cadence filter | `cadLp` | 5 s | low-pass time constant of the cadence (0 = off) |
| Class filter | `clsLp` | 10 s | low-pass time constant of the class probabilities (0 = off) |
| Speed filter | `speedLp` | 10 s | low-pass time constant of the speed (0 = off) |
| Buffers | `buffers` | 2 | overlapping windows per model (1–8): with 4 and a 256-sample window at 200 Hz a new value comes every 64 samples |
| Feather password | `password` | `prec` | the password that unlocks the feather |
| | `address`, `name` | | the feather used last |

## Speed on the watch

With the Polar read directly, the speed comes from the models chosen under
*Settings → Models*:

1. **Class.** With a classifier, its probabilities (run, walk, still, stretch)
   go through the class filter, and the most likely class picks the speed
   model:
   * *run* uses the run model and *walk* the walk model, each falling back
     to the *any speed* model;
   * *still* and *stretch* set the speed to 0.

   Without a classifier, the *any speed* model predicts.
2. **Speed and distance.** The speed goes through the speed filter, and the
   distance adds it up over time.
3. **Memory.** Only the classifier and the speed model in use are loaded at
   once, because RAM is tight. With a classifier and one speed model about
   40 KB stay free.

The models request the Polar's ACC rate and range. Without any model the app
asks for 50 Hz, which is enough for the cadence.

**Cadence** (steps per minute) is worked out the same way on the watch and on
the feather:

* **From a spectrum.** If a model computes a spectrum (`dft > 0`; the
  classifier is preferred), the spectrum of the up-down axis gives the step
  frequency. The strongest bin between 1 and 4.5 Hz is taken and refined by
  a parabola through it and its two neighbours.
* **By counting.** Otherwise steps are counted. The up-down axis is followed
  at about 50 Hz, and its running mean (2 s low-pass) sits near -1 g. A step
  is the signal rising through the mean after having been at least 150 mG
  below it. The crossing time is interpolated between samples, and the
  cadence is 60 / the mean of the last 8 step periods.
* **When counting takes over.** Counting is used when no spectrum came for
  5 s. It is also used when the strongest bin sits on the edge of the band,
  because then the real peak lies outside it; when walking that is often
  the stride, at half the step frequency.
* **Filter.** The cadence goes through a first-order low-pass, *Cadence
  filter* (`cadLp`, default 5 s, 0 = off). It starts again from the raw value
  after a stop. On the 65-minute recording, 5 s narrowed the second-to-second
  spread while running from about ±3 to ±1 steps/min.
* **No steps, no cadence.** A spectrum always has some peak, also when
  standing still, so either value is shown only while counting has found
  steps in the last 3 s.

On a 65-minute recording both methods gave 165–176 steps/min while running
and about 120 while walking.

**Recording.**
* When *Record raw data*, *Log heart rate* or *Log cadence* is on, the
  session writes a PREC stream ([PREC_FORMAT.md](PREC_FORMAT.md)) into
  Storage files `pr<yymmddHHMMSS>.<n>` of *File size* bytes each.
* What goes in:
  * SESSION_START, with firmware string `prec <version>`;
  * STREAM_START and the ACC words, only with *Record raw data*;
  * Polar battery words;
  * HEART_RATE and CADENCE words, as chosen;
  * SESSION_END on stop.
* With only the two log settings on, a recording is small: heart rate and
  cadence without the raw data.
* The recording stops, with a buzz and a red warning, when less than 1 MB
  of Storage would be left. Prediction carries on.
* F3 → *Sync recordings* fetches the recordings.

## Module API: `require("prec")`

Everything that talks to the feather returns a Promise. On an `ERR` reply the
Promise rejects with an `Error` whose `code` is the protocol error code (see
[COMMANDS.md](COMMANDS.md)).

**Data objects.** A *source* (a Polar `Session`, or `feather.monitor()`)
emits `"data"` with one object:

| Field | Meaning |
|---|---|
| `source` | `"polar"` or `"feather"` |
| `status`, `warn` | state text (e.g. `running`, `searching`, `logging`), a warning to show instead (in red) |
| `speed` | m/s, filtered |
| `cls` | `"run"`, `"walk"`, `"still"`, `"stretch"` |
| `hr` | bpm |
| `dist` | m |
| `time` | s since the session started |
| `cad` | steps per minute |
| `pbat`, `fbat` | Polar %, feather mV |
| `rec` | recording text, e.g. `REC 1.2MB` |

Any field may be `undefined`.

### Settings and display

| Function | Description |
|---|---|
| `DEFAULTS` | the default settings (table above) |
| `loadSettings()` → object | `prec.json` with defaults filled in |
| `saveSettings(s)` | stores it |
| `setPassword(pw)` | stores the feather password |
| `draw(d, units)` | draws the data screen for one data object in `Bangle.appRect` (`units` defaults to the setting) |
| `show(src, back)` → stop function | draws every `"data"` of `src` (and refreshes the clock); the back button calls `back(stop)` |
| `speedText(v, units)`, `distText(m, units)`, `timeText(s)` | the texts the screen shows |
| `UNITS`, `UNIT_KEYS`, `CLASSES`, `CLASS_COLORS` | unit names, class names and colours |
| `textInput(heading, initial, onSave, onBack)` | text entry: the `textinput` app if installed, else a character menu |
| `gpsTime(opts)` → Promise<Date> | Powers the GPS until it has a fix, sets the clock (UTC) and powers the GPS off. `opts.timeout` ms (default 600000), `opts.onProgress(fix)` is called for each GPS event. The Promise has `cancel()`. |

### Models

| Function | Description |
|---|---|
| `listModels()` → `[modelInfo]` | every model on the watch: `prm_*` and the old `pmodel`, `pclass`, `prun`, `pwalk` |
| `modelInfo(file)` → `{file, name, kind, freq, range, win, dft}` | read from the module text without loading it |
| `modelFiles(s)` → `{cls, run, walk, speed}` | the model files the settings choose that are on the watch |
| `checkModels(files)` → `undefined` or text | why these models cannot run together (wrong kind in a slot, a classifier without a speed model, different rate or range) |

### Polar session

```js
const prec = require("prec");
const s = prec.polar();                 // or prec.polar({address: "...", settings: {...}})
s.on("data", d => print(d.speed, d.hr, d.cad));
s.on("error", e => print("models:", e.message));   // the session then runs without models
s.start();
// ... later
s.stop();                               // ends the recording, disconnects, frees the models
```

`start()` loads the models chosen in the settings (or in `opts.settings`),
connects to `opts.address` or the default strap (or the first Polar found),
reconnects whenever the link drops, and emits `"data"` once a second.
`prec.show(s, back)` puts it on the data screen. The first strap the session
connects to is added to the known straps and becomes the default if there is
none.

### Feather

| Function | Description |
|---|---|
| `authDigest(nonce, password)` → hex string | `SHA-256(nonce + password)`, the AUTH argument |
| `encryptPassword(nonce, oldPw, newPw)` → hex string | the PASSWORD argument: `newPw XOR SHA-256(nonce + oldPw + "password")` |
| `findFeathers(timeout)` → Promise<[{id, name, rssi}]> | active scan (default 4000 ms) for `PREC-` devices |
| `connect(opts)` → Promise<Feather> | finds, connects and authenticates. `opts.address` connects only to that address; `opts.password` overrides the stored one; `opts.onProgress(text)` reports progress. Remembers the feather's address and name. Rejects with "No feather found" or "Wrong password" (code 401). |

`Feather` properties: `name`, `address`, `connected`. Events:
`feather.on("event", text => …)` for `! …` lines,
`feather.on("disconnect", () => …)`.

| Method | Command | Resolves to |
|---|---|---|
| `request(line, timeout)` | any | `{lines, args, reply}`: the `+` lines, the words after `OK`, the raw final line. Requests are queued; the default timeout is 20 s. |
| `status()` | STATUS | the key/value object from COMMANDS.md plus `logging` (bool); `bytes`, `bat` and `dropped` as numbers |
| `heartRate()` | HR | `{state, logging, polar (bool), hr, contact, age (ms), beats, rr: [ms], polarBattery, bat (mV), file, bytes, speed, rspeed, cls, dist, cad (steps/min), cadm ("count"/"dft"), rt (s logging), sage, preds, pms, model, mstate}` |
| `monitor(period)` | HR every `period` ms (1000) | a source: emits `"data"` objects; `stop()` ends the polling |
| `getSettings()` | SET | `{sleep, reconnect, flush, lowbattery, predict, clslp, speedlp, loghr, logcad, cadaxis, cadlp}` as numbers |
| `setSetting(key, value)` | SET key value | |
| `setTime(ms)` | TIME | `{ms, iso}`; `ms` defaults to `Date.now()` |
| `getTime()` | TIME | `{ms, iso}` |
| `listData()` | LS | `[{name, size}]` |
| `scan(seconds)` | SCAN | `[{address, rssi, known, name}]` (default 10 s) |
| `listDevices()` | DEVS | `[{index, address, isDefault, name}]` |
| `setDefault(addressOrIndex)` | DEFAULT | `{address, name}` |
| `getFormat()` | FORMAT | `{acc: {type, on, rate, res, range, ch}, ecg: …, …}` |
| `setFormat(type, f)` | FORMAT | the new format. `f` may hold `on` (bool), `rate`, `res`, `range`, `ch`; 0 means "do not send". |
| `query()` | QUERY | `{features: ["ecg","acc"], settings: {acc: {rate: [25,50,100,200], res: [16], range: [2,4,8]}, …}, battery, mtu}` |
| `startLog()` | LOG | `{file, started: ["acc"], failed: ["gyro:not_supported"]}` |
| `stopLog()` | STOP | `{file, bytes}` |
| `changePassword(newPw)` | HELLO + PASSWORD | stores `newPw` on the watch as well |
| `disconnect()` | BYE | drops the link; logging continues |

Example, the feather's data on the screen of another app:

```js
const prec = require("prec");
prec.connect().then(f => {
  const src = f.monitor();
  prec.show(src, stop => { stop(); src.stop(); f.disconnect().then(() => load()); });
}).catch(e => E.showAlert(e.message));
```

### Notes

* The module connects to the feather with a 20–50 ms connection interval. A
  shorter interval takes radio time the feather needs for the Polar link.
* Writes to the feather are split into 20-byte pieces, the Nordic UART limit.

## The TUI's Bangle tab (F3)

`tools/prec_bangle.py` talks to the watch's JavaScript console over
Bluetooth, with one connection per command. Status and sync leave a running
app alone. Installing the app or a model resets the watch first, then
returns to the clock.

| Item | What it does |
|---|---|
| Status | Battery, firmware, storage, the running app, the installed prec version (and whether this checkout has a newer one; the old ppred if still there), every model on the watch with the role it has (classifier, run, walk, any speed), the recordings waiting on the watch, and the app settings. |
| Sync recordings | Downloads every recording session, joins its files without their 0xFF padding into `<data>/<YYYYMMDD-HHMMSS>-bangle.prc` and checks that it starts with SESSION_START, then erases the files on the watch. While the app runs, the newest session is skipped if it has not ended yet. F4 → *Update pairs* then pairs the files like the feather's. About 6.5 kB/s. |
| Models | *Upload a model* uploads an exported model as `prm_<id>` / `prm_<id>.tfl` (with a `_classifier` suffix shortened to `_cls`, Storage names have 28 characters at most). The app then uses it for its role: a classifier as the classifier, `*_run` for run, `*_walk` for walk, anything else as *any speed*. *Remove a model* erases one and clears the role it had. The app's *Settings → Models* changes the choice. |
| Install prec app | Uploads the app from `bangle/prec` (modules, menus, settings, icon, launcher entry); removes the old ppred app and keeps its settings. |
| App settings | Units, class and speed filters, recording (raw data, heart rate, cadence), file size, cadence axis and filter, and buffers, written into the watch's `prec.json` (used the next time a Polar session starts). |

Command line: `prec_bangle.py [--address A] status | model NAME.js [--keep-use] | rmmodel FILE | app | set KEY=JSON... | sync DATA_DIR [--keep] | run CODE`.

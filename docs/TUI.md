# Laptop TUI (`tools/prec_tui.py`) and client library (`tools/prec_link.py`)

A menu driven terminal program that controls the feather from a laptop, over
USB or Bluetooth. It also copies the recordings to `~/prec/data`.

```
 PREC │ PREC-36C0 via usb │ LOGGING 20261029-233909.prc 112 kB │ bat 4.18 V
                                  │ Data files on the feather (12)
  1  Sync data to ~/prec/data     │ 20261029-220120.prc            17 kB  synced
  2  Status                       │ 20261029-225059.prc           159 kB  synced
  3  Set feather time             │ 20261029-233909.prc           210 kB  logging
  4  List data                    │
  5  Scan for Polar devices       │ Total 345 kB.  Local copies: /home/you/prec/data
  6  Polar devices / default      │
  7  Data format                  │
  8  Stop logging                 │
  9  Feather password             │
  0  Feather settings             │
  m  Monitor heart rate           │
  c  Disconnect (keep logging)    │
  q  Quit                         │
 ↑↓ move  Enter select  1-0/c/q shortcut  PgUp/PgDn scroll  Esc back  q quit
```

Requirements: Python 3.11 or newer, `pyserial`, `bleak` (Bluetooth only), and
a terminal of at least 50×10 (80×24 or larger recommended). It uses the
standard `curses` module, so nothing else is needed.

## Tabs

The TUI has five tabs. Switch with **F1**–**F5**, or **Tab** /
Shift+Tab (some terminals keep F1 for their own help). Each tab keeps its
own page and cursor.

| Tab | Contents |
|---|---|
| F1 Home | Overview: feather link, number of recordings, GPS files, pairs and avoid rectangles, and which config files are in use. *Write default config files* creates commented `tui.toml` and `prec.conf` if missing. |
| F2 Feather | Everything below: connection, sync, logging, monitor. F2 does not connect by itself: choose *Connect* (`c`; the cursor starts there). With `--connect` the TUI connects the first time F2 is opened. |
| F3 Bangle | The Bangle.js 2 over Bluetooth: status (app, models and their roles, recordings, storage, settings), syncing the prec app's recordings into the data folder, uploading and removing models, installing the prec app, the app's settings. See [BANGLE.md](BANGLE.md). |
| F4 Data | Pairing recordings with `.fit` files, offsets, intervals, plots and maps. See [MERGE.md](MERGE.md). |
| F5 Train | Roles, parameters, histogram, architectures, training a new model or retraining a saved one for more epochs (`t`; in the background, with a live log), models, predicting on a pair (`d`: a form with model, pair, shift and low-pass filters added with `l`; plot of prediction, filtered predictions and target), building models (speed, classifier, run, walk) into the feather firmware (`f`); putting models on the watch is in F3. See [TRAIN.md](TRAIN.md). |

The sync folder defaults to `paths.data` of `~/.config/prec/prec.conf`
(normally `~/prec/data`), so synced recordings are ready for F4.
`--prec-conf FILE` uses another data settings file.

## Running

```
tools/prec_tui.py                 # start; in F2 choose Connect (c): USB if plugged in, else Bluetooth
tools/prec_tui.py --usb           # USB, first /dev/ttyACM*;  --usb /dev/ttyACM1 for a given port
tools/prec_tui.py --ble           # Bluetooth;  --ble C7:CB:57:83:36:C0 for a given feather
tools/prec_tui.py --theme light   # one of: default, light, green, mono
tools/prec_tui.py --data-dir DIR  # sync somewhere other than ~/prec/data
tools/prec_tui.py --connect       # connect as soon as F2 is opened
tools/prec_tui.py --write-config  # write a commented ~/.config/prec/tui.toml and exit
```

* **Waking the feather.** If it is asleep, it neither advertises nor shows up
  on USB. Move it, then choose **Connect**.
* **Passwords.** Over Bluetooth the feather's password is needed. If it is
  wrong the TUI asks for it, and remembers it in `~/.config/prec/state.json`
  (mode 600) together with the feather's address.
* **USB.** Over USB no password is needed.

## Keys

| Key | Action |
|---|---|
| ↑ ↓ / j k | move |
| Enter, → | select |
| `1`–`0`, `m`, `c`, `q` | select the menu item with that key directly |
| PgUp / PgDn | scroll the output pane |
| Esc, ←, q | back (in lists and editors) |
| ← → | change a value in the data format editor; Enter or space toggles *Enabled* |

## Menu

| Item | What it does | Feather command |
|---|---|---|
| **1 Sync data to ~/prec/data** | Copies every finished recording that is missing or incomplete locally, with a progress bar. See below. | LS, READ |
| 2 Status | State, file, size, streams, Polar link, dropped words, battery, clock, default Polar, SD card, last reset, and the events received this session | STATUS |
| 3 Set feather time | Sets the feather RTC from the laptop clock (UTC, NTP synchronised) and shows how far off it was | TIME |
| 4 List data | Recordings on the card with their size and local state: `synced`, `partial`, `not synced`, or `logging` | LS |
| 5 Scan for Polar devices | Asks for a duration (default 10 s), then lists what was found; Enter makes one the default | SCAN, DEFAULT |
| 6 Polar devices / default | Known devices, ★ = default; Enter makes one the default | DEVS, DEFAULT |
| 7 Data format | Asks the Polar which measurements and values it supports, then edits Enabled / Rate / Resolution / Range for each. Changes are saved on the feather immediately and apply to the next session. While logging, or if the Polar is off, typical values are offered. | QUERY, FORMAT |
| 8 Start / Stop logging | Start: connects to the default Polar and starts the streams, then shows the file and which streams started or failed. Stop: asks for confirmation. | LOG / STOP |
| 9 Feather password | New password entered twice (masked). Sent encrypted over Bluetooth, plain over USB. Remembered in state.json. | PASSWORD |
| 0 Feather settings | sleep, reconnect, flush, lowbattery, predict, clslp and speedlp (the class and speed low-pass time constants) (see COMMANDS.md) | SET |
| m Monitor heart rate | Live heart rate in large digits with a red heart that pulses on each measurement, a 24 h clock (laptop time, HH:MM) at the top right, the speed predicted by the models in the feather (m/s and pace), the classifier's class, the distance and running time since logging started and the cadence (steps per minute, from a spectrum or counted), with the model names, plus state, file and size, Polar battery, feather battery, RR intervals and the age of the last measurement. Refreshes every second; Esc goes back. Only available while the feather is connected to the Polar, i.e. while logging. | HR |
| c Connect / Disconnect | Disconnect leaves a running session alone: the feather keeps logging | HELLO/AUTH, BYE |
| q Quit | Disconnects (logging continues) and exits | BYE |

* **Title bar.** Shows the connected feather, how it is connected, the
  logging state with file and size, the battery voltage, and
  `CLOCK NOT SET` when the RTC needs setting. It refreshes every 10 s.
* **Events.** Events from the feather (`! LOG …`: Polar lost, back, session
  ended, resumed) beep and appear in the status bar and under **Status**.

### Sync

* **What is copied.** Each recording in `/prec/data` on the card is copied to
  the sync directory under the same name.
* **What is skipped.** Files whose local copy already has the same size, and
  the file currently being logged, since it is still growing. Sync it after
  STOP.
* **Resuming.** Downloads are written to `<name>.part` and renamed when
  complete. Esc cancels, and the next sync continues where it stopped.
* **Integrity.** Every READ reply is checked against the byte count the
  feather reports, and re-requested if anything was lost.
* **Speed.** Sync uses the binary SEND stream on both links:
  * USB: about 300 kB/s.
  * Bluetooth: about 30 kB/s, or 15 kB/s while the feather is logging.

  It falls back to READ (40 kB/s over USB, 7 kB/s over Bluetooth) with older
  firmware. One hour of 200 Hz ACC is about 4.7 MB, which takes about 15 s
  over USB and 3 minutes over Bluetooth.
* **Integrity.** Each streamed piece is checked by offset and by CRC-32
  against what the feather reports.
* **Never deletes.** Sync does not change or remove anything on the feather.

## Configuration: `~/.config/prec/tui.toml`

All keys are optional. `--write-config` writes this file with comments:

```toml
theme = "default"            # default | light | green | mono

[connection]
transport = "auto"           # auto | usb | ble
serial_port = ""             # empty: first /dev/ttyACM*
ble_address = ""             # empty: last used feather, else strongest PREC-xxxx
password = ""                # empty: remembered password, else "prec"

[sync]
directory = "~/prec/data"

[colors]                     # override single elements of the theme
selected = "bold black on yellow"
heading  = "bold magenta"
```

Command line options override the file.

### Colours

A style is `[bold] [dim] [reverse] [underline] [italic] FOREGROUND [on BACKGROUND]`.
Colours are `black red green yellow blue magenta cyan white`, `default` (the
terminal's own colour) or a number 0–255. Examples: `"cyan"`,
`"bold white on blue"`, `"reverse"`, `"208 on 236"`. A misspelt style stops
the TUI at start-up with a message naming it.

| Element | Used for |
|---|---|
| `title` | top bar |
| `menu` / `key` / `selected` / `disabled` | menu items, their shortcut key, the highlighted item, items unavailable while disconnected |
| `heading` | the output pane's heading |
| `text` / `label` | normal text / labels and secondary information |
| `ok` / `warn` / `error` | success, warnings, errors |
| `event` | feather events |
| `status` | bottom bar |
| `progress` | sync progress bar |
| `border` | the line between menu and output |
| `role_train`, `role_valid`, `role_test`, `role_quant` | F5 roles list: green, amber (214), red, purple (135) |
| `heart` | the heart in the monitor (`bold red`; `bold` in mono) |

Built-in themes:
* `default`: cyan and yellow on the terminal background.
* `light`: blue, for light backgrounds.
* `green`: phosphor style.
* `mono`: no colours, only bold, reverse and underline. Use it on
  monochrome terminals.

## The client library `prec_link.py`

The TUI and `prec_cmd.py` are built on this synchronous library, which other
scripts can use too:

```python
from prec_link import Feather, PrecError
f = Feather.open("auto", password="prec", on_event=print)
print(f.status()["state"])
r = f.sync("~/prec/data", progress=lambda name, done, total, i, n: print(name, done, total))
f.bye()
```

| Function / method | Description |
|---|---|
| `Feather.open(transport="auto", port=None, address=None, password="prec", on_event=None)` | Connects and, over BLE, authenticates. `transport` is `"usb"`, `"ble"` or `"auto"`. `on_event(text)` is called for every `! …` line. Raises `PrecError` (code 401: wrong password, 0: not found or local error). |
| `f.kind`, `f.name` | `"usb"`/`"ble"`; the feather's name (`PREC-xxxx`) |
| `f.request(line, timeout=30)` | Any command. Returns `(args after OK, list of "+" lines)`; raises `PrecError(code, text)` on ERR or timeout. |
| `f.poll()` | Processes pending input (delivers events) without sending. |
| `f.status()` | STATUS as a dict; `logging` bool; `bytes`, `bat`, `dropped`, `up` as ints |
| `f.heart_rate()` | HR: `{"state", "logging", "polar", "hr", "contact", "age", "beats", "rr", "polar_battery", "bat", "file", "bytes"}` |
| `f.get_time()`, `f.set_time(ms=None)` | `(unix ms, ISO)`; `set_time` defaults to the laptop clock |
| `f.list_data()` | `[(name, size)]` |
| `f.read(name, offset, length, retries=3)` | `(bytes, file size)`, length checked |
| `f.download(name, dest, progress=None, offset=0)` | Appends the file to `dest` from `offset` with READ; `progress(done, total)` returning True cancels |
| `f.stream(name, dest, progress=None, offset=0)` | Same with the binary SEND stream (Bluetooth notifications, or frames on USB); verifies the offsets and the CRC-32, and continues from the last good offset after a gap or stall |
| `f.can_stream()` | True if the firmware supports SEND on this link (checked once); `sync` and `prec_cmd.py GET` then use `stream`, otherwise `download` |
| `f.sync(directory, progress=None)` | See *Sync*. `progress(name, done, total, index, count)` returning True cancels. Returns `{"copied", "skipped", "busy", "cancelled"}`. |
| `f.scan(seconds=10)` | `[(address, rssi, known, name)]` |
| `f.devices()` | `[(index, address, is_default, name)]` |
| `f.set_default(address_or_index)` | `(address, name)` |
| `f.get_format()` | `{type: {type, on, rate, res, range, ch}}` |
| `f.set_format(type, on=None, **settings)` | e.g. `set_format("acc", on=True, rate=100)`; returns the new format |
| `f.query()` | `{"features", "settings": {type: {key: [values]}}, "battery", "mtu"}` |
| `f.start_log()` | `{"file", "started", "failed"}` |
| `f.stop_log()` | `(file, bytes)` |
| `f.change_password(new, old)` | Plain over USB, encrypted over BLE |
| `f.get_settings()`, `f.set_setting(key, value)` | SET |
| `f.bye()` / `f.close()` | BYE and close / just close. Logging continues either way. |
| `find_feathers(timeout=10)` | `[(address, name, rssi)]` of advertising feathers |
| `serial_ports()` | candidate USB ports |
| `auth_digest(nonce, password, suffix="")`, `encrypt_password(nonce, old, new)` | protocol crypto helpers |
| `SerialTransport(port)`, `BleTransport(address)` | transports: `write(bytes)`, `read(timeout)`, `close()` |

### TUI internals (`prec_tui.py`)

| Function / method | Description |
|---|---|
| `load_config(path, theme=None)` | Reads tui.toml and merges theme and overrides: `{connection, sync, theme, styles}` |
| `load_state()` / `save_state(**changes)` | Remembered password and BLE address |
| `parse_style(text)` | `"bold black on yellow"` → `(attrs, fg, bg)`; ValueError on unknown words |
| `Styles(styles)` | curses attributes per element (`styles["error"]`) |
| `App(scr, cfg).run()` | Main loop: draw, keys, polling for events and status every 10 s |
| `App.menu()` | Current menu items `(label, handler, enabled)` |
| `App.draw(extra=None)` | Redraws the title bar, menu, output pane (or a custom pane `extra(y, x, h, w)`) and status bar |
| `App.show(heading)`, `say(text, style)`, `table(rows)`, `note(text, style)` | Output pane and status bar helpers |
| `App.prompt(label, initial="", secret=False)`, `prompt_int`, `confirm`, `pick(heading, rows, ...)` | Input on the status bar; list picker in the output pane |
| `App.do_monitor()`, `draw_monitor(...)`, `big_text(text)` | The heart rate monitor screen and its 5-row block digits |
| `App.do_connect`, `do_disconnect`, `do_quit`, `do_sync`, `do_status`, `do_time`, `do_list`, `do_scan`, `do_devices`, `do_format` (+ `edit_format`), `do_start`, `do_stop`, `do_password`, `do_settings` | The menu actions |

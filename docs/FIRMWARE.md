# Feather firmware (`firmware/prec_logger`)

## Hardware

| Part | Use | Connection |
|---|---|---|
| Adafruit Feather nRF52840 Sense | MCU, BLE central (to the Polar) and peripheral (to the command device) | |
| Adafruit Adalogger FeatherWing | SD card (`/prec/...`) | SPI, CS = D10 |
| | PCF8523 real time clock, with its own coin cell | I2C 0x68 |
| LSM6DS3TR-C (on the Sense) | motion detection for sleep and wake | I2C 0x6A, INT1 = D3 (P1.11) |
| LiPo battery | power; voltage read on A6 through a 1:2 divider | |

## Building and flashing

```
arduino-cli core install adafruit:nrf52        # tested with 1.7.0
arduino-cli lib install RTClib "SdFat - Adafruit Fork"
arduino-cli compile -b adafruit:nrf52:feather52840sense -u -p /dev/ttyACM0 firmware/prec_logger
```

If an upload fails, double-tap the reset button to enter the bootloader and
upload again.

## Files on the SD card

| Path | Contents |
|---|---|
| `/prec/data/YYYYMMDD-HHMMSS.prc` | one logging session in the [PREC format](PREC_FORMAT.md) (a `_N` suffix if the name exists) |
| `/prec/settings/settings.txt` | `key=value` lines: `password`, `default`, `sleep`, `reconnect`, `flush`, `lowbattery`, `resume`, `predict`, `clslp`, `speedlp`, `loghr`, `logcad`, `cadaxis`, `cadlp`, and one line per measurement (e.g. `acc=on rate=200 res=16 range=8`) |
| `/prec/settings/devices.txt` | known Polar devices, one per line: `<address> <address type> <name>` |

Both settings files can be edited by hand. Unknown keys are ignored, and
missing keys keep their defaults.

## How it works

* **Command channel.** Always available while the feather is awake. See
  [COMMANDS.md](COMMANDS.md). BLE commands need the password; USB commands
  do not.
* **Logging.** LOG connects to the default Polar and starts the configured
  PMD streams.
  * **Data path.** Each PMD notification becomes one PREC word. The BLE task
    copies it into a 16 KB RAM buffer. When that buffer is full, the buffers
    swap and `loop()` writes the full one to the card in one burst.
  * **Bluetooth first.** The BLE side never waits for the SD card.
  * **Flushing.** At least every `flush` seconds (default 5) any partial
    buffer is written and the file is synced. A power loss therefore costs
    at most that much data, and the file stays readable.
  * **Overflow.** If both buffers are full, whole words are dropped and a
    DROPPED word records how many, so the stream stays parseable.
* **Ending a session.** Disconnecting the command device (BYE, or the watch
  going out of range) does not affect logging. A session ends on
  * STOP,
  * the Polar staying away for longer than `reconnect` seconds (strap
    removed or Polar switched off),
  * the battery being under `lowbattery` mV three minutes in a row,
  * an SD write error.
* **Power.**
  * While idle, the feather stays awake as long as it moves, a command device
    is connected, a USB terminal is open, or a command arrived recently.
  * After `sleep` seconds without any of these it enters nRF52 *System OFF*,
    the lowest power state. Only the IMU stays on, in 26 Hz low-power mode.
  * The next movement over about 60 mg resets the board, which then starts
    advertising its command channel again.
  * It never sleeps while logging.
* **Robustness.**
  * A 15 s hardware watchdog resets the board if the firmware stops running.
  * If a session was running at the time (`resume=1` in settings.txt), the
    feather starts a new session on its own after the reset, retrying for
    `reconnect` seconds.
  * The step the firmware was executing is kept in a retained register and
    reported as `crumb=` in STATUS.
  * The I2C bus is freed at boot in case a reset caught a transfer midway.
* **LEDs.** Blue means a BLE connection (Bluefruit default). Red blinks
  briefly every 2 s while logging.

Breadcrumb values: `0x01` loop, `0x02` logger service, `0x03` motion check,
`0x10` Polar connect, `0x11` discovery, `0x12` MTU exchange, `0x13` control
point request, `0x14` Polar disconnect, `0x15` scan, `0x16` battery read,
`0x40+i` command number i of the HELP list.

## Modules and functions

Every module's functions are declared in `prec.h`.

### prec_logger.ino: main loop

| Function | Description |
|---|---|
| `setup()` | Starts the IMU, Bluefruit (1 peripheral + 1 central link), the PMD client, the command channel, the RTC, the SD card and settings, and the watchdog. |
| `loop()` | Feeds the watchdog; runs commands, the logger and resume service; checks motion every 250 ms; blinks the LED; enters sleep when idle long enough. |
| `idle_service()` | Called inside every blocking wait: feeds the watchdog and flushes the recorder, so logging continues while a command waits for the Polar. |
| `watchdog_feed()` | Reloads the 15 s hardware watchdog. |
| `breadcrumb(uint8_t step)` | Stores `step` in GPREGRET2, which survives a reset. |
| `breadcrumb_at_boot()` | The step stored before the last reset. |

### clock.cpp: real time clock

| Function | Description |
|---|---|
| `bool clock_begin()` | Starts the PCF8523 and anchors it. Returns false if there is no RTC. |
| `void clock_anchor()` | Waits (≤1.1 s) for an RTC second edge and remembers its `millis()`. |
| `uint64_t clock_now_ms()` | UTC milliseconds since 1970: anchor second plus elapsed `millis()`. |
| `void clock_set_ms(uint64_t unixMs, uint32_t receivedAtMillis)` | Sets the RTC to `unixMs`, which was valid when `millis()` was `receivedAtMillis`. Stops the PCF8523 divider, writes the next whole second and restarts it exactly on that edge. |
| `bool clock_valid()` | False if the RTC is missing or reports a power loss (time not set). |
| `void clock_format(uint64_t unixMs, char* out, size_t n, bool compact)` | `compact`: `YYYYMMDD-HHMMSS`; otherwise `YYYY-MM-DDTHH:MM:SS.mmmZ`. |

### storage.cpp: SD card, settings, devices

| Function | Description |
|---|---|
| `bool storage_begin()` | Mounts the card (FAT/exFAT), sets FAT timestamps from the RTC, creates `/prec/data` and `/prec/settings`. |
| `bool storage_ok()` | Whether the card is mounted. |
| `void settings_defaults()` | Factory settings: password `prec`, ACC on at 200 Hz / 16 bit / 8 G, ECG 130 Hz / 14 bit off, gyro 52 Hz / 2000 dps off, mag 50 Hz off, sleep 120 s, reconnect 60 s, flush 5 s, lowbattery 3400 mV. |
| `bool settings_load()` / `bool settings_save()` | Reads / rewrites `settings.txt`. |
| `bool format_apply(StreamFormat& f, int argc, char** argv)` | Applies tokens `on`, `off`, `rate=`, `res=`, `range=`, `ch=` to `f`. Returns false (leaving `f` unchanged) on a bad token. |
| `void format_print(uint8_t mt, const StreamFormat& f, char* out, size_t n)` | Writes `acc on rate=200 res=16 range=8` (zero values omitted). |
| `const char* mt_name(uint8_t mt)` / `int mt_from_name(const char* name)` | PMD measurement type ↔ `ecg`, `ppg`, `acc`, `ppi`, `gyro`, `mag`. |
| `bool devices_load()` / `bool devices_save()` | Reads / rewrites `devices.txt` into `devices[]`, `deviceCount` (max 16). |
| `int devices_find(const ble_gap_addr_t& addr)` | Index of a known device, or -1. |
| `int devices_add(const ble_gap_addr_t& addr, const char* name)` | Adds a device if new. Returns its index, or -1 if the list is full. |
| `bool addr_parse(const char* s, ble_gap_addr_t* out)` | Parses `AA:BB:CC:DD:EE:FF` (type set to random static). |
| `void addr_format(const ble_gap_addr_t& a, char out[18])` | Formats an address, most significant byte first. |

### recorder.cpp: PREC stream writer and double buffer

| Function | Description |
|---|---|
| `bool rec_open(const char* path)` | Creates a new file (fails if it exists) and resets the buffers and counters. |
| `bool rec_put(uint8_t type, uint64_t polarTs, const void* payload, uint16_t len)` | Appends one word. `polarTs = 0` uses the last Polar timestamp seen; the RTC timestamp is added automatically. Safe from any task. Returns false if dropped. |
| `bool rec_put_text(uint8_t type, const char* fmt, ...)` | Appends a word with printf-formatted text as payload (e.g. MESSAGE). |
| `bool rec_error(uint8_t source, uint8_t code, const char* text)` | Appends an ERROR word. |
| `void rec_set_polar_ts(uint64_t ts)` / `uint64_t rec_polar_ts()` | Latest Polar timestamp. |
| `bool rec_service(bool force)` | From `loop()`: writes full buffers. Every `flush` seconds (or when `force` is true) it also writes the partial buffer, emits a DROPPED word if needed, and syncs. Returns false after a card error. |
| `void rec_close()` | Flushes everything and closes the file. |
| `bool rec_is_open()`, `uint32_t rec_bytes()`, `uint32_t rec_dropped()`, `bool rec_failed()` | File open; bytes written; words dropped; write failed. |

### polar.cpp: Polar Measurement Data client

All PMD functions return 0 or a PMD error code (1–13, see the Polar
specification), or a negative transport error: -1 not connected, -2 write
failed, -3 no response within 3 s, -4 unexpected response.

| Function | Description |
|---|---|
| `void polar_init()` | Registers the PMD client service (control point with indications, data with notifications), the Battery Service client and the central callbacks. |
| `bool polar_connect(const ble_gap_addr_t& addr, uint32_t timeoutMs)` | Connects, requests the 2M PHY, discovers PMD and battery, enables CCCDs, exchanges an MTU of at least 232 (the Polar needs it). Blocking. For monitor mode it also subscribes to the standard Heart Rate Measurement and to battery notifications; these are optional, so a Polar without them still connects. |
| `const char* polar_last_error()` | Why the last `polar_connect` failed. |
| `void polar_disconnect()` | Disconnects (the Polar stops all streams) and waits up to 2 s. |
| `bool polar_connected()` | Whether the Polar link is up. |
| `bool polar_take_lost(uint8_t* reason)` | True once after the Polar dropped the link; `reason` gets the HCI reason code. |
| `uint16_t polar_mtu()` | Negotiated ATT MTU. |
| `int polar_read_features(uint8_t* mask)` | Reads the control point; one bit per supported measurement type. |
| `int polar_get_settings(uint8_t mt, uint8_t* out, uint16_t* len)` | *Get measurement settings*; `out` gets the setting TLVs. |
| `int polar_start(uint8_t mt, const StreamFormat& f, uint8_t* req, uint16_t* reqLen, uint8_t* resp, uint16_t* respLen)` | *Start measurement* with the non-zero settings of `f`. Returns the sent settings and the response parameters (as stored in STREAM_START). Restarts a stream that is already running. |
| `int polar_stop(uint8_t mt)` | *Stop measurement*. |
| `int polar_battery()` | Reads the battery percent (radio traffic); -1 if unknown. Also updates the cache. |
| `int polar_battery_cached()` | Last known battery percent without radio traffic: read at connect, by `polar_battery()` and from battery notifications. -1 when not connected. |
| `bool polar_heart_rate(HeartRate* out)` | Latest Heart Rate Measurement: `bpm`, `contact` (0 no, 1 yes, 2 not reported), `ageMs`, `count`, up to 4 newest RR intervals in ms. False if none has arrived on this connection. |
| `void polar_set_sink(uint8_t mask)` | Measurement types (bit mask) whose data go to the recorder. |
| `void polar_reset_packets()` / `uint32_t polar_packets(uint8_t mt)` | Per-type packet counters. |
| `int polar_scan(uint16_t seconds, ScanResult* out, int max)` | Active scan for names starting with `Polar `. Blocking; returns the count. |
| `const char* pmd_error_name(int code)` | Text for an error code. |

### logger.cpp: session state machine

| Function | Description |
|---|---|
| `int logger_start(char* fileOut, size_t n, char* info, size_t infoN)` | Opens the file, writes SESSION_START, connects, reads the Polar's features (retried up to 4 times; if that keeps failing, every enabled stream is tried anyway), starts streams, writes battery words, sets `resume=1`. Returns 0, 409, 404, 500, 503 or 422 (see COMMANDS.md); `info` gets `started=… failed=…`. |
| `int logger_stop(uint8_t reason)` | Stops streams (STREAM_STOP words), disconnects, writes SESSION_END, closes, clears `resume`. 409 if idle. |
| `void logger_service()` | From `loop()`: flushes the recorder; handles card errors; on Polar loss enters `reconnecting` and retries every 2 s for `reconnect` seconds, restarting the streams; writes battery words every 60 s; stops on low battery. |
| `void logger_resume_service()` | From `loop()`: restarts a session interrupted by a reset (see Robustness). |
| `LoggerState logger_state()`, `const char* logger_file()`, `uint8_t logger_streams()` | `LOG_IDLE` / `LOG_ACTIVE` / `LOG_RECONNECTING`; current file; mask of running streams. |
| `uint32_t logger_runtime_ms()` | Time since the session started (HR's `rt`), 0 when idle. |
| `void logger_note_time_set(uint64_t oldMs, uint64_t newMs)` | Writes a TIME_SET word if logging. |

### power.cpp: battery, motion, sleep

| Function | Description |
|---|---|
| `void power_begin()` | Frees the I2C bus, configures the LSM6DS3TR-C: gyro off, accelerometer 26 Hz low power ±2 g, latched wake-up interrupt (threshold 2/64 FS ≈ 62 mg) on INT1. |
| `bool power_motion()` | True if the IMU saw movement since the last call (reads and clears WAKE_UP_SRC). |
| `uint16_t power_battery_mv()` | Battery voltage in mV (4 ADC samples, 3.0 V reference, ×2 divider). |
| `void power_led(bool on)` | Red LED. |
| `void power_sleep()` | Stops advertising, clears the IMU latch and enters System OFF with D3 (high) as wake source. Does not return. |

### commands.cpp: command protocol

| Function | Description |
|---|---|
| `void cmd_begin()` | Names the device `PREC-xxxx`, starts the Nordic UART service and advertising. |
| `void cmd_poll()` | Reads complete lines from USB and BLE and executes them (`c_<command>` handlers). |
| `void cmd_event(const char* fmt, ...)` | Sends `! <text>` to USB and to an authenticated BLE client. |
| `bool cmd_ble_connected()` / `uint32_t cmd_last_activity()` | A command device is connected; `millis()` of the last received byte. |

Before writing, the USB channel issues a USB remote wakeup if the host has
autosuspended the port (Linux does this after 2 s of idle). Without it, replies
to slow commands such as SCAN would be lost.

### transfer.cpp: binary file streaming

Binary streaming over Bluetooth (the PREC file transfer service) or USB
(frames in the serial stream); see SEND in COMMANDS.md for the format.

| Function | Description |
|---|---|
| `void xfer_begin()` | Adds the service and its notify characteristic. Called before advertising starts. |
| `void xfer_begin()` (also) | Builds the CRC-32 lookup table. |
| `int xfer_start(bool usb, uint16_t connHandle, const char* name, uint32_t offset, uint32_t* sizeOut)` | Opens `/prec/data/<name>` and starts streaming from `offset`, over USB or to the BLE client `connHandle` (for BLE it also asks for the fast connection interval). Returns 0, 404, 409 (busy or file being logged) or 412 (BLE notifications not enabled). |
| `void xfer_service()` | From `loop()`: reads the file in 9600-byte blocks and sends packets for up to 30 ms, keeping a running CRC-32. BLE: 240-byte notifications. USB: 4800-byte frames, written only as far as the CDC buffer has room, so it never blocks. Sends the end packet, then closes the file (and, for BLE, asks for a relaxed interval). Stops when the client disconnects or the USB port is closed. |
| `void xfer_usb_flush()` | Completes a partly written USB frame (at most 1 s) so that a text line can follow. Called by the command channel before every USB write; aborts the transfer if the host does not read. |
| `void xfer_stop()` | Aborts (safe from the BLE task; takes effect in `xfer_service`). Also called when the client disconnects. |
| `bool xfer_active()`, `void xfer_progress(uint32_t* sent, uint32_t* ms)` | State and progress, shown by LINK. |

Breadcrumb `0x17` means the firmware was reading the file for a transfer.

### predictor.cpp, model_engine.h, model_data.h: the built-in models

* **Which models.** They are chosen at build time: a speed model, a
  classifier (run, walk, still, stretch), and run / walk speed models, in any
  combination that gives a speed. `model_data.h` is generated by
  `prec_train.py feather [SPEED] [--classifier C] [--run R] [--walk W]` (or TUI
  F5 → *Build models into feather*). Per model, in its own C++ namespace, it
  contains:
  * the feature code: the same C as the watch module (window, FFT,
    normalisation tables, `buffer_data`, `features`);
  * the layers as a table, with the weights as `static const float` arrays.

  `MODEL_SLOTS` (type `MSlot` in `model_engine.h`) lists the models with
  their kind, window, sizes and functions.
* **Pipeline.** Every `predict` ms:
  1. The classifier runs, and its probabilities go through a first-order
     low-pass with time constant `clslp` s.
  2. The most likely class picks the model: run → run model, walk → walk
     model (each falling back to the speed model), still / stretch → speed
     0.
  3. The speed goes through a low-pass with time constant `speedlp` s, and
     is integrated into the distance.

  Without a classifier, only steps 2 and 3 run, with the speed model.
* **No model.** `prec_train.py feather none` writes a stub
  (`MODEL_PRESENT 0`); the firmware then reports `model=-`.
* **The engine.** `model_engine.h` is a small plain-C float engine: Dense,
  Conv2D, DepthwiseConv2D, max/average pooling, folded BatchNormalization,
  and linear/ReLU/ReLU6/sigmoid/tanh/softmax activations, on channels-last
  tensors like Keras. It runs the float Keras weights, also for models whose
  watch version is int8.
* **Checked before flashing.** The generator compiles everything on the
  laptop and compares it with Keras on test windows. In tests the difference
  was below 1.5·10⁻⁶ m/s.

| Function | Description |
|---|---|
| `void predict_reset(uint16_t rate)` | A new ACC stream at `rate` Hz (0: none). The model runs if `rate` is a whole multiple of the model's frequency; the samples are then decimated. Called when the ACC stream starts, when the Polar is lost and when the session ends. |
| `void predict_feed(const uint8_t* samples, uint16_t n)` | From the PMD data callback (BLE task): copies n x,y,z int16 samples of a 16-bit ACC frame into a ring buffer (8 × window + 512 samples). |
| `void predict_service()` | From `loop()`: every `predict` ms, if new samples arrived, it builds the newest window, runs `buffer_data`, `features` and the layers, and keeps the result. |
| `bool predict_get(Prediction* p)` | The latest prediction: filtered and raw speed, class, distance, age, count and run time. |
| `const char* predict_class_name(int8_t cls)` | `run`, `walk`, `still`, `stretch`, or `-`. |
| `const char* predict_model()`, `predict_state()` | Model names (`speed:NAME,classifier:NAME,...`); `running`, `waiting for data`, `not logging`, `ACC rate does not fit the model`, `no model`. |

Measured with the int8 `conv3` test model (run in float): about 57 ms per
prediction, 31 KB more RAM and 20 KB more flash than without a model.
Breadcrumb `0x18` means the firmware was running the model. Predictions are
never written to the data file.

The first model of a step that has DFT features (the classifier, else the
speed model) also hands the spectrum of the up-down axis to cadence.cpp:
`spectrum(feat, out, ch)`, generated with the feature code, undoes the
normalisation of its features. `MSlot` carries `dft` and `spectrum` for this.

### cadence.cpp: steps per minute

The cadence is computed in two ways, the same as in the watch app:

* **Counting.** This always runs, with or without a model. `cad_feed()` gets
  every ACC frame (BLE task) and follows the up-down axis (`cadaxis`, x on an
  H10 worn normally) at about 50 Hz.
  * Its mean is tracked with a 2 s low-pass and sits near -1000 mG.
  * A step is the signal rising through the mean after having been at least
    150 mG below it.
  * The crossing time is interpolated between the two samples. The cadence
    is 60 / the mean of the last 8 step periods (0.2–1.5 s; a longer gap
    starts again).
* **DFT.** `cad_spectrum()` gets the magnitudes from a model.
  * The strongest bin between 1 and 4.5 Hz, refined by a parabola through
    it and its two neighbours, gives the step frequency.
  * This value wins while spectra keep coming (5 s).
  * When the strongest bin sits on the band edge, counting is used instead:
    the real peak lies outside the band. When walking that is often the
    stride, at half the step frequency.
  * Either value is reported only while counting has found steps in the
    last 3 s: a spectrum always has some peak, also when standing still.

`cad_service()` (loop) puts the value through a first-order low-pass with
time constant `cadlp` s (default 5, 0 = off; updated every 100 ms, starting
again from the raw value after a stop). It also writes a CADENCE word (the
filtered value) every second while logging if `logcad` is on. Tested on the laptop with the 65-minute recording, both
methods gave 165–176 steps/min while running and about 120 while walking.

| Function | Description |
|---|---|
| `void cad_reset(uint16_t rate)` | a new ACC stream (0: none); called with `predict_reset` |
| `void cad_feed(const uint8_t* samples, uint16_t n)` | counting, from the PMD data callback |
| `void cad_spectrum(const float* mag, int bins, float binHz)` | the DFT estimate, from predictor.cpp |
| `float cad_get(uint8_t* method)` | steps/min after the low-pass (0 = none) and 0 counting / 1 DFT |
| `void cad_service()` | from `loop()`: the low-pass and the CADENCE word |

Tested live with the H10 (classifier + run + walk + speed test models):
sitting still gave no cadence, and only the stretch with real movement
(accelerometer swings of several hundred mG) wrote CADENCE words. Every
heart rate notification became a HEART_RATE word, and the prec app's
*Display* showed the feather's data.

Heart rate words: with `loghr` on, `on_hr` in polar.cpp writes every Heart
Rate Measurement notification into the file as a HEART_RATE word.

### sha256.cpp

`void sha256(const uint8_t* data, size_t len, uint8_t out[32])`: SHA-256 of
`len` bytes, for the authentication challenge.

## Notes and limits

* The Polar H10 offers only ECG (130 Hz) and ACC (25/50/100/200 Hz, 16 bit,
  ±2/4/8 G) over PMD. The gyroscope and magnetometer options are kept for
  Polar devices that have them (e.g. Verity Sense). On an H10 they are
  reported as `failed=gyro:not_supported`.
* While Polar data flow, the BLE command link must not monopolise the radio.
  The peripheral (command) link therefore uses 7.5 ms radio events, with a
  12-deep notification queue.
  * **On connect.** The feather asks for data length extension (251-byte
    packets) and the 2M PHY. The interval is left to the client: the Bangle
    uses 20–50 ms, Linux 30 ms.
  * **During a SEND transfer.** The feather asks for 7.5 ms, or 15 ms while
    logging, then 30 ms afterwards.
  * **Measured.** The laptop (Intel AX201 with BlueZ) moved one packet per
    connection event: 15.2 kB/s at 15 ms and 30.4 kB/s at 7.5 ms. A deeper
    queue made no difference. Use LINK to see the current parameters.
* While a transfer runs, `loop()` skips its 10 ms pause.
* BLE replies are sent one notification at a time and retried while the
  notification queue is full (`ble_write` in commands.cpp), so long replies
  are never truncated.
* Logging can run for up to 49 days before `millis()` wraps.

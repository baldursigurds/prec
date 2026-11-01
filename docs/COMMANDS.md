# Feather command protocol

The feather accepts the same text commands on two channels:

| Channel | Transport | Access |
|---|---|---|
| Bluetooth | Nordic UART Service: service `6e400001-b5a3-f393-e0a9-e50e24dcca9e`; write to RX `…0002…`, subscribe to TX `…0003…` | password (HELLO/AUTH) |
| USB serial | `/dev/ttyACM0`, any baud rate | trusted, no password |

The feather advertises as **`PREC-xxxx`**, where xxxx is the end of its
Bluetooth address. It advertises only while it is awake (see "Power" in
[FIRMWARE.md](FIRMWARE.md)).

## Framing

* **Request.** One line `COMMAND arg arg …`, ended by `\n` (a `\r` is ignored).
  The command name is case-insensitive. Lines are limited to 159 characters.
  Over BLE, write at most 20 bytes per GATT write; the feather reassembles
  the lines.
* **Reply.** Zero or more data lines starting with `+ `, then exactly one
  final line:
  * `OK [values…]`
  * `ERR <code> <text>`
* **Events.** Unsolicited lines starting with `! ` can arrive at any time,
  even between the lines of a reply. Clients should handle them separately.
* **One at a time.** Send the next command only after the final line of the
  previous one.

Error codes:

| Code | Meaning |
|---:|---|
| 400 | bad command or arguments |
| 401 | not authenticated / wrong password |
| 404 | not found (no default Polar, unknown device, no such file) |
| 409 | conflicting state (already logging, not logging, busy logging) |
| 422 | no measurement stream could be started |
| 500 | SD card error |
| 502 | Polar answered unexpectedly |
| 503 | cannot connect to the Polar (text says why) |

## Authentication

```
→ HELLO
← OK PREC 1.0.0 PREC-36C0 9f2c4e01a7b3d655        (firmware, name, nonce)
→ AUTH 5e88489…(64 hex digits)
← OK
```

1. HELLO returns a fresh random 8-byte nonce, written as 16 hex digits.
2. AUTH takes `hex(SHA-256(nonce + password))`, where the nonce is the hex
   text exactly as received.
3. Each nonce allows one AUTH attempt. After three wrong attempts the feather
   drops the BLE connection.

Before AUTH succeeds, the BLE channel accepts only HELLO and AUTH. The
password never travels over the air in plain text. The default password is
**`prec`**; change it with PASSWORD.

## Commands

### HELP
Lists the commands: `+ NAME - description` lines, then `OK <count>`.

### HELLO
`OK PREC <firmware> <name> <nonce>`. Starts authentication, and also gives
the nonce used by PASSWORD.

### AUTH `<hex>`
`hex = SHA-256(nonce + password)` → `OK` or `ERR 401 wrong password`.

### STATUS
One line of `key=value` pairs:

```
OK state=logging file=/prec/data/20261029-225059.prc bytes=67391 dropped=0 polar=connected
   streams=acc packets=acc:298 bat=4120 time=2026-10-29T22:51:52.510Z clock=ok sd=ok
   default=C4:A4:AB:7B:59:29 reset=0x0 crumb=0x00 up=107
```

| Key | Meaning |
|---|---|
| `state` | `idle`, `logging`, or `reconnecting` (the Polar was lost and the feather is trying to get it back) |
| `file` | data file being written, `-` when idle |
| `bytes` | bytes written to the card in this session |
| `dropped` | data words dropped because the RAM buffers were full |
| `polar` | `connected` / `disconnected` |
| `streams` | measurements being logged, comma separated, `-` if none |
| `packets` | PMD packets received per measurement |
| `bat` | feather battery, millivolts |
| `time` | feather clock, UTC |
| `clock` | `ok`, or `unset` (the RTC lost power and needs TIME) |
| `sd` | `ok` / `fail` |
| `default` | address of the default Polar, `-` if none |
| `reset` | reset reason register at boot (diagnostics) |
| `crumb` | step the firmware was at before the last reset (diagnostics, see FIRMWARE.md) |
| `up` | seconds since boot |
| `hr` | latest heart rate in bpm while connected to the Polar, `-` otherwise |
| `pbat` | Polar battery in %, `-` when not connected |

### TIME `[unix-milliseconds]`
Without an argument this reads the clock; with one it sets the RTC (UTC).
Reply: `OK <unix ms> <ISO time>`, e.g. `OK 1793309603000 2026-10-29T21:33:23.000Z`.
Setting the time aligns the RTC's second boundary to the given time, to
within a few milliseconds plus the transmission delay. If a session is
running, a TIME_SET word records the change.

### LS
Lists the data files in `/prec/data`: `+ <name> <bytes>` lines, then `OK <count>`.

### READ `<file> <offset> <length>`
Returns up to 4096 bytes of `/prec/data/<file>` as `+ <hex>` lines of up to 96
bytes each, then `OK <bytes returned> <file size>`. The file being logged
cannot be read (409). A client should check that the hex it received adds up
to `<bytes returned>` and ask again if not; `tools/prec_link.py` does this.
READ is the simple fallback: about 40 kB/s over USB, about 7 kB/s over
Bluetooth. The tools use SEND when the firmware has it.

### SEND `<file> [offset]` / SEND STOP
Streams `/prec/data/<file>` from `offset` (default 0) in binary, over the
channel the command came on. It is about 4× faster than READ over Bluetooth
and 8× faster over USB.

The reply `OK <file size> <offset>` comes at once, followed by the packets:

```
[u32 offset][file data]                        data packet
[u32 0xFFFFFFFF][u32 bytes sent][u32 CRC-32]   end packet
```

All numbers are little endian. The CRC-32 uses the zlib/IEEE polynomial and
covers the bytes sent by this SEND.

**Over Bluetooth**, each packet is one notification on the file transfer service:

| | UUID |
|---|---|
| File transfer service | `9f3e0001-5052-4543-a5d3-6c7b4a1e2f80` |
| Data characteristic (notify) | `9f3e0002-5052-4543-a5d3-6c7b4a1e2f80` |

* Enable notifications on the data characteristic before sending SEND
  (otherwise `ERR 412`).
* Data packets carry up to MTU − 7 bytes (240 bytes with MTU 247).
* **Link speed.** While a transfer runs, the feather asks for a 7.5 ms
  connection interval, or 15 ms when a session is logging, to leave radio
  time for the Polar. Afterwards it asks for 30 ms. Laptop controllers
  usually move one packet per connection event, so this sets the speed:
  about 30 kB/s, or 15 kB/s while logging.

**Over USB**, each packet is wrapped in a frame inside the serial stream:

```
FE 50 [u16 packet length] [packet]
```

* Data packets carry up to 4800 bytes.
* Text never contains the byte `FE`. A client reads the stream item by item:
  if the next byte is `FE` it is a frame, otherwise a text line up to `\n`.
* Frames are never split by text: replies and `!` events are sent only
  between frames.
* Measured speed: about 300 kB/s.

**On the client side**, check that the offsets are contiguous and that the
end packet's byte count and CRC match. On a gap, a stall or a mismatch, send
`SEND STOP` and then SEND again from the last good offset. The feather may
already have sent more than the client has read; the client discards those
leftover packets before the next SEND. Disconnecting, or closing the USB
port, also stops a transfer.

Rules:
* Only one transfer runs at a time (`ERR 409` otherwise), and the file being
  logged cannot be sent (`ERR 409`).
* The command channel stays usable during a transfer, and logging continues.

### HR
Monitor mode: the latest heart rate plus the state and both batteries, in one
line. It is cheap enough to poll every second.

```
OK state=logging polar=connected hr=101 contact=- age=892 beats=6 rr=625,608 pbat=90 bat=4149
   file=/prec/data/20261030-134853.prc bytes=6503
```

| Key | Meaning |
|---|---|
| `hr` | bpm from the Polar's Heart Rate Measurement, `-` when there is none (no Polar connection yet) |
| `contact` | `yes` / `no` skin contact, `-` if the Polar does not report it (the H10 does not) |
| `age` | ms since that measurement arrived |
| `beats` | measurements received on this connection; a change means a new one arrived |
| `rr` | newest beat-to-beat intervals in ms, comma separated, `-` if none |
| `pbat` | Polar battery %. Read at connect, refreshed every 60 s while logging and by battery notifications. |
| `bat` | feather battery, mV |
| `file`, `bytes` | the session's file and size, `-`/0 when idle |

| `speed` | speed in m/s from the models built into the firmware (see TRAIN.md) after the speed low-pass; `-` if there is no prediction |
| `rspeed` | the same before the low-pass |
| `cls` | class from the built-in classifier (`run`, `walk`, `still`, `stretch`), `-` without one |
| `dist` | metres: the filtered speed integrated since the session started (a Polar reconnect keeps it) |
| `sage` | ms since that prediction |
| `preds`, `pms` | predictions in this session; milliseconds the last one took (all models of one step) |
| `model` | the built-in models as `slot:name,...` (e.g. `classifier:model_..._classifier,run:model_..._run`), `-` without one |
| `mstate` | `running`, `waiting_for_data`, `not_logging`, `ACC_rate_does_not_fit_the_model`, or `no_model` |
| `cad` | cadence in steps per minute after the `cadlp` low-pass, `-` when none (no steps for 3 s, or not logging); works without a model |
| `cadm` | how: `dft` (from a model's spectrum) or `count` (steps counted on the up-down axis), `-` |
| `rt` | seconds since the logging session started, 0 when idle |

Heart rate exists only while the feather is connected to the Polar, which
means while logging or reconnecting. It goes into the data file only with
`SET loghr 1` (HEART_RATE words); the cadence only with `SET logcad 1`
(CADENCE words, one a second).

### LINK
Diagnostics: the Bluetooth parameters actually in effect for the client
link and the Polar link, and the transfer state:

```
+ client interval=7.50ms latency=0 timeout=2000ms mtu=247 datalength=251 phy=2M
+ polar none
+ transfer active sent=1003440 ms=31880
OK
```

### SCAN `[seconds]`
Actively scans 1–60 s (default 10) for devices whose name starts with
`Polar `. Each device found is listed as `+ <address> <rssi> new|known <name>`.
New ones are added to `/prec/settings/devices.txt`. Reply:
`OK <found> <added>`. Not allowed while logging (409).

### DEVS
Known Polar devices: `+ <index> <address> <*|-> <name>`, where `*` marks the
default. Then `OK <count>`.

### DEFAULT `[address|index]`
Without an argument this shows the default Polar. With an address or a DEVS
index it sets the default, which must be a known device (404 otherwise).
Reply: `OK <address> <name>`.

### FORMAT `[type [on|off] [rate=N] [res=N] [range=N] [ch=N]]`
The data format requested from the Polar for each measurement type `ecg`,
`ppg`, `acc`, `ppi`, `gyro`, `mag`.

* Without arguments it lists all types: `+ acc on rate=200 res=16 range=8`.
* With only a type it shows that type.
* With settings it changes and saves them.

A value of 0 means the setting is not sent to the Polar, so the Polar uses
its default. Which values are valid depends on the Polar; ask it with QUERY.
Changes apply to the next session.

```
→ FORMAT acc on rate=100 range=4
← OK acc on rate=100 res=16 range=4
```

### QUERY
Connects to the default Polar and reports what it supports, then disconnects:

```
+ features ecg,acc
+ ecg rate=130 res=14
+ acc rate=25,50,100,200 res=16 range=2,4,8
+ battery 100
+ mtu 232
OK
```

Not allowed while logging. The Polar H10 supports **ECG and ACC only**; it
has no gyroscope or magnetometer. Other Polar devices report `gyro` and
`mag` here when they have them.

### LOG
Starts a logging session:

1. Creates `/prec/data/YYYYMMDD-HHMMSS.prc` (named from the feather clock, UTC).
2. Connects to the default Polar.
3. Starts every enabled measurement the Polar supports.

Reply: `OK <file> started=acc[,…] [failed=gyro:not_supported,…]`.
Measurements that fail are recorded as ERROR words and listed in `failed`.
If nothing starts, the file is removed and the reply is `ERR 422`. The
session continues after the command device disconnects.

### STOP
Stops the streams, disconnects the Polar and closes the file:
`OK <file> <bytes>`.

### PASSWORD `<hex>` / PASSWORD `-p <password>` (USB only)
Changes the password. Over BLE the new password is sent encrypted:

1. Send HELLO to get a nonce `n`.
2. Send `hex(new XOR SHA-256(n + old password + "password"))`, using the
   first `len(new)` bytes of the digest.

The new password must be 1–32 printable ASCII characters without spaces.

### SET `[sleep|reconnect|flush|lowbattery|predict|clslp|speedlp|loghr|logcad|cadaxis|cadlp <value>]`
Shows or changes the operating settings (saved to the SD card):

| Key | Default | Meaning |
|---|---:|---|
| `sleep` | 120 | seconds without motion, connection or command before sleeping (0 = never) |
| `reconnect` | 60 | seconds to try to get a lost Polar back before ending the session |
| `flush` | 5 | longest time (s) logged data stays in RAM before being written and synced |
| `lowbattery` | 3400 | stop logging when the battery is below this many mV (0 = never) |
| `predict` | 250 | run the built-in speed model this often (ms) while logging (0 = never) |
| `clslp` | 10 | time constant (s) of the low-pass on the classifier's probabilities (0 = off) |
| `speedlp` | 10 | time constant (s) of the low-pass on the predicted speed (0 = off) |
| `loghr` | 0 | 1: write every heart rate measurement into the data file (HEART_RATE words) |
| `logcad` | 0 | 1: write the cadence into the data file once a second (CADENCE words) |
| `cadaxis` | 0 | accelerometer axis pointing up and down, for the cadence: 0 x (the H10 worn normally), 1 y, 2 z |
| `cadlp` | 5 | time constant (s) of the low-pass on the cadence (0 = off) |

### BYE
`OK logging continues` or `OK idle`. The feather then drops the BLE
connection. Logging is not affected.

### SLEEP
Puts the feather into motion-wake sleep immediately (only when idle).

## Events

| Event | Meaning |
|---|---|
| `! LOG polar_lost` | the Polar disconnected; reconnecting for `reconnect` s |
| `! LOG polar_reconnected` | it came back; the streams were restarted |
| `! LOG stopped <reason> <file>` | the session ended on its own: `polar_lost`, `low_battery` or `sd_error` |
| `! LOG resumed <file>` | after an unexpected reset the feather started a new session on its own |

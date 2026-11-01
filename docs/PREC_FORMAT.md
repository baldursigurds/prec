# PREC binary stream format (version 1)

The PREC format records data from a Polar device (H10 or any other device
speaking the Polar Measurement Data service) as logged by the PREC feather,
or by the prec app on the Bangle.js 2.

In a watch recording:
* `rtc_ms` is the watch's clock;
* the SESSION_START firmware string is `prec <version>` (`ppred <version>`
  in older ones);
* only session, stream, ACC, Polar battery, HEART_RATE and CADENCE words
  are written (see [BANGLE.md](BANGLE.md), *Speed on the watch*).
A `.prc` file in `/prec/data` is one logging session: a plain concatenation of
**words**, with no file header, no padding and no index. A reader walks the file
word by word. Because every word carries its own length, a reader can skip
word types it does not know.

The reference implementation is `tools/prec_decode.py` (functions
`read_words`, `parse_settings`, `parse_session_start`, `parse_stream_start`,
`unpack_samples`, `unpack_delta_frame`, class `Decoder`).

All integers are **little endian**, the same byte order as the Polar PMD data.

## Word layout

| Offset | Size | Field       | Meaning |
|-------:|-----:|-------------|---------|
| 0      | 1    | `type`      | word type, see below |
| 1      | 2    | `length`    | number of payload bytes that follow the 19 byte header (0..65535) |
| 3      | 8    | `polar_ts`  | Polar timestamp, unsigned, nanoseconds on the Polar's own clock |
| 11     | 8    | `rtc_ms`    | feather timestamp, unsigned, milliseconds since 1970-01-01 UTC |
| 19     | `length` | `payload` | type specific data |

So the whole word is `19 + length` bytes.

### The two timestamps

* **`polar_ts`**
  * In a data word (type `0x80`–`0xFF`) this is the timestamp the Polar put in
    the PMD notification. Per Polar, that is the time of the **last sample** in
    the frame.
  * In every other word it is the most recent Polar timestamp seen so far in
    the session, or `0` if no data has arrived yet. This lets events be placed
    on the Polar's time axis.
  * The Polar's epoch is 2000-01-01T00:00:00 when its clock has been set by
    the Polar app or SDK. Otherwise its zero is arbitrary, but the clock stays
    monotonic within a session.
* **`rtc_ms`**
  * The feather time when the word was created, UTC.
  * It comes from the Adalogger's PCF8523 real time clock, anchored to its
    second boundary and interpolated with the nRF52 crystal, which gives
    millisecond resolution.
  * For data words this is the arrival time of the BLE notification. The
    difference `rtc_ms - polar_ts/1e6` therefore drifts slowly, and this is
    how the two clocks are aligned.

## Word types

| Type | Name | Payload |
|------|------|---------|
| `0x01` | SESSION_START   | see below; always the first word of a file |
| `0x02` | SESSION_END     | `u8 reason`; the last word of a cleanly closed file |
| `0x03` | STREAM_START    | a Polar measurement was started (settings) |
| `0x04` | STREAM_STOP     | `u8 measurement type` |
| `0x10` | POLAR_BATTERY   | `u8 percent` (Battery Service of the Polar) |
| `0x11` | FEATHER_BATTERY | `u16 millivolts` (feather LiPo) |
| `0x12` | TIME_SET        | `u64 old rtc_ms, u64 new rtc_ms`: the feather clock was changed during the session |
| `0x13` | DROPPED         | `u32 words, u32 bytes` dropped since the previous DROPPED word (RAM buffers full) |
| `0x14` | HEART_RATE      | the Polar's Heart Rate Measurement as received (only with heart rate logging on) |
| `0x15` | CADENCE         | `u16 steps/min × 10, u8 method` (only with cadence logging on) |
| `0x20` | ERROR           | `u8 source, u8 code, UTF-8 text` |
| `0x21` | MESSAGE         | UTF-8 text (e.g. `polar disconnected reason=0x08`, `polar reconnected`) |
| `0x80`–`0xFF` | PMD data | `0x80 \| PMD measurement type`; payload described below |

Types `0x05`–`0x0F`, `0x16`–`0x1F` and `0x22`–`0x7F` are reserved. Readers must
skip unknown types using `length`.

### SESSION_START (0x01)

| Size | Field |
|-----:|-------|
| 4 | magic `"PREC"` (ASCII) |
| 1 | format version (`1`) |
| 6 | Polar BLE address, most significant byte first (as written `C4:A4:AB:7B:59:29`) |
| 1 | Polar address type (0 public, 1 random static) |
| 1 + n | Polar name: length byte, then n bytes UTF-8 (e.g. `Polar H10 CE279A2A`) |
| 1 + m | feather firmware version: length byte, then m bytes (e.g. `1.0.0`) |

A reader should check the magic to recognise a PREC file.

### HEART_RATE (0x14)

Written when heart rate logging is on (`SET loghr 1` on the feather, *Log
heart rate* in the prec app). There is one word per notification of the
Polar's Heart Rate Measurement characteristic (Bluetooth SIG, 0x2A37), about
one per second, and the payload is that value unchanged:

| Size | Field |
|---:|---|
| 1 | flags: bit 0 = bpm is u16, bit 1 = skin contact, bit 2 = contact supported, bit 3 = energy present, bit 4 = RR present |
| 1 or 2 | heart rate, bpm |
| 0 or 2 | energy expended, kJ |
| 2 × n | RR intervals, 1/1024 s each |

`polar_ts` is the latest Polar timestamp (heart rate notifications carry
none of their own).

### CADENCE (0x15)

Written once a second when cadence logging is on (`SET logcad 1`, *Log
cadence*) and a cadence is known:

| Size | Field |
|---:|---|
| 2 | steps per minute × 10 (after the cadence low-pass) |
| 1 | method: 0 counting (up-down axis rising through its mean), 1 DFT (dominant frequency of a model's spectrum) |

### SESSION_END (0x02)

`u8 reason`:

| Value | Reason |
|------:|--------|
| 0 | STOP command |
| 1 | Polar lost: no reconnect within `reconnect` seconds (strap off or switched off) |
| 2 | feather battery low (below `lowbattery` mV three times in a row) |
| 3 | SD card write error. This word may not reach the card. |
| 4 | no stream could be started |

A file without SESSION_END was cut off by a power loss or reset. Everything up
to the last complete word is valid, because the feather syncs the file at
least every `flush` seconds.

### STREAM_START (0x03)

| Size | Field |
|-----:|-------|
| 1 | PMD measurement type (0 ECG, 1 PPG, 2 ACC, 3 PPI, 5 gyro, 6 mag) |
| 1 | n = length of the request settings |
| n | the settings sent in the PMD *Start measurement* request, exactly as on air |
| rest | the parameters of the Polar's response (bytes after `F0 02 type error more`), e.g. a reserved byte, or a `FACTOR` setting for compressed streams |

Both settings blocks use the PMD TLV encoding: `u8 setting type, u8 count,
count × value`. The value sizes are:

| Setting type | Name | Value size |
|---:|---|---:|
| 0 | sample rate (Hz) | 2 |
| 1 | resolution (bits) | 2 |
| 2 | range (G, dps or gauss) | 2 |
| 3 | range in milliunits | 4 |
| 4 | channels | 1 |
| 5 | factor (IEEE 754 float) | 4 |

Example from an H10 (ACC, 200 Hz, 16 bit, 8 G):
`02 0C 00 01 C8 00 01 01 10 00 02 01 08 00 01`, meaning type 2 (ACC), 12 bytes
of request settings (rate 200, resolution 16, range 8), and a response
parameter byte `01`.

A STREAM_START is written every time a stream is (re)started. This includes
after an automatic reconnect, so a decoder must use the most recent one for
each measurement type.

### PMD data (0x80 | measurement type)

The payload is the PMD data notification **from byte 9 on**: the frame type
byte followed by the samples, unchanged. The 9 bytes left out (measurement
type and the 8 byte timestamp) are already in the word header (`type & 0x7F`
and `polar_ts`).

| Word type | Measurement |
|---|---|
| `0x80` | ECG |
| `0x81` | PPG |
| `0x82` | ACC (accelerometer) |
| `0x83` | PPI |
| `0x85` | gyroscope |
| `0x86` | magnetometer |

Decoding the samples follows the Polar specification:

| Measurement | Frame type | Samples |
|---|---|---|
| ECG | 0 | 3 byte signed µV each |
| ACC | 0 / 1 / 2 | x, y, z as 1 / 2 / 3 byte signed integers, mG |
| any | `0x80` bit set | delta-compressed frame, see below |
| PPG | 0 | 4 × 3 bytes (ppg0, ppg1, ppg2, ambient) |
| PPI | 0 | 6 bytes: u8 bpm, u16 PP ms, u16 error ms, u8 flags |

**Delta frames** (frame type with bit 7 set) start with a reference sample of
`channels × ceil(resolution/8)` bytes, which is the first sample. Then come
blocks until the payload ends:

| Size | Field |
|---|---|
| 1 | delta size in bits (b) |
| 1 | sample count (k) |
| ceil(b × channels × k / 8) | k samples × channels deltas of b bits each, packed LSB first, two's complement |

Each sample is the previous one plus its deltas. Resolution and channel count
come from the STREAM_START word. If the Polar returned a FACTOR in its start
response, multiplying by it converts the values to the measurement's unit
(G, dps, gauss).

**Sample times.** `polar_ts` is the time of the last sample in a frame. With n
samples in the frame and the previous frame's timestamp `t_prev`, sample `i`
(0-based) is at `polar_ts - (n-1-i) × (polar_ts - t_prev)/n`. For the first
frame use the nominal rate.

## Example

The start of a real H10 recording (`prec_decode.py --words`):

```
       0 SESSION_START    polar=0 rtc=1793311280855  {'version': 1, 'polar': 'C4:A4:AB:7B:59:29', ...}
      56 STREAM_START     polar=0 rtc=1793311284148  acc {'rate': [200], 'res': [16], 'range': [8]}
      90 POLAR_BATTERY    polar=0 rtc=1793311284186  100 %
     110 FEATHER_BATTERY  polar=0 rtc=1793311284186  4186 mV
     131 PMD_ACC          polar=599618907809972224 rtc=1793311284419  frame type 0x01, 216 bytes
     ...
   16887 STREAM_STOP      polar=599618920119041472 rtc=1793311296788  acc
   16907 SESSION_END      polar=599618920119041472 rtc=1793311296827  stop command
```

Each H10 ACC data word is 19 + 1 + 36 × 6 = 236 bytes for 36 samples. At
200 Hz that is about 1.3 kB/s, or 4.7 MB per hour.

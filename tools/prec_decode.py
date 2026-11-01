#!/usr/bin/env python3
"""Decode PREC binary stream files (.prc) written by the PREC feather logger.

usage:
  prec_decode.py FILE.prc                 summary of the session
  prec_decode.py FILE.prc --words         one line per word
  prec_decode.py FILE.prc --csv OUTDIR    one CSV per measurement (acc.csv, ecg.csv, ...)

Reference implementation of docs/PREC_FORMAT.md. Can also be imported:
  for w in read_words(path): ...
  Decoder().feed(word) -> list of Sample rows
"""
import argparse, collections, math, os, struct, sys

HEADER = struct.Struct("<BHQQ")   # type, payload length, polar ts (ns), rtc (ms)

W_SESSION_START, W_SESSION_END, W_STREAM_START, W_STREAM_STOP = 0x01, 0x02, 0x03, 0x04
W_POLAR_BATTERY, W_FEATHER_BATTERY, W_TIME_SET, W_DROPPED = 0x10, 0x11, 0x12, 0x13
W_HEART_RATE, W_CADENCE = 0x14, 0x15
W_ERROR, W_MESSAGE, W_PMD_BASE = 0x20, 0x21, 0x80

WORD_NAMES = {
    0x01: "SESSION_START", 0x02: "SESSION_END", 0x03: "STREAM_START", 0x04: "STREAM_STOP",
    0x10: "POLAR_BATTERY", 0x11: "FEATHER_BATTERY", 0x12: "TIME_SET", 0x13: "DROPPED",
    0x14: "HEART_RATE", 0x15: "CADENCE",
    0x20: "ERROR", 0x21: "MESSAGE"}
MT_NAMES = {0: "ecg", 1: "ppg", 2: "acc", 3: "ppi", 5: "gyro", 6: "mag"}
END_REASONS = {0: "stop command", 1: "polar lost", 2: "low battery", 3: "sd error", 4: "no streams"}
ERR_SOURCES = {0: "feather", 1: "sd", 2: "ble", 3: "pmd"}
SETTING_SIZES = {0: 2, 1: 2, 2: 2, 3: 4, 4: 1, 5: 4}
CAD_METHODS = {0: "counting", 1: "dft"}
SETTING_NAMES = {0: "rate", 1: "res", 2: "range", 3: "range_milli", 4: "ch", 5: "factor"}

Word = collections.namedtuple("Word", "offset type polar_ts rtc_ms payload")


def word_name(t):
    if t >= W_PMD_BASE:
        return "PMD_" + MT_NAMES.get(t & 0x7F, "type%d" % (t & 0x7F)).upper()
    return WORD_NAMES.get(t, "UNKNOWN_0x%02X" % t)


def read_words(path):
    """Yield every Word in a .prc file. A truncated last word is ignored."""
    data = open(path, "rb").read()
    off = 0
    while off + HEADER.size <= len(data):
        t, n, pts, rtc = HEADER.unpack_from(data, off)
        end = off + HEADER.size + n
        if end > len(data):
            break
        yield Word(off, t, pts, rtc, data[off + HEADER.size:end])
        off = end


def parse_settings(b):
    """PMD setting TLVs (type, count, values) -> {name: [values]}."""
    out, i = {}, 0
    while i + 2 <= len(b):
        t, count = b[i], b[i + 1]
        i += 2
        size = SETTING_SIZES.get(t)
        if size is None:
            break
        vals = []
        for _ in range(count):
            raw = b[i:i + size]
            i += size
            vals.append(struct.unpack("<f", raw)[0] if t == 5 else int.from_bytes(raw, "little"))
        out[SETTING_NAMES[t]] = vals
    return out


def parse_session_start(p):
    if p[:4] != b"PREC":
        raise ValueError("not a PREC session start")
    addr = ":".join("%02X" % x for x in p[5:11])
    k = 12
    name = p[k + 1:k + 1 + p[k]].decode(errors="replace")
    k += 1 + p[k]
    fw = p[k + 1:k + 1 + p[k]].decode(errors="replace")
    return {"version": p[4], "polar": addr, "addr_type": p[11], "name": name, "firmware": fw}


def parse_stream_start(p):
    """-> (measurement type, settings sent, settings from the response)"""
    mt, n = p[0], p[1]
    return mt, parse_settings(p[2:2 + n]), parse_settings(p[2 + n:])


def describe(w):
    """Human readable one-line description of a word's payload."""
    p, t = w.payload, w.type
    if t == W_SESSION_START:
        return str(parse_session_start(p))
    if t == W_SESSION_END:
        return END_REASONS.get(p[0], str(p[0]))
    if t == W_STREAM_START:
        mt, req, resp = parse_stream_start(p)
        return "%s %s %s" % (MT_NAMES.get(mt, mt), req, resp or "")
    if t == W_STREAM_STOP:
        return MT_NAMES.get(p[0], str(p[0]))
    if t == W_POLAR_BATTERY:
        return "%d %%" % p[0]
    if t == W_FEATHER_BATTERY:
        return "%d mV" % struct.unpack("<H", p)[0]
    if t == W_TIME_SET:
        old, new = struct.unpack("<QQ", p)
        return "clock %+d ms" % (new - old)
    if t == W_DROPPED:
        return "%d words, %d bytes" % struct.unpack("<II", p)
    if t == W_HEART_RATE:
        bpm, contact, rr = parse_heart_rate(p)
        return "%d bpm%s%s" % (bpm, {True: "", False: ", no contact", None: ""}[contact],
                                 ", rr " + ",".join("%.0f" % x for x in rr) + " ms" if rr else "")
    if t == W_CADENCE:
        spm, m = parse_cadence(p)
        return "%.1f steps/min (%s)" % (spm, CAD_METHODS.get(m, m))
    if t == W_ERROR:
        return "%s code %d: %s" % (ERR_SOURCES.get(p[0], p[0]), p[1], p[2:].decode(errors="replace"))
    if t == W_MESSAGE:
        return p.decode(errors="replace")
    if t >= W_PMD_BASE:
        return "frame type 0x%02X, %d bytes" % (p[0], len(p) - 1)
    return p.hex()


def parse_heart_rate(p):
    """HEART_RATE payload (Bluetooth Heart Rate Measurement) -> (bpm, contact
    True/False/None if not reported, [RR intervals in ms])."""
    flags, i = p[0], 1
    if flags & 0x01:
        bpm = struct.unpack_from("<H", p, 1)[0]
        i = 3
    else:
        bpm = p[1]
        i = 2
    if flags & 0x08:
        i += 2                                       # energy expended
    rr = []
    if flags & 0x10:
        rr = [v * 1000 / 1024 for v in struct.unpack_from("<%dH" % ((len(p) - i) // 2), p, i)]
    contact = bool(flags & 0x02) if flags & 0x04 else None
    return bpm, contact, rr


def parse_cadence(p):
    """CADENCE payload -> (steps per minute, method 0 counting / 1 dft)."""
    v, m = struct.unpack_from("<HB", p)
    return v / 10, m


def signed(v, bits):
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def unpack_samples(data, nbytes, channels):
    """Uncompressed little endian signed samples -> list of channel tuples."""
    step = nbytes * channels
    return [tuple(signed(int.from_bytes(data[i + c * nbytes:i + (c + 1) * nbytes], "little"), 8 * nbytes)
                  for c in range(channels)) for i in range(0, len(data) - step + 1, step)]


def unpack_delta_frame(data, res_bits, channels):
    """Polar delta frame: reference sample, then blocks of
    [delta bits][sample count][deltas bit-packed LSB first]."""
    nbytes = math.ceil(res_bits / 8)
    ref = unpack_samples(data[:nbytes * channels], nbytes, channels)[0]
    samples = [ref]
    i = nbytes * channels
    while i + 2 <= len(data):
        bits, count = data[i], data[i + 1]
        i += 2
        if bits == 0 or count == 0:
            break
        nb = math.ceil(bits * channels * count / 8)
        block = int.from_bytes(data[i:i + nb], "little")
        i += nb
        mask = (1 << bits) - 1
        pos = 0
        for _ in range(count):
            prev = samples[-1]
            d = []
            for c in range(channels):
                d.append(signed((block >> pos) & mask, bits))
                pos += bits
            samples.append(tuple(prev[c] + d[c] for c in range(channels)))
    return samples


class Decoder:
    """Turns PMD data words into samples using the latest STREAM_START settings.

    feed(word) returns a list of (measurement name, polar_ts_ns, rtc_ms, values)
    where polar_ts_ns is the interpolated sample time on the Polar's clock and
    values are scaled by the stream's FACTOR when the Polar provided one.
    HEART_RATE words give "hr" rows (bpm,) and one "rr" row (ms,) per RR
    interval, CADENCE words "cadence" rows (steps/min,), all at the word's
    timestamps."""

    def __init__(self):
        self.streams = {}   # mt -> dict(rate, res, ch, factor, last_ts)

    def feed(self, w):
        if w.type == W_STREAM_START:
            mt, req, resp = parse_stream_start(w.payload)
            self.streams[mt] = {
                "rate": (req.get("rate") or [0])[0], "res": (req.get("res") or [16])[0],
                "ch": (req.get("ch") or [3])[0], "factor": (resp.get("factor") or [None])[0],
                "last_ts": None}
            return []
        if w.type == W_HEART_RATE:
            bpm, _, rr = parse_heart_rate(w.payload)
            return [("hr", w.polar_ts, w.rtc_ms, (bpm,))] + [("rr", w.polar_ts, w.rtc_ms, (x,)) for x in rr]
        if w.type == W_CADENCE:
            return [("cadence", w.polar_ts, w.rtc_ms, (parse_cadence(w.payload)[0],))]
        if w.type < W_PMD_BASE:
            return []
        mt = w.type & 0x7F
        st = self.streams.setdefault(mt, {"rate": 0, "res": 16, "ch": 3, "factor": None, "last_ts": None})
        frame, body = w.payload[0], w.payload[1:]
        if mt == 0 and frame == 0:                       # ECG, 3 byte uV
            vals = unpack_samples(body, 3, 1)
        elif frame & 0x80:                               # delta compressed
            vals = unpack_delta_frame(body, st["res"], st["ch"])
        elif mt == 2 and frame in (0, 1, 2):             # ACC 8/16/24 bit, 3 channels, mG
            vals = unpack_samples(body, frame + 1, 3)
        else:
            return []
        if st["factor"]:
            vals = [tuple(v * st["factor"] for v in s) for s in vals]
        n = len(vals)
        if st["last_ts"] is not None and w.polar_ts > st["last_ts"]:
            dt = (w.polar_ts - st["last_ts"]) / n
        else:
            dt = 1e9 / st["rate"] if st["rate"] else 0
        st["last_ts"] = w.polar_ts
        name = MT_NAMES.get(mt, str(mt))
        # the PMD timestamp belongs to the last sample of the frame
        return [(name, int(w.polar_ts - (n - 1 - i) * dt), w.rtc_ms - (n - 1 - i) * dt / 1e6, v)
                for i, v in enumerate(vals)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--words", action="store_true", help="print every word")
    ap.add_argument("--csv", metavar="OUTDIR", help="write one CSV per measurement")
    a = ap.parse_args()

    dec = Decoder()
    counts = collections.Counter()
    samples = collections.Counter()
    span_ns = {}
    writers = {}
    first = last = None
    for w in read_words(a.file):
        counts[word_name(w.type)] += 1
        first = first or w
        last = w
        if a.words or (w.type < W_PMD_BASE and not a.csv):
            print("%8d %-16s polar=%d rtc=%d  %s" % (w.offset, word_name(w.type), w.polar_ts, w.rtc_ms, describe(w)))
        for name, pts, rtc, vals in dec.feed(w):
            samples[name] += 1
            span_ns.setdefault(name, [pts, pts])[1] = pts
            if a.csv:
                if name not in writers:
                    os.makedirs(a.csv, exist_ok=True)
                    f = open(os.path.join(a.csv, name + ".csv"), "w")
                    cols = ["x", "y", "z"][:len(vals)] if len(vals) > 1 else \
                        [{"hr": "bpm", "rr": "ms", "cadence": "steps_per_min"}.get(name, "value")]
                    f.write("polar_ns,rtc_ms," + ",".join(cols) + "\n")
                    writers[name] = f
                writers[name].write("%d,%.3f,%s\n" % (pts, rtc, ",".join("%g" % v for v in vals)))
    for f in writers.values():
        f.close()
    if first is None:
        sys.exit("no words in file")
    print("words:", dict(counts))
    span = (last.rtc_ms - first.rtc_ms) / 1000
    print("duration: %.1f s (feather clock)" % span)
    for name, n in samples.items():
        s = (span_ns[name][1] - span_ns[name][0]) / 1e9
        if name in ("hr", "rr", "cadence"):
            print("%s: %d values" % (name, n))
        else:
            print("%s: %d samples, %.2f Hz (Polar clock)" % (name, n, (n - 1) / s if s else 0))


if __name__ == "__main__":
    main()

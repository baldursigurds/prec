#!/usr/bin/env python3
"""prec_merge.py - merge Polar H10 accelerometer recordings with GPS speed.

A *pair* is a JSON file in the pairs folder that joins one recording (a PREC
stream .prc in the data folder) with one Garmin .fit file in the gps folder,
plus a time offset, sub-intervals, run/walk/still/stretch intervals and the
name of the *cooked* array: a numpy (m, 5) float64 array in the cooked folder
with columns  t, ax, ay, az, speed, where t is seconds since the .fit file
starts, a* are the H10 accelerometer values (mG) and speed is the .fit
enhanced_speed (m/s) interpolated to every accelerometer sample.
Cooking also writes one array per class with intervals: <name>_run.npy and
<name>_walk.npy (same columns, the cooked rows inside those intervals) and
<name>_still.npy and <name>_stretch.npy (t, ax, ay, az: no speed, taken from
the whole recording, as these intervals may lie outside the GPS span).

Settings: ~/.config/prec/prec.conf (TOML, see write_default_conf / docs/MERGE.md),
or the file named by the PREC_CONF environment variable.

Command line (the TUI's F3 tab does the same interactively):
  prec_merge.py list                         pairs and their state
  prec_merge.py update                       pair new recordings with overlapping .fit files
  prec_merge.py cook NAME|--all              (re)build the cooked and class arrays of a pair
  prec_merge.py delete NAME                  delete a pair's JSON file (nothing else)
  prec_merge.py legacy BIN FIT [--offset S]  import a legacy sensors.bin with its .fit
  prec_merge.py offset NAME                  estimate the offset from the data
  prec_merge.py write-config                 write a commented default prec.conf
"""
import argparse, copy, datetime as dt, glob, hashlib, json, os, shutil, struct, sys, time, tomllib
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prec_decode as pdec   # noqa: E402

CONF_PATH = os.path.expanduser("~/.config/prec/prec.conf")
CLASSES = ("run", "walk", "still", "stretch")
COLUMNS = ["t", "ax", "ay", "az", "speed"]
CLASS_COLUMNS = {"run": COLUMNS, "walk": COLUMNS, "still": COLUMNS[:4], "stretch": COLUMNS[:4]}

DEFAULTS = {
    "paths": {
        "data": "~/prec/data",          # PREC streams (.prc), also where the TUI syncs to
        "gps": "~/prec/gps",            # Garmin .fit files
        "pairs": "~/prec/pairs",        # pair .json files
        "cooked": "~/prec/cooked",      # merged numpy arrays
        "avoid": "~/prec/avoid",        # rectangles to leave out, one .json each
        "legacy": "~/accrnr/data",      # where the legacy .bin file browser starts
        "cache": "~/.cache/prec",       # parsed .fit files, map tiles
        "models": "~/prec/models",      # trained models (.keras, .json, .tflite, .js, .c)
        "arch": "~/prec/arch",          # your model architectures (.py with create_model)
        "class": "~/prec/class",        # your classifier architectures (.py with create_model)
        "train": "~/prec/train.json",   # training parameters (edited in the TUI's F4 tab)
        "firmware": "~/.local/share/prec/firmware/prec_logger",   # firmware copy when the installed one is read-only
    },
    "pairing": {
        "min_overlap": 60.0,            # s of common time needed to pair automatically
        "min_samples": 2000,            # recordings with fewer ACC samples are ignored
        "min_interval": 10.0,           # s; shorter pieces left by avoid rectangles are dropped
    },
    "offset": {
        "step_fine": 0.1, "step": 1.0, "step_coarse": 10.0,   # s, offset window buttons
        "auto_range": 300.0,            # s searched either side by "Auto"
    },
    "plot": {
        "decimate": 10,                 # plot every n-th accelerometer sample
        "acc_x": "#1f77b4", "acc_y": "#2ca02c", "acc_z": "#9467bd", "speed": "#d62728",
        "interval": "#1f77b4", "track": "#666666",
        "run": "#2ca02c", "walk": "#ffbf00", "still": "#d62728", "stretch": "#7b2cbf",
    },
    "map": {
        "tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "user_agent": "prec-tools/1.0 (personal data analysis)",
        "max_tiles": 36,                # tiles per map view (chooses the zoom level)
    },
    "legacy": {
        "rate": 200,                    # nominal ACC rate written to converted streams
    },
}

DEFAULT_CONF = '''# PREC data merging configuration (TOML). Delete a line to use the default.

[paths]
data   = "~/prec/data"      # PREC streams (.prc); the TUI also syncs here
gps    = "~/prec/gps"       # Garmin .fit files
pairs  = "~/prec/pairs"     # pair .json files
cooked = "~/prec/cooked"    # merged numpy arrays (t, ax, ay, az, speed)
avoid  = "~/prec/avoid"     # avoid rectangles, one .json each
legacy = "~/accrnr/data"    # where the legacy .bin file browser starts
cache  = "~/.cache/prec"    # parsed .fit files and map tiles
models = "~/prec/models"    # trained models (.keras, .json, .tflite, .js, .c)
arch   = "~/prec/arch"      # your model architectures (.py files with create_model)
class  = "~/prec/class"     # your classifier architectures (.py files with create_model(input_shape, n_classes))
train  = "~/prec/train.json"  # training parameters (edited in the TUI's F4 tab)
firmware = "~/.local/share/prec/firmware/prec_logger"  # used when the installed firmware is read-only

[pairing]
min_overlap  = 60.0         # seconds of common time needed to pair automatically
min_samples  = 2000         # recordings with fewer accelerometer samples are ignored
min_interval = 10.0         # seconds; shorter pieces left by avoid rectangles are dropped

[offset]
step_fine   = 0.1           # offset window buttons, seconds
step        = 1.0
step_coarse = 10.0
auto_range  = 300.0         # "Auto" searches this many seconds either side

[plot]
decimate = 10               # plot every n-th accelerometer sample
acc_x = "#1f77b4"
acc_y = "#2ca02c"
acc_z = "#9467bd"
speed = "#d62728"
interval = "#1f77b4"
track = "#666666"
run = "#2ca02c"             # green
walk = "#ffbf00"            # amber
still = "#d62728"           # red
stretch = "#7b2cbf"         # purple

[map]
tile_url   = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
user_agent = "prec-tools/1.0 (personal data analysis)"
max_tiles  = 36

[legacy]
rate = 200                  # nominal accelerometer rate written to converted streams
'''


# ---- configuration --------------------------------------------------------

def load_conf(path=None):
    """Settings merged over DEFAULTS; paths expanded. path defaults to $PREC_CONF
    or ~/.config/prec/prec.conf (a missing file means all defaults)."""
    path = path or os.environ.get("PREC_CONF") or CONF_PATH
    conf = copy.deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for section, values in user.items():
            if isinstance(values, dict):
                conf.setdefault(section, {}).update(values)
    for k, v in conf["paths"].items():
        conf["paths"][k] = os.path.expanduser(v)
    conf["_path"] = path
    return conf


def write_default_conf(path=None):
    """Write the commented default prec.conf (refuses to overwrite). Returns the path."""
    path = path or os.environ.get("PREC_CONF") or CONF_PATH
    if os.path.exists(path):
        raise FileExistsError(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(DEFAULT_CONF)
    return path


def ensure_dirs(conf):
    """Create the data, gps, pairs, cooked, avoid and cache folders."""
    for k in ("data", "gps", "pairs", "cooked", "avoid", "cache", "models", "arch", "class"):
        os.makedirs(conf["paths"][k], exist_ok=True)


def iso(t):
    """Unix seconds -> ISO 8601 UTC text with milliseconds."""
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def from_iso(s):
    """ISO 8601 text -> unix seconds."""
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# ---- PREC streams ---------------------------------------------------------

def _acc_samples(frame, body, res, ch, factor):
    """Decode one PMD ACC frame (payload after the frame type byte) -> (n, 3) float array."""
    if frame & 0x80:
        a = np.array(pdec.unpack_delta_frame(body, res, ch), dtype=np.float64)
        return a * factor if factor else a
    nb = frame + 1
    if nb == 2:
        return np.frombuffer(body[:len(body) // 6 * 6], "<i2").reshape(-1, 3).astype(np.float64)
    if nb == 1:
        return np.frombuffer(body[:len(body) // 3 * 3], "i1").reshape(-1, 3).astype(np.float64)
    if nb == 3:
        raw = np.frombuffer(body[:len(body) // 9 * 9], "u1").reshape(-1, 3, 3).astype(np.int32)
        v = raw[..., 0] | raw[..., 1] << 8 | raw[..., 2] << 16
        return np.where(v & 0x800000, v - (1 << 24), v).astype(np.float64)
    return np.zeros((0, 3))


def _sample_times(ts, counts, rate):
    """Per-sample Polar times (ns) from per-frame last-sample times and sample
    counts: samples are spread evenly back to the previous frame's timestamp,
    or at the nominal rate after a gap."""
    ts = np.asarray(ts, dtype=np.int64)
    counts = np.asarray(counts, dtype=np.int64)
    nominal = 1e9 / rate
    prev = np.r_[ts[0] - counts[0] * nominal, ts[:-1]]
    step = (ts - prev) / np.maximum(counts, 1)
    bad = (step <= nominal / 3) | (step >= nominal * 3)
    step[bad] = nominal
    total = int(counts.sum())
    word = np.repeat(np.arange(len(ts)), counts)
    first = np.r_[0, np.cumsum(counts)[:-1]]
    within = np.arange(total) - first[word]
    back = counts[word] - 1 - within
    return ts[word] - np.round(back * step[word]).astype(np.int64)


def clock_map(polar_ns, rtc_ms):
    """Map the Polar clock to UTC from per-frame (Polar time, feather arrival
    time) pairs: a least squares line, moved down to the 5th percentile of the
    residuals because BLE arrival delays are only ever positive.
    Returns f(polar_ns array) -> unix seconds."""
    x = np.asarray(polar_ns, dtype=np.float64) / 1e9
    y = np.asarray(rtc_ms, dtype=np.float64) / 1e3
    x0, y0 = x[0], y[0]
    if len(x) < 2 or x[-1] - x0 < 1:
        a, b = 1.0, float(np.min(y - x)) + x0 - y0
    else:
        a, b = np.polyfit(x - x0, y - y0, 1)
        b += np.percentile(y - y0 - (a * (x - x0) + b), 5)
    return lambda p: a * (np.asarray(p, dtype=np.float64) / 1e9 - x0) + b + y0


def read_prc(path):
    """Accelerometer data of a PREC stream.
    Returns {"utc": (n,) unix s, "polar": (n,) Polar ns, "acc": (n, 3) mG,
    "rate": nominal Hz, "start", "end": unix s, "frames": number of data words}."""
    data = open(path, "rb").read()
    rate, res, ch, factor = 200.0, 16, 3, None
    ts, rtc, counts, chunks = [], [], [], []
    for w in _iter_words(data):
        t, pts, rms, p = w
        if t == pdec.W_STREAM_START and p and p[0] == 2:
            _, req, resp = pdec.parse_stream_start(p)
            rate = float((req.get("rate") or [rate])[0])
            res = (req.get("res") or [res])[0]
            ch = (req.get("ch") or [ch])[0]
            factor = (resp.get("factor") or [None])[0]
        elif t == 0x82 and len(p) > 1:
            a = _acc_samples(p[0], p[1:], res, ch, factor)
            if len(a):
                ts.append(pts)
                rtc.append(rms)
                counts.append(len(a))
                chunks.append(a)
    if not chunks:
        raise ValueError("%s has no accelerometer data" % os.path.basename(path))
    polar = _sample_times(ts, counts, rate)
    utc = clock_map(ts, rtc)(polar)
    return {"utc": utc, "polar": polar, "acc": np.concatenate(chunks), "rate": rate,
            "start": float(utc[0]), "end": float(utc[-1]), "frames": len(ts)}


def _iter_words(data):
    """(type, polar_ts, rtc_ms, payload) for every complete word in a PREC byte string."""
    off, n = 0, len(data)
    hdr = pdec.HEADER
    while off + hdr.size <= n:
        t, ln, pts, rtc = hdr.unpack_from(data, off)
        end = off + hdr.size + ln
        if end > n:
            break
        yield t, pts, rtc, data[off + hdr.size:end]
        off = end


def prc_span(path):
    """Quick (start, end, samples) of a PREC stream from its data word headers
    (feather clock, unix s); samples is approximate for compressed frames."""
    data = open(path, "rb").read()
    first = last = None
    samples = 0
    for t, pts, rtc, p in _iter_words(data):
        if t == 0x82 and len(p) > 1:
            first = rtc if first is None else first
            last = rtc
            samples += (len(p) - 1) // 6 if p[0] == 1 else max(1, (len(p) - 1) // 6)
    if first is None:
        return None
    return first / 1e3, last / 1e3, samples


# ---- legacy sensors.bin ---------------------------------------------------

def _legacy_header(d, p):
    return p + 10 <= len(d) and d[p] == 2 and d[p + 9] in (0, 1, 2)


def read_legacy_bin(path):
    """Raw PMD ACC notifications written back to back (no lengths, as recorded
    by the earlier logger): [0x02][u64 Polar ts][frame type][samples], all the
    same size, up to filler bytes at the end. The packet size is detected.
    Returns {"packets": [(ts, payload from the frame type on)], "polar", "acc",
    "packet_size", "frames"}."""
    d = open(path, "rb").read()
    p0 = next((p for p in range(min(len(d), 1024)) if _legacy_header(d, p)), None)
    if p0 is None:
        raise ValueError("%s: no PMD packet found" % os.path.basename(path))
    ts0 = struct.unpack_from("<Q", d, p0 + 1)[0]
    size = None
    for s in range(16, 600):
        ok = True
        for k in (1, 2, 3):
            q = p0 + k * s
            if not _legacy_header(d, q):
                ok = False
                break
            tq = struct.unpack_from("<Q", d, q + 1)[0]
            tp = struct.unpack_from("<Q", d, q - s + 1)[0]
            if not 0 < tq - tp < 2_000_000_000:
                ok = False
                break
        if ok:
            size = s
            break
    if size is None:
        raise ValueError("%s: cannot find the packet size" % os.path.basename(path))
    packets, prev = [], ts0 - 1
    p = p0
    while _legacy_header(d, p) and p + size <= len(d):
        ts = struct.unpack_from("<Q", d, p + 1)[0]
        if ts <= prev or ts - prev > 3_600_000_000_000:
            break
        packets.append((ts, d[p + 9:p + size]))
        prev = ts
        p += size
    accs = [_acc_samples(pl[0], pl[1:], 16, 3, None) for _, pl in packets]
    counts = [len(a) for a in accs]
    return {"packets": packets, "polar": _sample_times([t for t, _ in packets], counts, 200.0),
            "acc": np.concatenate(accs), "packet_size": size, "frames": len(packets)}


def legacy_to_prc(bin_path, out_path, start_utc, rate=200):
    """Write a legacy .bin as a PREC stream. The first packet's samples are
    placed at start_utc (unix s); the feather timestamps follow the Polar
    clock exactly. Returns (frames, samples)."""
    leg = read_legacy_bin(bin_path)
    pk = leg["packets"]
    ts0 = pk[0][0]
    n0 = len(_acc_samples(pk[0][1][0], pk[0][1][1:], 16, 3, None))
    t_first = ts0 - (n0 - 1) * 1e9 / rate      # time of the very first sample
    out = bytearray()

    def word(t, polar, rtc_ms, payload):
        out.extend(pdec.HEADER.pack(t, len(payload), int(polar), int(round(rtc_ms))))
        out.extend(payload)

    start_ms = start_utc * 1e3
    name = ("legacy " + os.path.basename(bin_path)).encode()[:60]
    fw = b"legacy"
    word(pdec.W_SESSION_START, 0, start_ms, b"PREC" + bytes([1]) + bytes(6) + bytes([0]) +
         bytes([len(name)]) + name + bytes([len(fw)]) + fw)
    req = bytes([0, 1]) + struct.pack("<H", rate) + bytes([1, 1]) + struct.pack("<H", 16)
    word(pdec.W_STREAM_START, ts0, start_ms, bytes([2, len(req)]) + req)
    word(pdec.W_MESSAGE, ts0, start_ms, ("converted from %s" % os.path.abspath(bin_path)).encode()[:200])
    for ts, payload in pk:
        word(0x82, ts, start_ms + (ts - t_first) / 1e6, payload)
    last = pk[-1][0]
    word(pdec.W_STREAM_STOP, last, start_ms + (last - t_first) / 1e6, bytes([2]))
    word(pdec.W_SESSION_END, last, start_ms + (last - t_first) / 1e6, bytes([0]))
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(out)
    os.replace(tmp, out_path)
    return len(pk), len(leg["acc"])


# ---- .fit files -----------------------------------------------------------

def _cache_key(path):
    st = os.stat(path)
    return hashlib.sha1(("%s|%d|%d" % (os.path.abspath(path), st.st_size, int(st.st_mtime))).encode()).hexdigest()[:16]


def read_fit(path, conf=None):
    """GPS data of a .fit file: {"start": unix s (session start, else first record),
    "end", "t": (k,) s since start, "speed": m/s (enhanced_speed, NaN if missing),
    "lat", "lon": degrees (NaN if missing), "sport"}. Parsed files are cached."""
    cache = None
    if conf:
        cache = os.path.join(conf["paths"]["cache"], "fit", _cache_key(path) + ".npz")
        if os.path.exists(cache):
            z = np.load(cache, allow_pickle=False)
            return {"start": float(z["start"]), "end": float(z["end"]), "t": z["t"], "speed": z["speed"],
                    "lat": z["lat"], "lon": z["lon"], "sport": str(z["sport"])}
    import fitparse
    ff = fitparse.FitFile(path)
    utc = dt.timezone.utc
    ts, sp, la, lo = [], [], [], []
    for m in ff.get_messages("record"):
        v = m.get_values()
        t = v.get("timestamp")
        if t is None:
            continue
        ts.append(t.replace(tzinfo=utc).timestamp())
        s = v.get("enhanced_speed", v.get("speed"))
        sp.append(np.nan if s is None else float(s))
        lat, lon = v.get("position_lat"), v.get("position_long")
        la.append(np.nan if lat is None else lat * 180.0 / 2 ** 31)
        lo.append(np.nan if lon is None else lon * 180.0 / 2 ** 31)
    if not ts:
        raise ValueError("%s has no records" % os.path.basename(path))
    start, sport = ts[0], ""
    for m in ff.get_messages("session"):
        v = m.get_values()
        if v.get("start_time"):
            start = min(start, v["start_time"].replace(tzinfo=utc).timestamp())
        sport = str(v.get("sport") or "")
        break
    out = {"start": start, "end": ts[-1], "t": np.array(ts) - start, "speed": np.array(sp),
           "lat": np.array(la), "lon": np.array(lo), "sport": sport}
    if cache:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        np.savez(cache, **{k: out[k] for k in ("start", "end", "t", "speed", "lat", "lon", "sport")})
    return out


def fit_name_for(fit):
    """Garmin style file name from the start time: YYYY-MM-DD-HH-MM-SS.fit (UTC)."""
    return dt.datetime.fromtimestamp(fit["start"], dt.timezone.utc).strftime("%Y-%m-%d-%H-%M-%S") + ".fit"


def import_fit(conf, path):
    """Make sure a .fit file is in the gps folder (copied, named by its start
    time, unless an identical file is already there). Returns its name there."""
    gps = conf["paths"]["gps"]
    if os.path.dirname(os.path.abspath(path)) == os.path.abspath(gps):
        return os.path.basename(path)
    fit = read_fit(path, conf)
    name = fit_name_for(fit)
    dest = os.path.join(gps, name)
    if os.path.exists(dest):
        if open(dest, "rb").read() == open(path, "rb").read():
            return name
        name = name[:-4] + "-" + _cache_key(path)[:6] + ".fit"
        dest = os.path.join(gps, name)
    os.makedirs(gps, exist_ok=True)
    shutil.copy2(path, dest)
    return name


# ---- spans index (for update) ---------------------------------------------

def _spans(conf):
    """{"prc": {name: [start, end, samples]}, "fit": {name: [start, end]}} for
    the data and gps folders, cached by file size and time."""
    path = os.path.join(conf["paths"]["cache"], "spans.json")
    try:
        idx = json.load(open(path))
    except (OSError, ValueError):
        idx = {}
    out = {"prc": {}, "fit": {}}
    for kind, folder, pattern in (("prc", "data", "*.prc"), ("fit", "gps", "*.fit")):
        for f in sorted(glob.glob(os.path.join(conf["paths"][folder], pattern))):
            st = os.stat(f)
            key = "%s|%s|%d|%d" % (kind, f, st.st_size, int(st.st_mtime))
            if key not in idx:
                try:
                    if kind == "prc":
                        s = prc_span(f)
                        idx[key] = list(s) if s else None
                    else:
                        fit = read_fit(f, conf)
                        idx[key] = [fit["start"], fit["end"]]
                except Exception:
                    idx[key] = None
            if idx[key]:
                out[kind][os.path.basename(f)] = idx[key]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(idx, open(path, "w"))
    return out


def overlap(a, b):
    """Seconds two (start, end) spans have in common (0 if none)."""
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def recordings(conf):
    """[(name, start, end, samples, paired pair name or None)] for the data folder."""
    sp = _spans(conf)
    used = {p["prc"]: p["name"] for p in list_pairs(conf)}
    return [(n, s[0], s[1], s[2], used.get(n)) for n, s in sorted(sp["prc"].items())]


def gps_files(conf, span=None):
    """[(name, start, end, overlap with span)] for the gps folder, most overlap first if span given."""
    sp = _spans(conf)
    rows = [(n, s[0], s[1], overlap(span, s) if span else 0.0) for n, s in sp["fit"].items()]
    return sorted(rows, key=lambda r: (-r[3], r[1])) if span else sorted(rows, key=lambda r: r[1])


# ---- pairs ----------------------------------------------------------------

def pair_path(conf, name):
    return os.path.join(conf["paths"]["pairs"], name + ".json")


def load_pair(conf, name):
    with open(pair_path(conf, name)) as f:
        return json.load(f)


def save_pair(conf, pair):
    """Write a pair file atomically (stamps "updated")."""
    pair["updated"] = iso(time.time())
    os.makedirs(conf["paths"]["pairs"], exist_ok=True)
    path = pair_path(conf, pair["name"])
    with open(path + ".tmp", "w") as f:
        json.dump(pair, f, indent=2)
    os.replace(path + ".tmp", path)


def list_pairs(conf):
    """All pairs, sorted by their .fit start time."""
    out = []
    for f in glob.glob(os.path.join(conf["paths"]["pairs"], "*.json")):
        try:
            with open(f) as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            pass
    return sorted(out, key=lambda p: p.get("fit_start", ""))


def delete_pair(conf, name):
    """Delete only the pair's .json file (the .prc, .fit and cooked array stay)."""
    os.remove(pair_path(conf, name))


def unique_name(conf, base):
    name, k = base, 2
    while os.path.exists(pair_path(conf, name)):
        name, k = "%s-%d" % (base, k), k + 1
    return name


def new_pair(conf, prc, fit, offset=0.0, source="prec", legacy_bin=None, name=None):
    """A new pair dict (not yet built or saved) for data/<prc> and gps/<fit>."""
    name = name or unique_name(conf, os.path.splitext(prc)[0])
    return {
        "version": 1, "name": name, "source": source,
        "prc": prc, "fit": fit, "legacy_bin": legacy_bin,
        "offset": float(offset), "offset_confirmed": False,
        "intervals": [], "intervals_auto": True,
        "run": [], "walk": [], "still": [], "stretch": [],
        "cooked": None, "columns": COLUMNS, "samples": 0,
        "created": iso(time.time()),
    }


# ---- avoid rectangles -----------------------------------------------------

def list_avoid(conf):
    """Avoid rectangles: [{"name", "south", "north", "west", "east", ...}], by name."""
    out = []
    for f in sorted(glob.glob(os.path.join(conf["paths"]["avoid"], "*.json"))):
        try:
            with open(f) as fh:
                r = json.load(fh)
            r["name"] = os.path.splitext(os.path.basename(f))[0]
            out.append(r)
        except (OSError, ValueError):
            pass
    return out


def save_avoid(conf, rect):
    """Save a rectangle (name, south, north, west, east); the name becomes the file name."""
    r = dict(rect)
    r["south"], r["north"] = sorted((float(r["south"]), float(r["north"])))
    r["west"], r["east"] = sorted((float(r["west"]), float(r["east"])))
    os.makedirs(conf["paths"]["avoid"], exist_ok=True)
    with open(os.path.join(conf["paths"]["avoid"], r["name"] + ".json"), "w") as f:
        json.dump(r, f, indent=2)


def delete_avoid(conf, name):
    os.remove(os.path.join(conf["paths"]["avoid"], name + ".json"))


def new_avoid_name(conf):
    names = {r["name"] for r in list_avoid(conf)}
    k = 1
    while "area-%d" % k in names:
        k += 1
    return "area-%d" % k


def inside_any(lat, lon, rects):
    """Boolean mask: positions inside any avoid rectangle (NaN positions are outside)."""
    m = np.zeros(len(lat), dtype=bool)
    for r in rects:
        m |= (lat >= r["south"]) & (lat <= r["north"]) & (lon >= r["west"]) & (lon <= r["east"])
    return m


def auto_intervals(fit, t_lo, t_hi, rects, min_len):
    """Maximal sub-intervals of [t_lo, t_hi] (s since the .fit start) during
    which the GPS track is outside every avoid rectangle; pieces shorter than
    min_len are dropped. Without rectangles: the whole [t_lo, t_hi]."""
    if t_hi <= t_lo:
        return []
    t = fit["t"]
    sel = (t >= t_lo) & (t <= t_hi)
    if not rects or not sel.any():
        return [[round(t_lo, 3), round(t_hi, 3)]]
    tt = t[sel]
    bad = inside_any(fit["lat"][sel], fit["lon"][sel], rects)
    out, start = [], None
    for k in range(len(tt)):
        if not bad[k] and start is None:
            start = t_lo if k == 0 else tt[k]
        if bad[k] and start is not None:
            out.append([start, tt[k - 1]])
            start = None
    if start is not None:
        out.append([start, t_hi])
    return [[round(a, 3), round(b, 3)] for a, b in out if b - a >= min_len]


def normalize_intervals(iv, lo=None, hi=None):
    """Sort, clip to [lo, hi], drop empty ones and merge overlapping ones
    (intervals that only touch, e.g. the two halves of a split, stay apart)."""
    out = []
    for a, b in sorted(sorted(x) for x in iv):
        if lo is not None:
            a = max(a, lo)
        if hi is not None:
            b = min(b, hi)
        if b - a <= 0:
            continue
        if out and a < out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[round(a, 3), round(b, 3)] for a, b in out]


# ---- building the cooked array ----------------------------------------------

def load_acc(conf, pair):
    """read_prc of the pair's stream."""
    return read_prc(os.path.join(conf["paths"]["data"], pair["prc"]))


def load_fit(conf, pair):
    return read_fit(os.path.join(conf["paths"]["gps"], pair["fit"]), conf)


def merge(acc, fit, offset):
    """The (m, 5) array t, ax, ay, az, speed: accelerometer samples (shifted by
    offset seconds) that fall between the first and last valid .fit speed, with
    the speed linearly interpolated to each sample; t in s since the .fit start."""
    t = acc["utc"] + offset - fit["start"]
    ok = ~np.isnan(fit["speed"])
    ft, fs = fit["t"][ok], fit["speed"][ok]
    if len(ft) < 2:
        return np.zeros((0, 5))
    keep = (t >= ft[0]) & (t <= ft[-1])
    return np.column_stack([t[keep], acc["acc"][keep], np.interp(t[keep], ft, fs)]).astype(np.float64)


def in_intervals(t, iv):
    """Boolean mask of the times t inside any of the intervals."""
    m = np.zeros(len(t), bool)
    for a, b in iv:
        m |= (t >= a) & (t <= b)
    return m


def class_array_path(conf, pair, cls):
    return os.path.join(conf["paths"]["cooked"], "%s_%s.npy" % (pair["name"], cls))


def write_class_arrays(conf, pair, acc, fit, arr):
    """<name>_<class>.npy for every class with intervals (removing those of
    classes without): run and walk are the cooked rows (t, ax, ay, az, speed)
    inside their intervals, still and stretch the samples (t, ax, ay, az) of
    the whole recording inside theirs. Sets pair["class_arrays"] = {class:
    samples}; does not save the pair."""
    t_all = acc["utc"] + pair["offset"] - fit["start"]
    out = {}
    for c in CLASSES:
        path = class_array_path(conf, pair, c)
        if not pair.get(c):
            if os.path.exists(path):
                os.remove(path)
            continue
        if CLASS_COLUMNS[c] == COLUMNS:
            a = arr[in_intervals(arr[:, 0], pair[c])]
        else:
            m = in_intervals(t_all, pair[c])
            a = np.column_stack([t_all[m], acc["acc"][m]]).astype(np.float64)
        np.save(path, a)
        out[c] = int(len(a))
    pair["class_arrays"] = out


def load_class_array(conf, pair, cls):
    """A pair's array of one class (see write_class_arrays), or None."""
    p = class_array_path(conf, pair, cls)
    return np.load(p) if cls in pair.get("class_arrays", {}) and os.path.exists(p) else None


def build_pair(conf, pair, acc=None, fit=None):
    """Cook a pair: cooked array, class arrays, spans, and, while they are
    automatic, the sub-intervals from the avoid rectangles. Saves the pair;
    returns it."""
    acc = acc or load_acc(conf, pair)
    fit = fit or load_fit(conf, pair)
    arr = merge(acc, fit, pair["offset"])
    os.makedirs(conf["paths"]["cooked"], exist_ok=True)
    cooked = pair["name"] + ".npy"
    np.save(os.path.join(conf["paths"]["cooked"], cooked), arr)
    pair.update({
        "cooked": cooked, "columns": COLUMNS, "samples": int(len(arr)),
        "t_start": round(float(arr[0, 0]), 3) if len(arr) else 0.0,
        "t_end": round(float(arr[-1, 0]), 3) if len(arr) else 0.0,
        "prc_start": iso(acc["start"]), "prc_end": iso(acc["end"]),
        "fit_start": iso(fit["start"]), "fit_end": iso(fit["end"]),
        "overlap_s": round(overlap((acc["start"] + pair["offset"], acc["end"] + pair["offset"]),
                                   (fit["start"], fit["end"])), 1),
        "built": iso(time.time()),
    })
    if pair.get("intervals_auto", True) or not pair.get("intervals"):
        pair["intervals"] = auto_intervals(fit, pair["t_start"], pair["t_end"], list_avoid(conf),
                                           conf["pairing"]["min_interval"])
        pair["intervals_auto"] = True
    write_class_arrays(conf, pair, acc, fit, arr)
    save_pair(conf, pair)
    return pair


def load_cooked(conf, pair):
    """The pair's cooked (m, 5) array (None if not built)."""
    if not pair.get("cooked"):
        return None
    p = os.path.join(conf["paths"]["cooked"], pair["cooked"])
    return np.load(p) if os.path.exists(p) else None


def reapply_avoid(conf):
    """Recompute the sub-intervals of every pair whose intervals are still
    automatic (after the avoid rectangles changed). Returns the names changed."""
    rects, changed = list_avoid(conf), []
    for p in list_pairs(conf):
        if not p.get("intervals_auto", True) or not p.get("cooked"):
            continue
        iv = auto_intervals(load_fit(conf, p), p.get("t_start", 0), p.get("t_end", 0), rects,
                            conf["pairing"]["min_interval"])
        if iv != p.get("intervals"):
            p["intervals"] = iv
            save_pair(conf, p)
            changed.append(p["name"])
    return changed


# ---- update and import ------------------------------------------------------

def update(conf, progress=None):
    """Pair every unpaired recording in the data folder with the .fit file in
    the gps folder it overlaps most (at least min_overlap s), and build it.
    progress(text) reports each step. Returns {"created": [names],
    "unmatched": [recordings], "skipped": [too short]}."""
    ensure_dirs(conf)
    sp = _spans(conf)
    used = {p["prc"] for p in list_pairs(conf)}
    report = {"created": [], "unmatched": [], "skipped": []}
    for prc, (s, e, n) in sorted(sp["prc"].items()):
        if prc in used:
            continue
        if n < conf["pairing"]["min_samples"]:
            report["skipped"].append(prc)
            continue
        best = max(((overlap((s, e), f), name) for name, f in sp["fit"].items()), default=(0, None))
        if best[0] < conf["pairing"]["min_overlap"]:
            report["unmatched"].append(prc)
            continue
        if progress:
            progress("pairing %s with %s" % (prc, best[1]))
        pair = new_pair(conf, prc, best[1])
        build_pair(conf, pair)
        report["created"].append(pair["name"])
    return report


def import_legacy(conf, bin_path, fit_path, offset=0.0, progress=None):
    """Legacy import: copy the .fit into the gps folder, convert the .bin into a
    PREC stream in the data folder whose first sample is at the .fit start plus
    offset, then create and build a pair. Returns the pair."""
    ensure_dirs(conf)
    if progress:
        progress("reading %s" % os.path.basename(fit_path))
    fit_name = import_fit(conf, fit_path)
    fit = read_fit(os.path.join(conf["paths"]["gps"], fit_name), conf)
    stem = dt.datetime.fromtimestamp(fit["start"] + offset, dt.timezone.utc).strftime("%Y%m%d-%H%M%S") + "-legacy"
    prc = stem + ".prc"
    k = 2
    while os.path.exists(os.path.join(conf["paths"]["data"], prc)):
        prc, k = "%s-%d.prc" % (stem, k), k + 1
    if progress:
        progress("converting %s" % os.path.basename(bin_path))
    legacy_to_prc(bin_path, os.path.join(conf["paths"]["data"], prc), fit["start"] + offset,
                  int(conf["legacy"]["rate"]))
    pair = new_pair(conf, prc, fit_name, 0.0, source="legacy", legacy_bin=os.path.abspath(bin_path))
    pair["legacy_offset"] = float(offset)
    if progress:
        progress("building the merged array")
    return build_pair(conf, pair, fit=fit)


# ---- offset estimation --------------------------------------------------------

def estimate_offset(acc, fit, center=0.0, search=300.0):
    """Offset (s) that best lines up accelerometer activity (per-second spread of
    |acc|) with the .fit speed, searched in center +- search in 1 s steps, then
    refined to 0.1 s. Samples are binned once at 0.1 s; each candidate offset
    only slides 1 s windows over cumulative sums.
    Returns (offset, correlation) or (None, 0) if there is too little overlap."""
    ok = ~np.isnan(fit["speed"])
    ft, fs = fit["t"][ok], fit["speed"][ok]
    grid = np.arange(np.floor(ft[0]), np.ceil(ft[-1]) + 1)      # 1 s bins on the .fit clock
    speed = np.interp(grid, ft, fs)
    t0 = acc["utc"] - fit["start"]
    mag = np.sqrt((acc["acc"] ** 2).sum(axis=1))
    base = int(np.floor(t0.min() * 10)) - 1                     # 0.1 s bins of the unshifted data
    f = np.floor(t0 * 10).astype(np.int64) - base
    nb = int(f.max()) + 2
    cum = [np.r_[0, np.cumsum(np.bincount(f, w, minlength=nb))] for w in (None, mag, mag * mag)]

    def score(m):          # m = offset in tenths of a second
        # 1 s bin j (grid value g) holds samples with t0 in [g - L, g + 1 - L)
        s = (grid * 10).astype(np.int64) - m - base
        e = s + 10
        valid = (s >= 0) & (e <= nb)
        s, e = s[valid], e[valid]
        n, s1, s2 = (c[e] - c[s] for c in cum)
        good = n >= 5
        if good.sum() < 60:
            return -2.0
        act = np.sqrt(np.maximum(s2[good] / n[good] - (s1[good] / n[good]) ** 2, 0))
        sp = speed[valid][good]
        if act.std() == 0 or sp.std() == 0:
            return -2.0
        return float(np.corrcoef(act, sp)[0, 1])

    c10 = int(round(center * 10))
    coarse = [(score(m), m) for m in range(c10 - int(search) * 10, c10 + int(search) * 10 + 1, 10)]
    best = max(coarse)
    if best[0] <= -2:
        return None, 0.0
    fine = max((score(m), m) for m in range(best[1] - 9, best[1] + 10))
    return round(fine[1] / 10, 1), fine[0]


# ---- command line ---------------------------------------------------------------

def describe_pair(p):
    """One line: name, fit, offset, intervals and class counts, samples."""
    cls = " ".join("%s:%d" % (c, len(p.get(c, []))) for c in CLASSES if p.get(c))
    return "%-28s %-24s offset %+8.1f s%s  %d intervals  %s  %d samples" % (
        p["name"], p["fit"], p["offset"], "" if p.get("offset_confirmed") else "?",
        len(p.get("intervals", [])), cls or "-", p.get("samples", 0))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf", help="prec.conf to use")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("update")
    for cmd in ("cook", "build"):              # build: the old name
        b = sub.add_parser(cmd)
        b.add_argument("name", nargs="?")
        b.add_argument("--all", action="store_true", help="every pair")
    d = sub.add_parser("delete")
    d.add_argument("name")
    o = sub.add_parser("offset")
    o.add_argument("name")
    lg = sub.add_parser("legacy")
    lg.add_argument("bin")
    lg.add_argument("fit")
    lg.add_argument("--offset", type=float, default=0.0)
    sub.add_parser("write-config")
    a = ap.parse_args()
    if a.cmd == "write-config":
        print("wrote", write_default_conf(a.conf))
        return
    conf = load_conf(a.conf)
    if a.cmd == "list":
        for p in list_pairs(conf):
            print(describe_pair(p))
    elif a.cmd == "update":
        r = update(conf, progress=print)
        print("created %s; no .fit overlap: %s; too short: %d" % (r["created"] or "-", r["unmatched"] or "-",
                                                                  len(r["skipped"])))
    elif a.cmd in ("cook", "build"):
        if not a.all and not a.name:
            ap.error("give a pair NAME or --all")
        for p in list_pairs(conf) if a.all else [load_pair(conf, a.name)]:
            p = build_pair(conf, p)
            print(describe_pair(p), " ".join("%s %d" % kv for kv in p.get("class_arrays", {}).items()))
    elif a.cmd == "delete":
        delete_pair(conf, a.name)
    elif a.cmd == "offset":
        p = load_pair(conf, a.name)
        off, c = estimate_offset(load_acc(conf, p), load_fit(conf, p), p["offset"], conf["offset"]["auto_range"])
        print("best offset %s s (correlation %.2f), current %.1f s" % (off, c, p["offset"]))
    elif a.cmd == "legacy":
        print(describe_pair(import_legacy(conf, a.bin, a.fit, a.offset, progress=print)))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""prec_train.py - train a speed model on merged PREC data and export it to the Bangle.js 2.

Pairs (see prec_merge.py) are assigned to the roles train, valid, test and
quant. Every interval of every pair is cut into windows of `window` samples
(taken every `stride` samples, a new window every `shift` samples); the label
of a window is the GPS speed (mean over the window by default). The model sees
either the normalised samples (dft = 0) or the first `dft` DFT coefficients of
each axis (magnitudes if dft_abs), normalised with the training mean and
standard deviation.

Results go to the models folder as model_yymmdd_hhmmss.{keras,json,tflite,c,js}:
the .js file is a Bangle.js module with compiled C code (buffer_data, the
features/FFT) and the TensorFlow Lite model, used by the prec app on the watch.

Command line (the TUI's F4 tab does the same):
  prec_train.py show                     training parameters
  prec_train.py set KEY=VALUE ...        change parameters (e.g. roles.train=a,b  epochs=30)
  prec_train.py histogram ROLE           labels per resolution bin (log(n+1))
  prec_train.py archs                    available model architectures
  prec_train.py train                    train, save and export a model
  prec_train.py export NAME              redo the .tflite/.c/.js of a saved model
  prec_train.py models                   list saved models
  prec_train.py feather [NAME|none] [--classifier C] [--run R] [--walk W]   build models into the feather firmware and flash it
  prec_train.py predict NAME PAIR        run a model on a pair, compare with the GPS speed
See docs/TRAIN.md.
"""
import argparse, base64, copy, datetime as dt, glob, importlib.util, json, math, os, subprocess, sys, tempfile, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prec_merge as M   # noqa: E402

ROLES = ("train", "valid", "test", "quant")
BUILTIN_ARCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prec_arch")
BUILTIN_CLASS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prec_class")   # classifier architectures
POLAR_RATES = (25, 50, 100, 200)

# TensorFlow Lite ops the Bangle.js 2 (firmware 2v29) runs, measured on the
# watch: anything else (SHAPE, ADD, MEAN, EXPAND_DIMS, TANH, LOGISTIC,
# LEAKY_RELU...) fails with "Didn't find op for builtin opcode".
BANGLE_OPS = ["FULLY_CONNECTED", "CONV_2D", "DEPTHWISE_CONV_2D", "MAX_POOL_2D", "AVERAGE_POOL_2D",
              "RESHAPE", "SOFTMAX", "QUANTIZE", "DEQUANTIZE"]

DEFAULT_TRAIN = {
    "roles": {r: [] for r in ROLES},          # pair names per role
    "class_arch": "dense3",                   # classifier architecture (prec_class/ or ~/prec/class/)
    "window": 256,                            # samples per window
    "shift": 1,                               # samples between window starts
    "stride": 1,                              # take every stride-th sample inside a window
    "batch": {"train": 32, "valid": 2048, "test": 2048, "quant": 2048},
    "min_speed": 0.0,                         # m/s, training windows outside are dropped
    "max_speed": 7.0,
    "resolution": 0.1,                        # m/s, bin width for strategy and histogram
    "strategy": "weight",                     # weight | pick
    "pick_per_bin": 0,                        # pick: windows per bin (0 = as many as the smallest bin)
    "target": "mean",                         # label: mean | last | center speed of the window
    "dft": 0,                                 # 0 = raw samples; n = first n DFT coefficients (overridden by the arch)
    "dft_abs": True,                          # magnitudes of the DFT coefficients (overridden by the arch)
    "window_fn": "hann",                      # window applied before the DFT (scipy.signal.get_window name)
    "arch": "flat3",                          # model architecture (built-in or ~/prec/arch/<name>.py)
    "epochs": 20,
    "learning_rate": 0.001,
    "loss": "mse",                            # mse | mae | huber
    "lr_patience": 2,                         # epochs without improvement before the learning rate drops 5x
    "early_stop": 6,                          # epochs without improvement before stopping (0 = never)
    "norm_samples": 50000,                    # training windows used to estimate mean and std
    "quantize": False,                        # int8 model, calibrated with the quant role
    "quant_samples": 500,                     # windows used for calibration
    "rate": 200,                              # Hz of the recordings (Polar setting)
    "range": 8,                               # G, Polar ACC range requested by the watch
    "arena": 0,                               # TensorFlow arena bytes on the watch (0 = estimate)
}


# ---- parameters ---------------------------------------------------------------

def train_path(conf):
    return conf["paths"].get("train") or os.path.expanduser("~/prec/train.json")


def load_train(conf):
    """Training parameters: train.json merged over DEFAULT_TRAIN."""
    t = copy.deepcopy(DEFAULT_TRAIN)
    p = os.path.expanduser(train_path(conf))
    if os.path.exists(p):
        with open(p) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(t.get(k), dict):
                t[k].update(v)
            else:
                t[k] = v
    return t


def save_train(conf, t):
    p = os.path.expanduser(train_path(conf))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as f:
        json.dump(t, f, indent=2)
    os.replace(p + ".tmp", p)


def set_param(t, key, value):
    """Set t[key] from text: "epochs=30", "roles.train=a,b", "batch.valid=1024",
    "dft_abs=false". Types follow the defaults. Returns the new value."""
    parts = key.split(".")
    ref, d = DEFAULT_TRAIN, t
    for p in parts[:-1]:
        ref, d = ref[p], d.setdefault(p, {})
    last = parts[-1]
    proto = ref.get(last) if isinstance(ref, dict) else None
    if parts[0] == "roles":
        v = [s for s in (x.strip() for x in value.split(",")) if s]
    elif isinstance(proto, bool):
        v = value.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(proto, int):
        v = int(value)
    elif isinstance(proto, float):
        v = float(value)
    elif proto is None and last not in DEFAULT_TRAIN:
        raise KeyError(key)
    else:
        v = value.strip()
    d[last] = v
    return v


# ---- architectures ----------------------------------------------------------------

def list_archs(conf, kind="speed"):
    """{name: path} of built-in architectures and the user's in the arch folder
    (which win). kind "class": the classifier architectures (class folder)."""
    out = {}
    dirs = (BUILTIN_CLASS, os.path.expanduser(conf["paths"].get("class", "~/prec/class"))) if kind == "class" else \
        (BUILTIN_ARCH, os.path.expanduser(conf["paths"].get("arch", "~/prec/arch")))
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, "*.py"))):
            name = os.path.splitext(os.path.basename(f))[0]
            if not name.startswith("_"):
                out[name] = f
    return out


def load_arch(conf, name, kind="speed"):
    """Import an architecture module (must define create_model(input_shape), for
    kind "class" create_model(input_shape, n_classes))."""
    archs = list_archs(conf, kind)
    if name not in archs:
        raise ValueError("unknown architecture %r (have: %s)" % (name, ", ".join(archs)))
    spec = importlib.util.spec_from_file_location("prec_%s_%s" % (kind, name), archs[name])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "create_model"):
        raise ValueError("%s has no create_model(input_shape)" % archs[name])
    return mod


def arch_info(conf, name, kind="speed"):
    """{"dft", "dft_abs", "doc", "path"} of an architecture, read from its source
    without importing it (no TensorFlow needed)."""
    import ast
    archs = list_archs(conf, kind)
    if name not in archs:
        raise ValueError("unknown architecture %r (have: %s)" % (name, ", ".join(archs)))
    tree = ast.parse(open(archs[name]).read())
    info = {"dft": None, "dft_abs": None, "path": archs[name],
            "doc": (ast.get_docstring(tree) or "").strip().splitlines()[0] if ast.get_docstring(tree) else ""}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            key = {"DFT": "dft", "DFT_ABS": "dft_abs"}.get(node.targets[0].id)
            if key:
                try:
                    info[key] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    return info


def effective(conf, t):
    """Parameters actually used: the arch's DFT / DFT_ABS override the
    configured ones; window coefficients; input shape. Raises ValueError for
    combinations the watch cannot run."""
    e = dict(t)
    if t.get("task") == "classifier":           # the classifier architecture, from the class folder
        e["arch"] = t["class_arch"]
    arch = arch_info(conf, e["arch"], "class" if t.get("task") == "classifier" else "speed")
    if arch["dft"] is not None:
        e["dft"] = int(arch["dft"])
    if arch["dft_abs"] is not None:
        e["dft_abs"] = bool(arch["dft_abs"])
    w, dft = int(e["window"]), int(e["dft"])
    if not 0 <= dft <= w // 2 + 1:
        raise ValueError("dft must be between 0 and window/2+1 = %d" % (w // 2 + 1))
    if dft and w & (w - 1):
        raise ValueError("with dft > 0 the window length must be a power of two (FFT on the watch)")
    if e["rate"] % e["stride"] or e["rate"] // e["stride"] not in POLAR_RATES:
        raise ValueError("rate/stride = %g Hz is not a Polar ACC rate %s" % (e["rate"] / e["stride"], POLAR_RATES))
    e["frequency"] = e["rate"] // e["stride"]
    e["window_coeffs"] = window_coeffs(e["window_fn"] if dft else "boxcar", w)
    e["channels"] = 3 if (dft == 0 or e["dft_abs"]) else 6
    e["input_shape"] = (w, 3) if dft == 0 else (dft, e["channels"])
    e["arch_doc"] = arch["doc"]
    e["arch_sets"] = [k for k in ("dft", "dft_abs") if arch[k] is not None]
    return e


def window_coeffs(name, n):
    """Window function values (float32): any scipy.signal.get_window name; "boxcar"/"none" = ones."""
    if name in ("boxcar", "none", "rect", "ones"):
        return np.ones(n, np.float32)
    import scipy.signal
    return scipy.signal.get_window(name, n, fftbins=True).astype(np.float32)


# ---- data ---------------------------------------------------------------------------

class RoleData:
    """All windows of one role: X (N, 3) float32 samples of all its pairs one
    after the other, starts (k,) index of each window's first sample in X,
    labels (k,) speed, weights (k,), pairs [(name, windows)]."""

    def __init__(self, X, starts, labels, weights, pairs):
        self.X, self.starts, self.labels, self.weights, self.pairs = X, starts, labels, weights, pairs


def window_starts(t, intervals, e):
    """Start indices of the windows that fit inside the intervals (t sorted)."""
    w, st, sh = int(e["window"]), int(e["stride"]), int(e["shift"])
    span = (w - 1) * st + 1
    starts = []
    for a, b in intervals:
        i0, i1 = np.searchsorted(t, a), np.searchsorted(t, b, side="right")
        if i1 - i0 >= span:
            starts.append(np.arange(i0, i1 - span + 1, sh, dtype=np.int64))
    return np.concatenate(starts) if starts else np.zeros(0, np.int64)


def windows_of(arr, intervals, e):
    """Window starts and labels for one cooked array (t, ax, ay, az, speed) inside the intervals."""
    w, st, sh = int(e["window"]), int(e["stride"]), int(e["shift"])
    span = (w - 1) * st + 1
    t, speed = arr[:, 0], arr[:, 4]
    cs = np.r_[0, np.cumsum(speed)]
    starts = []
    for a, b in intervals:
        i0, i1 = np.searchsorted(t, a), np.searchsorted(t, b, side="right")
        if i1 - i0 >= span:
            starts.append(np.arange(i0, i1 - span + 1, sh, dtype=np.int64))
    if not starts:
        return np.zeros(0, np.int64), np.zeros(0, np.float32)
    s = np.concatenate(starts)
    if e["target"] == "last":
        lab = speed[s + span - 1]
    elif e["target"] == "center":
        lab = speed[s + span // 2]
    else:
        lab = (cs[s + span] - cs[s]) / span
    return s, lab.astype(np.float32)


def class_role_data(conf, e, role, log=None):
    """RoleData of a role for the classifier: windows inside each class's
    intervals, from the pairs' class arrays, labelled with the class index in
    M.CLASSES. For the train role every class present gets the same total
    weight (mean weight 1)."""
    Xs, S, Lb, info, off = [], [], [], [], 0
    for name in e["roles"].get(role, []):
        pair = M.load_pair(conf, name)
        if not any(pair.get(c) for c in M.CLASSES):
            continue
        if any(pair.get(c) and c not in pair.get("class_arrays", {}) for c in M.CLASSES):
            pair = M.build_pair(conf, pair)      # not cooked since the class intervals were drawn
        n = 0
        for k, c in enumerate(M.CLASSES):
            arr = M.load_class_array(conf, pair, c) if pair.get(c) else None
            if arr is None or not len(arr):
                continue
            s = window_starts(arr[:, 0], pair[c], e)
            Xs.append(arr[:, 1:4].astype(np.float32))
            S.append(s + off)
            Lb.append(np.full(len(s), k, np.int32))
            off += len(arr)
            n += len(s)
        info.append((name, n))
    if not Xs:
        return RoleData(np.zeros((0, 3), np.float32), np.zeros(0, np.int64), np.zeros(0, np.int32),
                        np.zeros(0, np.float32), [])
    X, starts, labels = np.concatenate(Xs), np.concatenate(S), np.concatenate(Lb)
    weights = np.ones(len(starts), np.float32)
    counts = np.bincount(labels, minlength=len(M.CLASSES))
    if role == "train":
        present = counts > 0
        weights = (len(labels) / (present.sum() * counts[labels])).astype(np.float32)
    if log:
        log("%s: %d windows (%s) from %s" % (role, len(starts), ", ".join(
            "%s %d" % (c, n) for c, n in zip(M.CLASSES, counts)), ", ".join("%s (%d)" % i for i in info) or "no pairs"))
    return RoleData(X, starts, labels, weights, info)


def confusion(labels, pred, k):
    """k x k matrix: rows the true class, columns the predicted one."""
    m = np.zeros((k, k), np.int64)
    np.add.at(m, (labels, pred), 1)
    return m


def intersect(a, b):
    """The parts common to two lists of intervals."""
    return [[max(x0, y0), min(x1, y1)] for x0, x1 in a for y0, y1 in b if min(x1, y1) > max(x0, y0)]


def role_data(conf, e, role, log=None):
    """RoleData of a role. For the train role, windows with labels outside
    [min_speed, max_speed] are dropped and the strategy is applied. With
    e["cls"] (run or walk) only the pair's class array is used: windows lie
    inside both its class intervals and its sub-intervals; pairs without
    that class are skipped."""
    Xs, S, Lb, info, off = [], [], [], [], 0
    cls = e.get("cls")
    for name in e["roles"].get(role, []):
        pair = M.load_pair(conf, name)
        if cls:
            if not pair.get(cls):
                continue
            arr = M.load_class_array(conf, pair, cls)
            if arr is None:                      # not cooked since the class intervals were drawn
                arr = M.load_class_array(conf, M.build_pair(conf, pair), cls)
            if arr is None or not len(arr):
                continue
            iv = intersect(pair[cls], pair.get("intervals") or [[arr[0, 0], arr[-1, 0]]])
        else:
            arr = M.load_cooked(conf, pair)
            if arr is None:
                arr = M.load_cooked(conf, M.build_pair(conf, pair))
            iv = pair.get("intervals") or [[arr[0, 0], arr[-1, 0]]]
        s, lab = windows_of(arr, iv, e)
        Xs.append(arr[:, 1:4].astype(np.float32))
        S.append(s + off)
        Lb.append(lab)
        info.append((name, len(s)))
        off += len(arr)
    if not Xs:
        return RoleData(np.zeros((0, 3), np.float32), np.zeros(0, np.int64), np.zeros(0, np.float32),
                        np.zeros(0, np.float32), [])
    X, starts, labels = np.concatenate(Xs), np.concatenate(S), np.concatenate(Lb)
    weights = np.ones(len(starts), np.float32)
    if role == "train":
        keep = (labels >= e["min_speed"]) & (labels <= e["max_speed"])
        starts, labels = starts[keep], labels[keep]
        weights = np.ones(len(starts), np.float32)
        starts, labels, weights = apply_strategy(starts, labels, e)
    if log:
        log("%s%s: %d windows from %s" % (role, " (%s only)" % cls if cls else "", len(starts),
                                         ", ".join("%s (%d)" % i for i in info) or "no pairs"))
    return RoleData(X, starts, labels, weights, info)


def bins_of(labels, e):
    return np.floor((labels - e["min_speed"]) / e["resolution"] + 1e-9).astype(np.int64)


def apply_strategy(starts, labels, e, seed=1):
    """weight: every resolution bin gets the same total weight (mean weight 1).
    pick: the same number of windows from every bin, all weight 1."""
    if not len(starts):
        return starts, labels, np.zeros(0, np.float32)
    b = bins_of(labels, e)
    uniq, inv, counts = np.unique(b, return_inverse=True, return_counts=True)
    if e["strategy"] == "pick":
        rng = np.random.default_rng(seed)
        n = int(e["pick_per_bin"]) or int(counts.min())
        idx = []
        for k in range(len(uniq)):
            members = np.flatnonzero(inv == k)
            idx.append(rng.choice(members, n, replace=len(members) < n))
        idx = np.sort(np.concatenate(idx))
        return starts[idx], labels[idx], np.ones(len(idx), np.float32)
    w = (len(labels) / (len(uniq) * counts[inv])).astype(np.float32)
    return starts, labels, w


def histogram(conf, e, role):
    """(bin lower edges, counts) of the number of windows per resolution step of
    the label (train: after the speed limits and, for pick, the picking)."""
    rd = role_data(conf, e, role)
    if not len(rd.labels):
        return np.zeros(0), np.zeros(0)
    lo = math.floor(min(rd.labels.min(), e["min_speed"]) / e["resolution"]) * e["resolution"]
    k = np.floor((rd.labels - lo) / e["resolution"] + 1e-9).astype(np.int64)
    counts = np.bincount(k)
    return lo + np.arange(len(counts)) * e["resolution"], counts


# ---- features ---------------------------------------------------------------------------

def gather_np(rd, idx, e):
    """Windows (len(idx), window, 3) of RoleData rd for the window numbers idx."""
    offs = np.arange(e["window"]) * e["stride"]
    return rd.X[rd.starts[idx][:, None] + offs[None, :]]


def features_np(w, e):
    """Model input before normalisation, numpy version: (B, window, 3) ->
    (B, window, 3) or (B, dft, 3|6). Same arithmetic as features_tf and the C code."""
    dft = e["dft"]
    if not dft:
        return w.astype(np.float32)
    x = (w * e["window_coeffs"][None, :, None]).transpose(0, 2, 1)
    X = np.fft.rfft(x, axis=-1)[..., :dft]
    f = np.abs(X) if e["dft_abs"] else np.concatenate([X.real, X.imag], axis=1)
    return f.transpose(0, 2, 1).astype(np.float32)


def features_tf(w, e):
    """TensorFlow version of features_np, for tf.data."""
    import tensorflow as tf
    dft = e["dft"]
    if not dft:
        return w
    x = tf.transpose(w * e["window_coeffs"][None, :, None], [0, 2, 1])
    X = tf.signal.rfft(x)[..., :dft]
    f = tf.abs(X) if e["dft_abs"] else tf.concat([tf.math.real(X), tf.math.imag(X)], axis=1)
    return tf.transpose(f, [0, 2, 1])


def norm_stats(rd, e, seed=2):
    """Mean and standard deviation of the training features, from up to
    norm_samples random windows: per axis (dft = 0, shape (3,)) or per
    coefficient and axis (shape (dft, 3|6))."""
    n = min(len(rd.starts), int(e["norm_samples"]))
    idx = np.random.default_rng(seed).choice(len(rd.starts), n, replace=False)
    parts = [features_np(gather_np(rd, idx[i:i + 2048], e), e) for i in range(0, n, 2048)]
    f = np.concatenate(parts).astype(np.float64)
    if not e["dft"]:
        f = f.reshape(-1, 3)
    mean, std = f.mean(axis=0), f.std(axis=0)
    std[std < 1e-6] = 1.0
    return mean.astype(np.float32), std.astype(np.float32)


def make_dataset(rd, e, mean, std, batch, shuffle=False, weighted=False):
    """tf.data.Dataset of (features, speed[, weight]) batches; windows are
    gathered from rd.X on the fly."""
    import tensorflow as tf
    X = tf.constant(rd.X)
    offs = tf.constant(np.arange(e["window"]) * e["stride"], tf.int64)
    mean_t, std_t = tf.constant(mean), tf.constant(std)
    cols = (rd.starts, rd.labels) + ((rd.weights,) if weighted else ())
    ds = tf.data.Dataset.from_tensor_slices(cols)
    if shuffle:
        ds = ds.shuffle(min(len(rd.starts), 2_000_000), reshuffle_each_iteration=True)
    ds = ds.batch(int(batch))

    def prep(s, y, *w):
        win = tf.gather(X, s[:, None] + offs[None, :])
        x = (features_tf(win, e) - mean_t) / std_t
        return (x, y, w[0]) if w else (x, y)
    return ds.map(prep, num_parallel_calls=tf.data.AUTOTUNE).prefetch(tf.data.AUTOTUNE)


# ---- training ---------------------------------------------------------------------------

def model_name(cls=None):
    return "model_" + time.strftime("%y%m%d_%H%M%S") + ("_" + cls if cls else "")


def load_model_meta(conf, name):
    with open(os.path.join(os.path.expanduser(conf["paths"]["models"]), name + ".json")) as f:
        return json.load(f)


def retrain_params(conf, base):
    """The parameters and roles saved model `base` was trained with (from its
    .json); keys it lacks (older models) come from the current parameters."""
    meta = load_model_meta(conf, base)
    t = load_train(conf)
    missing = [k for k in t if k != "roles" and k not in meta.get("params", {})]
    t.update(copy.deepcopy(meta.get("params", {})))
    t["roles"] = copy.deepcopy(meta.get("roles", {}))
    return t, meta, missing


def train(conf, t=None, log=print, base=None, epochs=None, lr=None, cls=None, classifier=False):
    """Train a model with the current parameters, save .keras/.json and export.
    With base, continue training saved model `base` instead: its parameters,
    pairs and normalisation from its .json, its weights and optimizer state
    from its .keras, for `epochs` more epochs (default: its epochs parameter),
    at learning rate lr (default: where it stopped). The result is saved as a
    new model either way. cls (run or walk): train only on the pairs' class
    arrays of that class; the name ends in _run or _walk (a retrain keeps
    the class of its base). classifier: train a classifier of the windows
    into run, walk, still and stretch instead (architecture class_arch, from
    the pairs' class arrays; name ends in _classifier, no watch export).
    Returns the model name."""
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    import tensorflow as tf, keras
    if base:
        t, bmeta, missing = retrain_params(conf, base)
        log("retraining %s: parameters and pairs from its .json" % base)
        if missing:
            log("not in its .json, taken from the current parameters: " + ", ".join(missing))
    else:
        t = dict(t or load_train(conf))
        if cls:
            t["cls"] = cls
        if classifier:
            t["task"] = "classifier"
    cls = t.get("cls")
    clf = t.get("task") == "classifier"
    if clf:
        cls = None
        log("classifier: %s, from the pairs' class arrays" % ", ".join(M.CLASSES))
    if cls:
        log("%s model: trained on the %s intervals only (the pairs' _%s arrays)" % (cls, cls, cls))
    e = effective(conf, t)
    log("architecture %s: %s" % (e["arch"], e["arch_doc"]))
    log("input %s  (window %d, stride %d -> %d Hz, dft %d%s, window function %s)" % (
        e["input_shape"], e["window"], e["stride"], e["frequency"], e["dft"],
        " abs" if e["dft"] and e["dft_abs"] else "", e["window_fn"] if e["dft"] else "none"))
    data = {r: (class_role_data if clf else role_data)(conf, e, r, log) for r in ROLES}
    if clf and not len(data["train"].starts):
        raise ValueError("no training windows: no train pair has class intervals (draw them with F3 > Class intervals)")
    if not len(data["train"].starts):
        raise ValueError("no training windows: " + ("no train pair has %s intervals (draw them with F3 > Class intervals)" % cls
                                                    if cls else "assign pairs to the train role"))
    if base:
        shape = (-1,) if not e["dft"] else (e["dft"], e["channels"])
        mean = np.array(bmeta["mean"], np.float32).reshape(shape)
        std = np.array(bmeta["std"], np.float32).reshape(shape)
        log("normalisation from %s" % base)
    else:
        mean, std = norm_stats(data["train"], e)
        log("normalisation from %d training windows" % min(len(data["train"].starts), e["norm_samples"]))
    train_ds = make_dataset(data["train"], e, mean, std, e["batch"]["train"], shuffle=True, weighted=True)
    valid_ds = make_dataset(data["valid"], e, mean, std, e["batch"]["valid"]) if len(data["valid"].starts) else None
    keras.utils.set_random_seed(3)
    before = {}
    if base:
        model = keras.models.load_model(os.path.join(os.path.expanduser(conf["paths"]["models"]), base + ".keras"))
        if lr:
            model.optimizer.learning_rate.assign(float(lr))
        log("learning rate %.3g%s" % (float(model.optimizer.learning_rate.numpy()), "" if lr else " (where it stopped)"))
        before = bmeta.get("history", {})
    elif clf:
        model = load_arch(conf, e["arch"], "class").create_model(e["input_shape"], len(M.CLASSES))
        model.compile(optimizer=keras.optimizers.Adam(e["learning_rate"]), loss="sparse_categorical_crossentropy",
                      metrics=["accuracy"], weighted_metrics=["accuracy"])
    else:
        model = load_arch(conf, e["arch"]).create_model(e["input_shape"])
        loss = {"huber": keras.losses.Huber()}.get(e["loss"], e["loss"])
        model.compile(optimizer=keras.optimizers.Adam(e["learning_rate"]), loss=loss, metrics=["mae"],
                      weighted_metrics=["mae"])
    first = len(before.get("loss", []))              # epochs already done
    epochs = int(epochs or e["epochs"])
    mon = "val_loss" if valid_ds is not None else "loss"
    cbs = [keras.callbacks.ReduceLROnPlateau(monitor=mon, factor=0.2, patience=int(e["lr_patience"]), verbose=1)]
    stop = None
    if int(e["early_stop"]):
        stop = keras.callbacks.EarlyStopping(monitor=mon, patience=int(e["early_stop"]), restore_best_weights=True,
                                             verbose=1)
        cbs.append(stop)

    log("training %d epoch%s%s, %d steps each" % (epochs, "s" if epochs > 1 else "", " (%d to %d)" % (first + 1, first + epochs) if first else "",
                                                 math.ceil(len(data["train"].starts) / e["batch"]["train"])))
    # Keras draws its live per-epoch progress bar only on a terminal; claim to be
    # one so the bar also reaches the TUI's training log (redrawn with \r and \b).
    # Its final line of each epoch carries all metrics, validation included.
    class Terminal:
        def __init__(self, f):
            self.f = f

        def isatty(self):
            return True

        def __getattr__(self, k):
            return getattr(self.f, k)
    out = sys.stdout
    sys.stdout = Terminal(out)
    try:
        hist = model.fit(train_ds, validation_data=valid_ds, initial_epoch=first, epochs=first + epochs,
                         callbacks=cbs, verbose=1)
    finally:
        sys.stdout = out
    history = {k: [float(x) for x in before.get(k, [])] + [float(x) for x in v] for k, v in hist.history.items()}
    # the epoch whose weights are saved (EarlyStopping restores its best one, of this run)
    kept = stop.best_epoch + 1 if stop is not None and stop.best_weights is not None else first + len(hist.history["loss"])
    result = {}
    if len(data["test"].starts):
        r = model.evaluate(make_dataset(data["test"], e, mean, std, e["batch"]["test"]), verbose=0, return_dict=True)
        result = {"test_" + k: float(v) for k, v in r.items()}
        log("test: " + "  ".join("%s %.4g" % kv for kv in result.items()))
        if clf:
            p = model.predict(make_dataset(data["test"], e, mean, std, e["batch"]["test"]), verbose=0)
            cm = confusion(data["test"].labels, p.argmax(axis=1), len(M.CLASSES))
            result["confusion"] = cm.tolist()
            log("test confusion (rows: true class, columns: predicted)")
            log("%-9s" % "true\\pred" + "".join("%9s" % c for c in M.CLASSES))
            for c, row in zip(M.CLASSES, cm):
                log("%-9s" % c + "".join("%9d" % n for n in row))
    name = model_name("classifier" if clf else cls)
    mdir = os.path.expanduser(conf["paths"]["models"])
    os.makedirs(mdir, exist_ok=True)
    model.save(os.path.join(mdir, name + ".keras"))
    meta = {
        "name": name, "created": M.iso(time.time()), "arch": e["arch"],
        "window": e["window"], "shift": e["shift"], "stride": e["stride"], "rate": e["rate"],
        "frequency": e["frequency"], "range": e["range"], "dft": e["dft"], "dft_abs": e["dft_abs"],
        "window_fn": e["window_fn"] if e["dft"] else "boxcar", "window_coeffs": e["window_coeffs"].tolist(),
        "input_shape": list(e["input_shape"]), "mean": mean.tolist(), "std": std.tolist(),
        "kind": "classifier" if clf else "speed", **({"classes": list(M.CLASSES)} if clf else {}),
        "class": cls, "target": e["target"], "min_speed": e["min_speed"], "max_speed": e["max_speed"],
        "resolution": e["resolution"], "strategy": e["strategy"], "loss": "crossentropy" if clf else e["loss"],
        "roles": e["roles"], "windows": {r: len(data[r].starts) for r in ROLES},
        "params": {k: v for k, v in t.items() if k != "roles"},
        "history": history, "kept_epoch": kept,
        **({"parent": base, "parent_epochs": first} if base else {}),
        **result,
    }
    with open(os.path.join(mdir, name + ".json"), "w") as f:
        json.dump(meta, f, indent=2)
    log("saved %s.keras and %s.json" % (name, name))
    export(conf, name, model=model, data=data, e=e, log=log)
    return name


# ---- export ---------------------------------------------------------------------------------

def tflite_ops(blob):
    """Sorted op names used by a TFLite model."""
    import tensorflow as tf
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        it = tf.lite.Interpreter(model_content=blob)
    return sorted({o["op_name"] for o in it._get_ops_details()})


def to_tflite(model, meta, rep=None):
    """Convert with batch size 1 (static shapes: no SHAPE/PACK ops); int8 with
    float input and output if rep (a representative dataset generator) is given."""
    import tensorflow as tf, keras
    inp = keras.Input(tuple(meta["input_shape"]), batch_size=1)
    fixed = keras.Model(inp, model(inp))          # same weights, batch size 1: static shapes only
    conv = tf.lite.TFLiteConverter.from_keras_model(fixed)
    if rep is not None:
        conv.optimizations = [tf.lite.Optimize.DEFAULT]
        conv.representative_dataset = rep
        conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    return conv.convert()


def estimate_arena(blob):
    """TensorFlow Lite Micro arena estimate: the activations (model input and
    every op output; weights stay in the model) plus 6 kB of bookkeeping, in
    kB steps. The module retries with 2x and 4x if the watch needs more."""
    import tensorflow as tf
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        it = tf.lite.Interpreter(model_content=blob)
    sizes = {d["index"]: int(np.prod(d["shape"])) * np.dtype(d["dtype"]).itemsize for d in it.get_tensor_details()}
    act = {d["index"] for d in it.get_input_details()}
    for op in it._get_ops_details():
        act.update(int(i) for i in op["outputs"])
    return int(math.ceil((sum(sizes[i] for i in act) + 6144) / 1024) * 1024)


def export(conf, name, model=None, data=None, e=None, log=print):
    """Write <name>.tflite, <name>.c and <name>.js for a saved model; checks
    that the TFLite model only uses ops the Bangle runs. Returns the .js path."""
    import keras
    mdir = os.path.expanduser(conf["paths"]["models"])
    meta = json.load(open(os.path.join(mdir, name + ".json")))
    model = model or keras.models.load_model(os.path.join(mdir, name + ".keras"))
    t = load_train(conf)
    em = dict(e or {})
    em.update({k: meta[k] for k in ("window", "stride", "dft", "dft_abs")})
    em["cls"] = meta.get("class")
    em["window_coeffs"] = np.array(meta["window_coeffs"], np.float32)
    mean, std = np.array(meta["mean"], np.float32), np.array(meta["std"], np.float32)
    rep = None
    quantize = meta["params"].get("quantize", t["quantize"])
    if quantize:
        ed = dict(t)
        ed.update(em)
        rd = (data or {}).get("quant")
        if rd is None or not len(rd.starts):
            rd = role_data(conf, ed, "quant")
        if not len(rd.starts):
            rd = (data or {}).get("train") or role_data(conf, ed, "train")
            log("quantization: no quant pairs, calibrating with training windows")
        n = min(len(rd.starts), int(meta["params"].get("quant_samples", 500)))
        idx = np.random.default_rng(4).choice(len(rd.starts), n, replace=False)
        feats = (features_np(gather_np(rd, idx, ed), ed) - mean) / std

        def rep():
            for f in feats:
                yield [f[None].astype(np.float32)]
    blob = to_tflite(model, meta, rep)
    ops = tflite_ops(blob)
    bad = [o for o in ops if o not in t.get("allowed_ops", BANGLE_OPS)]
    open(os.path.join(mdir, name + ".tflite"), "wb").write(blob)
    log("%s.tflite: %d bytes, %s, ops %s" % (name, len(blob), "int8" if quantize else "float32", ", ".join(ops)))
    if bad:
        raise ValueError("the model uses ops the Bangle.js 2 does not have: %s (no .js written)" % ", ".join(bad))
    arena = int(meta["params"].get("arena") or 0) or estimate_arena(blob)
    src = c_source(meta, mean, std)
    open(os.path.join(mdir, name + ".c"), "w").write(src)
    code, funcs = compile_c(src)
    check = check_c_host(src, meta, mean, std)
    if check is not None:
        log("C features match numpy (max difference %.2g)" % check)
    js = module_js(meta, code, funcs, blob, arena, quantize)
    path = os.path.join(mdir, name + ".js")
    open(path, "w").write(js)
    meta.update({"tflite_bytes": len(blob), "quantized": bool(quantize), "ops": ops, "arena": arena,
                 "js_bytes": len(js)})
    with open(os.path.join(mdir, name + ".json"), "w") as f:
        json.dump(meta, f, indent=2)
    log("%s.js: Bangle module, %d bytes (arena %d bytes)" % (name, len(js), arena))
    return path


# ---- code for the watch -------------------------------------------------------------------

def _cfloat(x):
    s = "%.9g" % float(x)
    return (s if ("." in s or "e" in s) else s + ".0") + "f"


def _floats(a):
    return ", ".join(_cfloat(x) for x in np.asarray(a).ravel())


def c_source(meta, mean, std):
    """C source of buffer_data() and features() for E.nativeCall, with the
    window, normalisation and (if dft > 0) FFT tables built in."""
    w, dft, ab = int(meta["window"]), int(meta["dft"]), bool(meta["dft_abs"])
    nout = w * 3 if not dft else dft * (3 if ab else 6)
    lines = ["/* %s: input features for the Bangle.js 2, generated by prec_train.py." % meta["name"],
             " * Compiled with arm-none-eabi-gcc and called with E.nativeCall (see the .js module). */",
             "#ifndef PTR", "#define PTR int    /* addresses are 32 bit on the watch and the feather */", "#endif",
             "#define WIN %d" % w, "#define DFT %d" % dft, "#define DFT_ABS %d" % int(ab), "#define NOUT %d" % nout, "",
             "static const float WINDOW[WIN] = {%s};" % _floats(meta["window_coeffs"]),
             "static const float MEAN[%d] = {%s};" % (np.size(mean), _floats(mean)),
             "static const float ISTD[%d] = {%s};" % (np.size(std), _floats(1.0 / np.asarray(std)))]
    if dft:
        k = np.arange(w // 2)
        bits = int(math.log2(w))
        rev = [int(format(i, "0%db" % bits)[::-1], 2) for i in range(w)]
        lines += ["static const float COS[WIN / 2] = {%s};" % _floats(np.cos(2 * np.pi * k / w)),
                  "static const float SIN[WIN / 2] = {%s};" % _floats(np.sin(2 * np.pi * k / w)),
                  "static const unsigned short REV[WIN] = {%s};" % ", ".join(map(str, rev))]
    lines += [r'''
/* buffer_data(x, buf, a, b): buf[a..b) = (float)x[0..b-a) * WINDOW[i / 3]
   x: int16 x,y,z samples as they come from the Polar, buf: float window buffer
   (WIN x 3, interleaved), a and b: value positions in buf (multiples of 3). */
void buffer_data(PTR x, PTR buf, int a, int b) {
  const short *in = (const short *)x;
  float *out = (float *)buf;
  for (int i = a; i < b; i++) out[i] = (float)in[i - a] * WINDOW[i / 3];
}

/* features(buf, out, work): the normalised model input from a full buffer.
   out: NOUT floats; work: 2 * WIN floats of scratch space (FFT). */
void features(PTR buf, PTR out, PTR work) {
  const float *b = (const float *)buf;
  float *o = (float *)out;
#if DFT == 0
  (void)work;
  for (int i = 0; i < WIN * 3; i++) o[i] = (b[i] - MEAN[i % 3]) * ISTD[i % 3];
#else
  float *re = (float *)work, *im = re + WIN;
  for (int ch = 0; ch < 3; ch++) {
    for (int n = 0; n < WIN; n++) { re[REV[n]] = b[n * 3 + ch]; im[n] = 0.0f; }
    for (int size = 2; size <= WIN; size <<= 1) {          /* radix-2 FFT, forward */
      int half = size >> 1, step = WIN / size;
      for (int i = 0; i < WIN; i += size)
        for (int j = 0; j < half; j++) {
          float wr = COS[j * step], wi = -SIN[j * step];
          int p = i + j, q = p + half;
          float tr = wr * re[q] - wi * im[q], ti = wr * im[q] + wi * re[q];
          re[q] = re[p] - tr; im[q] = im[p] - ti;
          re[p] += tr; im[p] += ti;
        }
    }
    for (int k = 0; k < DFT; k++) {
#if DFT_ABS
      int i = k * 3 + ch;
      o[i] = (__builtin_sqrtf(re[k] * re[k] + im[k] * im[k]) - MEAN[i]) * ISTD[i];
#else
      int i = k * 6 + ch, j = k * 6 + 3 + ch;
      o[i] = (re[k] - MEAN[i]) * ISTD[i];
      o[j] = (im[k] - MEAN[j]) * ISTD[j];
#endif
    }
  }
#endif
}

/* spectrum(feat, out, ch): out[0..DFT) = |X_k| of channel ch, the magnitudes of
   the features from the last features() call with the normalisation undone
   (cadence from the dominant frequency). Does nothing when DFT == 0. */
void spectrum(PTR feat, PTR out, int ch) {
#if DFT == 0
  (void)feat; (void)out; (void)ch;
#else
  const float *f = (const float *)feat;
  float *o = (float *)out;
  for (int k = 0; k < DFT; k++) {
#if DFT_ABS
    int i = k * 3 + ch;
    o[k] = f[i] / ISTD[i] + MEAN[i];
#else
    int i = k * 6 + ch, j = i + 3;
    float re = f[i] / ISTD[i] + MEAN[i], im = f[j] / ISTD[j] + MEAN[j];
    o[k] = __builtin_sqrtf(re * re + im * im);
#endif
  }
#endif
}
''']
    return "\n".join(lines)


ARM_FLAGS = ["-mthumb", "-mcpu=cortex-m4", "-mfloat-abi=softfp", "-mfpu=fpv4-sp-d16", "-O2", "-fpic",
             "-mpic-data-is-text-relative", "-fno-common", "-ffreestanding", "-fno-builtin", "-fno-math-errno",
             "-nostdlib", "-ffunction-sections", "-fdata-sections"]
LINK_SCRIPT = """SECTIONS {
  . = 0;
  .text : { *(.text.buffer_data) *(.text*) *(.rodata*) *(.data*) *(.bss*) *(COMMON) }
  /DISCARD/ : { *(.ARM.exidx*) *(.ARM.attributes) *(.comment) *(.note*) }
}
"""


def compile_c(src):
    """Compile src with arm-none-eabi-gcc into position independent Thumb code
    (no relocations, tables reached PC-relative). Returns (binary, {function: offset})."""
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "f.c"), "w").write(src)
        open(os.path.join(d, "link.ld"), "w").write(LINK_SCRIPT)
        run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True, text=True)
        try:
            run("arm-none-eabi-gcc", *ARM_FLAGS, "-c", "f.c", "-o", "f.o")
            run("arm-none-eabi-gcc", *ARM_FLAGS, "-T", "link.ld", "-Wl,--no-undefined", "-o", "f.elf", "f.o", "-lgcc")
        except FileNotFoundError:
            raise RuntimeError("arm-none-eabi-gcc is needed to build the watch module")
        except subprocess.CalledProcessError as ex:
            raise RuntimeError("compiling the features code failed:\n" + ex.stderr)
        rel = run("arm-none-eabi-readelf", "-r", "f.elf").stdout
        if "R_ARM" in rel or ".got" in run("arm-none-eabi-objdump", "-h", "f.elf").stdout:
            raise RuntimeError("compiled code is not position independent")
        funcs = {}
        for line in run("arm-none-eabi-nm", "f.elf").stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] == "T":
                funcs[parts[2]] = int(parts[0], 16)
        run("arm-none-eabi-objcopy", "-O", "binary", "f.elf", "f.bin")
        return open(os.path.join(d, "f.bin"), "rb").read(), funcs


def check_c_host(src, meta, mean, std, n=4):
    """Compile the C code for this computer and compare features() with
    features_np on random windows. Returns the largest relative difference,
    or None if no host compiler is available. Raises if they disagree."""
    import ctypes
    with tempfile.TemporaryDirectory() as d:
        c = os.path.join(d, "f.c")
        so = os.path.join(d, "f.so")
        open(c, "w").write(src)
        try:
            subprocess.run(["cc", "-O2", "-shared", "-fPIC", "-DPTR=long", "-o", so, c], check=True, capture_output=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None
        lib = ctypes.CDLL(so)
        w = int(meta["window"])
        e = {"dft": int(meta["dft"]), "dft_abs": bool(meta["dft_abs"]),
             "window_coeffs": np.array(meta["window_coeffs"], np.float32)}
        rng = np.random.default_rng(5)
        worst = 0.0
        for _ in range(n):
            raw = rng.normal(0, 800, (w, 3)).round().astype(np.int16)
            buf = np.zeros(w * 3, np.float32)
            lib.buffer_data(ctypes.c_long(raw.ctypes.data), ctypes.c_long(buf.ctypes.data), 0, w * 3)
            nout = w * 3 if not e["dft"] else e["dft"] * (3 if e["dft_abs"] else 6)
            out = np.zeros(nout, np.float32)
            work = np.zeros(2 * w, np.float32)
            lib.features(ctypes.c_long(buf.ctypes.data), ctypes.c_long(out.ctypes.data), ctypes.c_long(work.ctypes.data))
            ref = (features_np(raw[None].astype(np.float32), e)[0] - mean) / std
            diff = np.abs(out - ref.ravel()) / (np.abs(ref.ravel()) + 1.0)
            worst = max(worst, float(diff.max()))
            if e["dft"]:                  # spectrum(): magnitudes of one axis, normalisation undone
                spec = np.zeros(e["dft"], np.float32)
                lib.spectrum(ctypes.c_long(out.ctypes.data), ctypes.c_long(spec.ctypes.data), 0)
                mag = np.abs(np.fft.rfft(raw[:, 0] * e["window_coeffs"]))[:e["dft"]]
                worst = max(worst, float((np.abs(spec - mag) / (mag + 1.0)).max()))
        if worst > 1e-3:
            raise RuntimeError("C features differ from numpy by %.3g" % worst)
        return worst


def module_js(meta, code, funcs, blob, arena, quantized):
    """The Bangle.js module. Upload it as Storage file prm_<id> (model name
    without "model_") with the .tflite file as prm_<id>.tfl (the TUI's
    F3 > Models does both): the model is read from Storage so that only one
    copy of it sits in RAM."""
    w, dft, ab = int(meta["window"]), int(meta["dft"]), bool(meta["dft_abs"])
    nin = w * 3 if not dft else dft * (3 if ab else 6)
    clf = meta.get("kind") == "classifier"
    return '''/* %(name)s: %(what)s for the Bangle.js 2 (generated by prec_train.py).
   arch %(arch)s, window %(w)d samples at %(freq)d Hz, dft %(dft)d%(abs)s, %(q)s.
   Upload as Storage file prm_<id> (the model name without "model_") with
   %(name)s.tflite as prm_<id>.tfl, then pick it in the prec app's Models menu; set
   MODEL_FILE to it before init (the prec app does). C source: %(name)s.c

   buffer_data(x, buf, a, b)  E.compiledC function: buf[a..b) = int16 x[0..b-a) * window
   run(buf)                   features (FFT) + normalisation + model; returns the NOUT outputs
   predict(buf)               run(buf)[0]: the speed in m/s (speed models)
   newBuffer()                a window buffer (WIN x 3 floats, flat)
   spectrum(ch)               DFT models: |X_k| (k < DFT) of axis ch from the last run(), else undefined
   free()                     drop the TensorFlow instance (init() makes it again) */
var bin = atob("%(bin)s");
exports.NAME = "%(name)s";
exports.KIND = "%(kind)s";       // "speed" or "classifier"
exports.CLASSES = %(classes)s;   // classifier outputs, in order
exports.NOUT = %(nout)d;
exports.MODEL_FILE = "pmodel.tfl";  // the .tflite model in Storage
exports.MODEL_BYTES = %(mbytes)d;
exports.FREQ = %(freq)d;        // Hz to request from the Polar
exports.WIN = %(w)d;            // samples per window
exports.CH = 3;                // x, y, z
exports.RANGE = %(range)d;       // G, Polar ACC range
exports.DFT = %(dft)d;
exports.DFT_ABS = %(abs_js)s;
exports.NIN = %(nin)d;          // model inputs
exports.ARENA = %(arena)d;      // TensorFlow arena bytes
exports.UNIT = "m/s";
exports.buffer_data = E.nativeCall(%(bd)d, "void(int,int,int,int)", bin);
var features = E.nativeCall(%(ft)d, "void(int,int,int)", bin);
var tf, feat, work;
exports.init = function(arena) {
  if (tf) return;
  var m = require("Storage").read(exports.MODEL_FILE);
  if (!m || m.length !== exports.MODEL_BYTES) throw new Error(exports.MODEL_FILE + " missing or not this model's");
  m = E.toFlatString(m);
  feat = new Float32Array(%(nin_pad)d);
  work = new Float32Array(%(work)d);
  var a = arena || exports.ARENA, err;
  for (var k = 0; k < 3 && !tf; k++, a *= 2) {      // retry with a larger arena
    try { tf = require("tensorflow").create(a, m); exports.ARENA = a; } catch (e) { err = e; }
  }
  if (!tf) throw err;
};
exports.newBuffer = function() {
  var b = new Float32Array(exports.WIN * 3);
  if (!E.getAddressOf(b, true)) throw new Error("buffer not flat");
  return b;
};
exports.run = function(buf) {
  if (!tf) exports.init();
  features(E.getAddressOf(buf, true), E.getAddressOf(feat, true), E.getAddressOf(work, true));
  tf.getInput().set(%(slice)s);
  tf.invoke();
  return new Float32Array(tf.getOutput());     // a copy: the next invoke overwrites the output
};
exports.predict = function(buf) { return exports.run(buf)[0]; };
var spec = E.nativeCall(%(sp)d, "void(int,int,int)", bin);
exports.spectrum = function(ch) {
  if (!exports.DFT || !feat) return;
  var o = new Float32Array(exports.DFT);
  spec(E.getAddressOf(feat, true), E.getAddressOf(o, true), ch);
  return o;
};
exports.free = function() { tf = undefined; };
''' % {"name": meta["name"], "arch": meta["arch"], "w": w, "freq": meta["frequency"], "dft": dft,
       "what": "run/walk/still/stretch classifier" if clf else "speed model", "kind": "classifier" if clf else "speed",
       "classes": json.dumps(meta.get("classes", [])), "nout": len(meta.get("classes", [])) if clf else 1,
       "abs": " (abs)" if dft and ab else "", "q": "int8" if quantized else "float32",
       "bin": base64.b64encode(code).decode(), "mbytes": len(blob),
       "range": int(meta.get("range", 8)), "abs_js": "true" if ab else "false", "nin": nin, "arena": arena,
       "bd": funcs["buffer_data"] | 1, "ft": funcs["features"] | 1, "sp": funcs["spectrum"] | 1, "nin_pad": max(nin, 64),
       "work": max(2 * w, 64) if dft else 64, "slice": "feat" if nin >= 64 else "new Float32Array(feat.buffer, 0, %d)" % nin}


# ---- prediction on a pair ---------------------------------------------------------------------

def lowpass(t, x, tau, gap=None):
    """Causal first-order low-pass with time constant tau (s):
    y += (1 - exp(-dt/tau)) * (x - y), restarting after gaps longer than gap s."""
    y = np.empty_like(x, dtype=np.float64)
    if not len(x):
        return y
    gap = gap if gap is not None else 5 * float(np.median(np.diff(t))) if len(t) > 2 else np.inf
    y[0] = x[0]
    for i in range(1, len(x)):
        dt = t[i] - t[i - 1]
        y[i] = x[i] if dt > gap else y[i - 1] + (1 - math.exp(-dt / tau)) * (x[i] - y[i - 1])
    return y


lowpass_ = lowpass   # under another name, for functions with a lowpass parameter


def predict_pair(conf, name, pair_name, max_windows=20000, batch=2048, shift=None):
    """Run saved model `name` on the windows of a pair (its sub-intervals, the
    model's window/stride/features/normalisation) and compare with the GPS
    speed. A new window starts every `shift` samples; without shift, at most
    max_windows windows are used (evenly spaced, never closer than the
    model's shift). Returns {"t" (window centres, s since the .fit start),
    "pred", "target", "mae", "rmse", "bias", "r", "n", "step", "model", "pair"}."""
    import keras
    mdir = os.path.expanduser(conf["paths"]["models"])
    meta = json.load(open(os.path.join(mdir, name + ".json")))
    model = keras.models.load_model(os.path.join(mdir, name + ".keras"))
    pair = M.load_pair(conf, pair_name)
    arr = M.load_cooked(conf, pair)
    if arr is None:
        arr = M.load_cooked(conf, M.build_pair(conf, pair))
    e = {"window": int(meta["window"]), "stride": int(meta["stride"]), "shift": 1, "target": meta.get("target", "mean"),
         "dft": int(meta["dft"]), "dft_abs": bool(meta["dft_abs"]),
         "window_coeffs": np.array(meta["window_coeffs"], np.float32)}
    starts, target = windows_of(arr, pair.get("intervals") or [[arr[0, 0], arr[-1, 0]]], e)
    if not len(starts):
        raise ValueError("pair %s has no complete window of %d samples in its intervals" % (pair_name, e["window"]))
    step = int(shift) if shift else max(int(meta.get("shift", 1)), math.ceil(len(starts) / max_windows))
    starts, target = starts[::step], target[::step]
    X = arr[:, 1:4].astype(np.float32)
    offs = np.arange(e["window"]) * e["stride"]
    mean, std = np.array(meta["mean"], np.float32), np.array(meta["std"], np.float32)
    pred = []
    for i in range(0, len(starts), batch):
        w = X[starts[i:i + batch][:, None] + offs[None, :]]
        pred.append(model.predict((features_np(w, e) - mean) / std, verbose=0)[:, 0])
    pred = np.concatenate(pred).astype(np.float64)
    span = (e["window"] - 1) * e["stride"] + 1
    t = arr[starts + span // 2, 0]
    err = pred - target
    r = float(np.corrcoef(pred, target)[0, 1]) if len(pred) > 2 and pred.std() > 0 and target.std() > 0 else float("nan")
    return {"t": t, "pred": pred, "target": target.astype(np.float64), "mae": float(np.abs(err).mean()),
            "rmse": float(np.sqrt((err ** 2).mean())), "bias": float(err.mean()), "r": r, "n": len(pred),
            "step": step, "model": name, "pair": pair_name, "arch": meta["arch"], "class": meta.get("class")}


def classify_pair(conf, name, pair_name, max_windows=20000, batch=2048, shift=None, lowpass=()):
    """Run saved classifier `name` over the whole recording of a pair (windows
    as in predict_pair) and compare with the pair's class intervals. Returns
    {"t" (window centres, s since the .fit start), "prob" (n, classes),
    "pred" (class index), "actual" (class index of the interval holding the
    window centre, -1 for none), "lowpass": {tau: predicted class index after
    smoothing the probabilities}, "accuracy" (over windows with a class),
    "confusion", "classes", "n", "step", "acc_t", "acc" (the recording, for
    plotting), "model", "pair", "arch"}."""
    import keras
    mdir = os.path.expanduser(conf["paths"]["models"])
    meta = json.load(open(os.path.join(mdir, name + ".json")))
    model = keras.models.load_model(os.path.join(mdir, name + ".keras"))
    classes = meta.get("classes", list(M.CLASSES))
    pair = M.load_pair(conf, pair_name)
    acc = M.load_acc(conf, pair)
    t_all = acc["utc"] + pair["offset"] - M.load_fit(conf, pair)["start"]
    X = acc["acc"].astype(np.float32)
    e = {"window": int(meta["window"]), "stride": int(meta["stride"]), "shift": 1,
         "dft": int(meta["dft"]), "dft_abs": bool(meta["dft_abs"]),
         "window_coeffs": np.array(meta["window_coeffs"], np.float32)}
    starts = window_starts(t_all, [[t_all[0], t_all[-1]]], e)
    if not len(starts):
        raise ValueError("pair %s: the recording is shorter than one window" % pair_name)
    step = int(shift) if shift else max(int(meta.get("shift", 1)), math.ceil(len(starts) / max_windows))
    starts = starts[::step]
    offs = np.arange(e["window"]) * e["stride"]
    mean, std = np.array(meta["mean"], np.float32), np.array(meta["std"], np.float32)
    prob = []
    for i in range(0, len(starts), batch):
        w = X[starts[i:i + batch][:, None] + offs[None, :]]
        prob.append(model.predict((features_np(w, e) - mean) / std, verbose=0))
    prob = np.concatenate(prob).astype(np.float64)
    span = (e["window"] - 1) * e["stride"] + 1
    t = t_all[starts + span // 2]
    actual = np.full(len(t), -1)
    for k, c in enumerate(classes):
        actual[M.in_intervals(t, pair.get(c, []))] = k
    known = actual >= 0
    pred = prob.argmax(axis=1)
    smooth = {tau: np.column_stack([lowpass_(t, prob[:, k], tau) for k in range(prob.shape[1])]).argmax(axis=1)
              for tau in lowpass}

    def score(p):
        return float((p[known] == actual[known]).mean()) if known.any() else float("nan")
    return {"t": t, "prob": prob, "pred": pred, "actual": actual, "lowpass": smooth,
            "accuracy": score(pred), "lowpass_accuracy": {tau: score(p) for tau, p in smooth.items()},
            "confusion": confusion(actual[known], pred[known], len(classes)) if known.any() else None,
            "classes": classes, "n": len(t), "step": step, "acc_t": t_all, "acc": acc["acc"],
            "model": name, "pair": pair_name, "arch": meta["arch"]}


# ---- the model in the feather firmware ----------------------------------------------------

FIRMWARE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware", "prec_logger")
FQBN = "adafruit:nrf52:feather52840sense"
_ACTS = {"linear": 0, "relu": 1, "relu6": 2, "sigmoid": 3, "tanh": 4, "softmax": 5}
_SKIP = ("Flatten", "Reshape", "Dropout", "SpatialDropout1D", "SpatialDropout2D", "GaussianNoise",
         "GaussianDropout", "AlphaDropout", "ActivityRegularization", "InputLayer")


def _hwc(s):
    s = tuple(int(x) for x in s)
    return {1: (1, 1) + s, 2: (1,) + s, 3: s}[len(s)]


def _act(layer):
    a = getattr(layer, "activation", None)
    name = getattr(a, "__name__", "linear") if a is not None else "linear"
    if name not in _ACTS:
        raise ValueError("layer %s: activation %s is not supported on the feather" % (layer.name, name))
    return _ACTS[name]


def _same_pad(n, k, s, padding):
    if padding != "same":
        return 0
    out = -(-n // s)
    return max((out - 1) * s + k - n, 0) // 2


def keras_layers(model, input_shape):
    """The model as a list of engine layers (dicts with op, act, shapes, weights).
    Raises ValueError for layers the feather engine cannot run."""
    shape = tuple(input_shape)
    out = []
    for L in model.layers:
        cls = L.__class__.__name__
        new = tuple(L.compute_output_shape((1,) + shape)[1:]) if cls != "InputLayer" else shape
        if cls in _SKIP:
            shape = new
            continue
        ih, iw, ic = _hwc(shape)
        oh, ow, oc = _hwc(new)
        d = {"op": 0, "act": 0, "in": (ih, iw, ic), "out": (oh, ow, oc), "k": (1, 1), "s": (1, 1), "p": (0, 0),
             "w": None, "b": None, "name": L.name}
        cfg = L.get_config()
        wts = L.get_weights()
        if cls == "Dense":
            d.update(op=1, act=_act(L), w=wts[0], b=wts[1] if len(wts) > 1 else None)
        elif cls in ("Conv2D", "DepthwiseConv2D"):
            if tuple(cfg.get("dilation_rate", (1, 1))) != (1, 1) or cfg.get("groups", 1) != 1:
                raise ValueError("layer %s: dilation/groups are not supported" % L.name)
            kh, kw = wts[0].shape[:2]
            sh, sw = cfg["strides"]
            d.update(op=2 if cls == "Conv2D" else 3, act=_act(L), w=wts[0], b=wts[1] if len(wts) > 1 else None,
                     k=(kh, kw), s=(sh, sw), p=(_same_pad(ih, kh, sh, cfg["padding"]), _same_pad(iw, kw, sw, cfg["padding"])))
        elif cls in ("MaxPooling2D", "AveragePooling2D"):
            kh, kw = cfg["pool_size"]
            sh, sw = cfg["strides"] or cfg["pool_size"]
            d.update(op=4 if cls == "MaxPooling2D" else 5, k=(kh, kw), s=(sh, sw),
                     p=(_same_pad(ih, kh, sh, cfg["padding"]), _same_pad(iw, kw, sw, cfg["padding"])))
        elif cls == "BatchNormalization":
            k = 0
            gamma = wts[k] if cfg.get("scale", True) else np.ones(ic)
            k += 1 if cfg.get("scale", True) else 0
            beta = wts[k] if cfg.get("center", True) else np.zeros(ic)
            k += 1 if cfg.get("center", True) else 0
            mean, var = wts[k], wts[k + 1]
            sc = gamma / np.sqrt(var + cfg.get("epsilon", 1e-3))
            d.update(op=6, w=sc, b=beta - mean * sc)
        elif cls == "Activation":
            if cfg["activation"] not in _ACTS:
                raise ValueError("layer %s: activation %s is not supported" % (L.name, cfg["activation"]))
            d.update(op=7, act=_ACTS[cfg["activation"]])
        elif cls == "ReLU":
            if cfg.get("negative_slope") or cfg.get("threshold"):
                raise ValueError("layer %s: leaky/thresholded ReLU is not supported" % L.name)
            d.update(op=7, act=2 if cfg.get("max_value") == 6 else 1)
            if cfg.get("max_value") not in (None, 6):
                raise ValueError("layer %s: ReLU max_value must be 6 or none" % L.name)
        elif cls == "Softmax":
            d.update(op=7, act=5)
        else:
            raise ValueError("layer %s (%s) is not supported by the feather engine" % (L.name, cls))
        out.append(d)
        shape = new
    return out


FEATHER_SLOTS = ("speed", "classifier", "run", "walk")    # MK_SPEED, MK_CLASS, MK_RUN, MK_WALK in model_engine.h


def feather_slots(slots):
    """{slot: model name} from a dict, a single (speed) model name, or None."""
    if isinstance(slots, dict):
        return {k: v for k, v in slots.items() if v}
    return {"speed": slots} if slots else {}


def feather_header(conf, slots):
    """Text of firmware/prec_logger/model_data.h for the models in slots
    ({"speed", "classifier", "run", "walk": model name}, or one speed model
    name; empty or None: no model). Each model's code and tables sit in their
    own C++ namespace; MODEL_SLOTS lists them for predictor.cpp. All models
    must run at the same accelerometer rate (the Polar streams one)."""
    slots = feather_slots(slots)
    if not slots:
        return ("/* model_data.h: no speed model in this firmware (prec_train.py feather none). */\n"
                "#define MODEL_PRESENT 0\n")
    import keras
    bad = [k for k in slots if k not in FEATHER_SLOTS]
    if bad:
        raise ValueError("unknown feather slot %s (have %s)" % (", ".join(bad), ", ".join(FEATHER_SLOTS)))
    if "speed" not in slots and not ("classifier" in slots and ("run" in slots or "walk" in slots)):
        raise ValueError("the feather needs a speed model, or a classifier with a run and/or walk model")
    mdir = os.path.expanduser(conf["paths"]["models"])
    metas = {k: load_model_meta(conf, n) for k, n in slots.items()}
    for k, m in metas.items():
        if (k == "classifier") != (m.get("kind") == "classifier"):
            raise ValueError("%s is %s, not a %s model" % (m["name"], "a classifier" if m.get("kind") == "classifier"
                                                          else "a speed model", k))
        if k == "classifier" and list(m.get("classes", [])) != list(M.CLASSES):
            raise ValueError("classifier %s has classes %s, the firmware expects %s" % (m["name"], m.get("classes"),
                                                                                      list(M.CLASSES)))
    freqs = {m["frequency"] for m in metas.values()}
    if len(freqs) > 1:
        raise ValueError("the models run at different rates (%s Hz): the Polar streams one" %
                         ", ".join(map(str, sorted(freqs))))
    lines = ["/* model_data.h: models for the feather, generated by prec_train.py feather. Do not edit;",
             " * regenerate instead.",
             *[" *   %-10s %s (%s, window %d at %d Hz, dft %d%s)" % (k, m["name"], m["arch"], m["window"], m["frequency"],
                                                                 m["dft"], " abs" if m["dft"] and m["dft_abs"] else "")
               for k, m in metas.items()],
             " */", "#define MODEL_PRESENT 1", "#define MODEL_FREQ %d" % freqs.pop(),
             '#define MODEL_NAME "%s"' % ",".join("%s:%s" % (k, m["name"]) for k, m in metas.items()),
             '#include "model_engine.h"', ""]
    entries, maxwin, maxnin, maxact = [], 0, 0, 0
    for k, m in metas.items():
        model = keras.models.load_model(os.path.join(mdir, m["name"] + ".keras"))
        layers = keras_layers(model, m["input_shape"])
        mean, std = np.array(m["mean"], np.float32), np.array(m["std"], np.float32)
        nin = int(np.prod(m["input_shape"]))
        act = max([nin] + [int(np.prod(l["out"])) for l in layers])
        nout = int(np.prod(layers[-1]["out"]))
        maxwin, maxnin, maxact = max(maxwin, int(m["window"])), max(maxnin, nin), max(maxact, act)
        ns = "m_" + k
        lines += ["namespace %s {   /* %s */" % (ns, m["name"]), c_source(m, mean, std), ""]
        rows = []
        for i, l in enumerate(layers):
            wn = bn = "0"
            if l["w"] is not None:
                wn = "L%d_W" % i
                lines.append("static const float %s[%d] = {%s};" % (wn, np.size(l["w"]), _floats(np.asarray(l["w"], np.float32))))
            if l["b"] is not None:
                bn = "L%d_B" % i
                lines.append("static const float %s[%d] = {%s};" % (bn, np.size(l["b"]), _floats(np.asarray(l["b"], np.float32))))
            rows.append("  {%d, %d, %d, %d, %d, %d, %d, %d, %d, %d, %d, %d, %d, %d, %s, %s},  /* %s */" % (
                l["op"], l["act"], *l["in"], *l["out"], *l["k"], *l["s"], *l["p"], wn, bn, l["name"]))
        lines += ["static const MLayer NET[%d] = {" % len(layers)] + rows + ["};", "}", "#undef WIN", "#undef DFT",
                                                                           "#undef DFT_ABS", "#undef NOUT", ""]
        entries.append('  {MK_%s, "%s", %d, %d, %d, %d, %d, %s::NET, %s::buffer_data, %s::features, %s::spectrum},' % (
            {"speed": "SPEED", "classifier": "CLASS", "run": "RUN", "walk": "WALK"}[k], m["name"], int(m["window"]),
            nin, nout, len(layers), int(m["dft"]), ns, ns, ns, ns))
    lines += ["#define MODEL_MAXWIN %d" % maxwin, "#define MODEL_MAXNIN %d" % maxnin, "#define MODEL_MAXACT %d" % maxact,
              "#define MODEL_NSLOTS %d" % len(entries), "static const MSlot MODEL_SLOTS[MODEL_NSLOTS] = {"] + entries + ["};", ""]
    return "\n".join(lines)


def check_feather_host(header, conf, slots, n=6):
    """Compile model_data.h + model_engine.h for this computer (as C++, like the
    firmware) and compare every model's whole chain (buffer_data, features,
    layers) with Keras on random windows. Returns the largest absolute
    difference of any output; None without a compiler."""
    import ctypes, keras
    slots = feather_slots(slots)
    mdir = os.path.expanduser(conf["paths"]["models"])
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "model_data.h"), "w").write(header)
        open(os.path.join(d, "model_engine.h"), "w").write(open(os.path.join(FIRMWARE, "model_engine.h")).read())
        open(os.path.join(d, "t.cpp"), "w").write(
            '#include "model_data.h"\n'
            "static float A[MODEL_MAXACT], B[MODEL_MAXACT];\n"
            'extern "C" int host_run(int slot, long raw, float* out) {\n'
            "  static float buf[MODEL_MAXWIN * 3], feat[MODEL_MAXNIN], work[2 * MODEL_MAXWIN + 2];\n"
            "  const MSlot* s = &MODEL_SLOTS[slot];\n"
            "  s->buffer_data(raw, (long)buf, 0, s->win * 3);\n"
            "  s->features((long)buf, (long)feat, (long)work);\n"
            "  const float* o = mrun(s->net, s->layers, feat, s->nin, A, B);\n"
            "  for (int i = 0; i < s->nout; i++) out[i] = o[i];\n"
            "  return s->nout;\n}\n")
        try:
            subprocess.run(["c++", "-O2", "-shared", "-fPIC", "-DPTR=long", "-o", os.path.join(d, "t.so"),
                            os.path.join(d, "t.cpp")], check=True, capture_output=True, text=True)
        except FileNotFoundError:
            return None
        except subprocess.CalledProcessError as ex:
            raise RuntimeError("model_data.h does not compile:\n" + ex.stderr[-2000:])
        lib = ctypes.CDLL(os.path.join(d, "t.so"))
        out = (ctypes.c_float * 16)()
        rng = np.random.default_rng(6)
        worst = 0.0
        for slot, (k, name) in enumerate(slots.items()):
            meta = load_model_meta(conf, name)
            model = keras.models.load_model(os.path.join(mdir, name + ".keras"))
            w = int(meta["window"])
            e = {"dft": int(meta["dft"]), "dft_abs": bool(meta["dft_abs"]),
                 "window_coeffs": np.array(meta["window_coeffs"], np.float32)}
            mean, std = np.array(meta["mean"], np.float32), np.array(meta["std"], np.float32)
            for _ in range(n):
                raw = rng.normal(0, 600, (w, 3)).round().astype(np.int16)
                raw[:, 0] -= 1000
                ref = model.predict(((features_np(raw[None].astype(np.float32), e) - mean) / std), verbose=0)[0]
                k_out = lib.host_run(slot, ctypes.c_long(raw.ctypes.data), out)
                worst = max(worst, float(np.abs(np.array(out[:k_out]) - ref).max()))
        if worst > 1e-3:
            raise RuntimeError("feather engine differs from Keras by %.3g" % worst)
        return worst


def firmware_dir(conf):
    """The firmware sketch folder to build in: firmware/prec_logger next to the
    tools when it is writable (a source checkout), otherwise a copy in
    paths.firmware (an installed, read-only package), refreshed from the
    installed files when those are newer."""
    src = os.path.abspath(FIRMWARE)
    if os.access(src, os.W_OK):
        return src
    import shutil
    dest = os.path.expanduser(conf["paths"].get("firmware") or "~/.local/share/prec/firmware/prec_logger")
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(src):
        s, d = os.path.join(src, f), os.path.join(dest, f)
        if os.path.isfile(s) and (f != "model_data.h" or not os.path.exists(d)) and \
                (not os.path.exists(d) or os.path.getmtime(s) > os.path.getmtime(d)):
            if os.path.exists(d):
                os.chmod(d, 0o644)
            shutil.copyfile(s, d)                    # the copies must be writable
            os.utime(d, (os.path.getmtime(s), os.path.getmtime(s)))
        if os.path.exists(d):
            os.chmod(d, 0o644)
    return dest


def build_feather(conf, slots, flash=True, port=None, log=print):
    """Put models into the firmware (slots: {"speed", "classifier", "run",
    "walk": name}, one speed model name, or None for none): write
    model_data.h, check it on this computer, compile with arduino-cli and, if
    flash, upload over USB."""
    slots = feather_slots(slots)
    header = feather_header(conf, slots)
    if slots:
        diff = check_feather_host(header, conf, slots)
        if diff is not None:
            log("feather engine matches Keras (max difference %.2g)" % diff)
    fw = firmware_dir(conf)
    path = os.path.join(fw, "model_data.h")
    open(path, "w").write(header)
    log("wrote %s (%s)" % (path, ", ".join("%s %s" % kv for kv in slots.items()) or "no model"))
    cmd = ["arduino-cli", "compile", "-b", FQBN, fw]
    if flash:
        port = port or (sorted(glob.glob("/dev/ttyACM*")) or [None])[0]
        if not port:
            raise RuntimeError("no feather on USB (plug it in and move it to wake it up)")
        cmd += ["-u", "-p", port]
    log("compiling%s ..." % (" and uploading to " + port if flash else ""))
    r = subprocess.run(cmd, capture_output=True, text=True)
    for line in (r.stdout + r.stderr).splitlines():
        if any(k in line for k in ("Sketch uses", "Global variables", "Device programmed", "error", "Error")):
            log(line.strip())
    if r.returncode:
        raise RuntimeError("arduino-cli failed (exit %d)" % r.returncode)
    return path


# ---- models ---------------------------------------------------------------------------------

def list_models(conf):
    """Saved models' metadata, newest first."""
    mdir = os.path.expanduser(conf["paths"]["models"])
    out = []
    for f in sorted(glob.glob(os.path.join(mdir, "model_*.json")), reverse=True):
        try:
            m = json.load(open(f))
            m["has_js"] = os.path.exists(f[:-5] + ".js")
            out.append(m)
        except (OSError, ValueError):
            pass
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    s = sub.add_parser("set")
    s.add_argument("assign", nargs="+")
    h = sub.add_parser("histogram")
    h.add_argument("role", choices=ROLES)
    sub.add_parser("archs")
    tr = sub.add_parser("train")
    tr.add_argument("--class", dest="cls", choices=("run", "walk"),
                    help="train only on the run or walk intervals (name ends in _run/_walk)")
    tr.add_argument("--classifier", action="store_true",
                    help="train a run/walk/still/stretch classifier (architecture class_arch; name ends in _classifier)")
    x = sub.add_parser("export")
    x.add_argument("name")
    rt = sub.add_parser("retrain", help="continue training a saved model on the pairs of its .json")
    rt.add_argument("name")
    rt.add_argument("--epochs", type=int, help="more epochs (default: its epochs parameter)")
    rt.add_argument("--lr", type=float, help="learning rate (default: where it stopped)")
    sub.add_parser("models")
    pr = sub.add_parser("predict")
    pr.add_argument("name")
    pr.add_argument("pair")
    pr.add_argument("--csv", help="write t, target, prediction (and the low-passed predictions) to this file")
    pr.add_argument("--shift", type=int, help="samples between windows (default: at most 20000 windows)")
    pr.add_argument("--lowpass", type=float, action="append", default=[],
                    help="also report the prediction through a low-pass with this time constant (s); repeatable")
    fe = sub.add_parser("feather")
    fe.add_argument("name", nargs="?", help="speed model name, or 'none'")
    for k in ("classifier", "run", "walk"):
        fe.add_argument("--" + k, help="%s model" % k)
    fe.add_argument("--no-flash", action="store_true", help="only compile")
    fe.add_argument("--port")
    a = ap.parse_args()
    conf = M.load_conf(a.conf)
    t = load_train(conf)
    if a.cmd == "show":
        print(json.dumps(t, indent=2))
    elif a.cmd == "set":
        for kv in a.assign:
            k, v = kv.split("=", 1)
            print(k, "=", set_param(t, k, v))
        save_train(conf, t)
    elif a.cmd == "histogram":
        e = effective(conf, t)
        edges, counts = histogram(conf, e, a.role)
        for lo, n in zip(edges, counts):
            print("%5.2f-%5.2f %10.1f %s" % (lo, lo + e["resolution"], n, "#" * int(round(4 * math.log(n + 1)))))
    elif a.cmd == "archs":
        for kind in ("speed", "class"):
            for n in list_archs(conf, kind):
                i = arch_info(conf, n, kind)
                print("%-6s %-10s dft=%s abs=%s  %s  (%s)" % (kind, n, i["dft"], i["dft_abs"], i["doc"], i["path"]))
    elif a.cmd == "train":
        print("model", train(conf, t, log=lambda s: print(s, flush=True), cls=a.cls, classifier=a.classifier), flush=True)
    elif a.cmd == "retrain":
        print("model", train(conf, log=lambda s: print(s, flush=True), base=a.name, epochs=a.epochs, lr=a.lr),
              flush=True)
    elif a.cmd == "export":
        export(conf, a.name)
    elif a.cmd == "predict":
        r = predict_pair(conf, a.name, a.pair, shift=a.shift)
        print("predict: %s on %s: %d windows (every %d), MAE %.3f m/s, RMSE %.3f, bias %+.3f, r %.3f" % (
            a.name, a.pair, r["n"], r["step"], r["mae"], r["rmse"], r["bias"], r["r"]))
        cols = [r["t"], r["target"], r["pred"]]
        for tau in a.lowpass:
            cols.append(lowpass(r["t"], r["pred"], tau))
            print("predict: low-pass %g s: MAE %.3f m/s" % (tau, np.abs(cols[-1] - r["target"]).mean()))
        if a.csv:
            np.savetxt(a.csv, np.column_stack(cols), delimiter=",", comments="", fmt="%.4f",
                       header=",".join(["t", "target", "prediction"] + ["lowpass_%g" % tau for tau in a.lowpass]))
    elif a.cmd == "feather":
        slots = {"speed": None if a.name in (None, "none") else a.name, "classifier": a.classifier, "run": a.run,
                 "walk": a.walk}
        build_feather(conf, slots, not a.no_flash, a.port)
    elif a.cmd == "models":
        for m in list_models(conf):
            k = "accuracy" if m.get("kind") == "classifier" else "mae"
            print("%s  %-6s  val_%s %s  test_%s %s  %s" % (
                m["name"], m["arch"], k, ("%.3f" % m["history"]["val_" + k][-1]) if m.get("history", {}).get("val_" + k) else "-",
                k, ("%.3f" % m["test_" + k]) if "test_" + k in m else "-", "js" if m["has_js"] else "no js"))


if __name__ == "__main__":
    main()

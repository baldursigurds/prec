#!/usr/bin/env python3
"""prec_gui.py - plot and map windows for merging PREC data with GPS data.

Started by the TUI's F3 tab (one window per command; the TUI waits until it
is closed), or by hand:

  prec_gui.py offset NAME            set the time offset of a pair
  prec_gui.py plot NAME              plot acceleration and speed of a pair
  prec_gui.py intervals NAME         edit the pair's sub-intervals (with a map)
  prec_gui.py classes NAME CLASS     edit run/walk/still/stretch intervals (with a map)
  prec_gui.py avoid [--pair NAME]    draw new avoid rectangles on a map
  prec_gui.py avoid-edit RECT        move, resize or delete one avoid rectangle
  prec_gui.py predict MODEL NAME     a model's predictions on a pair against the GPS speed
  prec_gui.py history MODEL          loss, MAE and learning rate per epoch of a saved model

Options: --conf FILE (prec.conf), --png FILE (draw the window into FILE and
exit, for checks and reports). Exit status: 0 when something was saved
(offset confirmed, intervals or rectangles saved), 1 when closed without
saving, 2 on errors. See docs/MERGE.md.
"""
import argparse, math, os, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prec_merge as M   # noqa: E402

R_EARTH = 6378137.0
ORIGIN = math.pi * R_EARTH


def setup_matplotlib(png):
    """Pick the backend and free the keys the windows use from matplotlib's defaults."""
    import matplotlib
    if png:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    for k in ("keymap.save", "keymap.quit", "keymap.back", "keymap.forward", "keymap.yscale",
              "keymap.xscale", "keymap.grid", "keymap.grid_minor", "keymap.fullscreen"):
        plt.rcParams[k] = []
    return plt


# ---- map tiles ------------------------------------------------------------

def merc(lat, lon):
    """WGS84 degrees -> Web Mercator metres (EPSG:3857)."""
    lat = np.clip(np.asarray(lat, dtype=float), -85.05, 85.05)
    x = np.radians(np.asarray(lon, dtype=float)) * R_EARTH
    y = np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * R_EARTH
    return x, y


def unmerc(x, y):
    """Web Mercator metres -> (lat, lon) degrees."""
    lon = np.degrees(np.asarray(x, dtype=float) / R_EARTH)
    lat = np.degrees(2 * np.arctan(np.exp(np.asarray(y, dtype=float) / R_EARTH)) - np.pi / 2)
    return lat, lon


def fetch_tile(conf, z, x, y):
    """One OpenStreetMap tile as a PIL image (cached on disk); None if unavailable."""
    from PIL import Image
    n = 2 ** z
    x %= n
    if not 0 <= y < n:
        return None
    path = os.path.join(conf["paths"]["cache"], "tiles", str(z), str(x), "%d.png" % y)
    if not os.path.exists(path):
        import requests
        try:
            r = requests.get(conf["map"]["tile_url"].format(z=z, x=x, y=y), timeout=10,
                             headers={"User-Agent": conf["map"]["user_agent"]})
            if r.status_code != 200:
                return None
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(r.content)
        except Exception:
            return None
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def map_image(conf, x0, x1, y0, y1):
    """Background image for the Mercator box x0..x1, y0..y1 at the most detailed
    zoom that needs at most map.max_tiles tiles. Returns (array, extent) or None."""
    from PIL import Image
    size0 = 2 * ORIGIN
    for z in range(18, 0, -1):
        t = size0 / 2 ** z
        tx0, tx1 = int((x0 + ORIGIN) // t), int((x1 + ORIGIN) // t)
        ty0, ty1 = int((ORIGIN - y1) // t), int((ORIGIN - y0) // t)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= conf["map"]["max_tiles"]:
            break
    img = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), (235, 235, 235))
    got = 0
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            tile = fetch_tile(conf, z, tx, ty)
            if tile:
                img.paste(tile, ((tx - tx0) * 256, (ty - ty0) * 256))
                got += 1
    if not got:
        return None
    extent = (tx0 * t - ORIGIN, (tx1 + 1) * t - ORIGIN, ORIGIN - (ty1 + 1) * t, ORIGIN - ty0 * t)
    return np.asarray(img), extent


class MapLayer:
    """OpenStreetMap background of an axes in Web Mercator coordinates that
    reloads its tiles after the view is zoomed or panned."""

    def __init__(self, ax, conf):
        self.ax, self.conf = ax, conf
        self.image = None
        self.shown = None
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xticks([])
        ax.set_yticks([])
        ax.text(0.995, 0.005, "© OpenStreetMap contributors", transform=ax.transAxes, ha="right", va="bottom",
                fontsize=7, bbox=dict(fc="white", ec="none", alpha=0.7), zorder=10)
        self.timer = ax.figure.canvas.new_timer(interval=400)
        self.timer.add_callback(self.check)
        self.timer.start()

    def fit(self, x, y, pad=0.12, min_size=400.0):
        """Show the box around the Mercator points x, y."""
        x, y = np.asarray(x)[~np.isnan(x)], np.asarray(y)[~np.isnan(y)]
        if not len(x):
            return
        cx, cy = (x.min() + x.max()) / 2, (y.min() + y.max()) / 2
        hw = max(x.max() - x.min(), min_size) * (0.5 + pad)
        hh = max(y.max() - y.min(), min_size) * (0.5 + pad)
        self.ax.set_xlim(cx - hw, cx + hw)
        self.ax.set_ylim(cy - hh, cy + hh)
        self.ax.apply_aspect()      # equal aspect widens one axis: load tiles for the final view
        self.load()

    def load(self):
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        res = map_image(self.conf, x0, x1, y0, y1)
        if self.image is not None:
            self.image.remove()
            self.image = None
        if res:
            arr, ext = res
            self.image = self.ax.imshow(arr, extent=ext, zorder=0, interpolation="bilinear")
            self.ax.set_xlim(x0, x1)
            self.ax.set_ylim(y0, y1)
        self.shown = (x0, x1, y0, y1)

    def check(self):
        lim = self.ax.get_xlim() + self.ax.get_ylim()
        if self.shown and max(abs(a - b) for a, b in zip(lim, self.shown)) > 1e-6 * (abs(lim[1] - lim[0]) + 1):
            self.load()
            self.ax.figure.canvas.draw_idle()


# ---- shared drawing -------------------------------------------------------

def plot_series(ax, conf, t, acc, ts, speed, decimate=None):
    """Acceleration x/y/z (left axis, mG) and speed (right axis, m/s) against t (s).
    Returns (acc lines, speed axis, speed line)."""
    pc = conf["plot"]
    k = max(1, int(decimate or pc["decimate"]))
    lines = []
    for i, (name, col) in enumerate((("x", pc["acc_x"]), ("y", pc["acc_y"]), ("z", pc["acc_z"]))):
        lines.append(ax.plot(t[::k], acc[::k, i], lw=0.5, color=col, alpha=0.8, label="acc " + name)[0])
    ax.set_xlabel("time since .fit start (s)")
    ax.set_ylabel("acceleration (mG)")
    ax2 = ax.twinx()
    sl = ax2.plot(ts, speed, lw=1.6, color=pc["speed"], label="speed")[0]
    ax2.set_ylabel("speed (m/s)", color=pc["speed"])
    ax2.set_ylim(bottom=0)
    ax.legend(handles=lines + [sl], loc="lower left", fontsize=8, ncol=4)
    return lines, ax2, sl


def class_bands(ax, conf, pair, skip=None, alpha=0.9):
    """Coloured bands at the top of ax for the run/walk/still/stretch intervals."""
    arts = []
    for c in M.CLASSES:
        if c == skip:
            continue
        for a, b in pair.get(c, []):
            arts.append(ax.axvspan(a, b, ymin=0.95, ymax=1.0, color=conf["plot"][c], alpha=alpha, lw=0))
    return arts


def fmt_t(s):
    s = float(s)
    return "%d:%02d:%04.1f" % (s // 3600, s % 3600 // 60, s % 60) if s >= 3600 else "%d:%04.1f" % (s // 60, s % 60)


# ---- offset ----------------------------------------------------------------

class OffsetWindow:
    """Acceleration and .fit speed against .fit time; the offset moves the
    acceleration. Buttons/keys change it, Confirm writes it to the pair."""

    def __init__(self, plt, conf, pair):
        from matplotlib.widgets import Button, TextBox
        self.conf, self.pair = conf, pair
        self.acc = M.load_acc(conf, pair)
        self.fit = M.load_fit(conf, pair)
        self.base = self.acc["utc"] - self.fit["start"]
        self.offset = float(pair["offset"])
        self.saved = False
        self.fig = plt.figure(figsize=(14, 7))
        self.fig.canvas.manager.set_window_title("PREC offset - %s" % pair["name"])
        self.ax = self.fig.add_axes([0.06, 0.24, 0.88, 0.68])
        k = max(1, int(conf["plot"]["decimate"]))
        self.t_dec = self.base[::k]
        self.lines, self.ax2, _ = plot_series(self.ax, conf, self.base + self.offset, self.acc["acc"],
                                               self.fit["t"], self.fit["speed"])
        self.ax.set_xlim(min(0, self.base[0] + self.offset) - 30, max(self.fit["t"][-1], self.base[-1] + self.offset) + 30)
        oc = conf["offset"]
        y = 0.08
        self.fig.text(0.06, y + 0.065, "offset (s):", fontsize=10)
        self.box = TextBox(self.fig.add_axes([0.12, y + 0.05, 0.1, 0.05]), "", initial="%.1f" % self.offset)
        self.box.on_submit(self.typed)
        self.buttons = []
        steps = [-oc["step_coarse"], -oc["step"], -oc["step_fine"], oc["step_fine"], oc["step"], oc["step_coarse"]]
        for i, s in enumerate(steps):
            b = Button(self.fig.add_axes([0.24 + i * 0.065, y + 0.05, 0.06, 0.05]), "%+g" % s)
            b.on_clicked(lambda e, s=s: self.set(self.offset + s))
            self.buttons.append(b)
        for i, (label, fn) in enumerate((("Auto", self.auto), ("Confirm", self.confirm), ("Cancel", self.cancel))):
            b = Button(self.fig.add_axes([0.66 + i * 0.1, y + 0.05, 0.09, 0.05]), label)
            b.on_clicked(lambda e, fn=fn: fn())
            self.buttons.append(b)
        self.info = self.fig.text(0.06, y - 0.02, "", fontsize=9)
        self.fig.text(0.06, 0.015, "keys: ←/→ ±%g s   shift+←/→ ±%g s   ,/. ±%g s   a auto   enter confirm   esc cancel   "
                      "(toolbar: zoom/pan)" % (oc["step"], oc["step_coarse"], oc["step_fine"]), fontsize=8, color="#555")
        self.fig.canvas.mpl_connect("key_press_event", self.key)
        self.update_title()

    def update_title(self, extra=""):
        p = self.pair
        self.ax.set_title("%s  ·  %s + %s  ·  offset %+.1f s  (saved: %+.1f s%s)" % (
            p["name"], p["prc"], p["fit"], self.offset, p["offset"],
            ", confirmed" if p.get("offset_confirmed") else ", not confirmed"))
        self.info.set_text(extra or "Positive offsets move the acceleration later. Confirm writes the offset and rebuilds the pair.")

    def set(self, value):
        self.offset = round(float(value), 3)
        x = self.base + self.offset
        k = max(1, int(self.conf["plot"]["decimate"]))
        for ln in self.lines:
            ln.set_xdata(x[::k])
        self.box.set_val("%.1f" % self.offset)
        self.update_title()
        self.fig.canvas.draw_idle()

    def typed(self, text):
        try:
            v = float(text)
        except ValueError:
            return
        if abs(v - self.offset) > 1e-9:
            self.set(v)

    def auto(self):
        self.info.set_text("searching ±%g s ..." % self.conf["offset"]["auto_range"])
        self.fig.canvas.draw()
        off, c = M.estimate_offset(self.acc, self.fit, self.offset, self.conf["offset"]["auto_range"])
        if off is None:
            self.update_title("Auto: too little overlap to estimate the offset.")
        else:
            self.set(off)
            self.update_title("Auto: best match of acceleration activity and speed at %+.1f s (correlation %.2f)." % (off, c))
        self.fig.canvas.draw_idle()

    def confirm(self):
        p = M.load_pair(self.conf, self.pair["name"])
        p["offset"] = self.offset
        p["offset_confirmed"] = True
        M.save_pair(self.conf, p)
        self.saved = True
        print("offset %+.3f s confirmed" % self.offset)
        import matplotlib.pyplot as plt
        plt.close(self.fig)

    def cancel(self):
        import matplotlib.pyplot as plt
        plt.close(self.fig)

    def key(self, e):
        if self.box.capturekeystrokes:
            return
        oc = self.conf["offset"]
        moves = {"left": -oc["step"], "right": oc["step"], "shift+left": -oc["step_coarse"],
                 "shift+right": oc["step_coarse"], ",": -oc["step_fine"], ".": oc["step_fine"]}
        if e.key in moves:
            self.set(self.offset + moves[e.key])
        elif e.key == "a":
            self.auto()
        elif e.key == "enter":
            self.confirm()
        elif e.key in ("escape", "q"):
            self.cancel()


# ---- plot ---------------------------------------------------------------------

class PlotWindow:
    """Read-only plot of a pair's cooked array with its intervals."""

    def __init__(self, plt, conf, pair):
        arr = M.load_cooked(conf, pair)
        if arr is None:
            raise SystemExit("pair %s has no cooked array; build it first" % pair["name"])
        self.fig = plt.figure(figsize=(14, 6.5))
        self.fig.canvas.manager.set_window_title("PREC plot - %s" % pair["name"])
        ax = self.fig.add_axes([0.06, 0.1, 0.88, 0.8])
        plot_series(ax, conf, arr[:, 0], arr[:, 1:4], arr[::int(max(1, conf["plot"]["decimate"])), 0],
                    arr[::int(max(1, conf["plot"]["decimate"])), 4])
        for a, b in pair.get("intervals", []):
            ax.axvspan(a, b, color=conf["plot"]["interval"], alpha=0.08, lw=0)
        class_bands(ax, conf, pair)
        ax.set_xlim(arr[0, 0], arr[-1, 0])
        counts = ", ".join("%s %d" % (c, len(pair.get(c, []))) for c in M.CLASSES if pair.get(c))
        ax.set_title("%s  ·  offset %+.1f s%s  ·  %d samples  ·  %d intervals (shaded)%s" % (
            pair["name"], pair["offset"], "" if pair.get("offset_confirmed") else " (not confirmed)",
            len(arr), len(pair.get("intervals", [])), "  ·  " + counts if counts else ""))
        self.saved = False


# ---- prediction --------------------------------------------------------------

class PredictWindow:
    """A model's predictions on a pair against the GPS speed: over time (with
    the pair's intervals and classes) and as a scatter plot."""

    def __init__(self, plt, conf, model, pair_name, shift=None, lowpass=()):
        import prec_train as T
        r = T.predict_pair(conf, model, pair_name, shift=shift)
        pair = M.load_pair(conf, pair_name)
        pc = conf["plot"]
        print("predict: %s on %s: %d windows (every %d), MAE %.3f m/s, RMSE %.3f, bias %+.3f, r %.3f" % (
            model, pair_name, r["n"], r["step"], r["mae"], r["rmse"], r["bias"], r["r"]), flush=True)
        self.saved = False
        self.fig = plt.figure(figsize=(15, 7))
        self.fig.canvas.manager.set_window_title("PREC predict - %s on %s" % (model, pair_name))
        ax = self.fig.add_axes([0.05, 0.1, 0.62, 0.8])
        for a, b in pair.get("intervals", []):
            ax.axvspan(a, b, color=pc["interval"], alpha=0.07, lw=0)
        cls = r.get("class")                     # a run or walk model: its intervals get a background
        if cls:
            for a, b in pair.get(cls, []):
                ax.axvspan(a, b, color=pc[cls], alpha=0.18, lw=0, zorder=0)
            m = M.in_intervals(r["t"], pair.get(cls, []))
            if m.any():
                print("predict: inside the %s intervals: %d windows, MAE %.3f m/s, bias %+.3f" % (
                    cls, m.sum(), np.abs(r["pred"][m] - r["target"][m]).mean(), (r["pred"][m] - r["target"][m]).mean()),
                    flush=True)
            else:
                print("predict: %s has no %s intervals" % (pair_name, cls), flush=True)
        class_bands(ax, conf, pair)
        # break the lines between intervals (no windows there)
        t, tg, pr = r["t"], r["target"], r["pred"]
        gap = np.flatnonzero(np.diff(t) > 5 * np.median(np.diff(t))) + 1 if len(t) > 2 else np.array([], int)
        t, tg, pr = (np.insert(v.astype(float), gap, np.nan) for v in (t, tg, pr))
        ax.plot(t, tg, color="black", lw=1.4, zorder=4, label="target (GPS speed)")
        ax.plot(t, pr, color=pc["speed"], lw=0.9, alpha=0.6 if lowpass else 0.85,
                label="prediction (MAE %.3f)" % r["mae"])
        colors = ["#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2"]
        for i, tau in enumerate(lowpass):
            f = T.lowpass(r["t"], r["pred"], tau)
            mae = float(np.abs(f - r["target"]).mean())
            print("predict: low-pass %g s: MAE %.3f m/s" % (tau, mae), flush=True)
            ax.plot(t, np.insert(f, gap, np.nan), color=colors[i % len(colors)], lw=1.6, zorder=3,
                    label="low-pass %g s (MAE %.3f)" % (tau, mae))
        ax.set_xlabel("time since .fit start (s)")
        ax.set_ylabel("speed (m/s)")
        ax.set_xlim(r["t"][0], r["t"][-1])
        ax.legend(loc="lower left", fontsize=9)
        ax.set_title("%s (%s%s) on %s  ·  %d windows%s" % (model, r["arch"], ", %s model: %s intervals shaded" % (cls, cls)
                                                          if cls else "", pair_name, r["n"],
                                                       ", every %d-th" % r["step"] if r["step"] > 1 else ""))
        sx = self.fig.add_axes([0.73, 0.1, 0.25, 0.8])
        sx.scatter(r["target"], r["pred"], s=3, alpha=0.25, color=pc["speed"], lw=0)
        lo = min(r["target"].min(), r["pred"].min())
        hi = max(r["target"].max(), r["pred"].max())
        sx.plot([lo, hi], [lo, hi], color="black", lw=1)
        sx.set_xlim(lo, hi)
        sx.set_ylim(lo, hi)
        sx.set_aspect("equal", adjustable="box")
        sx.set_xlabel("target (m/s)")
        sx.set_ylabel("prediction (m/s)")
        sx.set_title("MAE %.3f  RMSE %.3f  bias %+.3f  r %.3f" % (r["mae"], r["rmse"], r["bias"], r["r"]), fontsize=10)


def class_runs(t, labels):
    """[(start, end, label)] of the stretches of equal labels at times t (window
    centres): each one reaches halfway to its neighbours; gaps (more than 5x the
    median step) end a stretch."""
    if not len(t):
        return []
    dt = np.diff(t)
    med = float(np.median(dt)) if len(dt) else 1.0
    lo = np.r_[t[0] - med / 2, (t[1:] + t[:-1]) / 2]
    hi = np.r_[(t[1:] + t[:-1]) / 2, t[-1] + med / 2]
    gap = np.r_[dt > 5 * med, False]
    hi[gap], lo[np.r_[False, gap[:-1]]] = t[gap] + med / 2, t[np.r_[False, gap[:-1]]] - med / 2
    out = []
    for i, k in enumerate(labels):
        if out and out[-1][2] == k and not gap[i - 1] and abs(out[-1][1] - lo[i]) < 1e-9:
            out[-1][1] = hi[i]
        else:
            out.append([lo[i], hi[i], k])
    return out


class ClassifyWindow:
    """A classifier on a pair's whole recording: acceleration (and speed) on
    top; below, one band per row: the actual class intervals, the predicted
    class and, per low-pass time constant, the class after smoothing the
    probabilities; right, the confusion matrix of the windows with a class."""

    def __init__(self, plt, conf, model, pair_name, shift=None, lowpass=()):
        import prec_train as T
        r = T.classify_pair(conf, model, pair_name, shift=shift, lowpass=lowpass)
        pair = M.load_pair(conf, pair_name)
        pc, classes = conf["plot"], r["classes"]
        colors = [pc[c] for c in classes]
        print("predict: %s on %s: %d windows (every %d), accuracy %.3f over the windows with a class" % (
            model, pair_name, r["n"], r["step"], r["accuracy"]), flush=True)
        for tau in lowpass:
            print("predict: low-pass %g s: accuracy %.3f" % (tau, r["lowpass_accuracy"][tau]), flush=True)
        self.saved = False
        self.fig = plt.figure(figsize=(15, 8))
        self.fig.canvas.manager.set_window_title("PREC classify - %s on %s" % (model, pair_name))
        ax = self.fig.add_axes([0.11, 0.5, 0.57, 0.42])
        arr = M.load_cooked(conf, pair)
        k = int(max(1, pc["decimate"]))
        st, ss = (arr[::k, 0], arr[::k, 4]) if arr is not None else (np.zeros(0), np.zeros(0))
        plot_series(ax, conf, r["acc_t"], r["acc"], st, ss)
        ax.set_xlabel("")
        ax.set_title("%s (%s classifier) on %s  ·  %d windows%s" % (
            model, r["arch"], pair_name, r["n"], ", every %d-th" % r["step"] if r["step"] > 1 else ""))
        bx = self.fig.add_axes([0.11, 0.1, 0.57, 0.34], sharex=ax)
        rows = [("actual", [(a, b, i) for i, c in enumerate(classes) for a, b in pair.get(c, [])])]
        rows.append(("predicted (%.0f%%)" % (100 * r["accuracy"]), class_runs(r["t"], r["pred"])))
        for tau in lowpass:
            rows.append(("low-pass %g s (%.0f%%)" % (tau, 100 * r["lowpass_accuracy"][tau]),
                         class_runs(r["t"], r["lowpass"][tau])))
        n = len(rows)
        for j, (label, runs) in enumerate(rows):
            y = n - 1 - j
            for i in range(len(classes)):
                spans = [(a, b - a) for a, b, kk in runs if kk == i]
                if spans:
                    bx.broken_barh(spans, (y - 0.4, 0.8), color=colors[i], lw=0)
            if j == 0:
                bx.axhline(y - 0.5, color="black", lw=1)
        bx.set_yticks(range(n), [label for label, _ in reversed(rows)])
        bx.set_ylim(-0.6, n - 0.4)
        bx.set_xlim(min(r["acc_t"][0], r["t"][0]), max(r["acc_t"][-1], r["t"][-1]))
        bx.set_xlabel("time since .fit start (s)")
        from matplotlib.patches import Patch
        bx.legend(handles=[Patch(color=col, label=c) for c, col in zip(classes, colors)], loc="upper center",
                  bbox_to_anchor=(0.5, -0.18), ncol=len(classes), fontsize=9, frameon=False)
        # confusion matrix: rows the actual class, columns the predicted one; colour = share of the row
        cx = self.fig.add_axes([0.77, 0.2, 0.21, 0.6])
        cm = r["confusion"]
        if cm is None:
            cx.text(0.5, 0.5, "no class intervals\nin this pair", ha="center", va="center", transform=cx.transAxes)
            cx.set_axis_off()
        else:
            share = cm / np.maximum(cm.sum(axis=1, keepdims=True), 1)
            cx.imshow(share, cmap="Greens", vmin=0, vmax=1)
            for i in range(len(classes)):
                for j in range(len(classes)):
                    cx.text(j, i, "%d" % cm[i, j], ha="center", va="center", fontsize=9,
                            color="white" if share[i, j] > 0.6 else "black")
            cx.set_xticks(range(len(classes)), classes, rotation=30)
            cx.set_yticks(range(len(classes)), classes)
            cx.set_xlabel("predicted")
            cx.set_ylabel("actual")
            cx.set_title("accuracy %.3f  (%d windows with a class)" % (r["accuracy"], int(cm.sum())), fontsize=10)


# ---- training history -------------------------------------------------------

class HistoryWindow:
    """Loss, MAE and learning rate per epoch of a saved model, from its .json;
    the epoch whose weights were kept is marked."""

    def __init__(self, plt, conf, name):
        import json
        meta = json.load(open(os.path.join(os.path.expanduser(conf["paths"]["models"]), name + ".json")))
        h = meta.get("history", {})
        if not h.get("loss"):
            raise SystemExit("model %s has no training history" % name)
        ep = np.arange(1, len(h["loss"]) + 1)
        clf = meta.get("kind") == "classifier"
        metric = "accuracy" if clf else "mae"
        mon = "val_loss" if h.get("val_loss") else "loss"
        early = int(meta.get("params", {}).get("early_stop", 0) or 0)
        kept = int(np.argmin(h[mon])) + 1 if early else len(ep)   # restore_best_weights only with early stopping
        kept = int(meta.get("kept_epoch", kept))
        why = "lowest %s" % mon if early else "last epoch (early stopping off)"
        if meta.get("parent"):
            why = "%s of epochs %d-%d; epochs 1-%d (shaded): %s" % (
                why if early else "last epoch", meta["parent_epochs"] + 1, len(ep), meta["parent_epochs"], meta["parent"])
        self.saved = False
        self.fig = plt.figure(figsize=(14, 6.5))
        self.fig.canvas.manager.set_window_title("PREC history - %s" % name)
        axes = [self.fig.add_axes([0.05 + i * 0.32, 0.12, 0.27, 0.74]) for i in range(3)]
        if meta.get("parent"):                   # retrained: shade the epochs of the model it started from
            for ax in axes:
                ax.axvspan(0.5, meta["parent_epochs"] + 0.5, color="gray", alpha=0.12, lw=0)
        for ax, keys, label in ((axes[0], ("loss", "val_loss"), "loss (%s)" % meta.get("loss", "mse")),
                                (axes[1], (metric, "val_" + metric), "accuracy" if clf else "MAE (m/s)")):
            for k, style in zip(keys, ("-o", "-s")):
                if h.get(k):
                    ax.plot(ep, h[k], style, ms=3, label=("training " if not k.startswith("val") else "validation ") + k)
            ax.axvline(kept, color="green", ls="--", lw=1)
            ax.set_xlabel("epoch")
            ax.set_ylabel(label)
            ax.legend(fontsize=8)
            ax.grid(alpha=0.3)
        lr = h.get("learning_rate")
        if lr:
            axes[2].plot(ep, lr, "-o", ms=3, color="#9467bd")
            axes[2].set_yscale("log")
        axes[2].axvline(kept, color="green", ls="--", lw=1)
        axes[2].set_xlabel("epoch")
        axes[2].set_ylabel("learning rate")
        axes[2].grid(alpha=0.3)
        vals = "  ".join("%s %.4g" % (k, h[k][kept - 1]) for k in ("loss", "val_loss", metric, "val_" + metric) if h.get(k))
        test = ("   test accuracy %.3f" % meta["test_accuracy"] if "test_accuracy" in meta else "") if clf else \
            ("   test MAE %.3f" % meta["test_mae"] if "test_mae" in meta else "")
        self.fig.suptitle("%s (%s): %d epochs, kept epoch %d (%s)\n%s%s" % (
            name, meta.get("arch", "?"), len(ep), kept, why, vals, test), fontsize=10)


# ---- interval editor -------------------------------------------------------

class IntervalEditor:
    """Acceleration/speed plot above a map of the .fit track. Edits either the
    pair's sub-intervals (kind "intervals") or its class intervals (kind
    "classes": run, walk, still and stretch together, `cls` the class new
    intervals get first). For classes the plot shows the whole recording:
    still and stretch intervals may lie outside the GPS span (shaded grey),
    run and walk intervals are clipped to it. Mouse on the plot: drag an interval edge to move
    it, drag on empty space to add an interval, click one to select it.
    Classes: the coloured buttons (or 1-4) choose the class of new
    intervals and change the selected one; the classes never overlap, a new
    or moved interval takes its time from the others. Keys: x split the
    selected interval at the mouse, d delete it, ctrl+s save, q close. The
    map shows the whole track, the intervals in colour and the position
    under the mouse."""

    EDGE_PX = 7

    def __init__(self, plt, conf, pair, kind, cls=None):
        from matplotlib.widgets import Button
        self.plt, self.conf, self.pair, self.kind = plt, conf, pair, kind
        arr = M.load_cooked(conf, pair)
        if arr is None or not len(arr):
            raise SystemExit("pair %s has no cooked array; build it first" % pair["name"])
        self.fit = M.load_fit(conf, pair)
        glo, ghi = float(arr[0, 0]), float(arr[-1, 0])          # the GPS span
        t, xyz = arr[:, 0], arr[:, 1:4]
        if kind == "classes":                                   # the whole recording
            try:
                acc = M.load_acc(conf, pair)
                t, xyz = acc["utc"] + pair["offset"] - self.fit["start"], acc["acc"]
            except Exception as ex:
                print("whole recording not readable (%s): showing the GPS span only" % ex)
        self.lo, self.hi = min(float(t[0]), glo), max(float(t[-1]), ghi)
        # run and walk need speed: only inside the GPS span; still and stretch anywhere
        self.limits = {k: (self.lo, self.hi) if k in ("still", "stretch") else (glo, ghi)
                       for k in ("intervals",) + M.CLASSES}
        self.kinds = M.CLASSES if kind == "classes" else (kind,)
        self.cls = cls if cls in self.kinds else self.kinds[0]      # class of new intervals
        self.load(pair)
        self.colors = {k: conf["plot"]["interval" if k == "intervals" else k] for k in self.kinds}
        self.sel = None
        self.picked = False       # sel was clicked (not just drawn or moved): the class buttons change it
        self.drag = None          # (index, 0 start / 1 end) or ("new", t0)
        self.mouse_t = None
        self.dirty = False
        self.saved = False
        self.fig = plt.figure(figsize=(14, 9.5))
        self.fig.canvas.manager.set_window_title("PREC %s - %s" % (kind, pair["name"]))
        self.ax = self.fig.add_axes([0.06, 0.58, 0.88, 0.36])
        k = int(max(1, conf["plot"]["decimate"]))
        _, self.ax2, _ = plot_series(self.ax, conf, t, xyz, arr[::k, 0], arr[::k, 4])
        self.ax.set_xlim(self.lo, self.hi)
        for a, b in ((self.lo, glo), (ghi, self.hi)):          # no GPS: still and stretch only
            if b - a > 0:
                self.ax.axvspan(a, b, color="#888888", alpha=0.18, lw=0, zorder=0)
            if b - a > 0.05 * (self.hi - self.lo):
                self.ax.text((a + b) / 2, 0.02, "no GPS:\nstill, stretch", transform=self.ax.get_xaxis_transform(),
                             ha="center", va="bottom", fontsize=8, color="#444", zorder=5)
        if kind not in ("intervals", "classes"):
            class_bands(self.ax, conf, pair, skip=kind, alpha=0.5)
        self.cursor = self.ax.axvline(self.lo, color="k", lw=0.8, ls=":", alpha=0)
        # map
        self.mx, self.my = merc(self.fit["lat"], self.fit["lon"])
        self.map_ax = self.fig.add_axes([0.06, 0.14, 0.88, 0.39])
        self.maplayer = MapLayer(self.map_ax, conf)
        self.map_ax.plot(self.mx, self.my, color=conf["plot"]["track"], lw=1.5, alpha=0.8, zorder=2)
        self.marker = self.map_ax.plot([], [], "o", ms=9, mfc="white", mec="black", mew=2, zorder=6)[0]
        self.maplayer.fit(self.mx, self.my)
        self.span_arts, self.map_arts = [], []
        self.buttons = {}
        if kind == "classes":
            for i, c in enumerate(self.kinds):
                b = Button(self.fig.add_axes([0.06 + i * 0.09, 0.07, 0.08, 0.045]), "%d %s" % (i + 1, c.capitalize()),
                           color=self.colors[c], hovercolor=self.colors[c])
                b.on_clicked(lambda e, c=c: self.choose(c))
                self.buttons[c] = b
        self.b_save = Button(self.fig.add_axes([0.64, 0.07, 0.09, 0.045]), "Save")
        self.b_save.on_clicked(lambda e: self.save())
        self.b_revert = Button(self.fig.add_axes([0.74, 0.07, 0.09, 0.045]), "Revert")
        self.b_revert.on_clicked(lambda e: self.revert())
        self.b_close = Button(self.fig.add_axes([0.84, 0.07, 0.1, 0.045]), "Close")
        self.b_close.on_clicked(lambda e: self.close())
        self.status = self.fig.text(0.06, 0.035, "", fontsize=9)
        self.fig.text(0.06, 0.01, ("1-4 or a colour button: class of new intervals, and of a clicked interval   " if kind == "classes" else "")
                      + "drag an edge: move   drag on empty space: new   click: select   x: split at mouse   "
                      "d: delete   ctrl+s: save   q: close   map click: show that time", fontsize=8, color="#555")
        for ev, fn in (("button_press_event", self.press), ("motion_notify_event", self.motion),
                       ("button_release_event", self.release), ("key_press_event", self.key),
                       ("close_event", self.closing)):
            self.fig.canvas.mpl_connect(ev, fn)
        self.redraw()

    # -- helpers --
    def load(self, pair):
        """self.iv: [start, end, kind] of every interval being edited, by start."""
        self.iv = sorted([float(a), float(b), k] for k in self.kinds for a, b in pair.get(k, []))

    def busy_toolbar(self):
        tb = getattr(self.fig.canvas, "toolbar", None)
        return bool(tb and tb.mode)

    def edge_at(self, e):
        """(index, side) of an interval edge within EDGE_PX pixels of the event."""
        best = None
        for i, (a, b, _) in enumerate(self.iv):
            for side, t in ((0, a), (1, b)):
                px = self.ax.transData.transform((t, 0))[0]
                d = abs(px - e.x)
                if d <= self.EDGE_PX and (best is None or d < best[0]):
                    best = (d, i, side)
        return best[1:] if best else None

    def inside(self, t, kind=None):
        for i, (a, b, k) in enumerate(self.iv):
            if a <= t <= b and kind in (None, k):
                return i
        return None

    def position(self, t):
        """Mercator position of the track at time t (None outside / no GPS)."""
        ft = self.fit["t"]
        if not len(ft) or t < ft[0] or t > ft[-1]:
            return None
        ok = ~np.isnan(self.mx)
        if not ok.any():
            return None
        return np.interp(t, ft[ok], self.mx[ok]), np.interp(t, ft[ok], self.my[ok])

    def redraw(self):
        for a in self.span_arts + self.map_arts:
            a.remove()
        self.span_arts, self.map_arts = [], []
        for i, (a, b, k) in enumerate(self.iv):
            selected, color = i == self.sel, self.colors[k]
            self.span_arts.append(self.ax.axvspan(a, b, color=color, alpha=0.32 if selected else 0.15,
                                                  lw=0, zorder=0))
            for t in (a, b):
                self.span_arts.append(self.ax.axvline(t, color=color, lw=2.2 if selected else 1.2, zorder=4))
            m = (self.fit["t"] >= a) & (self.fit["t"] <= b)
            self.map_arts += self.map_ax.plot(self.mx[m], self.my[m], color=color,
                                              lw=5 if selected else 3.5, alpha=0.9, zorder=4)
        import matplotlib.colors as mcolors
        for c, b in self.buttons.items():           # the active class: full colour, bold, framed
            active = c == self.cls
            b.ax.set_alpha(1)
            b.color = b.hovercolor = self.colors[c] if active else mcolors.to_hex(
                0.45 * np.array(mcolors.to_rgb(self.colors[c])) + 0.55)
            b.ax.set_facecolor(b.color)
            b.label.set_fontweight("bold" if active else "normal")
            r, g, bl = mcolors.to_rgb(b.color)
            b.label.set_color("white" if 0.3 * r + 0.59 * g + 0.11 * bl < 0.6 else "black")
            for s in b.ax.spines.values():
                s.set_linewidth(3 if active else 0.5)
        title = {"intervals": "sub-intervals", "classes": "class intervals"}.get(self.kind, self.kind + " intervals")
        counts = ", ".join("%s %d" % (k, sum(x[2] == k for x in self.iv)) for k in self.kinds) \
            if self.kind == "classes" else "%d" % len(self.iv)
        self.ax.set_title("%s  ·  %s%s  ·  %s" % (self.pair["name"], title, " (unsaved changes)" if self.dirty else "",
                                                 counts))
        if self.sel is not None and self.sel < len(self.iv):
            a, b, k = self.iv[self.sel]
            self.status.set_text("selected %s%d: %s – %s  (%.1f s)" % (
                k + " " if self.kind == "classes" else "", self.sel + 1, fmt_t(a), fmt_t(b), b - a))
        else:
            total = sum(b - a for a, b, _ in self.iv)
            self.status.set_text("%d intervals, %.0f s in total%s" % (
                len(self.iv), total, "   ·   new intervals: " + self.cls if self.kind == "classes" else ""))
        self.fig.canvas.draw_idle()

    def changed(self, keep=None, claim=None):
        """Normalise after an edit, keeping the interval that contains time keep
        selected. claim = (a, b, kind): that span is cut out of the other kinds."""
        if claim:
            a, b, kind = claim
            a, b = max(a, self.limits[kind][0]), min(b, self.limits[kind][1])
            iv = []
            for x0, x1, k in self.iv:
                if k == kind or x1 <= a or x0 >= b:
                    iv.append([x0, x1, k])
                    continue
                if x0 < a:
                    iv.append([x0, a, k])
                if x1 > b:
                    iv.append([b, x1, k])
            self.iv = iv
        self.iv = sorted([a, b, k] for k in self.kinds
                         for a, b in M.normalize_intervals([x[:2] for x in self.iv if x[2] == k], *self.limits[k]))
        self.sel = self.inside(keep, claim[2] if claim else None) if keep is not None else None
        self.dirty = True
        self.redraw()

    def choose(self, c):
        """Class of new intervals; also the class of an interval selected by a click."""
        self.cls = c
        if self.sel is not None and self.picked and self.iv[self.sel][2] != c:
            a, b, _ = self.iv[self.sel]
            self.iv[self.sel][2] = c
            self.changed(keep=(a + b) / 2, claim=(a, b, c))
        else:
            self.redraw()

    # -- events --
    def press(self, e):
        if e.button == 1 and e.inaxes is self.map_ax and not self.busy_toolbar() and e.xdata is not None:
            d = np.hypot(self.mx - e.xdata, self.my - e.ydata)
            if np.isfinite(d).any():
                t = float(self.fit["t"][np.nanargmin(d)])
                self.mouse_t = t
                self.cursor.set_xdata([t, t])
                self.cursor.set_alpha(1)
                self.marker.set_data([self.mx[np.nanargmin(d)]], [self.my[np.nanargmin(d)]])
                self.fig.canvas.draw_idle()
            return
        if e.button != 1 or e.inaxes not in (self.ax, self.ax2) or self.busy_toolbar():
            return
        t = self.ax.transData.inverted().transform((e.x, e.y))[0]
        edge = self.edge_at(e)
        self.picked = False
        if edge:
            self.drag = edge
            self.sel = edge[0]
        elif self.inside(t) is not None:
            self.sel = self.inside(t)
            self.picked = True
            self.drag = None
        else:
            self.drag = ("new", t)
            self.sel = None
        self.redraw()

    def motion(self, e):
        if e.inaxes not in (self.ax, self.ax2) or e.x is None:
            return
        t = self.ax.transData.inverted().transform((e.x, e.y))[0]
        self.mouse_t = t
        pos = self.position(t)
        self.marker.set_data(([pos[0]], [pos[1]]) if pos else ([], []))
        if self.drag and self.drag[0] != "new":
            i, side = self.drag
            self.iv[i][side] = float(np.clip(t, self.lo, self.hi))
            self.redraw()
        elif self.drag:
            t0 = self.drag[1]
            if hasattr(self, "_rubber"):
                self._rubber.remove()
            self._rubber = self.ax.axvspan(min(t0, t), max(t0, t), color=self.colors[self.cls], alpha=0.25, lw=0)
            self.fig.canvas.draw_idle()
        else:
            self.fig.canvas.draw_idle()

    def release(self, e):
        if not self.drag:
            return
        if hasattr(self, "_rubber"):
            self._rubber.remove()
            del self._rubber
        if self.drag[0] == "new":
            t0 = self.drag[1]
            t1 = self.mouse_t if self.mouse_t is not None else t0
            self.drag = None
            if abs(t1 - t0) * 1.0 >= max(0.5, (self.hi - self.lo) / 2000):
                a, b = min(t0, t1), max(t0, t1)
                self.iv.append([a, b, self.cls])
                self.changed(keep=(a + b) / 2, claim=(a, b, self.cls))
            else:
                self.redraw()
            return
        i, side = self.drag
        self.drag = None
        a, b = sorted(self.iv[i][:2])
        k = self.iv[i][2]
        self.iv[i] = [a, b, k]
        self.changed(keep=(a + b) / 2, claim=(a, b, k))

    def key(self, e):
        if e.key in ("x", "s") and self.sel is not None and self.mouse_t is not None:
            a, b, k = self.iv[self.sel]
            t = self.mouse_t
            if a < t < b:
                self.iv[self.sel:self.sel + 1] = [[a, t, k], [t, b, k]]
                self.dirty = True
                self.sel = self.sel + 1
                self.redraw()
        elif e.key in ("d", "delete", "backspace") and self.sel is not None:
            del self.iv[self.sel]
            self.sel = None
            self.dirty = True
            self.redraw()
        elif self.kind == "classes" and e.key in ("1", "2", "3", "4"):
            self.choose(self.kinds[int(e.key) - 1])
        elif e.key in ("ctrl+s", "w"):
            self.save()
        elif e.key == "escape":
            self.sel = None
            self.redraw()
        elif e.key == "q":
            self.close()

    def save(self):
        p = M.load_pair(self.conf, self.pair["name"])
        for k in self.kinds:
            p[k] = M.normalize_intervals([x[:2] for x in self.iv if x[2] == k], *self.limits[k])
        if self.kind == "intervals":
            p["intervals_auto"] = False
        M.save_pair(self.conf, p)
        self.pair = p
        self.load(p)
        self.sel = None
        self.dirty = False
        self.saved = True
        print("saved " + ", ".join("%d %s" % (len(p[k]), k) for k in self.kinds))
        self.redraw()

    def revert(self):
        self.load(M.load_pair(self.conf, self.pair["name"]))
        self.sel = None
        self.dirty = False
        self.redraw()

    def close(self):
        self.plt.close(self.fig)

    def closing(self, e):
        if self.dirty:
            print("closed with unsaved changes (discarded)")


# ---- avoid rectangles ------------------------------------------------------

def track_points(conf, pairs):
    """[(name, x, y)] Mercator tracks of the given pairs' .fit files."""
    out = []
    for p in pairs:
        try:
            f = M.load_fit(conf, p)
        except Exception:
            continue
        x, y = merc(f["lat"], f["lon"])
        out.append((p["name"], x, y))
    return out


def rect_merc(r):
    x0, y0 = merc(r["south"], r["west"])
    x1, y1 = merc(r["north"], r["east"])
    return float(x0), float(x1), float(y0), float(y1)


class AvoidWindow:
    """Map with the tracks of the chosen pairs and the existing rectangles.
    Drag a rectangle, name it and press Save; repeat for more. With edit=NAME
    one existing rectangle can be moved, resized or deleted instead."""

    def __init__(self, plt, conf, pairs, edit=None):
        from matplotlib.widgets import Button, RectangleSelector, TextBox
        from matplotlib.patches import Rectangle
        self.plt, self.conf = plt, conf
        self.saved = False
        self.edit = edit
        self.rects = M.list_avoid(conf)
        self.fig = plt.figure(figsize=(12, 9.5))
        self.fig.canvas.manager.set_window_title("PREC avoid" + (" - " + edit if edit else ""))
        self.ax = self.fig.add_axes([0.03, 0.12, 0.94, 0.82])
        self.layer = MapLayer(self.ax, conf)
        xs, ys = [], []
        for name, x, y in track_points(conf, pairs):
            self.ax.plot(x, y, lw=1.5, alpha=0.8, color=conf["plot"]["track"], zorder=2)
            xs.append(x)
            ys.append(y)
        target = None
        for r in self.rects:
            x0, x1, y0, y1 = rect_merc(r)
            if r["name"] == edit:
                target = (x0, x1, y0, y1)
                continue
            self.ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fc="red", ec="darkred", alpha=0.25, zorder=3))
            self.ax.text(x0, y1, " " + r["name"], fontsize=8, color="darkred", va="bottom", zorder=4)
            xs.append(np.array([x0, x1]))
            ys.append(np.array([y0, y1]))
        if target:
            cx = np.array(target[:2])
            cy = np.array(target[2:])
            w, h = target[1] - target[0], target[3] - target[2]
            self.layer.fit(np.r_[cx - 2 * w, cx + 2 * w], np.r_[cy - 2 * h, cy + 2 * h], pad=0.05)
        elif xs:
            self.layer.fit(np.concatenate(xs), np.concatenate(ys))
        else:
            self.layer.fit(*merc(np.array([48.1, 48.3]), np.array([-0.5, -0.3])))
        self.extents = target
        self.selector = RectangleSelector(self.ax, self.selected, useblit=False, interactive=True,
                                          button=[1], minspanx=5, minspany=5, spancoords="pixels",
                                          props=dict(facecolor="orange", edgecolor="red", alpha=0.3, fill=True))
        if target:
            self.selector.extents = target
            self.selector.set_visible(True)
        self.fig.text(0.03, 0.065, "name:", fontsize=10)
        self.name = TextBox(self.fig.add_axes([0.08, 0.05, 0.18, 0.045]), "",
                            initial=edit or M.new_avoid_name(conf))
        self.b_save = Button(self.fig.add_axes([0.28, 0.05, 0.1, 0.045]), "Save")
        self.b_save.on_clicked(lambda e: self.save())
        if edit:
            self.b_del = Button(self.fig.add_axes([0.39, 0.05, 0.1, 0.045]), "Delete")
            self.b_del.on_clicked(lambda e: self.delete())
        self.b_close = Button(self.fig.add_axes([0.85, 0.05, 0.12, 0.045]), "Close")
        self.b_close.on_clicked(lambda e: plt.close(self.fig))
        self.status = self.fig.text(0.03, 0.015, "", fontsize=9)
        self.show_status("Drag a rectangle over the area to leave out (zoom/pan with the toolbar), "
                         "then Save." if not edit else "Drag the rectangle or its handles, then Save; or Delete.")
        self.ax.set_title("Avoid rectangles: the time spent inside them is left out of new pairs' intervals")

    def show_status(self, text):
        self.status.set_text(text)
        self.fig.canvas.draw_idle()

    def selected(self, eclick, erelease):
        self.extents = self.selector.extents

    def save(self):
        if not self.extents:
            return self.show_status("Draw a rectangle first.")
        x0, x1, y0, y1 = self.extents
        (s, w), (n, e) = unmerc(x0, y0), unmerc(x1, y1)
        name = "".join(ch for ch in self.name.text.strip() if ch.isalnum() or ch in "-_.") or M.new_avoid_name(self.conf)
        if self.edit and name != self.edit:
            M.delete_avoid(self.conf, self.edit)
            self.edit = name
        M.save_avoid(self.conf, {"name": name, "south": float(s), "north": float(n), "west": float(w),
                                 "east": float(e), "saved": M.iso(time.time())})
        self.saved = True
        print("saved rectangle %s" % name)
        from matplotlib.patches import Rectangle
        if not self.edit:
            self.ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fc="red", ec="darkred", alpha=0.25, zorder=3))
            self.ax.text(x0, y1, " " + name, fontsize=8, color="darkred", va="bottom", zorder=4)
            self.name.set_val(M.new_avoid_name(self.conf))
            self.selector.set_visible(False)
            self.extents = None
        self.show_status("Saved %s (%.5f..%.5f N, %.5f..%.5f E)." % (name, s, n, w, e))

    def delete(self):
        M.delete_avoid(self.conf, self.edit)
        self.saved = True
        print("deleted rectangle %s" % self.edit)
        self.plt.close(self.fig)


# ---- main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf")
    ap.add_argument("--png", help="draw into this PNG file and exit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("offset", "plot", "intervals"):
        sub.add_parser(c).add_argument("name")
    cl = sub.add_parser("classes")
    cl.add_argument("name")
    cl.add_argument("cls", nargs="?", choices=M.CLASSES, help="class of new intervals at first (default run)")
    av = sub.add_parser("avoid")
    av.add_argument("--pair", action="append", default=[], help="show this pair's track (repeatable; default: all)")
    sub.add_parser("history").add_argument("model")
    pd_ = sub.add_parser("predict")
    pd_.add_argument("model")
    pd_.add_argument("name")
    pd_.add_argument("--shift", type=int, help="samples between windows (default: at most 20000 windows)")
    pd_.add_argument("--lowpass", type=float, action="append", default=[],
                     help="also plot the prediction through a low-pass with this time constant (s); repeatable")
    ae = sub.add_parser("avoid-edit")
    ae.add_argument("rect")
    a = ap.parse_args()
    conf = M.load_conf(a.conf)
    plt = setup_matplotlib(a.png)
    try:
        if a.cmd in ("avoid", "avoid-edit"):
            pairs = M.list_pairs(conf)
            if a.cmd == "avoid" and a.pair:
                pairs = [p for p in pairs if p["name"] in a.pair]
            win = AvoidWindow(plt, conf, pairs, edit=a.rect if a.cmd == "avoid-edit" else None)
        elif a.cmd == "history":
            win = HistoryWindow(plt, conf, a.model)
        else:
            pair = M.load_pair(conf, a.name)
            if a.cmd == "offset":
                win = OffsetWindow(plt, conf, pair)
            elif a.cmd == "plot":
                win = PlotWindow(plt, conf, pair)
            elif a.cmd == "predict":
                import json
                meta = json.load(open(os.path.join(os.path.expanduser(conf["paths"]["models"]), a.model + ".json")))
                win = (ClassifyWindow if meta.get("kind") == "classifier" else PredictWindow)(
                    plt, conf, a.model, a.name, a.shift, a.lowpass)
            elif a.cmd == "intervals":
                win = IntervalEditor(plt, conf, pair, "intervals")
            else:
                win = IntervalEditor(plt, conf, pair, "classes", a.cls)
    except SystemExit:
        raise
    except Exception as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(2)
    if a.png:
        win.fig.savefig(a.png, dpi=80)
        print("wrote", a.png)
        return 0
    if os.environ.get("PREC_GUI_AUTOCLOSE"):        # tests: close by itself after N seconds
        t = win.fig.canvas.new_timer(interval=int(float(os.environ["PREC_GUI_AUTOCLOSE"]) * 1000))
        t.add_callback(lambda: plt.close(win.fig))
        t.start()
    plt.show()
    return 0 if win.saved else 1


if __name__ == "__main__":
    sys.exit(main())

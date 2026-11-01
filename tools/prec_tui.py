#!/usr/bin/env python3
"""prec_tui.py - menu driven terminal UI for the PREC feather logger.

usage: prec_tui.py [--config FILE] [--prec-conf FILE] [--theme NAME] [--usb [PORT] | --ble [ADDRESS]]
                   [--data-dir DIR] [--connect] [--write-config]

Tabs: F1 home, F2 feather (connection, sync, logging), F3 bangle (watch apps,
models, recordings), F4 data (pairs of recordings and .fit files: offsets,
intervals, plots, maps), F5 train (models). Tab cycles.
Keys: Up/Down or j/k move, Enter selects, the number/letter in front of an
item selects it directly, PgUp/PgDn scroll the output, Esc goes back, q quits.
Configuration: ~/.config/prec/tui.toml (looks, connection; --write-config)
and ~/.config/prec/prec.conf (folders and data settings). See docs/TUI.md
and docs/MERGE.md.
"""
import argparse, curses, json, locale, math, os, re, subprocess, sys, textwrap, time, tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prec_link import Feather, PrecError   # noqa: E402
import prec_merge as M                      # noqa: E402
import prec_train as T                      # noqa: E402

GUI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prec_gui.py")

CONFIG_PATH = os.path.expanduser("~/.config/prec/tui.toml")
STATE_PATH = os.path.expanduser("~/.config/prec/state.json")

DEFAULT_CONFIG = '''# PREC TUI configuration (TOML). Delete a line to get the default.

# Colour theme: "default", "light", "green" or "mono".
theme = "default"

[connection]
transport = "auto"        # "auto" (USB if plugged in, else Bluetooth), "usb" or "ble"
serial_port = ""          # empty: first /dev/ttyACM*
ble_address = ""          # empty: the feather used last time, else the strongest PREC-xxxx
password = ""             # empty: remembered password (state.json), else "prec"

[sync]
directory = ""            # empty: the data folder of prec.conf (~/prec/data)

# Override single elements of the theme. A style is
#   [bold] [dim] [reverse] [underline] [italic] FOREGROUND [on BACKGROUND]
# with colours black, red, green, yellow, blue, magenta, cyan, white,
# default (the terminal's own colour) or a number 0-255.
# Elements: title, menu, key, selected, disabled, text, heading, label,
#           ok, error, warn, event, status, progress, border, heart,
#           role_train, role_valid, role_test, role_quant (F5 roles list)
[colors]
# selected = "bold black on yellow"
# heading = "bold magenta"
'''

THEMES = {
    "default": {
        "title": "bold black on cyan", "menu": "default", "key": "cyan", "selected": "bold black on yellow",
        "disabled": "dim default", "text": "default", "heading": "bold cyan", "label": "cyan",
        "ok": "bold green", "error": "bold red", "warn": "yellow", "event": "bold magenta",
        "status": "black on white", "progress": "cyan", "border": "cyan", "heart": "bold red",
        "role_train": "bold green", "role_valid": "bold 214", "role_test": "bold red", "role_quant": "bold 135"},
    "light": {
        "title": "bold white on blue", "menu": "black", "key": "blue", "selected": "bold white on blue",
        "disabled": "dim black", "text": "black", "heading": "bold blue", "label": "blue",
        "ok": "green", "error": "bold red", "warn": "magenta", "event": "bold magenta",
        "status": "white on black", "progress": "blue", "border": "blue", "heart": "bold red",
        "role_train": "bold green", "role_valid": "bold 214", "role_test": "bold red", "role_quant": "bold 135"},
    "green": {
        "title": "bold black on green", "menu": "green", "key": "bold green", "selected": "bold black on green",
        "disabled": "dim green", "text": "green", "heading": "bold green", "label": "green",
        "ok": "bold green", "error": "bold red", "warn": "yellow", "event": "bold yellow",
        "status": "black on green", "progress": "green", "border": "green", "heart": "bold red",
        "role_train": "bold green", "role_valid": "bold 214", "role_test": "bold red", "role_quant": "bold 135"},
    "mono": {
        "title": "reverse", "menu": "default", "key": "bold", "selected": "reverse", "disabled": "dim",
        "text": "default", "heading": "bold underline", "label": "bold", "ok": "bold", "error": "bold",
        "warn": "bold", "event": "bold", "status": "reverse", "progress": "reverse", "border": "default",
        "heart": "bold", "role_train": "bold", "role_valid": "underline", "role_test": "reverse", "role_quant": "dim"},
}

COLOR_NAMES = {"black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5,
               "cyan": 6, "white": 7, "default": -1}
ATTRS = {"bold": curses.A_BOLD, "dim": curses.A_DIM, "reverse": curses.A_REVERSE,
         "underline": curses.A_UNDERLINE, "italic": getattr(curses, "A_ITALIC", 0)}

FALLBACK_OPTIONS = {   # used when the Polar cannot be asked (QUERY failed)
    "acc": {"rate": [25, 50, 100, 200], "res": [16], "range": [2, 4, 8]},
    "ecg": {"rate": [130], "res": [14]},
    "gyro": {"rate": [26, 52, 104, 208, 416, 833, 1666], "res": [16], "range": [250, 500, 1000, 2000]},
    "mag": {"rate": [10, 20, 50, 100], "res": [16], "range": [50]},
}
HOTKEYS = {1: "rfbdtwq", 2: "1234567890mcq", 3: "symiaq", 4: "unopikaelcxq", 5: "rphatlmdfq"}   # menu shortcuts per tab
TABS = {1: "Home", 2: "Feather", 3: "Bangle", 4: "Data", 5: "Train"}
FALLBACK_8 = {214: 3, 208: 3, 220: 3, 135: 5, 129: 5, 141: 5}    # 256-colour amber/purple on 8-colour terminals
ROLE_KEYS = {"t": "train", "v": "valid", "e": "test", "q": "quant"}
ROLE_LETTER = {"train": "T", "valid": "V", "test": "E", "quant": "Q"}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")       # colour codes in Keras' progress bar
KERAS_BAR = re.compile(r"^\s*(\d+)/(\d+) ━+(.*)$")
TRAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prec_train.py")

# F5 parameter editor: (key, label, choices or None, help)
TRAIN_PARAMS = [
    ("arch", "Architecture", "arch", "model architecture: built-in or ~/prec/arch/<name>.py"),
    ("class_arch", "Classifier arch", "class_arch", "classifier architecture: built-in or ~/prec/class/<name>.py"),
    ("window", "Window", None, "samples per window"),
    ("shift", "Shift", None, "samples between the starts of consecutive windows"),
    ("stride", "Stride", [1, 2, 4, 8], "take every n-th sample (rate/stride must be a Polar rate)"),
    ("target", "Label", ["mean", "last", "center"], "speed of a window: mean over it, last or centre sample"),
    ("dft", "DFT coefficients", None, "0: raw samples; n: first n DFT coefficients per axis"),
    ("dft_abs", "DFT absolute", [True, False], "magnitudes of the DFT coefficients (else real and imaginary)"),
    ("window_fn", "Window function", ["hann", "hamming", "blackman", "boxcar"], "applied before the DFT"),
    ("min_speed", "Min speed (m/s)", None, "training windows slower than this are dropped"),
    ("max_speed", "Max speed (m/s)", None, "training windows faster than this are dropped"),
    ("resolution", "Resolution (m/s)", None, "bin width for the strategy and the histogram"),
    ("strategy", "Strategy", ["weight", "pick"], "weight: equal total weight per bin; pick: equal count per bin"),
    ("pick_per_bin", "Pick per bin", None, "pick: windows per bin (0 = as many as the smallest bin)"),
    ("batch.train", "Batch train", None, "batch size of the training data"),
    ("batch.valid", "Batch valid", None, "batch size of the validation data"),
    ("batch.test", "Batch test", None, "batch size of the test data"),
    ("batch.quant", "Batch quant", None, "batch size of the quantization data"),
    ("epochs", "Epochs", None, "training epochs"),
    ("learning_rate", "Learning rate", None, "Adam learning rate (drops 5x on plateaus)"),
    ("loss", "Loss", ["mse", "mae", "huber"], "training loss"),
    ("lr_patience", "LR patience", None, "epochs without improvement before the learning rate drops"),
    ("early_stop", "Early stop", None, "epochs without improvement before stopping (0 = never)"),
    ("norm_samples", "Norm samples", None, "training windows used for mean and standard deviation"),
    ("quantize", "Quantize (int8)", [False, True], "int8 model calibrated with the quant pairs"),
    ("quant_samples", "Quant samples", None, "windows used for the int8 calibration"),
    ("rate", "Recording rate (Hz)", [200, 100, 50, 25], "accelerometer rate of the recordings"),
    ("range", "Polar range (G)", [8, 4, 2], "accelerometer range the watch requests"),
    ("arena", "Arena (bytes)", None, "TensorFlow memory on the watch (0 = estimate)"),
]
CLASS_LETTER = {"run": "R", "walk": "W", "still": "S", "stretch": "T"}
PAGE_KEYS = ("heading", "out", "scroll", "sel")

# 3x5 block digits for the heart rate monitor; each cell is drawn two characters wide
BIG_DIGITS = {
    "0": ["###", "# #", "# #", "# #", "###"], "1": [" # ", "## ", " # ", " # ", "###"],
    "2": ["###", "  #", "###", "#  ", "###"], "3": ["###", "  #", "###", "  #", "###"],
    "4": ["# #", "# #", "###", "  #", "  #"], "5": ["###", "#  ", "###", "  #", "###"],
    "6": ["###", "#  ", "###", "# #", "###"], "7": ["###", "  #", "  #", "  #", "  #"],
    "8": ["###", "# #", "###", "# #", "###"], "9": ["###", "# #", "###", "  #", "###"],
    "-": ["   ", "   ", "###", "   ", "   "],
}
HEART = {   # small and large (on a beat) red heart, 5 rows like the digits
    False: ["  ▄▄ ▄▄  ", " ███████ ", "  █████  ", "   ███   ", "    █    "],
    True: [" ▄██▄██▄ ", "█████████", " ███████ ", "  █████  ", "    █    "],
}


def big_text(text):
    """Rows of block characters for text (digits and "-")."""
    rows = [""] * 5
    for ch in text:
        for r in range(5):
            rows[r] += BIG_DIGITS[ch][r].replace("#", "██").replace(" ", "  ") + "  "
    return rows


UNITS = {"rate": "Hz", "res": "bit", "range": {"acc": "G", "gyro": "dps", "mag": "G"}, "ch": "ch"}
SETTING_HELP = {
    "sleep": ("Sleep after (s)", "seconds without motion or connection before sleeping, 0 = never"),
    "reconnect": ("Reconnect for (s)", "how long to try to get a lost Polar back"),
    "flush": ("Flush every (s)", "longest time logged data stays in RAM"),
    "lowbattery": ("Low battery (mV)", "stop logging below this voltage, 0 = never"),
    "predict": ("Predict every (ms)", "run the built-in speed model this often while logging, 0 = never"),
    "clslp": ("Class filter (s)", "low-pass time constant of the classifier's probabilities, 0 = off"),
    "speedlp": ("Speed filter (s)", "low-pass time constant of the predicted speed, 0 = off"),
    "loghr": ("Log heart rate", "1 = write HEART_RATE words while logging, 0 = off"),
    "logcad": ("Log cadence", "1 = write a CADENCE word every second while logging, 0 = off"),
    "cadaxis": ("Cadence axis", "accelerometer axis pointing up and down: 0 x, 1 y, 2 z"),
    "cadlp": ("Cadence filter (s)", "low-pass time constant of the cadence, 0 = off"),
}


# ---- configuration --------------------------------------------------------

def load_config(path, theme=None):
    """Read the TOML config (missing file: defaults). Returns a dict with
    connection, sync, theme and styles (element -> style string)."""
    cfg = {}
    if os.path.exists(path):
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    conn = {"transport": "auto", "serial_port": "", "ble_address": "", "password": ""}
    conn.update(cfg.get("connection", {}))
    sync = {"directory": ""}
    sync.update(cfg.get("sync", {}))
    name = theme or cfg.get("theme", "default")
    if name not in THEMES:
        raise SystemExit("unknown theme %r (choose from %s)" % (name, ", ".join(THEMES)))
    styles = dict(THEMES[name])
    styles.update(cfg.get("colors", {}))
    return {"connection": conn, "sync": sync, "theme": name, "styles": styles}


def load_state():
    """Remembered password and BLE address (~/.config/prec/state.json)."""
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(**changes):
    s = load_state()
    s.update(changes)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(s, f)
    os.chmod(STATE_PATH, 0o600)


def parse_style(text):
    """"bold black on yellow" -> (attrs, fg, bg). Raises ValueError."""
    attrs, colors, words = 0, [], text.lower().split()
    i = 0
    while i < len(words):
        w = words[i]
        if w in ATTRS:
            attrs |= ATTRS[w]
        elif w == "on":
            colors.append(None)          # marker: next colour is the background
        elif w in COLOR_NAMES or w.isdigit():
            colors.append(COLOR_NAMES[w] if w in COLOR_NAMES else int(w))
        else:
            raise ValueError("unknown word %r in style %r" % (w, text))
        i += 1
    fg, bg = -1, -1
    if None in colors:
        k = colors.index(None)
        fg = colors[k - 1] if k > 0 else -1
        bg = colors[k + 1] if k + 1 < len(colors) else -1
    elif colors:
        fg = colors[0]
    return attrs, fg, bg


class Styles:
    """curses attributes for the theme elements."""

    def __init__(self, styles):
        self.attr = {}
        color = curses.has_colors()
        if color:
            curses.start_color()
            curses.use_default_colors()
        for n, (name, text) in enumerate(sorted(styles.items()), start=1):
            attrs, fg, bg = parse_style(text)
            if color and (fg != -1 or bg != -1) and n < curses.COLOR_PAIRS:
                fg = fg if fg < curses.COLORS else FALLBACK_8.get(fg, -1)
                bg = bg if bg < curses.COLORS else -1
                curses.init_pair(n, fg, bg)
                attrs |= curses.color_pair(n)
            self.attr[name] = attrs

    def __getitem__(self, name):
        return self.attr.get(name, 0)


# ---- helpers --------------------------------------------------------------

def keras_bar(line):
    """Keras' progress line "  349/1211 ━━━━ 2s 3ms/step - loss: ..." with the
    bar redrawn from the step counter (its colours, which showed the progress,
    are gone from the log)."""
    m = KERAS_BAR.match(line)
    if not m:
        return line
    done, total = int(m.group(1)), int(m.group(2))
    k = round(20 * done / max(total, 1))
    return "%5d/%d %s%s%s" % (done, total, "█" * k, "░" * (20 - k), m.group(3))


def wrap_log(line, width):
    """A log line cut into pieces of at most width characters, preferably at
    Keras' " - " between metrics; continuation pieces are indented."""
    if len(line) <= width or width < 20:
        return [line]
    out, cur = [], ""
    for i, part in enumerate(line.split(" - ")):
        piece = part if i == 0 else " - " + part
        if cur and len(cur) + len(piece) > width:
            out.append(cur)
            cur = "      " + piece.lstrip(" -") if i else piece
        else:
            cur += piece
        while len(cur) > width:                  # a single piece longer than the line
            out.append(cur[:width])
            cur = "      " + cur[width:]
    out.append(cur)
    return out


def tilde(path):
    """path with the home folder written as ~."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def pace(v):
    """m/s -> "m:ss" per km."""
    if not v or v < 0.5:
        return "--:--"
    s = round(1000 / v)
    return "%d:%02d" % (s // 60, s % 60)


def human(n):
    return "%.1f MB" % (n / 1048576) if n >= 1048576 else "%d kB" % round(n / 1024) if n >= 1024 else "%d B" % n


class Disconnected(Exception):
    pass


# ---- the application ------------------------------------------------------

class App:
    """The TUI: a menu on the left, output on the right, status bar below."""

    MENU_W = 34

    def __init__(self, scr, cfg):
        self.scr = scr
        self.cfg = cfg
        self.st = Styles(cfg["styles"])
        self.feather = None
        self.status = {}
        self.sel = 0
        self.heading = "Welcome"
        self.out = []            # [(text, style)]
        self.scroll = 0
        self.message = ("", "text")
        self.events = []
        self.last_status = 0
        self.running = True
        self.data_dir = os.path.expanduser(cfg["sync"]["directory"])
        curses.curs_set(0)
        scr.keypad(True)
        scr.timeout(500)
        self.say("PREC feather control", "heading")
        self.say("")
        self.say("Sync copies all finished recordings to %s." % self.data_dir)
        self.say("Logging continues on the feather when you disconnect or quit.")
        self.say("")
        self.say("Theme: %s   config: %s" % (cfg["theme"], CONFIG_PATH), "label")
        self.mconf = cfg["merge"]
        self.left = None             # (title, labels, cursor) while a list replaces the menu
        self.autoconnect = cfg["connection"].get("_autoconnect", False)
        self.pages = {2: {k: getattr(self, k) for k in PAGE_KEYS}}
        self.tab = 1
        self.home_overview()

    # -- output pane --
    def show(self, heading):
        """Start a new output page."""
        self.heading, self.out, self.scroll = heading, [], 0

    def say(self, text="", style="text"):
        self.out.append((text, style))

    def table(self, rows, style="text"):
        """rows of (label, value[, style])."""
        w = max((len(r[0]) for r in rows), default=0)
        for r in rows:
            self.out.append(([(r[0].ljust(w + 2), "label"), (str(r[1]), r[2] if len(r) > 2 else style)]))

    def wrapped(self, width):
        """Output lines with plain text lines word-wrapped to width."""
        out = []
        for line in self.out:
            if isinstance(line, list) or len(line[0]) <= width:
                out.append(line)
            else:
                out += [(t, line[1]) for t in textwrap.wrap(line[0], width, subsequent_indent="  ")]
        return out

    def note(self, text, style="text"):
        self.message = (text, style)

    # -- menu --
    def menu(self):
        """[(label, handler, enabled[, reason when disabled])] of the current tab."""
        return {1: self.home_menu, 3: self.bangle_menu, 4: self.data_menu, 5: self.train_menu}.get(self.tab, self.feather_menu)()

    def home_menu(self):
        return [
            ("Refresh overview", self.home_overview, True),
            ("Feather (F2)", lambda: self.switch_tab(2), True),
            ("Bangle (F3)", lambda: self.switch_tab(3), True),
            ("Data (F4)", lambda: self.switch_tab(4), True),
            ("Train (F5)", lambda: self.switch_tab(5), True),
            ("Write default config files", self.do_write_configs, True),
            ("Quit", self.do_quit, True),
        ]

    def data_menu(self):
        return [
            ("Update pairs", self.do_update, True),
            ("New pair (choose files)", self.do_new_pair, True),
            ("Offset", self.do_offset, True),
            ("Plot", self.do_plot, True),
            ("Intervals", self.do_intervals, True),
            ("Class intervals", self.do_classes, True),
            ("Avoid: new rectangles", self.do_avoid, True),
            ("Edit avoid rectangles", self.do_edit_avoid, True),
            ("Legacy binary import", self.do_legacy, True),
            ("Cook", self.do_cook, True),
            ("Delete pair", self.do_delete, True),
            ("Quit", self.do_quit, True),
        ]

    def feather_menu(self):
        """The F2 tab: feather commands."""
        c = self.feather is not None
        logging = self.status.get("logging")
        items = [
            ("Sync data to %s" % self.cfg["sync"]["directory"], self.do_sync, c),
            ("Status", self.do_status, c),
            ("Set feather time", self.do_time, c),
            ("List data", self.do_list, c),
            ("Scan for Polar devices", self.do_scan, c),
            ("Polar devices / default", self.do_devices, c),
            ("Data format", self.do_format, c),
            ("Stop logging" if logging else "Start logging", self.do_stop if logging else self.do_start, c),
            ("Feather password", self.do_password, c),
            ("Feather settings", self.do_settings, c),
            ("Monitor heart rate", self.do_monitor, c and self.status.get("polar") == "connected",
             "Monitor needs the feather connected to the Polar: start logging first"),
            ("Disconnect (keep logging)" if c else "Connect", self.do_disconnect if c else self.do_connect, True),
            ("Quit", self.do_quit, True),
        ]
        return items

    def hotkeys(self):
        return HOTKEYS[self.tab]

    def hotkey(self, i):
        keys = self.hotkeys()
        return keys[i] if i < len(keys) else " "

    # -- drawing --
    def put(self, y, x, text, style="text", width=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        width = min(width if width is not None else w - x, w - x)
        if y == h - 1:
            width = min(width, w - x - 1)    # never write the bottom-right cell
        if width > 0:
            try:
                self.scr.addnstr(y, x, text, width, self.st[style])
            except curses.error:
                pass

    def put_segments(self, y, x, segs, width):
        for text, style in segs:
            if width <= 0:
                break
            self.put(y, x, text, style, width)
            x += len(text)
            width -= len(text)

    def draw(self, extra=None):
        """Redraw everything. extra(y0, x0, height, width) draws a custom right pane."""
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 10 or w < 50:
            self.put(0, 0, "Terminal too small (need 50x10)", "error")
            scr.refresh()
            return
        # title bar
        s = self.status
        if self.feather:
            parts = [" PREC ", "%s via %s" % (self.feather.name, self.feather.kind)]
            if s.get("logging"):
                parts.append("%s %s %s" % (s.get("state", "").upper(), os.path.basename(s.get("file", "")),
                                            human(s.get("bytes", 0))))
            else:
                parts.append("idle")
            if "bat" in s:
                parts.append("bat %.2f V" % (s["bat"] / 1000))
            if s.get("clock") == "unset":
                parts.append("CLOCK NOT SET")
        else:
            parts = [" PREC ", "not connected"]
        self.put(0, 0, "".ljust(w), "title")
        x = 0
        for n, name in TABS.items():
            label = " F%d %s " % (n, name)
            self.put(0, x, label, "selected" if n == self.tab else "title")
            x += len(label) + 1
        self.put(0, x + 1, " │ ".join(parts[1:]), "title", w - x - 2)
        # menu
        mw = self.MENU_W
        for y in range(1, h - 1):
            self.put(y, mw, "│", "border", 1)
        if self.left:
            title, labels, cur, styles = self.left
            self.put(1, 1, title, "heading", mw - 2)
            top = max(0, cur - (h - 4) + 1)
            for i, label in enumerate(labels[top:top + h - 3]):
                k = top + i
                style = "selected" if k == cur else (styles[k] if styles and styles[k] else "menu")
                self.put(2 + i, 1, (" " + label).ljust(mw - 2), style, mw - 2)
            items = []
        else:
            items = self.menu()
            self.sel = min(self.sel, len(items) - 1)
        for i, item in enumerate(items):
            label, enabled = item[0], item[2]
            y = 2 + i
            if y >= h - 1:
                break
            selected = i == self.sel and extra is None
            style = "selected" if selected else ("menu" if enabled else "disabled")
            if selected:
                self.put(y, 1, (" %s  %s" % (self.hotkey(i), label)).ljust(mw - 2), style, mw - 2)
            else:
                self.put(y, 1, " ", style)
                self.put(y, 2, self.hotkey(i), "key" if enabled else "disabled")
                self.put(y, 3, "  " + label, style, mw - 4)
        # right pane
        x0, pw, ph = mw + 2, w - mw - 3, h - 4
        if extra:
            extra(2, x0, ph, pw)
        else:
            self.put(1, x0, self.heading, "heading", pw)
            lines = self.wrapped(pw)[self.scroll:self.scroll + ph]
            for i, line in enumerate(lines):
                if isinstance(line, list):
                    self.put_segments(2 + i, x0, line, pw)
                else:
                    self.put(2 + i, x0, line[0], line[1], pw)
            if len(self.wrapped(pw)) > ph:
                self.put(h - 2, w - 22, " PgUp/PgDn %d/%d " % (self.scroll + 1, len(self.wrapped(pw))), "label")
        # status bar
        text, style = self.message
        if not text:
            keys = "/".join(self.hotkeys()) if self.tab != 2 else "1-0/m/c/q"
            text, style = "F1-F5 or Tab: tabs   ↑↓ Enter   %s shortcut   PgUp/PgDn scroll   q quit" % keys, "status"
        self.put(h - 1, 0, (" " + text).ljust(w), style if style != "text" else "status")
        scr.refresh()

    def busy(self, text):
        self.note("⏳ " + text, "warn")
        self.draw()

    # -- events and status --
    def on_event(self, text):
        self.events.append((time.strftime("%H:%M:%S"), text))
        self.note("Feather: " + text, "event")
        curses.beep()
        self.last_status = 0    # refresh soon

    def refresh_status(self):
        if self.feather:
            self.status = self.call(self.feather.status)
            self.last_status = time.time()

    def call(self, fn, *args, **kw):
        """Run a feather call; turn transport failures into Disconnected."""
        try:
            return fn(*args, **kw)
        except PrecError as e:
            if e.code == 0 and ("lost" in e.text or "timeout" in e.text) and self.feather and \
                    self.feather.kind == "ble" and not getattr(self.feather.t, "connected", True):
                raise Disconnected(e.text)
            raise
        except OSError as e:
            raise Disconnected(str(e))

    # -- input helpers --
    def prompt(self, label, initial="", secret=False):
        """Single line input on the status bar. Returns text, or None on Esc."""
        text = initial
        curses.curs_set(1)
        self.scr.timeout(-1)    # wait for keys (the main loop uses a 500 ms timeout)
        try:
            while True:
                self.draw()
                h, w = self.scr.getmaxyx()
                shown = "*" * len(text) if secret else text
                line = " %s: %s" % (label, shown)
                self.put(h - 1, 0, line.ljust(w), "selected")
                self.scr.move(h - 1, min(len(line), w - 2))
                ch = self.scr.get_wch() if hasattr(self.scr, "get_wch") else self.scr.getch()
                if ch in ("\n", "\r", curses.KEY_ENTER, 10, 13):
                    return text
                if ch in ("\x1b", 27):
                    return None
                if ch in (curses.KEY_BACKSPACE, "\x7f", "\b", 127, 8):
                    text = text[:-1]
                elif isinstance(ch, str) and ch.isprintable():
                    text += ch
        except curses.error:
            return None
        finally:
            curses.curs_set(0)
            self.scr.timeout(500)
            self.note("")

    def prompt_int(self, label, initial=None):
        t = self.prompt(label, "" if initial is None else str(initial))
        if t is None or not t.strip():
            return None
        try:
            return int(t)
        except ValueError:
            self.note("Not a number: %s" % t, "error")
            return None

    def confirm(self, question):
        t = self.prompt(question + " [y/N]")
        return bool(t) and t.strip().lower() in ("y", "yes")

    def pick(self, heading, rows, hint="Enter select  Esc back", keys=None):
        """Choose one of rows (list of str or segment lists). Returns the index, or
        None on Esc. keys: {key: callback(index)} for extra keys; a callback
        returning True ends the picker (returning None)."""
        if not rows:
            return None
        cur, top = 0, 0
        self.note(hint, "status")
        while True:
            def draw_list(y0, x0, ph, pw):
                nonlocal top
                self.put(y0 - 1, x0, heading, "heading", pw)
                top = min(max(top, cur - ph + 1), cur)
                for i, row in enumerate(rows[top:top + ph]):
                    sel = top + i == cur
                    text = row if isinstance(row, str) else "".join(t for t, _ in row)
                    if sel:
                        self.put(y0 + i, x0, ("▶ " + text).ljust(pw), "selected", pw)
                    elif isinstance(row, str):
                        self.put(y0 + i, x0, "  " + row, "text", pw)
                    else:
                        self.put_segments(y0 + i, x0, [("  ", "text")] + row, pw)
            self.draw(draw_list)
            ch = self.scr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                cur = max(0, cur - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                cur = min(len(rows) - 1, cur + 1)
            elif ch in (curses.KEY_HOME,):
                cur = 0
            elif ch in (curses.KEY_END,):
                cur = len(rows) - 1
            elif ch in (10, 13, curses.KEY_ENTER):
                self.note("")
                return cur
            elif ch in (27, ord("q"), curses.KEY_LEFT) and not (keys and ch in keys):
                self.note("")
                return None
            elif keys and ch in keys:
                if keys[ch](cur):
                    self.note("")
                    return None
            elif ch == curses.KEY_RESIZE:
                pass

    # -- commands --
    def do_connect(self):
        conn = self.cfg["connection"]
        state = load_state()
        password = conn["password"] or state.get("password") or "prec"
        transport = conn["transport"]
        address = conn["ble_address"] or (state.get("ble_address") if transport != "usb" else None)
        for attempt in range(3):
            self.busy("Connecting (%s)... move the feather if it is asleep" % transport)
            try:
                f = Feather.open(transport, port=conn["serial_port"] or None, address=address or None,
                                 password=password, on_event=self.on_event)
                break
            except PrecError as e:
                if e.code == 401:
                    pw = self.prompt("Wrong password - feather password", secret=True)
                    if pw is None:
                        return self.note("Not connected", "warn")
                    password = pw
                    continue
                if address and transport != "usb" and attempt == 0:
                    address = None    # remembered feather not around: scan
                    continue
                self.show("Connect")
                self.say("Could not connect: %s" % e, "error")
                self.say("")
                self.say("USB: plug the feather in.  Bluetooth: move it to wake it up.")
                return self.note("")
            except Exception as e:
                self.show("Connect")
                self.say("Could not connect: %s" % e, "error")
                return self.note("")
        else:
            return self.note("Not connected", "warn")
        self.feather = f
        if f.kind == "ble":
            save_state(password=password, ble_address=f.t.name)
        self.refresh_status()
        self.show("Connected")
        self.say("Connected to %s via %s" % (f.name, "USB " + f.t.name if f.kind == "usb" else "Bluetooth"), "ok")
        if self.status.get("logging"):
            self.say("")
            self.say("The feather is logging to %s" % os.path.basename(self.status["file"]), "warn")
        if self.status.get("clock") == "unset":
            self.say("The feather clock is not set: use 'Set feather time'.", "warn")
        self.note("")

    def do_disconnect(self):
        f, self.feather = self.feather, None
        self.busy("Disconnecting...")
        try:
            f.bye() if f.kind == "ble" else f.close()
        except Exception:
            pass
        self.show("Disconnected")
        if self.status.get("logging"):
            self.say("The feather keeps logging to %s." % os.path.basename(self.status.get("file", "")), "warn")
        self.status = {}
        self.select_connect()
        self.note("")

    def do_quit(self):
        if self.feather:
            try:
                self.feather.bye() if self.feather.kind == "ble" else self.feather.close()
            except Exception:
                pass
        self.running = False

    def do_status(self):
        self.busy("Reading status...")
        s = self.call(self.feather.status)
        self.status = s
        self.show("Status of %s" % self.feather.name)
        rows = [("State", s.get("state"), "ok" if not s["logging"] else "warn")]
        if s["logging"]:
            rows += [("File", s.get("file")), ("Written", human(s.get("bytes", 0))),
                     ("Streams", s.get("streams")), ("Packets", s.get("packets")),
                     ("Polar link", s.get("polar"), "ok" if s.get("polar") == "connected" else "error"),
                     ("Dropped words", s.get("dropped"), "error" if s.get("dropped") else "text")]
        rows += [("Battery", "%.2f V" % (s.get("bat", 0) / 1000), "warn" if s.get("bat", 4000) < 3500 else "text"),
                 ("Clock", s.get("time") + ("" if s.get("clock") == "ok" else "  (NOT SET)"),
                  "text" if s.get("clock") == "ok" else "error"),
                 ("Default Polar", s.get("default")),
                 ("SD card", s.get("sd"), "ok" if s.get("sd") == "ok" else "error"),
                 ("Uptime", "%d s" % s.get("up", 0)),
                 ("Last reset", "reason %s, step %s" % (s.get("reset"), s.get("crumb")))]
        self.table(rows)
        if self.events:
            self.say("")
            self.say("Events this session", "heading")
            for t, e in self.events[-10:]:
                self.say("%s  %s" % (t, e), "event")
        self.note("")

    def do_time(self):
        self.busy("Reading feather clock...")
        before, _ = self.call(self.feather.get_time)
        offset = before - time.time() * 1000
        ms, iso = self.call(self.feather.set_time)
        self.show("Set feather time")
        self.table([("Was off by", "%+.3f s" % (offset / 1000), "warn" if abs(offset) > 1000 else "text"),
                    ("Now", iso, "ok"), ("Source", "this computer's clock (UTC)")])
        self.refresh_status()
        self.note("")

    def local_state(self, name, size, logging_file):
        path = os.path.join(self.data_dir, name)
        if name == logging_file:
            return "logging", "warn"
        if os.path.exists(path) and os.path.getsize(path) == size:
            return "synced", "ok"
        if os.path.exists(path + ".part"):
            return "partial", "warn"
        return "not synced", "label"

    def do_list(self):
        self.busy("Listing data files...")
        files = self.call(self.feather.list_data)
        self.refresh_status()
        logging_file = os.path.basename(self.status.get("file", "-"))
        self.show("Data files on the feather (%d)" % len(files))
        total = 0
        for name, size in files:
            st, style = self.local_state(name, size, logging_file)
            self.out.append([("%-26s" % name, "text"), ("%10s  " % human(size), "label"), (st, style)])
            total += size
        self.say("")
        self.say("Total %s.  Local copies: %s" % (human(total), self.data_dir), "label")
        self.note("")

    def do_sync(self):
        self.busy("Listing data files...")
        t0, last = time.time(), [0]
        self.scr.nodelay(True)

        def progress(name, done, total, i, count):
            now = time.time()
            if now - last[0] > 0.15 or done == total:
                last[0] = now
                rate = done / max(0.001, now - t0)

                def pane(y0, x0, ph, pw):
                    self.put(y0 - 1, x0, "Syncing to %s" % self.data_dir, "heading", pw)
                    self.put(y0 + 1, x0, "File %d of %d: %s" % (i + 1, count, name), "text", pw)
                    bw = max(10, pw - 18)
                    fill = int(bw * done / max(1, total))
                    self.put(y0 + 3, x0, "█" * fill, "progress", bw)
                    self.put(y0 + 3, x0 + fill, "░" * (bw - fill), "disabled", bw - fill)
                    self.put(y0 + 3, x0 + bw + 1, "%3d%%" % (100 * done // max(1, total)), "text")
                    self.put(y0 + 4, x0, "%s of %s" % (human(done), human(total)), "label", pw)
                    self.put(y0 + 6, x0, "Esc cancels (the next sync resumes)", "label", pw)
                self.note("Syncing... %s/s" % human(rate), "warn")
                self.draw(pane)
            return self.scr.getch() == 27
        try:
            r = self.call(self.feather.sync, self.data_dir, progress)
        finally:
            self.scr.nodelay(False)
            self.scr.timeout(500)
        self.show("Sync to %s" % self.data_dir)
        if r["cancelled"]:
            self.say("Cancelled - run Sync again to resume.", "warn")
        for n in r["copied"]:
            self.say("copied      " + n, "ok")
        for n in r["busy"]:
            self.say("being logged, skipped   " + n, "warn")
        self.say("")
        self.say("%d copied, %d already up to date%s." % (
            len(r["copied"]), len(r["skipped"]), ", %d being logged" % len(r["busy"]) if r["busy"] else ""),
            "ok" if not r["cancelled"] else "warn")
        self.note("")

    def do_scan(self):
        if self.status.get("logging"):
            return self.note("Cannot scan while logging", "error")
        secs = self.prompt_int("Scan for how many seconds", 10)
        if not secs:
            return
        self.busy("Scanning for Polar devices for %d s (wear the strap so it is awake)..." % secs)
        found = self.call(self.feather.scan, secs)
        self.show("Scan result")
        if not found:
            self.say("No Polar device found. Is the strap worn (moist electrodes) or the device on?", "warn")
            return self.note("")
        rows = [[("%-24s" % name, "text"), ("%-18s" % addr, "label"), ("%4d dBm  " % rssi, "label"),
                 ("known" if known else "NEW", "text" if known else "ok")] for addr, rssi, known, name in found]
        i = self.pick("Found %d - Enter makes one the default Polar" % len(found), rows)
        if i is not None:
            self.set_default(found[i][0])
        else:
            self.show("Scan result")
            for r in rows:
                self.out.append(r)

    def set_default(self, device):
        self.busy("Saving default...")
        addr, name = self.call(self.feather.set_default, device)
        self.refresh_status()
        self.show("Default Polar")
        self.say("Logging will use %s (%s)" % (name, addr), "ok")
        self.note("")

    def do_devices(self):
        self.busy("Reading known devices...")
        devs = self.call(self.feather.devices)
        self.show("Known Polar devices")
        if not devs:
            self.say("None yet - use 'Scan for Polar devices'.", "warn")
            return self.note("")
        rows = [[("★ " if d else "  ", "ok"), ("%-24s" % name, "text"), (addr, "label")] for _, addr, d, name in devs]
        i = self.pick("Known Polar devices - Enter makes one the default", rows)
        if i is not None:
            self.set_default(devs[i][1])
        else:
            for r in rows:
                self.out.append(r)

    def do_format(self):
        self.busy("Asking the default Polar what it supports (it must be on)...")
        try:
            q = self.call(self.feather.query)
            options, features = q["settings"], q["features"]
        except PrecError as e:
            q, options, features = None, FALLBACK_OPTIONS, None
            why = "busy logging" if e.code == 409 else "Polar not reached"

        fmt = self.call(self.feather.get_format)
        types = [t for t in fmt if features is None and t in FALLBACK_OPTIONS or features and t in features]
        others = [t for t in fmt if t not in types and fmt[t]["on"]]

        def describe(t):
            f = fmt[t]
            segs = [("%-5s" % t.upper(), "text"), ("%-4s" % ("on" if f["on"] else "off"), "ok" if f["on"] else "label")]
            for k in ("rate", "res", "range"):
                if f[k]:
                    u = UNITS[k][t] if isinstance(UNITS[k], dict) else UNITS[k]
                    segs.append(("  %s %s" % (f[k], u), "text"))
            if t in others:
                segs.append(("  not supported by this Polar", "error"))
            return segs
        while True:
            rows = [describe(t) for t in types + others]
            head = "Data format" + (" - Polar supports %s" % ", ".join(features) if features is not None
                                    else " - %s, showing typical values" % why)
            i = self.pick(head, rows, "Enter edit  Esc back")
            if i is None:
                break
            t = (types + others)[i]
            self.edit_format(t, fmt, options.get(t, {}))
        self.show("Data format")
        for t in types + others:
            self.out.append(describe(t))
        self.say("")
        self.say("Applies to the next logging session.", "label")
        self.note("")

    def edit_format(self, t, fmt, opts):
        f = fmt[t]
        fields = [("on", "Enabled")] + [(k, n) for k, n in (("rate", "Rate"), ("res", "Resolution"), ("range", "Range"))
                                         if opts.get(k) or f[k]]
        cur = 0
        self.note("←/→ change  Enter toggle  Esc back", "status")
        while True:
            def pane(y0, x0, ph, pw):
                self.put(y0 - 1, x0, "%s format" % t.upper(), "heading", pw)
                for i, (k, label) in enumerate(fields):
                    if k == "on":
                        val = "[x] on" if f["on"] else "[ ] off"
                    else:
                        u = UNITS[k][t] if isinstance(UNITS[k], dict) else UNITS[k]
                        val = "◀ %s ▶" % ("%s %s" % (f[k], u) if f[k] else "Polar default")
                    style = "selected" if i == cur else "text"
                    self.put(y0 + i, x0, ("  %-12s %s" % (label, val)).ljust(pw), style, pw)
                choices = opts.get(fields[cur][0]) if fields[cur][0] != "on" else None
                if choices:
                    self.put(y0 + len(fields) + 1, x0, "Possible: " + ", ".join(str(c) for c in choices), "label", pw)
            self.draw(pane)
            ch = self.scr.getch()
            k = fields[cur][0]
            if ch in (curses.KEY_UP, ord("k")):
                cur = max(0, cur - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                cur = min(len(fields) - 1, cur + 1)
            elif ch in (27, ord("q")):
                break
            elif k == "on" and ch in (10, 13, ord(" "), curses.KEY_LEFT, curses.KEY_RIGHT):
                fmt[t] = f = self.call(self.feather.set_format, t, on=not f["on"])
            elif k != "on" and ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l")):
                values = sorted(set(opts.get(k, [])) | ({f[k]} if f[k] else set()))
                values = [0] + values
                i = values.index(f[k]) if f[k] in values else 0
                i = (i + (1 if ch in (curses.KEY_RIGHT, ord("l")) else -1)) % len(values)
                fmt[t] = f = self.call(self.feather.set_format, t, **{k: values[i]})
        self.note("")

    def do_start(self):
        self.busy("Starting: connecting to the Polar and starting streams...")
        r = self.call(self.feather.start_log)
        self.refresh_status()
        self.show("Logging")
        self.table([("File", r["file"], "ok"), ("Streams", ", ".join(r["started"]), "ok")] +
                   ([("Failed", ", ".join(r["failed"]).replace("_", " "), "error")] if r["failed"] else []))
        self.say("")
        self.say("You can disconnect or quit: the feather keeps logging.", "label")
        self.note("")

    def do_stop(self):
        if not self.confirm("Stop logging?"):
            return
        self.busy("Stopping...")
        f, n = self.call(self.feather.stop_log)
        self.refresh_status()
        self.show("Logging stopped")
        self.table([("File", f, "ok"), ("Size", human(n))])
        self.say("")
        self.say("Use 'Sync data' to copy it to %s." % self.data_dir, "label")
        self.note("")

    def do_password(self):
        new = self.prompt("New feather password (1-32 chars, no spaces)", secret=True)
        if not new:
            return
        if self.prompt("Repeat new password", secret=True) != new:
            return self.note("Passwords differ - not changed", "error")
        if " " in new or len(new) > 32:
            return self.note("Password must be 1-32 characters without spaces", "error")
        old = self.cfg["connection"]["password"] or load_state().get("password") or "prec"
        self.busy("Changing password...")
        self.call(self.feather.change_password, new, old)
        save_state(password=new)
        self.show("Feather password")
        self.say("Password changed and remembered for Bluetooth connections.", "ok")
        self.say("Set the same password on the Bangle (PREC > Settings > Feather password).", "label")
        if self.cfg["connection"]["password"]:
            self.say("Note: tui.toml sets a password; update it there too.", "warn")
        self.note("")

    def do_monitor(self):
        """Live heart rate, state and batteries, refreshed every second until Esc."""
        data, err, beats, beat_until, next_poll = None, None, -1, 0, 0
        self.scr.timeout(100)
        self.note("Esc back", "status")
        try:
            while True:
                now = time.time()
                if now >= next_poll:
                    next_poll = now + 1
                    try:
                        data, err = self.call(self.feather.heart_rate), None
                        if beats >= 0 and data["beats"] != beats:
                            beat_until = now + 0.25
                        beats = data["beats"]
                        self.status.update(state=data["state"], logging=data["logging"],
                                           polar="connected" if data["polar"] else "disconnected",
                                           bat=data["bat"], bytes=data["bytes"], file=data["file"])
                    except PrecError as e:
                        err = str(e)
                self.draw(lambda y0, x0, ph, pw: self.draw_monitor(y0, x0, ph, pw, data, err, now < beat_until))
                ch = self.scr.getch()
                if ch in (27, ord("q"), curses.KEY_LEFT):
                    break
        finally:
            self.scr.timeout(500)
            self.note("")
        self.show("Monitor")
        self.say("Left the heart rate monitor.", "label")

    def draw_monitor(self, y0, x0, ph, pw, d, err, beat):
        self.put(y0 - 1, x0, "Heart rate monitor", "heading", pw)
        self.put(y0 - 1, x0 + pw - 5, time.strftime("%H:%M"), "heading", 5)   # 24 h clock, laptop time
        if d is None:
            return self.put(y0 + 1, x0, err or "Reading...", "error" if err else "label", pw)
        fresh = d["hr"] is not None and d["age"] < 5000
        rows = big_text(str(d["hr"]) if fresh else "--")
        width = len(rows[0])
        for r in range(5):
            self.put(y0 + 1 + r, x0 + 1, rows[r], "text", pw - 1)
            self.put(y0 + 1 + r, x0 + 2 + width, HEART[beat][r], "heart", max(0, pw - width - 2))
        self.put(y0 + 4, x0 + 3 + width + len(HEART[True][0]), "bpm", "label")
        if not d["polar"]:
            note, style = "Polar not connected", "error"
        elif d["hr"] is None:
            note, style = "waiting for the first heart rate measurement", "warn"
        elif not fresh:
            note, style = "no heart rate for %d s" % (d["age"] // 1000), "warn"
        elif d["contact"] is False:
            note, style = "no skin contact - moisten the electrodes", "warn"
        else:
            note, style = "", "text"
        self.put(y0 + 7, x0 + 1, note, style, pw - 1)
        state_style = {"logging": "ok", "reconnecting": "warn"}.get(d["state"], "label")
        rows = [("State", d["state"].upper(), state_style)]
        if d["logging"]:
            rows.append(("File", "%s  %s" % (os.path.basename(d["file"]), human(d["bytes"]))))
        pb = d["polar_battery"]
        rows += [("Polar battery", "-" if pb is None else "%d %%" % pb, "warn" if pb is not None and pb < 20 else "text"),
                 ("Feather battery", "%.2f V" % (d["bat"] / 1000), "warn" if d["bat"] < 3500 else "text"),
                 ("Speed (model)", ("%.2f m/s  %s/km  (%.1f s ago, %d ms)" % (
                     d["speed"], pace(d["speed"]), d["speed_age"] / 1000, d["predict_ms"])) if d.get("speed") is not None
                  else d.get("model_state", "-"), "ok" if d.get("speed") is not None and d["speed_age"] < 5000 else "label"),
                 ("Class", d.get("cls") or "-", "text"),
                 ("Distance", "%.2f km" % (d.get("dist", 0) / 1000)),
                 ("Cadence", "%d steps/min (%s)" % (d["cadence"], {"dft": "spectrum", "count": "counted"}.get(
                     d.get("cadence_method"), "?")) if d.get("cadence") else "-"),
                 ("Running for", "%d:%02d:%02d" % (d["runtime"] // 3600, d["runtime"] // 60 % 60, d["runtime"] % 60)
                  if d.get("runtime") else "-"),
                 ("Model", d.get("model", "-")),
                 ("RR intervals", ", ".join("%d ms" % r for r in d["rr"]) or "-"),
                 ("Last update", "-" if d["hr"] is None else "%.1f s ago" % (d["age"] / 1000))]
        w = max(len(r[0]) for r in rows) + 2
        for i, r in enumerate(rows):
            self.put(y0 + 9 + i, x0 + 1, r[0], "label", pw - 1)
            self.put(y0 + 9 + i, x0 + 1 + w, r[1], r[2] if len(r) > 2 else "text", pw - 1 - w)
        if err:
            self.put(y0 + 10 + len(rows), x0 + 1, err, "error", pw - 1)

    def do_settings(self):
        self.busy("Reading settings...")
        s = self.call(self.feather.get_settings)
        keys = [k for k in SETTING_HELP if k in s]
        while True:
            rows = [[("%-20s" % SETTING_HELP[k][0], "label"), ("%6d   " % s[k], "text"), (SETTING_HELP[k][1], "label")]
                    for k in keys]
            i = self.pick("Feather settings - Enter to change", rows)
            if i is None:
                break
            v = self.prompt_int(SETTING_HELP[keys[i]][0], s[keys[i]])
            if v is not None:
                try:
                    self.call(self.feather.set_setting, keys[i], v)
                    s[keys[i]] = v
                except PrecError as e:
                    self.note(str(e), "error")
        self.show("Feather settings")
        self.table([(SETTING_HELP[k][0], s[k]) for k in keys])
        self.note("")

    # -- main loop --
    def run(self):
        while self.running:
            self.draw()
            ch = self.scr.getch()
            items = self.menu()
            try:
                if ch == -1:
                    if self.feather:
                        self.call(self.feather.poll)
                        if time.time() - self.last_status > 10:
                            self.refresh_status()
                    continue
                if self.message[1] in ("error", "event", "ok"):
                    self.note("")
                tab = {curses.KEY_F1: 1, curses.KEY_F2: 2, curses.KEY_F3: 3, curses.KEY_F4: 4, curses.KEY_F5: 5}.get(ch)
                if ch == 9:
                    tab = self.tab % len(TABS) + 1
                elif ch == curses.KEY_BTAB:
                    tab = (self.tab - 2) % len(TABS) + 1
                if tab:
                    self.switch_tab(tab)
                elif 0 <= ch < 256 and chr(ch) in self.hotkeys():      # before j/k: they can be shortcuts (F4 k)
                    i = self.hotkeys().index(chr(ch))
                    if i < len(items):
                        self.sel = i
                        self.activate(items[i])
                elif ch in (curses.KEY_UP, ord("k")):
                    self.sel = (self.sel - 1) % len(items)
                elif ch in (curses.KEY_DOWN, ord("j")):
                    self.sel = (self.sel + 1) % len(items)
                elif ch == curses.KEY_PPAGE:
                    self.scroll = max(0, self.scroll - 10)
                elif ch == curses.KEY_NPAGE:
                    self.scroll = min(max(0, len(self.wrapped(self.scr.getmaxyx()[1] - self.MENU_W - 3)) - 5), self.scroll + 10)
                elif ch in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
                    self.activate(items[self.sel])
            except Disconnected as e:
                self.feather = None
                self.status = {}
                self.show("Connection lost")
                self.say(str(e), "error")
                self.note("Connection lost", "error")
                if self.tab == 2:
                    self.select_connect()
            except PrecError as e:
                self.note("Error: %s" % e, "error")
            except Exception as e:      # keep the UI alive, show what happened
                self.left = None
                self.note("Unexpected error: %s" % (e if isinstance(e, (OSError, ValueError)) else repr(e)), "error")

    # -- F1 home --
    def home_overview(self):
        """Overview of the feather link and the data folders."""
        paths = self.mconf["paths"]
        self.show("PREC")
        rows = []
        if self.feather:
            s = self.status
            rows.append(("Feather", "%s via %s, %s" % (self.feather.name, self.feather.kind, s.get("state", "?")),
                         "ok"))
        else:
            rows.append(("Feather", "not connected (F2 connects)", "label"))
        try:
            recs = M.recordings(self.mconf)
            pairs = M.list_pairs(self.mconf)
            gps = M.gps_files(self.mconf)
            rows += [("Recordings", "%d in %s, %d not paired" % (len(recs), tilde(paths["data"]),
                                                               sum(1 for r in recs if not r[4]))),
                     ("GPS files", "%d in %s" % (len(gps), tilde(paths["gps"]))),
                     ("Pairs", "%d in %s, %d with a confirmed offset" % (
                         len(pairs), tilde(paths["pairs"]), sum(1 for p in pairs if p.get("offset_confirmed")))),
                     ("Avoid", "%d rectangles in %s" % (len(M.list_avoid(self.mconf)), tilde(paths["avoid"])))]
        except Exception as e:
            rows.append(("Data", "cannot read: %s" % e, "error"))
        self.table(rows)
        self.say("")
        self.say("Configuration", "heading")
        self.table([("Looks, connection", "%s (%s)" % (tilde(CONFIG_PATH), "found" if os.path.exists(CONFIG_PATH) else "defaults")),
                    ("Data settings", "%s (%s)" % (tilde(self.mconf["_path"]),
                                                   "found" if os.path.exists(self.mconf["_path"]) else "defaults"))])
        self.say("")
        self.say("F2: connect to the feather, sync, logging, heart rate.", "label")
        self.say("F3: the Bangle.js: status, recordings, the prec app, models.", "label")
        self.say("F4: pair recordings with .fit files, set offsets, intervals, plots and maps.", "label")
        self.say("F5: train speed models and classifiers on the pairs; build them into the feather.", "label")

    def do_write_configs(self):
        out = []
        for path, text in ((CONFIG_PATH, DEFAULT_CONFIG), (self.mconf["_path"], M.DEFAULT_CONF)):
            if os.path.exists(path):
                out.append("%s exists (unchanged)" % tilde(path))
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(text)
                out.append("wrote %s" % tilde(path))
        self.home_overview()
        self.note("; ".join(out), "ok")

    # -- F4 data: helpers --
    def show_pairs(self, lines=()):
        """The F4 page: result lines of the last command, then all pairs."""
        if self.message[1] == "warn" and self.message[0].startswith("⏳"):
            self.note("")
        self.show("Pairs  (%s)" % tilde(self.mconf["paths"]["pairs"]))
        for text, style in lines:
            self.say(text, style)
        if lines:
            self.say("")
        pairs = M.list_pairs(self.mconf)
        if not pairs:
            self.say("No pairs yet. 'Update pairs' pairs recordings in %s with overlapping .fit files in %s." % (
                tilde(self.mconf["paths"]["data"]), tilde(self.mconf["paths"]["gps"])), "label")
            return
        for p in pairs:
            start = p.get("fit_start")
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(M.from_iso(start))) if start else "?"
            dur = (p.get("t_end", 0) - p.get("t_start", 0)) / 60
            cls = " ".join("%s%d" % (CLASS_LETTER[c], len(p.get(c, []))) for c in M.CLASSES if p.get(c))
            self.out.append([
                ("%-24s " % p["name"][:24], "text"),
                ("%s %4.0f min " % (when, dur), "label"),
                ("%+7.1fs" % p.get("offset", 0), "ok" if p.get("offset_confirmed") else "warn"),
                ("%s " % (" " if p.get("offset_confirmed") else "?"), "warn"),
                ("%2d iv%s " % (len(p.get("intervals", [])), "" if p.get("intervals_auto", True) else "*"), "text"),
                (cls or "-", "label" if not cls else "text"),
                ("" if p.get("cooked") else "  not built", "error")])
        self.say("")
        self.say("offset: green = confirmed, yellow ? = not yet   iv* = intervals edited by hand   "
                 "R run, W walk, S still, T stretch intervals", "label")

    def pick_left(self, title, labels, styles=None):
        """Choose from a list shown in place of the menu (styles: optional style
        name per item). Returns the index or None (Esc)."""
        if not labels:
            return None
        cur = 0
        self.note("↑↓ choose   Enter select   Esc back", "status")
        try:
            while True:
                self.left = (title, labels, cur, styles)
                self.draw()
                ch = self.scr.getch()
                if ch in (curses.KEY_UP, ord("k")):
                    cur = (cur - 1) % len(labels)
                elif ch in (curses.KEY_DOWN, ord("j")):
                    cur = (cur + 1) % len(labels)
                elif ch in (curses.KEY_PPAGE,):
                    cur = max(0, cur - 10)
                elif ch in (curses.KEY_NPAGE,):
                    cur = min(len(labels) - 1, cur + 10)
                elif ch in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
                    return cur
                elif ch in (27, ord("q"), curses.KEY_LEFT):
                    return None
        finally:
            self.left = None
            self.note("")

    def pick_pair(self, title):
        pairs = M.list_pairs(self.mconf)
        if not pairs:
            self.note("No pairs yet: use Update pairs or New pair", "warn")
            return None
        i = self.pick_left(title, ["%s %s" % ("✓" if p.get("offset_confirmed") else "?", p["name"]) for p in pairs])
        return None if i is None else pairs[i]

    def run_gui(self, *args):
        """Open a prec_gui.py window and wait for it. Returns its exit status
        (0 saved, 1 closed, 2 error); errors are shown from the log."""
        log = os.path.join(self.mconf["paths"]["cache"], "gui.log")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        self.busy("Window open: %s - close it to come back here" % " ".join(args))
        with open(log, "a") as lf:
            lf.write("\n== %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(args)))
            lf.flush()
            rc = subprocess.run([sys.executable, GUI, "--conf", self.mconf["_path"], *args],
                                stdin=subprocess.DEVNULL, stdout=lf, stderr=lf).returncode
        curses.flushinp()
        self.note("")
        if rc not in (0, 1):
            tail = [l.strip() for l in open(log).read().splitlines() if l.strip()][-1:]
            self.note("Window failed: %s (see %s)" % (tail[0] if tail else rc, tilde(log)), "error")
        return rc

    def build(self, pair, what="Building"):
        self.busy("%s %s: reading the recording and the .fit file..." % (what, pair["name"]))
        return M.build_pair(self.mconf, pair)

    def browse(self, heading, start, exts):
        """File browser in the output pane: returns a file path ending in one of exts, or None."""
        d = start if os.path.isdir(start) else os.path.expanduser("~")
        while True:
            try:
                names = sorted(os.listdir(d))
            except OSError as e:
                self.note(str(e), "error")
                d = os.path.dirname(d)
                continue
            dirs = [n for n in names if not n.startswith(".") and os.path.isdir(os.path.join(d, n))]
            files = [n for n in names if n.lower().endswith(exts) and os.path.isfile(os.path.join(d, n))]
            rows = [[("..", "label")]] + [[(n + "/", "heading")] for n in dirs]
            for n in files:
                st = os.stat(os.path.join(d, n))
                rows.append([("%-32s " % n, "text"), ("%9s  %s" % (human(st.st_size), time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(st.st_mtime))), "label")])
            i = self.pick("%s   %s" % (heading, tilde(d)), rows, "Enter open folder / choose file   Esc cancel")
            if i is None:
                return None
            if i == 0:
                d = os.path.dirname(d.rstrip("/")) or "/"
            elif i <= len(dirs):
                d = os.path.join(d, dirs[i - 1])
            else:
                return os.path.join(d, files[i - 1 - len(dirs)])

    def offset_then_build(self, pair):
        """Open the offset window; rebuild the pair if an offset was confirmed."""
        if self.run_gui("offset", pair["name"]) == 0:
            pair = self.build(M.load_pair(self.mconf, pair["name"]), "Rebuilding")
            return pair, True
        return pair, False

    def ask_reapply(self):
        auto = [p for p in M.list_pairs(self.mconf) if p.get("intervals_auto", True) and p.get("cooked")]
        if auto and self.confirm("Re-apply the avoid rectangles to the %d pairs with automatic intervals?" % len(auto)):
            self.busy("Re-applying avoid rectangles...")
            return M.reapply_avoid(self.mconf)
        return None

    # -- F4 data: commands --
    def do_update(self):
        self.busy("Looking for recordings and .fit files that overlap...")
        r = M.update(self.mconf, progress=lambda t: self.busy(t))
        lines = [("Update: %d new pair%s" % (len(r["created"]), "" if len(r["created"]) == 1 else "s"),
                  "ok" if r["created"] else "label")]
        lines += [("  new: " + n, "ok") for n in r["created"]]
        if r["unmatched"]:
            lines.append(("  no overlapping .fit file in %s: %s" % (tilde(self.mconf["paths"]["gps"]),
                                                                  ", ".join(r["unmatched"])), "warn"))
        if r["skipped"]:
            lines.append(("  %d recordings too short to pair (under %d samples)" % (
                len(r["skipped"]), self.mconf["pairing"]["min_samples"]), "label"))
        if r["created"]:
            lines.append(("  Check each new pair's offset with 'Offset'.", "label"))
        self.show_pairs(lines)

    def do_new_pair(self):
        self.busy("Reading the data and gps folders...")
        recs = M.recordings(self.mconf)
        if not recs:
            return self.note("No recordings in %s" % tilde(self.mconf["paths"]["data"]), "warn")
        rows = []
        for name, s, e, n, paired in recs:
            rows.append([("%-26s " % name, "text"), ("%s  %4.0f min  " % (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(s)), (e - s) / 60), "label"),
                ("paired: " + paired if paired else "not paired", "label" if paired else "ok")])
        i = self.pick("New pair: choose the recording", rows)
        if i is None:
            return self.show_pairs()
        prc, s, e = recs[i][0], recs[i][1], recs[i][2]
        fits = M.gps_files(self.mconf, (s, e))
        if not fits:
            return self.note("No .fit files in %s" % tilde(self.mconf["paths"]["gps"]), "warn")
        rows = [[("%-30s " % n, "text"), ("%s  %4.0f min  " % (time.strftime("%Y-%m-%d %H:%M", time.localtime(fs)),
                                                              (fe - fs) / 60), "label"),
                 ("overlap %4.0f s" % ov if ov else "no overlap", "ok" if ov else "warn")] for n, fs, fe, ov in fits]
        j = self.pick("New pair: choose the .fit file for %s" % prc, rows)
        if j is None:
            return self.show_pairs()
        pair = self.build(M.new_pair(self.mconf, prc, fits[j][0]))
        pair, ok = self.offset_then_build(pair)
        self.show_pairs([("New pair %s (%s + %s)%s" % (pair["name"], prc, fits[j][0],
                                                      "" if ok else " - offset not confirmed yet"), "ok")])

    def do_offset(self):
        pair = self.pick_pair("Offset of which pair?")
        if pair:
            pair, ok = self.offset_then_build(pair)
            self.show_pairs([("%s: offset %+.1f s %s" % (pair["name"], pair["offset"],
                                                          "confirmed, rebuilt" if ok else "unchanged"),
                              "ok" if ok else "label")])

    def do_plot(self):
        pair = self.pick_pair("Plot which pair?")
        if pair:
            self.run_gui("plot", pair["name"])
            self.show_pairs()

    def do_intervals(self):
        pair = self.pick_pair("Intervals of which pair?")
        if pair:
            saved = self.run_gui("intervals", pair["name"]) == 0
            self.show_pairs([("%s: intervals %s" % (pair["name"], "saved" if saved else "unchanged"),
                              "ok" if saved else "label")])

    def do_classes(self):
        """Pick a pair on the right, then edit its run/walk/still/stretch intervals in one window."""
        pairs = M.list_pairs(self.mconf)
        if not pairs:
            return self.note("No pairs yet: use Update pairs or New pair", "warn")
        rows = [[("%s %-28s " % ("✓" if p.get("offset_confirmed") else "?", p["name"]), "text")]
                + [("%s %-3d" % (c, len(p.get(c, []))), "label" if p.get(c) else "disabled") for c in M.CLASSES]
                for p in pairs]
        k = self.pick("Class intervals of which pair?  (run, walk, still, stretch counts)", rows)
        if k is None:
            return self.show_pairs()
        pair = pairs[k]
        saved = self.run_gui("classes", pair["name"]) == 0
        if not saved:
            return self.show_pairs([("%s: class intervals unchanged" % pair["name"], "label")])
        p = self.build(M.load_pair(self.mconf, pair["name"]), "Cooking")        # the class arrays follow
        ca = p.get("class_arrays", {})
        self.show_pairs([("%s: class intervals saved and cooked: %s" % (pair["name"], ", ".join(
            "%s %d samples" % (c, ca[c]) for c in M.CLASSES if c in ca) or "no class arrays"), "ok")])

    def do_avoid(self):
        pairs = M.list_pairs(self.mconf)
        labels = ["all pairs"] + [p["name"] for p in reversed(pairs)]
        i = self.pick_left("Map around which track?", labels)
        if i is None:
            return
        args = ["avoid"] + ([] if i == 0 else ["--pair", labels[i]])
        lines = []
        if self.run_gui(*args) == 0:
            changed = self.ask_reapply()
            lines.append(("Avoid rectangles saved%s" % (
                "; intervals updated for: " + ", ".join(changed) if changed else ""), "ok"))
        self.show_pairs(lines)

    def do_edit_avoid(self):
        rects = M.list_avoid(self.mconf)
        if not rects:
            return self.note("No avoid rectangles yet: use 'Avoid: new rectangles'", "warn")
        labels = ["%s  %.0f×%.0f m" % (r["name"], (r["east"] - r["west"]) * 111320 *
                                       math.cos(math.radians((r["north"] + r["south"]) / 2)),
                  (r["north"] - r["south"]) * 110540) for r in rects]
        i = self.pick_left("Edit which rectangle?", labels)
        if i is None:
            return
        lines = []
        if self.run_gui("avoid-edit", rects[i]["name"]) == 0:
            changed = self.ask_reapply()
            lines.append(("Rectangle %s changed%s" % (rects[i]["name"], "; intervals updated for: " + ", ".join(changed)
                                                      if changed else ""), "ok"))
        self.show_pairs(lines)

    def do_legacy(self):
        b = self.browse("Legacy import: choose the .bin file", self.mconf["paths"]["legacy"], (".bin",))
        if not b:
            return self.show_pairs()
        f = self.browse("Legacy import: choose the .fit file for %s" % os.path.basename(b), os.path.dirname(b), (".fit",))
        if not f:
            return self.show_pairs()
        t = self.prompt("Offset: seconds from the .fit start to the first .bin sample", "0")
        if t is None:
            return self.show_pairs()
        try:
            off = float(t)
        except ValueError:
            return self.note("Not a number: %s" % t, "error")
        self.busy("Importing %s ..." % b)
        pair = M.import_legacy(self.mconf, b, f, off, progress=lambda s: self.busy("Legacy import: " + s))
        pair, ok = self.offset_then_build(pair)
        self.show_pairs([("Legacy import: stream %s, pair %s%s" % (pair["prc"], pair["name"],
                                                                   "" if ok else " - offset not confirmed yet"), "ok")])

    def do_cook(self):
        """Rebuild the cooked array and the class arrays (_run, _walk, _still, _stretch) of one pair or all."""
        pairs = M.list_pairs(self.mconf)
        if not pairs:
            return self.note("No pairs yet: use Update pairs or New pair", "warn")
        i = self.pick_left("Cook which pair?", ["all pairs"] + [p["name"] for p in pairs])
        if i is None:
            return
        lines = []
        for p in pairs if i == 0 else [pairs[i - 1]]:
            try:
                p = self.build(p, "Cooking")
            except Exception as ex:
                lines.append(("%s: %s" % (p["name"], ex), "error"))
                continue
            ca = p.get("class_arrays", {})
            lines.append(("%s: %d samples%s" % (p["name"], p["samples"], "".join(
                ", %s %d" % (c, ca[c]) for c in M.CLASSES if c in ca)), "ok"))
        self.show_pairs(lines)

    def do_delete(self):
        pair = self.pick_pair("Delete which pair?")
        if pair and self.confirm("Delete pair %s? (only its .json; the .prc, .fit and array stay)" % pair["name"]):
            M.delete_pair(self.mconf, pair["name"])
            self.show_pairs([("Deleted pair %s" % pair["name"], "ok")])

    # -- F5 training --
    def train_menu(self):
        running = self.training_running()
        return [
            ("Roles: assign pairs", self.do_roles, True),
            ("Parameters", self.do_params, True),
            ("Histogram", self.do_histogram, True),
            ("Architectures", self.do_archs, True),
            ("Stop training" if running else "Train", self.do_stop_training if running else self.do_train, True),
            ("Training log", self.do_train_log, True),
            ("Models", self.do_models, True),
            ("Predict on a pair", self.do_predict, True),
            ("Build models into feather", self.do_feather_model, True),
            ("Quit", self.do_quit, True),
        ]

    def tparams(self):
        return T.load_train(self.mconf)

    def show_training(self, lines=()):
        """The F5 page: roles, the main parameters and the newest models."""
        t = self.tparams()
        self.show("Training  (%s)" % tilde(T.train_path(self.mconf)))
        for text, style in lines:
            self.say(text, style)
        if lines:
            self.say("")
        try:
            e = T.effective(self.mconf, t)
            err = None
        except Exception as ex:
            e, err = t, str(ex)
        for r in T.ROLES:
            names = t["roles"].get(r, [])
            self.out.append([("%-6s " % r, "label"), (", ".join(names) if names else "-", "text" if names else "warn")])
        self.say("")
        inp = e.get("input_shape", "?")
        self.table([
            ("Architecture", "%s  %s" % (t["arch"], e.get("arch_doc", ""))),
            ("Input", "%s   window %d, stride %d (%s Hz), shift %d" % (
                inp, t["window"], t["stride"], e.get("frequency", "?"), t["shift"])),
            ("Features", ("raw samples" if not e["dft"] else "%d DFT coefficients%s, %s window" % (
                e["dft"], " (abs)" if e["dft_abs"] else " (re, im)", t["window_fn"])) +
             ("  (set by the architecture)" if e.get("arch_sets") else "")),
            ("Labels", "%s speed, %.1f-%.1f m/s, %s per %.2f m/s" % (
                t["target"], t["min_speed"], t["max_speed"], t["strategy"], t["resolution"])),
            ("Training", "%d epochs, lr %g, %s loss, batch %d%s" % (
                t["epochs"], t["learning_rate"], t["loss"], t["batch"]["train"], ", int8" if t["quantize"] else "")),
        ])
        if err:
            self.say("")
            self.say("Problem: " + err, "error")
        models = T.list_models(self.mconf)[:5]
        if models:
            self.say("")
            self.say("Newest models", "heading")
            for m in models:
                self.out.append(self.model_row(m))
        if self.training_running():
            self.say("")
            self.say("Training is running - 'Training log' shows its progress.", "warn")

    def model_row(self, m):
        if m.get("kind") == "classifier":
            va = m.get("history", {}).get("val_accuracy")
            return [("%-31s " % m["name"], "text"), ("%-7s " % m["arch"], "label"),
                    ("val acc %s  " % ("%.3f" % max(va) if va else "  -  "), "text"),
                    ("test acc %s  " % ("%.3f" % m["test_accuracy"] if "test_accuracy" in m else "  -  "), "text"),
                    ("classifier", "label")]
        vm = m.get("history", {}).get("val_mae")
        return [("%-31s " % m["name"], "text"), ("%-7s " % m["arch"], "label"),
                ("val mae %s  " % ("%.3f" % min(vm) if vm else "  -  "), "text"),
                ("test mae %s  " % ("%.3f" % m["test_mae"] if "test_mae" in m else "  -  "), "text"),
                ("int8 " if m.get("quantized") else "f32  ", "label"),
                ("js" if m.get("has_js") else "no js", "ok" if m.get("has_js") else "warn")]

    def checklist(self, heading, labels, checked):
        """Tick items with space or Enter; Esc returns the set of ticked indices."""
        cur, top, sel = 0, 0, set(checked)
        self.note("space/Enter tick   Esc done", "status")
        while True:
            def pane(y0, x0, ph, pw):
                nonlocal top
                self.put(y0 - 1, x0, heading, "heading", pw)
                top = min(max(top, cur - ph + 1), cur)
                for i, lab in enumerate(labels[top:top + ph]):
                    k = top + i
                    self.put(y0 + i, x0, ("[x] " if k in sel else "[ ] ") + lab, "selected" if k == cur else
                             ("ok" if k in sel else "text"), pw)
            self.draw(pane)
            ch = self.scr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                cur = max(0, cur - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                cur = min(len(labels) - 1, cur + 1)
            elif ch in (ord(" "), 10, 13, curses.KEY_ENTER):
                sel ^= {cur}
            elif ch in (27, ord("q"), curses.KEY_LEFT):
                self.note("")
                return sel

    def do_roles(self):
        """One list of all pairs: t, v, e, q toggle train, valid, test, quant for the
        highlighted pair (saved at once); Esc returns."""
        pairs = M.list_pairs(self.mconf)
        if not pairs:
            return self.note("No pairs yet: make them in F4", "warn")
        t = self.tparams()
        cur, top = 0, 0
        self.note("t train   v valid   e test   q quant   (toggle)   ↑↓ move   Esc done", "status")
        while True:
            def pane(y0, x0, ph, pw):
                nonlocal top
                self.put(y0 - 1, x0, "Roles: t train (green)  v valid (amber)  e test (red)  q quant (purple)", "heading", pw)
                top = min(max(top, cur - ph + 3), cur)
                for i, p in enumerate(pairs[top:top + ph - 2]):
                    k, y = top + i, y0 + i
                    has = [r for r in T.ROLES if p["name"] in t["roles"].get(r, [])]
                    x = x0
                    self.put(y, x, "▶" if k == cur else " ", "selected" if k == cur else "text")
                    x += 2
                    for r, letter in zip(T.ROLES, "TVEQ"):
                        self.put(y, x, letter if r in has else "·", "role_" + r if r in has else "disabled")
                        x += 2
                    text = " %-26s %4.0f min" % (p["name"], (p.get("t_end", 0) - p.get("t_start", 0)) / 60)
                    self.put(y, x, text, "selected" if k == cur else ("role_" + has[0] if has else "text"), pw - (x - x0))
                counts = "   ".join("%s %d" % (r, len(t["roles"].get(r, []))) for r in T.ROLES)
                self.put(y0 + min(ph - 1, len(pairs) + 1), x0, counts, "label", pw)
            self.draw(pane)
            ch = self.scr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                cur = max(0, cur - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                cur = min(len(pairs) - 1, cur + 1)
            elif ch in (curses.KEY_PPAGE,):
                cur = max(0, cur - 10)
            elif ch in (curses.KEY_NPAGE,):
                cur = min(len(pairs) - 1, cur + 10)
            elif 0 <= ch < 256 and chr(ch) in ROLE_KEYS:
                role, name = ROLE_KEYS[chr(ch)], pairs[cur]["name"]
                lst = t["roles"].setdefault(role, [])
                if name in lst:
                    lst.remove(name)
                else:
                    lst.append(name)
                T.save_train(self.mconf, t)
            elif ch in (27, curses.KEY_LEFT):
                break
        self.note("")
        self.show_training([("Roles: " + "   ".join("%s %d" % (r, len(t["roles"].get(r, []))) for r in T.ROLES), "ok")])

    def do_params(self):
        while True:
            t = self.tparams()
            try:
                e = T.effective(self.mconf, t)
            except Exception:
                e = dict(t, arch_sets=[])
            rows = []
            for key, label, choices, hlp in TRAIN_PARAMS:
                v = t["batch"][key.split(".")[1]] if key.startswith("batch.") else t.get(key)
                note = ""
                if key in e.get("arch_sets", []):
                    v, note = e[key], "  (set by %s)" % t["arch"]
                rows.append([("%-20s " % label, "label"), ("%-10s " % v, "text"), (note or hlp, "warn" if note else "label")])
            i = self.pick("Training parameters - Enter changes", rows, "Enter change   Esc back")
            if i is None:
                break
            key, label, choices, hlp = TRAIN_PARAMS[i]
            cur = t["batch"][key.split(".")[1]] if key.startswith("batch.") else t.get(key)
            if choices in ("arch", "class_arch"):
                archs = list(T.list_archs(self.mconf, "class" if choices == "class_arch" else "speed"))
                k = self.pick_left(label, archs)
                if k is None:
                    continue
                new = archs[k]
            elif choices:
                new = choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else choices[0]
            else:
                new = self.prompt("%s (%s)" % (label, hlp), str(cur))
                if new is None:
                    continue
            try:
                T.set_param(t, key, str(new))
                T.effective(self.mconf, t)       # complain early about impossible combinations
                T.save_train(self.mconf, t)
            except Exception as ex:
                self.note("Not changed: %s" % ex, "error")
        self.show_training()

    def do_histogram(self):
        i = self.pick_left("Histogram of which role?", list(T.ROLES))
        if i is None:
            return
        role = T.ROLES[i]
        t = self.tparams()
        self.busy("Counting %s windows..." % role)
        e = T.effective(self.mconf, t)
        edges, counts = T.histogram(self.mconf, e, role)
        self.show("Histogram: %s windows per %.2f m/s, log(n+1)" % (role, t["resolution"]))
        if not len(counts):
            self.say("No windows: assign pairs to the %s role." % role, "warn")
            return self.note("")
        w = max(10, self.scr.getmaxyx()[1] - self.MENU_W - 36)
        top = max(math.log(n + 1) for n in counts) or 1
        for lo, n in zip(edges, counts):
            self.out.append([("%5.2f-%5.2f m/s " % (lo, lo + t["resolution"]), "label"), ("%8d " % n, "text"),
                             ("█" * int(round(w * math.log(n + 1) / top)), "progress")])
        self.say("")
        self.say("%d windows%s" % (counts.sum(), "; training data: %s strategy, %.1f-%.1f m/s" % (
            t["strategy"], t["min_speed"], t["max_speed"]) if role == "train" else ""), "label")
        self.note("")

    def do_archs(self):
        """Speed and classifier architectures; Enter makes one the one used."""
        t = self.tparams()
        rows, keys = [], []
        for kind, key, title in (("speed", "arch", "speed"), ("class", "class_arch", "classifier")):
            for n in T.list_archs(self.mconf, kind):
                i = T.arch_info(self.mconf, n, kind)
                rows.append([("* " if n == t.get(key) else "  ", "ok"), ("%-10s " % title, "label"), ("%-10s " % n, "text"),
                             ("dft %-4s " % (i["dft"] if i["dft"] is not None else "cfg"), "label"),
                             ("%s  " % i["doc"], "text"), (tilde(i["path"]), "label")])
                keys.append((key, n))
        k = self.pick("Architectures (Enter uses one; add your own in %s or %s)" % (
            tilde(self.mconf["paths"]["arch"]), tilde(self.mconf["paths"].get("class", "~/prec/class"))), rows)
        if k is not None:
            T.set_param(t, *keys[k])
            T.save_train(self.mconf, t)
        self.show_training()

    def training_running(self):
        p = getattr(self, "train_proc", None)
        return p is not None and p.poll() is None

    def train_log_path(self):
        return os.path.join(self.mconf["paths"]["cache"], "train.log")

    def do_train(self):
        """Choose on the right: train a new model, or continue training a saved one."""
        t = self.tparams()
        k = self.pick("Train", [
            [("Train new model   ", "text"), ("%s, %d epochs, %d train pairs (set with p and r)" % (
                t["arch"], t["epochs"], len(t["roles"].get("train", []))), "label")],
            [("Train run         ", "text"), ("a new model on the run intervals only (_run arrays); name ends in _run", "label")],
            [("Train walk        ", "text"), ("a new model on the walk intervals only (_walk arrays); name ends in _walk", "label")],
            [("Train classifier  ", "text"), ("%s: run / walk / still / stretch from the class arrays; name ends in _classifier"
                                              % t.get("class_arch", "dense3"), "label")],
            [("Retrain a model   ", "text"), ("more epochs on a saved model, with the pairs and parameters of its .json", "label")],
        ])
        if k in (0, 1, 2):
            self.train_new((None, "run", "walk")[k])
        elif k == 3:
            self.train_new(classifier=True)
        elif k == 4:
            self.retrain()
        else:
            self.show_training()

    def train_new(self, cls=None, classifier=False):
        """A new model with the current parameters; cls run/walk: on those intervals
        only; classifier: a run/walk/still/stretch classifier."""
        t = self.tparams()
        try:
            T.effective(self.mconf, dict(t, task="classifier") if classifier else t)
        except Exception as ex:
            return self.note("Cannot train: %s" % ex, "error")
        if not t["roles"].get("train"):
            return self.note("Assign pairs to the train role first (Roles)", "warn")
        if cls and not any(M.load_pair(self.mconf, n).get(cls) for n in t["roles"]["train"]
                           if os.path.exists(M.pair_path(self.mconf, n))):
            return self.note("No train pair has %s intervals: draw them with F4 > Class intervals" % cls, "warn")
        if classifier and not any(any(M.load_pair(self.mconf, n).get(c) for c in M.CLASSES) for n in t["roles"]["train"]
                                  if os.path.exists(M.pair_path(self.mconf, n))):
            return self.note("No train pair has class intervals: draw them with F4 > Class intervals", "warn")
        self.start_training(["train"] + (["--class", cls] if cls else []) + (["--classifier"] if classifier else []))

    def retrain(self):
        models = [m for m in T.list_models(self.mconf)
                  if os.path.exists(os.path.join(os.path.expanduser(self.mconf["paths"]["models"]), m["name"] + ".keras"))]
        if not models:
            return self.note("No saved models to retrain", "warn")
        k = self.pick("Retrain which model?", [self.model_row(m) for m in models])
        if k is None:
            return self.show_training()
        m = models[k]
        if not m.get("roles", {}).get("train"):
            return self.note("%s has no train pairs in its .json" % m["name"], "error")
        epochs = self.prompt_int("More epochs for %s" % m["name"], m.get("params", {}).get("epochs", 8))
        if not epochs or epochs < 1:
            return self.show_training()
        lr = self.prompt("Learning rate (empty: continue where it stopped)")
        if lr is None:
            return self.show_training()
        args = ["retrain", m["name"], "--epochs", str(epochs)]
        if lr.strip():
            try:
                args += ["--lr", str(float(lr))]
            except ValueError:
                return self.note("Not a number: %s" % lr, "error")
        self.start_training(args)

    def start_training(self, args):
        """Run prec_train.py ARGS in the background, logging to train.log, and follow the log."""
        log = self.train_log_path()
        os.makedirs(os.path.dirname(log), exist_ok=True)
        lf = open(log, "w")
        env = dict(os.environ, TF_CPP_MIN_LOG_LEVEL="2", PYTHONUNBUFFERED="1")
        self.train_proc = subprocess.Popen([sys.executable, TRAIN, "--conf", self.mconf["_path"]] + args,
                                           stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, env=env)
        self.do_train_log()

    def do_stop_training(self):
        if self.training_running() and self.confirm("Stop the training? (nothing is saved)"):
            self.train_proc.terminate()
            self.show_training([("Training stopped", "warn")])

    def do_train_log(self):
        """Follow the training log (Esc leaves it running in the background)."""
        log = self.train_log_path()
        if not os.path.exists(log):
            return self.note("No training has run yet", "warn")
        self.scr.timeout(500)
        noise = ("I0000", "W0000", "WARNING:", "absl", "oneDNN", "instructions", "rebuild TensorFlow", "UserWarning",
                 "warnings.warn", "Interpreter is deprecated", "TF 2.20", "migration", "for details", "Saved artifact",
                 "Endpoint", "args_0", "Output Type", "TensorSpec", "Captures", "XNNPACK", "data_adapter")
        while True:
            # a progress bar is redrawn in place: keep only what follows the last \r of a line
            raw = open(log, errors="replace", newline="").read().split("\n")   # newline="": keep the \r
            lines = [keras_bar(ANSI.sub("", l.split("\r")[-1].replace("\b", "")).rstrip()) for l in raw]
            lines = [l for l in lines if l.strip() and not any(n in l for n in noise) and not l.startswith("  1")]
            state = "running" if self.training_running() else "finished"
            self.note("Esc back (training continues)   s stop training" if state == "running" else "Esc back", "status")

            def pane(y0, x0, ph, pw):
                self.put(y0 - 1, x0, "Training log (%s)  %s" % (state, tilde(log)), "heading", pw)
                rows = []                         # wrapped so that every metric stays visible
                for l in lines[-ph:]:
                    style = "error" if ("Error" in l or "Traceback" in l) else "ok" if l.startswith(("model ", "test:", "saved", "retraining")) \
                        else "progress" if "█" in l or "░" in l else "heading" if l.startswith("Epoch ") else "text"
                    rows += [(piece, style if k == 0 or style != "progress" else "text")
                             for k, piece in enumerate(wrap_log(l, pw))]
                for i, (l, style) in enumerate(rows[-ph:]):
                    self.put(y0 + i, x0, l, style, pw)
            self.draw(pane)
            ch = self.scr.getch()
            if ch in (27, ord("q"), curses.KEY_LEFT):
                break
            if ch == ord("s") and self.training_running():
                self.do_stop_training()
                break
        self.note("")
        self.show_training()

    def do_models(self):
        models = T.list_models(self.mconf)
        if not models:
            return self.note("No models yet: Train one", "warn")
        def history(i):                          # h: plot the training history, stay in the list
            self.run_gui("history", models[i]["name"])
            self.note("Enter details   h history   Esc back", "status")
            return False
        k = self.pick("Models in %s" % tilde(self.mconf["paths"]["models"]), [self.model_row(m) for m in models],
                      "Enter details   h history   Esc back", keys={ord("h"): history})
        if k is None:
            return self.show_training()
        m = models[k]
        self.show("Model %s" % m["name"])
        h = m.get("history", {})
        rows = [
            ("Architecture", m["arch"]), ("Created", m.get("created", "?")),
            ("Input", "%s   window %d, stride %d (%d Hz), dft %d%s" % (
                m["input_shape"], m["window"], m["stride"], m["frequency"], m["dft"], " abs" if m["dft"] and m["dft_abs"] else "")),
            ("Windows", ", ".join("%s %d" % kv for kv in m.get("windows", {}).items())),
        ]
        if m.get("kind") == "classifier":
            classes = m.get("classes", M.CLASSES)
            rows += [
                ("Epochs", "%d, best val accuracy %s" % (
                    len(h.get("loss", [])), "%.3f" % max(h["val_accuracy"]) if h.get("val_accuracy") else "-")),
                ("Test", "accuracy %.3f" % m["test_accuracy"] if "test_accuracy" in m else "-"),
            ]
            if m.get("confusion"):               # rows: true class; columns: predicted
                rows.append(("Confusion", "%-8s %s   (rows true, columns predicted)" % ("", "  ".join("%7s" % c for c in classes))))
                rows += [("", "%-8s %s" % (c, "  ".join("%7d" % n for n in row))) for c, row in zip(classes, m["confusion"])]
        else:
            rows += [
                ("Epochs", "%d, best val mae %s" % (len(h.get("loss", [])), "%.3f" % min(h["val_mae"]) if h.get("val_mae") else "-")),
                ("Test", "mae %.3f m/s" % m["test_mae"] if "test_mae" in m else "-"),
                ("TFLite", "%s bytes, %s, ops %s" % (m.get("tflite_bytes", "?"), "int8" if m.get("quantized") else "float32",
                                                      ", ".join(m.get("ops", [])) or "?")),
                ("Watch", "module %s bytes, arena %s bytes" % (m.get("js_bytes", "?"), m.get("arena", "?"))),
            ]
        rows.append(("Pairs", "; ".join("%s: %s" % (r, ", ".join(v)) for r, v in m.get("roles", {}).items() if v)))
        self.table(rows)
        self.note("")

    def pick_model_pair(self, model):
        """Pair picker coloured by the roles the pairs had when `model` was trained."""
        roles = model.get("roles", {})
        pairs = M.list_pairs(self.mconf)
        labels, styles = [], []
        for p in pairs:
            has = [r for r in T.ROLES if p["name"] in roles.get(r, [])]
            labels.append("%-4s%s" % ("".join(ROLE_LETTER[r] for r in has), p["name"]))
            styles.append("role_" + has[0] if has else None)
        j = self.pick_left("Pair (T V E Q: roles)", labels, styles)
        return None if j is None else pairs[j]["name"]

    def do_predict(self):
        """A form: model, pair, shift and low-pass filters; p or Plot opens the window."""
        models = T.list_models(self.mconf)
        if not models:
            return self.note("No models yet: Train one", "warn")
        pairs = [p["name"] for p in M.list_pairs(self.mconf)]
        if not pairs:
            return self.note("No pairs yet: make them in F4", "warn")
        f = getattr(self, "predict_form", None)
        if not f or f["model"] not in [m["name"] for m in models]:
            f = {"model": models[0]["name"], "pair": None, "shift": 0, "lowpass": []}
        if f["pair"] not in pairs:
            model = next(m for m in models if m["name"] == f["model"])
            tests = [n for n in model.get("roles", {}).get("test", []) + self.tparams()["roles"].get("test", []) if n in pairs]
            f["pair"] = tests[0] if tests else pairs[0]
        self.predict_form = f
        cur, result = 0, []
        hint = "↑↓ move   Enter change   l add low-pass   d delete low-pass   p plot   Esc back"
        self.note(hint, "status")
        while True:
            rows = [("model", "Model", f["model"]), ("pair", "Pair", f["pair"]),
                    ("shift", "Shift", "%d samples" % f["shift"] if f["shift"] else "auto (at most 20000 windows)")]
            rows += [("lp", "Low-pass", "time constant %g s" % tau) for tau in f["lowpass"]]
            rows += [("plot", "", "▶ Plot")]
            cur = min(cur, len(rows) - 1)

            def pane(y0, x0, ph, pw):
                self.put(y0 - 1, x0, "Predict on a pair", "heading", pw)
                for i, (key, label, value) in enumerate(rows):
                    style = "selected" if i == cur else ("ok" if key == "plot" else "text")
                    self.put(y0 + i, x0, ("  %-10s %s" % (label, value)).ljust(pw), style, pw)
                for i, (text, style) in enumerate(result):
                    self.put(y0 + len(rows) + 1 + i, x0, text, style, pw)
            self.draw(pane)
            ch = self.scr.getch()
            key = rows[cur][0]
            if ch in (curses.KEY_UP, ord("k")):
                cur = max(0, cur - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                cur = min(len(rows) - 1, cur + 1)
            elif ch in (27, curses.KEY_LEFT):
                break
            elif ch == ord("l"):
                v = self.prompt("Low-pass time constant (s)")
                try:
                    if v is not None and float(v) > 0:
                        f["lowpass"].append(float(v))
                except ValueError:
                    self.note("Not a number: %s" % v, "error")
                self.note(hint, "status")
            elif ch in (ord("d"), curses.KEY_DC) and key == "lp":
                del f["lowpass"][cur - 3]
            elif ch == ord("p") or (ch in (10, 13, curses.KEY_ENTER) and key == "plot"):
                args = ["predict", f["model"], f["pair"]] + (["--shift", str(f["shift"])] if f["shift"] else [])
                for tau in f["lowpass"]:
                    args += ["--lowpass", "%g" % tau]
                self.run_gui(*args)
                log = os.path.join(self.mconf["paths"]["cache"], "gui.log")
                try:
                    text = open(log, errors="replace").read()
                    last = text[text.rindex("== "):]
                    result = [(l[len("predict: "):], "ok") for l in last.splitlines() if l.startswith("predict:")]
                    if not result:
                        result = [("error: " + last.strip().splitlines()[-1], "error")]
                except (OSError, ValueError):
                    result = []
                self.note(hint, "status")
            elif ch in (10, 13, curses.KEY_ENTER):
                if key == "model":
                    k = self.pick_left("Model", [m["name"] + "  " + m["arch"] for m in models])
                    if k is not None:
                        f["model"] = models[k]["name"]
                elif key == "pair":
                    p = self.pick_model_pair(next(m for m in models if m["name"] == f["model"]))
                    if p:
                        f["pair"] = p
                elif key == "shift":
                    v = self.prompt("Shift in samples (empty or 0 = auto)", str(f["shift"] or ""))
                    try:
                        f["shift"] = max(0, int(v)) if v else 0
                    except ValueError:
                        self.note("Not a number: %s" % v, "error")
                self.note(hint, "status")
        self.note("")
        self.show_training(result)

    def do_feather_model(self):
        """Choose the feather's models (speed, classifier, run, walk), check them,
        build the firmware and flash it over USB."""
        models = [m for m in T.list_models(self.mconf)]
        speed = [m["name"] for m in models if m.get("kind") != "classifier"]
        clf = [m["name"] for m in models if m.get("kind") == "classifier"]
        prev = getattr(self, "feather_choice", {})
        choice = {}
        for slot, title, names in (
                ("classifier", "Classifier (run/walk/still/stretch), or none", clf),
                ("run", "Speed model for running", sorted(speed, key=lambda n: not n.endswith("_run"))),
                ("walk", "Speed model for walking", sorted(speed, key=lambda n: not n.endswith("_walk"))),
                ("speed", "Speed model for anything (without a classifier, or as the fallback)", speed)):
            if slot in ("run", "walk") and not choice.get("classifier"):
                continue
            labels = ["none"] + names
            styles = [None] + ["ok" if n == prev.get(slot) else None for n in names]
            k = self.pick_left(title, labels, styles)
            if k is None:
                return self.show_training()
            choice[slot] = None if k == 0 else names[k - 1]
        chosen = {k: v for k, v in choice.items() if v}
        if chosen and "speed" not in chosen and not ("classifier" in chosen and ("run" in chosen or "walk" in chosen)):
            return self.show_training([("The feather needs a speed model, or a classifier with a run or walk model",
                                        "error")])
        self.feather_choice = chosen
        what = ", ".join("%s %s" % kv for kv in chosen.items()) or "no model"
        if not self.confirm("Build %s into the firmware and flash the feather over USB?" % what):
            return self.show_training()
        if self.feather and self.feather.kind == "usb":
            self.feather.close()                 # the upload needs the serial port
            self.feather, self.status = None, {}
        log = os.path.join(self.mconf["paths"]["cache"], "feather_build.log")
        self.busy("Checking the models and building the firmware (about a minute; keep the feather on USB and awake)...")
        env = dict(os.environ, TF_CPP_MIN_LOG_LEVEL="3")
        args = [chosen.get("speed") or "none"] + sum((["--" + k, v] for k, v in chosen.items() if k != "speed"), [])
        with open(log, "w") as lf:
            rc = subprocess.run([sys.executable, TRAIN, "--conf", self.mconf["_path"], "feather"] + args,
                                stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, env=env).returncode
        keep = ("matches", "wrote", "compiling", "Sketch uses", "Global", "Device programmed", "Error", "error")
        lines = [l.strip() for l in open(log, errors="replace") if any(x in l for x in keep)]
        self.show_training([(l, "error" if "rror" in l else "text") for l in lines] +
                           [("Feather now runs %s" % what, "ok") if rc == 0 else
                            ("Build failed (see %s)" % tilde(log), "error")])

    # -- F3 bangle --
    def bangle_menu(self):
        return [
            ("Status", self.do_bangle_status, True),
            ("Sync recordings", self.do_bangle_sync, True),
            ("Models", self.do_bangle_models, True),
            ("Install prec app", self.bangle_install, True),
            ("App settings", self.do_bangle_settings, True),
            ("Quit", self.do_quit, True),
        ]

    def show_bangle(self, lines=()):
        """The F3 page: result lines, then the last status read from the watch."""
        if self.message[0].startswith("⏳"):
            self.note("")
        self.show("Bangle.js")
        for text, style in lines:
            self.say(text, style)
        if lines:
            self.say("")
        st = getattr(self, "bangle_state", None)
        if not st:
            self.say("Status (s) reads the watch over Bluetooth: app, models, recordings, storage.", "label")
            self.say("Disconnect the watch from the phone app first if it does not answer.", "label")
            return
        import prec_bangle as B
        avail = json.load(open(os.path.join(B.BANGLE_DIR, "prec", "metadata.json")))["version"]
        have = st["apps"].get("prec")
        self.table([
            ("Read", time.strftime("%H:%M:%S", time.localtime(st["_at"]))),
            ("Battery", "%d %%%s" % (st["battery"], ", charging" if st["charging"] else "")),
            ("Firmware", st["firmware"]),
            ("Storage", "%s free of %s (%s deleted, reclaimed by compacting)" % (
                human(st["free"]), human(st["total"]), human(st["trash"]))),
            ("Running", st["app"] or "-"),
            ("App prec", "%s%s" % (have or "not installed", "" if have == avail else
                                   "  (here: %s - Install updates it)" % avail), "ok" if have == avail else "warn"),
        ] + ([("Old app ppred", "%s  (Install prec removes it and keeps its settings)" % st["apps"]["ppred"], "warn")]
             if st["apps"].get("ppred") else []))
        self.say("")
        self.say("Models", "heading")
        roles = {f: k for k, f in st.get("use", {}).items()}
        names = {"cls": "classifier", "run": "run", "walk": "walk", "speed": "any speed"}
        rows = []
        for m in st["models"]:
            rows.append((m["file"], "%s%s, %s Hz, window %s%s, %s + %s bytes" % (
                "[%s] " % names[roles[m["file"]]] if m["file"] in roles else "", m.get("kind"), m.get("freq"),
                m.get("win"), ", dft %s" % m["dft"] if m.get("dft") else "", m["bytes"], m["tfl"]),
                "text" if m["file"] in roles else "label"))
        if rows:
            self.table(rows)
        else:
            self.say("none", "label")
        u = st.get("use", {})
        mode = ("classifier -> " + ", ".join("%s %s" % (k, u[k]) for k in ("run", "walk", "speed") if u.get(k))) \
            if u.get("cls") else ("one speed model, %s" % u["speed"]) if u.get("speed") else "no model (heart rate and cadence only)"
        self.say("The app uses: " + mode, "label")
        self.say("")
        self.say("Recordings on the watch", "heading")
        if st["recordings"]:
            self.table([("pr" + s, "%d file%s, %s" % (len(v), "" if len(v) == 1 else "s", human(sum(n for _, n in v))))
                        for s, v in st["recordings"].items()])
        else:
            self.say("none", "label")
        self.say("")
        self.say("App settings (prec.json)", "heading")
        s = st["settings"]
        pol = {p.get("id"): p.get("name") for p in s.get("polars", [])}
        rec = [n for k, n in (("record", "raw data"), ("logHr", "heart rate"), ("logCad", "cadence")) if s.get(k)]
        units = {"pace": "min/km", "pacemi": "min/mi", "kmh": "km/h", "mph": "mph", "ms": "m/s"}
        self.table([("Units", units.get(s.get("units", "pace"), s.get("units"))), ("Class filter", "%s s" % s.get("clsLp", 10)),
                    ("Speed filter", "%s s" % s.get("speedLp", 10)),
                    ("Record", ", ".join(rec) + ", files of %s" % human(s.get("chunk", 524288)) if rec else "off"),
                    ("Cadence axis", "xyz"[s.get("cadAxis", 0)]),
                    ("Cadence filter", "%s s" % s.get("cadLp", 5) if s.get("cadLp", 5) else "off"), ("Buffers", s.get("buffers", 2)),
                    ("Polar", "%s %s" % (pol.get(s.get("polar"), ""), s.get("polar") or "none chosen")),
                    ("Feather", "%s %s" % (s.get("name", ""), s.get("address") or "none yet"))])

    def bangle_call(self, what, fn):
        """Run fn(prec_bangle, progress) with progress on the status line; returns
        its result, or None after showing the error."""
        import prec_bangle as B
        last = [0]

        def prog(name, done, total):
            if time.time() - last[0] > 0.3:
                last[0] = time.time()
                self.busy("%s: %s  %s / %s" % (what, name, human(done), human(total)))
        self.busy("%s: looking for the Bangle.js (disconnect it from the phone app if needed)..." % what)
        try:
            return fn(B, prog)
        except Exception as ex:
            self.show_bangle([("%s failed: %s" % (what, ex), "error")])
            return None

    def do_bangle_status(self, lines=()):
        st = self.bangle_call("Reading the watch", lambda B, prog: B.status())
        if st is not None:
            st["_at"] = time.time()
            self.bangle_state = st
            self.show_bangle(lines)

    def do_bangle_sync(self):
        data = self.mconf["paths"]["data"]
        r = self.bangle_call("Syncing", lambda B, prog: B.sync(os.path.expanduser(data), progress=prog))
        if r is None:
            return
        lines = [("Saved %s (removed from the watch)" % tilde(p), "ok") for p in r["saved"]]
        lines += [("Skipped pr%s: %s" % s, "warn") for s in r["skipped"]]
        if r["saved"]:
            lines.append(("F4 > Update pairs pairs them with .fit files.", "label"))
        self.do_bangle_status(lines or [("No recordings on the watch", "label")])

    def do_bangle_models(self):
        """Upload a model (it becomes the one the app uses for its role) or remove one."""
        import prec_bangle as B
        st = getattr(self, "bangle_state", None) or {}
        k = self.pick_left("Models on the watch", ["Upload a model", "Remove a model from the watch"])
        if k is None:
            return self.show_bangle()
        if k == 1:
            if not st:
                return self.show_bangle([("Read the Status first: it lists the models on the watch", "warn")])
            files = [m["file"] for m in st["models"]]
            if not files:
                return self.show_bangle([("There are no models on the watch", "label")])
            j = self.pick_left("Remove which model?", ["%s  (%s)" % (m["file"], m.get("name") or "?") for m in st["models"]])
            if j is None:
                return self.show_bangle()
            if self.bangle_call("Removing", lambda B, prog: B.remove_model(files[j]) or True):
                self.do_bangle_status([("Removed %s" % files[j], "ok")])
            return
        models = [m for m in T.list_models(self.mconf) if m.get("has_js")]
        if not models:
            return self.show_bangle([("No exported models (prec_train.py export NAME makes the watch files)", "warn")])
        names = {"cls": "classifier", "run": "run", "walk": "walk", "speed": "any speed"}
        j = self.pick_left("Upload which model?", [m["name"] for m in models])
        if j is None:
            return self.show_bangle()
        name = models[j]["name"]
        js = os.path.join(os.path.expanduser(self.mconf["paths"]["models"]), name + ".js")
        r = self.bangle_call("Uploading", lambda B, prog: B.upload_model(js, progress=prog))
        if r is not None:
            self.do_bangle_status([("%s uploaded as %s, used for %s" % (name, r[0], names[r[1]]), "ok"),
                                   ("The app's Settings > Models changes the choice (all models must take the same "
                                    "accelerometer rate and range).", "label")])

    def bangle_install(self):
        if self.bangle_call("Installing prec", lambda B, prog: B.install_app("prec", progress=prog)) is not None:
            self.do_bangle_status([("The prec app is installed", "ok")])

    def do_bangle_settings(self):
        """Edit the watch's prec.json (units, filters, recording, cadence)."""
        st = getattr(self, "bangle_state", None)
        s = dict((st or {}).get("settings") or {})
        units, taus, chunks = ["pace", "pacemi", "kmh", "mph", "ms"], [0, 2, 5, 10, 20, 30, 60], [262144, 524288, 1048576]
        names = {"pace": "min/km", "pacemi": "min/mi", "kmh": "km/h", "mph": "mph", "ms": "m/s"}
        for k, v in (("units", "pace"), ("clsLp", 10), ("speedLp", 10), ("record", False), ("logHr", False),
                     ("logCad", False), ("chunk", 524288), ("cadAxis", 0), ("cadLp", 5), ("buffers", 2)):
            s.setdefault(k, v)
        onoff = lambda v: "on" if v else "off"
        keys = [("units", "Units", lambda v: names.get(v, v), lambda v: cyc(units, v)),
                ("clsLp", "Class filter", lambda v: "%s s" % v if v else "off", lambda v: cyc(taus, v)),
                ("speedLp", "Speed filter", lambda v: "%s s" % v if v else "off", lambda v: cyc(taus, v)),
                ("record", "Record raw data", onoff, lambda v: not v),
                ("logHr", "Log heart rate", onoff, lambda v: not v),
                ("logCad", "Log cadence", onoff, lambda v: not v),
                ("chunk", "File size", human, lambda v: cyc(chunks, v)),
                ("cadAxis", "Cadence axis", lambda v: "xyz"[v], lambda v: (v + 1) % 3),
                ("cadLp", "Cadence filter", lambda v: "%s s" % v if v else "off", lambda v: cyc(taus, v)),
                ("buffers", "Buffers", str, lambda v: v % 8 + 1)]
        cyc = lambda lst, v: lst[(lst.index(v) + 1) % len(lst)] if v in lst else lst[0]
        changed = {}
        while True:
            rows = [[("%-16s " % label, "label"), (fmt(s[key]), "text")] for key, label, fmt, _ in keys]
            rows.append([("Save to the watch", "ok")])
            k = self.pick("App settings%s - Enter changes" % ("" if st else " (Status first shows the watch's)"),
                          rows, "Enter change   Esc back")
            if k is None:
                return self.show_bangle()
            if k == len(keys):
                break
            key = keys[k][0]
            s[key] = keys[k][3](s[key])
            changed[key] = s[key]
        if not changed:
            return self.show_bangle([("Nothing changed", "label")])
        r = self.bangle_call("Saving settings", lambda B, prog: B.set_settings(changed))
        if r is not None:
            self.do_bangle_status([("Settings saved on the watch (used the next time a Polar session starts)", "ok")])

    # -- tabs --
    def switch_tab(self, tab):
        """Show another tab; each keeps its own output page and cursor."""
        if tab == self.tab:
            return
        self.pages[self.tab] = {k: getattr(self, k) for k in PAGE_KEYS}
        self.tab = tab
        self.note("")
        if tab in self.pages:
            for k, v in self.pages[tab].items():
                setattr(self, k, v)
        else:
            self.sel, self.scroll = 0, 0
        if tab == 1:
            self.home_overview()
        elif tab == 3:
            self.show_bangle()
        elif tab == 4:
            self.show_pairs()
        elif tab == 5:
            self.show_training()
        elif tab == 2:
            if self.autoconnect:                 # --connect: connect the first time F2 opens
                self.autoconnect = False
                self.draw()
                self.do_connect()
            if not self.feather:
                self.select_connect()            # otherwise wait for Connect (c)

    def select_connect(self):
        """Put the cursor on Connect (used while not connected)."""
        self.sel = len(self.menu()) - 2

    def activate(self, item):
        label, handler, enabled = item[:3]
        if not enabled:
            return self.note(item[3] if len(item) > 3 and self.feather else "Connect first", "warn")
        handler()


def main():
    ap = argparse.ArgumentParser(description="Menu driven terminal UI for the PREC feather.")
    ap.add_argument("--config", default=CONFIG_PATH, help="configuration file (default %(default)s)")
    ap.add_argument("--theme", choices=sorted(THEMES), help="colour theme (overrides the config)")
    ap.add_argument("--usb", nargs="?", const="", metavar="PORT", help="connect over USB serial")
    ap.add_argument("--ble", nargs="?", const="", metavar="ADDRESS", help="connect over Bluetooth")
    ap.add_argument("--data-dir", help="sync directory (overrides the config)")
    ap.add_argument("--prec-conf", help="data settings file (default $PREC_CONF or ~/.config/prec/prec.conf)")
    ap.add_argument("--connect", action="store_true",
                    help="connect to the feather when F2 is first opened (default: wait for Connect)")
    ap.add_argument("--no-connect", action="store_true", help=argparse.SUPPRESS)   # the default now; kept for old scripts
    ap.add_argument("--write-config", action="store_true", help="write a commented default config and exit")
    a = ap.parse_args()
    if a.write_config:
        if os.path.exists(a.config):
            sys.exit("%s exists; not overwriting" % a.config)
        os.makedirs(os.path.dirname(a.config), exist_ok=True)
        with open(a.config, "w") as f:
            f.write(DEFAULT_CONFIG)
        print("wrote", a.config)
        return
    cfg = load_config(a.config, a.theme)
    try:
        for text in cfg["styles"].values():
            parse_style(text)
    except ValueError as e:
        sys.exit("config: %s" % e)
    if a.usb is not None:
        cfg["connection"].update(transport="usb", serial_port=a.usb or cfg["connection"]["serial_port"])
    if a.ble is not None:
        cfg["connection"].update(transport="ble", ble_address=a.ble or cfg["connection"]["ble_address"])
    cfg["merge"] = M.load_conf(a.prec_conf)
    if a.data_dir:
        cfg["sync"]["directory"] = a.data_dir
    if not cfg["sync"]["directory"]:
        cfg["sync"]["directory"] = tilde(cfg["merge"]["paths"]["data"])
    cfg["connection"]["_autoconnect"] = a.connect and not a.no_connect
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: App(scr, cfg).run())


if __name__ == "__main__":
    main()

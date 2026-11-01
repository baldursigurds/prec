# Merging accelerometer data with GPS (`tools/prec_merge.py`, `tools/prec_gui.py`, TUI tab F4)

Each recording from the feather (`.prc`) or a legacy dump (`.bin`) is joined
with the Garmin `.fit` file of the same activity. The result is a numpy
array: every accelerometer sample with the GPS speed at that moment, on the
`.fit` file's time axis.

## Folders and settings

All folders and settings live in `~/.config/prec/prec.conf` (TOML). A
different file can be given with `--prec-conf` (TUI), `--conf` (scripts) or
the `PREC_CONF` environment variable. A missing file or key means the
default. The TUI writes a commented copy under F1 → *Write default config files*.

| Key | Default | Contents |
|---|---|---|
| `paths.data` | `~/prec/data` | PREC streams (`.prc`); the TUI's Sync copies here |
| `paths.gps` | `~/prec/gps` | Garmin `.fit` files |
| `paths.pairs` | `~/prec/pairs` | pair files (`.json`) |
| `paths.cooked` | `~/prec/cooked` | merged arrays (`.npy`) |
| `paths.avoid` | `~/prec/avoid` | avoid rectangles, one `.json` each |
| `paths.legacy` | `~/accrnr/data` | where the legacy `.bin` browser starts |
| `paths.cache` | `~/.cache/prec` | parsed `.fit` files, file time spans, map tiles, `gui.log` |
| `pairing.min_overlap` | 60 | seconds of common time needed for Update to pair two files |
| `pairing.min_samples` | 2000 | recordings with fewer accelerometer samples are ignored by Update |
| `pairing.min_interval` | 10 | seconds; shorter pieces left by avoid rectangles are dropped |
| `offset.step_fine` / `step` / `step_coarse` | 0.1 / 1 / 10 | buttons and keys of the offset window (s) |
| `offset.auto_range` | 300 | *Auto* searches the current offset ± this many seconds |
| `plot.decimate` | 10 | plot every n-th accelerometer sample |
| `plot.acc_x`, `acc_y`, `acc_z`, `speed`, `interval`, `track` | | line colours |
| `plot.run`, `walk`, `still`, `stretch` | green, amber, red, purple | class colours (also of the class buttons) |
| `map.tile_url` | OpenStreetMap | map tile server (`{z}/{x}/{y}`) |
| `map.user_agent` | `prec-tools/1.0 …` | sent to the tile server (OSM requires one) |
| `map.max_tiles` | 36 | tiles per view (picks the zoom level) |
| `legacy.rate` | 200 | nominal rate written into converted legacy streams |
| `paths.models` | `~/prec/models` | trained models (see [TRAIN.md](TRAIN.md)) |
| `paths.arch` | `~/prec/arch` | your model architectures |
| `paths.class` | `~/prec/class` | your classifier architectures |
| `paths.train` | `~/prec/train.json` | training parameters |

## Time

* **`.prc` times.** Each data word carries the Polar's own timestamp (precise
  but on an arbitrary clock) and the feather's arrival time (UTC, but delayed
  by Bluetooth by a few to tens of ms).
  * Sample times are spread evenly between consecutive Polar timestamps.
  * They are mapped to UTC with a straight line fitted through all (Polar,
    feather) pairs. The line is moved down to the 5th percentile of the
    residuals, because arrival delays are only ever positive.
  * This keeps the Polar's exact sample spacing and the feather's absolute
    time.
* **`.fit` times.** These are UTC, one record per second. The *start* of a
  `.fit` file is its session start time, or its first record if earlier.
* **Offset.** Seconds added to the accelerometer times; positive moves the
  acceleration later. It corrects clock differences and GPS lag. Garmin's
  smoothed speed typically trails the real motion by a few seconds; in the
  test data the best offset was +6.4 s.
* **t.** Every time in a pair (intervals, class intervals, the cooked array's
  first column) is in seconds since the `.fit` start.

## Pair files (`~/prec/pairs/<name>.json`)

```json
{
  "version": 1,
  "name": "20261030-111144",
  "source": "prec",                       // or "legacy"
  "prc": "20261030-111144.prc",           // in paths.data
  "fit": "2026-10-30-12-12-13.fit",       // in paths.gps
  "legacy_bin": null,                     // legacy: the original .bin file
  "offset": 6.4,                          // s added to the accelerometer times
  "offset_confirmed": true,               // set by Confirm in the offset window
  "intervals": [[0.004, 600.0], [900.0, 3561.998]],   // sub-intervals, s since the .fit start
  "intervals_auto": true,                 // false once edited by hand
  "run": [[60.0, 2900.0]], "walk": [], "still": [], "stretch": [],
  "cooked": "20261030-111144.npy",        // in paths.cooked
  "columns": ["t", "ax", "ay", "az", "speed"],
  "samples": 731351,
  "t_start": 0.004, "t_end": 3561.998,    // first and last t in the cooked array
  "prc_start": "2026-10-30T11:11:49.899Z", "prc_end": "...",
  "fit_start": "2026-10-30T11:12:13.000Z", "fit_end": "...",
  "overlap_s": 3562.0,
  "created": "...", "updated": "...", "built": "..."
}
```

* **Sub-intervals.** By default there is one, the whole cooked range.
  * **Avoid rectangles:** when a pair is built (and on request after the
    rectangles change), it becomes the maximal pieces of that range during
    which the GPS track is outside every rectangle. Pieces shorter than
    `min_interval` are dropped.
  * **Edited by hand:** once you edit them, `intervals_auto` becomes false
    and they are never recomputed.
* **Class intervals** (`run`, `walk`, `still`, `stretch`) are empty until
  you mark them. `run` and `walk` lie inside the GPS span
  (`t_start`–`t_end`). `still` and `stretch` may extend over the whole
  recording, so their times can be negative or past `t_end`; the cooked
  array does not cover those parts.

## The cooked array (`~/prec/cooked/<name>.npy`)

`numpy.load()` gives a float64 array of shape (m, 5):

| Column | Contents |
|---|---|
| 0 `t` | seconds since the `.fit` start (offset applied) |
| 1–3 `ax`, `ay`, `az` | H10 accelerometer, mG, as recorded |
| 4 `speed` | `.fit` `enhanced_speed`, m/s, linearly interpolated to `t` |

* **Which samples.** Only accelerometer samples between the first and last
  valid speed record are kept (m ≤ n). Records without speed are skipped
  before interpolating.
* **Cooking.** The array is rebuilt, together with the class arrays below,
  in these cases:
  * when a pair is created;
  * after a confirmed offset;
  * after class intervals are saved;
  * with F4 → *Cook* (`c`).

### Class arrays (`~/prec/cooked/<name>_<class>.npy`)

Cooking also writes one array per class that has intervals:

| File | Shape | Contents |
|---|---|---|
| `<name>_run.npy`, `<name>_walk.npy` | (m, 5): `t, ax, ay, az, speed` | the rows of the cooked array inside the run or walk intervals |
| `<name>_still.npy`, `<name>_stretch.npy` | (m, 4): `t, ax, ay, az` | the samples of the *whole* recording inside the still or stretch intervals; no speed, as these may lie outside the GPS span |

* **Joined intervals.** The intervals of a class follow each other in one
  array; look for jumps in `t` to find where one interval ends and the next
  begins.
* **Kept in sync.** A class without intervals has no file, and a file left
  from earlier is deleted when the pair is cooked.
* **In the pair.** The pair's `class_arrays` holds the number of samples
  per class, e.g. `{"run": 143891, "still": 2981}`.

```python
import json, numpy as np
pair = json.load(open("/home/you/prec/pairs/20261030-111144.json"))
a = np.load("/home/you/prec/cooked/" + pair["cooked"])          # everything with GPS
run = np.load("/home/you/prec/cooked/20261030-111144_run.npy")   # t, ax, ay, az, speed
still = np.load("/home/you/prec/cooked/20261030-111144_still.npy")  # t, ax, ay, az
```

## Avoid rectangles (`~/prec/avoid/<name>.json`)

`{"name": "park", "south": 40.37371, "north": 40.37771, "west": -3.70120, "east": -3.69455, "saved": "..."}`
(degrees, WGS84). Time spent inside any rectangle is left out of
automatically computed sub-intervals (see above).

## TUI tab F4 (Data)

The left side holds the commands; the right side lists the pairs:

| Column | Meaning |
|---|---|
| name | the pair |
| time | the `.fit` start (local time) |
| length | in minutes |
| offset | green when confirmed, yellow `?` when not |
| `iv` | number of sub-intervals; `*` means edited by hand |
| R/W/S/T | counts of run/walk/still/stretch intervals |

When a command needs a pair, the menu is replaced by the list of pairs (Enter
chooses, Esc goes back). Windows open on the desktop, and the TUI waits until
they are closed.

| Key | Command | What it does |
|---|---|---|
| `u` | Update pairs | For every recording in `paths.data` that is not paired yet, finds the `.fit` file in `paths.gps` with the most time in common (at least `min_overlap`), creates the pair and builds it. Reports recordings without a match. Offsets start at 0, unconfirmed. |
| `n` | New pair | Choose a recording (with its time and whether it is paired), then a `.fit` file (sorted by overlap with it). Creates and builds the pair, then opens the offset window. |
| `o` | Offset | Offset window (below). Confirm saves the offset and rebuilds the array. |
| `p` | Plot | Acceleration and speed of the cooked array, sub-intervals shaded, class intervals as coloured bands at the top. |
| `i` | Intervals | Interval editor for the sub-intervals, with the map. |
| `k` | Class intervals | Choose the pair on the right (with its run, walk, still and stretch counts): one editor for all four classes, with the map. |
| `a` | Avoid: new rectangles | Choose the track to centre the map on (or all), then draw rectangles. Afterwards you are asked whether to re-apply them to pairs with automatic intervals. |
| `e` | Edit avoid rectangles | Choose a rectangle (with its size in metres), then move, resize or delete it on the map. Same question afterwards. |
| `l` | Legacy binary import | Browse to a `.bin` file (from `paths.legacy`), then to its `.fit` (the browser starts in the `.bin` folder), then give the offset (s from the `.fit` start to the first `.bin` sample). See below. |
| `c` | Cook | Rebuild the cooked array and the class arrays (`_run`, `_walk`, `_still`, `_stretch`) of one pair or all; shows the samples per class. |
| `x` | Delete pair | Deletes only the pair `.json`; the `.prc`, `.fit` and cooked array stay. |

## Windows (`prec_gui.py`)

All windows have matplotlib's toolbar for zooming and panning; map tiles
reload at the new zoom.

**Offset**
* **What it shows:** acceleration x/y/z on the left axis and `.fit` speed on
  the right axis, against `t`.
* **Changing the offset:** type it, or use the −10, −1, −0.1, +0.1, +1, +10
  buttons. Keys: ←/→ ±1 s, Shift+←/→ ±10 s, `,`/`.` ±0.1 s.
* **Auto** (key `a`) looks within ±`auto_range` for the offset where the
  per-second spread of |acceleration| correlates best with speed, and shows
  that correlation. On the test runs it found +6.4 s (r = 0.90) and −7.9 s
  (r = 0.80, legacy).
* **Confirm** (Enter) saves the offset; **Cancel** (Esc) leaves the pair
  unchanged.

**Interval editor** (sub-intervals, or the class intervals)
* **Layout:** the plot is on top and the map below. The map shows the whole
  track and the intervals being edited in their colour.
* **Class buttons** (class intervals only): *1 Run* (green), *2 Walk*
  (amber), *3 Still* (red) and *4 Stretch* (purple).
  * The highlighted button (full colour, bold, framed) is the class that new
    intervals get. Click another button, or press `1`–`4`, to switch.
  * After clicking an interval to select it, a button also changes that
    interval's class. A freshly drawn or moved interval is not changed this
    way, so you can draw and then switch class.
  * **Whole recording.** The class editor plots all accelerometer data of
    the recording, read from its `.prc` and shifted by the pair's offset,
    including the parts before and after the `.fit` track. Those parts have
    no GPS and are shaded grey.
    * **Still** and **stretch** intervals may lie anywhere, so they can
      cover warm-up or stretching done before the watch recorded.
    * **Run** and **walk** intervals need speed, so they are clipped to the
      GPS span.
  * **Classes never overlap.** Drawing, moving or reclassifying an interval
    takes its time away from the other classes: drawing *walk* across
    the end of a *run* shortens the run.
* **Mouse in the plot:**
  * Drag an interval edge to move it.
  * Drag on empty space to add an interval.
  * Click an interval to select it.
* **Keys:**
  * `x` splits the selected interval at the mouse position.
  * `d` or Delete deletes it.
  * Ctrl+S saves; Save and Revert are also buttons.
  * `q` closes; unsaved changes are discarded.
* **Map:** moving over the plot shows that moment's position on the map.
  Clicking the map marks that time in the plot.
* **Overlaps:** overlapping intervals of the same kind are merged. Pieces
  that only touch, such as the two halves of a split, stay separate.

**Avoid**
* **Map:** shows the chosen tracks and the existing rectangles (red).
* **New rectangle:** drag it (it can be adjusted with its handles), type a
  name, and press Save; repeat for more.
* **Editing one rectangle:** move or resize it and Save, or Delete it.
  Renaming it saves it under the new name.

Command line: `prec_gui.py [--conf FILE] [--png FILE] offset|plot|intervals NAME`,
`classes NAME [run|walk|still|stretch]` (the class new intervals get first; default run), `avoid [--pair NAME]...`, `avoid-edit RECT`.
Exit status: 0 saved, 1 closed without saving, 2 error. `--png` draws the
window into a file and exits. The windows' messages go to
`~/.cache/prec/gui.log` when started from the TUI.

## Legacy `.bin` files

These are raw PMD accelerometer notifications written back to back by the
earlier logger. There are no lengths and no PREC headers; each packet is
`02 <u64 Polar timestamp> <frame type> <samples>`. In the examples each
packet is 124 bytes (frame type 1, 19 samples of x/y/z int16), followed by
`FF` filler up to a fixed file size (7 MiB). The packet size is detected
from the data.

Such files carry no real time; the H10's clock starts at 2019-01-01 when it
was never set. The import therefore:

1. **Copies the `.fit`** into `paths.gps`, named by its start time
   (`YYYY-MM-DD-HH-MM-SS.fit`, UTC), because legacy folders all call it
   `garmin.fit`.
2. **Writes a PREC stream** `paths.data/<YYYYMMDD-HHMMSS>-legacy.prc`. Its
   first sample is at the `.fit` start + the given offset; the following
   samples follow the Polar clock exactly. It contains SESSION_START (name
   `legacy sensors.bin`), STREAM_START (ACC, `legacy.rate`, 16 bit), a
   MESSAGE with the source path, one ACC word per packet, then STREAM_STOP
   and SESSION_END.
3. **Creates and builds the pair** (`source: "legacy"`,
   `legacy_bin`, `legacy_offset`) with offset 0, then opens the offset
   window. Later corrections go into the pair's offset like for any pair.

## `prec_merge.py`

Command line: `list`, `update`, `cook NAME|--all` (`build` is the old name), `delete NAME`, `offset NAME`
(prints the *Auto* estimate), `legacy BIN FIT [--offset S]`, `write-config`;
`--conf FILE` for another settings file.

| Function | Description |
|---|---|
| `load_conf(path=None)`, `write_default_conf(path=None)`, `ensure_dirs(conf)` | settings and folders |
| `read_prc(path)` | `{"utc", "polar", "acc" (n,3), "rate", "start", "end", "frames"}` with UTC sample times (see *Time*) |
| `prc_span(path)` | quick `(start, end, samples)` from the word headers |
| `clock_map(polar_ns, rtc_ms)` | the Polar → UTC mapping function |
| `read_legacy_bin(path)` | packets, per-sample Polar times and acceleration of a legacy dump |
| `legacy_to_prc(bin, out, start_utc, rate=200)` | convert a legacy dump into a PREC stream |
| `read_fit(path, conf=None)` | `{"start", "end", "t", "speed", "lat", "lon", "sport"}` (cached when conf is given) |
| `import_fit(conf, path)` | copy a `.fit` into the gps folder under its start time |
| `recordings(conf)`, `gps_files(conf, span=None)` | the files with their time spans (cached in `spans.json`) and pairing state / overlap |
| `new_pair`, `load_pair`, `save_pair`, `list_pairs`, `delete_pair`, `unique_name` | pair files |
| `merge(acc, fit, offset)` | the (m, 5) array |
| `build_pair(conf, pair)` | cook: write the cooked array, the class arrays, the spans and (while automatic) the sub-intervals; save the pair |
| `load_class_array(conf, pair, cls)` | a class array (`run`, `walk`: t, ax, ay, az, speed; `still`, `stretch`: t, ax, ay, az), or None |
| `load_cooked(conf, pair)` | the cooked array |
| `update(conf, progress=None)` | the Update command |
| `import_legacy(conf, bin, fit, offset)` | the legacy import |
| `estimate_offset(acc, fit, center, search)` | the *Auto* estimate: `(offset, correlation)` |
| `list_avoid`, `save_avoid`, `delete_avoid`, `new_avoid_name` | avoid rectangles |
| `inside_any(lat, lon, rects)`, `auto_intervals(fit, t_lo, t_hi, rects, min_len)`, `reapply_avoid(conf)` | interval computation |
| `normalize_intervals(iv, lo, hi)` | sort, clip and merge overlapping intervals |

`prec_gui.py` provides `OffsetWindow`, `PlotWindow`, `IntervalEditor`,
`AvoidWindow`, `MapLayer` (OSM background that follows zoom and pan),
`map_image`, `fetch_tile`, `merc`/`unmerc` (Web Mercator) and `plot_series`.

# Training a speed model and running it on the Bangle.js 2 (`tools/prec_train.py`, TUI tab F5, the prec app)

The models estimate running speed (m/s) from a short window of Polar H10
accelerometer data. They are trained on the merged pairs from F4 (see
[MERGE.md](MERGE.md)), exported to TensorFlow Lite, and run on the watch by the
**prec** app, which reads the H10 directly (*Polar H10* in its menu), or in
the feather.

## Overview

1. **Pairs.** In F4, make pairs and check their offsets and intervals.
2. **Roles.** In F5 → *Roles*, assign pairs to the four roles: **train**,
   **valid** (validation), **test** and **quant** (calibration for int8).
   * **One list.** All pairs are shown in one list. On the highlighted pair,
     `t`, `v`, `e` and `q` toggle train, valid, test and quant; each change
     is saved immediately, and Esc returns.
   * **Colours.** Every row shows its roles as T V E Q markers: train green,
     valid amber, test red, quant purple. The pair name takes the colour of
     its first role.
   * **Several roles.** A pair can have more than one.
   * **Theming.** The colours are the theme elements `role_train`,
     `role_valid`, `role_test` and `role_quant` in `tui.toml`. Amber and
     purple are 256-colour codes 214 and 135; on 8-colour terminals they
     show as yellow and magenta.
3. **Parameters.** Choose them (window, features, strategy,
   architecture...) and check the label distribution with **Histogram**.
4. **Train.** Choose *Train new model* (or *Retrain a model* for more
   epochs on a saved one). Training runs in the background; *Training log*
   follows it.
   The result is `~/prec/models/model_yymmdd_hhmmss.{keras,json,tflite,c,js}`.
5. **F3 → Models → Upload a model** puts a model on the watch as `prm_<id>`
   (the `.js` module) and `prm_<id>.tfl` (the `.tflite`). The app uses it
   from then on for its role: a classifier as the classifier, `*_run` for
   running, `*_walk` for walking, anything else as the speed model for any
   activity. The app's *Settings → Models* changes the choice.
6. **F3 → Install prec app**, then choose *Polar H10* in the app with the
   strap on.

## Parameters (`~/prec/train.json`)

They are edited in F5 → *Parameters*, or with `prec_train.py set KEY=VALUE`.
Defaults are shown below. The file's location is `paths.train` in
`prec.conf`, which also has `paths.models` (`~/prec/models`) and
`paths.arch` (`~/prec/arch`) and `paths.class` (`~/prec/class`, classifier
architectures).

| Key | Default | Meaning |
|---|---|---|
| `roles.train`, `.valid`, `.test`, `.quant` | `[]` | pair names per role |
| `window` | 256 | samples per window |
| `shift` | 1 | samples between the starts of consecutive windows |
| `stride` | 1 | inside a window, every `stride`-th sample is used. `rate/stride` must be a Polar ACC rate (25, 50, 100, 200 Hz), because the watch requests exactly that rate. |
| `batch.train`, `.valid`, `.test`, `.quant` | 32, 2048, 2048, 2048 | batch sizes |
| `min_speed`, `max_speed` | 0.0, 7.0 | m/s: training windows with labels outside are discarded |
| `resolution` | 0.1 | m/s: bin width for the strategy and the histogram |
| `strategy` | `weight` | `weight`: every bin in [min_speed, max_speed] gets the same total weight (mean weight 1). `pick`: the same number of windows from every bin, unweighted. |
| `pick_per_bin` | 0 | for `pick`: windows per bin; 0 = as many as the smallest bin has (bins with fewer are sampled with replacement) |
| `target` | `mean` | label of a window: `mean`, `last` or `center` speed |
| `dft` | 0 | 0: the window itself; n > 0: the first n DFT coefficients of each axis. An architecture may set it. |
| `dft_abs` | true | with dft > 0: magnitudes (else real and imaginary parts). An architecture may set it. |
| `window_fn` | `hann` | window multiplied in before the DFT (any `scipy.signal.get_window` name; `boxcar` = none) |
| `arch` | `flat3` | model architecture (below) |
| `class_arch` | `dense3` | classifier architecture (below) |
| `epochs` | 20 | |
| `learning_rate` | 0.001 | Adam; drops 5× after `lr_patience` epochs without improvement |
| `loss` | `mse` | `mse`, `mae` or `huber` |
| `lr_patience`, `early_stop` | 2, 6 | epochs; early stopping restores the best weights (0 = off) |
| `norm_samples` | 50000 | random training windows used for the normalisation |
| `quantize` | false | int8 model calibrated with `quant_samples` windows of the quant pairs (falls back to train pairs) |
| `quant_samples` | 500 | |
| `rate` | 200 | Hz of the recordings |
| `range` | 8 | G: Polar ACC range the watch requests (use the recordings' range) |
| `arena` | 0 | TensorFlow memory on the watch in bytes (0 = estimated) |
| `allowed_ops` | (measured list) | TFLite ops the export accepts; see *Watch limits* |

## Datasets

* **Building the windows.** For every pair of a role, each of its
  sub-intervals is cut into windows: a window starts every `shift` samples
  and has `window` samples taken `stride` apart. The label is the GPS speed
  of the window (see `target`).
* **Lazy windows.** Windows are not copied. A `tf.data` pipeline gathers each
  batch from the pair arrays on the fly, so a shift of 1 (one window per
  sample) costs no extra memory.
* **Training role only:** windows outside [min_speed, max_speed] are dropped
  and the strategy is applied. Validation, test and quant data are used as
  they are.
* **Histogram** (F5) shows, for a role, the number of windows per
  `resolution` bin of the label, as bars of log(n+1). For train this is
  after the speed limits and, with `pick`, after picking.

## Features and normalisation

* **dft = 0:** the window (window × 3), each axis normalised by the training
  mean and standard deviation of that axis.
* **dft > 0:**
  1. Each axis is multiplied by the window function.
  2. It is Fourier transformed (real FFT of length `window`, which must be a
     power of two) and truncated to the first `dft` coefficients.
  3. With `dft_abs` this gives magnitudes (dft × 3); otherwise
     [re x, re y, re z, im x, im y, im z] (dft × 6).
  4. Every coefficient of every axis is normalised separately.
* **Normalisation statistics:** mean and standard deviation come from
  `norm_samples` random training windows and are stored in the model `.json`.
* **Three implementations:** the features exist in numpy (statistics),
  TensorFlow (training data) and C (the watch). The export compiles the C
  code for the laptop as well and checks that it agrees with numpy (in tests:
  difference < 1e-6).

## Architectures

An architecture is a Python file with

```python
DFT = 20           # optional: overrides the dft parameter
DFT_ABS = True     # optional: overrides dft_abs
def create_model(input_shape):   # (window, 3), (dft, 3) or (dft, 6)
    ...return an (uncompiled) keras model with one output: the speed
```

Built in (`tools/prec_arch/`):

| Name | Input | Layers |
|---|---|---|
| `flat3` | 20 DFT magnitudes × 3 | Flatten, Dense 64 relu, Dense 32 relu, Dense 1 |
| `conv3` | 256 × 3 raw samples | Reshape to (256, 1, 3), three Conv2D (8/16/16 filters, kernel (8, 1), stride (2, 1), relu), Flatten, Dense 1 |

Your own go in `~/prec/arch/<name>.py`; a file with a built-in name replaces
the built-in one. F5 → *Architectures* lists them, and Enter selects one.

**Classifier architectures** work the same way, but are kept apart. They
live in `tools/prec_class/` (built in) and `~/prec/class/<name>.py` (yours),
and the parameter `class_arch` picks one (F5 → *Parameters* → *Classifier
arch*, or Enter on it in *Architectures*). Their `create_model` gets the
number of classes, and the model must end in a softmax over them:

```python
DFT = 20           # optional, as above
DFT_ABS = True
def create_model(input_shape, n_classes):   # n_classes = 4: run, walk, still, stretch
    ...return an (uncompiled) keras model ending in Dense(n_classes, activation="softmax")
```

| Name | Input | Layers |
|---|---|---|
| `dense3` (example) | 20 DFT magnitudes × 3 | Flatten, Dense 64 relu, Dense 32 relu, Dense 4 softmax |

### Watch limits

The TensorFlow Lite Micro on the Bangle.js 2 (firmware 2v29) only has
certain ops. The list was **measured on the watch**:

| Works | Missing (model fails to load) |
|---|---|
| FULLY_CONNECTED, CONV_2D, DEPTHWISE_CONV_2D, MAX_POOL_2D, AVERAGE_POOL_2D, RESHAPE, SOFTMAX, QUANTIZE, DEQUANTIZE; fused ReLU/ReLU6 activations | SHAPE, STRIDED_SLICE/PACK (dynamic shapes), ADD, EXPAND_DIMS, MEAN, TANH, LOGISTIC (sigmoid), LEAKY_RELU |

Consequences:

* **No Conv1D.** It needs EXPAND_DIMS; use Conv2D on a (window, 1, 3)
  reshape with (k, 1) kernels instead, like `conv3`.
* **No global pooling** (MEAN) and **no residual connections** (ADD).
* **Activations:** only relu/relu6, and softmax.
* **Fixed batch size.** The export converts the model with batch size 1, so
  that Flatten becomes a plain RESHAPE.
* **Op check.** After converting, the export lists the model's ops and
  refuses to write the `.js` if any is not in `allowed_ops`. The `.keras`
  and `.tflite` are kept for inspection.

## Training and the saved files

*Train* (`t`) offers two choices on the right:

* **Train new model** trains a new model from scratch with the parameters
  set under *Parameters* (`p`) and the pairs under *Roles* (`r`).
* **Train run** and **Train walk** train a new model the same way, but on
  the run or walk intervals only. These use the pairs' `_run` or `_walk`
  class arrays (see [MERGE.md](MERGE.md)).
  * **Windows.** Windows lie inside both the class intervals and the
    pair's sub-intervals, so avoid rectangles still apply.
  * **Pairs.** Pairs without that class are left out, in every role, and the
    log shows `train (run only): … windows`. If no train pair has the class,
    the TUI says so and nothing starts.
  * **Not cooked yet.** A pair cooked before its class intervals were drawn
    is cooked first.
  * **Names.** The model's name ends in `_run` or `_walk`
    (`model_yymmdd_hhmmss_run`). Its `.json` has `"class": "run"` (`null`
    for ordinary models), and its `params` hold `cls`.
  * **Retraining.** Retraining such a model stays on its class and keeps
    the suffix.
* **Retrain a model** continues training a saved model for more epochs.
  1. Pick a model from the list (only models that still have their
     `.keras`).
  2. Enter the number of extra epochs (default: that model's epochs
     parameter).
  3. Enter a learning rate. Leave it empty to keep the rate where the
     model stopped, including any reductions on plateaus.

  What the retrain uses:

  * **From the model's `.json`:** the parameters, the pairs of each role
    and the normalisation (mean and std). The current *Parameters* and
    *Roles* are ignored, so the input stays the one the model was trained
    on. The pairs' current files are read, though, so changed offsets or
    avoid regions do apply.
  * **From its `.keras`:** the weights and the optimizer state.
  * **Keys missing from an older `.json`** come from the current parameters,
    and the log names them.

  The result is a new model (`model_yymmdd_hhmmss`); the old one is not
  touched. Epochs carry on from the old count (*Epoch 9/10* after 8), and
  the history in the `.json` holds the old epochs followed by the new ones.
  The `.json` also gets `parent` (the model it came from) and
  `parent_epochs`.

  Early stopping restores the best epoch *of this run*: retraining
  always keeps new weights, even if no new epoch beats the old model's
  best. The old history ends with its last epoch, while its saved weights
  may come from an earlier epoch (its kept epoch); the new run starts from
  those saved weights.

Both start `prec_train.py` in the background and write
`~/.cache/prec/train.log`; *Training log* follows it (Esc returns,
training continues; `s` stops it).

* **Progress bar.** The log shows TensorFlow/Keras' progress bar for every
  epoch: the step counter, a bar, the time left, and the running loss and
  MAE, updated live.
* **End of an epoch.** When an epoch ends, its line keeps the final values,
  validation loss and MAE included.
* **Wrapping.** Lines wider than the pane wrap at the ` - ` between metrics,
  with continuation lines indented, so loss, MAE or accuracy, validation
  values and learning rate all stay visible.
* **Command line.** `prec_train.py train [--class run|walk]` and `prec_train.py retrain NAME
  [--epochs N] [--lr X]` show the usual Keras bar.

It produces, in `~/prec/models`:

| File | Contents |
|---|---|
| `model_yymmdd_hhmmss.keras` | the Keras model |
| `.json` | everything needed to rebuild the input (see below), plus the training history, test loss and MAE |
| `.tflite` | the converted model (float32, or int8 with float input/output) |
| `.c` | the C feature code with its built-in tables |
| `.js` | the watch module |

The `.json` holds: `name`, `arch`, `window`, `shift`, `stride`, `rate`,
`frequency` (= rate/stride), `range`, `dft`, `dft_abs`, `window_fn`,
`window_coeffs` (the window as a list of floats), `input_shape`, `mean`,
`std`, `target`, `min_speed`, `max_speed`, `resolution`, `strategy`, `loss`,
`roles`, `windows` (count per role), `params` (all training parameters),
`history`, `kept_epoch` (the epoch whose weights were saved), `parent` and
`parent_epochs` (retrained models only), `test_loss`/`test_mae`, `tflite_bytes`, `quantized`, `ops`,
`arena`, `js_bytes`.

`prec_train.py export NAME` redoes the `.tflite`, `.c` and `.js` of a saved model.

In F5 → *Models*, `h` on a model opens its **training history**, read from
its `.json` (no TensorFlow needed). Closing it returns to the list.

* **Plots:** training and validation loss, training and validation MAE, and
  the learning rate (log scale) per epoch.
* **Kept epoch:** a dashed line marks the epoch whose weights were saved.
  That is the lowest `val_loss` (or training `loss` without validation
  pairs) when early stopping is on, otherwise the last epoch. It is read
  from `kept_epoch`; older `.json` files without that key fall back to the
  same rule. The title gives that epoch's values and the test MAE.
* **Retrained models:** the epochs from the parent model are shaded grey,
  and the title names the parent.
* **Command line:** `prec_gui.py history MODEL`.

## Classifier (run, walk, still, stretch)

F5 → *Train* → **Train classifier** trains a model that tells the four
classes apart.

* **Input.** The same windows and features as the speed models (window,
  stride, shift, DFT, normalisation from the parameters). The DFT can be
  overridden by the classifier architecture.
* **Target.** The class of the interval a window lies in: 0 run, 1 walk,
  2 still, 3 stretch.
* **Data.** The pairs' class arrays (`_run`, `_walk`, `_still`, `_stretch`,
  see [MERGE.md](MERGE.md)), for the pairs of each role. Each window lies
  inside one class interval.
  * Still and stretch windows may come from parts of the recording without
    GPS. Speed limits, strategy and sub-intervals do not apply.
  * Pairs without class intervals are left out.
  * A pair cooked before its class intervals were drawn is cooked first.
* **Balance.** In the train role every class present gets the same total
  weight, so a long run does not drown out a short stretch.
* **Training.** Loss `sparse_categorical_crossentropy`, metric accuracy
  (plus weighted accuracy). Learning rate, plateaus, early stopping and
  epochs as for the speed models.
* **Result.** `model_yymmdd_hhmmss_classifier.keras` and `.json`. The
  `.json` has `"kind": "classifier"`, `classes`, `test_accuracy` and
  `confusion`: test windows counted by true class (rows) and predicted
  class (columns), also printed in the log.
  * Like speed models, classifiers are exported to `.tflite`, `.c` and `.js`
    (ops FULLY_CONNECTED, RESHAPE, SOFTMAX for `dense3`). They go on the
    watch (F3 → Models, as `prm_<id>_cls`) and into the feather as its
    classifier (below), and
    *Predict on a pair* takes them too.
  * *Models* shows validation and test accuracy and, with Enter, the
    confusion matrix. `h` plots loss and accuracy.
  * *Retrain a model* continues a classifier like any other model.
* **Command line:** `prec_train.py train --classifier`.

**Predicting with a classifier.** Choose a classifier in the *Predict on a
pair* form (below). Its window works differently from the speed one:

* **Whole recording.** It runs over the pair's whole recording, read from
  the `.prc`. Still and stretch may lie outside the GPS span.
* **Top:** acceleration, and the speed where there is GPS, kept low.
* **Below it, sharing the time axis:**
  * **actual**: the class intervals as a band in the class colours (run
    green, walk amber, still red, stretch purple);
  * **predicted**: a pair of graphs, the most likely class of every window
    as a band, and under it the classifier's output, the four class
    probabilities (they add up to 1) as lines in the class colours;
  * **low-pass** filters: each one in the form adds such a pair. The
    probabilities are each smoothed with that time constant, then the most
    likely class is taken.

  Every band gives its accuracy over the windows that lie in a class
  interval. The probabilities show how sure the model is and where two
  classes compete. The window grows with the number of filters.
* **Right:** the confusion matrix of those windows: rows actual, columns
  predicted, shaded by the share of the row.
* **Results.** The `predict:` lines (accuracy, and accuracy per low-pass)
  appear in F5 as for speed models.
* **Function.** `classify_pair(conf, model, pair, shift=None, lowpass=())`
  in `prec_train.py`; it returns the raw probabilities (`prob`) and, per
  time constant, the smoothed ones (`lowpass_prob`) and their classes
  (`lowpass`).

## Predict (checking a model on a pair)

F5 → *Predict on a pair* (`d`) shows a form on the right:

| Row | Default | Enter |
|-----|---------|-------|
| Model | the newest model | pick a model from the list on the left |
| Pair | the model's first test pair (else the first test pair in the current roles, else the first pair) | pick a pair (see the colours below) |
| Shift | auto: at most 20,000 windows, never closer than the model's shift | type the samples between windows (empty or 0 = auto) |
| Low-pass | none | — (`d` deletes the highlighted filter) |
| ▶ Plot | | open the window (`p` does this from any row) |

`l` asks for a time constant τ in seconds and adds a *Low-pass* row; press
it again for more filters. Every filter adds a curve to the plot: the
prediction through a causal first-order low-pass,
y ← y + (1 − e^(−Δt/τ))·(x − y), started from the first prediction and
restarted after each gap between intervals. Its MAE against the target is in
the legend and in the result lines below the form. The form keeps its values
until the TUI exits; Esc goes back to the menu.

In the pair list, every pair is coloured by the role it had when
*that* model was trained: train green, valid amber, test red, quant purple,
using the same `role_*` colours as the Roles list. The role letters
(`T V E Q`) stand in front of the name; pairs the model never saw are plain.
This makes it easy to tell a check on unseen data from one on training data.
The window shows:

* **Left: speed over time.** The model's prediction (red), the low-passed
  predictions (one colour each) and the GPS target speed (black) against seconds since the `.fit` start. The pair's
  sub-intervals are shaded and its class intervals are shown as bands; lines
  break between intervals.
* **Run and walk models.** A `_run` or `_walk` model still predicts the
  whole pair. Its class intervals get a background in the class colour:
  green for run, amber for walk. The title says so, and an extra result
  line gives the MAE and bias inside those intervals, the part the model
  was trained for.
* **Right: predicted against target.** A scatter plot with the y = x line,
  and MAE, RMSE, bias (mean of prediction − target) and the correlation r.
  Each dot has the colour of the class interval its window centre lies in
  (run green, walk amber, still red, stretch purple), or blue outside the
  class intervals; the legend counts the windows of each.

How it works:

* **Same input as training.** The `.keras` model gets exactly its training
  input: the window, stride, features and normalisation come from its
  `.json`. The targets are the window labels (its `target`) over the pair's
  sub-intervals.
* **Thinning.** With Shift on auto, at most 20,000 windows are used, evenly
  spaced and never closer than the model's shift; the window shows the step.
  A Shift given in the form is used as it is (a small shift on a long pair
  means many windows and a slower plot). The low-pass time step Δt follows
  the shift, so τ means the same in seconds whatever the shift.
* **Results.** The metrics also appear in F5 after the window closes.
* **Command line.** `prec_train.py predict MODEL PAIR [--shift N]
  [--lowpass TAU]... [--csv FILE]` prints the metrics and can save
  `t, target, prediction, lowpass_TAU...`; `prec_gui.py predict MODEL PAIR`
  takes the same `--shift` and `--lowpass` and opens the window.

## The watch module (`.js`, Storage file `prm_<id>`)

```js
var M = require("prm_261031_142534_cls");
M.NAME, M.KIND ("speed" | "classifier"), M.CLASSES, M.NOUT, M.FREQ, M.WIN, M.CH (3),
M.RANGE, M.DFT, M.DFT_ABS, M.NIN, M.ARENA, M.UNIT
M.MODEL_FILE = "prm_261031_142534_cls.tfl";   // its .tflite in Storage (the prec app sets it)
M.init([arena])                  // load it into TensorFlow (retries with 2x, 4x arena)
var buf = M.newBuffer();         // Float32Array(WIN*3), flat
M.buffer_data(x, E.getAddressOf(buf, true), a, b)  // x: address of int16 x,y,z data
var out = M.run(buf);            // the NOUT outputs: class probabilities, or [speed]
var speed = M.predict(buf);      // run(buf)[0]: m/s (speed models)
var mag = M.spectrum(0);         // DFT models: |X_k| of axis 0 from the last run() (cadence)
M.free();                        // drop the TensorFlow instance; init() makes it again
```

* **`buffer_data(x, buf, a, b)`** is an `E.compiledC` function. It takes
  `b − a` int16 values at address `x` (interleaved x, y, z, as they arrive
  from the Polar), converts them to float, multiplies them by the window
  (`WINDOW[i/3]`, a `static const float` table) and writes them to
  `buf[a..b)`.
* **`predict(buf)`** calls the compiled `features(buf, out, work)`: an
  in-place radix-2 FFT with hard-coded cosine/sine and bit-reversal tables,
  truncation, magnitudes, and normalisation by the built-in mean and 1/std
  tables. It then copies the features into the TensorFlow input, runs the
  model and returns the output.
* **`spectrum(ch)`** calls the compiled `spectrum(feat, out, ch)`, which
  undoes the normalisation of the last features for one axis and returns
  the DFT magnitudes (`DFT` floats). The prec app and the feather take the
  cadence from them. Modules exported before it existed have no
  `spectrum`, and the cadence is then counted.
* **Compiling.** The C code is compiled at export time with
  `arm-none-eabi-gcc` (Cortex-M4, FPU, position independent, no
  relocations). The result is embedded the way the Espruino IDE embeds
  `E.compiledC` code: base64 machine code bound with `E.nativeCall(offset,
  signature, code)`.
* **Separate model file.** The model is read from Storage (`MODEL_FILE`)
  rather than stored inside the module, so it is in RAM only once.
* **Measured on the watch:**

  | Model | Predict time | RAM after `init` |
  |---|---|---|
  | `flat3` float32 (26 KB) | 10 ms | 41 KB |
  | `conv3` int8 (9 KB) | 230 ms | 24 KB |

  In both cases the prediction equalled the laptop's TFLite output for the
  same window (2.4962893 and 2.5744979).

## Running the models on the watch

The prec app's *Polar H10* runs the models: classifier, run / walk / any
speed model, class and speed filters, distance, cadence and recording. See
[BANGLE.md](BANGLE.md), *Speed on the watch*.

* **Buffers.** Each model has `buffers` window buffers (1–8, default 2)
  whose starts are WIN/buffers samples apart. Every accelerometer
  notification is copied into them with `buffer_data`. A full buffer is
  predicted at once and then refilled.
* **Switching.** Switching class frees the old speed model and loads the
  new one. Its first value comes once its window has filled (about 1.3 s at
  200 Hz).
* **Older modules.** Modules exported before `run()`, `free()` and
  `spectrum()` existed still work (as `pmodel`, `prun`, ... or under new
  names).
* **Tested on the watch.** The session code ran with recorded H10 frames
  sent through the console instead of a live strap:
  * Classifier + run + walk: the class settled on run, and the run model
    was loaded. The filtered speed and the distance were updated, and the
    cadence from the classifier's spectrum read 172–176 steps/min. 35 KB of
    RAM stayed free.
  * With the spectrum switched off, counting gave 170–171 steps/min.
  * Recording with heart rate and cadence logging: the synced file decoded
    to the 9000 samples sent, 50 HEART_RATE and 49 CADENCE words.
  * Live with the H10 and the old `pmodel`: heart rate, speed, distance
    and time on the data screen.

## The model in the feather

The feather can run models too: while it logs from the H10, it predicts
the speed from the live accelerometer stream. The pipeline is the same as
on the watch: an optional classifier, whose low-passed class picks the run or
walk model (still and stretch give speed 0), then the speed low-pass, then
the distance, and the cadence (from a DFT model's spectrum, otherwise
counted; see [FIRMWARE.md](FIRMWARE.md)). The prec app's *Display* and the
TUI's monitor show speed, class, distance, cadence and running time.

1. **Choose and build.** F5 → *Build models into feather* asks, in turn,
   for:
   * a classifier (or none);
   * with a classifier, the run and walk models (`_run` / `_walk` models
     are listed first);
   * a speed model for anything: the only model without a classifier, the
     fallback with one.

   From the command line: `prec_train.py feather [SPEED|none] [--classifier
   C] [--run R] [--walk W]`.
   1. Each model's layers and feature code go into
      `firmware/prec_logger/model_data.h`, each in its own C++ namespace.
      The table `MODEL_SLOTS` lists them for `predictor.cpp`. All the models
      must share one rate, because the Polar streams at one.
   2. That header is compiled on the laptop (as C++) and every model is
      compared with Keras on test windows. The build refuses if any output
      differs by more than 0.001.
   3. The firmware is compiled and flashed over USB; a USB connection from
      the TUI is closed first. Three flat3/dense3 models take 36 % of the
      flash and 39 % of the RAM.
2. **Rates.** The ACC rate being logged must be a whole multiple of the
   model's frequency. For example, 200 Hz logging works with a 200 Hz or a
   100 Hz model; otherwise `mstate` says the rate does not fit.
3. **How often and how smooth.** These are F2 → Feather settings, or the
   `SET` command:
   * `predict` (`SET predict MS`): how often it predicts; 250 ms by default,
     0 = never.
   * `clslp` and `speedlp` (`SET clslp S`, `SET speedlp S`): the time
     constants of the class and speed low-pass; 10 s by default, 0 = off.

   The distance starts again with every logging session. A Polar
   reconnect within the session keeps it.
4. **Supported layers.** These run on the feather (in float, also for int8
   watch models): Dense, Conv2D, DepthwiseConv2D, MaxPooling2D,
   AveragePooling2D, BatchNormalization, Flatten, Reshape, Dropout, and
   ReLU/ReLU6/sigmoid/tanh/softmax. Other layers are refused when building.
5. **Pipeline tested on the laptop.** `predictor.cpp` was compiled on the
   laptop with the 3-model header and fed a 65-minute recording. The class
   settled on run during the run (with the filters off it sometimes
   flickered to walk). The speed was smoothed and the distance added up
   steadily.
6. **Tested live** (single model, earlier firmware) with the H10:
   * The feather predicted every 250 ms, taking 57 ms each.
   * The prec app's Monitor showed the speed.
   * The same model on the laptop gave the same values for that recording
     (7.21–7.31 m/s on the laptop vs 7.22–7.28 on the feather).
   * Those values were nonsense for sitting still, because of how that
     2-epoch test model reacts to an unfamiliar strap orientation. The
     strap's gravity vector was rotated compared with the training
     recordings. Models on raw samples are sensitive to how the strap is
     worn.

## Command line

```
prec_train.py show | set KEY=VALUE... | histogram ROLE | archs | train [--class run|walk] [--classifier] | retrain NAME [--epochs N] [--lr X] | export NAME | models | predict NAME PAIR [--shift N] [--lowpass TAU]... [--csv F] | feather [SPEED|none] [--classifier C] [--run R] [--walk W] [--no-flash] [--port P]
prec_bangle.py [--address A] status | model ~/prec/models/NAME.js [--keep-use] | rmmodel FILE | app | set KEY=JSON... | sync DATA_DIR [--keep] | run CODE
```

`prec_train.py` functions (for scripts):

| Function | Description |
|---|---|
| `load_train`, `save_train`, `set_param` | parameters |
| `list_archs`, `arch_info`, `load_arch` | architectures |
| `effective(conf, t)` | the parameters as used (arch overrides, window coefficients, input shape, frequency); raises on impossible combinations |
| `role_data`, `windows_of`, `apply_strategy`, `histogram` | datasets |
| `features_np`, `features_tf`, `norm_stats`, `make_dataset` | features |
| `train`, `export`, `to_tflite`, `tflite_ops`, `estimate_arena` | training and export |
| `c_source`, `compile_c`, `check_c_host`, `module_js` | watch code (`buffer_data`, `features`, `spectrum`) |
| `list_models` | saved models |
| `predict_pair(conf, model, pair, max_windows=20000, shift=None)` | predictions and targets of a model on a pair: `{t, pred, target, mae, rmse, bias, r, n, step}` |
| `lowpass(t, x, tau)` | causal first-order low-pass of x(t) with time constant tau s, restarted after gaps |
| `keras_layers`, `feather_header`, `check_feather_host`, `build_feather` | the model in the feather firmware |

`prec_bangle.py`: `upload(files, address=None, code=None, progress=None)`,
`model_file(name)`, `model_slot(name, kind)`, `model_files(js)`,
`upload_model(js, use=True)`, `remove_model(file)`, `install_app("prec")`,
`status()`, `set_settings(changes)`, `sync(data_dir)`, `find_bangle()`.

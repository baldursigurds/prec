/* PREC module for Bangle.js 2: everything the prec app does, for any app.

Installed as Storage file "prec":   const prec = require("prec");
The Polar part (models, cadence, recording) is the module "precpol", loaded
by prec.polar(). See docs/BANGLE.md for the full API.

  loadSettings() / saveSettings(s)      prec.json (DEFAULTS below)
  polar(opts) -> Session                the H10 directly: speed, class, heart rate, cadence, distance
  connect(opts) -> Promise<Feather>     the PREC feather; feather.monitor() -> Source of the same data
  draw(d), show(src, back)              the data screen for either source
  listModels(), modelFiles(s), checkModels(files)
  speedText, distText, timeText, gpsTime, textInput

A source (Session, feather.monitor()) emits "data" with one object:
  {source: "polar"|"feather", status, warn, speed (m/s), cls ("run", "walk",
   "still", "stretch"), hr (bpm), dist (m), time (s running), cad (steps/min),
   pbat (Polar %), fbat (feather mV), rec (recording text)}
any of which may be undefined. All feather operations return Promises that
reject with an Error (err.code = protocol error code) on "ERR" replies. */

const NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e";
const NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e";   // we write here
const NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e";   // feather notifies here
const SETTINGS_FILE = "prec.json";
const CHUNK = 20;

/* ---- settings ---------------------------------------------------------- */

/* password, address, name: the feather (its password, the one used last);
   units: pace, pacemi, kmh, mph, ms; polars: [{id, name}] known straps,
   polar: id of the default one; record: write the raw ACC stream; logHr,
   logCad: write heart rate / cadence words; chunk: bytes per recording file;
   cadAxis: 0 x, 1 y, 2 z, the up-down axis; cadLp, clsLp, speedLp: low-pass
   time constants (s, 0 = off) of the cadence, class and speed; buffers: overlapping windows per model; models:
   {cls, run, walk, speed}: Storage files ("" = none, missing = the old
   names pclass, prun, pwalk, pmodel if present). */
exports.DEFAULTS = {password: "prec", address: "", name: "", units: "pace", polars: [], polar: "", record: false,
  logHr: false, logCad: false, chunk: 524288, cadAxis: 0, cadLp: 5, clsLp: 10, speedLp: 10, buffers: 2, models: {}};

/** loadSettings() -> the settings in prec.json, with defaults filled in. */
exports.loadSettings = function() {
  let s = Object.assign({}, exports.DEFAULTS, require("Storage").readJSON(SETTINGS_FILE, 1) || {});
  s.models = Object.assign({}, s.models);
  return s;
};

/** saveSettings(s): store the settings object returned by loadSettings(). */
exports.saveSettings = function(s) {
  require("Storage").writeJSON(SETTINGS_FILE, s);
};

/** setPassword(pw): store the password used to unlock the feather. */
exports.setPassword = function(pw) {
  let s = exports.loadSettings();
  s.password = pw;
  exports.saveSettings(s);
};

/* ---- text ---------------------------------------------------------------- */

exports.UNITS = {pace: "min/km", pacemi: "min/mi", kmh: "km/h", mph: "mph", ms: "m/s"};
exports.UNIT_KEYS = ["pace", "pacemi", "kmh", "mph", "ms"];
exports.CLASSES = ["run", "walk", "still", "stretch"];
exports.CLASS_COLORS = {run: "#0f0", walk: "#ff0", still: "#f00", stretch: "#f0f"};

/** speedText(v, units) -> v (m/s) in units: pace "m:ss" per km or mile, km/h, mph or m/s. */
exports.speedText = function(v, units) {
  if (v === undefined) return "--";
  if (units === "kmh") return (v * 3.6).toFixed(1);
  if (units === "mph") return (v * 2.23694).toFixed(1);
  if (units === "ms") return v.toFixed(2);
  if (v < 0.5) return "--:--";
  let s = Math.round((units === "pacemi" ? 1609.344 : 1000) / v);
  return Math.floor(s / 60) + ":" + ("0" + s % 60).substr(-2);
};

function miles(units) { return units === "mph" || units === "pacemi"; }

/** distText(m, units) -> "1.23" in km, or miles for the mph / pacemi units (no unit). */
exports.distText = function(m, units) {
  return m === undefined ? "--" : (m / (miles(units) ? 1609.344 : 1000)).toFixed(2);
};

/** timeText(s) -> "m:ss" or "h:mm:ss". */
exports.timeText = function(s) {
  if (s === undefined) return "--";
  s = Math.floor(s);
  let p = x => ("0" + x).substr(-2), h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60;
  return (h ? h + ":" + p(m) : m) + ":" + p(s % 60);
};

/* ---- the data screen ------------------------------------------------------ */

function heart(x, y, r) {
  g.setColor("#f00").fillCircle(x - r * 0.9, y, r).fillCircle(x + r * 0.9, y, r);
  g.fillPoly([x - r * 1.85, y + r * 0.35, x + r * 1.85, y + r * 0.35, x, y + r * 2.3]);
}

// the largest Vector font up to size that fits text into width
function fit(text, size, width) {
  g.setFont("Vector", size);
  while (size > 10 && g.stringWidth(text) > width) g.setFont("Vector", size -= 2);
}

// the unit as a fraction: numerator over denominator
const UNIT_PARTS = {pace: ["min", "km"], pacemi: ["min", "mi"], kmh: ["km", "h"], mph: ["mi", "h"], ms: ["m", "s"]};

/** draw(d, units): the data screen in Bangle.appRect (below the widgets) for
 * one data object (see the top of this file):
 *   status (small) and clock;
 *   the class as a coloured rectangle, the speed, the unit as a fraction
 *   (min over km, a line between, the speed centred on that line);
 *   a 2 x 2 grid of heart rate, distance, time and cadence, one font size,
 *   with tiny labels;
 *   the batteries. */
exports.draw = function(d, units) {
  let R = Bangle.appRect, x = R.x, y = R.y, w = R.w, t = new Date();
  if (units === undefined) units = exports.loadSettings().units;
  g.reset().clearRect(R);
  // status (small, left) and clock (right)
  g.setFont("6x8:2").setFontAlign(1, -1).drawString(("0" + t.getHours()).substr(-2) + ":" +
    ("0" + t.getMinutes()).substr(-2), R.x2 - 1, y + 1);
  let st = d.warn || ((d.status || "") + (d.rec ? " " + d.rec : ""));
  g.setFont("6x8").setFontAlign(-1, 0).setColor(d.warn ? "#f00" : g.theme.fg);
  while (st.length && g.stringWidth(st) > w - 66) st = st.substr(0, st.length - 1);
  g.drawString(st, x + 2, y + 9);
  // the class rectangle, the speed and the unit fraction, centred on one line
  let mid = y + 41;
  if (d.cls) {
    g.setColor(exports.CLASS_COLORS[d.cls] || "#888").fillRect(x + 2, y + 21, x + 25, mid + 20);
    g.setColor(g.theme.fg).drawRect(x + 2, y + 21, x + 25, mid + 20);
  }
  g.setColor(g.theme.fg);
  let u = UNIT_PARTS[units] || ["", ""], uw;
  g.setFont("Vector", 16);
  uw = Math.max(g.stringWidth(u[0]), g.stringWidth(u[1]));
  let ux = R.x2 - 2 - uw / 2;
  g.setFontAlign(0, 1).drawString(u[0], ux, mid - 1);
  g.setFontAlign(0, -1).drawString(u[1], ux, mid + 3);
  g.fillRect(ux - uw / 2, mid, ux + uw / 2, mid + 1);
  let sp = exports.speedText(d.speed, units), left = x + 30, right = R.x2 - uw - 8;
  fit(sp, 46, right - left);
  g.setFontAlign(0, 0).drawString(sp, (left + right) / 2, mid + 2);
  // heart rate, distance, time, cadence: 2 x 2, one font size
  let cells = [[d.hr === undefined ? "--" : String(d.hr), "bpm"],
    [exports.distText(d.dist, units), miles(units) ? "mi" : "km"],
    [exports.timeText(d.time), "time"], [d.cad ? String(Math.round(d.cad)) : "--", "steps/min"]];
  let top = y + 66, rowH = 37, colW = w / 2, size = 30;
  cells.forEach(function(c) { fit(c[0], size, colW - 6); size = Math.min(size, g.getFontHeight()); });
  cells.forEach(function(c, i) {
    let cx = x + colW * (i % 2) + colW / 2, cy = top + rowH * (i >> 1);
    g.setColor(g.theme.fg).setFont("Vector", size).setFontAlign(0, 0).drawString(c[0], cx, cy + 13);
    g.setFont("6x8").drawString(c[1], cx + (i ? 0 : 5), cy + 31);
    if (!i) heart(cx - g.stringWidth(c[1]) / 2 - 4, cy + 30, 3);
  });
  // batteries
  let b = "Polar " + (d.pbat === undefined ? "-" : d.pbat + "%");
  if (d.source === "feather") b += "  Feather " + (d.fbat ? (d.fbat / 1000).toFixed(2) + "V" : "-");
  g.setColor(g.theme.fg).setFont("6x8").setFontAlign(0, 1).drawString(b, x + w / 2, R.y2);
};

/** show(src, back): the data screen for src (a Session or a feather monitor)
 * until back() is called by the button; returns a function that stops it.
 * src is not stopped: back() decides. */
exports.show = function(src, back) {
  let units = exports.loadSettings().units, last = {status: "starting"};
  function onData(d) { last = d; exports.draw(d, units); }
  src.on("data", onData);
  let timer = setInterval(function() { exports.draw(last, units); }, 30000);   // the clock
  exports.draw(last, units);
  function stop() {
    src.removeListener("data", onData);
    clearInterval(timer);
  }
  Bangle.setUI({mode: "custom", back: function() { back(stop); }});
  return stop;
};

/* ---- models ---------------------------------------------------------------- */

const OLD_MODELS = {cls: "pclass", run: "prun", walk: "pwalk", speed: "pmodel"};

/** modelInfo(file) -> {file, name, kind ("speed"|"classifier"), freq, range,
 * win, dft} read from a model module in Storage (without loading it), or undefined. */
exports.modelInfo = function(file) {
  let t = require("Storage").read(file);
  if (!t || t.indexOf("exports.FREQ") < 0) return;
  function get(k) {
    let i = t.indexOf("exports." + k + " = ");
    if (i < 0) return;
    i += k.length + 11;
    return t.substring(i, t.indexOf(";", i)).replace(/"/g, "");
  }
  return {file: file, name: get("NAME") || file, kind: get("KIND") || "speed", freq: +get("FREQ"),
    range: +get("RANGE"), win: +get("WIN"), dft: +get("DFT") || 0};
};

/** listModels() -> [modelInfo] of every model on the watch: prm_* files
 * (uploaded by the PREC TUI) and the old pmodel, pclass, prun, pwalk. */
exports.listModels = function() {
  let S = require("Storage");
  return S.list(/^prm_/).filter(f => !f.endsWith(".tfl")).concat(
    ["pmodel", "pclass", "prun", "pwalk"].filter(f => S.read(f, 0, 1))).map(exports.modelInfo).filter(m => m);
};

/** modelFiles(s) -> {cls, run, walk, speed}: the model files the settings
 * choose that are present (a slot never set falls back to the old names). */
exports.modelFiles = function(s) {
  let S = require("Storage"), r = {};
  Object.keys(OLD_MODELS).forEach(function(k) {
    let f = s.models[k] === undefined ? OLD_MODELS[k] : s.models[k];
    if (f && S.read(f, 0, 1) && S.read(f + ".tfl", 0, 1)) r[k] = f;
  });
  return r;
};

/** checkModels(files) -> undefined if the models ({cls, run, walk, speed})
 * work together, else the problem: a classifier where a speed model belongs
 * (or the other way round), a classifier without a speed model, or models
 * that need different accelerometer input (rate or range). */
exports.checkModels = function(files) {
  let first, err;
  Object.keys(files).forEach(function(k) {
    if (err) return;
    let m = exports.modelInfo(files[k]);
    if (!m) return err = files[k] + " is not a model";
    if ((k === "cls") !== (m.kind === "classifier")) return err = m.name + (k === "cls" ? " is not a classifier"
      : " is a classifier");
    if (!first) first = m;
    else if (m.freq !== first.freq || m.range !== first.range) err = m.name + " needs " + m.freq + " Hz " +
      m.range + " G, " + first.name + " " + first.freq + " Hz " + first.range + " G";
  });
  if (!err && files.cls && !files.run && !files.walk && !files.speed) err = "the classifier needs a run, walk or speed model";
  return err;
};

/** polar(opts) -> Session: the Polar H10 read directly (module precpol). */
exports.polar = function(opts) { return new (require("precpol").Session)(opts); };

/* ---- text entry -------------------------------------------------------------- */

const PW_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&*+-./:;=?@_~";

/** textInput(heading, initial, onSave(text), onBack): the textinput app if
 * installed, else a character-by-character menu. */
exports.textInput = function(heading, initial, onSave, onBack) {
  if (require("Storage").read("textinput")) {
    return require("textinput").input({text: initial}).then(function(t) {
      if (t) onSave(t); else onBack();
    });
  }
  let pw = initial || "", ci = 0;
  function menu() {
    let m = {"": {title: heading, back: onBack}};
    m[pw.length ? pw : "(empty)"] = function() {};
    m["Char"] = {value: ci, min: 0, max: PW_CHARS.length - 1, wrap: true,
      format: v => PW_CHARS[v], onchange: v => { ci = v; }};
    m["Add char"] = function() { if (pw.length < 32) pw += PW_CHARS[ci]; menu(); };
    m["Delete last"] = function() { pw = pw.slice(0, -1); menu(); };
    m["Save"] = function() {
      if (!pw.length) return E.showAlert("Empty").then(menu);
      onSave(pw);
    };
    E.showMenu(m);
  }
  menu();
};

/* ---- crypto helpers ---------------------------------------------------- */

function toHex(buf) {
  let a = new Uint8Array(buf), s = "";
  for (let i = 0; i < a.length; i++) s += (a[i] < 16 ? "0" : "") + a[i].toString(16);
  return s;
}

/** authDigest(nonce, password) -> hex SHA-256(nonce + password), the AUTH argument. */
exports.authDigest = function(nonce, password) {
  return toHex(require("crypto").SHA256(nonce + password));
};

/** encryptPassword(nonce, oldPw, newPw) -> hex argument of the PASSWORD command:
 * newPw XOR SHA-256(nonce + oldPw + "password"). */
exports.encryptPassword = function(nonce, oldPw, newPw) {
  let key = new Uint8Array(require("crypto").SHA256(nonce + oldPw + "password"));
  let out = new Uint8Array(newPw.length);
  for (let i = 0; i < newPw.length; i++) out[i] = newPw.charCodeAt(i) ^ key[i];
  return toHex(out.buffer);
};

/* ---- GPS time ---------------------------------------------------------- */

/** gpsTime(opts) -> Promise<Date>. Powers the GPS until it has a fix, then
 * sets the Bangle clock (UTC) from the GPS time and powers the GPS off.
 * opts.timeout: ms before giving up (default 10 minutes)
 * opts.onProgress(fix): called for every GPS event (fix.satellites etc.)
 * The returned promise has a cancel() method. */
exports.gpsTime = function(opts) {
  opts = opts || {};
  let finish;
  let p = new Promise(function(resolve, reject) {
    let timer;
    function onGPS(fix) {
      if (opts.onProgress) opts.onProgress(fix);
      if (fix.fix && fix.time && fix.time.getFullYear() >= 2024) {
        setTime(fix.time.getTime() / 1000);
        finish(null, fix.time);
      }
    }
    finish = function(err, date) {
      if (!finish) return;
      finish = undefined;
      Bangle.removeListener("GPS", onGPS);
      Bangle.setGPSPower(0, "prec");
      clearTimeout(timer);
      if (err) reject(err); else resolve(date);
    };
    Bangle.on("GPS", onGPS);
    Bangle.setGPSPower(1, "prec");
    timer = setTimeout(function() { if (finish) finish(new Error("No GPS fix")); }, opts.timeout || 600000);
  });
  p.cancel = function() { if (finish) finish(new Error("Cancelled")); };
  return p;
};

/* ---- feather connection ------------------------------------------------ */

function parseKV(tokens) {
  let o = {};
  tokens.forEach(function(t) {
    let i = t.indexOf("=");
    if (i > 0) o[t.substr(0, i)] = t.substr(i + 1);
  });
  return o;
}

function num(v) { return v === undefined ? undefined : parseInt(v, 10); }

/** A connected, authenticated feather. Created by connect().
 * Properties: name, address, connected.
 * Events: "event" (text of a "! ..." line), "disconnect". */
function Feather(device, rx) {
  this.device = device;
  this.rx = rx;
  this.address = device.id;
  this.name = device.name || "";
  this.connected = true;
  this.buf = "";
  this.queue = Promise.resolve();
  this.pending = undefined;
}

Feather.prototype._onData = function(str) {
  this.buf += str;
  let i;
  while ((i = this.buf.indexOf("\n")) >= 0) {
    let line = this.buf.substr(0, i).replace("\r", "");
    this.buf = this.buf.substr(i + 1);
    this._onLine(line);
  }
};

Feather.prototype._onLine = function(l) {
  if (l.startsWith("! ")) return this.emit("event", l.substr(2));
  let p = this.pending;
  if (!p) return;
  if (l.startsWith("+ ")) return p.lines.push(l.substr(2));
  if (!l.startsWith("OK") && !l.startsWith("ERR")) return;
  clearTimeout(p.timer);
  this.pending = undefined;
  let args = l.split(" ").slice(1);
  if (l.startsWith("OK")) return p.resolve({lines: p.lines, args: args, reply: l});
  let e = new Error(args.slice(1).join(" "));
  e.code = parseInt(args[0], 10);
  p.reject(e);
};

Feather.prototype._write = function(str) {
  let rx = this.rx, p = Promise.resolve();
  for (let i = 0; i < str.length; i += CHUNK) {
    let part = str.substr(i, CHUNK);
    p = p.then(function() { return rx.writeValue(part); });
  }
  return p;
};

/** request(line, timeout) -> Promise<{lines, args, reply}>. Sends one command
 * line; resolves on "OK" with the "+" data lines, rejects on "ERR" or timeout
 * (default 20 s). Requests are queued and run one at a time. */
Feather.prototype.request = function(line, timeout) {
  let self = this;
  let p = this.queue.then(function() {
    if (!self.connected) throw new Error("Not connected");
    return new Promise(function(resolve, reject) {
      self.pending = {lines: [], resolve: resolve, reject: reject,
        timer: setTimeout(function() {
          self.pending = undefined;
          reject(new Error("Timeout: " + line.split(" ")[0]));
        }, timeout || 20000)};
      self._write(line + "\n").catch(function(e) {
        clearTimeout(self.pending.timer);
        self.pending = undefined;
        reject(e);
      });
    });
  });
  this.queue = p.catch(function() {});
  return p;
};

/** status() -> Promise<object>. Parsed STATUS: {state: "idle"|"logging"|
 * "reconnecting", logging (bool), file, bytes, dropped, polar, streams,
 * packets, bat (mV), time, clock, sd, default, reset, up, hr, pbat}. */
Feather.prototype.status = function() {
  return this.request("STATUS").then(function(r) {
    let s = parseKV(r.args);
    s.logging = s.state !== "idle";
    s.bytes = num(s.bytes);
    s.bat = num(s.bat);
    s.dropped = num(s.dropped);
    return s;
  });
};

/** heartRate() -> Promise<object>. Monitor data (HR command), cheap enough to
 * poll every second: {state, logging (bool), polar (bool: connected),
 * hr (bpm or undefined), contact (true/false/undefined), age (ms since the
 * last measurement), beats (measurements so far), rr ([ms...]),
 * polarBattery (% or undefined), bat (feather mV), file, bytes,
 * speed (m/s predicted by the models in the feather, after the low-pass, or
 * undefined), rspeed (m/s before it), cls (class: "run", "walk", "still",
 * "stretch", or undefined without a classifier), dist (m since logging
 * started), cad (steps/min or undefined), cadm ("count" or "dft"), rt (s
 * since logging started), sage (ms since that prediction), preds (count), pms (ms per
 * prediction), model (names or "-"), mstate ("running", "waiting_for_data",
 * "no_model", ...)}. */
Feather.prototype.heartRate = function() {
  return this.request("HR", 5000).then(function(r) {
    let s = parseKV(r.args);
    return {state: s.state, logging: s.state !== "idle", polar: s.polar === "connected",
      hr: s.hr === "-" ? undefined : num(s.hr),
      contact: s.contact === "yes" ? true : s.contact === "no" ? false : undefined,
      age: num(s.age), beats: num(s.beats), rr: s.rr === "-" ? [] : s.rr.split(",").map(Number),
      polarBattery: s.pbat === "-" ? undefined : num(s.pbat), bat: num(s.bat),
      file: s.file, bytes: num(s.bytes),
      speed: s.speed === undefined || s.speed === "-" ? undefined : parseFloat(s.speed),
      rspeed: s.rspeed === undefined || s.rspeed === "-" ? undefined : parseFloat(s.rspeed),
      cls: s.cls === undefined || s.cls === "-" ? undefined : s.cls, dist: s.dist === undefined ? 0 : num(s.dist),
      sage: num(s.sage), preds: num(s.preds), pms: num(s.pms), model: s.model,
      mstate: s.mstate || "unknown", cad: s.cad === undefined || s.cad === "-" ? undefined : num(s.cad),
      cadm: s.cadm, rt: num(s.rt)};
  });
};

/** monitor(period) -> a source emitting "data" (see the top of this file)
 * every period ms (default 1000) from HR polls; stop() ends it. */
Feather.prototype.monitor = function(period) {
  let self = this, src = {}, busy = false;
  function poll() {
    if (busy || !self.connected) return;
    busy = true;
    self.heartRate().then(function(r) {
      busy = false;
      let p = r.speed !== undefined && r.sage < 5000;
      src.emit("data", {source: "feather", status: r.state + (r.polar ? "" : " no polar"), speed: p ? r.speed : undefined,
        cls: p ? r.cls : undefined, hr: r.hr !== undefined && r.age < 5000 ? r.hr : undefined, dist: r.logging ? r.dist :
        undefined, time: r.logging ? r.rt : undefined, cad: r.cad, pbat: r.polarBattery, fbat: r.bat});
    }, function(e) {
      busy = false;
      src.emit("data", {source: "feather", warn: e.message});
    });
  }
  let timer = setInterval(poll, period || 1000);
  poll();
  src.stop = function() { clearInterval(timer); };
  return src;
};

/** getSettings() -> Promise<{sleep, reconnect, flush, lowbattery, predict,
 * clslp, speedlp, loghr, logcad, cadaxis}> (numbers): the SET command. */
Feather.prototype.getSettings = function() {
  return this.request("SET").then(function(r) {
    let o = parseKV(r.args);
    Object.keys(o).forEach(function(k) { o[k] = num(o[k]); });
    return o;
  });
};

/** setSetting(key, value) -> Promise: SET key value on the feather. */
Feather.prototype.setSetting = function(key, value) {
  return this.request("SET " + key + " " + value);
};

/** setTime(ms) -> Promise<{ms, iso}>. Sets the feather RTC to ms (default:
 * the Bangle's current time, Date.now()). */
Feather.prototype.setTime = function(ms) {
  if (ms === undefined) ms = Date.now();
  return this.request("TIME " + Math.round(ms)).then(function(r) {
    return {ms: parseInt(r.args[0], 10), iso: r.args[1]};
  });
};

/** getTime() -> Promise<{ms, iso}>: the feather's clock. */
Feather.prototype.getTime = function() {
  return this.request("TIME").then(function(r) {
    return {ms: parseInt(r.args[0], 10), iso: r.args[1]};
  });
};

/** listData() -> Promise<[{name, size}]>: data files in /prec/data. */
Feather.prototype.listData = function() {
  return this.request("LS").then(function(r) {
    return r.lines.map(function(l) {
      let a = l.split(" ");
      return {name: a[0], size: parseInt(a[1], 10)};
    });
  });
};

/** scan(seconds) -> Promise<[{address, rssi, known, name}]>. The feather scans
 * for Polar devices (default 10 s) and remembers new ones. */
Feather.prototype.scan = function(seconds) {
  seconds = seconds || 10;
  return this.request("SCAN " + seconds, (seconds + 10) * 1000).then(function(r) {
    return r.lines.map(function(l) {
      let a = l.split(" ");
      return {address: a[0], rssi: parseInt(a[1], 10), known: a[2] === "known", name: a.slice(3).join(" ")};
    });
  });
};

/** listDevices() -> Promise<[{index, address, isDefault, name}]>: known Polar devices. */
Feather.prototype.listDevices = function() {
  return this.request("DEVS").then(function(r) {
    return r.lines.map(function(l) {
      let a = l.split(" ");
      return {index: parseInt(a[0], 10), address: a[1], isDefault: a[2] === "*", name: a.slice(3).join(" ")};
    });
  });
};

/** setDefault(addressOrIndex) -> Promise<{address, name}>: choose the Polar to log from. */
Feather.prototype.setDefault = function(d) {
  return this.request("DEFAULT " + d).then(function(r) {
    return {address: r.args[0], name: r.args.slice(1).join(" ")};
  });
};

function parseFormat(line) {
  let a = line.split(" ");
  let f = parseKV(a.slice(2));
  return {type: a[0], on: a[1] === "on", rate: num(f.rate) || 0, res: num(f.res) || 0,
    range: num(f.range) || 0, ch: num(f.ch) || 0};
}

/** getFormat() -> Promise<{acc: {on, rate, res, range, ch}, ecg: ..., gyro: ..., mag: ..., ...}>.
 * The data format requested from the Polar (0 = setting not sent). */
Feather.prototype.getFormat = function() {
  return this.request("FORMAT").then(function(r) {
    let o = {};
    r.lines.forEach(function(l) { let f = parseFormat(l); o[f.type] = f; });
    return o;
  });
};

/** setFormat(type, f) -> Promise<format>. type "acc"|"ecg"|"gyro"|"mag"|...;
 * f may contain on (bool), rate, res, range, ch (numbers, 0 = do not send). */
Feather.prototype.setFormat = function(type, f) {
  let cmd = "FORMAT " + type;
  if (f.on !== undefined) cmd += f.on ? " on" : " off";
  ["rate", "res", "range", "ch"].forEach(function(k) {
    if (f[k] !== undefined) cmd += " " + k + "=" + f[k];
  });
  return this.request(cmd).then(function(r) { return parseFormat(r.args.join(" ")); });
};

/** query() -> Promise<{features: ["ecg","acc"], settings: {acc: {rate: [..],
 * res: [..], range: [..]}, ...}, battery, mtu}>. The feather connects to the
 * default Polar and asks which measurements and settings it supports. */
Feather.prototype.query = function() {
  return this.request("QUERY", 30000).then(function(r) {
    let q = {features: [], settings: {}};
    r.lines.forEach(function(l) {
      let a = l.split(" ");
      if (a[0] === "features") q.features = a[1] === "-" ? [] : a[1].split(",");
      else if (a[0] === "battery") q.battery = parseInt(a[1], 10);
      else if (a[0] === "mtu") q.mtu = parseInt(a[1], 10);
      else {
        let s = {};
        a.slice(1).forEach(function(t) {
          let kv = t.split("=");
          if (kv.length === 2) s[kv[0]] = kv[1].split(",").map(parseFloat);
        });
        q.settings[a[0]] = s;
      }
    });
    return q;
  });
};

/** startLog() -> Promise<{file, started: [..], failed: [..]}>: start logging. */
Feather.prototype.startLog = function() {
  return this.request("LOG", 40000).then(function(r) {
    let kv = parseKV(r.args.slice(1));
    return {file: r.args[0], started: (kv.started || "").split(","),
      failed: kv.failed ? kv.failed.split(",") : []};
  });
};

/** stopLog() -> Promise<{file, bytes}>: stop logging. */
Feather.prototype.stopLog = function() {
  return this.request("STOP", 20000).then(function(r) {
    return {file: r.args[0], bytes: parseInt(r.args[1], 10)};
  });
};

/** changePassword(newPw) -> Promise. Changes the feather's password (sent
 * encrypted) and stores it as the Bangle's password too. */
Feather.prototype.changePassword = function(newPw) {
  let self = this, old = exports.loadSettings().password;
  return this.request("HELLO").then(function(r) {
    return self.request("PASSWORD " + exports.encryptPassword(r.args[3], old, newPw));
  }).then(function() { exports.setPassword(newPw); });
};

/** disconnect() -> Promise. Says BYE and drops the connection. A running
 * logging session continues on the feather. */
Feather.prototype.disconnect = function() {
  let self = this;
  if (!this.connected) return Promise.resolve();
  return this.request("BYE", 3000).catch(function() {}).then(function() {
    self.connected = false;
    try { self.device.gatt.disconnect(); } catch (e) {}
  });
};

/** findFeathers(timeout) -> Promise<[{id, name, rssi}]>: feathers advertising nearby. */
exports.findFeathers = function(timeout) {
  return new Promise(function(resolve) {
    NRF.findDevices(function(devs) {
      resolve(devs.map(function(d) { return {id: d.id, name: d.name, rssi: d.rssi}; }));
    }, {filters: [{namePrefix: "PREC-"}], timeout: timeout || 4000, active: true});
  });
};

/** connect(opts) -> Promise<Feather>. Finds a feather (the one used last time
 * if it is around, otherwise the strongest), connects and authenticates.
 * opts.address: connect to this address instead
 * opts.password: override the stored password
 * opts.onProgress(text): progress messages */
exports.connect = function(opts) {
  opts = opts || {};
  let s = exports.loadSettings();
  let progress = opts.onProgress || function() {};
  let password = opts.password || s.password;
  let feather, device;
  progress("Searching...");
  return exports.findFeathers(4000).then(function(devs) {
    if (opts.address) devs = devs.filter(function(d) { return d.id === opts.address; });
    if (!devs.length) throw new Error("No feather found");
    devs.sort(function(a, b) { return b.rssi - a.rssi; });
    let pick = devs.find(function(d) { return d.id === s.address; }) || devs[0];
    progress("Connecting to\n" + pick.name);
    // 20..50 ms: responsive, but leaves the feather's radio time for the Polar
    return NRF.connect(pick.id, {minInterval: 20, maxInterval: 50});
  }).then(function(gatt) {
    device = gatt.device;
    device.name = device.name || "";
    return gatt.getPrimaryService(NUS_SERVICE);
  }).then(function(svc) {
    return svc.getCharacteristic(NUS_RX).then(function(rx) {
      feather = new Feather(device, rx);
      return svc.getCharacteristic(NUS_TX);
    });
  }).then(function(tx) {
    tx.on("characteristicvaluechanged", function(ev) {
      let v = ev.target.value;
      feather._onData(E.toString(new Uint8Array(v.buffer, v.byteOffset, v.byteLength)));
    });
    device.on("gattserverdisconnected", function() {
      if (!feather.connected) return;
      feather.connected = false;
      if (feather.pending) {
        clearTimeout(feather.pending.timer);
        feather.pending.reject(new Error("Disconnected"));
        feather.pending = undefined;
      }
      feather.emit("disconnect");
    });
    return tx.startNotifications();
  }).then(function() {
    progress("Unlocking...");
    return feather.request("HELLO");
  }).then(function(r) {
    feather.name = r.args[2];
    return feather.request("AUTH " + exports.authDigest(r.args[3], password)).catch(function(e) {
      if (e.code === 401) e.message = "Wrong password";
      throw e;
    });
  }).then(function() {
    s.address = feather.address;
    s.name = feather.name;
    exports.saveSettings(s);
    return feather;
  }).catch(function(e) {
    if (device) try { device.gatt.disconnect(); } catch (x) {}
    throw e;
  });
};

exports.Feather = Feather;

/* precpol: the Polar H10 read directly by the watch (part of the prec app).

  const s = require("prec").polar(opts);   // or new (require("precpol").Session)(opts)
  s.on("data", d => ...);  s.start();  ...  s.stop();

opts: {address: Polar to use (default: the default from the settings),
       settings: an object like prec.loadSettings() (default: those)}.
Events: "data" once a second (see the prec module for the fields), "error"
(Error: the models cannot be used; the session runs without them).

Speed: the models chosen in the settings (prec.modelFiles). With a classifier,
its class probabilities go through a low-pass (clsLp) and the most likely
class picks the speed model: run -> run model, walk -> walk model (either
falling back to the speed model), still and stretch -> 0. Without one, the
speed model predicts. The speed goes through its own low-pass (speedLp) and is
integrated into the distance. Only the classifier and the speed model in use
are loaded (RAM).

Cadence: a model with DFT features gives the spectrum of the up-down axis
(cadAxis); the strongest bin between 1 and 4.5 Hz, refined by a parabola
through its neighbours, is the step frequency. Otherwise (or when that peak
sits on the band edge) the steps are counted; without counted steps in the
last 3 s there is no cadence (a spectrum always has some peak). The value
goes through a low-pass (cadLp s, 0 = off).: the axis rising through its
running mean (about -1 g) after being at least 150 mG below it.

Recording (record, logHr, logCad): a PREC stream (docs/PREC_FORMAT.md) in
Storage files of `chunk` bytes, pr<yymmddHHMMSS>.<n>: raw ACC words if record,
HEART_RATE words if logHr, a CADENCE word a second if logCad. It stops, with a
warning, when less than 1 MB would be left. */

const PMD_SVC = "fb005c80-02e7-f387-1cad-8acd2d8df0c8";
const PMD_CP = "fb005c81-02e7-f387-1cad-8acd2d8df0c8";
const PMD_DATA = "fb005c82-02e7-f387-1cad-8acd2d8df0c8";
const CLASSES = ["run", "walk", "still", "stretch"];
const RESERVE = 1048576;                 // bytes of Storage to leave free when recording
const VERSION = "prec " + ((require("Storage").readJSON("prec.info", 1) || {}).version || "?");
const HYST = 150, MEAN_TAU = 2, NPER = 8;

// weight of a new value in a first-order low-pass with time constant tau (s); 1 = off
function alpha(dt, tau) { return tau > 0 && dt > 0 ? 1 - Math.exp(-dt / tau) : 1; }

/* ---- a model with its staggered window buffers -------------------------------- */

// feed() copies the staged samples into each buffer; when one is full,
// onFull(outputs, runner) gets the model's outputs.
function Runner(file, nbuf, onFull) {
  this.file = file;
  this.M = require(file);
  this.M.MODEL_FILE = file + ".tfl";
  if (!this.M.run) this.M.run = b => [this.M.predict(b)];   // modules exported before run() and free()
  if (!this.M.free) this.M.free = function() {};
  this.spec = !!(this.M.DFT && this.M.spectrum);
  this.nbuf = nbuf;
  this.onFull = onFull;
}
Runner.prototype.start = function() {
  let M = this.M, k = this.nbuf;
  M.init();
  this.bufs = [];
  this.addr = [];
  this.pos = [];
  for (let i = 0; i < k; i++) {
    this.bufs.push(M.newBuffer());
    this.addr.push(E.getAddressOf(this.bufs[i], true));
    this.pos.push(-Math.round(i * M.WIN / k));          // negative: samples to skip before starting
  }
};
Runner.prototype.stop = function() {
  this.M.free();
  this.bufs = this.addr = this.pos = undefined;
};
Runner.prototype.feed = function(stageAddr, n) {
  let M = this.M;
  for (let k = 0; k < this.bufs.length; k++) {
    let i = 0;
    while (i < n) {
      if (this.pos[k] < 0) {
        let skip = Math.min(-this.pos[k], n - i);
        this.pos[k] += skip;
        i += skip;
        continue;
      }
      let m = Math.min(n - i, M.WIN - this.pos[k]);
      M.buffer_data(stageAddr + i * 6, this.addr[k], this.pos[k] * 3, (this.pos[k] + m) * 3);
      this.pos[k] += m;
      i += m;
      if (this.pos[k] === M.WIN) {                     // full: predict before it is refilled
        let out = M.run(this.bufs[k]);
        this.pos[k] = 0;
        this.onFull(out, this);
      }
    }
  }
};

/* ---- the session ------------------------------------------------------------------ */

function Session(opts) {
  opts = opts || {};
  this.S = opts.settings || require("prec").loadSettings();
  this.address = opts.address || this.S.polar;
  this.d = {source: "polar", status: "starting", dist: 0};
}
exports.Session = Session;

// setupModels(): the Runners for the chosen models; FREQ and RANGE to ask the Polar for
Session.prototype.setupModels = function() {
  let prec = require("prec"), files = prec.modelFiles(this.S), self = this;
  let nbuf = Math.max(1, Math.min(8, this.S.buffers | 0));
  this.FREQ = 50;                                        // without models: enough for the cadence
  this.RANGE = 8;
  this.C = this.active = undefined;
  this.speedFile = {};
  if (!Object.keys(files).length) return;
  let err = prec.checkModels(files);
  if (err) throw new Error(err);
  let m = prec.modelInfo(files.cls || files.run || files.walk || files.speed);
  this.FREQ = m.freq;
  this.RANGE = m.range;
  this.nbuf = nbuf;
  if (files.cls) {
    this.C = new Runner(files.cls, nbuf, (p, r) => self.onClass(p, r));
    this.speedFile = {0: files.run || files.speed, 1: files.walk || files.speed};
    this.C.start();
  } else this.use(files.speed);
};

// use(file): make the speed model in Storage file `file` the active one (undefined: none)
Session.prototype.use = function(file) {
  if (this.active && this.active.file === file) return;
  if (this.active) this.active.stop();
  this.active = undefined;
  if (!file) return;
  let self = this;
  this.active = new Runner(file, this.nbuf, (out, r) => self.onSpeed(out[0], r));
  this.active.start();
};

Session.prototype.onClass = function(p, r) {
  let now = getTime(), a = this.probs ? alpha(now - this.lastClsAt, this.S.clsLp) : 1;
  this.lastClsAt = now;
  if (!this.probs) this.probs = new Float32Array(p.length);
  for (let k = 0; k < p.length; k++) this.probs[k] += a * (p[k] - this.probs[k]);
  let c = 0;
  for (let k = 1; k < this.probs.length; k++) if (this.probs[k] > this.probs[c]) c = k;
  if (c !== this.cls) {
    this.cls = c;
    this.use(this.speedFile[c]);
  }
  this.d.cls = CLASSES[c];
  if (r.spec) this.spectrum(r.M);
  if (!this.active) this.onSpeed(0);                     // still, stretch (or no model for the class)
};

Session.prototype.onSpeed = function(v, r) {           // a new raw speed (m/s), or 0 for still / stretch
  let now = getTime(), d = this.d;
  if (d.speed !== undefined && this.lastSpeedAt) d.dist += d.speed * (now - this.lastSpeedAt);
  d.speed = v === 0 || d.speed === undefined ? v : Math.max(0, d.speed + alpha(now - this.lastSpeedAt, this.S.speedLp) *
    (v - d.speed));
  this.lastSpeedAt = now;
  if (r && r.spec && !(this.C && this.C.spec)) this.spectrum(r.M);
};

/* ---- cadence ---------------------------------------------------------------------- */

Session.prototype.cadReset = function() {
  let step = this.FREQ >= 100 ? this.FREQ / 50 : 1;
  this.cad = {step: step, skip: 0, dt: step / this.FREQ, a: 1 - Math.exp(-step / this.FREQ / MEAN_TAU), mean: -1000,
    prev: undefined, armed: false, t: 0, last: -1, per: [], at: 0, dftAt: 0};
};

// count steps in n staged samples
Session.prototype.cadCount = function(n) {
  let c = this.cad, v16 = this.stage16, ax = this.S.cadAxis | 0, i;
  for (i = c.skip; i < n; i += c.step) {
    let v = v16[i * 3 + ax];
    c.mean += c.a * (v - c.mean);
    if (v < c.mean - HYST) c.armed = true;
    else if (c.armed && c.prev !== undefined && v >= c.mean && c.prev < c.mean) {
      c.armed = false;
      let tc = c.t - c.dt * (v - c.mean) / (v - c.prev);
      if (c.last >= 0) {
        let p = tc - c.last;
        if (p >= 0.2 && p <= 1.5) {
          c.per.push(p);
          if (c.per.length > NPER) c.per.shift();
          if (c.per.length >= 3) {
            c.count = 60 * c.per.length / c.per.reduce((a, b) => a + b, 0);
            c.at = getTime();
          }
        } else if (p > 1.5) c.per = [];
      }
      c.last = tc;
    }
    c.prev = v;
    c.t += c.dt;
  }
  c.skip = i - n;                                        // samples of the next notification to pass over
};

// the step frequency from a DFT model's spectrum of the up-down axis
Session.prototype.spectrum = function(M) {
  let mag = M.spectrum(this.S.cadAxis | 0);
  if (!mag) return;
  let bin = M.FREQ / M.WIN, lo = Math.max(1, Math.ceil(1 / bin)), hi = Math.min(mag.length - 2, Math.floor(4.5 / bin));
  if (hi < lo) return;
  let k = lo;
  for (let i = lo + 1; i <= hi; i++) if (mag[i] > mag[k]) k = i;
  let a = mag[k - 1], b = mag[k], c = mag[k + 1], d = a - 2 * b + c;
  if (a > b || c > b || d >= 0) return;                  // the peak lies outside the band: count instead
  this.cad.dft = 60 * (k + 0.5 * (a - c) / d) * bin;
  this.cad.dftAt = getTime();
};

// steps per minute now (undefined: none) and the method (1 DFT, 0 counting)
Session.prototype.cadence = function() {
  let c = this.cad, now = getTime();
  if (!c || !c.at || now - c.at >= 3) return;          // no steps: a spectrum always has some peak
  if (c.dftAt && now - c.dftAt < 5) return [c.dft, 1];
  return [c.count, 0];
};

/* ---- recording (PREC stream, docs/PREC_FORMAT.md) ------------------------------------- */

// word(type, payload, ts): one PREC word: type, u16 length, u64 Polar ts, u64 rtc ms, payload
Session.prototype.word = function(type, payload, ts) {
  let w = new Uint8Array(19 + payload.length), dv = new DataView(w.buffer), ms = Date.now();
  w[0] = type;
  dv.setUint16(1, payload.length, true);
  w.set(ts || this.lastTs, 3);
  dv.setUint32(11, ms % 4294967296, true);
  dv.setUint32(15, Math.floor(ms / 4294967296), true);
  w.set(payload, 19);
  return w;
};

Session.prototype.recWrite = function(w) {
  let rec = this.rec, S = this.S;
  if (!rec) return;
  if (rec.off + w.length > S.chunk) {                    // the next file
    rec.idx++;
    rec.off = 0;
    if (require("Storage").getFree() < S.chunk + RESERVE) return this.recStop("storage full: not recording");
  }
  let name = rec.base + "." + rec.idx;
  try {
    if (rec.off === 0) require("Storage").write(name, w, 0, S.chunk);
    else require("Storage").write(name, w, rec.off);
  } catch (e) {
    return this.recStop("recording failed: " + e.message);
  }
  rec.off += w.length;
  rec.bytes += w.length;
  this.d.rec = "REC " + (rec.bytes / 1048576).toFixed(1) + "MB";
};

Session.prototype.recStop = function(warning) {
  if (!this.rec) return;
  if (!warning) this.recWrite(this.word(0x02, [0]));    // SESSION_END, reason: stop
  this.rec = undefined;
  this.d.rec = undefined;
  this.d.warn = warning;
  if (warning) Bangle.buzz(400);
};

Session.prototype.streamStart = function() {           // STREAM_START, ACC
  let F = this.FREQ;
  if (this.S.record) this.recWrite(this.word(0x03, [2, 12, 0, 1, F & 255, F >> 8, 1, 1, 16, 0, 2, 1, this.RANGE, 0]));
};

Session.prototype.recStart = function(dev) {
  let S = this.S;
  if (!(S.record || S.logHr || S.logCad) || this.rec) return;
  if (require("Storage").getFree() < S.chunk + RESERVE) {
    this.d.warn = "storage full: not recording";
    Bangle.buzz(400);
    return;
  }
  let d = new Date(), p2 = x => ("0" + x).substr(-2);
  this.rec = {base: "pr" + p2(d.getFullYear() % 100) + p2(d.getMonth() + 1) + p2(d.getDate()) + p2(d.getHours()) +
    p2(d.getMinutes()) + p2(d.getSeconds()), idx: 0, off: 0, bytes: 0};
  let addr = (dev.id || "").split(" ")[0].split(":").map(h => parseInt(h, 16));
  let name = E.toUint8Array(dev.name || "Polar"), ver = E.toUint8Array(VERSION);
  let p = [0x50, 0x52, 0x45, 0x43, 1].concat(addr.length === 6 ? addr : [0, 0, 0, 0, 0, 0],
    [(dev.id || "").indexOf("random") > 0 ? 1 : 0, name.length], [].slice.call(name), [ver.length], [].slice.call(ver));
  this.recWrite(this.word(0x01, p));                     // SESSION_START
  this.streamStart();
  if (this.d.pbat !== undefined) this.recWrite(this.word(0x10, [this.d.pbat]));
};

/* ---- Polar data ----------------------------------------------------------------- */

// One PMD data notification: [2][u64 time][frame type 1][x,y,z int16]...
Session.prototype.onAcc = function(ev) {
  let dv = ev.target.value;
  if (dv.getUint8(0) !== 2 || dv.getUint8(9) !== 1) return;
  let n = Math.min(((dv.byteLength - 10) / 6) | 0, 84);
  this.lastTs.set(new Uint8Array(dv.buffer, dv.byteOffset + 1, 8));    // a copy: the notification buffer is reused
  if (this.rec && this.S.record) this.recWrite(this.word(0x82, new Uint8Array(dv.buffer, dv.byteOffset + 9,
    dv.byteLength - 9), this.lastTs));
  this.stage.set(new Uint8Array(dv.buffer, dv.byteOffset + 10, n * 6));
  if (!this.startedAt) this.startedAt = getTime();
  this.cadCount(n);
  if (this.C) this.C.feed(this.stageAddr, n);
  if (this.active) this.active.feed(this.stageAddr, n);
};

Session.prototype.onHr = function(ev) {
  let v = ev.target.value, f = v.getUint8(0);
  this.d.hr = (f & 1) ? v.getUint16(1, true) : v.getUint8(1);
  this.hrAt = getTime();
  if (this.rec && this.S.logHr) this.recWrite(this.word(0x14, new Uint8Array(v.buffer, v.byteOffset, v.byteLength)));
};

// once a second: refresh d and emit it; the CADENCE word
Session.prototype.tick = function() {
  let d = this.d, now = getTime(), c = this.cadence();
  // the cadence low-pass (cadLp s, 0 = off); it starts again after a stop
  d.cad = !c ? undefined : d.cad === undefined ? c[0] : d.cad + alpha(1, this.S.cadLp) * (c[0] - d.cad);
  if (c && this.rec && this.S.logCad) {
    let v = Math.round(d.cad * 10);
    this.recWrite(this.word(0x15, [v & 255, v >> 8, c[1]]));
  }
  if (this.hrAt && now - this.hrAt > 5) d.hr = undefined;
  if (this.lastSpeedAt && now - this.lastSpeedAt > 10) d.speed = d.cls = undefined;
  d.time = this.startedAt ? now - this.startedAt : undefined;
  this.emit("data", d);
};

Session.prototype.connect = function() {
  let self = this, d = this.d, device;
  d.status = "searching";
  let find = this.address ? Promise.resolve({id: this.address}) :
    NRF.requestDevice({filters: [{namePrefix: "Polar"}], active: true, timeout: 20000});
  return find.then(function(dev) {
    device = dev;
    d.status = "connecting";
    return NRF.connect(dev.id, {minInterval: 7.5, maxInterval: 30});
  }).then(function(g2) {
    self.gatt = g2;
    g2.device.on("gattserverdisconnected", function() {
      if (self.stopped) return;
      d.status = "lost";
      self.retry = setTimeout(() => self.run(), 3000);
    });
    return g2.getPrimaryService("180d").then(s => s.getCharacteristic("2a37")).then(function(c) {
      c.on("characteristicvaluechanged", ev => self.onHr(ev));
      return c.startNotifications();
    }).catch(function() {});
  }).then(function() {
    return self.gatt.getPrimaryService("180f").then(s => s.getCharacteristic("2a19")).then(function(c) {
      c.on("characteristicvaluechanged", function(ev) {
        d.pbat = ev.target.value.getUint8(0);
        if (self.rec) self.recWrite(self.word(0x10, [d.pbat]));
      });
      return c.readValue().then(v => { d.pbat = v.getUint8(0); return c.startNotifications(); });
    }).catch(function() {});
  }).then(function() {
    return self.gatt.getPrimaryService(PMD_SVC);
  }).then(function(svc) {
    return svc.getCharacteristic(PMD_CP).then(function(cp) {
      return cp.startNotifications().then(() => svc.getCharacteristic(PMD_DATA)).then(function(data) {
        data.on("characteristicvaluechanged", ev => self.onAcc(ev));
        return data.startNotifications();
      }).then(function() {                               // start ACC: rate, 16 bit, range
        let F = self.FREQ;
        return cp.writeValue([2, 2, 0, 1, F & 255, F >> 8, 1, 1, 16, 0, 2, 1, self.RANGE, 0]);
      });
    });
  }).then(function() {
    self.rememberPolar(self.gatt.device.id, device.name);
    if (self.rec) self.streamStart();                    // restarted after a reconnect
    else self.recStart({id: self.gatt.device.id, name: device.name});
    d.status = "running";
  });
};

// rememberPolar(id, name): add to the known straps; the first one becomes the default
Session.prototype.rememberPolar = function(id, name) {
  let prec = require("prec"), s = prec.loadSettings();
  if (s.polars.some(p => p.id === id) && s.polar) return;
  if (!s.polars.some(p => p.id === id)) s.polars.push({id: id, name: name || id.substr(0, 17)});
  if (!s.polar) s.polar = id;
  prec.saveSettings(s);
  this.address = this.address || id;
};

Session.prototype.run = function() {
  let self = this;
  if (this.stopped) return;
  this.connect().catch(function(e) {
    if (self.stopped) return;
    self.d.status = "retry";
    print("prec polar:", e);
    if (self.gatt) try { self.gatt.disconnect(); } catch (x) {}
    self.retry = setTimeout(() => self.run(), 5000);
  });
};

/** start(): load the models and connect (and reconnect until stop()). A
 * model problem is reported with an "error" event and in d.warn; the session
 * then runs without models. */
Session.prototype.start = function() {
  let self = this;
  this.stopped = false;
  this.lastTs = new Uint8Array(8);                       // the most recent Polar timestamp (u64 LE)
  this.stage = new Uint8Array(512);
  this.stageAddr = E.getAddressOf(this.stage, true);
  this.stage16 = new Int16Array(this.stage.buffer);
  try {
    this.setupModels();
  } catch (e) {
    this.C = this.active = undefined;
    this.FREQ = 50;
    this.RANGE = 8;
    this.d.warn = "models: " + e.message;
    this.emit("error", e);
  }
  this.cadReset();
  this.timer = setInterval(() => self.tick(), 1000);
  this.run();
};

/** stop(): end the recording, disconnect, free the models. */
Session.prototype.stop = function() {
  this.stopped = true;
  clearInterval(this.timer);
  clearTimeout(this.retry);
  this.recStop();
  if (this.gatt) try { this.gatt.disconnect(); } catch (x) {}
  if (this.C) this.C.stop();
  if (this.active) this.active.stop();
  this.C = this.active = undefined;
};

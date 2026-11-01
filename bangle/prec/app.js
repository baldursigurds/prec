/* PREC: speed, heart rate and cadence from the Polar H10, read directly or
through the PREC feather logger, and control of the feather.
Everything lives in the "prec" module (lib.js, polar.js); this file is the
menus. See docs/BANGLE.md. */

const prec = require("prec");
const FALLBACK_OPTIONS = {   // used when the Polar cannot be asked (QUERY failed)
  acc: {rate: [25, 50, 100, 200], res: [16], range: [2, 4, 8]},
  ecg: {rate: [130], res: [14]},
  gyro: {rate: [26, 52, 104, 208, 416, 833, 1666], res: [16], range: [250, 500, 1000, 2000]},
  mag: {rate: [10, 20, 50, 100], res: [16], range: [50]}
};
const UNITS = {rate: " Hz", res: " bit", range: {acc: " G", gyro: " dps", mag: " G"}};
const LABELS = {rate: "Rate", res: "Resolution", range: "Range"};

let feather;          // connected prec.Feather, or undefined
let status = {};      // last STATUS from the feather
let onFeatherMenu = false;
let leaving = false;  // true while we disconnect on purpose
let display;          // {src, stop} while the feather's data screen is shown
let session;          // the running Polar session

Bangle.loadWidgets();
Bangle.drawWidgets();

function title() { return feather ? feather.name : "PREC"; }
function baseName(path) { return (path || "").split("/").pop(); }
function size(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.round(n / 1024) + " kB"; }
function show(menu) { onFeatherMenu = false; E.showMenu(menu); }

/* Show msg while promise runs; call done(result) or report the error. */
function run(msg, promise, done) {
  onFeatherMenu = false;
  E.showMessage(msg, title());
  promise.then(done, function(err) {
    E.showAlert(err.message || String(err), "Error").then(function() {
      if (feather && feather.connected) featherMenu(); else mainMenu();
    });
  });
}

/* ---- main menu --------------------------------------------------------- */

function mainMenu() {
  show({
    "": {title: "PREC", back: function() { load(); }},
    "Polar H10": polar,
    "Feather": connect,
    "GPS time": gpsTime,
    "Settings": function() { eval(require("Storage").read("prec.settings.js"))(mainMenu); }
  });
}

function gpsTime() {
  let t0 = Date.now();
  let p = prec.gpsTime({timeout: 15 * 60000, onProgress: function(fix) {
    E.showMessage("Searching...\n\nsatellites: " + (fix.satellites || 0) + "\n" +
      Math.round((Date.now() - t0) / 1000) + " s\n\n(back to cancel)", "GPS time");
  }});
  E.showMessage("Starting GPS...", "GPS time");
  Bangle.setUI({mode: "custom", back: function() { p.cancel(); }});
  p.then(function(d) {
    Bangle.buzz();
    return E.showAlert("Clock set\n" + d.toISOString().substr(11, 8) + " UTC", "GPS time");
  }, function(err) {
    return E.showAlert(err.message, "GPS time");
  }).then(mainMenu);
}

/* ---- Polar H10, read directly ------------------------------------------------ */

// polar(): connect to the default strap (scan and pick one if there is none)
// and show the data screen until back.
function polar() {
  let s = prec.loadSettings();
  if (!s.polar) return pickPolar();
  session = prec.polar({settings: s});
  session.on("error", function(e) { Bangle.buzz(300); });
  session.start();
  polarScreen();
}

function polarScreen() {
  prec.show(session, function(stopShow) {
    stopShow();
    E.showPrompt("Stop?", {title: "Polar"}).then(function(yes) {
      if (!yes) return polarScreen();
      session.stop();
      session = undefined;
      mainMenu();
    });
  });
}

function pickPolar() {
  E.showMessage("Scanning...", "Polar H10");
  NRF.findDevices(function(devs) {
    let m = {"": {title: devs.length ? "Choose strap" : "None found", back: mainMenu}};
    devs.forEach(function(d) {
      m[d.name || d.id] = function() {
        let s = prec.loadSettings();
        if (!s.polars.some(p => p.id === d.id)) s.polars.push({id: d.id, name: d.name || d.id.substr(0, 17)});
        s.polar = d.id;
        prec.saveSettings(s);
        polar();
      };
    });
    if (!devs.length) m["Scan again"] = pickPolar;
    show(m);
  }, {filters: [{namePrefix: "Polar"}], timeout: 6000, active: true});
}

/* ---- connecting -------------------------------------------------------- */

function connect() {
  run("Connecting...", prec.connect({onProgress: function(m) { E.showMessage(m, "PREC"); }}), function(f) {
    feather = f;
    leaving = false;
    f.on("event", onFeatherEvent);
    f.on("disconnect", function() {
      feather = undefined;
      stopDisplay();
      if (leaving) return;
      Bangle.buzz(400);
      E.showAlert("Connection lost", "PREC").then(mainMenu);
    });
    refresh(function() {
      if (status.logging) {
        Bangle.buzz();
        E.showAlert("Feather is logging\n" + baseName(status.file), title()).then(featherMenu);
      } else featherMenu();
    });
  });
}

function refresh(then) {
  run("Reading status...", feather.status(), function(s) { status = s; then(); });
}

function onFeatherEvent(text) {
  // "LOG stopped <reason> <file>", "LOG polar_lost", "LOG polar_reconnected"
  Bangle.buzz();
  if (display) return;        // the data screen shows the new state itself
  let wasOnMenu = onFeatherMenu;
  feather.status().then(function(s) {
    status = s;
    if (text.startsWith("LOG stopped")) {
      E.showAlert("Logging stopped\n" + text.split(" ")[2].replace("_", " "), title()).then(featherMenu);
    } else if (wasOnMenu) featherMenu();
  });
}

function disconnect() {
  let ask = status.logging ? E.showPrompt("Disconnect?\nThe feather keeps logging.", {title: title()})
    : Promise.resolve(true);
  ask.then(function(yes) {
    if (!yes) return featherMenu();
    leaving = true;
    run("Disconnecting...", feather.disconnect(), function() {
      feather = undefined;
      mainMenu();
    });
  });
}

/* ---- connected menu ---------------------------------------------------- */

function featherMenu() {
  if (!feather || !feather.connected) return mainMenu();
  let m = {"": {title: status.logging ? "LOGGING" : title(), back: disconnect}};
  if (status.logging) m["Log: " + baseName(status.file).replace(".prc", "")] = showStatus;
  m["Display"] = featherDisplay;
  m["Status"] = showStatus;
  m["Set feather time"] = setFeatherTime;
  m["List data"] = listData;
  m["Scan for Polar"] = scanPolar;
  m["Polar devices"] = polarDevices;
  m["Data format"] = dataFormat;
  m["Logger settings"] = loggerSettings;
  if (status.logging) m["Stop logging"] = stopLog;
  else m["Start logging"] = startLog;
  m["Feather password"] = changeFeatherPassword;
  m[status.logging ? "Disconnect (keep logging)" : "Disconnect"] = disconnect;
  show(m);
  onFeatherMenu = true;
}

/* ---- the feather's data on the data screen ------------------------------------ */

function stopDisplay() {
  if (!display) return;
  display.stop();
  display.src.stop();
  display = undefined;
}

function featherDisplay() {
  onFeatherMenu = false;
  let src = feather.monitor(1000);
  display = {src: src, stop: prec.show(src, function() { stopDisplay(); featherMenu(); })};
}

/* ---- logger settings on the feather (SET) ---------------------------------------- */

const TAUS = [0, 2, 5, 10, 20, 30, 60];

function loggerSettings() {
  run("Reading...", feather.getSettings(), function(o) {
    function set(k, v) {
      o[k] = v;
      feather.setSetting(k, v).catch(function(e) { E.showAlert(e.message, "Error").then(featherMenu); });
    }
    function tau(k) {
      return {value: Math.max(0, TAUS.indexOf(o[k])), min: 0, max: TAUS.length - 1,
        format: v => TAUS[v] ? TAUS[v] + " s" : "off", onchange: v => set(k, TAUS[v])};
    }
    let m = {"": {title: "Logger", back: featherMenu}};
    if (o.loghr === undefined) m["(old firmware)"] = function() {};
    m["Log heart rate"] = {value: !!o.loghr, onchange: v => set("loghr", v ? 1 : 0)};
    m["Log cadence"] = {value: !!o.logcad, onchange: v => set("logcad", v ? 1 : 0)};
    m["Cadence axis"] = {value: o.cadaxis | 0, min: 0, max: 2, format: v => "xyz"[v], onchange: v => set("cadaxis", v)};
    if (o.cadlp !== undefined) m["Cadence filter"] = tau("cadlp");
    m["Class filter"] = tau("clslp");
    m["Speed filter"] = tau("speedlp");
    show(m);
  });
}

function showStatus() {
  run("Reading status...", feather.status(), function(s) {
    status = s;
    let m = {"": {title: "Status", back: featherMenu}};
    m["State: " + s.state] = function() {};
    if (s.logging) {
      m[baseName(s.file)] = function() {};
      m["Written: " + size(s.bytes)] = function() {};
      m["Streams: " + s.streams] = function() {};
      m["Polar: " + s.polar] = function() {};
      if (s.dropped) m["Dropped: " + s.dropped] = function() {};
    }
    m["Battery: " + (s.bat / 1000).toFixed(2) + " V"] = function() {};
    m["Clock: " + s.time.substr(11, 8) + (s.clock === "ok" ? "" : " (unset)")] = function() {};
    m["Polar: " + (s["default"] === "-" ? "none chosen" : s["default"])] = function() {};
    if (s.sd !== "ok") m["SD CARD ERROR"] = function() {};
    show(m);
  });
}

function setFeatherTime() {
  run("Setting time...", feather.setTime(), function(t) {
    E.showAlert("Feather clock\n" + t.iso.substr(0, 10) + "\n" + t.iso.substr(11, 8) + " UTC", title()).then(featherMenu);
  });
}

function listData() {
  run("Listing...", feather.listData(), function(files) {
    let m = {"": {title: files.length + " files", back: featherMenu}};
    files.forEach(function(f) { m[f.name.replace(".prc", "") + " " + size(f.size)] = function() {}; });
    show(m);
  });
}

function scanPolar() {
  run("Scanning 10 s...", feather.scan(10), function(list) {
    let m = {"": {title: list.length + " found", back: featherMenu}};
    list.forEach(function(d) {
      m[(d.known ? "" : "NEW ") + d.name] = function() { chooseDefault(d.address, d.name); };
    });
    if (!list.length) m["No Polar found"] = featherMenu;
    show(m);
  });
}

function polarDevices() {
  run("Reading...", feather.listDevices(), function(list) {
    let m = {"": {title: "Polar devices", back: featherMenu}};
    list.forEach(function(d) {
      m[(d.isDefault ? "* " : "") + d.name] = function() { chooseDefault(d.address, d.name); };
    });
    if (!list.length) m["None - scan first"] = scanPolar;
    show(m);
  });
}

function chooseDefault(address, name) {
  E.showPrompt("Log from\n" + name + "?", {title: "Default Polar"}).then(function(yes) {
    if (!yes) return featherMenu();
    run("Saving...", feather.setDefault(address), function() {
      return feather.status().then(function(s) { status = s; featherMenu(); });
    });
  });
}

/* ---- data format ------------------------------------------------------- */

function dataFormat() {
  let q;
  run("Asking Polar...", feather.query().catch(function() { return undefined; }).then(function(r) {
    q = r;
    return feather.getFormat();
  }), function(fmt) { formatMenu(q, fmt); });
}

function formatMenu(q, fmt) {
  let types = q ? q.features.filter(function(t) { return fmt[t]; }) : ["acc", "ecg", "gyro", "mag"];
  let options = q ? q.settings : FALLBACK_OPTIONS;
  let m = {"": {title: "Data format", back: featherMenu}};
  types.forEach(function(t) {
    m[t.toUpperCase() + (fmt[t].on ? "  on" : "  off")] = function() { typeMenu(q, fmt, t, options[t] || {}); };
  });
  if (q) {   // enabled streams this Polar cannot deliver
    Object.keys(fmt).forEach(function(t) {
      if (fmt[t].on && types.indexOf(t) < 0) m[t.toUpperCase() + " unsupported, off"] = function() {
        feather.setFormat(t, {on: false}).then(function() { fmt[t].on = false; formatMenu(q, fmt); });
      };
    });
  } else m["(Polar not reached)"] = function() {};
  show(m);
}

function typeMenu(q, fmt, type, opts) {
  let f = fmt[type];
  let m = {"": {title: type.toUpperCase(), back: function() { formatMenu(q, fmt); }}};
  function save(change) {
    Object.assign(f, change);
    feather.setFormat(type, change).catch(function(e) { E.showAlert(e.message, "Error").then(featherMenu); });
  }
  m["Enabled"] = {value: f.on, onchange: function(v) { save({on: v}); }};
  ["rate", "res", "range"].forEach(function(k) {
    let list = (opts[k] || []).slice();
    if (f[k] && list.indexOf(f[k]) < 0) list.push(f[k]);
    if (!list.length) return;
    list.sort(function(a, b) { return a - b; });
    if (!f[k]) list.unshift(0);
    let unit = typeof UNITS[k] === "object" ? (UNITS[k][type] || "") : UNITS[k];
    m[LABELS[k]] = {value: Math.max(0, list.indexOf(f[k])), min: 0, max: list.length - 1,
      format: function(i) { return list[i] ? list[i] + unit : "-"; },
      onchange: function(i) { let c = {}; c[k] = list[i]; save(c); }};
  });
  show(m);
}

/* ---- logging ----------------------------------------------------------- */

function startLog() {
  run("Starting log...\n(connecting to\nthe Polar)", feather.startLog(), function(r) {
    Bangle.buzz();
    return feather.status().then(function(s) {
      status = s;
      return E.showAlert(baseName(r.file).replace(".prc", "") + "\n" + r.started.join(", ") +
        (r.failed.length ? "\nfailed: " + r.failed.join(", ").replace(/_/g, " ") : ""), "Logging");
    }).then(featherMenu);
  });
}

function stopLog() {
  E.showPrompt("Stop logging?", {title: title()}).then(function(yes) {
    if (!yes) return featherMenu();
    run("Stopping...", feather.stopLog(), function(r) {
      return feather.status().then(function(s) {
        status = s;
        return E.showAlert("Saved\n" + baseName(r.file) + "\n" + size(r.bytes), title());
      }).then(featherMenu);
    });
  });
}

function changeFeatherPassword() {
  prec.textInput("New password", "", function(pw) {
    run("Changing...", feather.changePassword(pw), function() {
      E.showAlert("Password changed\non feather and\nBangle", title()).then(featherMenu);
    });
  }, featherMenu);
}

E.on("kill", function() {
  if (session) session.stop();
  if (feather) { leaving = true; feather.disconnect(); }
});
mainMenu();

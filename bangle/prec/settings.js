/* prec settings (Storage prec.settings.js): from the Settings app, and the
prec app's Settings item. Units, Polar straps, recording, cadence, filters,
models, the feather password. */
(function(back) {
  const prec = require("prec");
  let s = prec.loadSettings();
  const TAUS = [0, 2, 5, 10, 20, 30, 60], CHUNKS = [262144, 524288, 1048576], AXES = ["x", "y", "z"];
  function save() { prec.saveSettings(s); }
  function choice(list, v) { return Math.max(0, list.indexOf(v)); }
  function tau(key, label) {
    return {value: choice(TAUS, s[key]), min: 0, max: TAUS.length - 1, format: v => TAUS[v] ? TAUS[v] + " s" : "off",
      onchange: v => { s[key] = TAUS[v]; save(); }};
  }

  function main() {
    E.showMenu({
      "": {title: "PREC settings", back: back},
      "Units": {value: choice(prec.UNIT_KEYS, s.units), min: 0, max: prec.UNIT_KEYS.length - 1,
        format: v => prec.UNITS[prec.UNIT_KEYS[v]], onchange: v => { s.units = prec.UNIT_KEYS[v]; save(); }},
      "Polar straps": polars,
      "Models": models,
      "Record raw data": {value: !!s.record, onchange: v => { s.record = v; save(); }},
      "Log heart rate": {value: !!s.logHr, onchange: v => { s.logHr = v; save(); }},
      "Log cadence": {value: !!s.logCad, onchange: v => { s.logCad = v; save(); }},
      "File size": {value: choice(CHUNKS, s.chunk), min: 0, max: CHUNKS.length - 1,
        format: v => CHUNKS[v] / 1048576 + " MB", onchange: v => { s.chunk = CHUNKS[v]; save(); }},
      "Cadence axis": {value: s.cadAxis | 0, min: 0, max: 2, format: v => AXES[v],
        onchange: v => { s.cadAxis = v; save(); }},
      "Cadence filter": tau("cadLp"),
      "Class filter": tau("clsLp"),
      "Speed filter": tau("speedLp"),
      "Buffers": {value: s.buffers, min: 1, max: 8, onchange: v => { s.buffers = v; save(); }},
      "Feather password": function() {
        prec.textInput("Password", s.password, function(pw) {
          s.password = pw;
          save();
          E.showAlert("Saved", "Password").then(main);
        }, main);
      }
    });
  }

  /* ---- Polar straps: the default, forget, scan ---- */
  function polars() {
    let m = {"": {title: "Polar straps", back: main}};
    s.polars.forEach(function(p) {
      m[(p.id === s.polar ? "* " : "") + p.name] = () => polarMenu(p);
    });
    if (!s.polars.length) m["(none yet)"] = function() {};
    m["Scan"] = scan;
    E.showMenu(m);
  }
  function polarMenu(p) {
    E.showMenu({
      "": {title: p.name, back: polars},
      "Make default": function() { s.polar = p.id; save(); polars(); },
      "Forget": function() {
        s.polars = s.polars.filter(q => q.id !== p.id);
        if (s.polar === p.id) s.polar = s.polars.length ? s.polars[0].id : "";
        save();
        polars();
      }
    });
  }
  function scan() {
    E.showMessage("Scanning...", "Polar");
    NRF.findDevices(function(devs) {
      let m = {"": {title: devs.length + " found", back: polars}};
      devs.forEach(function(d) {
        let known = s.polars.some(p => p.id === d.id);
        m[(known ? "" : "NEW ") + (d.name || d.id)] = function() {
          if (!known) s.polars.push({id: d.id, name: d.name || d.id.substr(0, 17)});
          s.polar = d.id;
          save();
          polars();
        };
      });
      if (!devs.length) m["No Polar found"] = polars;
      E.showMenu(m);
    }, {filters: [{namePrefix: "Polar"}], timeout: 6000, active: true});
  }

  /* ---- models: the classifier, run, walk and speed models ---- */
  const SLOTS = [["cls", "Classifier"], ["run", "Run speed"], ["walk", "Walk speed"], ["speed", "Any speed"]];
  function models() {
    E.showMessage("Reading models...", "Models");
    let all = prec.listModels(), now = prec.modelFiles(s);
    let m = {"": {title: "Models", back: main}};
    SLOTS.forEach(function(sl) {
      let f = now[sl[0]];
      m[sl[1] + ": " + (f ? short(f) : "none")] = () => pick(sl, all);
    });
    let err = prec.checkModels(now);
    m[err ? "! " + err : "OK: " + (Object.keys(now).length ? "models fit" : "no models")] = function() {
      if (err) E.showAlert(err, "Models").then(models);
    };
    E.showMenu(m);
  }
  function short(f) { return f.replace(/^prm_/, ""); }
  function pick(sl, all) {
    let k = sl[0], m = {"": {title: sl[1], back: models}};
    m["none"] = () => set(k, "");
    all.filter(i => (i.kind === "classifier") === (k === "cls")).forEach(function(i) {
      m[short(i.file) + " " + i.freq + "Hz"] = () => set(k, i.file);
    });
    E.showMenu(m);
  }
  function set(k, f) {
    let old = s.models[k];
    s.models[k] = f;
    let next = prec.modelFiles(s), err = prec.checkModels(next);
    if (err && f) {                                      // a model that does not fit the others
      return E.showPrompt(err + "\nUse it anyway?", {title: "Models"}).then(function(yes) {
        if (yes) save(); else s.models[k] = old;
        models();
      });
    }
    save();
    models();
  }

  main();
})

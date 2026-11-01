#!/usr/bin/env python3
"""prec_bangle.py - the Bangle.js side of PREC over Bluetooth: the prec app,
models, settings, status and recordings.

Used by the TUI's F3 (Bangle) tab. Every function opens one Bluetooth session,
talks to the watch's JavaScript console and closes it again. Only installing
the app or uploading a model resets the watch (to free the old files);
status and sync leave a running app alone.

  status(address=None) -> {"battery", "charging", "firmware", "free", "total",
      "trash", "apps": {id: version}, "models": [{file, name, kind, freq, win,
      dft, bytes, tfl}], "use": {cls, run, walk, speed: file}, "recordings":
      {session: [(file, size)...]}, "settings": {...}, "app", "mem"}
  upload(files, address=None, code=None, progress=None) -> console output
  upload_model(js_path, address=None, progress=None, use=True) -> (file, slot)
  remove_model(file, address=None)
  install_app("prec", address=None, progress=None)   (replaces the old ppred app)
  set_settings(changes, address=None) -> prec.json after the change
  sync(data_dir, address=None, progress=None, delete=True) -> {"saved": [paths], "skipped": [...]}
  find_bangle(timeout=10) -> address

Models: model_<id> is stored as Storage file prm_<id> (the module; a
_classifier suffix becomes _cls) and prm_<id>.tfl (the TensorFlow Lite model); prec.json "models" says which ones
the app uses: {cls, run, walk, speed}. upload_model(use=True) picks the new
one for its role: a classifier for cls, *_run for run, *_walk for walk,
anything else for speed.

Recordings: the prec app writes PREC streams in fixed-size Storage files
pr<yymmddHHMMSS>.<n>; the unused end of each file is 0xFF. sync() joins the
files of a session without that padding into <data>/<YYYYMMDD-HHMMSS>-bangle.prc,
checks it and (delete=True) removes the files from the watch.

Command line: prec_bangle.py [--address A] status | model NAME.js [--keep-use] |
  rmmodel FILE | app [prec] | set KEY=JSON... | sync DATA_DIR [--keep] | run CODE
"""
import argparse, asyncio, base64, json, os, re, struct, sys, time, warnings

warnings.simplefilter("ignore")
RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # write to the Bangle
TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # notifications from the Bangle
CHUNK = 512                                    # file bytes per Storage.write
READ = 1536                                    # file bytes per read while downloading
BANGLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bangle")
OLD_MODELS = {"cls": "pclass", "run": "prun", "walk": "pwalk", "speed": "pmodel"}    # before prec 0.03 (ppred)
SETTINGS = "prec.json"
SESSION = re.compile(r"^pr(\d{12})\.(\d+)$")
HEADER = struct.Struct("<BHQQ")               # PREC word header (docs/PREC_FORMAT.md)


def find_bangle(timeout=10.0):
    """Address of the strongest advertising Bangle.js, or None."""
    from bleak import BleakScanner

    async def scan():
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        b = [(a.rssi, d.address) for d, a in found.values() if (a.local_name or "").startswith("Bangle.js")]
        return max(b)[1] if b else None
    return asyncio.run(scan())


class Session:
    """An open console connection: run(code) sends code and returns what it
    printed up to an end marker."""

    def __init__(self, client):
        self.c, self.out, self.n = client, bytearray(), 0

    async def send(self, s):
        b = s.encode()
        for i in range(0, len(b), 20):               # the Nordic UART RX takes 20 bytes per write
            await self.c.write_gatt_char(RX, b[i:i + 20], response=True)

    async def run(self, code, timeout=10.0):
        """Run one line of code (echo off); returns its printed output."""
        self.n += 1
        end = "<<END%d>>" % self.n
        start = len(self.out)
        await self.send("\x10" + code.replace("\n", " ") + ";print(%s)\n" % json.dumps(end))
        t0 = time.time()
        while end.encode() not in self.out[start:]:
            if time.time() - t0 > timeout:
                raise RuntimeError("the watch did not answer (%s...)" % code[:40])
            await asyncio.sleep(0.05)
        text = self.out[start:].decode(errors="replace")
        return text[:text.index(end)]

    async def json(self, expr, timeout=10.0):
        """The value of a JavaScript expression (via JSON)."""
        text = await self.run("print('<<J'+JSON.stringify(%s)+'J>>')" % expr, timeout)
        m = re.search(r"<<J(.*?)J>>", text, re.S)
        if not m:
            raise RuntimeError("unexpected answer from the watch: %r" % text[-200:])
        return json.loads(m.group(1))

    async def reset(self):
        await self.send("\x03")                      # Ctrl-C: interrupt whatever runs
        await asyncio.sleep(0.3)
        await self.send("\x10reset();\n")            # forget loaded modules (they point at the old files)
        await asyncio.sleep(1.5)

    async def write_file(self, name, data, progress=None, done=0, total=0):
        for i in range(0, max(len(data), 1), CHUNK):
            part = data[i:i + CHUNK]
            await self.send("\x10require('Storage').write(%s,atob(%s),%d,%d);\n" % (
                json.dumps(name), json.dumps(base64.b64encode(part).decode()), i, len(data)))
            if progress:
                progress(name, done + i + len(part), total)
        size = await self.json("(require('Storage').read(%s)||'').length" % json.dumps(name))
        if size != len(data):
            raise RuntimeError("upload of %s failed (watch has %s of %d bytes)" % (name, size, len(data)))

    async def read_file(self, name, size, progress=None, done=0, total=0):
        out = bytearray()
        while len(out) < size:
            n = min(READ, size - len(out))
            text = await self.run("print('<<B'+btoa(require('Storage').read(%s,%d,%d))+'B>>')" % (
                json.dumps(name), len(out), n), timeout=20)
            m = re.search(r"<<B([A-Za-z0-9+/=]*)B>>", text)
            if not m:
                raise RuntimeError("reading %s failed at byte %d" % (name, len(out)))
            part = base64.b64decode(m.group(1))
            if len(part) != n:
                raise RuntimeError("reading %s: got %d of %d bytes at %d" % (name, len(part), n, len(out)))
            out += part
            if progress:
                progress(name, done + len(out), total)
        return bytes(out)


def _with_session(fn, address=None):
    """Run the coroutine fn(session) in a fresh connection to the watch."""
    from bleak import BleakClient
    address = address or find_bangle()
    if not address:
        raise RuntimeError("no Bangle.js found (is Bluetooth on and the watch not connected elsewhere?)")

    async def main():
        from bleak.exc import BleakError
        for attempt in range(3):                     # a reconnect right after a disconnect can fail in discovery
            try:
                c = BleakClient(address, timeout=20)
                await c.connect()
                s = Session(c)
                await c.start_notify(TX, lambda h, d: s.out.extend(d))
                break
            except (BleakError, asyncio.TimeoutError, EOFError):
                try:
                    await c.disconnect()
                except Exception:
                    pass
                if attempt == 2:
                    raise
                await asyncio.sleep(2)
        try:
            return await fn(s)
        finally:
            await c.disconnect()
    return asyncio.run(main())


# ---- uploads ---------------------------------------------------------------------------

def upload(files, address=None, code=None, progress=None, wait=1.0):
    """Reset the watch, write files ({Storage name: bytes}), check their sizes,
    then run code or go back to the clock; returns the console output."""
    async def go(s):
        await s.reset()
        total, done = sum(len(v) for v in files.values()), 0
        for name, data in files.items():
            await s.write_file(name, data, progress, done, total)
            done += len(data)
        if code:
            await s.send("\x10" + code.replace("\n", " ") + "\n")
            await asyncio.sleep(wait)
        else:
            await s.send("\x10load();\n")            # back to the clock
            await asyncio.sleep(0.5)
        return s.out.decode(errors="replace")
    return _with_session(go, address)


def model_file(name):
    """Storage name of a model: model_261031_140049_run -> prm_261031_140049_run,
    model_261031_142534_classifier -> prm_261031_142534_cls."""
    f = "prm_" + (name[6:] if name.startswith("model_") else name)
    if f.endswith("_classifier"):
        f = f[:-len("classifier")] + "cls"
    if len(f) + 4 > 28:                                # Storage names: 28 characters, with ".tfl"
        raise ValueError("model name %s is too long for the watch (%s.tfl has more than 28 characters)" % (name, f))
    return f


def model_slot(name, kind):
    """The role a model takes on the watch: cls, run, walk or speed."""
    return "cls" if kind == "classifier" else "run" if name.endswith("_run") else \
        "walk" if name.endswith("_walk") else "speed"


def model_files(js_path):
    """{prm_<id>: .js, prm_<id>.tfl: .tflite} for a model exported by prec_train.py."""
    f = model_file(os.path.basename(js_path)[:-3])
    return {f: open(js_path, "rb").read(), f + ".tfl": open(js_path[:-3] + ".tflite", "rb").read()}


def _use_js(changes):
    """JavaScript that merges changes into prec.json's models."""
    return ("(function(){var S=require('Storage'),o=S.readJSON('%s',1)||{};o.models=Object.assign(o.models||{},%s);"
            "S.writeJSON('%s',o);})()" % (SETTINGS, json.dumps(changes), SETTINGS))


def upload_model(js_path, address=None, progress=None, use=True):
    """Upload a model; with use, the prec app uses it from now on for its role
    (model_slot). Returns (Storage file, role)."""
    name = os.path.basename(js_path)[:-3]
    m = re.search(r'exports\.KIND = "(\w+)"', open(js_path).read())
    kind = m.group(1) if m else "speed"
    files, slot, f = model_files(js_path), model_slot(name, kind), model_file(name)
    upload(files, address, progress=progress, code=(_use_js({slot: f}) + ";load()") if use else None)
    return f, slot


def remove_model(file, address=None):
    """Erase a model (prm_<id> or an old pmodel, pclass, prun, pwalk) and its .tfl;
    a role that used it is left empty."""
    async def go(s):
        await s.run("require('Storage').erase(%s);require('Storage').erase(%s);"
                    "(function(){var S=require('Storage'),o=S.readJSON('%s',1)||{},m=o.models||{},old=%s;"
                    "Object.keys(old).forEach(function(k){if(m[k]===%s||(m[k]===undefined&&old[k]===%s))m[k]='';});"
                    "o.models=m;S.writeJSON('%s',o);})()" % (json.dumps(file), json.dumps(file + ".tfl"), SETTINGS,
                                                             json.dumps(OLD_MODELS), json.dumps(file), json.dumps(file),
                                                             SETTINGS))
    _with_session(go, address)


def app_files(app):
    """An app's Storage files (bangle/<app>/metadata.json), including the
    evaluated icon and the launcher .info."""
    d = os.path.abspath(os.path.join(BANGLE_DIR, app))
    meta = json.load(open(os.path.join(d, "metadata.json")))
    files = {}
    for s in meta["storage"]:
        data = open(os.path.join(d, s["url"]), "rb").read()
        if s.get("evaluate"):
            data = base64.b64decode(re.search(rb'atob\("([^"]+)"\)', data).group(1))
        files[s["name"]] = data
    info = {"id": meta["id"], "name": meta["name"], "src": "%s.app.js" % meta["id"], "icon": "%s.img" % meta["id"],
            "type": "app", "version": meta["version"],
            "files": ",".join(["%s.info" % meta["id"]] + [s["name"] for s in meta["storage"]]),
            "data": ",".join(x["name"] for x in meta.get("data", []) if "name" in x)}
    if meta.get("shortName"):
        info["name"] = meta["shortName"]
    files["%s.info" % meta["id"]] = json.dumps(info).encode()
    return files


# the old ppred app (before prec 0.03): its settings move into prec.json, its files go
MIGRATE_PPRED = """(function(){var S=require('Storage'),p=S.readJSON('ppred.json',1),o=S.readJSON('prec.json',1)||{};
if(p){['units','clsLp','speedLp','record','chunk','buffers'].forEach(function(k){if(p[k]!==undefined&&o[k]===undefined)
o[k]=p[k];});if(p.polar&&!o.polar){o.polar=p.polar;o.polars=(o.polars||[]).concat([{id:p.polar,name:'Polar H10'}]);}
S.writeJSON('prec.json',o);}['ppred.info','ppred.app.js','ppred.settings.js','ppred.img','ppred.json'].forEach(
function(f){S.erase(f);});})()"""


def install_app(app="prec", address=None, progress=None):
    """Install (or update) the app; for prec, the old ppred app's settings are
    taken over and its files removed."""
    code = (MIGRATE_PPRED.replace("\n", "") + ";load()") if app == "prec" else None
    return upload(app_files(app), address, progress=progress, code=code)


# ---- status ----------------------------------------------------------------------------

STATUS_JS = """(function(){var S=require('Storage'),st=S.getStats(),r={battery:E.getBattery(),
charging:Bangle.isCharging(),firmware:process.env.VERSION,free:st.freeBytes,total:st.totalBytes,trash:st.trashBytes,
app:global.__FILE__||'',apps:{},models:[],recordings:[],settings:S.readJSON('prec.json',1)||{},
mem:process.memory().free*process.memory().blocksize};
['prec','ppred'].forEach(function(a){var i=S.readJSON(a+'.info',1);if(i)r.apps[a]=i.version||'?';});
S.list(/^prm_/).filter(function(f){return f.substr(-4)!=='.tfl';}).concat(%s.filter(function(f){return S.read(f,0,1);}))
.forEach(function(f){var t=S.read(f);var g=function(k){var i=t.indexOf('exports.'+k+' = ');if(i<0)return;i+=k.length+11;
return t.substring(i,t.indexOf(';',i)).replace(/"/g,'');};r.models.push({file:f,name:g('NAME'),kind:g('KIND')||'speed',
freq:+g('FREQ'),win:+g('WIN'),dft:+g('DFT')||0,bytes:t.length,tfl:(S.read(f+'.tfl')||'').length});});
S.list(/^pr\\d+\\.\\d+$/).forEach(function(f){r.recordings.push([f,S.read(f).length]);});return r;})()"""


def status(address=None):
    """The watch's battery, storage, apps, models (and which ones the prec app
    uses), recordings and prec settings."""
    async def go(s):
        r = await s.json(STATUS_JS.replace("\n", "") % json.dumps(list(OLD_MODELS.values())).replace('"', "'"),
                         timeout=30)
        have = {m["file"] for m in r["models"] if m["tfl"]}
        chosen = r["settings"].get("models") or {}
        r["use"] = {}
        for k, old in OLD_MODELS.items():
            f = chosen.get(k, old)
            if f and f in have:
                r["use"][k] = f
        rec = {}
        for f, size in r["recordings"]:
            m = SESSION.match(f)
            if m:                                    # Espruino regexps have no {n}: match exactly here
                rec.setdefault(m.group(1), []).append((f, size))
        r["recordings"] = {k: sorted(v, key=lambda x: int(SESSION.match(x[0]).group(2))) for k, v in sorted(rec.items())}
        return r
    return _with_session(go, address)


def set_settings(changes, address=None):
    """Merge changes into the watch's prec.json; returns the result."""
    async def go(s):
        return await s.json("(function(){var S=require('Storage'),o=S.readJSON('%s',1)||{};"
                            "Object.assign(o,%s);S.writeJSON('%s',o);return o;})()" % (SETTINGS, json.dumps(changes),
                                                                                        SETTINGS))
    return _with_session(go, address)


# ---- recordings ------------------------------------------------------------------------

def used_length(data):
    """Bytes of a recording file holding whole PREC words: the rest is the
    file's unused 0xFF end (or a word cut off by a reset)."""
    off = 0
    while off + HEADER.size <= len(data):
        t, n, _, _ = HEADER.unpack_from(data, off)
        if t == 0xFF and n == 0xFFFF or off + HEADER.size + n > len(data):
            break
        off += HEADER.size + n
    return off


def last_word_type(data):
    """Type of the last whole PREC word in data, or None."""
    off, last = 0, None
    while off + HEADER.size <= len(data):
        t, n, _, _ = HEADER.unpack_from(data, off)
        if off + HEADER.size + n > len(data):
            break
        last, off = t, off + HEADER.size + n
    return last


def session_file(data_dir, session):
    """Local name of a watch recording: yymmddHHMMSS -> <data>/20yymmdd-HHMMSS-bangle.prc."""
    return os.path.join(data_dir, "20%s-%s-bangle.prc" % (session[:6], session[6:]))


def sync(data_dir, address=None, progress=None, delete=True):
    """Download every finished recording session from the watch into data_dir
    (joined, padding removed, checked: it must start with SESSION_START) and,
    with delete, erase its files on the watch. While the prec app runs, the
    newest session is skipped if it has no SESSION_END yet (still recording).
    progress(text, done, total). Returns {"saved": [paths], "skipped": [(session, why)]}."""
    os.makedirs(data_dir, exist_ok=True)

    async def go(s):
        st = await s.json("{app:global.__FILE__||'',files:require('Storage').list(/^pr\\d+\\.\\d+$/)"
                          ".map(function(f){return [f,require('Storage').read(f).length];})}")
        sessions = {}
        for f, size in st["files"]:
            m = SESSION.match(f)
            if m:
                sessions.setdefault(m.group(1), []).append((int(m.group(2)), f, size))
        saved, skipped = [], []
        newest = max(sessions) if sessions else None
        total = sum(size for v in sessions.values() for _, _, size in v)
        done = 0
        for sess in sorted(sessions):
            files = sorted(sessions[sess])
            dest = session_file(data_dir, sess)
            if os.path.exists(dest):
                skipped.append((sess, "%s already exists" % os.path.basename(dest)))
                continue
            data = bytearray()
            for _, f, size in files:
                raw = await s.read_file(f, size, (lambda n, d, t: progress("%s: %s" % (sess, n), d, t)) if progress
                                        else None, done, total)
                done += size
                data += raw[:used_length(raw)]
            if sess == newest and st["app"] in ("prec.app.js", "ppred.app.js") and last_word_type(data) != 0x02:
                skipped.append((sess, "still being recorded (the prec app runs and the session has not ended)"))
                continue
            if len(data) < HEADER.size or data[0] != 0x01 or data[HEADER.size:HEADER.size + 4] != b"PREC":
                skipped.append((sess, "does not start with a PREC SESSION_START word"))
                continue
            with open(dest + ".part", "wb") as fh:
                fh.write(data)
            os.replace(dest + ".part", dest)
            saved.append(dest)
            if delete:
                await s.run(";".join("require('Storage').erase(%s)" % json.dumps(f) for _, f, _ in files))
        return {"saved": saved, "skipped": skipped}
    return _with_session(go, address)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    m = sub.add_parser("model", help="upload a model (NAME.js with NAME.tflite next to it)")
    m.add_argument("js")
    m.add_argument("--keep-use", action="store_true", help="upload only: the app keeps its current models")
    sub.add_parser("rmmodel").add_argument("file", help="prm_<id>, or an old pmodel / pclass / prun / pwalk")
    sub.add_parser("app").add_argument("app", nargs="?", default="prec", choices=["prec"])
    st = sub.add_parser("set", help="change prec.json: KEY=JSON ...")
    st.add_argument("pairs", nargs="+")
    sy = sub.add_parser("sync")
    sy.add_argument("data_dir")
    sy.add_argument("--keep", action="store_true", help="leave the recordings on the watch")
    r = sub.add_parser("run")
    r.add_argument("code")
    a = ap.parse_args()
    prog = lambda n, d, t: print("\r%-30s %8d / %d bytes" % (n, d, t), end="", flush=True)
    if a.cmd == "status":
        print(json.dumps(status(a.address), indent=2))
    elif a.cmd == "model":
        print("\n%s, used for %s" % upload_model(a.js, a.address, progress=prog, use=not a.keep_use))
    elif a.cmd == "rmmodel":
        remove_model(a.file, a.address)
    elif a.cmd == "app":
        install_app(a.app, a.address, progress=prog)
    elif a.cmd == "set":
        print(json.dumps(set_settings({k: json.loads(v) for k, v in (x.split("=", 1) for x in a.pairs)}, a.address)))
    elif a.cmd == "sync":
        print(json.dumps(sync(a.data_dir, a.address, progress=prog, delete=not a.keep), indent=2))
    else:
        print(upload({}, a.address, code=a.code, wait=3))
    print()


if __name__ == "__main__":
    main()

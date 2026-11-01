"""PREC feather client library: talk to the feather over USB serial or BLE.

Synchronous API used by prec_cmd.py and prec_tui.py (see docs/TUI.md):

    f = Feather.open(transport="auto", password="prec")
    print(f.status()["state"])
    f.sync("~/prec/data", progress=print)
    f.close()

Every command raises PrecError on an "ERR <code> <text>" reply.
"""
import asyncio, glob, hashlib, os, queue, struct, threading, time, warnings, zlib

warnings.simplefilter("ignore")
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # write to the feather
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # notifications from the feather
FT_DATA = "9f3e0002-5052-4543-a5d3-6c7b4a1e2f80"  # file transfer data (SEND)
READ_CHUNK = 4096                                 # bytes per READ command


class PrecError(Exception):
    """An ERR reply (code = protocol error code, 0 for local errors)."""

    def __init__(self, code, text):
        super().__init__("%s %s" % (code, text) if code else text)
        self.code, self.text = code, text


def auth_digest(nonce, password, suffix=""):
    """SHA-256(nonce + password + suffix) as bytes."""
    return hashlib.sha256((nonce + password + suffix).encode()).digest()


def encrypt_password(nonce, old, new):
    """Hex argument of PASSWORD: new XOR SHA-256(nonce + old + "password")."""
    key = auth_digest(nonce, old, "password")
    return bytes(a ^ b for a, b in zip(new.encode(), key)).hex()


def serial_ports():
    """USB serial ports that may be a feather."""
    return sorted(glob.glob("/dev/ttyACM*"))


# ---- transports ------------------------------------------------------------

class SerialTransport:
    """USB serial (trusted channel, no password)."""
    kind = "usb"

    def __init__(self, port):
        import serial
        self.port = port
        self.s = serial.Serial(port, 115200, timeout=0)
        time.sleep(0.2)
        self.s.reset_input_buffer()
        self.name = port

    def write(self, data):
        self.s.write(data)

    def read(self, timeout):
        """Bytes received within timeout seconds (may be empty)."""
        self.s.timeout = timeout
        data = self.s.read(1)
        if data:
            self.s.timeout = 0
            data += self.s.read(65536)
        return data

    def close(self):
        self.s.close()


class BleTransport:
    """Nordic UART over BLE, driven by bleak on a private asyncio thread."""
    kind = "ble"

    def __init__(self, address):
        self.rx = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.connected = True
        try:
            self._run(self._connect(address), 30)
        except BaseException:
            self._stop_loop()
            raise
        self.name = address

    def _run(self, coro, timeout):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _connect(self, address):
        from bleak import BleakClient
        self.c = BleakClient(address, timeout=20,
                             disconnected_callback=lambda c: setattr(self, "connected", False))
        await self.c.connect()
        await self.c.start_notify(NUS_TX, lambda h, d: self.rx.put(bytes(d)))
        self.stream = None      # queue of file transfer packets once subscribed

    def subscribe_transfer(self):
        """Enable notifications on the file transfer characteristic (once).
        Returns the queue the packets arrive in, or None if the feather has none."""
        if self.stream is None:
            if not any(ch.uuid == FT_DATA for s in self.c.services for ch in s.characteristics):
                return None
            self.stream = queue.Queue()
            self._run(self.c.start_notify(FT_DATA, lambda h, d: self.stream.put(bytes(d))), 10)
        return self.stream

    async def _write(self, data):
        for i in range(0, len(data), 20):   # the NUS RX characteristic takes 20 bytes per write
            await self.c.write_gatt_char(NUS_RX, data[i:i + 20], response=True)

    def write(self, data):
        if not self.connected:
            raise PrecError(0, "BLE connection lost")
        self._run(self._write(data), 20)

    def read(self, timeout):
        try:
            data = self.rx.get(timeout=timeout)
        except queue.Empty:
            return b""
        while not self.rx.empty():
            data += self.rx.get_nowait()
        return data

    def close(self):
        try:
            self._run(self.c.disconnect(), 10)
        except Exception:
            pass
        self._stop_loop()

    def _stop_loop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2)


def find_feathers(timeout=10.0):
    """Scan for advertising feathers: [(address, name, rssi)], strongest first.
    Returns 1.5 s after the first one is seen, or after timeout seconds."""
    from bleak import BleakScanner

    async def scan():
        found = {}

        def seen(d, a):
            if (a.local_name or "").startswith("PREC-"):
                found[d.address] = (d.address, a.local_name, a.rssi)
        async with BleakScanner(detection_callback=seen):
            end, first = time.time() + timeout, None
            while time.time() < end and (first is None or time.time() < first + 1.5):
                await asyncio.sleep(0.1)
                if found and first is None:
                    first = time.time()
        return list(found.values())
    return sorted(asyncio.run(scan()), key=lambda x: -x[2])


# ---- the feather -----------------------------------------------------------

def _kv(tokens):
    return dict(t.split("=", 1) for t in tokens if "=" in t)


class Feather:
    """A connected (and, over BLE, authenticated) feather."""

    def __init__(self, transport, on_event=None):
        self.t = transport
        self.on_event = on_event or (lambda text: None)
        self.buf = b""
        self.lines = []
        self.frames = []        # USB transfer packets (SEND over USB)
        self._can_stream = None
        self.name = transport.name
        self.nonce = None

    # -- connection --
    @classmethod
    def open(cls, transport="auto", port=None, address=None, password="prec", on_event=None):
        """Connect. transport: "usb", "ble" or "auto" (USB if a port exists).
        Over BLE without address the strongest PREC-xxxx is used. Raises PrecError
        (code 401 for a wrong password, 0 when no feather is found)."""
        if transport == "auto":
            transport = "usb" if (port or serial_ports()) else "ble"
        if transport == "usb":
            port = port or (serial_ports() or [None])[0]
            if not port:
                raise PrecError(0, "no USB serial port found")
            t = SerialTransport(port)
        else:
            if not address:
                found = find_feathers()
                if not found:
                    raise PrecError(0, "no PREC feather advertising (move it to wake it up)")
                address = found[0][0]
            t = BleTransport(address)
        f = cls(t, on_event)
        try:
            args, _ = f.request("HELLO")
            f.name = args[2]
            if t.kind == "ble":
                f.request("AUTH " + auth_digest(args[3], password).hex())
        except BaseException:
            f.close()
            raise
        return f

    @property
    def kind(self):
        """"usb" or "ble"."""
        return self.t.kind

    def close(self):
        """Close the link (BLE: the feather keeps logging)."""
        self.t.close()

    # -- line protocol --
    def _pump(self, timeout):
        data = self.t.read(timeout)
        self.buf += data
        while self.buf:
            if self.buf[0] == 0xFE:          # USB transfer frame: FE 50 <u16 len> <packet>
                if len(self.buf) < 4:
                    break
                n = struct.unpack_from("<H", self.buf, 2)[0]
                if len(self.buf) < 4 + n:
                    break
                self.frames.append(self.buf[4:4 + n])
                self.buf = self.buf[4 + n:]
                continue
            i = self.buf.find(b"\n")
            if i < 0:
                break
            line = self.buf[:i].decode(errors="replace").rstrip("\r")
            self.buf = self.buf[i + 1:]
            if line.startswith("! "):
                self.on_event(line[2:])
            else:
                self.lines.append(line)
        return bool(data)

    def _next_packet(self, q, timeout):
        """Next transfer packet: from the BLE queue q, or from USB frames. None on timeout."""
        if q is not None:
            try:
                return q.get(timeout=timeout)
            except queue.Empty:
                return None
        end = time.time() + timeout
        while not self.frames and time.time() < end:
            self._pump(0.05)
        return self.frames.pop(0) if self.frames else None

    def poll(self):
        """Process pending input without sending anything (delivers events)."""
        while self._pump(0.01):
            pass

    def request(self, line, timeout=30.0):
        """Send one command; return (args after OK, list of "+" lines)."""
        self.poll()
        self.lines.clear()
        self.t.write((line + "\n").encode())
        data, end = [], time.time() + timeout
        while time.time() < end:
            self._pump(min(0.2, max(0.01, end - time.time())))
            while self.lines:
                l = self.lines.pop(0)
                if l.startswith("+ "):
                    data.append(l[2:])
                elif l.startswith("OK"):
                    return l.split()[1:], data
                elif l.startswith("ERR"):
                    p = l.split(" ", 2)
                    raise PrecError(int(p[1]) if len(p) > 1 and p[1].isdigit() else 0,
                                    p[2] if len(p) > 2 else "")
        raise PrecError(0, "timeout waiting for %s" % line.split()[0])

    # -- commands --
    def status(self):
        """STATUS as a dict of strings, plus logging (bool) and ints for bytes/bat/dropped."""
        args, _ = self.request("STATUS")
        s = _kv(args)
        s["logging"] = s.get("state") != "idle"
        for k in ("bytes", "bat", "dropped", "up"):
            if k in s:
                s[k] = int(s[k])
        return s

    def heart_rate(self):
        """HR (monitor mode): {"state", "logging", "polar" (bool), "hr" (bpm or None),
        "contact" (True/False/None), "age" (ms since the last measurement), "beats",
        "rr" ([ms]), "polar_battery" (% or None), "bat" (feather mV), "file", "bytes",
        "speed" (m/s from the models in the feather after the low-pass, or None), "raw_speed"
        (before it), "cls" (classifier's class or None), "dist" (m since logging started),
        "speed_age" (ms), "predictions", "predict_ms", "model" (names or "-"), "model_state",
        "cadence" (steps/min or None), "cadence_method" ("count", "dft" or None), "runtime" (s logging)}."""
        args, _ = self.request("HR", 5)
        s = _kv(args)
        return {"state": s["state"], "logging": s["state"] != "idle", "polar": s["polar"] == "connected",
                "hr": None if s["hr"] == "-" else int(s["hr"]),
                "contact": {"yes": True, "no": False}.get(s["contact"]),
                "age": int(s["age"]), "beats": int(s["beats"]),
                "rr": [] if s["rr"] == "-" else [int(x) for x in s["rr"].split(",")],
                "polar_battery": None if s["pbat"] == "-" else int(s["pbat"]),
                "bat": int(s["bat"]), "file": s["file"], "bytes": int(s["bytes"]),
                "speed": None if s.get("speed", "-") == "-" else float(s["speed"]),
                "raw_speed": None if s.get("rspeed", "-") == "-" else float(s["rspeed"]),
                "cls": None if s.get("cls", "-") == "-" else s["cls"], "dist": float(s.get("dist", 0)),
                "speed_age": int(s.get("sage", 0)), "predictions": int(s.get("preds", 0)),
                "predict_ms": int(s.get("pms", 0)), "model": s.get("model", "-"),
                "model_state": s.get("mstate", "unknown").replace("_", " "),
                "cadence": None if s.get("cad", "-") == "-" else int(s["cad"]),
                "cadence_method": None if s.get("cadm", "-") == "-" else s["cadm"], "runtime": int(s.get("rt", 0))}

    def get_time(self):
        """Feather clock: (unix ms, ISO text)."""
        args, _ = self.request("TIME")
        return int(args[0]), args[1]

    def set_time(self, ms=None):
        """Set the feather clock (default: this computer's clock). Returns (ms, ISO)."""
        if ms is None:
            ms = time.time_ns() // 1_000_000
        args, _ = self.request("TIME %d" % ms)
        return int(args[0]), args[1]

    def list_data(self):
        """Data files: [(name, size)]."""
        _, lines = self.request("LS")
        return [(l.split()[0], int(l.split()[1])) for l in lines]

    def read(self, name, offset, length, retries=3):
        """READ: up to length bytes of a data file. Returns (bytes, file size).
        The reply is checked against the byte count the feather reports and
        re-requested if anything was lost on the way."""
        for attempt in range(retries + 1):
            args, lines = self.request("READ %s %d %d" % (name, offset, length))
            try:
                data = bytes.fromhex("".join(lines))
            except ValueError:
                data = None
            if data is not None and len(data) == int(args[0]):
                return data, int(args[1])
        raise PrecError(0, "READ of %s at %d kept arriving incomplete" % (name, offset))

    def download(self, name, dest, progress=None, offset=0):
        """Download /prec/data/<name> into the file dest, appending from offset
        (dest must then already hold offset bytes). progress(done, total) may
        return True to cancel. Returns the bytes now in dest."""
        done, size = offset, None
        with open(dest, "ab" if offset else "wb") as out:
            while size is None or done < size:
                chunk, size = self.read(name, done, READ_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress and progress(done, size):
                    break
        return done

    def stream(self, name, dest, progress=None, offset=0):
        """Download /prec/data/<name> into dest from offset with SEND (binary
        streaming over BLE or USB; dest must already hold offset bytes). Each SEND is
        verified with the feather's byte count and CRC-32; after a gap or a stall
        the transfer continues from the last good offset. progress(done, total)
        may return True to cancel. Returns the bytes now in dest."""
        if not self.can_stream():
            raise PrecError(0, "the feather firmware does not support SEND")
        q = self.t.subscribe_transfer() if self.kind == "ble" else None
        done, stalls = offset, 0
        with open(dest, "ab" if offset else "wb") as out:
            while True:
                while self._next_packet(q, 0.01) is not None:   # leftovers of an earlier SEND
                    pass
                args, _ = self.request("SEND %s %d" % (name, done))
                size, start, crc, ok = int(args[0]), done, 0, None
                while ok is None:
                    p = self._next_packet(q, 5)
                    if p is None:
                        ok = False               # stalled
                        break
                    off = struct.unpack_from("<I", p)[0]
                    if off == 0xFFFFFFFF:
                        n, fcrc = struct.unpack_from("<II", p, 4)
                        ok = n == done - start and fcrc == crc
                        if not ok:               # should not happen: redo this SEND
                            out.truncate(start)
                            out.seek(start)
                            done = start
                        break
                    if off != done:
                        ok = False               # gap
                        break
                    data = p[4:]
                    out.write(data)
                    crc = zlib.crc32(data, crc)
                    done += len(data)
                    if progress and progress(done, size):
                        self.request("SEND STOP")
                        return done
                if ok:
                    return done
                self.request("SEND STOP")
                stalls += 1
                if stalls > 5:
                    raise PrecError(0, "transfer of %s keeps failing at %d" % (name, done))

    def can_stream(self):
        """True if the feather supports SEND on this connection (checked once)."""
        if self._can_stream is None:
            if self.kind == "ble":
                self._can_stream = self.t.subscribe_transfer() is not None
            else:
                _, lines = self.request("HELP")
                self._can_stream = any(l.startswith("SEND ") for l in lines)
        return self._can_stream

    def sync(self, directory, progress=None):
        """Copy every closed data file missing or incomplete in directory.
        Downloads go to <name>.part and are renamed when complete, so a cancelled
        sync resumes where it stopped. progress(file, done, total, file_index,
        file_count) may return True to cancel.
        Returns {"copied": [...], "skipped": [...], "busy": [...], "cancelled": bool}."""
        directory = os.path.expanduser(directory)
        os.makedirs(directory, exist_ok=True)
        logging_file = os.path.basename(self.status().get("file", "-"))
        files = self.list_data()
        report = {"copied": [], "skipped": [], "busy": [], "cancelled": False}
        todo = []
        for name, size in files:
            path = os.path.join(directory, name)
            if name == logging_file:
                report["busy"].append(name)
            elif os.path.exists(path) and os.path.getsize(path) == size:
                report["skipped"].append(name)
            else:
                todo.append((name, size))
        for i, (name, size) in enumerate(todo):
            path = os.path.join(directory, name)
            part = path + ".part"
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have > size:
                have = 0
            cancel = [False]

            def cb(done, total, name=name, i=i):
                if progress and progress(name, done, total, i, len(todo)):
                    cancel[0] = True
                    return True
            get = self.stream if self.can_stream() else self.download
            done = get(name, part, cb, have)
            if cancel[0]:
                report["cancelled"] = True
                break
            if done == size:
                os.replace(part, path)
                report["copied"].append(name)
        return report

    def scan(self, seconds=10):
        """SCAN: [(address, rssi, known, name)]; new devices are remembered."""
        _, lines = self.request("SCAN %d" % seconds, seconds + 15)
        out = []
        for l in lines:
            p = l.split(" ", 3)
            out.append((p[0], int(p[1]), p[2] == "known", p[3] if len(p) > 3 else ""))
        return out

    def devices(self):
        """DEVS: [(index, address, is_default, name)]."""
        _, lines = self.request("DEVS")
        out = []
        for l in lines:
            p = l.split(" ", 3)
            out.append((int(p[0]), p[1], p[2] == "*", p[3] if len(p) > 3 else ""))
        return out

    def set_default(self, device):
        """DEFAULT <address|index>. Returns (address, name)."""
        args, _ = self.request("DEFAULT %s" % device)
        return args[0], " ".join(args[1:])

    @staticmethod
    def _format(tokens):
        kv = _kv(tokens[2:])
        return {"type": tokens[0], "on": tokens[1] == "on",
                **{k: int(kv.get(k, 0)) for k in ("rate", "res", "range", "ch")}}

    def get_format(self):
        """FORMAT: {type: {type, on, rate, res, range, ch}} (0 = not sent)."""
        _, lines = self.request("FORMAT")
        return {f["type"]: f for f in (self._format(l.split()) for l in lines)}

    def set_format(self, mtype, on=None, **settings):
        """FORMAT <type> [on|off] [rate=..] [res=..] [range=..] [ch=..]. Returns the new format."""
        cmd = "FORMAT " + mtype
        if on is not None:
            cmd += " on" if on else " off"
        for k, v in settings.items():
            cmd += " %s=%d" % (k, v)
        args, _ = self.request(cmd)
        return self._format(args)

    def query(self):
        """QUERY the default Polar: {"features": [...], "settings": {type: {key: [values]}},
        "battery": int, "mtu": int}."""
        _, lines = self.request("QUERY", 40)
        q = {"features": [], "settings": {}}
        for l in lines:
            p = l.split()
            if p[0] == "features":
                q["features"] = [] if p[1] == "-" else p[1].split(",")
            elif p[0] in ("battery", "mtu"):
                q[p[0]] = int(p[1])
            else:
                q["settings"][p[0]] = {k: [float(x) if "." in x else int(x) for x in v.split(",")]
                                       for k, v in _kv(p[1:]).items()}
        return q

    def start_log(self):
        """LOG: {"file", "started": [...], "failed": [...]}."""
        args, _ = self.request("LOG", 45)
        kv = _kv(args[1:])
        return {"file": args[0], "started": kv.get("started", "").split(","),
                "failed": kv["failed"].split(",") if "failed" in kv else []}

    def stop_log(self):
        """STOP: (file, bytes)."""
        args, _ = self.request("STOP", 20)
        return args[0], int(args[1])

    def change_password(self, new, old):
        """PASSWORD: plain over USB, encrypted (needs the old password) over BLE."""
        if self.kind == "usb":
            self.request("PASSWORD -p " + new)
        else:
            args, _ = self.request("HELLO")
            self.request("PASSWORD " + encrypt_password(args[3], old, new))

    def get_settings(self):
        """SET: {"sleep", "reconnect", "flush", "lowbattery"} as ints."""
        args, _ = self.request("SET")
        return {k: int(v) for k, v in _kv(args).items()}

    def set_setting(self, key, value):
        """SET <key> <value>."""
        self.request("SET %s %d" % (key, value))

    def bye(self):
        """BYE and close: the feather keeps logging."""
        try:
            self.request("BYE", 3)
        except PrecError:
            pass
        self.close()

#!/usr/bin/env python3
"""Run JavaScript on a Bangle.js over the Nordic UART REPL and print the output.

usage: bangle_repl.py [-a ADDR] [-t SECONDS] [-r] [-u STORAGE_NAME LOCAL_FILE]... [CODE]

CODE is sent as one line (prefixed with \\x10 so it is not echoed).
-u uploads LOCAL_FILE to Bangle Storage as STORAGE_NAME before running CODE.
"""
import argparse, asyncio, base64, io, json, re, sys, warnings
warnings.simplefilter("ignore")
from bleak import BleakClient, BleakScanner

RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # write to Bangle
TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notifications from Bangle


async def find_bangle():
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: (a.local_name or "").startswith("Bangle.js"), timeout=15)
    if not dev:
        sys.exit("no Bangle.js found")
    return dev.address


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-a", "--address")
    ap.add_argument("-t", "--timeout", type=float, default=3.0,
                    help="seconds to keep printing output after sending")
    ap.add_argument("-u", "--upload", nargs=2, action="append", default=[],
                    metavar=("NAME", "FILE"))
    ap.add_argument("-r", "--reset", action="store_true",
                    help="reset() the interpreter first (needed before replacing a loaded module)")
    ap.add_argument("-s", "--screenshot", metavar="PNG",
                    help="after CODE (and the timeout) save the screen to PNG")
    ap.add_argument("code", nargs="?", default="")
    args = ap.parse_args()
    addr = args.address or await find_bangle()
    async with BleakClient(addr, timeout=20) as c:
        try:
            await c._backend._acquire_mtu()
        except Exception:
            pass
        chunk = 20  # the Bangle NUS RX characteristic takes at most 20 bytes per write
        out = bytearray()
        quiet = [False]

        def on_rx(h, d):
            out.extend(d)
            if b"data:image" in out[-len(d) - 16:] or quiet[0]:
                quiet[0] = True     # do not print screenshot data
                return
            sys.stdout.write(d.decode(errors="replace"))
            sys.stdout.flush()
        await c.start_notify(TX, on_rx)

        async def send(s):
            b = s.encode()
            for i in range(0, len(b), chunk):
                await c.write_gatt_char(RX, b[i:i + chunk], response=True)

        await send("\x03")
        await asyncio.sleep(0.3)
        if args.reset:
            await send("\x10reset();\n")
            await asyncio.sleep(2)
        for name, path in args.upload:
            data = open(path, "rb").read()
            # write in pieces so the Bangle's input buffer never overflows
            step = 512
            for i in range(0, len(data), step):
                part = data[i:i + step]
                js = "require('Storage').write(%s,atob(%s),%d,%d);" % (
                    json.dumps(name), json.dumps(base64.b64encode(part).decode()),
                    i, len(data))
                await send("\x10" + js + "\n")
                await asyncio.sleep(0.05)
            await send("\x10print('uploaded %s', require('Storage').read(%s).length)\n" % (name, json.dumps(name)))
            await asyncio.sleep(0.5)
        if args.code:
            await send("\x10" + args.code.replace("\n", " ") + "\n")
        await asyncio.sleep(args.timeout)
        if args.screenshot:
            out.clear()
            await send("\x10g.dump()\n")
            for _ in range(100):
                await asyncio.sleep(0.2)
                m = re.search(rb"data:image/bmp;base64,([A-Za-z0-9+/=]+)\s*\n", out)
                if m:
                    from PIL import Image
                    Image.open(io.BytesIO(base64.b64decode(m.group(1)))).convert("RGB").resize((352, 352), Image.NEAREST).save(args.screenshot)
                    print("\nscreenshot saved to", args.screenshot)
                    break


asyncio.run(main())

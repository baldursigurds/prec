#!/usr/bin/env python3
"""Send PREC feather commands over USB serial or BLE and print the replies.

usage:
  prec_cmd.py [--serial PORT | --ble [--address ADDR]] [--password PW] COMMAND [COMMAND ...]

Each COMMAND is one protocol line, e.g. "STATUS" or "FORMAT acc on rate=100".
Special commands:
  GET <file> [dest]      download a data file (SEND stream, or READ on old firmware)
  SYNC [dir]             copy all new data files (default ~/prec/data)
  SETPASSWORD <new>      change the password (encrypted over BLE)
Exit status is 1 if any command answered ERR.
"""
import argparse, os, sys
from prec_link import Feather, PrecError


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=None, help="USB serial port (default: first /dev/ttyACM*)")
    ap.add_argument("--ble", action="store_true", help="use Bluetooth instead of USB serial")
    ap.add_argument("--address", default="", help="BLE address (default: strongest PREC-xxxx)")
    ap.add_argument("--password", default="prec")
    ap.add_argument("--timeout", type=float, default=40)
    ap.add_argument("commands", nargs="+")
    a = ap.parse_args()
    try:
        f = Feather.open("ble" if a.ble else "usb", port=a.serial, address=a.address or None,
                         password=a.password, on_event=lambda e: print("!", e, flush=True))
    except PrecError as e:
        sys.exit("cannot connect: %s" % e)
    failed = False
    try:
        for cmd in a.commands:
            print(">", cmd, flush=True)
            word = cmd.split()[0].upper()
            try:
                if word == "GET":
                    p = cmd.split()
                    dest = p[2] if len(p) > 2 else p[1]
                    get = f.stream if f.can_stream() else f.download
                    n = get(p[1], dest, lambda d, t: print("\r  %d / %d bytes" % (d, t), end="", flush=True))
                    print("\n  saved %s (%d bytes)" % (dest, n))
                elif word == "SYNC":
                    p = cmd.split()
                    d = p[1] if len(p) > 1 else "~/prec/data"
                    r = f.sync(d, lambda n, done, tot, i, cnt: print("\r  [%d/%d] %s %d / %d" % (i + 1, cnt, n, done, tot), end="", flush=True))
                    print("\n  copied %d, up to date %d, being logged %s" % (len(r["copied"]), len(r["skipped"]), r["busy"] or "-"))
                elif word == "SETPASSWORD":
                    f.change_password(cmd.split(" ", 1)[1], a.password)
                    print("  OK")
                else:
                    args, lines = f.request(cmd, a.timeout)
                    for l in lines:
                        print("  +", l)
                    print("  OK", " ".join(args))
            except PrecError as e:
                print("  ERR", e)
                failed = True
    finally:
        f.close()
    return 1 if failed else 0


sys.exit(main())

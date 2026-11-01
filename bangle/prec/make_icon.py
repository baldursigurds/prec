"""app.png -> app-icon.js: the prec app's launcher icon as an Espruino image.

Up to 16 colours (one may be transparent) become a 4 bit image with a
palette (RGB565); partly transparent pixels count as transparent.
Run from bangle/prec after changing app.png: python make_icon.py"""
import base64
from PIL import Image

im = Image.open("app.png").convert("RGBA")
w, h = im.size
px = [(0, 0, 0, 0) if a < 128 else (r, g, b, 255) for r, g, b, a in im.get_flattened_data()]
colours = sorted(set(px), key=lambda c: c != (0, 0, 0, 0))     # transparent first: index 0
if len(colours) > 16:
    raise SystemExit("app.png has %d colours; at most 16 fit" % len(colours))
index = {c: i for i, c in enumerate(colours)}
pal = [((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3) for r, g, b, _ in colours] + [0] * (16 - len(colours))
transparent = (0, 0, 0, 0) in index
# header: width, height, bpp | 64 (palette) | 128 (transparent colour follows), [transparent index]
hdr = bytes([w, h, 4 | 64 | (128 if transparent else 0)]) + (bytes([0]) if transparent else b"")
bits = bytearray()
for i in range(0, len(px), 2):
    hi = index[px[i]]
    lo = index[px[i + 1]] if i + 1 < len(px) else 0
    bits.append(hi << 4 | lo)
data = hdr + b"".join(p.to_bytes(2, "little") for p in pal) + bits
open("app-icon.js", "w").write('atob("%s")\n' % base64.b64encode(data).decode())
print("app-icon.js: %dx%d, %d colours, %d bytes" % (w, h, len(colours), len(data)))

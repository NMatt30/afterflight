"""Tray icons drawn at runtime, so the five states are told apart by SHAPE.

Windows' stock icons (IDI_ERROR, IDI_WARNING, IDI_INFORMATION) were doing this
job badly: "recording" and "replay" both landed on IDI_INFORMATION and were
indistinguishable, and "idle" - a normal, healthy state - wore a yellow warning
triangle that read as a fault.

Each state gets its own silhouette as well as its own color, so the tray stays
readable in grayscale and for a colorblind user:

    down        slashed ring    watcher not running
    starting    plain ring      coming up
    idle        plain ring      watcher up, sim not connected
    connected   ring + center   ready
    recording   filled disc     the universal record dot
    replay      triangle        the universal play glyph

No pip dependency: pixels are composed in Python, handed to a DIB section, and
turned into an HICON by CreateIconIndirect. Icons are cached per state and
destroyed on shutdown - a tray that polls once a second would otherwise leak a
GDI handle per poll.
"""

import ctypes
from ctypes import Structure, byref, c_void_p
from ctypes.wintypes import BOOL, DWORD, HBITMAP, HICON, LONG, WORD

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

SM_CXSMICON = 49
BI_RGB = 0
DIB_RGB_COLORS = 0

gdi32.CreateDIBSection.restype = HBITMAP
gdi32.CreateBitmap.restype = HBITMAP
gdi32.DeleteObject.argtypes = [c_void_p]
user32.CreateIconIndirect.restype = HICON
user32.DestroyIcon.argtypes = [HICON]


class BITMAPINFOHEADER(Structure):
    _fields_ = [("biSize", DWORD), ("biWidth", LONG), ("biHeight", LONG),
                ("biPlanes", WORD), ("biBitCount", WORD), ("biCompression", DWORD),
                ("biSizeImage", DWORD), ("biXPelsPerMeter", LONG),
                ("biYPelsPerMeter", LONG), ("biClrUsed", DWORD),
                ("biClrImportant", DWORD)]


class ICONINFO(Structure):
    _fields_ = [("fIcon", BOOL), ("xHotspot", DWORD), ("yHotspot", DWORD),
                ("hbmMask", HBITMAP), ("hbmColor", HBITMAP)]


# ------------------------------------------------------------------ palette

# Saturated mid-tones, chosen to hold up on both a light and a dark taskbar.
GRAY = (150, 158, 170)
AMBER = (232, 176, 84)
TEAL = (86, 190, 214)
RED = (232, 72, 72)
GREEN = (80, 205, 137)


# ------------------------------------------------------------------ shapes
#
# Every shape is a coverage test over the unit square, sampled SS x SS times
# per pixel for antialiasing. At 16 px that is 256 samples per icon - far too
# cheap to bother optimizing, and it only runs once per state.

SS = 4


def _disc(x, y, r):
    dx, dy = x - 0.5, y - 0.5
    return (dx * dx + dy * dy) <= r * r


def _ring(x, y, r_out, r_in):
    dx, dy = x - 0.5, y - 0.5
    d2 = dx * dx + dy * dy
    return (r_in * r_in) <= d2 <= (r_out * r_out)


def _slash(x, y, half_width, r):
    """A 45-degree bar, clipped to a circle so it stays inside the ring."""
    if not _disc(x, y, r):
        return False
    # Distance from the line y = x, through the center.
    return abs((x - 0.5) + (y - 0.5)) / 1.4142135 <= half_width


def _triangle(x, y):
    """Right-pointing play glyph, apex at 0.82, back edge at 0.24."""
    if x < 0.24 or x > 0.82:
        return False
    # Half-height shrinks linearly from the back edge to the apex.
    half = 0.38 * (0.82 - x) / (0.82 - 0.24)
    return abs(y - 0.5) <= half


def shape_down(x, y):
    return _ring(x, y, 0.42, 0.28) or _slash(x, y, 0.055, 0.42)


def shape_ring(x, y):
    return _ring(x, y, 0.40, 0.24)


def shape_connected(x, y):
    return _ring(x, y, 0.42, 0.28) or _disc(x, y, 0.15)


def shape_recording(x, y):
    return _disc(x, y, 0.36)


def shape_replay(x, y):
    return _triangle(x, y)


# state -> (shape, color)
STATES = {
    "down": (shape_down, GRAY),
    "starting": (shape_ring, GRAY),
    "idle": (shape_ring, AMBER),
    "connected": (shape_connected, TEAL),
    "recording": (shape_recording, RED),
    "replay": (shape_replay, GREEN),
}
DEFAULT = "connected"


# ------------------------------------------------------------------ render


def render_bgra(shape, rgb, size):
    """Premultiplied top-down BGRA, which is what CreateIconIndirect wants."""
    r, g, b = rgb
    out = bytearray(size * size * 4)
    step = 1.0 / (size * SS)
    i = 0
    for py in range(size):
        for px in range(size):
            hits = 0
            for sy in range(SS):
                y = (py * SS + sy + 0.5) * step
                for sx in range(SS):
                    x = (px * SS + sx + 0.5) * step
                    if shape(x, y):
                        hits += 1
            if hits:
                a = (hits * 255) // (SS * SS)
                # Premultiply: the alpha channel is honored only if the color
                # channels are already scaled by it.
                out[i] = (b * a) // 255
                out[i + 1] = (g * a) // 255
                out[i + 2] = (r * a) // 255
                out[i + 3] = a
            i += 4
    return bytes(out)


def make_icon(shape, rgb, size):
    """Build an HICON. Returns None rather than raising if GDI says no."""
    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth = size
    hdr.biHeight = -size          # negative: top-down, matching our row order
    hdr.biPlanes = 1
    hdr.biBitCount = 32
    hdr.biCompression = BI_RGB

    bits = c_void_p()
    color = gdi32.CreateDIBSection(None, byref(hdr), DIB_RGB_COLORS,
                                   byref(bits), None, 0)
    if not color or not bits:
        return None

    buf = render_bgra(shape, rgb, size)
    ctypes.memmove(bits, buf, len(buf))

    # A 32bpp color bitmap carries its own alpha, but CreateIconIndirect still
    # requires a mask. An all-zero mask means "opaque everywhere" and lets the
    # alpha channel do the work.
    stride = ((size + 15) // 16) * 2
    mask = gdi32.CreateBitmap(size, size, 1, 1, bytes(stride * size))
    if not mask:
        gdi32.DeleteObject(color)
        return None

    info = ICONINFO()
    info.fIcon = True
    info.hbmMask = mask
    info.hbmColor = color
    hicon = user32.CreateIconIndirect(byref(info))

    # CreateIconIndirect copies the bitmaps; ours are ours to free.
    gdi32.DeleteObject(color)
    gdi32.DeleteObject(mask)
    return hicon or None


# ------------------------------------------------------------------ cache


class IconSet(object):
    """Lazily renders and owns one HICON per state."""

    def __init__(self, size=None):
        if size is None:
            size = user32.GetSystemMetrics(SM_CXSMICON) or 16
        self.size = size
        self._icons = {}

    def get(self, state):
        if state not in STATES:
            state = DEFAULT
        if state not in self._icons:
            shape, rgb = STATES[state]
            self._icons[state] = make_icon(shape, rgb, self.size)
        return self._icons[state]

    def close(self):
        for hicon in self._icons.values():
            if hicon:
                user32.DestroyIcon(hicon)
        self._icons = {}


# ------------------------------------------------------------------ preview


def ascii_preview(size=16):
    """py -3 trayicon.py - check the silhouettes without a taskbar."""
    ramp = " .:-=+*#%@"
    cols = []
    for name in ["down", "starting", "idle", "connected", "recording", "replay"]:
        shape, _ = STATES[name]
        raw = render_bgra(shape, (255, 255, 255), size)
        rows = []
        for py in range(size):
            row = ""
            for px in range(size):
                a = raw[(py * size + px) * 4 + 3]
                row += ramp[min(len(ramp) - 1, a * len(ramp) // 256)]
            rows.append(row)
        cols.append((name, rows))
    for i in range(0, len(cols), 3):
        group = cols[i:i + 3]
        print("  ".join(n.ljust(size) for n, _ in group))
        for r in range(size):
            print("  ".join(rows[r] for _, rows in group))
        print("")


if __name__ == "__main__":
    ascii_preview()

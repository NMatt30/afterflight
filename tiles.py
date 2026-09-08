"""OSM basemap tiles for the baked track maps.

The doc allows this explicitly - "cache tiles if we keep OSM" - with one hard
rule: never hit OSM during the flight. Callers pass allow_network=False while a
flight is live, and the map is re-baked with a basemap on a later rebuild.

Tiles are cached on disk forever (OSM raster tiles are effectively static at
these zooms), so a given area is fetched once and never again.
"""

import math
import os
import threading
import time
import urllib.error
import urllib.request

try:
    from PIL import Image, ImageEnhance
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

TILE_SIZE = 256
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# Which basemap the bake draws on.
#   osm   - OpenStreetMap standard. Best for airports, taxiways and street
#           detail; terrain reads as flat green.
#   topo  - OpenTopoMap. Contours and hill shading, which is what mountain and
#           canyon flying wants. Rendered from the same OSM data plus SRTM.
#
# OpenTopoMap is a small volunteer service with a tighter usage policy than
# OSM's, so it gets a lower zoom ceiling and the same cache-forever, fetch-only-
# when-parked treatment. Every tile is fetched once and kept.
TILE_SOURCE = "topo"
TILE_SOURCES = {
    "osm": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "subdomains": "",
        "max_zoom": 17,
        "gap_sec": 0.06,
        "attribution": "(c) OpenStreetMap contributors",
    },
    "topo": {
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "subdomains": "abc",
        # OpenTopoMap publishes to 17, but coverage thins above 16 and a miss
        # costs a request for nothing.
        "max_zoom": 16,
        # OpenTopoMap is a small volunteer service on a modest server and asks
        # people not to bulk-download. A first bake of a new area is hundreds
        # of tiles, so it gets a slower gap than OSM's.
        "gap_sec": 0.22,
        # Their wiki asks to be named, not just credited by style.
        "attribution": "map data (c) OpenStreetMap contributors, SRTM  |  "
                       "style (c) opentopomap.org (CC-BY-SA)",
    },
}


def tile_source(name=None):
    """Resolve a source name to its settings, falling back to the default."""
    return TILE_SOURCES.get(name or TILE_SOURCE) or TILE_SOURCES[TILE_SOURCE]


def attribution(name=None):
    return tile_source(name)["attribution"]

# OSM's tile usage policy requires an identifying User-Agent.
USER_AGENT = "AfterFlight/1.0 (personal flight logbook; local use)"

# 17 is enough to read taxiways and helipads; OSM raster goes to 19.
MAX_ZOOM = 17
MIN_ZOOM = 2
# Maps are supersampled (see mapbake scale), so the budget has to cover a 2x
# canvas. Still modest, and every tile is cached after the first bake.
MAX_TILES = 240
FETCH_TIMEOUT = 8.0
# Small gap between uncached fetches, so a first-time bake is not a burst.
FETCH_GAP_SEC = 0.06

# OSM tiles are designed for light UIs; dim them to sit under a dark logbook.
DIM = 0.62
DESATURATE = 0.85

# Below this share of the needed tiles, prefer no basemap at all.
MIN_TILE_COVERAGE = 0.75

ATTRIBUTION = "(c) OpenStreetMap contributors"

_fetch_lock = threading.Lock()
_last_fetch = [0.0]
# Remember failures so one offline bake does not retry every tile every time.
_failed = set()


def cache_dir(base, source=None):
    """One directory per source - the same z/x/y means a different picture."""
    name = source or TILE_SOURCE
    return os.path.join(base, "sessions", "tilecache", name)


# ---------------------------------------------------------------- mercator


def lonlat_to_px(lat, lon, z):
    """Web Mercator pixel coordinates at zoom z."""
    lat = max(min(float(lat), 85.05112878), -85.05112878)
    n = TILE_SIZE * (2 ** z)
    x = (float(lon) + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def pick_zoom(min_lat, min_lon, max_lat, max_lon, width, height, margin,
              max_zoom=None):
    """Largest zoom where the track still fits, within the tile budget."""
    inner_w = max(width - 2 * margin, 32)
    inner_h = max(height - 2 * margin, 32)
    top_zoom = min(MAX_ZOOM, int(max_zoom or MAX_ZOOM))
    for z in range(top_zoom, MIN_ZOOM - 1, -1):
        x0, y0 = lonlat_to_px(max_lat, min_lon, z)
        x1, y1 = lonlat_to_px(min_lat, max_lon, z)
        if (x1 - x0) > inner_w or (y1 - y0) > inner_h:
            continue
        nx = int(width / TILE_SIZE) + 2
        ny = int(height / TILE_SIZE) + 2
        if nx * ny > MAX_TILES:
            continue
        return z
    return MIN_ZOOM


# ---------------------------------------------------------------- fetching


def cached_path(cache, z, x, y):
    return os.path.join(cache, str(z), str(x), "%d.png" % y)


def net_allowed(allow_network):
    """Resolve the network gate, which may be a live callable.

    The watcher passes a callable so permission is re-asked per request rather
    than sampled once at the start of a build. That makes every plain
    truthiness test on this value wrong: a lambda is always truthy, so
    `if allow_network` reads "allowed" even while the callable is answering no.
    Route every read through here.
    """
    return bool(allow_network() if callable(allow_network) else allow_network)


def fetch_tile(cache, z, x, y, allow_network=True, source=None):
    """Return a tile image, from cache if possible. None if unavailable."""
    if not HAVE_PIL:
        return None
    path = cached_path(cache, z, x, y)
    if os.path.isfile(path):
        try:
            with Image.open(path) as im:
                return im.convert("RGB")
        except Exception:
            try:
                os.remove(path)      # corrupt cache entry
            except OSError:
                pass
    if not net_allowed(allow_network):
        return None
    src = tile_source(source)
    # Keyed by source: a miss on one basemap says nothing about the other.
    key = (src["url"], z, x, y)
    if key in _failed:
        return None

    subs = src.get("subdomains") or ""
    url = src["url"].format(z=z, x=x, y=y,
                            s=subs[(x + y) % len(subs)] if subs else "")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        want_gap = float(src.get("gap_sec") or FETCH_GAP_SEC)
        with _fetch_lock:
            gap = time.time() - _last_fetch[0]
            if gap < want_gap:
                time.sleep(want_gap - gap)
            _last_fetch[0] = time.time()
        if not net_allowed(allow_network):
            return None
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            data = r.read()
    except Exception:
        _failed.add(key)
        return None

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        pass
    try:
        from io import BytesIO
        with Image.open(BytesIO(data)) as im:
            return im.convert("RGB")
    except Exception:
        _failed.add(key)
        return None


# ---------------------------------------------------------------- basemap


def basemap(base, min_lat, min_lon, max_lat, max_lon, width, height, margin,
            allow_network=True, log=None, dim=None, desaturate=None,
            source=None, check=None):
    """Build a basemap image plus a lat/lon -> pixel projector.

    Returns (image, project, zoom, tiles_used) or None if no tiles could be had.
    """
    if not HAVE_PIL:
        return None
    src = tile_source(source)
    cache = cache_dir(base, source)
    z = pick_zoom(min_lat, min_lon, max_lat, max_lon, width, height, margin,
                  max_zoom=src["max_zoom"])

    # Center the viewport on the track's bounding box.
    x0, y0 = lonlat_to_px(max_lat, min_lon, z)
    x1, y1 = lonlat_to_px(min_lat, max_lon, z)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    left, top = cx - width / 2.0, cy - height / 2.0

    tx0, ty0 = int(math.floor(left / TILE_SIZE)), int(math.floor(top / TILE_SIZE))
    tx1 = int(math.floor((left + width) / TILE_SIZE))
    ty1 = int(math.floor((top + height) / TILE_SIZE))

    n = 2 ** z
    canvas = Image.new("RGB", (width, height), (32, 38, 48))
    got = 0
    wanted = 0
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            if check:
                check()
            if ty < 0 or ty >= n:
                continue
            wanted += 1
            im = fetch_tile(cache, z, tx % n, ty, allow_network=allow_network,
                            source=source)
            if im is None:
                continue
            canvas.paste(im, (int(tx * TILE_SIZE - left), int(ty * TILE_SIZE - top)))
            got += 1

    if got == 0:
        return None
    # A half-tiled map reads as broken rather than as "not fetched yet". When
    # coverage is poor and we are not allowed to fetch - during a flight, or
    # when a removal has moved the extent onto ground never cached - fall back
    # to the plain graticule so the picture at least looks deliberate.
    if wanted and (float(got) / wanted) < MIN_TILE_COVERAGE:
        if log:
            log("basemap only %d/%d tiles cached; using the plain background"
                % (got, wanted))
        return None
    if log:
        log("basemap %s z%d, %d tiles%s"
            % (src is not None and (source or TILE_SOURCE), z, got,
               "" if net_allowed(allow_network) else " (cache only)"))

    # The caller picks the treatment; DIM/DESATURATE remain the defaults so
    # anything still calling without a style behaves as it always did.
    canvas = ImageEnhance.Color(canvas).enhance(
        DESATURATE if desaturate is None else float(desaturate))
    canvas = ImageEnhance.Brightness(canvas).enhance(
        DIM if dim is None else float(dim))

    def project(lat, lon):
        px, py = lonlat_to_px(lat, lon, z)
        return (px - left, py - top)

    return canvas, project, z, got

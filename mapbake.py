"""Local track-map baking.

Rules:
  - Bake after the sortie, never during the flight.
  - No network. v1 is a plain polyline on a flat background, no OSM tiles.
  - Downsample the jsonl before drawing.
"""

import math
import os
import persistence

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except Exception:  # Pillow missing -> callers fall back to the UI polyline
    HAVE_PIL = False

# Dark palette, matches the logbook shell.
BG = (14, 17, 22)
GRID = (32, 38, 48)
TRACK = (94, 179, 255)
TRACK_GLOW = (34, 78, 122)
START = (86, 211, 129)
END = (255, 122, 122)
# Where the aircraft stopped between legs. One marker per place, not a landing
# and a takeoff on top of each other.
STOP = (255, 190, 92)
TEXT = (150, 162, 178)

EARTH_NM = 3440.065

# Maps are rendered at this multiple of the requested size. The basemap then
# comes from one zoom level deeper, so the PNG holds real detail when opened
# full size instead of a blurry upscale.
SUPERSAMPLE = 2

# Canvas is chosen from the track's own shape rather than fixed landscape. A
# north-south hop in a 1000x430 frame wastes most of the picture and forces a
# low zoom, which is why short mountain legs read as cramped and under-zoomed.
# These are logical sizes; the PNG comes out at SUPERSAMPLE times this.
MAP_LONG_SIDE_LEG = 1280
MAP_LONG_SIDE_SORTIE = 1400
# A dead-straight track would otherwise render as a sliver.
# The UI shows these full width with height:auto, so the canvas aspect IS
# the shape on screen. Fitting the canvas to the track's own aspect made
# a north-south flight portrait, which then hit max-height:70vh and got
# letterboxed inside a wide slot - hence the empty bars either side.
#
# Clamping to a landscape range costs nothing, because the basemap window
# is derived from the canvas: widening it fetches more tiles, so the extra
# space fills with real map and more context rather than blank canvas.
MAP_ASPECT_MIN = 1.80      # tallest allowed, e.g. 1280x711
MAP_ASPECT_MAX = 2.40      # widest allowed, e.g. 1280x533

# Basemap treatment, switchable rather than baked in.
#   light - close to OSM as published; terrain and place labels stay legible
#   dark  - the previous dimmed, desaturated look; suits the dark UI but
#           washes out terrain detail on mountain and canyon flying
MAP_STYLE = "light"
MAP_STYLES = {
    "light": {"dim": 0.96, "desaturate": 1.0,
              "ink": (24, 28, 34), "card": (255, 255, 255, 216),
              "card_edge": (0, 0, 0, 46), "halo": (255, 255, 255)},
    "dark": {"dim": 0.62, "desaturate": 0.85,
             "ink": (235, 240, 246), "card": (14, 18, 24, 208),
             "card_edge": (255, 255, 255, 38), "halo": (0, 0, 0)},
}


def map_style(name=None):
    """Resolve a style name to its settings, falling back to the default."""
    return MAP_STYLES.get(name or MAP_STYLE) or MAP_STYLES[MAP_STYLE]


def choose_canvas(points, long_side, aspect_min=MAP_ASPECT_MIN,
                  aspect_max=MAP_ASPECT_MAX):
    """Logical (width, height) for a track, always landscape.

    The track's own aspect is followed where it can be, and clamped into the
    landscape range where it cannot. Width is always the long side, so the
    result matches the full-width slot the UI draws it into.

    Bounds are converted to nautical miles first, so a degree of longitude
    counts for less the further north the flight is - otherwise every track
    would look wider than it really is.
    """
    long_side = int(long_side)
    pts = [p for p in (points or []) if _finite(p.get("lat")) and _finite(p.get("lon"))]
    if len(pts) < 2:
        return long_side, int(round(long_side / aspect_max))
    min_lat, min_lon, max_lat, max_lon = track_bounds(pts)
    mid = math.radians((min_lat + max_lat) / 2.0)
    y_nm = max((max_lat - min_lat) * 60.0, 1e-6)
    x_nm = max((max_lon - min_lon) * 60.0 * math.cos(mid), 1e-6)
    aspect = min(max(x_nm / y_nm, float(aspect_min)), float(aspect_max))
    return long_side, max(1, int(round(long_side / aspect)))


def _font(px):
    """A scalable font; PIL's built-in bitmap font is fixed at ~11px."""
    if not HAVE_PIL:
        return None
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, int(px))
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _finite(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and abs(f) != float("inf")


def is_spawn_junk(lat, lon):
    """The sim parks at (0, 90) before the world loads; never plot that."""
    if not (_finite(lat) and _finite(lon)):
        return True
    return abs(float(lat)) < 1e-4 and abs(abs(float(lon)) - 90.0) < 1e-3


def clean_points(points):
    """Keep only plottable lat/lon pairs."""
    out = []
    for p in points or []:
        lat = p.get("lat")
        lon = p.get("lon")
        if is_spawn_junk(lat, lon):
            continue
        out.append(p)
    return out


def _perp_nm(p, a, b, coslat):
    """Perpendicular distance of p from segment a-b, in nautical miles."""
    ax = (a["lon"] - b["lon"]) * coslat
    ay = a["lat"] - b["lat"]
    px = (p["lon"] - b["lon"]) * coslat
    py = p["lat"] - b["lat"]
    seg2 = ax * ax + ay * ay
    if seg2 <= 0.0:
        d = math.hypot(px, py)
    else:
        t = max(0.0, min(1.0, (px * ax + py * ay) / seg2))
        d = math.hypot(px - t * ax, py - t * ay)
    return math.radians(d) * EARTH_NM


def downsample(points, max_points=1200, tol_nm=0.004):
    """Douglas-Peucker on the track, then a hard cap on point count."""
    pts = clean_points(points)
    n = len(pts)
    if n <= 2:
        return pts
    coslat = math.cos(math.radians(pts[n // 2]["lat"])) or 1e-6

    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        worst = 0.0
        idx = -1
        a, b = pts[lo], pts[hi]
        for i in range(lo + 1, hi):
            d = _perp_nm(pts[i], a, b, coslat)
            if d > worst:
                worst, idx = d, i
        if idx >= 0 and worst > tol_nm:
            keep[idx] = True
            stack.append((lo, idx))
            stack.append((idx, hi))
    out = [p for p, k in zip(pts, keep) if k]

    if len(out) > max_points:  # stride, always keeping both ends
        step = len(out) / float(max_points - 1)
        thinned = [out[min(len(out) - 1, int(round(i * step)))] for i in range(max_points - 1)]
        thinned.append(out[-1])
        seen = set()
        deduped = []
        for p in thinned:
            key = (p.get("ts"), p.get("lat"), p.get("lon"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(p)
        out = deduped
    return out


# Two consecutive samples this far apart were not flown between - it is a gap
# in the recording, a reposition, or a fragment boundary. Even a 500 kt sample
# at 1 Hz covers only 0.14 nm, so this cannot split real flying.
SEGMENT_BREAK_NM = 0.35
# A piece with less extent than this never went anywhere: a parked aircraft
# recorded before the flight began, not a hover.
SEGMENT_MIN_EXTENT_NM = 0.01


def _nm_apart(a, b):
    """Great-circle nm between two points. mapbake has no haversine of its own."""
    lat1, lon1 = math.radians(float(a["lat"])), math.radians(float(a["lon"]))
    lat2, lon2 = math.radians(float(b["lat"])), math.radians(float(b["lon"]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2.0) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2)
    return 2.0 * EARTH_NM * math.asin(min(1.0, math.sqrt(h)))


def _split_on_jumps(seg):
    """Break one run of points wherever the aircraft could not have flown it."""
    out, cur = [], []
    for p in seg:
        if cur:
            try:
                jumped = _nm_apart(cur[-1], p) > SEGMENT_BREAK_NM
            except (KeyError, TypeError, ValueError):
                jumped = False
            if jumped:
                if len(cur) >= 2:
                    out.append(cur)
                cur = []
        cur.append(p)
    if len(cur) >= 2:
        out.append(cur)
    return out


def _extent_nm(seg):
    try:
        min_lat, min_lon, max_lat, max_lon = track_bounds(seg)
        return _nm_apart({"lat": min_lat, "lon": min_lon},
                         {"lat": max_lat, "lon": max_lon})
    except (KeyError, TypeError, ValueError):
        return 0.0


def as_segments(points):
    """Accept either a flat point list or a list of segments; return segments.

    Hiding a leg leaves the remaining flying in disjoint pieces. Drawing those
    as one polyline would join them with a straight line the aircraft never
    flew, so each piece is kept and stroked separately.

    A sortie that merged a reconnect arrives as one flat list even though the
    aircraft was moved between fragments, so the run is also broken wherever
    consecutive samples are further apart than flying could account for -
    otherwise the join is drawn as a route that was never taken. Pieces that
    never moved are dropped: they are the aircraft sitting on a pad before the
    flight, and they only contribute a stray marker.
    """
    if not points:
        return []
    first = points[0]
    raw = ([seg for seg in points if seg and len(seg) >= 2]
           if isinstance(first, (list, tuple)) else [points])
    out = []
    for seg in raw:
        out.extend(_split_on_jumps(seg))
    moved = [seg for seg in out if _extent_nm(seg) >= SEGMENT_MIN_EXTENT_NM]
    # Never return nothing just because the whole track is a tight hover.
    return moved if moved else out


def track_bounds(points):
    lats = [float(p["lat"]) for p in points]
    lons = [float(p["lon"]) for p in points]
    return min(lats), min(lons), max(lats), max(lons)


def _projector(points, width, height, margin):
    """Equirectangular fit with correct aspect at this latitude."""
    min_lat, min_lon, max_lat, max_lon = track_bounds(points)
    mid_lat = (min_lat + max_lat) / 2.0
    coslat = max(math.cos(math.radians(mid_lat)), 1e-6)

    span_x = max((max_lon - min_lon) * coslat, 1e-6)
    span_y = max(max_lat - min_lat, 1e-6)
    inner_w = max(width - 2 * margin, 1)
    inner_h = max(height - 2 * margin, 1)
    scale = min(inner_w / span_x, inner_h / span_y)

    off_x = margin + (inner_w - span_x * scale) / 2.0
    off_y = margin + (inner_h - span_y * scale) / 2.0

    def project(lat, lon):
        x = off_x + (float(lon) - min_lon) * coslat * scale
        y = off_y + (max_lat - float(lat)) * scale
        return (x, y)

    nm_per_px = math.radians(1.0 / scale) * EARTH_NM
    return project, nm_per_px


def _scale_bar_nm(nm_per_px, target_px):
    raw = nm_per_px * target_px
    if raw <= 0:
        return None
    power = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 5, 10):
        if mult * power >= raw * 0.55:
            return mult * power
    return 10 * power


def bake_track_png(points, out_path, width=900, height=600, label=None, max_points=1200,
                   base_dir=None, basemap=True, allow_network=True, log=None,
                   scale=SUPERSAMPLE, long_side=None, legend=None,
                   start_label=None, end_label=None, style=None,
                   tiles_source=None, start_at=None, end_at=None, stops=None, check=None):
    """Draw one track to a PNG. Returns the path, or None if it could not draw.

    With a basemap the track sits on real OSM terrain; without one it falls
    back to the plain graticule, which is what section 10 specifies for v1.
    """
    if check:
        check()
    if not HAVE_PIL:
        return None
    segments = as_segments(points)
    if not segments:
        return None
    per = max(64, int(max_points / len(segments)))
    segments = [downsample(seg, max_points=per) for seg in segments]
    segments = [seg for seg in segments if len(seg) >= 2]
    if not segments:
        return None
    pts = [p for seg in segments for p in seg]      # for bounds and projection
    if len(pts) < 2:
        return None

    # long_side fits the canvas to the track instead of forcing landscape.
    if long_side:
        width, height = choose_canvas(pts, long_side)
    st = map_style(style)

    scale = max(1, int(scale or 1))
    width = int(width) * scale
    height = int(height) * scale
    margin = 46 * scale
    font = _font(13 * scale)
    img = None
    project = None
    nm_per_px = None
    used_basemap = False

    if basemap and base_dir:
        try:
            import tiles
            min_lat, min_lon, max_lat, max_lon = track_bounds(pts)
            got = tiles.basemap(base_dir, min_lat, min_lon, max_lat, max_lon,
                                width, height, margin,
                                allow_network=allow_network, log=log,
                                dim=st["dim"], desaturate=st["desaturate"],
                                source=tiles_source, check=check)
            if got:
                img, project, zoom, _n = got
                # One mercator pixel in nm, for the scale bar.
                nm_per_px = (math.cos(math.radians((min_lat + max_lat) / 2.0))
                             * 2 * math.pi * EARTH_NM) / (256.0 * (2 ** zoom))
                used_basemap = True
        except persistence.MaintenanceDeferred:
            raise
        except Exception as exc:
            if log:
                log("basemap failed, falling back to plain: %r" % (exc,))

    if img is None:
        img = Image.new("RGB", (width, height), BG)
        project, nm_per_px = _projector(pts, width, height, margin)

    d = ImageDraw.Draw(img)

    if not used_basemap:
        for gx in range(margin, width - margin + 1, max(1, (width - 2 * margin) // 6)):
            d.line([(gx, margin), (gx, height - margin)], fill=GRID)
        for gy in range(margin, height - margin + 1, max(1, (height - 2 * margin) // 4)):
            d.line([(margin, gy), (width - margin, gy)], fill=GRID)
        d.rectangle([margin, margin, width - margin, height - margin], outline=GRID)

    r = 6 * scale
    for si, seg in enumerate(segments):
        if check:
            check()
        xy = [project(p["lat"], p["lon"]) for p in seg]
        d.line(xy, fill=TRACK_GLOW, width=7 * scale, joint="curve")
        d.line(xy, fill=TRACK, width=3 * scale, joint="curve")
        # Every kept piece gets its own start and end marker, so a gap reads as
        # a gap rather than as a missing stretch of one continuous line. The
        # very first and last are skipped when the caller names the real
        # takeoff and landing, so those get one marker each, in the right place.
        sx, sy = xy[0]
        ex, ey = xy[-1]
        if not (start_at and si == 0):
            d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=START, outline=BG, width=scale)
        if not (end_at and si == len(segments) - 1):
            d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=END, outline=BG, width=scale)

    # Over a basemap, plain text vanishes into the terrain; give it a shadow.
    def text(xy_at, s, fill=TEXT):
        x, y = xy_at
        if used_basemap:
            o = scale
            for dx, dy in ((-o, 0), (o, 0), (0, -o), (0, o)):
                d.text((x + dx, y + dy), s, fill=st["halo"], font=font)
        d.text((x, y), s, fill=fill, font=font)

    label_fill = st["ink"] if used_basemap else TEXT

    # Name the ends of the flight. Only the first start and the last end get a
    # word - the per-segment dots stay unlabeled so a gap still reads as a gap.
    def marker_text(xy_at, s, anchor_right=False, dy=0):
        x, y = xy_at
        try:
            tw = int(d.textlength(s, font=font))
        except Exception:
            tw = 7 * scale * len(s)
        x = x - tw - 10 * scale if anchor_right else x + 10 * scale
        text((x, y - 7 * scale + dy), s, label_fill)

    # None means "do not label this end"; "" means label it with no place name,
    # which is what an unnamed pad gets - the coordinates would only repeat what
    # the map already shows.
    # Prefer the caller's takeoff/landing over the ends of the recording. A
    # sortie that merged a reconnect can begin its track well before the first
    # takeoff - measured at half a mile on a Grand Canyon sortie - which put
    # START somewhere the aircraft never departed from, while the legend and
    # route names used the leg events. One source of truth now.
    first = project(*(start_at if start_at else
                      (segments[0][0]["lat"], segments[0][0]["lon"])))
    last = project(*(end_at if end_at else
                     (segments[-1][-1]["lat"], segments[-1][-1]["lon"])))
    # Intermediate stops first, so a start or landing sharing the spot wins.
    for lat_s, lon_s in (stops or []):
        try:
            sx, sy = project(float(lat_s), float(lon_s))
        except Exception:
            continue
        rs = 5 * scale
        d.ellipse([sx - rs, sy - rs, sx + rs, sy + rs],
                  fill=STOP, outline=BG, width=scale)
    if start_at:
        d.ellipse([first[0] - r, first[1] - r, first[0] + r, first[1] + r],
                  fill=START, outline=BG, width=scale)
    if end_at:
        d.ellipse([last[0] - r, last[1] - r, last[0] + r, last[1] + r],
                  fill=END, outline=BG, width=scale)
    if start_label is not None or end_label is not None:
        # Out and back from the same pad puts both labels on the same pixel,
        # which hides one of them entirely. Stack them when they collide.
        close = (abs(first[0] - last[0]) < 46 * scale
                 and abs(first[1] - last[1]) < 20 * scale)
        s_dy, e_dy = (-11 * scale, 11 * scale) if close else (0, 0)
        if start_label is not None:
            marker_text(first, ("START  %s" % start_label).strip(),
                        anchor_right=first[0] > width * 0.6, dy=s_dy)
        if end_label is not None:
            marker_text(last, ("LANDING  %s" % end_label).strip(),
                        anchor_right=last[0] > width * 0.6, dy=e_dy)

    # The compass sits top-right unless the legend card claims that corner.
    compass_right = True
    if legend:
        rows = [str(r) for r in legend if r]
        if rows:
            lf = _font(15 * scale)
            pad = 14 * scale
            line_h = 21 * scale
            try:
                tw = max(int(d.textlength(r, font=lf)) for r in rows)
            except Exception:
                tw = 9 * scale * max(len(r) for r in rows)
            bw = tw + 2 * pad
            bh = line_h * len(rows) + 2 * pad - 4 * scale
            # Park the card in whichever corner the track uses least. Fixed
            # top-left buried the START marker whenever a leg began up there.
            proj_pts = [project(q["lat"], q["lon"]) for sg in segments for q in sg]
            corners = [(margin, margin),
                       (width - margin - bw, margin),
                       (margin, height - margin - bh),
                       (width - margin - bw, height - margin - bh)]
            def _busy(c):
                x0, y0 = c
                x1, y1 = x0 + bw, y0 + bh
                near = 28 * scale
                return sum(1 for (px, py) in proj_pts
                           if x0 - near <= px <= x1 + near
                           and y0 - near <= py <= y1 + near)
            bx, by = min(corners, key=lambda c: (_busy(c), corners.index(c)))
            if bx > width / 2.0 and by < height / 2.0:
                compass_right = False
            try:
                card = Image.new("RGBA", img.size, (0, 0, 0, 0))
                cd = ImageDraw.Draw(card)
                box = [bx, by, bx + bw, by + bh]
                try:
                    cd.rounded_rectangle(box, radius=10 * scale, fill=st["card"],
                                         outline=st["card_edge"], width=scale)
                except Exception:
                    cd.rectangle(box, fill=st["card"], outline=st["card_edge"],
                                 width=scale)
                img = Image.alpha_composite(img.convert("RGBA"), card).convert("RGB")
                d = ImageDraw.Draw(img)
            except Exception as exc:
                if log:
                    log("legend card failed, text only: %r" % (exc,))
            for i, row in enumerate(rows):
                d.text((bx + pad, by + pad + i * line_h), row,
                       fill=st["ink"], font=lf)

    bar_nm = _scale_bar_nm(nm_per_px, (width - 2 * margin) * 0.25)
    if bar_nm:
        bar_px = bar_nm / nm_per_px
        by = height - margin + 18 * scale
        bx = margin
        passes = (((scale, scale), (0, 0, 0)), ((0, 0), label_fill)) if used_basemap             else (((0, 0), TEXT),)
        for (ox, oy), col in passes:
            d.line([(bx + ox, by + oy), (bx + bar_px + ox, by + oy)],
                   fill=col, width=2 * scale)
            d.line([(bx + ox, by - 4 * scale + oy), (bx + ox, by + 4 * scale + oy)],
                   fill=col, width=2 * scale)
            d.line([(bx + bar_px + ox, by - 4 * scale + oy),
                    (bx + bar_px + ox, by + 4 * scale + oy)],
                   fill=col, width=2 * scale)
        txt = ("%g nm" % bar_nm) if bar_nm >= 1 else ("%.2f nm" % bar_nm)
        text((bx + bar_px + 8 * scale, by - 8 * scale), txt, label_fill)

    ax = (width - margin - 14 * scale) if compass_right else (margin + 14 * scale)
    ay = margin + 14 * scale
    d.line([(ax, ay + 16 * scale), (ax, ay - 10 * scale)], fill=label_fill, width=2 * scale)
    d.polygon([(ax, ay - 16 * scale), (ax - 5 * scale, ay - 6 * scale),
               (ax + 5 * scale, ay - 6 * scale)], fill=label_fill)
    text((ax - 4 * scale, ay + 18 * scale), "N", label_fill)

    if label and not legend:
        text((margin, margin - 24 * scale), str(label), label_fill)

    if used_basemap:
        # Required by the OSM tile usage policy.
        try:
            import tiles as _tiles
            note = _tiles.attribution(tiles_source)
        except Exception:
            note = "(c) OpenStreetMap contributors"
        try:
            tw = int(d.textlength(note, font=font))
        except Exception:
            tw = 8 * scale * len(note)
        text((width - margin - tw, height - margin + 18 * scale), note, label_fill)

    if check:
        check()
    tmp = out_path + ".tmp"
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.save(tmp, "PNG", optimize=True)
    os.replace(tmp, out_path)
    return {"path": out_path, "basemap": used_basemap}


def polyline(points, max_points=400):
    """Compact [[lat, lon], ...] for the UI to draw with no network."""
    pts = downsample(points, max_points=max_points, tol_nm=0.01)
    return [[round(float(p["lat"]), 5), round(float(p["lon"]), 5)] for p in pts]


def polyline_segments(segments, max_points=400):
    """[[[lat, lon], ...], ...] - one list per kept piece of the flight."""
    segs = [seg for seg in (segments or []) if seg and len(seg) >= 2]
    if not segs:
        return []
    per = max(24, int(max_points / len(segs)))
    return [polyline(seg, max_points=per) for seg in segs]

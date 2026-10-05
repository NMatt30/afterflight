"""Where on the runway an airplane touched down.

Geometry only. Which runway an arrival was on, which end it landed from, and
how far past that end's threshold the wheels met the surface - worked out from
the runway the sim describes and the touchdown point the recording holds.
Nothing here grades anything; grading.py owns every threshold.

THE RUNWAY
----------
A runway is described the way the sim's facility data describes it: its
centre, the true heading of its primary end, its length and width, and how far
each end's threshold is displaced. The primary end is the one whose number
matches the heading - runway 09's primary heading is about 090, and an
airplane landing on 09 travels that way, from the west end to the east.

THE THRESHOLD
-------------
Distances are measured from the landing threshold, which is the displaced one
when there is a displacement. That is the point AC 91-79A measures the
touchdown zone from, and the point certification has the airplane cross at
50 ft. A touchdown before it is negative.

THE CHOICE
----------
An airport has several runways, and an arrival near the intersection of two of
them is near both. The one landed on is the one the aircraft was travelling
along: its heading within MATCH_HEADING_DEG of the landing direction, the
touchdown within the runway's width plus MATCH_LATERAL_FT of the centreline,
and between the two ends give or take MATCH_BEYOND_FT. Of those, the nearest
centreline wins. None qualifying is an answer too - a landing on a grass
strip the sim does not list, a taxiway, a field - and it is reported as no
runway rather than forced onto the nearest one.

    py -3 runways.py        offline self-test
"""
import datetime
import json
import math
import os

FT_PER_M = 3.28084
FT_PER_NM = 6076.12

# THE CACHE
# ---------
# One file per airport under sessions/runways/, written by the watcher after a
# landing and read by the builder. Runway geometry is static scenery, so an
# airport is fetched once. These files say where the owner has landed, which
# is why they live under sessions/ and never in git.
CACHE_SCHEMA = 1

# What the sim's facility data actually sends, as measured against a running
# sim rather than taken from the SDK: lengths and widths in METRES (Seattle's
# three runways came back within 0.1% of their published lengths once
# converted), headings in degrees true for the primary end, displaced
# thresholds as two child records in definition order, primary first (San
# Diego's 27 came back second, at 1,808 ft against a published 1,810).
# Designators 1-3 are L, R, C - Seattle read 16L/34R, 16C/34C, 16R/34L. The
# rest are the SDK's order and have not been seen.
DESIGNATORS = {0: "", 1: "L", 2: "R", 3: "C", 4: "W", 5: "A", 6: "B"}

MATCH_HEADING_DEG = 30.0
MATCH_LATERAL_FT = 75.0
MATCH_BEYOND_FT = 500.0


def _angle_apart(a, b):
    d = abs(float(a) - float(b)) % 360.0
    return min(d, 360.0 - d)


def _local_ft(lat0, lon0, lat, lon):
    """(east, north) feet from (lat0, lon0). Flat over a few miles, which is all it needs."""
    north = (float(lat) - float(lat0)) * 60.0 * FT_PER_NM
    east = ((float(lon) - float(lon0)) * 60.0 * FT_PER_NM
            * math.cos(math.radians(float(lat0))))
    return east, north


def beside_threshold(hit, lat, lon):
    """Feet from the extended centreline of a located runway, right positive."""
    h = math.radians(float(hit["landing_heading"]))
    east, north = _local_ft(hit["threshold_lat"], hit["threshold_lon"], lat, lon)
    return east * math.cos(h) - north * math.sin(h)


def offset_ft(lat, lon, bearing_deg, ft):
    """The point ft feet from (lat, lon) along bearing_deg. Flat, like _local_ft."""
    b = math.radians(bearing_deg)
    dn, de = ft * math.cos(b), ft * math.sin(b)
    return (lat + dn / (60.0 * FT_PER_NM),
            lon + de / (60.0 * FT_PER_NM * math.cos(math.radians(lat))))


def end_ident(number, designator=None):
    """'09', '27L' - the painted name of a runway end."""
    if number is None:
        return None
    try:
        n = int(number)
    except (TypeError, ValueError):
        return str(number)
    return "%02d%s" % (n, designator or "")


def locate(runway, lat, lon, track_deg):
    """Where a touchdown point lies on one runway, or None if not on it.

    runway: {lat, lon, heading, length_ft, width_ft,
             primary, secondary,                 painted names of the two ends
             primary_displaced_ft, secondary_displaced_ft}
    track_deg: the direction the aircraft was travelling at touchdown.

    Returns the landing end's name, the distance past its threshold (negative
    before it), the distance from the centreline (positive right of it, looking
    down the runway), the runway length and the landing distance available
    from that threshold - and where that threshold is and which way the runway
    is landed on, so any other point of the arrival can be placed against it
    (past_threshold).
    """
    try:
        h = float(runway["heading"])
        length = float(runway["length_ft"])
        width = float(runway.get("width_ft") or 0.0)
        east, north = _local_ft(runway["lat"], runway["lon"], lat, lon)
    except (KeyError, TypeError, ValueError):
        return None
    ux, uy = math.sin(math.radians(h)), math.cos(math.radians(h))
    along = east * ux + north * uy           # from the centre, toward the primary's far end
    across = east * uy - north * ux          # right of the primary direction

    if _angle_apart(track_deg, h) <= MATCH_HEADING_DEG:
        end, from_end, lateral = "primary", along + length / 2.0, across
    elif _angle_apart(track_deg, h + 180.0) <= MATCH_HEADING_DEG:
        end, from_end, lateral = "secondary", length / 2.0 - along, -across
    else:
        return None
    if abs(lateral) > width / 2.0 + MATCH_LATERAL_FT:
        return None
    if from_end < -MATCH_BEYOND_FT or from_end > length + MATCH_BEYOND_FT:
        return None

    displaced = float(runway.get(end + "_displaced_ft") or 0.0)
    # The landing threshold, on the centreline: the near end plus any
    # displacement, in the direction of landing.
    landing_h = h % 360.0 if end == "primary" else (h + 180.0) % 360.0
    thr = offset_ft(float(runway["lat"]), float(runway["lon"]), landing_h,
                    displaced - length / 2.0)
    return {
        "runway": runway.get(end),
        "distance_ft": round(from_end - displaced),
        "lateral_ft": round(lateral),
        "length_ft": round(length),
        "available_ft": round(length - displaced),
        "threshold_lat": round(thr[0], 7),
        "threshold_lon": round(thr[1], 7),
        "landing_heading": round(landing_h, 2),
    }


def past_threshold(hit, lat, lon):
    """Feet past the landing threshold of a located runway, negative before it.

    Along the runway's direction only, so it is the same number for a point
    on the extended centreline and for one beside it - which is what "over
    the threshold" means for an aircraft that is not exactly lined up.
    """
    h = math.radians(float(hit["landing_heading"]))
    east, north = _local_ft(hit["threshold_lat"], hit["threshold_lon"], lat, lon)
    return east * math.sin(h) + north * math.cos(h)


def touchdown_on(runways, lat, lon, track_deg):
    """The runway landed on, of an airport's runways, or None."""
    best = None
    for rw in runways or []:
        hit = locate(rw, lat, lon, track_deg)
        if hit is None:
            continue
        if best is None or abs(hit["lateral_ft"]) < abs(best["lateral_ft"]):
            best = hit
    return best


def nm_apart(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    x = (math.sin((p2 - p1) / 2.0) ** 2 + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(float(lon2) - float(lon1)) / 2.0) ** 2)
    return 2.0 * 3440.065 * math.asin(min(1.0, math.sqrt(x)))


# --------------------------------------------------------------------- cache

def from_facility(ident, region, airport, runways):
    """A cache document from decoded facility records.

    airport: (lat, lon, alt_m, n_runways)
    runways: [(lat, lon, alt_m, heading, length_m, width_m, primary_number,
               primary_designator, secondary_number, secondary_designator,
               primary_displaced_m, secondary_displaced_m)]
    """
    out = []
    for r in runways:
        (lat, lon, _alt, heading, length_m, width_m, pn, pd, sn, sd,
         pdisp_m, sdisp_m) = r
        out.append({
            "lat": lat, "lon": lon, "heading": heading,
            "length_ft": round(length_m * FT_PER_M, 1),
            "width_ft": round(width_m * FT_PER_M, 1),
            "primary": end_ident(pn, DESIGNATORS.get(pd, "")),
            "secondary": end_ident(sn, DESIGNATORS.get(sd, "")),
            "primary_displaced_ft": round((pdisp_m or 0.0) * FT_PER_M, 1),
            "secondary_displaced_ft": round((sdisp_m or 0.0) * FT_PER_M, 1),
        })
    return {
        "schema": CACHE_SCHEMA, "ident": ident, "region": region,
        "lat": airport[0], "lon": airport[1],
        "alt_ft": round(airport[2] * FT_PER_M, 1),
        "source": "sim facility data",
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "runways": out,
    }


# Airport names, one file beside the airport files: {"schema", "names":
# {ident: name}}. Kept apart so that adding a name never rewrites an airport's
# runway file - those are the evidence landings are graded against, and are
# never replaced automatically. load_airport refuses it - it has no ident or
# runways - so it never enters the airport index.
NAMES_FILE = "_names.json"
NAMES_SCHEMA = 1


def names_path(cache_dir):
    return os.path.join(cache_dir, NAMES_FILE)


def clean_name(text):
    """An airport name as a person reads it. The sim's are UTF-8, measured,
    but one hospital's carried a no-break space and an invisible left-to-
    right mark; both go, and runs of spaces become one."""
    import unicodedata
    text = "".join(" " if unicodedata.category(c) == "Zs" else c
                   for c in str(text) if unicodedata.category(c) != "Cf")
    return " ".join(text.split())


def load_names(cache_dir, asked=False):
    """{ident: name} for the airports whose names the sim has given, or {}.

    With asked, also the airports it was asked about and gave no name for,
    as "" - so they are not asked again."""
    try:
        with open(names_path(cache_dir), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    if not (isinstance(doc, dict) and doc.get("schema") == NAMES_SCHEMA
            and isinstance(doc.get("names"), dict)):
        return {}
    return {str(k): clean_name(v) for k, v in doc["names"].items()
            if isinstance(v, str) and (asked or clean_name(v))}


def names_doc(names):
    return {"schema": NAMES_SCHEMA, "names": dict(sorted(names.items()))}


# Helipads, one file beside the airport files as the names are, for the same
# reason: {"schema", "helipads": {ident: [{lat, lon, heading, length_ft,
# width_ft}]}}. An empty list is an airport asked about that has none.
HELIPADS_FILE = "_helipads.json"
HELIPADS_SCHEMA = 1


def helipads_path(cache_dir):
    return os.path.join(cache_dir, HELIPADS_FILE)


def load_helipads(cache_dir):
    """{ident: [pad]} for every airport asked about, or {}."""
    try:
        with open(helipads_path(cache_dir), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    if not (isinstance(doc, dict) and doc.get("schema") == HELIPADS_SCHEMA
            and isinstance(doc.get("helipads"), dict)):
        return {}
    out = {}
    for ident, pads in doc["helipads"].items():
        if isinstance(pads, list):
            out[str(ident)] = [p for p in pads if isinstance(p, dict)
                               and isinstance(p.get("lat"), (int, float))
                               and isinstance(p.get("lon"), (int, float))]
    return out


def helipads_doc(pads):
    return {"schema": HELIPADS_SCHEMA, "helipads": dict(sorted(pads.items()))}


# A helicopter's end, off every pad. Both are owner's settings, applied over
# these defaults (settings.py, "Route names").
#
# On an airport's pavement: within its parking circles, or within half a taxi
# path's width of the path, plus PAVEMENT_MARGIN_FT - aprons are not in the
# sim's data, and a helicopter parked on one between marked stands sat 100-160
# ft from anything mapped. 200 ft is about a stand's depth beside its
# taxilane: a judgment, labelled as one, chosen by the owner.
PAVEMENT_MARGIN_FT = 200.0
# Off every pad and every airport's pavement, an airport with runways whose
# reference point is within this radius. The owner's choice: pavement first,
# the radius as the backup.
HELICOPTER_AIRPORT_RADIUS_NM = 0.5
# Whether the radius backs up the pavement at every airport (True, the
# owner's reading of "radius as a backup") or only at an airport the sim maps
# no pavement for (False - stricter: a field beside an airport whose
# pavement is mapped then keeps its coordinates).
RADIUS_WHERE_PAVEMENT_MAPPED = True
# Where the position point sits on a helicopter on a pad: an AS365 is 14 m
# long. A judgment from the airframe, not fitted to flights.
HELIPAD_MARGIN_FT = 30.0

# An airport's ground - parking circles and taxi paths - one file per airport
# under ground/, since a large airport's is hundreds of kilobytes and the
# airport index reads every file beside it. Fetched only for airports near a
# helicopter's end that was on no pad. A file with nothing in it is an
# airport the sim gave no ground for.
GROUND_DIR = "ground"
GROUND_SCHEMA = 1


def ground_path(cache_dir, ident):
    return cache_path(os.path.join(cache_dir, GROUND_DIR), ident)


def ground_doc(ident, parkings, paths):
    """parkings: [(lat, lon, radius_ft)]; paths: [(lat1, lon1, lat2, lon2,
    width_ft)]."""
    r = lambda v: round(float(v), 7)
    return {"schema": GROUND_SCHEMA, "ident": ident,
            "parkings": [[r(a), r(b), round(float(c), 1)] for a, b, c in parkings],
            "paths": [[r(a), r(b), r(c), r(d), round(float(w), 1)] for a, b, c, d, w in paths]}


def load_ground(cache_dir, ident):
    """An airport's ground as saved, or None when it has not been asked."""
    try:
        with open(ground_path(cache_dir, ident), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not (isinstance(doc, dict) and doc.get("schema") == GROUND_SCHEMA
            and isinstance(doc.get("parkings"), list) and isinstance(doc.get("paths"), list)):
        return None
    return doc


def _segment_ft(lat, lon, a_lat, a_lon, b_lat, b_lon):
    ax, ay = _local_ft(lat, lon, a_lat, a_lon)
    bx, by = _local_ft(lat, lon, b_lat, b_lon)
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
    return math.hypot(ax + t * dx, ay + t * dy)


def off_runways_ft(airport, lat, lon):
    """Feet from this point to the nearest of an airport's runways - 0 on
    one, its rectangle being its length by its width - or None without any."""
    best = None
    for rw in (airport or {}).get("runways") or []:
        try:
            e, n = _local_ft(rw["lat"], rw["lon"], lat, lon)
            h = math.radians(float(rw["heading"]))
            along = e * math.sin(h) + n * math.cos(h)
            across = e * math.cos(h) - n * math.sin(h)
            d = math.hypot(max(0.0, abs(along) - float(rw["length_ft"]) / 2.0),
                           max(0.0, abs(across) - float(rw["width_ft"]) / 2.0))
        except (KeyError, TypeError, ValueError):
            continue
        best = d if best is None else min(best, d)
    return best


def off_pavement_ft(ground, lat, lon):
    """Feet from this point to the airport's mapped pavement - 0 inside a
    parking circle or on a taxi path - or None when nothing is mapped."""
    if not ground:
        return None
    best = None
    near_deg = 0.05                         # about 3 nm: nothing further can matter
    for p_lat, p_lon, radius in ground.get("parkings") or []:
        if abs(p_lat - lat) > near_deg:
            continue
        d = max(0.0, nm_apart(lat, lon, p_lat, p_lon) * FT_PER_NM - radius)
        best = d if best is None else min(best, d)
    for a_lat, a_lon, b_lat, b_lon, width in ground.get("paths") or []:
        if abs(a_lat - lat) > near_deg:
            continue
        d = max(0.0, _segment_ft(lat, lon, a_lat, a_lon, b_lat, b_lon) - width / 2.0)
        best = d if best is None else min(best, d)
    if best is None and (ground.get("parkings") or ground.get("paths")):
        return float("inf")
    return best


def on_helipad(pads, lat, lon, margin_ft):
    """The pad this point is on, as (distance_ft, pad), or None.

    On is within the pad's own half-diagonal - its corner, from its centre -
    plus margin_ft for where the aircraft's position point sits on its body.
    """
    best = None
    for pad in pads or []:
        reach = 0.5 * math.hypot(float(pad.get("length_ft") or 0.0),
                                 float(pad.get("width_ft") or 0.0)) + margin_ft
        d = nm_apart(lat, lon, pad["lat"], pad["lon"]) * FT_PER_NM
        if d <= reach and (best is None or d < best[0]):
            best = (d, pad)
    return best


def cache_path(cache_dir, ident):
    safe = "".join(ch for ch in str(ident) if ch.isalnum() or ch in "-_")
    return os.path.join(cache_dir, (safe or "unnamed") + ".json")


def load_index(cache_dir):
    """{ident: (lat, lon, path)} for every cached airport. Unreadable files are skipped."""
    out = {}
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(cache_dir, name)
        doc = load_airport(path)
        if doc:
            out[doc["ident"]] = (doc["lat"], doc["lon"], path)
    return out


def load_airport(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not (isinstance(doc, dict) and doc.get("schema") == CACHE_SCHEMA
            and doc.get("ident") and isinstance(doc.get("runways"), list)):
        return None
    try:
        float(doc["lat"]); float(doc["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return doc


def airports_near(index, lat, lon, radius_nm):
    """Paths of cached airports within radius_nm, nearest first."""
    found = []
    for ident, (a_lat, a_lon, path) in (index or {}).items():
        d = nm_apart(lat, lon, a_lat, a_lon)
        if d <= radius_nm:
            found.append((d, path))
    return [p for _d, p in sorted(found)]


def runway_at(index, lat, lon, radius_nm=3.0):
    """True if a cached runway contains this point, landed on either way.

    What "this landing's runway is known" means. An airport being near is not
    it: a heliport 2.5 nm from a landing, cached on the way past, made the
    landing count as covered, and the airport it was really at was never
    asked about. The same tolerances as a touchdown match (locate), checked
    along the primary direction - containment is the same either way.
    """
    for path in airports_near(index, lat, lon, radius_nm):
        doc = load_airport(path)
        for rw in (doc or {}).get("runways") or []:
            try:
                if locate(rw, lat, lon, float(rw["heading"])) is not None:
                    return True
            except (KeyError, TypeError, ValueError):
                continue
    return False


def touchdown_at(index, lat, lon, track_deg, radius_nm=3.0):
    """The runway a touchdown was on, from the cache, or None.

    Every cached airport within radius_nm is tried: a touchdown near a
    boundary can be closer to one airport's reference point than to the
    airport whose runway it is actually on.
    """
    best = None
    for path in airports_near(index, lat, lon, radius_nm):
        doc = load_airport(path)
        if not doc:
            continue
        hit = touchdown_on(doc["runways"], lat, lon, track_deg)
        if hit and (best is None or abs(hit["lateral_ft"]) < abs(best["lateral_ft"])):
            best = dict(hit, airport=doc["ident"])
    return best


# --------------------------------------------------------------------- tests

_offset = offset_ft


def _self_test():
    rw = {"lat": 47.0, "lon": -122.0, "heading": 90.0, "length_ft": 8000.0,
          "width_ft": 150.0, "primary": "09", "secondary": "27",
          "primary_displaced_ft": 0.0, "secondary_displaced_ft": 1000.0}
    west_end = _offset(47.0, -122.0, 270.0, 4000.0)
    east_end = _offset(47.0, -122.0, 90.0, 4000.0)

    # Landing on 09, 1,200 ft past the west end, on the centreline.
    p = _offset(west_end[0], west_end[1], 90.0, 1200.0)
    r = locate(rw, p[0], p[1], 92.0)
    assert r and r["runway"] == "09" and abs(r["distance_ft"] - 1200) <= 2, r
    assert abs(r["lateral_ft"]) <= 2 and r["available_ft"] == 8000, r

    # Landing on 27 from the east, whose threshold is displaced 1,000 ft:
    # 2,500 ft from the end is 1,500 ft past the threshold.
    p = _offset(east_end[0], east_end[1], 270.0, 2500.0)
    r = locate(rw, p[0], p[1], 268.0)
    assert r and r["runway"] == "27" and abs(r["distance_ft"] - 1500) <= 2, r
    assert r["available_ft"] == 7000, r
    # Any point of the arrival measures against the same displaced threshold:
    # the touchdown itself, one on the approach, and one off the centreline.
    assert abs(past_threshold(r, p[0], p[1]) - 1500) <= 2, r
    q = _offset(east_end[0], east_end[1], 90.0, 2000.0)
    assert abs(past_threshold(r, q[0], q[1]) + 3000) <= 2
    q = _offset(*_offset(east_end[0], east_end[1], 270.0, 1000.0), 0.0, 60.0)
    assert abs(past_threshold(r, q[0], q[1])) <= 2
    # 60 ft north of runway 27 is to the right of an aircraft landing west.
    assert abs(beside_threshold(r, q[0], q[1]) - 60) <= 2, beside_threshold(r, q[0], q[1])

    # Before the threshold is negative, not discarded.
    p = _offset(west_end[0], west_end[1], 270.0, 300.0)
    r = locate(rw, p[0], p[1], 90.0)
    assert r and abs(r["distance_ft"] + 300) <= 2, r

    # 50 ft right of the centreline, landing on 09 (right is south).
    p = _offset(*_offset(west_end[0], west_end[1], 90.0, 1500.0), 180.0, 50.0)
    r = locate(rw, p[0], p[1], 90.0)
    assert r and abs(r["lateral_ft"] - 50) <= 2, r

    # Crossing the runway is not landing on it.
    p = _offset(47.0, -122.0, 0.0, 0.0)
    assert locate(rw, p[0], p[1], 0.0) is None
    # Off to the side by more than the width allows.
    p = _offset(*_offset(west_end[0], west_end[1], 90.0, 1500.0), 0.0, 300.0)
    assert locate(rw, p[0], p[1], 90.0) is None
    # Well past the far end.
    p = _offset(east_end[0], east_end[1], 90.0, 2000.0)
    assert locate(rw, p[0], p[1], 90.0) is None

    # Two runways crossing at the centre: the one being travelled along wins.
    cross = dict(rw, heading=0.0, primary="36", secondary="18",
                 secondary_displaced_ft=0.0)
    p = _offset(west_end[0], west_end[1], 90.0, 4000.0)       # the intersection
    r = touchdown_on([cross, rw], p[0], p[1], 91.0)
    assert r and r["runway"] == "09", r
    r = touchdown_on([cross, rw], p[0], p[1], 2.0)
    assert r and r["runway"] == "36", r

    # A heading that wraps through north still matches.
    north_rw = dict(rw, heading=359.0, primary="36", secondary="18")
    south_end = _offset(47.0, -122.0, 179.0, 4000.0)
    p = _offset(south_end[0], south_end[1], 359.0, 800.0)
    r = locate(north_rw, p[0], p[1], 3.0)
    assert r and r["runway"] == "36" and abs(r["distance_ft"] - 800) <= 3, r

    assert end_ident(9) == "09" and end_ident(27, "L") == "27L"

    # The cache, from records shaped as the sim sends them: metres, and a
    # displaced secondary threshold (San Diego's 27, 551 m = 1,808 ft).
    import shutil, tempfile
    d = tempfile.mkdtemp()
    try:
        doc = from_facility("KTST", "K2", (47.0, -122.0, 100.0, 1),
                            [(47.0, -122.0, 100.0, 90.0, 2864.0, 45.7,
                              9, 0, 27, 0, 0.0, 551.0)])
        rw = doc["runways"][0]
        assert rw["primary"] == "09" and rw["secondary"] == "27", rw
        assert abs(rw["length_ft"] - 9396) < 2, rw
        assert abs(rw["secondary_displaced_ft"] - 1808) < 2, rw
        with open(cache_path(d, "KTST"), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        with open(os.path.join(d, "broken.json"), "w") as f:
            f.write("{not json")
        idx = load_index(d)
        assert list(idx) == ["KTST"], idx
        east_end = _offset(47.0, -122.0, 90.0, 4698.0)
        p = _offset(east_end[0], east_end[1], 270.0, 3000.0)   # 3,000 ft from the east end
        hit = touchdown_at(idx, p[0], p[1], 270.0)
        assert hit and hit["airport"] == "KTST" and hit["runway"] == "27", hit
        assert abs(hit["distance_ft"] - (3000 - 1808)) <= 3, hit
        assert touchdown_at(idx, 48.0, -122.0, 270.0) is None, "found a runway 60 nm away"
        assert cache_path(d, "../evil") == os.path.join(d, "evil.json")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("runways: self-test ok")


if __name__ == "__main__":
    _self_test()

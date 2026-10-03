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

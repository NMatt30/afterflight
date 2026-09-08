"""Flight tracks as KML and GPX, for viewing somewhere with real terrain.

The baked PNGs answer "where did I go". This answers "what did that look like
over the actual ground", which wants a 3D viewer and a basemap we are not going
to draw ourselves.

Deliberately not Google-specific. The same KML opens in Google Earth, and the
GPX in ForeFlight, SkyDemon, QGIS and most flight-tracking tools; framing this
as "export the track" rather than "open in Google" gets more for the same work
and keeps working if Google changes something.

Pure string building from the session tree: no network, no API key, no pip
dependency. Generated on demand rather than baked like the maps, so there is
nothing on disk to go stale and nothing to invalidate.

    py -3 trackexport.py     # offline self-test
"""
import io
import json
import os
from xml.sax.saxutils import escape

SCHEMA = 1

FT_TO_M = 0.3048

# A long sortie can be tens of thousands of points and none of them are needed
# to see the shape of it. Endpoints are always kept.
EXPORT_MAX_POINTS = 3000

# How the altitude in the file should be read by the viewer.
#
#   "absolute"         - meters above sea level, which is what the sim recorded
#                        and therefore the truth about the flight.
#   "relativeToGround" - meters above the terrain, from the recorded AGL.
#
# They look different because MSFS terrain and Google Earth terrain do not
# agree: an absolute path can visibly clip into a hillside or float above a
# ridge in mountainous country, which reads as a bug in the export rather than
# a difference of elevation data. relativeToGround hugs whatever terrain the
# viewer has, at the cost of no longer being the altitude actually flown.
# Absolute is the default because it is the honest one.
ALTITUDE_MODE = "absolute"

# Not every track carries AGL, so relativeToGround
# falls back to absolute rather than inventing a height for older ones.
_AGL_MODES = ("relativeToGround",)


def _finite(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f not in (float("inf"), float("-inf"))


def usable(p):
    return _finite(p.get("lat")) and _finite(p.get("lon"))


def read_track(sessions_dir, flight_ids, t0=None, t1=None):
    """Points for these flight records, optionally clipped to a time window.

    t0/t1 are ISO strings as the track writes them, compared as text: they all
    carry the same offset from the same writer, so this is ordering without
    parsing thousands of timestamps.
    """
    out = []
    for fid in flight_ids or []:
        path = os.path.join(sessions_dir, str(fid) + ".jsonl")
        if not os.path.isfile(path):
            continue
        with io.open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                except ValueError:
                    continue
                if not usable(p):
                    continue
                ts = p.get("ts")
                if t0 and ts and ts < t0:
                    continue
                if t1 and ts and ts > t1:
                    continue
                out.append(p)
    out.sort(key=lambda p: p.get("ts") or "")
    return out


def decimate(points, max_points=EXPORT_MAX_POINTS):
    """Even stride, endpoints kept. Enough to see the shape of a flight."""
    n = len(points)
    if max_points <= 2 or n <= max_points:
        return list(points)
    step = float(n - 1) / (max_points - 1)
    out = [points[int(round(i * step))] for i in range(max_points)]
    if out[-1] is not points[-1]:
        out[-1] = points[-1]
    return out


def _alt_m(p, mode):
    """Altitude in meters for the requested mode, or None."""
    if mode in _AGL_MODES and _finite(p.get("agl")):
        return float(p["agl"]) * FT_TO_M
    if _finite(p.get("alt")):
        return float(p["alt"]) * FT_TO_M
    return None


def effective_mode(points, mode):
    """The mode actually usable for these points.

    Asking for height above ground on a track recorded without AGL
    captured would put the whole track on the deck. Say so by falling back
    rather than drawing something wrong.
    """
    if mode in _AGL_MODES and not any(_finite(p.get("agl")) for p in points):
        return ALTITUDE_MODE
    return mode if mode in ("absolute", "relativeToGround", "clampToGround") \
        else ALTITUDE_MODE


def _coords(points, mode):
    rows = []
    for p in points:
        alt = _alt_m(p, mode)
        rows.append("%.6f,%.6f,%.1f" % (float(p["lon"]), float(p["lat"]),
                                        alt if alt is not None else 0.0))
    return "\n".join(rows)


def _placemark(name, p, style):
    if not p or not usable(p):
        return ""
    return (
        '<Placemark><name>%s</name><styleUrl>#%s</styleUrl>'
        '<Point><coordinates>%.6f,%.6f,0</coordinates></Point></Placemark>'
        % (escape(str(name)), style, float(p["lon"]), float(p["lat"])))


def kml(points, name, description=None, mode=ALTITUDE_MODE,
        start=None, end=None):
    """A KML document for one leg or sortie.

    extrude draws the curtain down to the ground, which is what makes a climb
    and descent readable rather than a line floating in space.
    """
    pts = [p for p in points if usable(p)]
    if len(pts) < 2:
        return None
    mode = effective_mode(pts, mode)
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        "<name>%s</name>" % escape(str(name)),
    ]
    if description:
        body.append("<description>%s</description>" % escape(str(description)))
    body += [
        '<Style id="track"><LineStyle>'
        # KML color is aabbggrr, not rrggbb.
        '<color>ff2fa8ff</color><width>3</width></LineStyle>'
        '<PolyStyle><color>402fa8ff</color></PolyStyle></Style>',
        '<Style id="takeoff"><IconStyle><color>ff54c46a</color>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/paddle/grn-circle.png</href></Icon>'
        "</IconStyle></Style>",
        '<Style id="landing"><IconStyle><color>ff4b4bff</color>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/paddle/red-circle.png</href></Icon>'
        "</IconStyle></Style>",
        "<Placemark><name>%s</name><styleUrl>#track</styleUrl>" % escape(str(name)),
        "<LineString><extrude>1</extrude><tessellate>1</tessellate>",
        "<altitudeMode>%s</altitudeMode>" % mode,
        "<coordinates>%s</coordinates>" % _coords(pts, mode),
        "</LineString></Placemark>",
    ]
    body.append(_placemark(start or "Takeoff", pts[0], "takeoff"))
    body.append(_placemark(end or "Landing", pts[-1], "landing"))
    body.append("</Document></kml>")
    return "\n".join(x for x in body if x)


def gpx(points, name):
    """A GPX track. Elevation is meters above sea level, per the format."""
    pts = [p for p in points if usable(p)]
    if len(pts) < 2:
        return None
    rows = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<gpx version="1.1" creator="AfterFlight" '
            'xmlns="http://www.topografix.com/GPX/1/1">',
            "<trk><name>%s</name><trkseg>" % escape(str(name))]
    for p in pts:
        alt = _alt_m(p, "absolute")
        row = '<trkpt lat="%.6f" lon="%.6f">' % (float(p["lat"]), float(p["lon"]))
        if alt is not None:
            row += "<ele>%.1f</ele>" % alt
        if p.get("ts"):
            row += "<time>%s</time>" % escape(str(p["ts"]))
        rows.append(row + "</trkpt>")
    rows.append("</trkseg></trk></gpx>")
    return "\n".join(rows)


def maps_url(lat, lon):
    """Google Maps with a pin dropped on one point.

    The search form, not map_action=map: Google's documentation says that one
    "returns a map with no markers or directions", which is a map of nothing -
    it centered the view and showed no trace of the flight or even of the point
    being asked about. The URL API supports no way to show several markers at
    once, and no way to draw an arbitrary polyline, so one point per link and
    the track goes to KML.
    """
    if not (_finite(lat) and _finite(lon)):
        return None
    return ("https://www.google.com/maps/search/?api=1&query=%.6f,%.6f"
            % (float(lat), float(lon)))


def safe_name(text):
    """A filename that Windows will accept."""
    out = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in str(text))
    return out.strip("-") or "track"


def self_test():
    pts = [{"lat": 40.0, "lon": -105.0, "alt": 5000.0, "agl": 100.0, "ts": "t1"},
           {"lat": 40.1, "lon": -105.1, "alt": 6000.0, "agl": 900.0, "ts": "t2"},
           {"lat": 40.2, "lon": -105.2, "alt": 7000.0, "agl": 50.0, "ts": "t3"}]

    doc = kml(pts, "Leg 1 <test> & co")
    assert doc and "<altitudeMode>absolute</altitudeMode>" in doc
    # KML is lon,lat,alt - the order is the classic way to get this wrong, and
    # a swapped track lands in the wrong hemisphere without erroring.
    assert "-105.000000,40.000000,1524.0" in doc, doc[:400]
    assert "&lt;test&gt; &amp; co" in doc, "name must be XML-escaped"
    assert doc.count("<Placemark>") == 3, "track plus both ends"

    agl = kml(pts, "agl", mode="relativeToGround")
    assert "<altitudeMode>relativeToGround</altitudeMode>" in agl
    assert "-105.000000,40.000000,30.5" in agl, "AGL feet -> meters"

    # A flight with no AGL must not be drawn on the deck.
    old = [{k: v for k, v in p.items() if k != "agl"} for p in pts]
    assert "<altitudeMode>absolute</altitudeMode>" in kml(
        old, "old", mode="relativeToGround"), "must fall back, not flatten"
    assert effective_mode(old, "relativeToGround") == "absolute"

    g = gpx(pts, "leg")
    assert '<trkpt lat="40.000000" lon="-105.000000">' in g
    assert "<ele>1524.0</ele>" in g, "GPX elevation is meters"

    assert kml(pts[:1], "short") is None and gpx(pts[:1], "short") is None

    big = [{"lat": 40.0 + i * 1e-5, "lon": -105.0, "alt": 1.0} for i in range(9000)]
    small = decimate(big, 500)
    assert len(small) == 500
    assert small[0] is big[0] and small[-1] is big[-1], "endpoints are kept"
    assert decimate(big[:10], 500) == big[:10], "short tracks pass through"

    # The pin form. map_action=map draws no marker at all, which is the bug
    # this replaced, so assert on the part that makes it show something.
    assert maps_url(40.0, -105.0) == (
        "https://www.google.com/maps/search/?api=1&query=40.000000,-105.000000")
    assert maps_url(None, -105.0) is None
    assert safe_name("flt-2026/09:03 leg1") == "flt-2026-09-03-leg1"
    return True


if __name__ == "__main__":
    print("offline self-test:", "PASS" if self_test() else "FAIL")

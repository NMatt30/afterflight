"""Where on the runway an airplane touched down, from end to end.

  the geometry          runways.py's own self-test
  the score             grading's touchdown-zone curve
  the parsing           the watcher's facility messages, built in the layout
                        measured against a running sim
  the builder           a leg gets its touchdown point from the runway cache
  the cache signature   a runway cached after a landing rebuilds that sortie,
                        and only that sortie
  the settings          a band set in settings.json moves the score and the
                        letter together, rebuilds what it changes, and never
                        reaches a helicopter - nor makes the watcher ask the
                        sim about one

What the sim sends was measured live before any of this was written: message
ids 18, 28 and 29, a 36-byte airport-list entry, 28- and 52-byte airport and
runway records, lengths in metres, displaced thresholds primary first. Those
facts are encoded here so a change that drifts from them fails offline.

Synthetic throughout: no recording, no runway the owner has landed on.

    py -3 test_runways.py
"""
import json
import math
import os
import shutil
import struct
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import grading                                            # noqa: E402
import logbook_build                                      # noqa: E402
import runways                                            # noqa: E402
import watcher                                            # noqa: E402
watcher.log = lambda *a, **k: None      # never append to the operational log

LAT0, LON0 = 47.0, -122.0               # synthetic; nowhere in particular


def test_the_geometry_self_test():
    runways._self_test()


# --------------------------------------------------------------------------
# the score
# --------------------------------------------------------------------------

def test_the_touchdown_zone_curve():
    s = grading.touchdown_point_score
    assert s(0, 12000) == 100 and s(1400, 12000) == 100
    assert abs(s(3000, 12000) - grading.TDZ_END_SCORE) < 1e-6
    assert s(4000, 12000) == 0
    # A shorter runway's zone ends at its first third.
    assert abs(s(7000 / 3.0, 7000) - grading.TDZ_END_SCORE) < 1e-6
    # Inside the zone it never falls below the end-of-zone score.
    for d in range(0, 3001, 50):
        assert s(d, 12000) >= grading.TDZ_END_SCORE, d
    # Landing short is not good at any distance past the allowance.
    assert s(-grading.TDZ_SHORT_ALLOWANCE_FT - 1, 12000) == 0
    assert s(-grading.TDZ_SHORT_ALLOWANCE_FT + 1, 12000) == 100
    # It never rises as the touchdown moves down the runway.
    last = 101.0
    for d in range(0, 5001, 25):
        v = s(d, 12000)
        assert v <= last + 1e-9
        last = v


def test_off_by_default():
    assert grading.GRADE_TOUCHDOWN_POINT is False, (
        "the touchdown point caps landings by default; it was to stay off "
        "until reviewed against real landings")
    part, score = grading._touchdown_point_part(
        {"score": 0.0, "distance_ft": 5000.0}, 80.0)
    assert part["counted"] is False and score == 80.0, (
        "switched off, a touchdown spot past the zone still lowered the landing")


# --------------------------------------------------------------------------
# the parsing, against the measured layout
# --------------------------------------------------------------------------

def _airport_list_parts(entries, per_part=3):
    """Message 18, split into parts the way the sim splits it."""
    parts = []
    chunks = [entries[i:i + per_part] for i in range(0, len(entries), per_part)]
    for k, chunk in enumerate(chunks):
        body = b"".join(ident.encode().ljust(9, b"\0") + region.encode().ljust(3, b"\0")
                        + struct.pack("<ddd", la, lo, al)
                        for ident, region, la, lo, al in chunk)
        head = struct.pack("<III", 28 + len(body), 6, 18)
        parts.append(head + struct.pack("<IIII", 0, len(chunk), k, len(chunks)) + body)
    return parts


def _facility(uniq, parent, typ, data):
    head = struct.pack("<III", 40 + len(data), 6, 28)
    return head + struct.pack("<IIIIIII", 0, uniq, parent, typ, 0, 0, 0) + data


class _FakeConn(object):
    """A GameSimConnect with the sim replaced by canned replies."""

    def __init__(self, list_parts=None, runway_parts=None):
        g = object.__new__(watcher.GameSimConnect)
        import threading
        g._lock = threading.Lock()
        g._fac, g._fac_defined, g._quit = {}, False, False
        g._next_req = 1
        g.handle = None
        g._h = lambda: None
        self.added = []

        def req_list(h, typ, rid):
            with g._lock:
                g._fac[rid]["parts"].extend(list_parts or [])
            return 0

        def add(h, define, field):
            self.added.append(field.decode())
            return 0

        def req_fac(h, define, rid, ident, region):
            with g._lock:
                g._fac[rid]["parts"].extend(runway_parts or [])
                g._fac[rid]["done"] = True
            return 0

        g.fns = {"SimConnect_RequestFacilitiesList": req_list,
                 "SimConnect_AddToFacilityDefinition": add,
                 "SimConnect_RequestFacilityData": req_fac}
        self.g = g


def test_the_airport_list_is_read_in_the_measured_layout():
    entries = [("KTST", "K2", 47.0, -122.0, 100.0), ("ABCDEFGHI", "XY", 1.5, 2.5, 3.0),
               ("L41", "K2", 36.8, -111.6, 1098.9), ("Q", "", -10.0, 20.0, 0.0)]
    conn = _FakeConn(list_parts=_airport_list_parts(entries))
    got = conn.g.facility_airports(timeout=1.0)
    assert got == entries, "airport list read as %r" % (got,)


def test_a_part_missing_from_the_list_is_not_a_complete_list():
    entries = [("A%d" % i, "K1", 10.0 + i, 20.0, 0.0) for i in range(7)]
    parts = _airport_list_parts(entries)
    conn = _FakeConn(list_parts=parts[:-1])
    assert conn.g.facility_airports(timeout=0.3) is None, (
        "a list with a part missing was returned as though complete")


def test_runways_are_read_in_the_measured_layout():
    airport = _facility(1, 0, 0, struct.pack("<dddi", 47.0, -122.0, 100.0, 1))
    runway = _facility(2, 1, 1, struct.pack("<dddfffiiii", 47.0, -122.0, 100.0,
                                            106.06, 2864.0, 45.7, 9, 0, 27, 1))
    primary_thr = _facility(3, 2, 23, struct.pack("<f", 216.2))
    secondary_thr = _facility(4, 2, 23, struct.pack("<f", 551.0))
    conn = _FakeConn(runway_parts=[airport, runway, primary_thr, secondary_thr])
    doc = conn.g.facility_runways("KTST", "K2", timeout=1.0)
    assert doc and doc["ident"] == "KTST" and len(doc["runways"]) == 1, doc
    rw = doc["runways"][0]
    assert rw["primary"] == "09" and rw["secondary"] == "27L", rw
    assert abs(rw["length_ft"] - 2864.0 * runways.FT_PER_M) < 1, "length not read as metres"
    assert abs(rw["primary_displaced_ft"] - 216.2 * runways.FT_PER_M) < 1, rw
    assert abs(rw["secondary_displaced_ft"] - 551.0 * runways.FT_PER_M) < 1, (
        "the second threshold record was not the secondary end's")
    assert conn.added == list(watcher.FACILITY_FIELDS), "the definition sent differs"


def _name_record(text_bytes):
    """NAME64 as the sim sent it, measured: the airport record (type 0), 64
    bytes of text ending at a zero byte, then whatever was in memory."""
    data = (text_bytes + b"\0" + b"A\0\x88\x95\x89;\xf6\x7f\xb7\xd9")[:64].ljust(64, b"\xb7")
    return _facility(1, 0, 0, data)


def _name(conn):
    return conn.g.facility_details("X", "", timeout=1.0)["name"]


def _pad_record(lat, lon, length_m, width_m, heading=0.0):
    """A helipad as the sim sent it, measured: type 4, 44 bytes - latitude,
    longitude, altitude (doubles), heading, length, width (floats, metres),
    surface, type (ints)."""
    return _facility(5, 1, 4, struct.pack("<dddfffii", lat, lon, 733.8,
                                          heading, length_m, width_m, 0, 0))


def test_an_airport_name_is_read_in_the_measured_layout():
    conn = _FakeConn(runway_parts=[_name_record(b"Zurich")])
    got = conn.g.facility_details("LSZH", "LS", timeout=1.0)
    assert got == {"name": "Zurich", "helipads": []}, (
        "the bytes after the name's zero were read as part of it: %r" % (got,))
    assert conn.added == list(watcher.AIRPORT_NAME_FIELDS)
    long = b"Municipal Acoustics Center Madrid City Council"
    assert _name(_FakeConn(runway_parts=[_name_record(long)])) == long.decode()
    full = b"N" * 64                                   # no zero at all
    assert _name(_FakeConn(runway_parts=[_facility(1, 0, 0, full)])) == "N" * 64
    assert watcher.airport_name_from(b"TT:AIRPORT.KXXX.NAME\0".ljust(64, b"\0")) == "", (
        "a localisation key was shown as a name")
    # A record that is not the airport's is no name.
    assert _name(_FakeConn(runway_parts=[_facility(2, 1, 1, b"Runway".ljust(64, b"\0"))])) == ""
    # Measured: UTF-8, with a no-break space and an invisible mark in one.
    hospital = b"Our Lady Of Sonsoles Hospital,\xc2\xa0\xc3\x81vila\xe2\x80\x8e"
    assert _name(_FakeConn(runway_parts=[_name_record(hospital)])) == (
        "Our Lady Of Sonsoles Hospital, \u00c1vila")


def test_helipads_are_read_in_the_measured_layout():
    parts = [_name_record(b"Example Tower Heliport"),
             _pad_record(LAT0 + 0.001, LON0, 20.7, 20.7, 101.6),
             _pad_record(LAT0 + 0.002, LON0, 21.0, 21.0, 11.7)]
    got = _FakeConn(runway_parts=parts).g.facility_details("XHEL", "XX", timeout=1.0)
    assert got["name"] == "Example Tower Heliport"
    assert len(got["helipads"]) == 2, got
    p = got["helipads"][0]
    assert abs(p["lat"] - (LAT0 + 0.001)) < 1e-9 and abs(p["lon"] - LON0) < 1e-9, p
    assert abs(p["length_ft"] - 20.7 * runways.FT_PER_M) < 0.2, "pad sizes not read as metres"
    assert p["heading"] == 101.6
    # A record of another size is not a pad.
    odd = _facility(5, 1, 4, struct.pack("<dddfffi", 1.0, 2.0, 3.0, 0.0, 20.0, 20.0, 0))
    assert _FakeConn(runway_parts=[odd]).g.facility_details("X", "", timeout=1.0)["helipads"] == []


def test_on_a_helipad_is_inside_its_corner_plus_the_margin():
    pad = {"lat": LAT0, "lon": LON0, "length_ft": 60.0, "width_ft": 80.0}
    reach = 50.0 + 30.0                              # half of the 100 ft diagonal, + margin
    inside = _east_of(LAT0, LON0, reach - 1.0)
    outside = _east_of(LAT0, LON0, reach + 1.0)
    assert runways.on_helipad([pad], inside[0], inside[1], 30.0)
    assert runways.on_helipad([pad], outside[0], outside[1], 30.0) is None


def test_names_are_asked_for_once_and_kept_beside_the_runways():
    """Every cached airport is named once - the ones cached before names
    were asked for too - in a file of its own: the airport files, the
    evidence landings are graded against, are not rewritten."""
    look = Lookup()
    try:
        look.run()
        assert sorted(look.named) == ["KAAA", "KBBB"], look.named
        files = {n: os.path.getmtime(os.path.join(look.dir, n))
                 for n in os.listdir(look.dir) if not n.startswith("_")}
        assert runways.load_names(look.dir) == {"KAAA": "Name of KAAA", "KBBB": "Name of KBBB"}
        assert set(runways.load_index(look.dir)) == {"KAAA", "KBBB"}, (
            "the names file was read as an airport")
        assert look.rebuilds, "names arrived and the logbook was not rebuilt"

        # Another session: the backfill runs again, and asks nobody twice.
        look.named[:] = []
        watcher._runway_state.update(backfilled=False)
        look.run()
        assert look.named == [], "named again: %r" % look.named
        assert files == {n: os.path.getmtime(os.path.join(look.dir, n))
                         for n in os.listdir(look.dir) if not n.startswith("_")}, (
            "an airport file was rewritten")
    finally:
        look.close()


def test_an_airport_cached_before_names_gets_one_at_the_backfill():
    """Nothing left to look up, but an airport with no name yet: the
    once-a-session backfill asks for it alone."""
    look = Lookup()
    try:
        look.run()
        os.remove(runways.names_path(look.dir))
        look.named[:] = []
        look.rebuilds[:] = []
        watcher._runway_state.update(backfilled=False)
        look.run()
        assert sorted(look.named) == ["KAAA", "KBBB"], look.named
        assert look.lists == 1, "the airport list was asked for with nothing to look up"
        assert look.rebuilds, "names arrived and the logbook was not rebuilt"
    finally:
        look.close()


def test_an_airport_named_before_helipads_is_asked_again_for_them():
    """An install that named its airports before helipads were asked for
    has every name and no pads; the backfill asks again, once."""
    look = Lookup()
    try:
        look.run()
        os.remove(runways.helipads_path(look.dir))
        look.named[:] = []
        watcher._runway_state.update(backfilled=False)
        look.run()
        assert sorted(look.named) == ["KAAA", "KBBB"], look.named
        assert set(runways.load_helipads(look.dir)) == {"KAAA", "KBBB"}
    finally:
        look.close()


def test_a_name_that_fails_is_asked_for_next_time_and_costs_no_landing():
    look = Lookup()
    try:
        look.name_for = lambda ident: None if ident == "KBBB" else "Name of " + ident
        look.run()
        assert watcher._runway_queue == [], "a failed name kept the landing queued"
        assert runways.load_names(look.dir, asked=True) == {"KAAA": "Name of KAAA"}
        look.name_for = lambda ident: "Name of " + ident
        look.named[:] = []
        watcher._runway_state.update(backfilled=False)
        look.run()
        assert look.named == ["KBBB"], look.named
    finally:
        look.close()


def test_names_breaking_never_costs_the_runways():
    """Names are tooltips. A name request that raises leaves the runways
    saved and the landing done, with a note in the log."""
    look = Lookup()
    try:
        def boom(ident):
            raise RuntimeError("names broke")
        look.name_for = boom
        look.run()
        assert set(runways.load_index(look.dir)) == {"KAAA", "KBBB"}
        assert watcher._runway_queue == [] and watcher._runway_state["backfilled"], (
            "a names failure cost the landing its finished lookup")
    finally:
        look.close()


def test_no_name_is_asked_for_once_the_aircraft_moves():
    look = Lookup()
    try:
        # Moving during the runway requests: no names at all.
        look.on_request = lambda ident: look.parked(False)
        look.run()
        assert look.named == [], "named %r while moving" % look.named
        # Moving during the names: the one in hand, and no more.
        look.on_request = None
        look.parked(True)
        watcher._runway_state.update(retry_after=0.0)

        def then_move(ident):
            look.parked(False)
            return "Name of " + ident
        look.name_for = then_move
        look.run()
        assert len(look.named) == 1, "named %r after the aircraft moved" % look.named
    finally:
        look.close()


def test_the_logbook_carries_the_names_of_the_airports_it_uses():
    """The page's tooltips read {ident: name} from the index. Only airports
    a leg used, and a name arriving later rebuilds no flight."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        t.runway()
        _departure_runway(t)
        t.runway("KFAR", lat=LAT0 + 2.0)
        _leg(t)
        doc = _logbook(t)
        assert doc.get("airport_names") == {}, doc.get("airport_names")
        # The row the page draws: the fixture's 1970 dates make no month
        # file, so the summary is taken from the flight's own detail.
        detail = json.load(open(os.path.join(t.dir, "detail", FID + ".json"), encoding="utf-8"))
        row = logbook_build.summarize_sortie(detail)
        assert (row["route_from_airport"], row["route_to_airport"]) == ("KDEP", "KTST"), row

        def sig():
            flights = logbook_build.scan_flights()
            group = logbook_build.group_sorties(flights)[0]
            events = {}
            for line in open(logbook_build.EVENTS_JSONL, encoding="utf-8"):
                e = json.loads(line)
                events.setdefault(e["flight_id"], []).append(e)
            return logbook_build.sortie_signature(group, "g", events, FID, None, {})
        before = sig()
        d = os.path.join(t.dir, "runways")
        with open(runways.names_path(d), "w", encoding="utf-8") as f:
            json.dump(runways.names_doc({"KDEP": "Departure Field", "KTST": "Test Intl",
                                         "KFAR": "Far Away", "KNONE": ""}), f)
        assert sig() == before, "a name rebuilt the flight"
        logbook_build.build(bake_maps=False, allow_network=False, force=False)
        assert _logbook(t)["airport_names"] == {"KDEP": "Departure Field", "KTST": "Test Intl"}, (
            _logbook(t)["airport_names"])
    finally:
        t.close()


def test_nothing_is_sent_to_an_aircraft():
    """The lookup is facility data only; it must never reach for an object id."""
    import inspect
    for fn in (watcher.GameSimConnect.facility_airports,
               watcher.GameSimConnect.facility_runways, watcher.lookup_runways):
        src = inspect.getsource(fn)
        for forbidden in ("SetDataOnSimObject", "TransmitClientEvent", "CameraSet",
                          "AICreate", "object_id"):
            assert forbidden not in src, "%s mentions %s" % (fn.__name__, forbidden)


# --------------------------------------------------------------------------
# the builder and the cache signature
# --------------------------------------------------------------------------

FID = "flt-19990101T000000Z"


class Tree(object):
    NAMES = ("BASE", "SESSIONS", "CLIPS_DIR", "EVENTS_JSONL", "LOGBOOK_JSON",
             "CACHE_JSON", "EXCLUDED_JSON", "DETAIL_DIR")

    def __init__(self):
        self.root = tempfile.mkdtemp()
        self.dir = os.path.join(self.root, "sessions")
        self._keep = {n: getattr(logbook_build, n) for n in self.NAMES}
        logbook_build.BASE = self.root
        logbook_build.SESSIONS = self.dir
        logbook_build.CLIPS_DIR = os.path.join(self.dir, "clips")
        logbook_build.EVENTS_JSONL = os.path.join(self.root, "events.jsonl")
        logbook_build.LOGBOOK_JSON = os.path.join(self.root, "logbook.json")
        logbook_build.CACHE_JSON = os.path.join(self.root, "logbook.cache.json")
        logbook_build.EXCLUDED_JSON = os.path.join(self.root, "excluded.json")
        logbook_build.DETAIL_DIR = os.path.join(self.dir, "detail")
        os.makedirs(logbook_build.CLIPS_DIR)
        logbook_build._runway_memo["key"] = None

    def close(self):
        for n, v in self._keep.items():
            setattr(logbook_build, n, v)
        logbook_build._runway_memo["key"] = None
        shutil.rmtree(self.root, ignore_errors=True)

    def runway(self, ident="KTST", lat=LAT0, lon=LON0):
        """An east-west runway, 9,400 ft, centred on (lat, lon)."""
        d = os.path.join(self.dir, "runways")
        os.makedirs(d, exist_ok=True)
        doc = runways.from_facility(ident, "K2", (lat, lon, 0.0, 1),
                                    [(lat, lon, 0.0, 90.0, 2865.0, 45.0, 9, 0, 27, 0, 0.0, 0.0)])
        with open(runways.cache_path(d, ident), "w", encoding="utf-8") as f:
            json.dump(doc, f)


def _east_of(lat, lon, ft):
    return lat, lon + ft / (60.0 * runways.FT_PER_NM * math.cos(math.radians(lat)))


def _flight(tree, touchdown_past_threshold_ft, slope=0.0, cliff_ft=0.0):
    """A jet arriving eastbound on runway 09 and touching down where asked.

    slope tilts the runway's surface (rise per foot past the threshold, its
    plane extended before it); cliff_ft drops the ground before the threshold
    below that plane. agl is the height above the ground beneath, as the sim
    reports it; with neither, the runway is flat at 0 ft.
    """
    thr = _east_of(LAT0, LON0, -2865.0 * runways.FT_PER_M / 2.0)
    td = _east_of(thr[0], thr[1], touchdown_past_threshold_ft)
    fps = 120 * 1.68781
    rows, t = [], 1000.0
    # climb out from somewhere west, cruise, descend, land: enough for phases
    alt = 0.0
    for k in range(900):
        x = (k - 900) * fps
        la, lo = _east_of(td[0], td[1], x)
        phase_alt = min(8000.0, max(0.0, -x * 0.0524)) if k > 600 else min(8000.0, k * 30.0)
        on_ground = k < 20
        rows.append({"t": t, "lat": la, "lon": lo, "alt": 0.0 if on_ground else max(60.0, phase_alt),
                     "on_ground": on_ground, "gs": 0 if on_ground else 120, "vs": 0.0})
        t += 1.0
    for k in range(30):
        la, lo = _east_of(td[0], td[1], k * 100.0)
        rows.append({"t": t, "lat": la, "lon": lo, "alt": 0.0, "on_ground": True, "gs": 60, "vs": 0.0})
        t += 1.0
    # Tilt the world to the runway's slope, near it, and record the height
    # above the ground beneath. past is feet past the threshold along the
    # runway, which these rows fly straight down.
    for r in rows:
        e, n = runways._local_ft(thr[0], thr[1], r["lat"], r["lon"])
        past = e
        plane = slope * max(past, -3000.0)
        terrain = plane - (cliff_ft if past < 0 else 0.0)
        r["alt"] = r["alt"] + plane
        r["agl"] = r["alt"] - terrain
    t_land = next(r["t"] for r in rows[25:] if r["on_ground"])
    import datetime as dt
    for r in rows:
        r["ts"] = dt.datetime.fromtimestamp(r["t"], dt.timezone.utc).isoformat()
    with open(os.path.join(tree.dir, FID + ".jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({k: v for k, v in r.items() if k != "t"}) + "\n")
    with open(os.path.join(tree.dir, FID + ".meta.json"), "w", encoding="utf-8") as f:
        json.dump({"flight_id": FID, "sortie_id": FID, "aircraft": "Test Jet",
                   "category": "Airplane", "vs0": 90.0, "ended_at": rows[-1]["ts"]}, f)
    t_to = rows[20]["t"]
    with open(os.path.join(tree.root, "events.jsonl"), "w", encoding="utf-8") as f:
        for kind, tt in (("takeoff", t_to), ("landing", t_land)):
            r = next(x for x in rows if x["t"] >= tt)
            f.write(json.dumps({"time": r["ts"], "kind": kind, "flight_id": FID,
                                "sortie_id": FID, "leg": 1, "lat": r["lat"],
                                "lon": r["lon"], "rate_fpm": 150.0 if kind == "landing" else None,
                                "aircraft": "Test Jet"}) + "\n")
    return td


def _leg(tree, force=True):
    logbook_build.build(bake_maps=False, allow_network=False, force=force)
    with open(os.path.join(tree.dir, "detail", FID + ".json"), encoding="utf-8") as f:
        return json.load(f)["legs"][0]


def test_a_leg_gets_its_touchdown_point_from_the_cache():
    t = Tree()
    try:
        _flight(t, 1800.0)
        assert _leg(t).get("touchdown_point") is None, (
            "a touchdown point appeared with no runway cached")
        t.runway()
        tp = _leg(t).get("touchdown_point")
        assert tp, "no touchdown point with the runway cached"
        assert tp["airport"] == "KTST" and tp["runway"] == "09", tp
        assert abs(tp["distance_ft"] - 1800) < 130, (
            "touched down 1,800 ft past the threshold, measured %d" % tp["distance_ft"])
        assert abs(tp["score"] - grading.touchdown_point_score(tp["distance_ft"], tp["length_ft"])) < 0.1
    finally:
        t.close()


def test_no_runway_means_no_float_and_no_touchdown_point():
    """Off airport, or before the watcher has looked the runway up: nothing
    says where the runway began, so a float counted from 50 ft would count
    the approach. Neither measure is taken."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        leg = _leg(t)
        assert leg.get("touchdown_point") is None
        assert leg.get("landing_float") is None, (
            "a float was measured with no runway to measure it over")
        t.runway()
        leg = _leg(t)
        assert leg.get("landing_float"), "no float with the runway cached"
        # The fixture flies a 3-degree path to its touchdown point, so it is
        # 1,800 x tan(3 deg), about 94 ft, over a threshold 1,800 ft before.
        h = leg["touchdown_point"].get("threshold_height_ft")
        assert h is not None and abs(h - 94) < 6, (
            "crossed the threshold at about 94 ft, measured %r" % h)
    finally:
        t.close()


def test_the_threshold_height_is_measured_against_a_sloped_runway():
    """The builder reads the runway's shape from the track's height above
    ground, so a crossing reads the same on a runway that climbs - and the
    ground falling away before the threshold is not the runway."""
    t = Tree()
    try:
        _flight(t, 1800.0, slope=0.02, cliff_ft=300.0)
        # One reading from the downwind leg, abeam the threshold half a mile
        # to the north over lower ground: not the runway.
        path = os.path.join(t.dir, FID + ".jsonl")
        rows = [json.loads(l) for l in open(path, encoding="utf-8")]
        td = next(i for i in range(25, len(rows)) if rows[i]["on_ground"])
        thr = _east_of(LAT0, LON0, -2865.0 * runways.FT_PER_M / 2.0 - 10.0)
        r = rows[td - 40]
        r["lat"], r["lon"] = thr[0] + 3000.0 / (60.0 * runways.FT_PER_NM), thr[1] + 20.0 / (60.0 * runways.FT_PER_NM * math.cos(math.radians(thr[0])))
        r["agl"] = r["alt"] + 400.0
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        t.runway()
        tp = _leg(t)["touchdown_point"]
        h = tp.get("threshold_height_ft")
        assert h is not None and abs(h - 94) < 6, (
            "crossed about 94 ft over a runway climbing 2%%, measured %r" % h)
    finally:
        t.close()


def test_a_runway_cached_later_rebuilds_that_sortie_and_no_other():
    """The cache trap AGENTS.md describes, for the runway files.

    The runway arrives after the landing - the watcher looks it up once the
    aircraft is parked - so a sortie built at landing time must rebuild when
    it does. An airport somewhere else must not rebuild it.
    """
    t = Tree()
    try:
        _flight(t, 1800.0)

        def sig():
            flights = logbook_build.scan_flights()
            group = logbook_build.group_sorties(flights)[0]
            events = {}
            for line in open(logbook_build.EVENTS_JSONL, encoding="utf-8"):
                e = json.loads(line)
                events.setdefault(e["flight_id"], []).append(e)
            return logbook_build.sortie_signature(group, "g", events, FID, None, {})

        before = sig()
        t.runway("KFAR", lat=LAT0 + 2.0)          # 120 nm away
        assert sig() == before, "an airport 120 nm away changed this sortie's signature"
        t.runway("KTST")
        assert sig() != before, (
            "the runway this sortie landed on was cached and its signature did "
            "not move, so the cached sortie wins with no touchdown point for ever")
    finally:
        t.close()


def test_a_helicopter_gets_no_touchdown_point():
    t = Tree()
    try:
        _flight(t, 1800.0)
        t.runway()
        meta = os.path.join(t.dir, FID + ".meta.json")
        doc = json.load(open(meta, encoding="utf-8"))
        doc.update(aircraft="Test Helicopter", category="Helicopter", vs0=0.0)
        json.dump(doc, open(meta, "w", encoding="utf-8"))
        assert _leg(t).get("touchdown_point") is None, (
            "a helicopter was measured against the airplane touchdown zone")
    finally:
        t.close()


# --------------------------------------------------------------------------
# the settings
# --------------------------------------------------------------------------

class Settings(object):
    """A settings.json, laid over grading the way a command-line build does.

    Through settings.apply_saved rather than by assigning grading's constants,
    so what is tested is the path a saved setting really takes.
    """

    def __init__(self, root, values):
        import settings
        self.s = settings
        self._keep = (settings.SETTINGS_PATH, dict(settings._defaults),
                      settings._values, dict(settings._registry))
        self._grading = grading.runway_tunables()
        settings.SETTINGS_PATH = os.path.join(root, "settings.json")
        with open(settings.SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(values, f)
        settings.apply_saved(("grading",))

    def close(self):
        s = self.s
        s.SETTINGS_PATH, defaults, s._values, registry = self._keep
        s._defaults.clear()
        s._defaults.update(defaults)
        s._registry.clear()
        s._registry.update(registry)
        for name, value in self._grading.items():
            setattr(grading, name, value)


def _with(tree, values, force=True):
    st = Settings(tree.root, values)
    try:
        return _leg(tree, force=force)
    finally:
        st.close()


def _descent_part(leg, key):
    """A part of the phase that carries the landing - its own, or the descent."""
    phases = (leg.get("phase_grade") or {}).get("phases") or {}
    for name in ("landing", "descent"):
        for p in (phases.get(name) or {}).get("parts") or []:
            if p.get("key") == key:
                return p
    return None


# Every band as tight as the settings page allows, and both switches on: the
# harshest a saved settings.json can make these two measures.
HARSHEST = {"grade_float": True, "float_normal_s": 3.0, "float_margin_ft": 500.0,
            "float_beyond_ft": 100.0, "grade_touchdown_point": True,
            "tdz_target_ft": 0.0, "tdz_tolerance_ft": 0.0, "tdz_end_ft": 500.0,
            "tdz_beyond_ft": 100.0}


def test_one_grade_for_the_landing_and_the_touchdown_in_words():
    """Through the builder. The landing's grade is its phase's; the touchdown
    has no letter of its own - a second one beside the first read as a
    contradiction - only a word on its type's scale: a jet's 150 fpm is in
    the airline target band, where the light ladder would say "Smooth"."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        leg = _leg(t)
        land = leg["phase_grade"]["phases"]["landing"]
        assert (leg["landing_grade"], leg["landing_score"]) == (land["letter"], land["score"]), (
            "the landing grade is not the landing phase's")
        assert leg["touchdown_word"] == "On target", leg["touchdown_word"]
        assert grading.touchdown_word(leg["landing_rate_fpm"], grading.LIGHT_GA) != "On target", (
            "the fixture cannot tell the type's word from the light ladder's")
        assert "landing_grade_name" not in leg, "the touchdown still carries a letter's name"
    finally:
        t.close()


def test_the_passenger_is_told_why_a_soft_landing_was_lowered():
    """A gentle touchdown 3,800 ft down the runway, with the touchdown spot
    counted: the letter comes down, and the passenger line must describe the
    runway used - not a firm arrival the touchdown never was."""
    import passenger
    t = Tree()
    try:
        _flight(t, 3800.0)
        t.runway()
        leg = _with(t, {"grade_touchdown_point": True})
        on = leg["passenger"]["graded_on"]
        assert on["landing_held_by"] == "touchdown_point", on
        assert "ABCDF".index(leg["landing_grade"]) > "ABCDF".index(on["landing"]), (
            "the landing was not graded below how its touchdown felt: %r" % on)
        rate = passenger._fmt_rate(leg["landing_rate_fpm"])
        assert leg["passenger"]["landing"] in [
            l.format(rate=rate) for l in passenger.LANDING["held_long"]], (
            "the passenger described a landing lowered for landing long as %r"
            % leg["passenger"]["landing"])
    finally:
        t.close()


def _blend(spot_score, float_score, profile=grading.LIGHT_GA):
    td = grading.score_for_touchdown_fpm(50.0, profile)
    keep = (grading.GRADE_FLOAT, grading.GRADE_TOUCHDOWN_POINT)
    grading.GRADE_FLOAT = grading.GRADE_TOUCHDOWN_POINT = True
    try:
        tp = {"score": spot_score, "distance_ft": 4000.0, "runway": "09"}
        fl = {"score": float_score, "seconds": 20.0, "from": "threshold",
              "zone_end_s": 16.0, "zero_s": 21.0, "speed_kt": 100.0}
        score, parts, _ = grading._landing_blend(
            td, 50.0, profile, landing_float=fl, touchdown_point=tp)
    finally:
        grading.GRADE_FLOAT, grading.GRADE_TOUCHDOWN_POINT = keep
    return {"letter": grading.letter_for_score(score), "score": score, "parts": parts}


def test_two_limits_name_the_one_that_set_the_grade():
    """SME review R7. The caps ran in one order and the explanation was
    rebuilt afterwards in another, so with two of them biting the passenger
    was told the wrong cause. The cause is now read from the landing phase's
    own record: whichever limit set the final score, either way round."""
    import logbook_build
    import passenger
    float_last = _blend(spot_score=20.0, float_score=10.0)      # both bite
    spot_only = _blend(spot_score=10.0, float_score=20.0)       # float's cap is above
    held = [p["key"] for p in float_last["parts"] if p.get("held")]
    assert held == ["touchdown_point", "float"], (
        "fixture: both limits should bite here, got %r" % held)
    assert logbook_build.landing_marked_down_by(float_last, "A") == "float"
    assert [p["key"] for p in spot_only["parts"] if p.get("held")] == ["touchdown_point"]
    assert logbook_build.landing_marked_down_by(spot_only, "A") == "touchdown_point"
    for land, pool in ((float_last, "held_float"), (spot_only, "held_long")):
        by = logbook_build.landing_marked_down_by(land, "A")
        got = passenger.assess("f", 1, aircraft="Test", grade="A", rate_fpm=50.0,
                               duration_s=600, held_by=by)
        assert got["landing"] in [l.format(rate=passenger._fmt_rate(50.0))
                                  for l in passenger.LANDING[pool]], (
            "marked down by %s, described from the wrong pool" % by)
    # Not marked down - the landing graded no worse than the touchdown felt.
    assert logbook_build.landing_marked_down_by(
        {"letter": "A", "score": 95.0, "parts": []}, "A") is None
    # A helicopter's landing has no float or touchdown spot to be marked down
    # by - neither is measured for one (test_no_setting_reaches_a_helicopter)
    # - so a soft helicopter touchdown is never described as long or floaty.
    td = grading.score_for_touchdown_fpm(50.0, grading.ROTARY)
    score, parts, _ = grading._landing_blend(td, 50.0, grading.ROTARY)
    heli = {"letter": grading.letter_for_score(score), "score": score, "parts": parts}
    assert logbook_build.landing_marked_down_by(
        heli, grading.touchdown_feel(50.0, grading.ROTARY)) is None


def test_every_band_is_a_setting_whose_default_is_the_published_figure():
    import settings
    by_attr = {s["attr"]: s for s in settings.SPEC if s["module"] == "grading"}
    missing = [n for n in grading.RUNWAY_TUNABLES if n not in by_attr]
    assert not missing, "not settable: %s" % ", ".join(missing)
    for key, value in HARSHEST.items():
        spec = settings.BY_KEY[key]
        assert settings.coerce(spec, value) == value, key
        if spec["type"] == "float":
            assert grading.PUBLISHED[spec["attr"]] >= spec["min"], (
                "%s cannot be set back to its published figure" % key)


def test_a_band_setting_moves_the_score_and_the_letter_together():
    """The reason the landing ladder is not a setting is that the letter and
    the curve could part company. Here a setting has to move both."""
    t = Tree()
    try:
        _flight(t, 2500.0)
        t.runway()
        free = _leg(t)
        held = _with(t, {"grade_touchdown_point": True})
        assert held["touchdown_point"]["score"] < 100, held["touchdown_point"]
        assert held["landing_grade"] != free["landing_grade"], (
            "2,500 ft down a 9,400 ft runway with the touchdown point counted "
            "did not hold the landing letter, so this test proves nothing")
        own = _with(t, {"grade_touchdown_point": True, "tdz_tolerance_ft": 2000.0})
        tp = own["touchdown_point"]
        assert tp["score"] == 100, (
            "a 3,000 ft full-marks setting still scored a 2,500 ft touchdown "
            "%s" % tp["score"])
        assert own["landing_grade"] == free["landing_grade"], (
            "the score moved with the setting and the letter did not: %s, "
            "where the uncounted letter is %s"
            % (own["landing_grade"], free["landing_grade"]))
        part = _descent_part(own, "touchdown_point")
        assert part and part["band"].startswith("Full marks within 3,000 ft"), (
            "the leg prints a band other than the one it was scored on: %r"
            % (part and part["band"]))

        # The panel cites AC 91-79A, so it has to say when the bands are not.
        def why(values):
            st = Settings(t.root, values)
            try:
                return next(m for ph in grading.describe_profile(grading.FIXED_WING)["phases"]
                            for m in ph["metrics"]
                            if m["key"] == "touchdown_point")["why"]
            finally:
                st.close()
        assert "your own settings" in why({"tdz_tolerance_ft": 2000.0}), (
            "a band from settings is explained as if it were AC 91-79A's")
        assert "your own settings" not in why({"grade_touchdown_point": True}), (
            "the published band is described as a setting")
    finally:
        t.close()


def test_a_band_setting_rebuilds_the_logbook_without_force():
    """The bands change without grading.py changing, so grading_revision
    cannot see them; without their own term in the signature the cached
    sortie, graded on the old band, wins for ever."""
    t = Tree()
    try:
        _flight(t, 2500.0)
        t.runway()
        first = _with(t, {"grade_touchdown_point": True}, force=False)
        second = _with(t, {"grade_touchdown_point": True, "tdz_tolerance_ft": 2000.0},
                       force=False)
        assert first["touchdown_point"]["score"] < 100
        assert second["touchdown_point"]["score"] == 100, (
            "a changed band left the cached sortie in place")
    finally:
        t.close()


def test_no_setting_reaches_a_helicopter():
    t = Tree()
    try:
        _flight(t, 3900.0)
        t.runway()
        # The control: the same settings on an airplane do bite, so a
        # helicopter coming through untouched is the gate and not a no-op.
        plane = _with(t, HARSHEST)
        assert plane["touchdown_point"]["score"] == 0 and plane["landing_grade"] == "F", (
            "the harshest settings left an airplane at %s; the helicopter half "
            "of this test proves nothing" % plane["landing_grade"])

        meta = os.path.join(t.dir, FID + ".meta.json")
        doc = json.load(open(meta, encoding="utf-8"))
        doc.update(aircraft="Test Helicopter", category="Helicopter", vs0=0.0)
        json.dump(doc, open(meta, "w", encoding="utf-8"))
        base = _leg(t)
        heli = _with(t, HARSHEST)
        assert heli["touchdown_point"] is None and heli["landing_float"] is None, (
            "a helicopter was measured against an airplane band")
        assert heli["landing_grade"] == base["landing_grade"], (
            "settings for airplanes moved a helicopter landing from %s to %s"
            % (base["landing_grade"], heli["landing_grade"]))
        assert (heli.get("phase_grade") or {}).get("letter") == \
            (base.get("phase_grade") or {}).get("letter")
        for key in ("float", "touchdown_point"):
            assert _descent_part(heli, key) is None, (
                "a helicopter descent carries a %s line" % key)

        st = Settings(t.root, HARSHEST)
        try:
            panel = grading.describe_profile(grading.ROTARY)["phases"]
        finally:
            st.close()
        keys = {m["key"] for ph in panel for m in ph["metrics"]}
        assert not keys & {"float", "touchdown_point"}, (
            "the helicopter tab explains %s" % sorted(keys & {"float", "touchdown_point"}))
    finally:
        t.close()


def test_saving_a_band_in_the_watcher_rebuilds_the_logbook():
    """The watcher rebuilds on a settings save only for the keys it watches.
    A band missing from that list would save, apply, and leave every built
    grade on the old band until something else happened to rebuild."""
    import settings
    root = tempfile.mkdtemp()
    keep_s = (settings.SETTINGS_PATH, dict(settings._defaults),
              settings._values, dict(settings._registry))
    keep_g = grading.runway_tunables()
    keep_r = {s["attr"]: getattr(runways, s["attr"]) for s in settings.SPEC
              if s["module"] == "runways"}
    keep_w = (watcher.BASE, watcher.schedule_logbook_rebuild)
    asked = []
    try:
        watcher.BASE = root
        watcher.schedule_logbook_rebuild = lambda *a, **k: asked.append(k)
        settings.SETTINGS_PATH = os.path.join(root, "settings.json")
        settings._defaults.clear()
        settings._values = {}
        watcher.load_settings_at_startup()        # registers what the watcher does
        modules = {"grading": grading, "runways": runways}
        seen = set()
        for spec in settings.SPEC:
            if spec["module"] not in modules:
                continue
            seen.add(spec["module"])
            new = (not settings._defaults[spec["key"]] if spec["type"] == "bool"
                   else spec["min"])
            del asked[:]
            ok, out = watcher.apply_settings({spec["key"]: new})
            assert ok, out
            assert getattr(modules[spec["module"]], spec["attr"]) == new, (
                "saving %s did not reach %s.%s" % (spec["key"], spec["module"], spec["attr"]))
            assert out["rebuild_scheduled"] and asked, (
                "saving %s changed grades and scheduled no rebuild" % spec["key"])
            del asked[:]
            ok, out = watcher.apply_settings({spec["key"]: new})
            assert not out["rebuild_scheduled"] and not asked, (
                "saving %s unchanged rebuilt the logbook anyway" % spec["key"])
        assert seen == set(modules), "no settings found for %r" % (set(modules) - seen)
    finally:
        settings.SETTINGS_PATH, defaults, settings._values, registry = keep_s
        settings._defaults.clear()
        settings._defaults.update(defaults)
        settings._registry.clear()
        settings._registry.update(registry)
        for name, value in keep_g.items():
            setattr(grading, name, value)
        for name, value in keep_r.items():
            setattr(runways, name, value)
        watcher.BASE, watcher.schedule_logbook_rebuild = keep_w
        shutil.rmtree(root, ignore_errors=True)


def test_a_landing_is_covered_by_a_runway_not_by_an_airport_nearby():
    """A heliport cached 2.5 nm from a landing made the landing count as
    covered, so the airport it was really at was never looked up."""
    d = tempfile.mkdtemp()
    try:
        heli = runways.from_facility("XHEL", "K2", (LAT0 + 0.04, LON0, 0.0, 0), [])
        with open(runways.cache_path(d, "XHEL"), "w", encoding="utf-8") as f:
            json.dump(heli, f)
        index = runways.load_index(d)
        assert runways.airports_near(index, LAT0, LON0, 3.0), "the fixture has no airport nearby"
        assert not runways.runway_at(index, LAT0, LON0), (
            "a landing counted as covered by a heliport 2.4 nm away")
        doc = runways.from_facility("KTST", "K2", (LAT0, LON0, 0.0, 1),
                                    [(LAT0, LON0, 0.0, 90.0, 2865.0, 45.0, 9, 0, 27, 0, 0.0, 0.0)])
        with open(runways.cache_path(d, "KTST"), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        index = runways.load_index(d)
        assert runways.runway_at(index, LAT0, LON0), "a touchdown on a cached runway"
        # Either direction, and beside the runway is not on it.
        assert runways.runway_at(index, *_east_of(LAT0, LON0, 3000.0))
        assert not runways.runway_at(index, LAT0 + 0.01, LON0), "600 m north of the runway"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_the_lookup_fetches_the_airport_a_heliport_used_to_hide():
    """Through lookup_runways, with the sim replaced: a cached heliport near a
    landing no longer stops the airport it landed at being fetched."""
    d = tempfile.mkdtemp()
    keep = (watcher.RUNWAYS_DIR, watcher.GameSimConnect)
    asked = []

    class FakeSim(object):
        def open(self):
            return True

        def close(self):
            pass

        def facility_airports(self):
            return [("XHEL", "K2", LAT0 + 0.04, LON0, 0.0),
                    ("KTST", "K2", LAT0, LON0, 0.0)]

        def facility_runways(self, ident, region):
            asked.append(ident)
            rws = [] if ident == "XHEL" else [
                (LAT0, LON0, 0.0, 90.0, 2865.0, 45.0, 9, 0, 27, 0, 0.0, 0.0)]
            return runways.from_facility(ident, region, (LAT0, LON0, 0.0, len(rws)), rws)

    try:
        watcher.RUNWAYS_DIR = d
        watcher.GameSimConnect = FakeSim
        heli = runways.from_facility("XHEL", "K2", (LAT0 + 0.04, LON0, 0.0, 0), [])
        with open(runways.cache_path(d, "XHEL"), "w", encoding="utf-8") as f:
            json.dump(heli, f)
        saved = watcher.lookup_runways([(LAT0, LON0)], keep_going=lambda: True)["saved"]
        assert asked == ["KTST"] and saved == 1, (
            "asked the sim for %r and saved %d: the landing's own airport was "
            "not fetched, or a cached one was fetched again" % (asked, saved))
        del asked[:]
        again = watcher.lookup_runways([(LAT0, LON0)], keep_going=lambda: True)
        assert again == {"status": "done", "saved": 0} and asked == [], (
            "a landing on a cached runway was looked up again")
    finally:
        watcher.RUNWAYS_DIR, watcher.GameSimConnect = keep
        shutil.rmtree(d, ignore_errors=True)


class Lookup(object):
    """start_runway_lookup for real, on a stand-in sim with two airports near
    the landing, and the aircraft's state in RUNTIME - which is what the
    lookup's parked check reads - set by the test."""

    # The landing is 1.2 and 1.8 nm from the two airports and on neither's
    # runway, so both are needed: a landing on a cached runway is covered,
    # and its other neighbours are rightly never asked about.
    TOUCHDOWN = (LAT0 - 0.02, LON0)

    def __init__(self, open_ok=True, airports=("KAAA", "KBBB"), far=False):
        self.dir = tempfile.mkdtemp()
        self.far = 1.0 if far else 0.0
        self.asked = []
        self.lists = 0
        self.on_request = None
        self.on_list = None
        self.named = []
        self.name_for = lambda ident: "Name of " + ident
        self.pads_for = lambda ident: []
        self.grounded = []
        self.ground_for = lambda ident: runways.ground_doc(ident, [], [])
        # A list held from an earlier test's "connection" would answer for
        # this one.
        watcher.sim_session_changed()
        self.airports = airports
        self.open_ok = open_ok
        self.rebuilds = []
        events = os.path.join(self.dir, "events.jsonl")
        open(events, "w").close()
        self._keep = (watcher.RUNWAYS_DIR, watcher.GameSimConnect,
                      watcher.EVENTS_JSONL, watcher.schedule_logbook_rebuild,
                      dict(watcher._runway_state), list(watcher._runway_queue))
        with watcher.RUNTIME_LOCK:
            self._keep_rt = (watcher.RUNTIME["connected"],
                             watcher.RUNTIME.get("current_doc"))
        look = self

        class FakeSim(object):
            def open(self):
                return look.open_ok

            def close(self):
                pass

            def facility_airports(self):
                look.lists += 1
                if look.on_list:
                    look.on_list()
                return [(ident, "K2", LAT0 + look.far + 0.01 * i, LON0, 0.0)
                        for i, ident in enumerate(look.airports)]

            def facility_details(self, ident, region):
                look.named.append(ident)
                name = look.name_for(ident)
                return None if name is None else {"name": name, "helipads": look.pads_for(ident)}

            def facility_ground(self, ident, region):
                look.grounded.append(ident)
                return look.ground_for(ident)

            def facility_runways(self, ident, region):
                look.asked.append(ident)
                if look.on_request:
                    look.on_request(ident)
                i = look.airports.index(ident)
                rws = [(LAT0 + 0.01 * i, LON0, 0.0, 90.0, 2865.0, 45.0, 9, 0, 27, 0, 0.0, 0.0)]
                return runways.from_facility(ident, region,
                                             (LAT0 + 0.01 * i, LON0, 0.0, 1), rws)

        watcher.RUNWAYS_DIR = self.dir
        watcher.GameSimConnect = FakeSim
        watcher.EVENTS_JSONL = events
        watcher.schedule_logbook_rebuild = lambda **k: self.rebuilds.append(k)
        watcher._runway_state.update(thread=None, backfilled=False, retry_after=0.0)
        watcher._runway_queue[:] = [self.TOUCHDOWN]
        self.parked(True)

    def parked(self, yes):
        with watcher.RUNTIME_LOCK:
            watcher.RUNTIME["connected"] = True
            watcher.RUNTIME["current_doc"] = (
                {"on_ground": True, "gs": 0.0} if yes else {"on_ground": False, "gs": 100.0})

    def run(self):
        started = watcher.start_runway_lookup()
        th = watcher._runway_state.get("thread")
        if started and th is not None:
            th.join(10)
        return started

    def close(self):
        (watcher.RUNWAYS_DIR, watcher.GameSimConnect, watcher.EVENTS_JSONL,
         watcher.schedule_logbook_rebuild, state, queue) = self._keep
        watcher._runway_state.clear()
        watcher._runway_state.update(state)
        watcher._runway_queue[:] = queue
        with watcher.RUNTIME_LOCK:
            watcher.RUNTIME["connected"], watcher.RUNTIME["current_doc"] = self._keep_rt
        watcher.sim_session_changed()
        shutil.rmtree(self.dir, ignore_errors=True)


def test_a_lookup_stops_when_the_aircraft_moves_and_finishes_later():
    """SME review R5. The parked check was made once, before the worker
    started; a touch-and-go after the first airport still had the second
    requested and written. Now it stops after the request in hand, keeps the
    landing, and the next parked moment finishes the job."""
    look = Lookup()
    try:
        look.on_request = lambda ident: look.parked(False)   # moving from here
        look.run()
        assert look.asked == ["KAAA"], (
            "the sim was asked about %r after the aircraft started moving" % look.asked)
        assert watcher._runway_queue == [Lookup.TOUCHDOWN], (
            "the landing was dropped by a lookup that never finished")
        assert not watcher._runway_state["backfilled"], (
            "an unfinished backfill was marked done")
        look.on_request = None
        look.parked(True)
        look.run()
        assert look.asked == ["KAAA", "KBBB"], look.asked
        assert watcher._runway_queue == [] and watcher._runway_state["backfilled"]
        assert look.rebuilds, "runways were cached and nothing was rebuilt"
    finally:
        look.close()


def test_the_airport_list_is_not_asked_for_while_moving():
    """The airport list is the big request - every airport in the world - so
    the check comes before it too."""
    look = Lookup()
    try:
        look.parked(False)
        look.run()
        assert look.lists == 0 and look.asked == [], "the sim was asked while moving"
        assert watcher._runway_queue == [Lookup.TOUCHDOWN]
    finally:
        look.close()


def test_a_failed_lookup_keeps_its_work_and_waits_before_asking_again():
    look = Lookup(open_ok=False)
    try:
        look.run()
        assert watcher._runway_queue == [Lookup.TOUCHDOWN], "a failed lookup lost its landing"
        assert not watcher._runway_state["backfilled"]
        assert not watcher.runway_lookup_wanted(), (
            "a sim that could not be asked is asked again at once")
        assert not look.run(), "a second lookup started inside the retry window"
        watcher._runway_state["retry_after"] = 0.0
        look.open_ok = True
        look.run()
        assert watcher._runway_queue == [] and look.asked == ["KAAA", "KBBB"]
    finally:
        look.close()


def test_an_off_airport_landing_is_searched_once_and_not_again():
    """Done with nothing saved is a finished search, not unfinished work."""
    look = Lookup(airports=("KFAR",), far=True)
    try:
        look.run()
        assert look.lists == 1 and watcher._runway_queue == [], (
            "an off-airport landing was kept to be searched again")
        assert watcher._runway_state["backfilled"]
    finally:
        look.close()


def test_the_airport_list_is_asked_for_once_per_sim_connection():
    """Every lookup with anything to find asked the sim for all 84,000
    airports. Now the list is held for the connection it came from, and a
    new connection asks again - scenery may have changed with the sim. The
    log says which, and what the list cost."""
    look = Lookup()
    lines, keep_log = [], watcher.log
    watcher.log = lambda msg, *a, **k: lines.append(str(msg))
    try:
        look.run()
        assert look.lists == 1 and look.asked == ["KAAA", "KBBB"], (look.lists, look.asked)
        assert any("airport list fetched in" in l for l in lines), lines
        # Another landing, off airport, in the same connection.
        watcher._runway_queue[:] = [(LAT0 + 5.0, LON0)]
        look.run()
        assert look.lists == 1, "the list was asked for again in the same connection"
        assert any("airport list held from this connection" in l for l in lines), lines
        # The sim connection ends and begins again.
        watcher.sim_session_changed()
        watcher.sim_session_changed()
        watcher._runway_queue[:] = [(LAT0 + 6.0, LON0)]
        look.run()
        assert look.lists == 2, "a new connection used the old connection's list"
    finally:
        watcher.log = keep_log
        look.close()


def test_a_list_fetched_as_the_connection_ends_is_not_kept():
    """The lookup runs on its own thread; the sim can go while it waits for
    the list. That list belongs to a connection that is over."""
    look = Lookup()
    try:
        look.on_list = watcher.sim_session_changed
        look.run()
        assert look.lists == 1
        assert watcher.held_airport_list()[1] is None, (
            "a list fetched under the old connection was kept for the new one")
    finally:
        look.close()


def test_the_held_list_reads_back_what_the_sim_sent():
    rows = [("KSEA", "K1", 47.45, -122.31, 130.0), ("CYVR", "CY", 49.19, -123.18, 4.0),
            ("LONGIDENT", "ABC", -33.9, 151.2, 6.0), ("", "", 0.0, 0.0, 0.0)]
    held = watcher.AirportList(rows)
    assert len(held) == 4
    assert list(held) == [r[:4] for r in rows], list(held)
    assert (held.ident(1), held.region(1)) == ("CYVR", "CY")


def test_work_past_the_per_pass_limit_is_kept():
    keep = watcher.RUNWAY_BACKFILL_MAX
    look = Lookup()
    try:
        watcher.RUNWAY_BACKFILL_MAX = 1
        look.run()
        assert look.asked == ["KAAA"] and watcher._runway_queue == [Lookup.TOUCHDOWN], (
            "work past the per-pass limit was dropped")
        look.run()
        assert look.asked == ["KAAA", "KBBB"] and watcher._runway_queue == []
    finally:
        watcher.RUNWAY_BACKFILL_MAX = keep
        look.close()


def _asked_for(touchdown, airport):
    """Which airports lookup_runways asks the sim about, for one landing and
    one airport at the given positions - the real prefilter, a stand-in sim."""
    d = tempfile.mkdtemp()
    keep = (watcher.RUNWAYS_DIR, watcher.GameSimConnect)
    asked = []

    class FakeSim(object):
        def open(self):
            return True

        def close(self):
            pass

        def facility_airports(self):
            return [("XTST", "K2", airport[0], airport[1], 0.0)]

        def facility_details(self, ident, region):
            return {"name": "", "helipads": []}

        def facility_ground(self, ident, region):
            return runways.ground_doc(ident, [], [])

        def facility_runways(self, ident, region):
            asked.append(ident)
            return runways.from_facility(ident, region, (airport[0], airport[1], 0.0, 0), [])

    try:
        watcher.RUNWAYS_DIR = d
        watcher.GameSimConnect = FakeSim
        # Each case is a world of its own: a new connection, so the list
        # held from the last case is not this one's.
        watcher.sim_session_changed()
        watcher.lookup_runways([touchdown], keep_going=lambda: True)
    finally:
        watcher.sim_session_changed()
        watcher.RUNWAYS_DIR, watcher.GameSimConnect = keep
        shutil.rmtree(d, ignore_errors=True)
    return asked


def test_the_runway_search_reaches_3_nm_at_every_latitude():
    """SME review R6. The prefilter looked one 0.1 degree cell either way,
    which is under 3 nm of longitude beyond about 60 degrees: an airport
    2.19 nm from a landing at 80 N was never asked about."""
    cases = [
        ("80 N, three columns apart", (80.0, 10.09), (80.0, 10.30)),
        ("80 S", (-80.0, 10.09), (-80.0, 10.30)),
        ("65 N, two columns apart", (65.0, -17.999), (65.0, -18.11)),
        ("across 0 degrees, both ways", (-0.001, -0.001), (0.03, 0.03)),
        ("across the antimeridian", (0.0, 179.99), (0.0, -179.99)),
        ("near the pole, far apart in longitude", (89.97, 10.0), (89.97, 100.0)),
    ]
    for name, td, ap in cases:
        nm = runways.nm_apart(td[0], td[1], ap[0], ap[1])
        assert nm <= watcher.RUNWAY_LOOKUP_RADIUS_NM, "fixture %s: %.2f nm" % (name, nm)
        assert _asked_for(td, ap) == ["XTST"], (
            "%s: an airport %.2f nm from the landing was never asked about" % (name, nm))
    # and the exact distance still decides: just outside is not asked about
    far = ((80.0, 10.09), (80.0, 10.41))
    nm = runways.nm_apart(far[0][0], far[0][1], far[1][0], far[1][1])
    assert nm > watcher.RUNWAY_LOOKUP_RADIUS_NM, "fixture: %.2f nm" % nm
    assert _asked_for(*far) == [], "an airport %.2f nm away was asked about" % nm
    # and in the same cell, where only the exact distance can say no
    near_cell = ((0.0, 0.0), (0.06, 0.0))
    assert watcher._runway_cell(*near_cell[0]) == watcher._runway_cell(*near_cell[1])
    nm = runways.nm_apart(0.0, 0.0, 0.06, 0.0)
    assert nm > watcher.RUNWAY_LOOKUP_RADIUS_NM, "fixture: %.2f nm" % nm
    assert _asked_for(*near_cell) == [], (
        "an airport %.2f nm away, in the landing's own cell, was asked about" % nm)


def test_the_watcher_asks_the_sim_about_airplane_runways_only():
    # Any aircraft whose category is known: an airplane's ends are named
    # from runways, a helicopter's from helipads and nearby airports.
    assert watcher.asks_about_airports("Airplane", 90.0, "Test Jet")
    assert watcher.asks_about_airports("Helicopter", 0.0, "Test Helicopter")
    assert watcher.asks_about_airports("Airplane", 10.0, "Test Gyroplane")
    assert not watcher.asks_about_airports(None)

    keep_q = list(watcher._runway_queue)
    keep = (watcher.RUNWAY_LOOKUP, watcher.SESSIONS, watcher.EVENTS_JSONL)
    root = tempfile.mkdtemp()
    try:
        watcher.RUNWAY_LOOKUP = True
        del watcher._runway_queue[:]
        watcher.queue_runway_lookup(LAT0, LON0, None, None, "Who Knows")
        assert not watcher._runway_queue, "an aircraft of no known kind queued a lookup"
        watcher.queue_runway_lookup(LAT0, LON0, "Helicopter", 0.0, "Test Helicopter")
        assert watcher._runway_queue == [(LAT0, LON0)], (
            "a helicopter landing did not ask about the airports near it")

        # The backfill reads landings on record, and skips the helicopters.
        watcher.SESSIONS = root
        watcher.EVENTS_JSONL = os.path.join(root, "events.jsonl")
        flights = (("flt-19990101T000000Z", "Airplane", 90.0, LAT0),
                   ("flt-19990102T000000Z", "Helicopter", 0.0, LAT0 + 1.0),
                   ("flt-19990103T000000Z", None, None, LAT0 + 2.0))
        with open(watcher.EVENTS_JSONL, "w", encoding="utf-8") as ev:
            for fid, cat, vs0, lat in flights:
                with open(os.path.join(root, fid + ".meta.json"), "w", encoding="utf-8") as f:
                    json.dump({"flight_id": fid, "category": cat, "vs0": vs0}, f)
                ev.write(json.dumps({"kind": "takeoff", "flight_id": fid,
                                     "lat": lat + 0.5, "lon": LON0}) + "\n")
                ev.write(json.dumps({"kind": "landing", "flight_id": fid,
                                     "lat": lat, "lon": LON0}) + "\n")
            ev.write(json.dumps({"kind": "landing", "lat": LAT0 + 3.0,
                                 "lon": LON0}) + "\n")
        # Takeoffs too, so flights recorded before departures were named get
        # their departure airports; helicopters too; never an aircraft whose
        # kind is not known.
        assert watcher._runway_points_on_record() == [
            (LAT0 + 0.5, LON0), (LAT0, LON0), (LAT0 + 1.5, LON0), (LAT0 + 1.0, LON0)], (
            "the backfill would ask the sim about %r"
            % watcher._runway_points_on_record())
    finally:
        watcher.RUNWAY_LOOKUP, watcher.SESSIONS, watcher.EVENTS_JSONL = keep
        watcher._runway_queue[:] = keep_q
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# the airport and runway at each end of a leg
# --------------------------------------------------------------------------

def _rows(tree):
    path = os.path.join(tree.dir, FID + ".jsonl")
    return path, [json.loads(l) for l in open(path, encoding="utf-8")]


def _departure_runway(tree, ident="KDEP"):
    """An east-west runway under the fixture's takeoff roll - the first 20
    rows, on the ground, rolling east."""
    _path, rows = _rows(tree)
    tree.runway(ident, lat=rows[10]["lat"], lon=rows[10]["lon"])
    return rows[10]["lat"], rows[10]["lon"]


def _move_roll(tree, dlat):
    """Shift the recorded takeoff roll north, off any runway under it."""
    path, rows = _rows(tree)
    for r in rows[:21]:
        r["lat"] += dlat
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return rows


def _logbook(tree):
    with open(logbook_build.LOGBOOK_JSON, encoding="utf-8") as f:
        return json.load(f)


def test_a_leg_records_the_airport_and_runway_at_each_end():
    """Recorded lat/lon only, before: the route said where in numbers. Now an
    end on a runway the sim describes is that airport and runway, and an end
    off one keeps its coordinates."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        t.runway()                                   # the arrival, KTST
        leg = _leg(t)
        assert leg.get("arrival") == {"airport": "KTST", "runway": "09"}, leg.get("arrival")
        assert leg.get("departure") is None, (
            "a departure was named with no runway cached under it: %r" % leg.get("departure"))
        r = leg["route"]
        assert r["to"] == "KTST 09" and r["to_kind"] == "airport" and r["to_named"], r
        assert r["from_kind"] == "coords" and not r["from_named"], r
        assert r["from"] == logbook_build.coord_label(
            leg["takeoff"]["lat"], leg["takeoff"]["lon"]), r

        _departure_runway(t)
        leg = _leg(t)
        dep = leg.get("departure") or {}
        assert (dep.get("airport"), dep.get("runway")) == ("KDEP", "09"), (
            "rolled east down KDEP's runway and recorded %r" % dep)
        assert dep.get("source") == "track" and abs(dep["lateral_ft"]) < 10, dep
        assert leg["route"]["from"] == "KDEP 09", leg["route"]

        doc = _logbook(t)
        row = doc["find"][0]
        assert row["f"] == "KDEP 09" and row["t"] == "KTST 09", row
        assert set(row["p"]) == {"KDEP 09", "KTST 09"}, row
    finally:
        t.close()


def test_a_takeoff_off_the_runway_is_not_given_one():
    """An airport nearby is not the runway rolled down: a takeoff from a
    field beside it keeps its coordinates."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        _departure_runway(t)
        _move_roll(t, 0.01)                          # about 3,600 ft north
        leg = _leg(t)
        assert leg.get("departure") is None, leg.get("departure")
        assert leg["route"]["from_kind"] == "coords", leg["route"]
    finally:
        t.close()


def test_a_roll_the_recording_missed_names_no_runway():
    """The last sample on the ground has to be the roll's end, a moment
    before lift-off. With the roll missing from the recording, the last
    ground sample is from long before - here still on the runway, so the
    guard is all that stops it naming one from stale evidence."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        _departure_runway(t)
        path, rows = _rows(t)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows[:5] + rows[20:]:
                f.write(json.dumps(r) + "\n")
        assert _leg(t).get("departure") is None, _leg(t).get("departure")
    finally:
        t.close()


def test_the_takeoff_clip_places_the_roll_before_the_track_does():
    """The 10 Hz takeoff clip is the better record of the roll, as the
    landing clip is of the touchdown. Here the track puts the roll off the
    runway and the clip puts it on: the clip decides."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        _departure_runway(t)
        rows = _move_roll(t, 0.01)
        pts = []
        for r in rows[5:25]:
            p = dict(r)
            if p["on_ground"] or rows.index(r) <= 20:
                p["lat"] -= 0.01
            p["t"] = logbook_build.parse_ts(r["ts"])
            pts.append(p)
        with open(os.path.join(logbook_build.CLIPS_DIR, FID + "-leg1-takeoff.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"id": FID + "-leg1-takeoff", "kind": "takeoff", "points": pts}, f)
        dep = _leg(t).get("departure") or {}
        assert (dep.get("airport"), dep.get("source")) == ("KDEP", "clip"), dep
    finally:
        t.close()


def test_a_departure_runway_cached_later_rebuilds_that_sortie():
    """The cache trap again, for the takeoff end: the departure's runways are
    looked up after the flight, so the sortie must rebuild when they arrive."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        t.runway()

        def sig():
            flights = logbook_build.scan_flights()
            group = logbook_build.group_sorties(flights)[0]
            events = {}
            for line in open(logbook_build.EVENTS_JSONL, encoding="utf-8"):
                e = json.loads(line)
                events.setdefault(e["flight_id"], []).append(e)
            return logbook_build.sortie_signature(group, "g", events, FID, None, {})

        before = sig()
        _departure_runway(t)
        assert sig() != before, (
            "the departure's runway was cached and the signature did not move, "
            "so the cached sortie keeps its coordinates for ever")
    finally:
        t.close()


def test_a_place_name_and_an_airport_at_the_same_end():
    """A places.json name at an airport: the airport and runway by default,
    the place's name with ROUTE_PREFER_PLACE_NAMES - and the other is kept,
    so a search finds the sortie either way."""
    t = Tree()
    keep = (logbook_build.PLACES_JSON, logbook_build.ROUTE_PREFER_PLACE_NAMES)
    try:
        _flight(t, 1800.0)
        lat, lon = _departure_runway(t)
        logbook_build.PLACES_JSON = os.path.join(t.root, "places.json")
        with open(logbook_build.PLACES_JSON, "w", encoding="utf-8") as f:
            json.dump([{"name": "Home field", "lat": lat, "lon": lon, "radius_nm": 3}], f)
        r = _leg(t)["route"]
        assert (r["from"], r["from_kind"], r["from_place"]) == ("KDEP 09", "airport", "Home field"), r
        assert {"KDEP 09", "Home field"} <= set(_logbook(t)["find"][0]["p"])

        g_airport = logbook_build.global_signature(False)
        logbook_build.ROUTE_PREFER_PLACE_NAMES = True
        assert logbook_build.global_signature(False) != g_airport, (
            "the flag does not reach the signature, so the cached routes keep "
            "the old names")
        r = _leg(t, force=False)["route"]
        assert (r["from"], r["from_kind"]) == ("Home field", "place"), r
        assert {"KDEP 09", "Home field"} <= set(_logbook(t)["find"][0]["p"])
    finally:
        logbook_build.PLACES_JSON, logbook_build.ROUTE_PREFER_PLACE_NAMES = keep
        t.close()


def _helicopter(tree):
    meta = os.path.join(tree.dir, FID + ".meta.json")
    doc = json.load(open(meta, encoding="utf-8"))
    doc.update(aircraft="Test Helicopter", category="Helicopter", vs0=0.0)
    json.dump(doc, open(meta, "w", encoding="utf-8"))


def _event_point(tree, kind):
    for line in open(os.path.join(tree.root, "events.jsonl"), encoding="utf-8"):
        e = json.loads(line)
        if e["kind"] == kind:
            return e["lat"], e["lon"]


def _heliport(tree, ident, lat, lon, pads):
    """A heliport - an airport with no runways - and its pads."""
    d = os.path.join(tree.dir, "runways")
    os.makedirs(d, exist_ok=True)
    with open(runways.cache_path(d, ident), "w", encoding="utf-8") as f:
        json.dump(runways.from_facility(ident, "LE", (lat, lon, 0.0, 0), []), f)
    _pads(tree, ident, pads)


def _pads(tree, ident, pads):
    """An airport's pads, and only those - its runway file untouched."""
    d = os.path.join(tree.dir, "runways")
    have = runways.load_helipads(d)
    have[ident] = [{"lat": la, "lon": lo, "heading": 0.0, "length_ft": 66.0, "width_ft": 66.0}
                   for la, lo in pads]
    with open(runways.helipads_path(d), "w", encoding="utf-8") as f:
        json.dump(runways.helipads_doc(have), f)


def test_a_helicopter_is_named_from_the_pad_it_was_on():
    """A helicopter's end is the heliport whose pad it was on. Off every
    pad, and further than HELICOPTER_AIRPORT_RADIUS_NM from any airport with
    runways, it keeps its coordinates."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        _helicopter(t)
        la, lo = _event_point(t, "takeoff")
        # A heliport 100 ft away whose only pad is under the takeoff.
        hp = _east_of(la, lo, 100.0)
        _heliport(t, "LEPAD", hp[0], hp[1], [(la, lo)])
        leg = _leg(t)
        dep = leg.get("departure") or {}
        assert (dep.get("airport"), dep.get("how")) == ("LEPAD", "helipad"), leg.get("departure")
        assert leg["route"]["from"] == "LEPAD" and leg["route"]["from_kind"] == "airport"
        assert leg.get("arrival") is None and leg["route"]["to_kind"] == "coords", leg.get("arrival")

        # The pad moved 200 ft away: no longer on it, and a heliport is not
        # named for being near.
        pad = _east_of(la, lo, 200.0)
        _heliport(t, "LEPAD", hp[0], hp[1], [pad])
        assert _leg(t, force=False).get("departure") is None, (
            "a helicopter 200 ft from a pad was named as on it, or a heliport "
            "was named for being near")
    finally:
        t.close()


def test_a_helicopter_near_an_airport_with_runways_is_named_for_it():
    """The owner's rule, for airports that list no helipads: an airport with
    runways whose reference point is within 0.5 nm."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        _helicopter(t)
        la, lo = _event_point(t, "landing")
        # North of the landing, so its east-west runway is not under it.
        t.runway("KNEAR", lat=la + 0.4 / 60.0, lon=lo)
        arr = _leg(t).get("arrival") or {}
        assert (arr.get("airport"), arr.get("how")) == ("KNEAR", "nearby"), arr
        assert arr.get("runway") is None, "a helicopter was given a runway"
        # Its ground mapped, and the landing nowhere near it: the radius
        # still backs it up - the owner's reading - unless the stricter
        # setting is on.
        _ground(t, "KNEAR", parkings=[(la + 0.4 / 60.0, lo, 60.0)])
        arr = _leg(t, force=False).get("arrival") or {}
        assert (arr.get("airport"), arr.get("how")) == ("KNEAR", "nearby"), (
            "the radius did not back up a mapped airport: %r" % arr)
        keep = runways.RADIUS_WHERE_PAVEMENT_MAPPED
        runways.RADIUS_WHERE_PAVEMENT_MAPPED = False
        try:
            assert _leg(t, force=False).get("arrival") is None, (
                "with the strict setting, a mapped airport was named for being near")
        finally:
            runways.RADIUS_WHERE_PAVEMENT_MAPPED = keep
        os.remove(runways.cache_path(os.path.join(t.dir, "runways"), "KNEAR"))
        t.runway("KFARR", lat=la + 0.6 / 60.0, lon=lo)
        assert _leg(t).get("arrival") is None, "0.6 nm away was named"
    finally:
        t.close()


def test_an_airplane_is_never_named_for_being_near():
    """An airplane's end is the runway it was on, or nothing - not a pad,
    and not an airport nearby."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        la, lo = _event_point(t, "takeoff")
        t.runway("KNEAR", lat=la + 0.1 / 60.0, lon=lo)        # 0.1 nm north
        _heliport(t, "LEPAD", la, lo, [(la, lo)])
        _move_roll(t, 0.003)                                  # off the runway
        leg = _leg(t)
        assert leg.get("departure") is None, leg.get("departure")
    finally:
        t.close()


def _ground(tree, ident, parkings=(), paths=()):
    d = os.path.join(tree.dir, "runways")
    path = runways.ground_path(d, ident)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(runways.ground_doc(ident, list(parkings), list(paths)), f)


def _ground_record(kind, fmt, *values):
    return _facility(9, 1, kind, struct.pack(fmt, *values))


def test_an_airports_ground_is_read_in_the_measured_layout():
    """Parking spots and taxi points are metres east and north of the
    airport's reference; a taxi path joins two points, except that a path of
    type 3 ends at a parking spot - measured, read as a point its median
    length was thousands of feet."""
    m = 1.0 / runways.FT_PER_M                       # a foot, in metres
    parts = [_facility(1, 0, 0, struct.pack("<dd", LAT0, LON0)),
             _ground_record(15, "<iffff", 3, 30.0, 90.0, 1000.0 * m, 0.0),      # 1,000 ft east
             _ground_record(14, "<iff", 4, 0.0, 500.0 * m),                    # 500 ft north
             _ground_record(14, "<iff", 4, 0.0, 900.0 * m),                    # 900 ft north
             _ground_record(16, "<ifii", 4, 25.0, 0, 1),                       # point to point
             _ground_record(16, "<ifii", 3, 25.0, 0, 0),                       # point to PARKING 0
             _ground_record(16, "<ifii", 4, 25.0, 0, 7)]                       # no such point
    conn = _FakeConn(runway_parts=parts)
    doc = conn.g.facility_ground("XAPT", "XX", timeout=1.0)
    assert conn.added == list(watcher.GROUND_FIELDS)
    (p_lat, p_lon, rad), = doc["parkings"]
    assert abs(rad - 30.0 * runways.FT_PER_M) < 0.2, "a parking radius not read as metres"
    assert abs(runways.nm_apart(LAT0, LON0, p_lat, p_lon) * runways.FT_PER_NM - 1000.0) < 2
    assert abs(p_lat - LAT0) < 1e-6 and p_lon > LON0, "BIAS_X is not east"
    assert len(doc["paths"]) == 2, "a path to a missing point was kept: %r" % doc["paths"]
    to_point, to_parking = doc["paths"]
    assert abs((to_point[2] - LAT0) * 60.0 * runways.FT_PER_NM - 900.0) < 2, "BIAS_Z is not north"
    assert (to_parking[2], to_parking[3]) == (p_lat, p_lon), (
        "a type 3 path did not end at its parking spot")
    assert abs(to_point[4] - 25.0 * runways.FT_PER_M) < 0.2


def test_off_the_pavement_is_measured_from_its_edges():
    g = runways.ground_doc("X", [(LAT0, LON0, 50.0)],
                           [(LAT0, LON0 + 0.01, LAT0 + 0.01, LON0 + 0.01, 80.0)])
    assert runways.off_pavement_ft(g, LAT0, LON0) == 0.0
    east = _east_of(LAT0, LON0, 150.0)
    assert abs(runways.off_pavement_ft(g, *east) - 100.0) < 1, "a parking circle's edge"
    path_side = _east_of(LAT0 + 0.005, LON0 + 0.01, 140.0)
    assert abs(runways.off_pavement_ft(g, *path_side) - 100.0) < 1, "a path's edge"
    assert runways.off_pavement_ft(runways.ground_doc("X", [], []), LAT0, LON0) is None
    assert runways.off_pavement_ft(None, LAT0, LON0) is None
    airport = {"runways": [{"lat": LAT0, "lon": LON0, "heading": 90.0,
                            "length_ft": 1000.0, "width_ft": 100.0}]}
    assert runways.off_runways_ft(airport, *_east_of(LAT0, LON0, 400.0)) == 0.0
    assert abs(runways.off_runways_ft(airport, LAT0 + 150.0 / (60 * runways.FT_PER_NM), LON0) - 100.0) < 1


def test_a_helicopter_on_an_airports_pavement_is_named_for_it():
    """Off every pad: inside a parking circle, on a taxi path or a runway,
    or within the pavement margin of one, a helicopter's end is at that
    airport - however far its reference point."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        _helicopter(t)
        la, lo = _event_point(t, "landing")
        t.runway("KBIG", lat=la + 2.0 / 60.0, lon=lo)            # 2 nm north
        _ground(t, "KBIG", parkings=[(la, lo, 60.0)])
        arr = _leg(t).get("arrival") or {}
        assert (arr.get("airport"), arr.get("how")) == ("KBIG", "pavement"), arr

        # 150 ft beyond the circle: inside the 200 ft margin, for aprons.
        _ground(t, "KBIG", parkings=[(la + 210.0 / (60 * runways.FT_PER_NM), lo, 60.0)])
        arr = _leg(t, force=False).get("arrival") or {}
        assert arr.get("how") == "pavement" and abs(arr["distance_ft"] - 150) < 2, arr
        # 260 ft beyond: not.
        _ground(t, "KBIG", parkings=[(la + 320.0 / (60 * runways.FT_PER_NM), lo, 60.0)])
        assert _leg(t, force=False).get("arrival") is None

        # On its runway, with nothing else mapped near: pavement.
        _ground(t, "KBIG", parkings=[(la + 2.0 / 60.0, lo, 60.0)])
        t.runway("KBIG", lat=la, lon=lo)
        arr = _leg(t).get("arrival") or {}
        assert (arr.get("airport"), arr.get("how")) == ("KBIG", "pavement"), arr
    finally:
        t.close()


def test_the_lookup_fetches_ground_only_for_a_helicopter_on_no_pad():
    """The ground of a large airport takes half a minute, so it is asked
    for only where it can name something: an airport with runways near an
    end that no runway contains and no pad is under - and once."""
    look = Lookup()
    try:
        look.run()
        assert sorted(look.grounded) == ["KAAA", "KBBB"], look.grounded
        assert runways.load_ground(look.dir, "KAAA") is not None
        look.grounded[:] = []
        watcher._runway_queue[:] = [look.TOUCHDOWN]
        look.run()
        assert look.grounded == [], "ground asked for twice: %r" % look.grounded

        # The same end on a pad: no ground wanted.
        for ident in ("KAAA", "KBBB"):
            os.remove(runways.ground_path(look.dir, ident))
        pads = runways.load_helipads(look.dir)
        pads["KAAA"] = [{"lat": look.TOUCHDOWN[0], "lon": look.TOUCHDOWN[1],
                         "heading": 0.0, "length_ft": 66.0, "width_ft": 66.0}]
        with open(runways.helipads_path(look.dir), "w", encoding="utf-8") as f:
            json.dump(runways.helipads_doc(pads), f)
        watcher._runway_queue[:] = [look.TOUCHDOWN]
        look.run()
        assert look.grounded == [], "ground asked for an end on a pad"
    finally:
        look.close()


def test_ground_that_fails_costs_nothing_and_is_asked_again():
    look = Lookup()
    try:
        look.ground_for = lambda ident: None
        look.run()
        assert watcher._runway_queue == [] and watcher._runway_state["backfilled"]
        assert runways.load_ground(look.dir, "KAAA") is None
        def boom(ident):
            raise RuntimeError("ground broke")
        look.ground_for = boom
        watcher._runway_queue[:] = [look.TOUCHDOWN]
        look.run()
        assert set(runways.load_index(look.dir)) == {"KAAA", "KBBB"}
        look.ground_for = lambda ident: runways.ground_doc(ident, [], [])
        look.grounded[:] = []
        watcher._runway_queue[:] = [look.TOUCHDOWN]
        look.run()
        assert sorted(look.grounded) == ["KAAA", "KBBB"], look.grounded
    finally:
        look.close()


def test_no_ground_is_asked_for_once_the_aircraft_moves():
    look = Lookup()
    try:
        def then_move(ident):
            look.parked(False)
            return runways.ground_doc(ident, [], [])
        look.ground_for = then_move
        look.run()
        assert len(look.grounded) == 1, "ground asked for after moving: %r" % look.grounded
    finally:
        look.close()


def test_a_helipad_found_later_rebuilds_that_sortie():
    t = Tree()
    try:
        _flight(t, 1800.0)
        _helicopter(t)
        la, lo = _event_point(t, "takeoff")
        hp = _east_of(la, lo, 100.0)
        _heliport(t, "LEPAD", hp[0], hp[1], [])
        _heliport(t, "LEFAR", LAT0 + 3.0, LON0, [])

        def sig():
            flights = logbook_build.scan_flights()
            group = logbook_build.group_sorties(flights)[0]
            events = {}
            for line in open(logbook_build.EVENTS_JSONL, encoding="utf-8"):
                e = json.loads(line)
                events.setdefault(e["flight_id"], []).append(e)
            return logbook_build.sortie_signature(group, "g", events, FID, None, {})
        before = sig()
        _pads(t, "LEFAR", [(LAT0 + 3.0, LON0)])
        assert sig() == before, "a pad 180 nm away rebuilt this sortie"
        before_ground = sig()
        _ground(t, "LEFAR", parkings=[(LAT0 + 3.0, LON0, 50.0)])
        assert sig() == before_ground, "ground 180 nm away rebuilt this sortie"
        _ground(t, "LEPAD", parkings=[(la, lo, 50.0)])
        assert sig() != before_ground, "ground found near this sortie did not rebuild it"
        _pads(t, "LEPAD", [(la, lo)])
        assert sig() != before, (
            "the pad this sortie took off from was found and its signature did "
            "not move, so the cached sortie keeps its coordinates for ever")
        g = logbook_build.global_signature(False)
        for attr, value in (("HELICOPTER_AIRPORT_RADIUS_NM", 1.0), ("PAVEMENT_MARGIN_FT", 50.0),
                            ("RADIUS_WHERE_PAVEMENT_MAPPED", False)):
            keep = getattr(runways, attr)
            setattr(runways, attr, value)
            try:
                assert logbook_build.global_signature(False) != g, (
                    "%s is not in the signature" % attr)
            finally:
                setattr(runways, attr, keep)
    finally:
        t.close()


def test_search_covers_every_stop_of_a_sortie():
    """A sortie of three legs stops twice on the way. The search row carries
    every named end - the middle ones too - once each, and no coordinates."""
    def leg(f, fk, to, tk, fp=None, tp=None):
        return {"route": {"from": f, "to": to, "from_kind": fk, "to_kind": tk,
                          "from_named": fk in ("airport", "place"),
                          "to_named": tk in ("airport", "place"),
                          "from_place": fp, "to_place": tp}}
    s = {"sortie_id": FID, "started_at": "1999-01-01T00:00:00",
         "legs": [leg("KAAA 09", "airport", "KBBB 27", "airport"),
                  leg("KBBB 27", "airport", "47.1000, -122.0000", "coords"),
                  leg("47.1000, -122.0000", "coords", "KCCC 18", "airport", tp="Lakeside")]}
    summary = logbook_build.summarize_sortie(s)
    assert summary["route_from"] == "KAAA 09" and summary["route_to"] == "KCCC 18"
    assert summary["places"] == ["KAAA 09", "KBBB 27", "KCCC 18", "Lakeside"], summary["places"]
    assert logbook_build.find_rows([summary])[0]["p"] == summary["places"]


def _graded(tree):
    """What a restore must give back: the leg as the user sees it."""
    leg = _leg(tree)
    pg = leg.get("phase_grade") or {}
    return {
        "landing_grade": leg.get("landing_grade"),
        "leg_letter": pg.get("letter"),
        "overall": pg.get("overall"),
        "phases": {k: (v or {}).get("score") for k, v in (pg.get("phases") or {}).items()},
        "touchdown_point": leg.get("touchdown_point"),
        "landing_float": leg.get("landing_float"),
        "route": leg.get("route"),
        "passenger": (leg.get("passenger") or {}).get("text"),
    }


def _backup_and_restore(tree, full):
    """Run the real backup.ps1, quiet and verified, then copy what its
    manifest lists - and only that - into a fresh tree. Returns the new root."""
    import subprocess
    dest = tempfile.mkdtemp()
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               os.path.join(BASE, "backup.ps1"), "-From", tree.root, "-To", dest,
               "-RequireQuiet", "-Verify"] + (["-Full"] if full else [])
    out = subprocess.run(command, capture_output=True, text=True)
    assert out.returncode == 0, "backup failed: %s%s" % (out.stdout, out.stderr)
    archive = [os.path.join(dest, n) for n in os.listdir(dest)
               if n.startswith("afterflight-backup-")][0]
    with open(os.path.join(archive, "manifest.json"), encoding="utf-8-sig") as f:
        manifest = json.load(f)
    assert manifest["consistency"] == "quiescent", manifest["consistency"]
    restored = tempfile.mkdtemp()
    for row in manifest["files"]:
        target = os.path.join(restored, row["path"])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(archive, row["path"]), target)
    shutil.rmtree(dest, ignore_errors=True)
    return restored


def test_a_restored_backup_grades_the_landing_the_same():
    """SME review R2. The runway cache is evidence a landing is graded
    against, and places.json is the user's own writing; neither was in any
    backup. A landing held down by its touchdown spot came back from a
    verified, quiet restore as if it had touched down on the aim point - F
    to A - and its place names were gone. Both switches on, the same
    settings either side; the default backup and -Full."""
    keep = (grading.GRADE_FLOAT, grading.GRADE_TOUCHDOWN_POINT,
            logbook_build.PLACES_JSON)
    t = Tree()
    try:
        grading.GRADE_FLOAT = grading.GRADE_TOUCHDOWN_POINT = True
        td = _flight(t, 3800.0)               # long: past the touchdown zone
        t.runway()
        logbook_build.PLACES_JSON = os.path.join(t.root, "places.json")
        with open(logbook_build.PLACES_JSON, "w", encoding="utf-8") as f:
            json.dump([{"name": "Synthetic Field", "lat": td[0], "lon": td[1],
                        "radius_nm": 3}], f)
        before = _graded(t)
        assert before["touchdown_point"], "fixture: no touchdown spot measured"
        assert before["route"]["to_named"], "fixture: the place was not named"
        assert before["landing_grade"] in ("D", "F"), (
            "fixture: the long landing should be held down by its spot, got %s"
            % before["landing_grade"])

        source = t.root
        for full in (False, True):
            restored = _backup_and_restore(t, full)
            try:
                t.root = restored                       # rebuild over the copy
                for name, value in (
                        ("BASE", restored),
                        ("SESSIONS", os.path.join(restored, "sessions")),
                        ("CLIPS_DIR", os.path.join(restored, "sessions", "clips")),
                        ("EVENTS_JSONL", os.path.join(restored, "events.jsonl")),
                        ("LOGBOOK_JSON", os.path.join(restored, "logbook.json")),
                        ("CACHE_JSON", os.path.join(restored, "logbook.cache.json")),
                        ("EXCLUDED_JSON", os.path.join(restored, "excluded.json")),
                        ("DETAIL_DIR", os.path.join(restored, "sessions", "detail")),
                        ("PLACES_JSON", os.path.join(restored, "places.json"))):
                    setattr(logbook_build, name, value)
                t.dir = logbook_build.SESSIONS
                logbook_build._runway_memo["key"] = None
                after = _graded(t)
            finally:
                t.root = source
                for name, value in (
                        ("BASE", source),
                        ("SESSIONS", os.path.join(source, "sessions")),
                        ("CLIPS_DIR", os.path.join(source, "sessions", "clips")),
                        ("EVENTS_JSONL", os.path.join(source, "events.jsonl")),
                        ("LOGBOOK_JSON", os.path.join(source, "logbook.json")),
                        ("CACHE_JSON", os.path.join(source, "logbook.cache.json")),
                        ("EXCLUDED_JSON", os.path.join(source, "excluded.json")),
                        ("DETAIL_DIR", os.path.join(source, "sessions", "detail")),
                        ("PLACES_JSON", os.path.join(source, "places.json"))):
                    setattr(logbook_build, name, value)
                t.dir = logbook_build.SESSIONS
                logbook_build._runway_memo["key"] = None
                shutil.rmtree(restored, ignore_errors=True)
            moved = sorted(k for k in before if before[k] != after[k])
            assert not moved, (
                "%s backup: after restore %s changed - %s"
                % ("-Full" if full else "default", ", ".join(moved),
                   "; ".join("%s %r -> %r" % (k, before[k], after[k]) for k in moved)))
    finally:
        (grading.GRADE_FLOAT, grading.GRADE_TOUCHDOWN_POINT,
         logbook_build.PLACES_JSON) = keep
        t.close()


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print("  FAIL  %s" % name)
            for line in str(e).splitlines():
                print("        %s" % line)
        except Exception as e:
            failed += 1
            print("  ERROR %s: %r" % (name, e))
        else:
            print("  ok    %s" % name)
    print()
    print("  %d runway test(s), %d failed" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

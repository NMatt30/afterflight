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
    assert grading.touchdown_point_ceiling_letter({"score": 0.0}) is None


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


def test_the_built_letter_is_the_types_own_verdict():
    """Through the builder: a jet's touchdown is lettered on the transport
    curve, not the light-aircraft ladder."""
    t = Tree()
    try:
        _flight(t, 1800.0)
        leg = _leg(t)
        want = grading.touchdown_letter(leg["landing_rate_fpm"], grading.FIXED_WING)
        assert leg["landing_grade"] == want, (
            "a jet at %s fpm was lettered %s; its own criteria say %s"
            % (leg["landing_rate_fpm"], leg["landing_grade"], want))
        import passenger
        assert passenger.grade_for_rate(leg["landing_rate_fpm"])[0] != want, (
            "the fixture cannot tell the type's letter from the fixed ladder")
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
        assert on["landing_held_from"] and on["landing_held_from"] != leg["landing_grade"], on
        rate = passenger._fmt_rate(leg["landing_rate_fpm"])
        assert leg["passenger"]["landing"] in [
            l.format(rate=rate) for l in passenger.LANDING["held_long"]], (
            "the passenger described a landing lowered for landing long as %r"
            % leg["passenger"]["landing"])
    finally:
        t.close()


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
    keep_w = (watcher.BASE, watcher.schedule_logbook_rebuild)
    asked = []
    try:
        watcher.BASE = root
        watcher.schedule_logbook_rebuild = lambda *a, **k: asked.append(k)
        settings.SETTINGS_PATH = os.path.join(root, "settings.json")
        settings._defaults.clear()
        settings._values = {}
        watcher.load_settings_at_startup()        # registers what the watcher does
        for spec in settings.SPEC:
            if spec["module"] != "grading":
                continue
            new = (not settings._defaults[spec["key"]] if spec["type"] == "bool"
                   else spec["min"])
            del asked[:]
            ok, out = watcher.apply_settings({spec["key"]: new})
            assert ok, out
            assert getattr(grading, spec["attr"]) == new, (
                "saving %s did not reach grading.%s" % (spec["key"], spec["attr"]))
            assert out["rebuild_scheduled"] and asked, (
                "saving %s changed grades and scheduled no rebuild" % spec["key"])
            del asked[:]
            ok, out = watcher.apply_settings({spec["key"]: new})
            assert not out["rebuild_scheduled"] and not asked, (
                "saving %s unchanged rebuilt the logbook anyway" % spec["key"])
    finally:
        settings.SETTINGS_PATH, defaults, settings._values, registry = keep_s
        settings._defaults.clear()
        settings._defaults.update(defaults)
        settings._registry.clear()
        settings._registry.update(registry)
        for name, value in keep_g.items():
            setattr(grading, name, value)
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
                return [(ident, "K2", LAT0 + look.far + 0.01 * i, LON0, 0.0)
                        for i, ident in enumerate(look.airports)]

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


def test_the_watcher_asks_the_sim_about_airplane_runways_only():
    assert watcher.measures_runway("Airplane", 90.0, "Test Jet")
    assert watcher.measures_runway("Airplane", 45.0, "Test Single")
    assert not watcher.measures_runway("Helicopter", 0.0, "Test Helicopter")
    assert not watcher.measures_runway("Airplane", 10.0, "Test Gyroplane")
    assert not watcher.measures_runway(None)

    keep_q = list(watcher._runway_queue)
    keep = (watcher.RUNWAY_LOOKUP, watcher.SESSIONS, watcher.EVENTS_JSONL)
    root = tempfile.mkdtemp()
    try:
        watcher.RUNWAY_LOOKUP = True
        del watcher._runway_queue[:]
        watcher.queue_runway_lookup(LAT0, LON0, "Helicopter", 0.0, "Test Helicopter")
        assert not watcher._runway_queue, "a helicopter landing queued a runway lookup"
        watcher.queue_runway_lookup(LAT0, LON0, "Airplane", 90.0, "Test Jet")
        assert watcher._runway_queue == [(LAT0, LON0)], watcher._runway_queue

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
                ev.write(json.dumps({"kind": "landing", "flight_id": fid,
                                     "lat": lat, "lon": LON0}) + "\n")
            ev.write(json.dumps({"kind": "landing", "lat": LAT0 + 3.0,
                                 "lon": LON0}) + "\n")
        assert watcher._landing_points_on_record() == [(LAT0, LON0)], (
            "the backfill would ask the sim about %r"
            % watcher._landing_points_on_record())
    finally:
        watcher.RUNWAY_LOOKUP, watcher.SESSIONS, watcher.EVENTS_JSONL = keep
        watcher._runway_queue[:] = keep_q
        shutil.rmtree(root, ignore_errors=True)


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

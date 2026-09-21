"""What the route map draws as one line, and what it breaks.

The baked map splits a track wherever two consecutive samples are further
apart than flying could account for, so it does not draw a straight line
across a reposition - and every piece it keeps gets its own start and end
marker. That is right for a real reposition and wrong for a moment where the
sim simply skipped ahead along the route: measured once at cruise, 1.4 nm
between two samples, airborne on both sides with altitude, speed and heading
unchanged, and the map drew a landing marker and a start marker a third of the
way through a flight that did neither.

These tests drive as_segments, which is what the baker calls and what decides
how many pieces - and so how many pairs of markers - a route gets.

Synthetic throughout - no track off disk, and nobody's route.

    py -3 test_map.py
"""
import math
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import mapbake                                            # noqa: E402

LAT0, LON0 = 47.5, -122.3               # synthetic; nowhere in particular


def along(nm, bearing_deg, lat=LAT0, lon=LON0):
    """A point nm away on an initial bearing. Flat-earth is plenty at this scale."""
    b = math.radians(bearing_deg)
    dlat = (nm * math.cos(b)) / 60.0
    dlon = (nm * math.sin(b)) / (60.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def cruise(n=40, step_nm=0.12, bearing=30.0, alt=34000.0, heading=None,
           on_ground=False, start=0.0):
    """n points flown along one bearing, 1 Hz, starting start nm out."""
    pts = []
    for i in range(n):
        lat, lon = along(start + i * step_nm, bearing)
        pts.append({"lat": lat, "lon": lon, "alt": alt, "on_ground": on_ground,
                    "heading": bearing if heading is None else heading,
                    "gs": 434.0, "t": float(i)})
    return pts


def skip(pts, nm, bearing=30.0, alt_delta=0.0, **extra):
    """Continue pts after a jump of nm along bearing, then fly on normally."""
    last = pts[-1]
    lat, lon = along(nm, bearing, last["lat"], last["lon"])
    rest = []
    for i in range(20):
        la, lo = along(i * 0.12, bearing, lat, lon)
        p = dict(last, lat=la, lon=lo, alt=last["alt"] + alt_delta,
                 t=last["t"] + 2.0 + i)
        p.update(extra)
        rest.append(p)
    return pts + rest


def pieces(pts):
    return len(mapbake.as_segments(pts))


# --------------------------------------------------------------------------
# joined: the aircraft is simply further along its route
# --------------------------------------------------------------------------

def test_a_sim_skip_along_the_route_is_one_line():
    """The reported case: a jump at cruise that is the route, just faster."""
    track = skip(cruise(), 1.4)
    assert pieces(track) == 1, (
        "a 1.4 nm skip along track at cruise was drawn as %d pieces - each "
        "gets a landing and a start marker the flight never had" % pieces(track))


def test_a_skip_in_a_crosswind_is_one_line():
    """Ground track, not heading.

    At altitude the aircraft points into the wind and travels along a track
    that differs by the wind correction angle. A test on heading would split
    exactly the long, windy cruises where these skips turn up. Here the nose
    is 20 degrees off the ground track - a 150 kt crosswind at 434 kt, which
    a jet stream delivers - and the skip is along the track.

    The correction has to exceed FLOWN_ACROSS_TRACK_DEG for this to prove
    anything. A first draft used 12 degrees, inside the tolerance, and a
    heading-based test passed it just as well as the real one.
    """
    assert 20.0 > mapbake.FLOWN_ACROSS_TRACK_DEG
    track = skip(cruise(bearing=45.0, heading=25.0), 1.4, bearing=45.0)
    assert pieces(track) == 1, (
        "a skip along the ground track split because the heading differed "
        "by the wind correction angle")


# --------------------------------------------------------------------------
# split: a real reposition keeps its gap
# --------------------------------------------------------------------------

def test_a_reposition_on_the_ground_still_splits():
    """A new stand is not a route. It is the case the split exists for."""
    track = skip(cruise(step_nm=0.0005, alt=1000.0, on_ground=True), 1.0)
    assert pieces(track) == 2, (
        "a 1 nm move on the ground was drawn as flown")


def test_a_jump_off_track_still_splits():
    """Airborne and level, but somewhere the aircraft was not heading."""
    track = skip(cruise(bearing=30.0), 1.4, bearing=120.0)
    assert pieces(track) == 2, (
        "a jump at right angles to the track was joined into the route")


def test_a_jump_with_a_height_change_still_splits():
    track = skip(cruise(), 1.4, alt_delta=3000.0)
    assert pieces(track) == 2, (
        "a jump that also moved 3000 ft was joined as though flown")


def test_slew_still_splits():
    """Slewing is moving the aircraft, even along its own track."""
    track = skip(cruise(), 1.4, slew=True)
    assert pieces(track) == 2, "a slewed jump was drawn as flown"


def test_a_long_jump_still_splits_however_straight():
    """Along track and level, but not a skip anyone flew."""
    n = mapbake.FLOWN_ACROSS_MAX_NM * 2
    track = skip(cruise(), n)
    assert pieces(track) == 2, (
        "a %.0f nm jump was joined because it happened to be along track" % n)


def test_a_jump_that_cannot_be_checked_still_splits():
    """Missing fields keep the old behaviour, rather than guessing.

    Two ways to be missing, and they fail through different code: no ground
    state is refused by the airborne check, while no altitude is only caught
    by the error handling around the arithmetic. Testing one covered neither
    the other nor the handler.
    """
    for field in ("on_ground", "alt"):
        track = skip(cruise(), 1.4)
        for p in track:
            p.pop(field)
        assert pieces(track) == 2, (
            "a jump with no %s was joined; the answer when this cannot vouch "
            "for a jump is to split, as it always did" % field)


def test_ordinary_flying_never_splits():
    """The baseline, at the fastest a sample normally moves."""
    assert pieces(cruise(step_nm=0.14)) == 1


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
    print("  %d map test(s), %d failed" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

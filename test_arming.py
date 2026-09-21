"""When a reported aircraft becomes a flight.

The sim answers with a valid aircraft title and a plausible position while its
own menus are still up, with the chosen aircraft parked at the departure
position. `is_valid` is satisfied by exactly those two things, so choosing an
aircraft used to mint a flight, and a session spent picking one minted several:
a few minutes of a stationary aeroplane each, no takeoff, no distance, all of
them indistinguishable from real flights in the logbook.

A candidate is now held in a ring and promoted only when something proves the
sim is flying it. These are the tests for what "proves" means.

WHY NOT SPEED
-------------
The obvious trigger is the wrong one twice over.

  AIRSPEED INDICATED on a parked aircraft reads the wind. A breezy day at the
  gate reports twenty knots of it, so any airspeed gate arms every menu.

  GROUND VELOCITY is ground-referenced and so immune to that, but a parked
  aircraft in this sim still jitters up to about a knot of it - the aeroplane
  settling on its gear. That leaves no usable room under a one-knot gate.

Net displacement over a sliding window has neither problem: an aircraft rocking
where it stands nets to nothing over a minute, and a measured sim aircraft that
is genuinely parked drifts single-digit metres per minute at worst.

The window slides rather than accumulating from the moment the candidate was
armed, which is the difference between this and the first version of the
design: a running total lets a slow drift add up past any threshold if the sim
sits in its menus for long enough. test_slow_drift_never_accumulates_over is
that bug, kept.

Synthetic throughout - no track off disk, so this runs on a fresh checkout with
no flights in it, and carries nobody's flying.

    py -3 test_arming.py
"""
import math
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import logbook_build                                      # noqa: E402
import watcher                                            # noqa: E402
watcher.log = lambda *a, **k: None      # never append to the operational log

LAT0, LON0 = 47.5, -122.3               # synthetic; nowhere in particular
NM_PER_DEG = 60.0


def sample(t, lat=LAT0, lon=LON0, on_ground=True, gs=0.0, aircraft="Test Type",
           peaks=None):
    return {"t": float(t), "ts": "1999-01-01T00:00:%02dZ" % (int(t) % 60),
            "aircraft": aircraft, "lat": lat, "lon": lon, "alt": 500.0,
            "vs": 0.0, "gs": gs, "heading": 90.0, "airspeed": 0.0,
            "on_ground": on_ground, "peaks": peaks if peaks is not None else {}}


def north_of(metres):
    """A latitude `metres` north of the fixture origin."""
    return LAT0 + (metres / 1852.0) / NM_PER_DEG


def feed_all(p, samples):
    """Feed in order, returning the first promotion reason or None."""
    for s in samples:
        why = p.feed(s)
        if why:
            return why
    return None


# --------------------------------------------------------------------------
# 1. the menus must never arm
# --------------------------------------------------------------------------

def test_a_parked_aircraft_never_promotes():
    """The bug itself: frozen position, valid aircraft, hours of it.

    Every sample identical, which is what the sim reports while its menus are
    up - the aircraft is not being simulated, so nothing about it changes.
    """
    p = watcher.PendingFlight(sample(0))
    why = feed_all(p, [sample(t) for t in range(0, 7200)])
    assert why is None, (
        "two hours of a stationary aircraft armed a flight because %s; this is "
        "the menu bug" % why)


def test_wind_jitter_never_promotes():
    """An aircraft rocking where it stands on a windy day.

    Both speeds read high and neither means anything. Airspeed is the wind
    itself - 25 kt across the airframe while the wheels have not turned. Ground
    speed genuinely swings past a knot, because rocking two metres back and
    forth is real motion; it just returns to where it started.

    Any speed gate arms here. The displacement gate must not, however long it
    runs, because the aircraft has gone nowhere.
    """
    p = watcher.PendingFlight(sample(0))
    rows = []
    for t in range(0, 7200):
        wobble = 2.0 * math.sin(t / 3.0)
        s = sample(t, lat=north_of(wobble), gs=abs(4.0 * math.cos(t / 3.0)))
        s["airspeed"] = 25.0
        rows.append(s)
    why = feed_all(p, rows)
    assert why is None, (
        "wind rocking a parked aircraft armed a flight because %s" % why)
    net = abs(north_of(2.0 * math.sin(7199 / 3.0)) - LAT0) * NM_PER_DEG * 1852.0
    assert net < watcher.FLIGHT_ARM_MOVE_M, (
        "the fixture itself drifted %.1f m, so it is not testing what it "
        "claims to" % net)


def test_slow_drift_never_accumulates_over():
    """The sliding window, as a regression.

    An earlier design measured displacement from where the candidate was
    armed. At a drift an order of magnitude gentler than the threshold that
    still crosses it given long enough - here 0.5 m a minute for four hours,
    which totals 120 m against a 50 m line. The window has to be what bounds
    this, not the ring depth.
    """
    p = watcher.PendingFlight(sample(0))
    rows = [sample(t, lat=north_of(0.5 * (t / 60.0))) for t in range(0, 14400)]
    why = feed_all(p, rows)
    assert why is None, (
        "drift totalling 120 m at 0.5 m/min armed a flight because %s; the "
        "displacement is being accumulated rather than measured over a "
        "window" % why)


# --------------------------------------------------------------------------
# 2. real flying must arm, and promptly
# --------------------------------------------------------------------------

def test_a_taxi_promotes():
    """Rolling away from the stand. Nothing else needs to happen."""
    p = watcher.PendingFlight(sample(0))
    rows = [sample(t) for t in range(0, 300)]            # five minutes parked
    # then taxi at about 6 kt: ~3 m a second
    rows += [sample(300 + t, lat=north_of(3.0 * t), gs=6.0) for t in range(0, 120)]
    why = feed_all(p, rows)
    assert why is not None, "a taxi away from the stand never armed a flight"
    assert why.startswith("moved"), "armed for the wrong reason: %s" % why


def test_a_vertical_liftoff_promotes():
    """Straight up from the spawn, which never travels 50 m along the ground.

    A helicopter does this every flight. Without the airborne trigger it would
    be recorded from the moment it drifted 50 m sideways - if it ever did.
    """
    p = watcher.PendingFlight(sample(0))
    rows = [sample(t) for t in range(0, 60)]
    rows.append(sample(60, on_ground=False))
    why = feed_all(p, rows)
    assert why == "airborne", (
        "a vertical liftoff did not arm a flight (got %r)" % why)


def test_promotion_is_prompt_once_moving():
    """A taxi must not take the whole window to be believed.

    At a walking-pace 6 kt the 50 m line is 18 s away, so a flight starts
    recording well inside the first taxi.
    """
    p = watcher.PendingFlight(sample(0))
    fired = None
    for t in range(0, 300):
        if p.feed(sample(t, lat=north_of(3.0 * t), gs=6.0)):
            fired = t
            break
    assert fired is not None and fired <= 30, (
        "took %s s of taxiing to arm; the gate is too slow to be invisible"
        % fired)


# --------------------------------------------------------------------------
# 3. the ring
# --------------------------------------------------------------------------

def test_the_ring_is_bounded():
    """It turns for as long as the sim sits in a menu, so it cannot grow."""
    p = watcher.PendingFlight(sample(0))
    feed_all(p, [sample(t) for t in range(0, watcher.FLIGHT_ARM_MAX * 3)])
    assert len(p.ring) == watcher.FLIGHT_ARM_MAX, (
        "ring held %d points against a cap of %d"
        % (len(p.ring), watcher.FLIGHT_ARM_MAX))
    assert p.dropped > 0, "the ring filled and reported dropping nothing"


def test_the_window_is_bounded_too():
    """The displacement window is a separate, much shorter deque.

    If it grew with the ring the comparison would be against a point twenty
    minutes old, which is the accumulating behaviour again by another route.
    """
    p = watcher.PendingFlight(sample(0))
    feed_all(p, [sample(t) for t in range(0, 3600)])
    span = p._win[-1][0] - p._win[0][0]
    assert span <= watcher.FLIGHT_ARM_WINDOW_SEC + 2.0, (
        "the displacement window spans %.0f s against a configured %.0f s"
        % (span, watcher.FLIGHT_ARM_WINDOW_SEC))


def test_held_points_are_kept_in_order():
    p = watcher.PendingFlight(sample(0))
    feed_all(p, [sample(t) for t in range(0, 50)])
    held = p.history()
    assert [h["t"] for h in held] == [float(t) for t in range(0, 50)], (
        "the held points are not the points fed, in order")


# --------------------------------------------------------------------------
# 4. the peak accumulator
# --------------------------------------------------------------------------

def test_peaks_are_taken_and_cleared_per_point():
    """The recorder empties the accumulator; while armed nobody else does.

    Left alone, the first real point of every flight carries a g reading
    accumulated across the whole time the sim sat in its menus.
    """
    acc = {}
    p = watcher.PendingFlight(sample(0, peaks=acc))
    acc["gforce_max"] = 4.0                 # a spike while still in the menus
    p.feed(sample(0, peaks=acc))
    assert acc == {}, (
        "the shared accumulator still holds %r after a point was held; it "
        "will be carried into the first point of the next flight" % (acc,))
    acc["gforce_max"] = 1.0
    p.feed(sample(1, peaks=acc))
    held = p.history()
    assert held[0]["peaks"].get("gforce_max") == 4.0, "first point lost its peak"
    assert held[1]["peaks"].get("gforce_max") == 1.0, (
        "the second point carries %r; peaks are accumulating across points "
        "instead of being a per-point window"
        % (held[1]["peaks"].get("gforce_max"),))


def test_a_held_snapshot_is_not_the_live_sample():
    """Feeding must copy, or every held point is the same mutated dict."""
    live = sample(0)
    p = watcher.PendingFlight(live)
    p.feed(live)
    live["lat"] = 0.0
    assert p.history()[0]["lat"] == LAT0, (
        "the ring holds a reference to the caller's sample, so the whole "
        "history changes when the next sample is read")


# --------------------------------------------------------------------------
# 5. continuity - the menu position must not ride into the flight
# --------------------------------------------------------------------------

def test_a_different_aircraft_breaks_the_candidate():
    p = watcher.PendingFlight(sample(0))
    p.feed(sample(0))
    assert not p.continues(sample(1, aircraft="Something Else")), (
        "points held for one aircraft would be carried into another's track")


def test_a_spawn_jump_breaks_the_candidate():
    """Menu preview here, spawn over there. The held points belong to neither."""
    p = watcher.PendingFlight(sample(0))
    p.feed(sample(0))
    far = sample(1, lat=LAT0 + (watcher.RESUME_JUMP_NM * 2) / NM_PER_DEG)
    assert not p.continues(far), (
        "a spawn-sized position jump did not break the candidate; the menu "
        "preview position rides into the track of the flight that follows")


def test_an_unbroken_candidate_continues():
    """The break must not fire on ordinary sampling, or the ring never fills."""
    p = watcher.PendingFlight(sample(0))
    p.feed(sample(0))
    assert p.continues(sample(1, lat=north_of(1.0))), (
        "a metre of drift broke the candidate; nothing would ever be held")


def test_an_invalid_sample_breaks_the_candidate():
    p = watcher.PendingFlight(sample(0))
    p.feed(sample(0))
    blank = sample(1)
    blank["aircraft"] = None
    assert not p.continues(blank), "a sample with no aircraft continued a candidate"


# --------------------------------------------------------------------------
# 6. what a promoted flight looks like
# --------------------------------------------------------------------------

def test_a_promoted_flight_starts_when_its_oldest_point_did():
    """Not when it proved itself.

    Otherwise every cold-and-dark start is stamped at the moment the aircraft
    first rolled, and the whole point of holding the points is lost.
    """
    import types
    rows = [sample(t) for t in range(0, 10)]
    rows[0]["ts"] = "1999-01-01T00:00:00+00:00"
    recorded = []
    flight = types.SimpleNamespace()
    # Drive Flight.__init__'s history loop without touching the disk.
    watcher.Flight._record_point = lambda self, s: recorded.append(s)
    watcher.Flight._save_meta = lambda self, **kw: None
    saved_claim = watcher.persistence.claim_recording
    watcher.persistence.claim_recording = lambda path: None
    try:
        flight = watcher.Flight(rows[-1], history=rows)
    finally:
        watcher.persistence.claim_recording = saved_claim
    assert flight.started_at == "1999-01-01T00:00:00+00:00", (
        "started_at is %r, not the oldest held point" % flight.started_at)
    assert len(recorded) == len(rows), (
        "%d of %d held points reached the track" % (len(recorded), len(rows)))


# --------------------------------------------------------------------------
# 7. the builder backstop, for the ones already on disk
# --------------------------------------------------------------------------

def sortie(**kw):
    base = {"sortie_id": "flt-19990101T000000Z", "legs": [], "distance_nm": 0.0,
            "airborne_s": 0.0, "edited": False}
    base.update(kw)
    return base


def test_a_sortie_that_never_moved_is_not_published():
    assert not logbook_build.is_real_sortie(sortie()), (
        "a sortie with no legs, no distance and no airborne time is published")


def test_a_sortie_with_a_leg_is_published():
    assert logbook_build.is_real_sortie(sortie(legs=[{"leg": 1}]))


def test_a_sortie_that_only_taxied_is_published():
    """It went somewhere. Not flying is not the same as not happening."""
    assert logbook_build.is_real_sortie(sortie(distance_nm=0.4))


def test_a_sortie_that_only_hovered_is_published():
    assert logbook_build.is_real_sortie(sortie(airborne_s=45.0))


def test_an_edited_sortie_is_published_even_when_empty():
    """Emptied by the user, so the row is their own work and stays visible."""
    assert logbook_build.is_real_sortie(sortie(edited=True)), (
        "a sortie the user emptied themselves vanished from the logbook")


# --------------------------------------------------------------------------
# 7b. the decision the detect loop actually makes
# --------------------------------------------------------------------------

class FakeTracker(object):
    """Enough of ClipTracker for arm_or_begin. Records what it was asked."""

    def __init__(self):
        self.leg = 0
        self.attached = []

    def finish(self):
        pass

    def reset(self):
        pass

    def attach(self, flight):
        self.attached.append(flight)


class NoDisk(object):
    """Flight, minus the disk. Nothing here may write to a sessions tree."""

    def __init__(self):
        self.saved = []
        self._keep = (watcher.Flight._record_point, watcher.Flight._save_meta,
                      watcher.persistence.claim_recording, watcher.write_event)

    def __enter__(self):
        recorded = self.saved
        watcher.Flight._record_point = lambda self, s: recorded.append(s)
        watcher.Flight._save_meta = lambda self, **kw: None
        watcher.persistence.claim_recording = lambda path: None
        watcher.write_event = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        (watcher.Flight._record_point, watcher.Flight._save_meta,
         watcher.persistence.claim_recording, watcher.write_event) = self._keep
        return False


def test_the_decision_holds_a_parked_aircraft():
    """The whole bug, through the function the detect loop calls.

    PendingFlight being right is not the same as it being reached. Every
    lifecycle defect in this project so far has been in a branch the tests
    drove around rather than through.
    """
    with NoDisk() as d:
        pending = None
        flight = None
        for t in range(0, 600):
            flight, pending = watcher.arm_or_begin(
                sample(t), FakeTracker(), pending, None)
            assert flight is None, (
                "a flight was started at t=%d with the aircraft parked" % t)
        assert pending is not None and len(pending.ring) == 600
        assert d.saved == [], "points were written for an aircraft that never moved"


def test_the_decision_promotes_and_hands_over_the_ring():
    with NoDisk() as d:
        pending = None
        flight = None
        tracker = FakeTracker()
        for t in range(0, 300):
            flight, pending = watcher.arm_or_begin(
                sample(t), tracker, pending, None)
        assert flight is None and pending is not None
        held = len(pending.ring)
        # now move
        for t in range(300, 340):
            flight, pending = watcher.arm_or_begin(
                sample(t, lat=north_of(3.0 * (t - 300)), gs=6.0),
                tracker, pending, None)
            if flight is not None:
                break
        assert flight is not None, "moving off never started a flight"
        assert pending is None, (
            "a flight and a candidate are both live; they are alternatives")
        assert len(d.saved) > held, (
            "%d points written for a ring that held %d; the history was not "
            "handed over" % (len(d.saved), held))
        assert tracker.attached and tracker.attached[-1] is flight


def test_the_decision_never_holds_back_a_reconnect():
    """A watcher restart mid-flight must resume, not re-arm.

    Otherwise restarting the watcher in the cruise stops recording until the
    aircraft happens to move 50 m - which at altitude it does immediately, but
    parked at a fuel stop it does not.
    """
    with NoDisk():
        snap = {"aircraft": "Test Type", "lat": LAT0, "lon": LON0,
                "sortie_id": "flt-19990101T000000Z",
                "flight_id": "flt-19990101T000000Z"}
        flight, pending = watcher.arm_or_begin(
            sample(0), FakeTracker(), None, snap)
        assert flight is not None, (
            "a reconnect to an outing already under way was held as a "
            "candidate; recording stops until the aircraft moves")
        assert pending is None
        assert flight.sortie_id == "flt-19990101T000000Z", (
            "the resumed fragment did not rejoin its sortie")


def test_the_decision_drops_the_ring_when_the_aircraft_changes():
    """Swapping airframe without moving.

    The reposition check inside feed() cannot see this - the aircraft has not
    gone anywhere, it has become something else - so continues() is the only
    thing standing between one airframe's held points and another's track.
    """
    with NoDisk():
        tracker, pending, flight = FakeTracker(), None, None
        for t in range(0, 120):
            flight, pending = watcher.arm_or_begin(
                sample(t), tracker, pending, None)
        assert len(pending.ring) == 120
        flight, pending = watcher.arm_or_begin(
            sample(120, aircraft="A Different Type"), tracker, pending, None)
        assert flight is None
        assert pending.aircraft == "A Different Type", (
            "the candidate still names the previous airframe")
        assert len(pending.ring) == 1, (
            "%d point(s) recorded for one airframe were kept for another"
            % len(pending.ring))


def test_the_decision_drops_the_ring_on_a_spawn_jump():
    """Menu preview, then a spawn somewhere else.

    The held points belong to the preview position. Carrying them into the new
    flight's track puts a teleport at the start of it.
    """
    with NoDisk():
        pending = None
        flight = None
        tracker = FakeTracker()
        for t in range(0, 120):
            flight, pending = watcher.arm_or_begin(
                sample(t), tracker, pending, None)
        assert len(pending.ring) == 120
        far = LAT0 + (watcher.RESUME_JUMP_NM * 3) / NM_PER_DEG
        flight, pending = watcher.arm_or_begin(
            sample(120, lat=far), tracker, pending, None)
        assert flight is None
        assert len(pending.ring) == 1, (
            "the ring kept %d points across a spawn jump" % len(pending.ring))


def test_a_new_parking_spot_is_not_a_flight():
    """Changing your mind about where to start.

    The aircraft is somewhere else without having gone there: the position
    changes in one sample and the sim reports no ground speed for it. The
    continuity check only catches repositions bigger than RESUME_JUMP_NM, so
    everything from a stand away to most of an airport used to land between
    that and the 50 m trigger, and minted a flight every time.
    """
    for nm, label in ((120.0, "another airport"), (4.0, "across this airport"),
                      (1.0, "another ramp"), (0.3, "the next stand"),
                      (0.05, "one stand over")):
        with NoDisk():
            tracker, pending, flight = FakeTracker(), None, None
            for t in range(0, 120):
                flight, pending = watcher.arm_or_begin(
                    sample(t), tracker, pending, None)
            assert flight is None, "armed before moving at all (%s)" % label
            far = LAT0 + nm / NM_PER_DEG
            for t in range(120, 240):
                flight, pending = watcher.arm_or_begin(
                    sample(t, lat=far), tracker, pending, None)
                if flight is not None:
                    break
            assert flight is None, (
                "a reposition of %.2f nm (%s) started a flight" % (nm, label))


def test_slew_is_a_reposition():
    """Slewing is moving the aircraft, not flying it.

    Deliberately slewed at a rate the reported ground speed would account for,
    so the arithmetic alone cannot reject it. Only the slew flag can, which is
    the point: a big obvious teleport would have let this pass whatever the
    flag did.
    """
    p = watcher.PendingFlight(sample(0))
    p.feed(sample(0))
    why = None
    d = 0.0
    for t in range(1, 120):
        d += 20.0                                  # 20 m a second, gs says 60 kt
        s = sample(t, lat=north_of(d), gs=60.0)    # 60 kt reaches 46 m - plenty
        s["slew"] = True
        why = p.feed(s)
        if why:
            break
    assert why is None, "slewing armed a flight (%s)" % why
    assert p.repositions > 0, "slew was never recognised as a reposition"


def test_a_step_with_no_speed_reading_cannot_arm():
    """A missing reading must never read as a good one.

    Nothing corroborates the distance, so the answer that does not invent a
    flight is the safe one - the same rule the landing grade follows when a
    touchdown rate cannot be recovered.
    """
    p = watcher.PendingFlight(sample(0))
    p.feed(sample(0))
    why = None
    d = 0.0
    for t in range(1, 120):
        d += 20.0
        s = sample(t, lat=north_of(d))
        s["gs"] = None
        why = p.feed(s)
        if why:
            break
    assert why is None, (
        "a run of steps with no ground speed reading armed a flight (%s)" % why)


def test_a_takeoff_roll_is_not_a_reposition():
    """The corroboration must not reject real speed.

    An airliner is doing about 150 kt on the ground at rotation, which is
    roughly 77 m between one held point and the next - far beyond anything a
    reposition floor may forbid. The speed reading is what makes it legitimate.
    """
    p = watcher.PendingFlight(sample(0))
    why = None
    d = 0.0
    for t in range(0, 60):
        gs = min(150.0, 4.0 * t)                  # accelerating down the runway
        d += gs * 0.514444
        why = p.feed(sample(t, lat=north_of(d), gs=gs))
        if why:
            break
    assert why is not None, "a takeoff roll never armed a flight"
    assert p.repositions == 0, (
        "%d step(s) of a takeoff roll were rejected as repositions"
        % p.repositions)


def test_parked_jitter_is_not_a_reposition():
    """Or the ring is shredded and a cold start keeps no prefix.

    A parked aircraft drifts single metres per minute with the ground speed
    reading at zero, and zero speed cannot corroborate any distance at all.
    The floor is what keeps that from reading as a reposition every second.
    """
    p = watcher.PendingFlight(sample(0))
    rows = [sample(t, lat=north_of(0.4 * math.sin(t / 7.0))) for t in range(0, 900)]
    feed_all(p, rows)
    assert p.repositions == 0, (
        "ordinary parked drift was called a reposition %d time(s); the ring "
        "is being cleared under a cold-and-dark start" % p.repositions)
    assert len(p.ring) == 900, (
        "the ring holds %d of 900 parked points" % len(p.ring))


def test_a_promotion_does_not_swallow_the_takeoff():
    """The transition that promotes is also the one the tracker needs.

    _begin_new_flight resets the tracker, which clears prev_on_ground, and
    takeoff detection is precisely "was on the ground, now is not". Promote on
    the first airborne sample and that memory is wiped by the very event it
    describes - so a helicopter that lifts straight up starts a flight and
    never logs a takeoff for it.

    Harmless before this change, because a flight was minted while the
    aircraft was still parked and the memory was rebuilt long before it flew.

    Driven through a real ClipTracker: a FakeTracker would have no opinion
    about prev_on_ground and this would pass no matter what.
    """
    with NoDisk():
        tracker = watcher.ClipTracker()
        pending = None
        flight = None
        for t in range(0, 120):                       # parked, on the ground
            s = sample(t)
            tracker.feed(s)
            flight, pending = watcher.arm_or_begin(s, tracker, pending, None)
        assert flight is None, "armed while parked"

        lift = sample(120, on_ground=False)           # straight up
        tracker.feed(lift)
        flight, pending = watcher.arm_or_begin(lift, tracker, pending, None)
        assert flight is not None, "the liftoff did not start a flight"

        # The next sample is the first the tracker sees with a flight attached.
        tracker.feed(sample(121, on_ground=False))
        assert tracker.pending is not None and tracker.pending["kind"] == "takeoff", (
            "no takeoff was opened after the aircraft left the ground; the "
            "promotion reset the tracker's ground memory and the takeoff went "
            "unrecorded (prev_on_ground=%r)" % tracker.prev_on_ground)


# --------------------------------------------------------------------------
# 7c. "not on the ground" is not the same as flying
# --------------------------------------------------------------------------
#
# While the sim loads an aircraft in, it can report SIM ON GROUND false for a
# few ticks with the aircraft still frozen on its pad - G FORCE reads 0.0,
# because nothing is being simulated. That blip used to start the flight and
# then, a moment later, become a landing: 1 second, 0 nm, graded A. The
# tracker runs at 10 Hz, so the blip has to be driven at 10 Hz; the recording
# is 1 Hz and flattens it into a single sample, which does not reproduce it.

class _AlwaysFlown(watcher.ClipTracker):
    """The tracker as it was before landings required a flight to land from."""
    flown = property(lambda self: True, lambda self, v: None)


def _tracker(cls=watcher.ClipTracker):
    import types
    tr = cls()
    tr.attach(types.SimpleNamespace(flight_id="flt-19990101T000000Z",
                                    sortie_id="flt-19990101T000000Z",
                                    aircraft="Test Type"))
    tr.committed = []
    tr._commit = lambda kind, ev, rate, **k: tr.committed.append(kind)
    return tr


def _drive(tr, phases, t0=0.0):
    """Feed (seconds, on_ground, g) phases at 10 Hz. Returns the end time."""
    t = t0
    for seconds, on_ground, g in phases:
        for _ in range(int(round(seconds * 10))):
            s = sample(t, on_ground=on_ground)
            s["gforce"] = g
            s["vs"] = 0.0 if on_ground else -300.0
            tr.feed(s)
            t += 0.1
    return t


BLIP = [(20.0, True, 0.0),       # frozen on the pad while the sim loads
        (0.3, False, 0.0),       # three ticks of "not on the ground"
        (6.0, True, 1.0)]        # physics starts; still on the pad


def test_a_load_in_blip_is_not_a_landing():
    with NoDisk():
        before = _tracker(_AlwaysFlown)
        _drive(before, BLIP)
        assert before.committed == ["landing"], (
            "fixture does not reproduce the fault: the old rule logged %r, "
            "not a landing" % (before.committed,))

        tr = _tracker()
        _drive(tr, BLIP)
        assert tr.committed == [], (
            "a load-in blip with the aircraft frozen on its pad was logged as "
            "%r" % (tr.committed,))


def test_a_one_tick_flicker_while_taxiing_is_not_a_landing():
    """Same rule, different cause: physics running, a single-tick flicker.

    G force reads a normal 1.0 here, so no g test could catch it. It is the
    reason this is fixed at the landing and not only at the arming gate.
    """
    phases = [(10.0, True, 1.0), (0.2, False, 1.0), (6.0, True, 1.0)]
    with NoDisk():
        before = _tracker(_AlwaysFlown)
        _drive(before, phases)
        assert before.committed == ["landing"], "fixture does not reproduce it"
        tr = _tracker()
        _drive(tr, phases)
        assert tr.committed == [], (
            "a flicker off the ground while taxiing became %r" % (tr.committed,))


def test_a_real_flight_still_lands():
    with NoDisk():
        tr = _tracker()
        _drive(tr, [(10.0, True, 1.0), (60.0, False, 1.0), (6.0, True, 1.0)])
        assert tr.committed == ["takeoff", "landing"], (
            "a normal flight logged %r" % (tr.committed,))


def test_a_bounce_is_still_one_landing():
    """Down, up for a second, down again - one arrival, not none."""
    with NoDisk():
        tr = _tracker()
        _drive(tr, [(10.0, True, 1.0), (60.0, False, 1.0),
                    (0.5, True, 1.3), (1.0, False, 1.0), (6.0, True, 1.1)])
        assert tr.committed.count("landing") == 1, (
            "a bounced landing logged %r" % (tr.committed,))


def test_a_touch_and_go_lands_twice():
    with NoDisk():
        tr = _tracker()
        _drive(tr, [(10.0, True, 1.0), (60.0, False, 1.0), (6.0, True, 1.0),
                    (30.0, False, 1.0), (6.0, True, 1.0)])
        assert tr.committed.count("landing") == 2, (
            "two real landings logged as %r" % (tr.committed,))


def test_a_hop_too_short_to_be_a_takeoff_is_not_a_landing():
    """A second off the ground after landing is neither a takeoff nor a landing.

    The takeoff debounce already refused the lift as too brief; the old rule
    then logged the set-down anyway, as a landing with no takeoff.
    """
    phases = [(10.0, True, 1.0), (60.0, False, 1.0), (6.0, True, 1.0),
              (1.0, False, 1.0), (6.0, True, 1.0)]
    with NoDisk():
        before = _tracker(_AlwaysFlown)
        _drive(before, phases)
        assert before.committed.count("landing") == 2, "fixture does not reproduce it"
        tr = _tracker()
        _drive(tr, phases)
        assert tr.committed.count("landing") == 1, (
            "a one-second hop was logged as a second landing: %r" % (tr.committed,))


def test_a_restart_just_before_touchdown_still_lands():
    """Reattached in the air, a moment before the wheels touch.

    The tracker has not seen two seconds airborne yet, but the outing it is
    rejoining was already flying. Refusing this landing would lose a real one.
    """
    with NoDisk():
        tr = watcher.ClipTracker()
        tr.committed = []
        tr._commit = lambda kind, ev, rate, **k: tr.committed.append(kind)
        snap = {"aircraft": "Test Type", "lat": LAT0, "lon": LON0,
                "sortie_id": "flt-19990101T000000Z",
                "flight_id": "flt-19990101T000000Z"}
        s = sample(0.0, on_ground=False)
        s["gforce"] = 1.0
        tr.feed(s)
        flight, _ = watcher.arm_or_begin(s, tr, None, snap)
        assert flight is not None and tr.flown, (
            "a reconnect in the air was not treated as a flight under way")
        _drive(tr, [(0.5, False, 1.0), (6.0, True, 1.0)], t0=0.1)
        assert tr.committed.count("landing") == 1, (
            "a landing half a second after reattaching was refused: %r"
            % (tr.committed,))


def test_a_disk_resume_in_the_air_is_still_flying():
    """The other way back into a flight under way: reopening it from disk."""
    import types
    with NoDisk():
        tr = watcher.ClipTracker()
        tr.leg = 1                               # skip the clip-directory scan
        flight = types.SimpleNamespace(flight_id="flt-19990101T000000Z",
                                       started_at="1999-01-01T00:00:00Z",
                                       update=lambda s: None)
        s = sample(0.0, on_ground=False)
        watcher._keep_existing_flight(flight, tr, s, "test")
        assert tr.flown, (
            "a flight resumed from disk while airborne was not treated as "
            "flying, so a landing in the next two seconds would be refused")


def test_a_frozen_airborne_sample_does_not_arm():
    p = watcher.PendingFlight(sample(0))
    p.feed(sample(0))
    s = sample(1, on_ground=False)
    s["gforce"] = 0.0
    assert p.feed(s) is None, (
        "a not-on-ground sample reading 0.0 g - a frozen aircraft - armed a flight")


def test_a_liftoff_at_one_g_arms():
    p = watcher.PendingFlight(sample(0))
    s = sample(1, on_ground=False)
    s["gforce"] = 1.0
    assert p.feed(s) == "airborne"


def test_a_missing_g_reading_still_arms():
    """No reading is not evidence of a frozen sim.

    Legacy-fallback samples carry no g at all. Treating that as frozen would
    stop a real lift-off from starting a flight on exactly those ticks.
    """
    p = watcher.PendingFlight(sample(0))
    s = sample(1, on_ground=False)
    s.pop("gforce", None)
    s["peaks"] = {}
    assert p.feed(s) == "airborne", "a lift-off with no g reading was refused"


def test_a_peak_reading_counts_as_simulating():
    """An instant reading of zero with a normal peak in the same second."""
    p = watcher.PendingFlight(sample(0))
    s = sample(1, on_ground=False, peaks={"gforce_max": 1.02})
    s["gforce"] = 0.0
    assert p.feed(s) == "airborne", (
        "a sample whose peak g was 1.02 was treated as a frozen sim")


# --------------------------------------------------------------------------
# 8. the backstop, driven through the real builder
# --------------------------------------------------------------------------

class Tree(object):
    """A sessions tree the builder is pointed at, thrown away afterwards."""

    # BASE among them, and it has to be. write_months takes its directory from
    # SESSIONS but builds the file path from BASE, so a fixture that redirects
    # only SESSIONS writes its month files into the real repository - which is
    # exactly what this one did until it was caught.
    NAMES = ("BASE", "SESSIONS", "CLIPS_DIR", "EVENTS_JSONL", "LOGBOOK_JSON",
             "CACHE_JSON", "EXCLUDED_JSON", "DETAIL_DIR")

    def __init__(self):
        import tempfile
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
        os.makedirs(logbook_build.CLIPS_DIR, exist_ok=True)

    def close(self):
        import shutil
        for name, value in self._keep.items():
            setattr(logbook_build, name, value)
        shutil.rmtree(self.root, ignore_errors=True)

    def flight(self, fid, rows):
        import json
        with open(os.path.join(self.dir, fid + ".jsonl"), "w",
                  encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        with open(os.path.join(self.dir, fid + ".meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"flight_id": fid, "sortie_id": fid,
                       "aircraft": "Test Type", "category": "Airplane",
                       "vs0": 45.0, "started_at": rows[0]["ts"],
                       "ended_at": rows[-1]["ts"]}, f)


def row(i, lat=LAT0, alt=500.0, gs=0.0, on_ground=True):
    return {"ts": "1999-01-01T%02d:%02d:%02d+00:00" % (i // 3600, (i // 60) % 60, i % 60),
            "lat": lat, "lon": LON0, "alt": alt, "vs": 0.0, "gs": gs,
            "heading": 90.0, "airspeed": gs, "on_ground": on_ground}


def test_the_builder_leaves_out_a_sortie_that_never_moved():
    """End to end, not the predicate on its own.

    The predicate being right is not the same as it being reached: the empty
    sorties already on disk are exactly the ones a rebuild finds in its cache,
    and an earlier draft filtered inside the build loop, which the cached path
    skips entirely.
    """
    t = Tree()
    try:
        t.flight("flt-19990101T000000Z", [row(i) for i in range(200)])
        t.flight("flt-19990101T020000Z",
                 [row(i, lat=north_of(30.0 * i), alt=500.0 + 40.0 * i,
                      gs=140.0, on_ground=(i < 5)) for i in range(400)])
        doc = logbook_build.build(bake_maps=False, allow_network=False, force=True)
        ids = set()
        import json
        months = os.path.join(t.dir, "months")
        for name in os.listdir(months):
            if name.endswith(".json"):
                for s in json.load(open(os.path.join(months, name),
                                        encoding="utf-8")).get("sorties", []):
                    ids.add(s["sortie_id"])
        assert "flt-19990101T000000Z" not in ids, (
            "the frozen sortie reached the logbook: %s" % sorted(ids))
        assert "flt-19990101T020000Z" in ids, (
            "the flight that actually flew was dropped too: %s" % sorted(ids))
        assert doc is not None
    finally:
        t.close()


def test_the_builder_leaves_out_a_cached_empty_sortie():
    """Second pass, so the frozen sortie is served from the cache."""
    t = Tree()
    try:
        t.flight("flt-19990101T000000Z", [row(i) for i in range(200)])
        t.flight("flt-19990101T020000Z",
                 [row(i, lat=north_of(30.0 * i), alt=500.0 + 40.0 * i,
                      gs=140.0, on_ground=(i < 5)) for i in range(400)])
        logbook_build.build(bake_maps=False, allow_network=False, force=True)
        logbook_build.build(bake_maps=False, allow_network=False)   # cached
        import json
        ids = set()
        months = os.path.join(t.dir, "months")
        for name in os.listdir(months):
            if name.endswith(".json"):
                for s in json.load(open(os.path.join(months, name),
                                        encoding="utf-8")).get("sorties", []):
                    ids.add(s["sortie_id"])
        assert "flt-19990101T020000Z" in ids, (
            "the flight that flew is missing, so this test would pass however "
            "the filter behaved: %s" % sorted(ids))
        assert "flt-19990101T000000Z" not in ids, (
            "a cached empty sortie came back into the logbook: %s" % sorted(ids))
    finally:
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
    print("  %d arming test(s), %d failed" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

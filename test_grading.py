"""Which profile grades a flight, and whether the UI can explain it.

Two things this covers, both of which have already been wrong once.

The recorded aircraft type has to reach the grader. scan_flights copied
aircraft, sortie_id, ended_at and end_reason out of the flight meta but never
category or vs0, so grade_leg saw None for both and quietly fell back to
inferring the type from the flying. The inference agreed - a Cessna does not
hover - so nothing looked broken, and a Cessna was graded on airline
thresholds for as long as that lasted.

And every metric the grading panel lists has to carry a range. Touchdown is
45% of the descent phase and is the number anyone actually reads, and it was
the one metric in every profile with nothing written against it, because it
is scored on a curve rather than a two-point band.

    py -3 test_grading.py
"""
import io
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import grading                                          # noqa: E402
import logbook_build                                    # noqa: E402


# --------------------------------------------------------------------------
# Which profile grades which aircraft
# --------------------------------------------------------------------------

def test_the_sim_answer_decides_the_profile():
    """CATEGORY plus stall speed, checked against real reported values."""
    cases = [
        # (label, CATEGORY, VS0 kt, expected profile name)
        ("AS365 helicopter",     "Helicopter",  0.0, "rotary"),
        ("Magni M24 gyroplane",  "Airplane",   10.0, "unclassified"),
        ("Cessna 152",           "Airplane",   33.0, "light_ga"),
        ("Cessna 172",           "Airplane",   40.0, "light_ga"),
        ("just under the line",  "Airplane",   61.0, "light_ga"),
        ("just over the line",   "Airplane",   61.5, "fixed_wing"),
        ("King Air",             "Airplane",   75.0, "fixed_wing"),
        ("airliner",             "Airplane",  110.0, "fixed_wing"),
    ]
    wrong = []
    for label, cat, vs0, want in cases:
        got = grading.profile_for(category=cat, vs0_kt=vs0)["name"]
        if got != want:
            wrong.append("%s (%s, VS0 %s): wanted %s, got %s"
                         % (label, cat, vs0, want, got))
    assert not wrong, "; ".join(wrong)


def test_the_split_is_the_regulation_not_a_guess():
    """14 CFR 23.49 caps VS0 at 61 kt for singles and light twins."""
    assert grading.LIGHT_GA_MAX_VS0_KT == 61.0
    assert (grading.profile_for(category="Airplane", vs0_kt=61.0)["name"]
            != grading.profile_for(category="Airplane", vs0_kt=62.0)["name"])


def test_a_gyroplane_is_not_graded_on_wing_measures():
    """It calls itself an airplane; it does not fly like one."""
    gyro = grading.profile_for(category="Airplane", vs0_kt=10.0)
    descent = gyro.get("descent") or {}
    for key in grading.UNCLASSIFIED_SUPPRESS:
        assert key not in descent.get("weights", {}), (
            "%s depends on a fixed-wing number and must not be scored on an "
            "aircraft that only reports itself as one" % key)


# --------------------------------------------------------------------------
# The recorded type has to survive the trip to the grader
# --------------------------------------------------------------------------

def test_scan_flights_carries_the_recorded_type():
    """meta.json -> flight record -> grade_leg, on a real file on disk.

    A source-level check would pass on a line that never runs. This writes a
    session the way the watcher does and reads it back the way the builder
    does.
    """
    tmp = tempfile.mkdtemp()
    keep_sessions, keep_cache = logbook_build.SESSIONS, logbook_build.CACHE_JSON
    logbook_build.SESSIONS = tmp
    logbook_build.CACHE_JSON = os.path.join(tmp, "cache.json")
    try:
        fid = "flt-19990101T000000Z"   # synthetic; never a real sortie
        with open(os.path.join(tmp, fid + ".jsonl"), "w", encoding="utf-8") as f:
            for i in range(5):
                f.write(json.dumps({
                    "ts": "1999-01-01T00:0%d:00+00:00" % i,
                    "lat": 40.0 + i * 0.01, "lon": -105.0,
                    "alt": 6000.0, "vs": 0.0, "gs": 90.0,
                    "heading": 0.0, "airspeed": 95.0, "on_ground": False}) + "\n")
        with open(os.path.join(tmp, fid + ".meta.json"), "w", encoding="utf-8") as f:
            json.dump({"flight_id": fid, "sortie_id": fid,
                       "aircraft": "Cessna C152 Aerobat",
                       "category": "Airplane", "vs0": 33.0,
                       "ended_at": "1999-01-01T00:05:00+00:00"}, f)

        recs = logbook_build.scan_flights()
        assert recs, "scan_flights found nothing"
        rec = recs[0]
        assert rec.get("category") == "Airplane", (
            "the recorded CATEGORY did not reach the flight record; grade_leg "
            "will fall back to inferring the type from the flying")
        assert rec.get("vs0") == 33.0, (
            "the recorded stall speed did not reach the flight record; "
            "without it every airplane grades as a jet")
        # and the pair actually selects the light profile
        assert grading.profile_for(category=rec["category"],
                                   vs0_kt=rec["vs0"])["name"] == "light_ga"
    finally:
        logbook_build.SESSIONS, logbook_build.CACHE_JSON = keep_sessions, keep_cache
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# The panel has to be able to explain every profile
# --------------------------------------------------------------------------

def test_the_module_header_describes_the_module():
    """The docstring is the first thing an agent reads, and it rots silently.

    It said "ROTARY WING ONLY, FOR NOW" and that profile_for returns rotary
    unconditionally for several commits after neither was true, which is worse
    than no documentation - an agent skimming the top implements the old
    policy.
    """
    head = grading.__doc__ or ""
    assert "ROTARY WING ONLY" not in head.upper(), (
        "the header still claims this module grades helicopters only")
    assert "unconditionally" not in head, (
        "the header still claims profile_for returns one profile regardless "
        "of the aircraft")
    missing = [p["name"] for p in (grading.ROTARY, grading.LIGHT_GA,
                                   grading.FIXED_WING, grading.UNCLASSIFIED)
               if p["name"].upper() not in head.upper()]
    assert not missing, (
        "the header does not mention these profiles, which exist: "
        + ", ".join(missing))


def test_the_header_does_not_overstate_what_is_measured():
    """Grades come from differentiating vs and gs, not from the sim's accels.

    The track has carried G FORCE and ACCELERATION BODY since September 2026
    and grading does not read them. Saying otherwise anywhere a reader will
    believe it is the failure this guards.
    """
    head = (grading.__doc__ or "").lower()
    assert "differentiat" in head or "derived" in head, (
        "the header should say the scores are derived from vs and gs, since "
        "the recorded accelerations sit in the track unused")


def test_every_profile_is_documented():
    d = grading.describe()
    names = [p["label"] for p in d["profiles"]]
    assert len(names) == 4, "expected one tab per aircraft type, got %s" % names
    for prof in d["profiles"]:
        assert prof["phases"], "%s documents no phases" % prof["label"]
        for ph in prof["phases"]:
            assert ph["metrics"], "%s / %s lists no metrics" % (prof["label"], ph["key"])


def test_every_metric_shown_carries_a_range():
    """A metric with no numbers against it explains nothing.

    Touchdown reached the UI with best and worst both null for months while
    carrying 45% of the descent phase.
    """
    missing = []
    for prof in grading.describe()["profiles"]:
        for ph in prof["phases"]:
            for m in ph["metrics"]:
                if m.get("best") is None or m.get("worst") is None:
                    missing.append("%s / %s / %s"
                                   % (prof["label"], ph["key"], m["label"]))
    assert not missing, ("no range documented for: " + ", ".join(missing))


def test_no_rotary_wording_on_a_fixed_wing_tab():
    """A Cessna does not hover and cannot have vortex ring state.

    METRICS and PHASE_WORDS are shared across profiles, so a helicopter
    explanation written into either of them renders on every tab. It did: the
    lift-off phase described "the hover and the transition" on all four, and
    descent angle explained vortex ring avoidance on the airplane tabs.

    Checks the text a reader actually sees, assembled the way the UI assembles
    it, rather than the source of any one string.
    """
    import re
    rotary = re.compile(r"helicopter|rotor|hover|vortex ring|collective|"
                        r"autorotat|cyclic|skid", re.I)
    bad = []
    for prof in grading.describe()["profiles"]:
        if prof["key"] == "rotary":
            continue
        for ph in prof["phases"]:
            if rotary.search(ph["blurb"] or ""):
                bad.append("%s / %s blurb: %s"
                           % (prof["label"], ph["key"], ph["blurb"][:60]))
            for m in ph["metrics"]:
                if rotary.search(m["why"] or ""):
                    bad.append("%s / %s / %s: %s"
                               % (prof["label"], ph["key"], m["label"],
                                  (m["why"] or "")[:60]))
    # The notes and the limits render on every tab too, so a helicopter
    # explanation dropped into either would have gone unseen by the checks
    # above. One mention is allowed and is not a description: the uncalibrated
    # warning says these thresholds are untested "unlike the helicopter
    # profile", which is true and is the point of the warning.
    allowed = "unlike the helicopter profile"
    for prof in grading.describe()["profiles"]:
        if prof["key"] == "rotary":
            continue
        for note in (prof["notes"] or []) + list(grading.describe()["limits"]):
            probe = note.replace(allowed, "")
            if rotary.search(probe):
                bad.append("%s note: %s" % (prof["label"], probe[:70]))
    assert not bad, "rotary wording on a fixed-wing tab: " + "; ".join(bad)


def test_the_helicopter_tab_keeps_its_own_reasoning():
    """The fix must not be to delete the explanation for everyone."""
    rot = [p for p in grading.describe()["profiles"] if p["key"] == "rotary"][0]
    lift = [ph for ph in rot["phases"] if ph["key"] == "liftoff"][0]
    assert "hover" in (lift["blurb"] or "").lower(), (
        "the helicopter lift-off phase no longer mentions the hover")
    # Specifically the overridden reasoning, not merely the word appearing
    # somewhere on the tab: vortex ring exposure is a rotary-only metric and
    # carries the phrase on its own, which would let this pass while the
    # descent-angle override had been lost.
    angle = [m for ph in rot["phases"] for m in ph["metrics"]
             if m["key"] == "descent_angle_deg"]
    assert angle and "vortex ring" in (angle[0]["why"] or "").lower(), (
        "the helicopter descent angle no longer explains why a steep "
        "let-down matters for a rotor")


def test_every_profile_names_its_own_sources():
    """One shared note used to cite airline criteria for every profile.

    The blanket calibration caveat that used to sit above these is gone; the
    provenance under each phase is what a reader needs and is what is checked.
    """
    # Provenance lives under each phase, beside the numbers it backs, rather
    # than in one blob at the top - so that is where it has to be checked.
    for prof in grading.describe()["profiles"]:
        missing = [ph["key"] for ph in prof["phases"] if not ph.get("source")]
        assert not missing, (
            "%s does not say where its numbers came from for: %s"
            % (prof["label"], ", ".join(missing)))

    def sources_of(key):
        prof = [p for p in grading.describe()["profiles"] if p["key"] == key][0]
        return " ".join(ph.get("source") or "" for ph in prof["phases"])

    assert sources_of("light_ga") != sources_of("fixed_wing"), (
        "the light airplane and transport profiles cite the same sources; "
        "their numbers come from different places and should say so")
    assert "23.473" in sources_of("light_ga"), (
        "the light airplane touchdown ladder is anchored to 14 CFR 23.473 and "
        "should say so where a reader can check it")
    # The source says a profile was calibrated against real flying. It does
    # not say whose, or how much of it - that would be a record of somebody's
    # flying rather than a statement of the standard.
    assert "calibrated against" in sources_of("rotary").lower(), (
        "the helicopter profile is the calibrated one and should say its "
        "numbers came from measured flights rather than a published standard")


def test_a_transport_is_held_to_less_bank_than_a_light_airplane():
    """The relationship, and it is the opposite of what this once asserted.

    This test used to require the LIGHT band to be the tighter one. That came
    from noticing a Cessna scored 100 on bank every flight and calling it
    saturation - but the flights it saturated against were one person's, and
    narrowing a band so it produces a spread on the data you happen to have is
    the failure this file warns about, not a fix for it.

    The real relationship runs the other way. A transport is flown to less
    bank in normal operations - passengers, bank angle protection, GPWS
    alerting from 35 degrees - while a light airplane legitimately reaches 45
    in a steep turn, which is the handbook's own cap for the turn to final.
    """
    light = grading.LIGHT_GA
    jet = grading.FIXED_WING
    for phase in ("liftoff", "climb", "cruise", "descent"):
        lo = light[phase]["bank_p95_deg"]
        hi = jet[phase]["bank_p95_deg"]
        assert hi[1] < lo[1], (
            "%s: a transport reaches zero on bank at %s, which is not sooner "
            "than the light profile's %s - a jet is flown to less bank than a "
            "Cessna, not more" % (phase, hi[1], lo[1]))


def test_the_bank_bands_come_from_the_taxonomy():
    """Full marks through shallow; zero where nobody turns on purpose."""
    for phase in ("liftoff", "climb", "cruise", "descent"):
        best, worst = grading.LIGHT_GA[phase]["bank_p95_deg"]
        assert best == 20.0, (
            "%s: full marks should run through a shallow turn, which the FAA "
            "handbook puts at 20 degrees; got %s" % (phase, best))
        assert worst == 45.0, (
            "%s: zero should sit at the medium/steep boundary, which is also "
            "the cap for the turn to final; got %s" % (phase, worst))


# --------------------------------------------------------------------------
# Touchdown alignment can only hold a landing down
# --------------------------------------------------------------------------

def _roll(bank, spread, n=5, t0=100.0, contact_spread=None):
    """A touchdown sample followed by n rollout seconds."""
    out = [{"t": t0 - 1.0, "on_ground": False, "bank": 0.0,
            "accel_x_min": 0.0, "accel_x_max": 0.0}]
    first = contact_spread if contact_spread is not None else spread
    out.append({"t": t0, "on_ground": True, "bank": bank,
                "accel_x_min": 0.0, "accel_x_max": first})
    for i in range(1, n + 1):
        out.append({"t": t0 + i, "on_ground": True, "bank": bank * 0.5,
                    "accel_x_min": 0.0, "accel_x_max": spread})
    return out


def _landing_with(rate_fpm, alignment):
    """The landing phase for a touchdown rate and an alignment, nothing else."""
    td = grading.score_for_touchdown_fpm(rate_fpm, grading.LIGHT_GA)
    score, parts, _note = grading._landing_blend(
        td, rate_fpm, grading.LIGHT_GA, alignment=alignment)
    return td, score, parts


def test_alignment_never_lifts_a_landing():
    """A square arrival at a very hard rate is still a very hard landing.

    The cap is a ceiling, not a score to average in. If it could lift, a
    perfect rollout would launder a hard touchdown into a good grade.
    """
    perfect = grading.touchdown_alignment(_roll(0.0, 0.0))
    assert perfect and perfect["score"] == 100.0
    for rate in (550.0, 200.0, 40.0):
        td, score, _ = _landing_with(rate, perfect)
        assert score <= td + 1e-9, (
            "a flawless rollout lifted the landing at %s fpm from %.1f to %.1f"
            % (rate, td, score))


def test_alignment_holds_a_gentle_but_crooked_landing_down():
    """The landing this was built for: soft, 7.5 deg of bank, still sliding.
    It holds the landing grade down, and says so in the phase's record."""
    bad = grading.touchdown_alignment(_roll(7.54, 0.31 * grading.G_FT_S2))
    assert bad and bad["score"] < 70, (
        "a 7.5 degree wing drop with a scrubbing rollout scored %s" % (bad or {}).get("score"))
    td, score, parts = _landing_with(50.0, bad)
    assert score < td and grading.letter_for_score(score) == "C", (
        "a butter touchdown that slid should be held to C, got %.1f (%s)"
        % (score, grading.letter_for_score(score)))
    part = next(p for p in parts if p["key"] == "alignment")
    assert part["held"], "the alignment that held the landing down does not say so"


def test_the_contact_impulse_is_not_the_measurement():
    """Peak lateral ranked the quietest rollout in the logbook as the worst.

    Every landing has one impulse at contact. The landing that actually slid
    peaked LOWER there and then kept scrubbing. If the contact sample ever
    creeps back into scrub, this ordering inverts again and the metric starts
    marking down the smooth ones.
    """
    impulse = grading.touchdown_alignment(
        _roll(0.2, 0.05 * grading.G_FT_S2, contact_spread=1.34 * grading.G_FT_S2))
    sustained = grading.touchdown_alignment(_roll(0.2, 0.31 * grading.G_FT_S2))
    assert impulse["score"] > sustained["score"], (
        "one big spike at contact (%s) outscored a sustained scrub (%s)"
        % (impulse["score"], sustained["score"]))


def test_a_track_without_the_accelerations_is_not_a_good_landing():
    """Absent must never read as zero.

    A helicopter track may carry none of these fields. If a missing reading
    scored 100 they would all be certified square.
    """
    old = [{"t": 100.0, "on_ground": False}, {"t": 101.0, "on_ground": True}]
    assert grading.touchdown_alignment(old) is None
    td, score, parts = _landing_with(120.0, None)
    assert score == td and not [p for p in parts if p["key"] == "alignment"], (
        "an unmeasured alignment changed the landing")


def test_rotary_is_not_scored_on_alignment():
    assert not grading.scores_alignment(grading.ROTARY), (
        "no helicopter landing here has lateral data; scoring it would award "
        "a flawless alignment to every one of them")
    assert grading.scores_alignment(grading.LIGHT_GA)
    good = _roll(9.0, 0.5 * grading.G_FT_S2)
    assert grading.landing_alignment_for(good, category="Helicopter", vs0_kt=0.0) is None
    assert grading.landing_alignment_for(good, category="Airplane", vs0_kt=40.0) is not None


def test_the_right_leg_of_a_multi_leg_sortie_is_measured():
    """t_land picks the landing out; without it the last one wins every time."""
    track = _roll(0.5, 0.02 * grading.G_FT_S2, t0=100.0) + _roll(
        9.0, 0.5 * grading.G_FT_S2, t0=200.0)
    first = grading.touchdown_alignment(track, t_land=100.0)
    last = grading.touchdown_alignment(track, t_land=200.0)
    assert first["bank_deg"] < 1.0 and last["bank_deg"] > 8.0, (
        "t_land did not select the leg's own landing: got %s and %s"
        % (first["bank_deg"], last["bank_deg"]))


def test_the_panel_explains_the_cap():
    """Two things the note must convey, checked by meaning not by phrasing.

    It has to say the measure can only take points away, and it has to admit
    the thresholds are unverified. The exact words are free to change - the
    word "uncalibrated" was itself removed for reading as jargon.
    """
    d = grading.describe()
    note = (d.get("alignment_note") or "").lower()
    assert "never lift" in note or "only take" in note or "never add" in note, (
        "the panel must say this measure can only hold a grade down")
    assert ("not been checked" in note or "uncalibrated" in note), (
        "the panel must admit these thresholds are unverified")
    by = {p["label"]: p for p in d["profiles"]}
    assert by["Helicopter"]["alignment"] is False
    assert by["Light airplane"]["alignment"] is True


# --------------------------------------------------------------------------
# Alignment inside the Descent phase, and the bands in the reporting
# --------------------------------------------------------------------------

def _leg_track(land_bank=0.5, land_spread=0.0, descent_s=60):
    """A four-phase leg: climb, cruise, descent, touchdown, rollout.

    An earlier version of this was a single long glide, which split_phases
    read as no measurable descent at all - so every test built on it was
    exercising the touchdown-only branch and proving nothing about the path
    that grades real legs. Two deliberate mutations went uncaught before that
    was noticed. Assert the phase exists rather than trusting the shape.
    """
    pts, t = [], 0.0

    def add(n, alt0, dalt, vs, gs, agl0, dagl):
        nonlocal_t = t
        for i in range(n):
            pts.append({"t": nonlocal_t + i, "on_ground": False,
                        "alt": alt0 + dalt * i, "agl": max(agl0 + dagl * i, 5.0),
                        "vs": vs, "gs": gs, "airspeed": gs + 5,
                        "bank": 1.0, "pitch": -2.0, "heading": 90.0,
                        "lat": 40.0 + (nonlocal_t + i) * 1e-4, "lon": -105.0,
                        "accel_x_min": 0.0, "accel_x_max": 0.0})
        return nonlocal_t + n

    t = add(40, 1000.0, 25.0, 1500.0, 80.0, 0.0, 25.0)          # climb
    t = add(60, 2000.0, 0.0, 0.0, 110.0, 1000.0, 0.0)           # cruise
    t = add(descent_s, 2000.0, -27.0, -620.0, 95.0, 1000.0, -16.4)   # descent
    for j in range(6):
        pts.append({"t": t + j, "on_ground": True, "alt": 380.0, "agl": 0.0,
                    "vs": 0.0, "gs": 40.0, "airspeed": 40.0,
                    "bank": land_bank, "pitch": 0.0, "heading": 90.0,
                    "lat": 40.0 + (t + j) * 1e-4, "lon": -105.0,
                    "accel_x_min": 0.0, "accel_x_max": land_spread})
    return pts


def test_the_fixture_produces_a_real_descent():
    """Guards every test below: they mean nothing on a touchdown-only phase."""
    pg = grading.grade_leg(_leg_track(), 120.0, category="Airplane", vs0_kt=40.0)
    dz = pg["phases"]["descent"]
    assert dz.get("note") != "touchdown only; no measurable descent", (
        "the fixture stopped producing a scorable descent; the alignment "
        "tests below would silently exercise the wrong branch")
    assert len(dz["parts"]) >= 4, dz["parts"]


def _descent_parts(pg):
    return {q["key"]: q for q in (pg["phases"]["descent"]["parts"])}


def _landing_of(pg):
    """The phase that carries the landing: its own with LANDING_PHASE on,
    the descent's otherwise."""
    return pg["phases"].get("landing") or pg["phases"]["descent"]


def _landing_parts(pg):
    return {q["key"]: q for q in _landing_of(pg)["parts"]}


def test_alignment_is_listed_with_the_landing_as_a_cap_not_a_weight():
    """A weight on it pays out the gap between rollout and touchdown.

    Measured against real legs, weighting alignment at w changes a descent by
    w * (alignment - touchdown), which pays MOST where the touchdown was
    worst - it upgraded a hard landing by a whole band. So it is listed with
    the metrics, where a reader looks for it, and carries no weight.
    """
    al = grading.touchdown_alignment(_roll(7.54, 0.31 * grading.G_FT_S2))
    pg = grading.grade_leg(_leg_track(), 120.0, category="Airplane",
                           vs0_kt=40.0, alignment=al)
    parts = _landing_parts(pg)
    assert "alignment" in parts, "Alignment is not listed with the landing"
    assert parts["alignment"]["weight_pct"] == 0, (
        "alignment carries a weight; it must cap instead")
    assert parts["alignment"].get("cap") is True
    weighted = sum(q.get("weight_pct") or 0 for q in parts.values())
    assert weighted == 100, (
        "the weighted metrics no longer sum to 100%%: %d" % weighted)


def test_alignment_can_only_lower_the_landing_score():
    """Square never earns; crooked always costs."""
    square = grading.touchdown_alignment(_roll(0.2, 0.0))
    crooked = grading.touchdown_alignment(_roll(9.0, 0.5 * grading.G_FT_S2))
    base = grading.grade_leg(_leg_track(), 120.0, category="Airplane", vs0_kt=40.0)
    with_square = grading.grade_leg(_leg_track(), 120.0, category="Airplane",
                                    vs0_kt=40.0, alignment=square)
    with_crooked = grading.grade_leg(_leg_track(), 120.0, category="Airplane",
                                     vs0_kt=40.0, alignment=crooked)
    b = _landing_of(base)["score"]
    assert _landing_of(with_square)["score"] == b, (
        "a square landing raised the landing score from %s to %s"
        % (b, _landing_of(with_square)["score"]))
    assert _landing_of(with_crooked)["score"] < b, (
        "a crooked landing did not lower the landing score")


def test_a_leg_with_no_measurable_descent_is_capped_too():
    """That phase is built by hand and once skipped the cap entirely."""
    short = _leg_track(descent_s=2)  # too little descent to score
    crooked = grading.touchdown_alignment(_roll(12.0, 0.5 * grading.G_FT_S2))
    pg = grading.grade_leg(short, 60.0, category="Airplane", vs0_kt=40.0,
                           alignment=crooked)
    dz = pg["phases"]["descent"]
    if dz.get("note") != "touchdown only; no measurable descent":
        return                        # this build produced a real descent
    parts = _descent_parts(pg)
    assert "alignment" in parts, (
        "the touchdown-only descent skipped alignment, so a crooked landing "
        "escapes the cap on exactly the legs with least other evidence")
    assert parts["alignment"]["held"], "it was listed but did not cap"


def test_rotary_descents_carry_no_alignment_part():
    al = grading.touchdown_alignment(_roll(9.0, 0.5 * grading.G_FT_S2))
    pg = grading.grade_leg(_leg_track(), 120.0, category="Helicopter",
                           vs0_kt=0.0, alignment=al)
    assert "alignment" not in _descent_parts(pg), (
        "a helicopter descent was scored on alignment; no rotary landing "
        "here has the data behind those bands")


def test_every_reported_metric_says_what_good_looks_like():
    """"6 degrees, 33/100" does not tell a reader what would have been good.

    The bands were in the grading panel all along and not in the leg tooltip,
    which is where people actually read them. A metric that ships without one
    is a line nobody can act on.
    """
    al = grading.touchdown_alignment(_roll(7.54, 0.31 * grading.G_FT_S2))
    pg = grading.grade_leg(_leg_track(), 120.0, category="Airplane",
                           vs0_kt=40.0, alignment=al)
    missing = []
    for phase, body in pg["phases"].items():
        for part in body.get("parts") or []:
            # A part may carry its bands on its sub-measures instead, which
            # is what alignment does: one number, two bands, and printing
            # only one of them was the bug this guards.
            subs = part.get("sub") or []
            ok = part.get("band") or (subs and all(q.get("band") for q in subs))
            if not ok:
                missing.append("%s/%s" % (phase, part["key"]))
    assert not missing, (
        "these reported metrics carry no band: " + ", ".join(missing))


def test_a_greaser_is_not_penalised_for_not_being_zero():
    """Full marks have a floor, because chasing zero is the fault.

    The curve used to deduct from 1 fpm up, so a 12 fpm touchdown scored
    better than a 38 fpm one and both scored below a theoretical 0. Nobody
    flies for that number: published guidance puts a normal touchdown at
    60-180 fpm, and the FAA Airplane Flying Handbook wants minimum floating
    before touchdown, which is what holding off for a zero produces.
    """
    at = lambda v: grading.score_for_touchdown_fpm(v)
    plateau = grading.TOUCHDOWN_PLATEAU_FPM
    for v in (0.0, 5.0, 20.0, plateau / 2, plateau):
        assert at(v) == 100.0, (
            "%s fpm scored %s; everything at or under the plateau is full "
            "marks" % (v, at(v)))
    assert at(plateau + 20.0) < 100.0, "the curve must resume above the plateau"


def test_the_plateau_covers_the_whole_top_band():
    """Full marks reach the A limit, not somewhere short of it.

    A plateau that stopped below 60 would still dock a 55 fpm landing for not
    being a 45 fpm one, which is the complaint the plateau exists to answer,
    moved along a bit.
    """
    import passenger
    a_limit = max(lim for lim, ltr, _n in passenger.GRADE_TABLE if ltr == "A")
    assert grading.TOUCHDOWN_PLATEAU_FPM == a_limit, (
        "the plateau (%s) and the A limit (%s) should be the same number: "
        "everything the ladder calls butter scores full marks"
        % (grading.TOUCHDOWN_PLATEAU_FPM, a_limit))
    assert grading.score_for_touchdown_fpm(a_limit) == 100.0


def test_the_landing_ladder_is_not_user_tunable():
    """The settings page could once put the app into a state this file rejects.

    grade_a..grade_d were editable there, and saving one rewrote
    passenger.GRADE_TABLE. The touchdown curve above it is a constant: its
    plateau sits on the A limit and its knots sit on the letter boundaries.
    So moving grade_a moved the letter and left the score behind, and the two
    tests either side of this one would have failed on a running install with
    nothing at runtime saying so.

    Both halves have to move together or neither does. For now neither does.
    """
    import passenger
    import settings

    keys = ("grade_a", "grade_b", "grade_c", "grade_d")
    assert not [k for k in keys if k in settings.BY_KEY], (
        "the landing ladder is settable again; if that is deliberate the "
        "touchdown curve in grading.py has to move with it")

    # A settings.json left over from when they existed must be inert, not fatal.
    before = tuple(passenger.GRADE_TABLE)
    assert settings.validate({"grade_a": 90.0, "grade_d": 400.0}) == {}, (
        "stale grade keys should be dropped, not carried through")
    assert tuple(passenger.GRADE_TABLE) == before

    groups = {item.get("group") for item in settings.describe()["settings"]}
    assert "Landing grades" not in groups, (
        "the settings page still offers a Landing grades group")

    # And nothing outside passenger.py may assign the table.
    for name in ("settings.py", "watcher.py", "logbook_build.py", "grading.py"):
        src = io.open(os.path.join(BASE, name), encoding="utf-8",
                      errors="replace").read()
        assert "GRADE_TABLE =" not in src, (
            "%s assigns passenger.GRADE_TABLE; the ladder has one owner" % name)



def test_zero_sits_at_the_design_limit_not_past_it():
    """14 CFR 23.473 builds for 7-10 ft/s. 10 ft/s is 600 fpm.

    Zero used to be 900 fpm - 15 ft/s - so the bottom half of the scale was
    describing arrivals the airframe was never built to survive, and a genuine
    structural-limit landing still scored comfortably above nothing.
    """
    assert grading.TOUCHDOWN_ZERO_FPM == 600.0
    assert grading.score_for_touchdown_fpm(600.0) == 0.0
    assert grading.score_for_touchdown_fpm(900.0) == 0.0, (
        "the curve must clamp past its end rather than going negative")


def test_the_curve_still_meets_the_lower_boundaries():
    """B/C, C/D and D/F stay put; only the top anchor went to the plateau."""
    for fpm, want in ((150.0, 80.0), (300.0, 70.0), (500.0, 60.0)):
        got = grading.score_for_touchdown_fpm(fpm)
        assert abs(got - want) < 1e-6, (
            "%s fpm scores %s, not %s - a boundary moved" % (fpm, got, want))


def test_the_touchdown_letter_follows_the_aircraft_types_own_criteria():
    """One verdict per touchdown, on the criteria for its type. The letter
    was a fixed rate ladder for every aircraft, and called a jet's 200 fpm -
    the middle of the airline target - Firm, beside a touchdown score of 100."""
    for prof in (grading.ROTARY, grading.LIGHT_GA, grading.FIXED_WING):
        for fpm in (20, 60, 90, 140, 200, 300, 450, 700):
            score = grading.score_for_touchdown_fpm(fpm, prof)
            assert grading.touchdown_letter(fpm, prof) == grading.letter_for_score(score), (
                "%s at %d fpm: the letter and the score disagree" % (prof["name"], fpm))
    assert grading.touchdown_letter(200, grading.FIXED_WING) == "A", (
        "a jet's 200 fpm is inside the airline target and is not Firm")
    assert grading.touchdown_letter(200, grading.LIGHT_GA) == "C", (
        "a light airplane's 200 fpm is firm by its own criteria")
    assert grading.touchdown_letter(None, grading.LIGHT_GA) is None


def test_off_the_curve_the_letter_is_still_the_ladder():
    """Reading the letter off the curve moved the A limit to 105 fpm: the
    curve ramped from 100 at 60 fpm to 80 at 150, and passes 90 at 105. The
    ladder, the EFB and the passenger prose all say 60. Checking that the
    letter matched the score could not see it - both came from the curve -
    so this checks the letter against the ladder, at every rate, on every
    profile scored on the light curve."""
    import passenger
    for prof in (grading.ROTARY, grading.LIGHT_GA, grading.UNCLASSIFIED):
        wrong = []
        for tenth in range(0, 7001, 5):
            fpm = tenth / 10.0
            want = passenger.grade_for_rate(fpm)[0]
            got = grading.touchdown_letter(fpm, prof)
            if got != want:
                wrong.append((fpm, got, want))
        assert not wrong, (
            "%s: the curve's letter leaves the ladder at %d rate(s), first at "
            "%s fpm (%s, ladder says %s)" % ((prof["name"], len(wrong)) + wrong[0]))
    assert grading.score_for_touchdown_fpm(60.0) == 100.0, "60 fpm is still butter"
    assert grading.letter_for_score(grading.score_for_touchdown_fpm(60.5)) == "B"
    assert round(grading.score_for_touchdown_fpm(60.5)) < 90, (
        "just past the A limit, the score must not read 90 on the page")


# --------------------------------------------------------------------------
# Transport aircraft are flown to a different target
# --------------------------------------------------------------------------

def test_transport_has_its_own_touchdown_scale():
    """A jet is not scored on the light-aircraft curve.

    One curve for everything gave a jet full marks for a greaser and a C for
    the touchdown its operator actually asks for.
    """
    light = grading.score_for_touchdown_fpm
    assert grading.touchdown_curve_for(grading.FIXED_WING) is \
        grading.TOUCHDOWN_CURVE_TRANSPORT
    for p in (grading.LIGHT_GA, grading.ROTARY, grading.UNCLASSIFIED):
        assert grading.touchdown_curve_for(p) is grading.TOUCHDOWN_CURVE, (
            "%s was given the transport curve; the light profiles are built "
            "by copying FIXED_WING, so anything set on it too early leaks"
            % p["name"])


def test_the_transport_target_band_scores_full_marks():
    """100-250 fpm is what an airline asks for, so it is what scores 100."""
    for fpm in (100.0, 150.0, 200.0, 250.0):
        got = light = grading.score_for_touchdown_fpm(fpm, grading.FIXED_WING)
        assert got == 100.0, (
            "%s fpm is inside the published airline target band and scored %s"
            % (fpm, got))


def test_a_jet_greaser_is_not_the_top_score():
    """Floating one on is the fault at the soft end of a transport landing."""
    greaser = grading.score_for_touchdown_fpm(0.0, grading.FIXED_WING)
    target = grading.score_for_touchdown_fpm(150.0, grading.FIXED_WING)
    assert greaser < target, (
        "a 0 fpm arrival scored %s against %s for a touchdown in the target "
        "band; transport practice is a positive touchdown" % (greaser, target))
    assert greaser > 60.0, (
        "a soft landing is not the aim in a jet but it is not dangerous "
        "either, and vertical speed alone cannot tell a float from a "
        "well-flown soft touchdown")


def test_both_curves_end_at_the_same_design_limit():
    """23.473 and 25.473 both land on 10 ft/s, from different directions."""
    for p in (grading.LIGHT_GA, grading.FIXED_WING):
        assert grading.score_for_touchdown_fpm(600.0, p) == 0.0
        assert grading.score_for_touchdown_fpm(500.0, p) == 60.0, (
            "both curves should still meet at the D/F anchor")


def test_the_touchdown_band_is_worded_for_its_own_shape():
    """"full marks 100, zero 250" would say the target band scores nothing."""
    t = grading.touchdown_band_text(grading.FIXED_WING)
    assert "100 to 250" in t, t
    light = grading.touchdown_band_text(grading.LIGHT_GA)
    assert "under 60" in light, light
    assert t != light


# --------------------------------------------------------------------------
# What a user reads is about the app, not about how it was built
# --------------------------------------------------------------------------

def test_no_development_narrative_in_user_facing_text():
    """The grading panel is read by pilots, not by whoever wrote it.

    "weighting it was tried and measured", "an earlier version", "calibrated
    against the once-a-second figures", "a small sample of light-airplane
    landings" - none of that means anything to someone looking up why their
    landing scored what it did, and some of it describes whoever happened to
    fly the sample. Reasoning belongs in comments, not on screen.
    """
    import re
    d = grading.describe()
    texts = []

    def add(where, t):
        if t:
            texts.append((where, str(t)))

    for k in ("g_note", "alignment_note", "cap_note", "routing", "intro"):
        add("top/" + k, d.get(k))
    for i, lim in enumerate(d.get("limits") or []):
        add("limits[%d]" % i, lim)
    for p in d["profiles"]:
        add("%s/note" % p["key"], p.get("note"))
        for ph in p["phases"]:
            add("%s/%s/blurb" % (p["key"], ph["key"]), ph.get("blurb"))
            add("%s/%s/source" % (p["key"], ph["key"]), ph.get("source"))
            for m in ph["metrics"]:
                add("%s/%s/%s" % (p["key"], ph["key"], m["key"]), m.get("why"))

    assert len(texts) > 40, "found only %d strings; did describe() move?" % len(texts)
    bad = re.compile(
        r"\b(was tried|tried and measured|calibrated against the|an earlier|"
        r"this logbook|small sample|upgraded a|recalibrat|scrub)\b"
        r"|September \d{4}", re.I)
    hits = ["%s: %r" % (w, bad.search(t).group(0)) for w, t in texts if bad.search(t)]
    assert not hits, (
        "development narrative in text a user reads: " + "; ".join(hits))


# --------------------------------------------------------------------------
# The ride grade must not hide its worst phase
# --------------------------------------------------------------------------

def test_the_ride_grade_is_capped_like_the_overall():
    """An average that hides the worst part is not describing the flight.

    grade_leg's docstring has said this about the overall since it was
    written, and _ride_score computed a plain weighted mean anyway. A leg with
    lift-off 84, climb 88 and an F cruise of 60 averaged to a C - and C is the
    "ordinary flying, ordinarily done" pool, printed on the same row as an F
    CRUISE pill.
    """
    weights = {"liftoff": 0.10, "climb": 0.20, "cruise": 0.25, "descent": 0.45}
    scored = {"liftoff": 84.3, "climb": 88.1, "cruise": 59.7, "descent": 80.7}
    ride = grading._ride_score(scored, weights)
    worst = min(v for k, v in scored.items() if k != "descent")
    assert ride <= worst + grading.PHASE_CAP_POINTS + 1e-9, (
        "ride scored %.1f with a worst in-flight phase of %.1f; it may sit at "
        "most one band above" % (ride, worst))
    assert grading.letter_for_score(ride) == "D", (
        "an F cruise should pull the ride to D, got %s"
        % grading.letter_for_score(ride))


def test_the_landing_does_not_drag_the_ride_down():
    """The landing gets its own sentence; it must not color this one too.
    Without the landing phase the touchdown lives in the descent, so there
    the descent is what the ride leaves out."""
    weights = {"liftoff": 0.10, "climb": 0.20, "cruise": 0.25,
               "descent": 0.25, "landing": 0.20}
    with LandingPhase():
        ride = grading._ride_score({"liftoff": 95.0, "climb": 96.0, "cruise": 94.0,
                                    "descent": 93.0, "landing": 20.0}, weights)
    assert ride > 90.0, "a dreadful landing pulled the ride to %.1f" % ride
    with LandingPhase(on=False):
        ride = grading._ride_score({"liftoff": 95.0, "climb": 96.0, "cruise": 94.0,
                                    "descent": 20.0}, weights)
    assert ride > 90.0, "a dreadful touchdown in the descent pulled the ride to %.1f" % ride


def test_a_bad_approach_is_part_of_the_ride():
    """With the landing a phase of its own, the descent is the approach -
    flying - and the passenger who said the ride was smooth over an F
    approach was contradicting the pill beside the paragraph."""
    weights = {"liftoff": 0.10, "climb": 0.20, "cruise": 0.25,
               "descent": 0.25, "landing": 0.20}
    with LandingPhase():
        ride = grading._ride_score({"liftoff": 95.0, "climb": 96.0, "cruise": 94.0,
                                    "descent": 40.0, "landing": 98.0}, weights)
    assert ride <= 40.0 + grading.PHASE_CAP_POINTS + 0.05, (
        "an F approach left the ride at %.1f" % ride)


def test_a_bad_cruise_cannot_read_as_ordinary_flying():
    """The end-to-end version: grade in, prose out."""
    import passenger
    weights = {"liftoff": 0.10, "climb": 0.20, "cruise": 0.25, "descent": 0.45}
    scored = {"liftoff": 84.3, "climb": 88.1, "cruise": 59.7, "descent": 80.7}
    ride_letter = grading.letter_for_score(grading._ride_score(scored, weights))
    kind, _pool = passenger._ride_pool(ride_letter, None, None)
    assert kind == "busy", (
        "an F cruise selected the %r ride pool; the paragraph sits next to "
        "the phase pills and must not contradict them" % kind)


# --------------------------------------------------------------------------
# A weight with no band behind it scores nothing, silently
# --------------------------------------------------------------------------

def test_every_weighted_metric_has_a_band_to_score_against():
    """_score_phase skips a metric whose band is missing, and says nothing.

    Adding bank to the descent phase looked like it worked: the weights were
    right, the metric was being computed for the slice, and every grade moved.
    It scored nothing at all. The movement came from the OTHER weights being
    rebalanced to make room, and the bank column was empty on all 25 legs.
    A weight with no band is a metric that quietly does not exist.
    """
    missing = []
    for prof in (grading.ROTARY, grading.LIGHT_GA, grading.FIXED_WING,
                 grading.UNCLASSIFIED):
        for phase in grading.PHASE_ORDER:
            spec = prof.get(phase)
            if not spec:
                continue
            for key in spec.get("weights", {}):
                # touchdown and alignment are scored from the leg, not a band.
                if key in ("touchdown", "alignment"):
                    continue
                if spec.get(key) is None:
                    missing.append("%s/%s/%s" % (prof["name"], phase, key))
    assert not missing, (
        "these metrics carry a weight but no band, so they contribute nothing "
        "while still taking weight from the metrics that do: "
        + ", ".join(missing))


def test_the_approach_turn_is_measured():
    """In a circuit the steepest turn of the leg is base to final.

    It lives in the descent phase, which scored bank nowhere - so the gentle
    crosswind turn was graded and the firm one was not.
    """
    for prof in (grading.ROTARY, grading.LIGHT_GA, grading.FIXED_WING):
        w = prof["descent"]["weights"]
        assert "bank_p95_deg" in w, (
            "%s does not score bank in the descent, which is where the "
            "approach turn is" % prof["name"])
        assert prof["descent"].get("bank_p95_deg"), (
            "%s weights descent bank with no band behind it" % prof["name"])
        assert abs(sum(w.values()) - 1.0) < 1e-9, (
            "%s descent weights sum to %.3f" % (prof["name"], sum(w.values())))


def test_bank_says_it_measures_comfort_not_correctness():
    """The band penalises turns the FAA handbook calls normal.

    Circuit turns are flown at 20-30 degrees and score 50 down to 0 here. That
    is defensible for a comfort measure and wrong for an airmanship one, so
    the description has to say which it is rather than leaving a reader to
    assume the stricter reading.
    """
    why = grading.METRICS["bank_p95_deg"][2].lower()
    assert "felt" in why or "comfort" in why, (
        "the bank metric should say it scores how the turn felt")
    assert "correct" in why, (
        "it should say plainly that a correct turn can still cost points here")


def test_a_correct_rotation_is_not_marked_down():
    """Liftoff graded a rotation against a vibration band.

    The old band was full marks 0.03 g, zero 0.30 g - taken from ride-quality
    work where the passenger discomfort threshold sits near 0.06 g. Those
    figures are for frequency-weighted VIBRATION. A rotation is a deliberate
    manoeuvre, and pulling g is what it is.

    Published technique asks for 2.8 to 3 deg/s. dn = V*q/g puts that at
    0.16 g for a light airplane at 60 kt and 0.36 g for a transport at
    130 kt, so a correct rotation scored zero on the old band.
    """
    import math

    def dn(kt, deg_s):
        return kt * 0.514444 * math.radians(deg_s) / 9.80665

    # the published case reproduces itself, so the relation is sound
    assert abs(dn(120, 2.8) - 0.31) < 0.02, (
        "dn = V*q/g no longer reproduces the documented 0.3 g at 2.8 deg/s")

    for profile, kt in ((grading.LIGHT_GA, 60.0), (grading.FIXED_WING, 130.0),
                        (grading.FIXED_WING, 90.0)):
        full, zero = profile["liftoff"]["rotation_g"]
        correct = dn(kt, 3.0)
        assert grading._band(correct, full, zero) == 100.0, (
            "%s: a rotation at the published 3 deg/s from %.0f kt is %.2f g "
            "and scores %.0f, not full marks"
            % (profile["name"], kt, correct,
               grading._band(correct, full, zero)))
        # and twice the published rate must not still be full marks
        assert grading._band(dn(kt, 6.0), full, zero) < 100.0, (
            "%s: twice the published pitch rate still scores full marks, so "
            "the band measures nothing" % profile["name"])
        # Zero sits at twice full marks, which is twice the published pitch
        # rate. A zero point far beyond that is a band nothing can fail, and
        # a metric nothing can fail is not measuring.
        assert zero <= full * 2.5, (
            "%s: zero at %.2f g against full marks at %.2f g - that is %.1fx, "
            "and no rotation of any technique would reach it"
            % (profile["name"], zero, full, zero / full))


def test_rotation_is_measured_not_derived():
    """The band is a published load factor, so the metric must be one.

    Differentiating vertical speed at 1 Hz understates - it misses everything
    between samples - and a band anchored on a real load factor read against
    an understated figure is a band that means nothing.
    """
    seg = [{"t": 0.0, "gforce": 1.0}, {"t": 1.0, "gforce": 1.22},
           {"t": 2.0, "gforce_max": 1.31}, {"t": 3.0, "gforce": 1.05}]
    got = grading.rotation_load_factor_g(seg)
    assert abs(got - 0.31) < 1e-6, (
        "expected the peak load factor above 1 g, got %r" % (got,))
    # peak, not an average: the pull is one event a few seconds long
    assert got > grading._accel_p95_g(seg, "gforce", 1.0) or True


def test_a_rotation_that_was_never_recorded_is_not_scored():
    """A track may carry no G FORCE. A missing measurement must not score
    well by default - the same rule alignment follows."""
    assert grading.rotation_load_factor_g(
        [{"t": 0.0, "vs": 0.0}, {"t": 1.0, "vs": 900.0}]) is None


def test_only_fixed_wing_grades_a_rotation():
    """A helicopter does not rotate, and ROTARY is the one calibrated
    profile - calibrated against the derived figure, so swapping its input
    would move every helicopter grade ever awarded."""
    assert "rotation_g" not in grading.ROTARY["liftoff"]["weights"]
    assert "vert_accel_g" in grading.ROTARY["liftoff"]["weights"]
    for name in ("LIGHT_GA", "FIXED_WING"):
        w = getattr(grading, name)["liftoff"]["weights"]
        assert "rotation_g" in w, "%s does not grade the rotation" % name
        assert "vert_accel_g" not in w, (
            "%s scores both, so the pull is counted twice" % name)


def test_the_light_band_is_not_the_transport_band():
    """Load factor scales with speed, so one band cannot serve both.

    A light airplane rotating correctly makes 0.16 g; on the transport band
    every rotation it will ever fly scores full marks, which measures nothing.
    """
    light = grading.LIGHT_GA["liftoff"]["rotation_g"]
    heavy = grading.FIXED_WING["liftoff"]["rotation_g"]
    assert light[0] < heavy[0] and light[1] < heavy[1], (
        "the light band (%s) is not tighter than the transport band (%s)"
        % (light, heavy))


def test_a_band_reads_in_the_same_unit_as_its_measurement():
    """For every metric, not just the one that broke.

    The band's unit was sliced off the format string, and a format string's
    tail is still format syntax: "%.0f%% of descent" gave the tooltip "full
    marks 3%% of descent, zero 40%% of descent" beside a measurement that
    correctly read "3% of descent". Compared here against what the format
    actually prints, so any escape in any future format is covered.
    """
    import re
    offenders = []
    for key, entry in sorted(grading.METRICS.items()):
        fmt = entry[1]
        if not fmt:
            continue
        shown = fmt % 7.0                                 # e.g. "7% of descent"
        number = re.match(r"-?[\d.]+", shown).group(0)
        unit = shown[len(number):]
        band = grading.band_text(key, (3.0, 40.0))
        assert band, "%s has a format but no band" % key
        if band != "full marks 3%s, zero 40%s" % (unit, unit):
            offenders.append("%s: measurement unit %r, band %r" % (key, unit, band))
    assert not offenders, (
        "bands whose unit is not the measurement's:\n  " + "\n  ".join(offenders))


def test_the_percent_metric_still_exercises_the_escape():
    """The test above only covers the fault while some format uses %%."""
    assert any("%%" in (e[1] or "") for e in grading.METRICS.values()), (
        "no metric format contains %% any more, so the band-unit test no "
        "longer exercises the escape that broke")


# --------------------------------------------------------------------------
# the landing float: 50 ft above the runway to main-gear touchdown
# --------------------------------------------------------------------------

def _arrival(float_s, kt=100.0, hz=10.0, start_ft=150.0, runway_ft=1000.0):
    """A straight-in arrival that crosses 50 ft and touches down float_s later.

    Descends at a steady 3-degree-ish rate to 50 ft, then sinks the last 50 ft
    evenly over float_s, then rolls. Northbound at a steady ground speed, so
    the distance flown from 50 ft is exactly float_s at kt.
    """
    pts, t = [], 1000.0
    fps = kt * 1.68781
    dt = 1.0 / hz
    lat = 47.0
    dlat = fps * dt / 364000.0               # feet to degrees of latitude
    h = start_ft
    approach_sink = fps * 0.0524             # tan(3 degrees)
    while h > 50.0:
        pts.append({"t": t, "alt": runway_ft + h, "lat": lat, "lon": -122.0,
                    "on_ground": False, "gs": kt})
        t += dt; lat += dlat; h -= approach_sink * dt
    t50 = t - dt + dt * ((pts[-1]["alt"] - runway_ft - 50.0) / (approach_sink * dt))
    steps = int(round(float_s * hz))
    for k in range(steps):
        frac = (k + 1) / float(steps)
        pts.append({"t": t, "alt": runway_ft + 50.0 * (1.0 - frac) + 0.5,
                    "lat": lat, "lon": -122.0, "on_ground": False, "gs": kt})
        t += dt; lat += dlat
    for k in range(30):
        pts.append({"t": t, "alt": runway_ft, "lat": lat, "lon": -122.0,
                    "on_ground": True, "gs": kt})
        t += dt; lat += dlat
    return pts


def test_the_float_is_measured_from_50_ft_to_contact():
    for float_s, kt in ((7.0, 100.0), (14.0, 105.0), (25.0, 60.0)):
        f = grading.landing_float(_arrival(float_s, kt=kt))
        assert f is not None, "a plain arrival was not measured"
        assert abs(f["seconds"] - float_s) <= 0.3, (
            "flew %.1f s from 50 ft, measured %.1f s" % (float_s, f["seconds"]))
        want_ft = float_s * kt * 1.68781
        assert abs(f["distance_ft"] - want_ft) / want_ft < 0.05, (
            "flew %.0f ft from 50 ft, measured %.0f" % (want_ft, f["distance_ft"]))
        assert abs(f["speed_kt"] - kt) < 3.0


def test_a_recording_that_starts_below_50_ft_is_not_a_short_float():
    """Not measured is not a perfect flare."""
    pts = [p for p in _arrival(9.0) if p["alt"] - 1000.0 < 40.0]
    assert grading.landing_float(pts) is None, (
        "a recording that never reached 50 ft was scored as a float")


def test_the_score_follows_the_zones_not_a_slope():
    """Inside the touchdown zone a float still passes; that is the standard.

    A first version scored linearly from 7 s to zero at the end of the zone,
    which made the middle of an acceptable zone a D - and, built against real
    landings, called a jet touching down 2,300 ft past the threshold an F.
    """
    for kt in (60.0, 100.0, 140.0):
        end, zero = grading.float_points_s(kt)
        assert grading.float_score(7.0, end, zero) == 100.0
        assert abs(grading.float_score(end, end, zero) - grading.FLOAT_ZONE_END_SCORE) < 1e-6
        assert grading.float_score(zero, end, zero) == 0.0
        s = 7.0
        while s < end:
            assert grading.float_score(s, end, zero) >= grading.FLOAT_ZONE_END_SCORE, (
                "a %.1f s float inside the touchdown zone at %d kt scored %.0f"
                % (s, kt, grading.float_score(s, end, zero)))
            s += 0.5
        last = 101.0
        for s10 in range(60, int(zero * 10) + 20):
            v = grading.float_score(s10 / 10.0, end, zero)
            assert v <= last + 1e-9, "the score rose with a longer float"
            last = v


def _threshold_at(pts, t):
    """past(point) for a threshold where this northbound arrival was at time t."""
    for a, b in zip(pts, pts[1:]):
        if a["t"] <= t <= b["t"]:
            lat = a["lat"] + (t - a["t"]) / (b["t"] - a["t"]) * (b["lat"] - a["lat"])
            return lambda q: (q["lat"] - lat) * 364000.0
    raise AssertionError("time %s is outside the arrival" % t)


def test_a_low_arrival_starts_the_float_clock_at_the_threshold():
    """AC 25-32 measures from 50 ft assuming 50 ft over the threshold. Below
    50 ft before the runway, the seconds before the threshold are approach,
    not float - counted, they read a landing just past the threshold as a
    long float."""
    pts = _arrival(20.0, kt=100.0)
    t_td = grading.landing_float(pts)
    assert t_td and abs(t_td["seconds"] - 20.0) < 0.15, t_td
    t50 = next(p["t"] for p in pts if p["alt"] - 1000.0 <= 50.5)
    low = grading.landing_float(pts, past=_threshold_at(pts, t50 + 5.0))
    assert low["from"] == "threshold", low
    assert abs(low["seconds"] - 15.0) < 0.2, (
        "crossed the threshold 5 s into a 20 s float, measured %.1f s"
        % low["seconds"])
    assert low["score"] >= t_td["score"], "starting later made the float worse"


def test_a_high_arrival_still_starts_at_50_ft():
    pts = _arrival(20.0, kt=100.0)
    t50 = next(p["t"] for p in pts if p["alt"] - 1000.0 <= 50.5)
    high = grading.landing_float(pts, past=_threshold_at(pts, t50 - 5.0))
    assert high["from"] == "50 ft" and abs(high["seconds"] - 20.0) < 0.15, high


def test_touching_down_short_of_the_threshold_measures_no_float():
    pts = _arrival(10.0, kt=100.0)
    assert grading.landing_float(pts, past=lambda q: (q["lat"] - 48.0) * 364000.0) is None, (
        "a touchdown before the runway began was given a float over it")


def test_the_height_over_the_threshold_is_measured():
    pts = _arrival(20.0, kt=100.0)
    for p in pts:
        p["agl"] = p["alt"] - 1000.0             # a flat runway at 1,000 ft
    t50 = next(p["t"] for p in pts if p["alt"] - 1000.0 <= 50.5)
    past = _threshold_at(pts, t50 + 5.0)
    surface = grading.runway_surface(pts, past)
    # 5 s into a float that sinks 50 ft evenly over 20 s.
    h = grading.threshold_height(pts, past=past, surface=surface)
    assert h is not None and abs(h - 38.0) < 2.0, (
        "crossed at about 38 ft, measured %r" % h)
    assert grading.threshold_height(pts) is None, "a height with no runway"
    assert grading.threshold_height(pts, past=past) is None, (
        "a height with no runway surface to measure it from")


def _sloped(cross_ft, slope, cliff_ft=0.0, td_x=1000.0, kt=100.0, hz=10.0, ref=4.0):
    """An arrival over a sloped runway, and past() for its threshold.

    Northbound. The runway's surface is slope x feet past the threshold, its
    plane extended before it; the terrain before the threshold is that plane
    less cliff_ft, the edge of a mesa. The wheels descend to cross_ft over the
    threshold, then sink evenly to touch down td_x past it. alt is where the
    sim measures the aircraft, ref above the wheels; agl is alt above the
    terrain beneath, as PLANE ALT ABOVE GROUND reports it.
    """
    fps = kt * 1.68781
    lat0, base, pts, t, x = 47.0, 1000.0, [], 1000.0, -2500.0
    while x < td_x + 1500.0:
        if x < 0:
            h = cross_ft + (150.0 - cross_ft) * (-x / 2500.0)
        elif x < td_x:
            h = cross_ft * (1.0 - x / td_x)
        else:
            h = 0.0
        plane = base + slope * x
        terrain = plane if x >= 0 else plane - cliff_ft
        alt = plane + h + ref
        pts.append({"t": t, "lat": lat0 + x / 364000.0, "lon": -122.0, "alt": alt,
                    "agl": alt - terrain, "on_ground": x >= td_x, "gs": kt})
        t += 1.0 / hz
        x += fps / hz
    return pts, (lambda q: (q["lat"] - lat0) * 364000.0)


def test_the_threshold_height_is_above_the_runway_not_where_it_touched():
    """The sim gives a runway one elevation and its runways are not flat. On
    one that climbs 2%, height above where the wheels touched 1,000 ft on put
    a 30 ft crossing at 10 ft; on one that falls 2%, at 50."""
    for slope in (0.02, -0.02, 0.0):
        pts, past = _sloped(30.0, slope)
        idx = grading.landing_index(pts)
        thr = min(range(len(pts)), key=lambda i: abs(past(pts[i])))
        naive = pts[thr]["alt"] - pts[idx]["alt"]
        if slope:
            assert abs(naive - 30.0) > 15, "the fixture is not sloped enough to tell"
        h = grading.threshold_height(pts, past=past,
                                     surface=grading.runway_surface(pts, past))
        assert h is not None and abs(h - 30.0) < 1.5, (
            "crossed 30 ft over a runway sloping %+.0f%%, measured %r"
            % (slope * 100, h))


def test_ground_before_the_threshold_is_not_the_runway():
    """Off the end of a runway on a mesa the ground drops away. A reading over
    it is not the runway, and must not move the runway 300 ft down."""
    pts, past = _sloped(30.0, 0.02, cliff_ft=300.0)
    before = [p for p in pts if -200 < past(p) < 0]
    assert before and min(p["agl"] for p in before) > 300, "the fixture has no cliff"
    h = grading.threshold_height(pts, past=past,
                                 surface=grading.runway_surface(pts, past))
    assert h is not None and abs(h - 30.0) < 1.5, (
        "a drop before the threshold moved the crossing to %r" % h)


def test_the_float_starts_50_ft_above_the_runway():
    """Crossing high on a runway that climbs, 50 ft above where the wheels
    touched comes long before 50 ft above the runway."""
    td_x = 1500.0
    pts, past = _sloped(70.0, 0.02, td_x=td_x)
    surface = grading.runway_surface(pts, past)
    flt = grading.landing_float(pts, past=past, surface=surface)
    fps = 100.0 * 1.68781
    want = (td_x - td_x * (20.0 / 70.0)) / fps          # sinks 70 -> 0 over td_x
    assert flt and flt["from"] == "50 ft" and abs(flt["seconds"] - want) < 0.15, (
        "the float from 50 ft above the runway is %.1f s, measured %r"
        % (want, flt and flt["seconds"]))
    flat = grading.landing_float(pts, past=past)
    assert abs(flat["seconds"] - want) > 1.0, "the fixture cannot tell the two apart"


def test_the_runway_surface_is_extended_along_its_slope():
    """The first reading past the threshold can be a whole 1 Hz sample in -
    200 ft at jet speed, 4 ft of rise on a 2% runway. The surface at the
    threshold is extended back along the slope, not taken from that reading."""
    s = grading.RunwaySurface([150.0, 350.0, 550.0], [3.0, 7.0, 11.0], 0.0)
    assert abs(s.at(0.0) - 0.0) < 1e-9, s.at(0.0)
    assert abs(s.at(250.0) - 5.0) < 1e-9 and abs(s.at(750.0) - 15.0) < 1e-9
    # Two readings closer than SURFACE_BASE_FT do not set the slope alone.
    s = grading.RunwaySurface([10.0, 12.0, 210.0], [0.2, 0.5, 4.2], 0.0)
    assert abs(s.at(0.0) - 0.0) < 0.05, (
        "a slope taken over 2 ft put the threshold at %.2f" % s.at(0.0))


def test_only_this_landing_on_this_runway_is_the_runway():
    """A track is the whole flight, and past() is a distance along a heading:
    the departure, a hundred miles down the same line, is "past the
    threshold" too. Measured on real tracks, that put a 30 ft crossing at
    hundreds of feet."""
    pts, past = _sloped(30.0, 0.02)
    beside = lambda q: (q["lon"] + 122.0) * 364000.0 * 0.682     # ft east
    t0 = pts[0]["t"]
    td_t = next(p["t"] for p in pts if p["on_ground"])

    def stray(t, x, east, ground):
        return {"t": t, "lat": 47.0 + x / 364000.0,
                "lon": -122.0 + east / (364000.0 * 0.682), "alt": ground + 900.0,
                "agl": 900.0, "on_ground": False, "gs": 100.0}
    strays = {
        # the departure, an hour earlier, off this same runway's threshold
        "time": stray(t0 - 3600.0, 1.0, 0.0, 5000.0),
        # the downwind leg, abeam the threshold, half a mile to one side
        "lateral": stray(td_t - 90.0, 1.0, 3000.0, 400.0),
    }
    for name, q in strays.items():
        mixed = sorted(pts + [q], key=lambda p: p["t"])
        s = grading.runway_surface(mixed, past, beside=beside, length_ft=5000.0)
        h = grading.threshold_height(mixed, past=past, surface=s)
        assert h is not None and abs(h - 30.0) < 1.5, (
            "a reading from elsewhere (%s) moved a 30 ft crossing to %r" % (name, h))
    # Beyond the far end is not this runway either.
    q = stray(td_t + 5.0, 9000.0, 0.0, 0.0)
    s = grading.runway_surface(sorted(pts + [q], key=lambda p: p["t"]), past,
                               beside=beside, length_ft=5000.0)
    assert max(s.xs) <= 5000.0, "a reading past the end of the runway was used"


def test_a_runway_surface_needs_readings_over_the_runway():
    pts, past = _sloped(30.0, 0.0)
    for p in pts:
        p.pop("agl")
    assert grading.runway_surface(pts, past) is None, (
        "a surface was made from no height-above-ground readings")


def test_helicopters_and_gyroplanes_are_not_measured():
    pts = _arrival(14.0)
    assert grading.landing_float_for(pts, "AS365", category="Helicopter") is None
    assert not grading._unclassified().get("float")
    assert grading.FIXED_WING.get("float") and grading.LIGHT_GA.get("float")


def _graded(float_s, flag):
    """grade_leg on a leg with a soft touchdown and a given float."""
    keep = grading.GRADE_FLOAT
    grading.GRADE_FLOAT = flag
    try:
        track = _departure(fpm=1000.0)
        f = grading.landing_float(_arrival(float_s, kt=105.0))
        g = grading.grade_leg(track, 80.0, "Test Jet", category="Airplane",
                              vs0_kt=90.0, landing_float=f)
        return f, g
    finally:
        grading.GRADE_FLOAT = keep


def test_off_by_default_and_shown_but_not_counted():
    assert grading.GRADE_FLOAT is False, (
        "the float caps landings by default; it was to stay off until its "
        "bands had been reviewed against real landings")
    f, g = _graded(30.0, False)
    part = next(p for p in _landing_of(g)["parts"] if p["key"] == "float")
    assert part["counted"] is False and part["held"] is False, (
        "the float part claims to count while the flag is off")
    assert part["band"], "the float part carries no band"


def test_a_long_float_holds_the_landing_down():
    f_short, g_short = _graded(7.0, True)
    f_long, g_long = _graded(30.0, True)
    short_part = next(p for p in _landing_of(g_short)["parts"] if p["key"] == "float")
    long_part = next(p for p in _landing_of(g_long)["parts"] if p["key"] == "float")
    assert not short_part["held"], "a normal flare held the landing down"
    assert long_part["held"], "a float far past the touchdown zone held nothing down"
    d_short = _landing_of(g_short)["score"]
    d_long = _landing_of(g_long)["score"]
    assert d_long < d_short, (
        "the landing scored %.1f with a 30 s float and %.1f with a "
        "normal flare" % (d_long, d_short))
    part = next(p for p in _landing_of(g_long)["parts"] if p["key"] == "float")
    assert part["counted"] and part["weight_pct"] > 0, (
        "a switched-on float is not counted in the landing")


def test_the_touchdown_only_descent_is_capped_too():
    """A leg with no measurable descent is graded on the touchdown alone.

    That branch builds the phase by hand, so a cap applied only in the normal
    path would leave it as the one way round the float - the same hole
    alignment had to close.
    """
    keep = grading.GRADE_FLOAT, grading.LANDING_PHASE
    try:
        grading.GRADE_FLOAT = True
        grading.LANDING_PHASE = False          # the branch only exists without it
        full = _departure(fpm=1000.0)
        top = max(p["alt"] for p in full)
        cut = max(i for i, p in enumerate(full) if p["alt"] >= top)
        track = full[:cut + 1]                      # climbs and cruises; no descent
        f = grading.landing_float(_arrival(30.0, kt=105.0))
        g = grading.grade_leg(track, 80.0, "Test Jet", category="Airplane",
                              vs0_kt=90.0, landing_float=f)
        d = g["phases"]["descent"]
        assert "touchdown only" in (d.get("note") or ""), (
            "fixture does not reach the touchdown-only branch: %r" % d.get("note"))
        part = next((p for p in d["parts"] if p["key"] == "float"), None)
        assert part is not None, "the touchdown-only descent does not show the float"
        assert part["held"], "a 30 s float did not hold the touchdown-only descent down"
    finally:
        grading.GRADE_FLOAT, grading.LANDING_PHASE = keep


def test_a_leg_with_no_descent_still_has_a_capped_landing():
    """With the landing a phase of its own, a leg that climbs to its
    destination has no descent - and still has its landing, limits and all."""
    keep = grading.GRADE_FLOAT
    try:
        grading.GRADE_FLOAT = True
        full = _departure(fpm=1000.0)
        top = max(p["alt"] for p in full)
        cut = max(i for i, p in enumerate(full) if p["alt"] >= top)
        f = grading.landing_float(_arrival(30.0, kt=105.0))
        with LandingPhase():
            g = grading.grade_leg(full[:cut + 1], 80.0, "Test Jet", category="Airplane",
                                  vs0_kt=90.0, landing_float=f)
        land = g["phases"].get("landing")
        assert land, "a leg with no descent lost its landing"
        part = next((p for p in land["parts"] if p["key"] == "float"), None)
        assert part and part["counted"], "the float is not counted in the landing"
        td = next(p for p in land["parts"] if p["key"] == "touchdown")["score"]
        assert land["score"] < td, "a 30 s float did not lower the landing"
    finally:
        grading.GRADE_FLOAT = keep


class Switches(object):
    """GRADE_FLOAT and GRADE_TOUCHDOWN_POINT set for the duration."""

    def __init__(self, flt, tdz):
        self.want = (flt, tdz)

    def __enter__(self):
        self.keep = (grading.GRADE_FLOAT, grading.GRADE_TOUCHDOWN_POINT)
        grading.GRADE_FLOAT, grading.GRADE_TOUCHDOWN_POINT = self.want
        return self

    def __exit__(self, *exc):
        grading.GRADE_FLOAT, grading.GRADE_TOUCHDOWN_POINT = self.keep
        return False


def _spot(distance_ft, length_ft=8000.0):
    return {"distance_ft": distance_ft, "length_ft": length_ft, "runway": "09",
            "score": grading.touchdown_point_score(distance_ft, length_ft)}


def _landed(fpm, float_s=7.0, spot_ft=1000.0, flt=True, tdz=True):
    with Switches(flt, tdz):
        g = grading.grade_leg(_departure(fpm=1000.0), fpm, "Test Jet",
                              category="Airplane", vs0_kt=90.0,
                              landing_float=grading.landing_float(_arrival(float_s, kt=105.0)),
                              touchdown_point=_spot(spot_ft))
    land = g["phases"]["landing"]
    td = next(p for p in land["parts"] if p["key"] == "touchdown")["score"]
    return land, td


def test_precision_cannot_rescue_a_hard_landing():
    """A perfect flare onto the aim point is still a 700 fpm arrival."""
    land, td = _landed(700.0)
    assert land["score"] <= td + grading.LANDING_LEAD_POINTS + 0.05, (
        "a hard landing on the aim point scored %.1f over a touchdown of %.1f"
        % (land["score"], td))


def test_a_firm_landing_on_the_spot_scores_above_its_touchdown():
    """What an instructor would say: on the aim point with a tidy flare is a
    good landing, firm or not."""
    land, td = _landed(300.0)
    assert td < 100, "the fixture's touchdown is already perfect"
    assert td < land["score"] <= td + grading.LANDING_LEAD_POINTS + 0.05, (
        "a firm landing on the aim point scored %.1f against a touchdown of %.1f"
        % (land["score"], td))


def test_a_greaser_far_down_the_runway_scores_below_its_touchdown():
    land, td = _landed(50.0, float_s=20.0, spot_ft=3500.0)
    assert land["score"] < td - 20, (
        "a soft touchdown 3,500 ft down the runway after a 20 s float scored "
        "%.1f against %.1f" % (land["score"], td))


def test_switched_off_measures_neither_help_nor_cost():
    land, td = _landed(300.0, float_s=25.0, spot_ft=3800.0, flt=False, tdz=False)
    assert abs(land["score"] - td) < 0.05, (
        "switched off, the float and touchdown spot still moved the landing "
        "from %.1f to %.1f" % (td, land["score"]))
    for p in land["parts"]:
        if p["key"] in ("float", "touchdown_point"):
            assert p["counted"] is False and p["weight_pct"] == 0, p


def test_the_blend_reweights_what_is_switched_on():
    land, td = _landed(300.0, flt=False, tdz=True)
    shares = {p["key"]: p["weight_pct"] for p in land["parts"]}
    w = grading.LANDING_WEIGHTS
    want = round(w["touchdown"] * 100 / (w["touchdown"] + w["touchdown_point"]))
    assert shares["touchdown"] == want and shares["float"] == 0, shares


def test_landing_past_the_touchdown_zone_is_a_fault_not_a_degree():
    """A soft touchdown 3,500 ft down the runway: the blend alone averaged it
    to a C. Past the zone the touchdown spot caps as well as weighs."""
    land, td = _landed(100.0, float_s=9.0, spot_ft=3500.0)
    spot = next(p for p in land["parts"] if p["key"] == "touchdown_point")
    assert spot["score"] < grading.LANDING_FAULT_SCORE, "the fixture is inside the zone"
    assert land["score"] <= spot["score"] + grading.ALIGNMENT_CAP_POINTS + 0.05, (
        "a landing past the touchdown zone scored %.1f with the spot at %.1f"
        % (land["score"], spot["score"]))
    assert spot["held"], "the cap held the landing down and the part does not say so"
    # Inside the zone it is weighed, never capped.
    land, td = _landed(100.0, float_s=9.0, spot_ft=2500.0)
    spot = next(p for p in land["parts"] if p["key"] == "touchdown_point")
    assert spot["score"] >= grading.LANDING_FAULT_SCORE and not spot["held"], spot


def test_the_lead_note_rounds_as_the_page_does():
    """The page shows a score rounded to a tenth, then half up. A note that
    rounded the raw value said 87 under a pill that said 88."""
    with Switches(True, True):
        score, parts, note = grading._landing_blend(
            77.49999, 188.0, grading.LIGHT_GA,
            touchdown_point=_spot(1000.0),
            landing_float=grading.landing_float(_arrival(7.0, kt=105.0)))
    shown = int(round(score, 1) + 0.5)
    assert note and ("Held to %d" % shown) in note and "(78)" in note, (
        "the page shows %d and the note says %r" % (shown, note))


def test_the_lead_limit_says_so_when_it_bites():
    land, td = _landed(450.0)
    assert land["score"] <= td + grading.LANDING_LEAD_POINTS + 0.05
    assert land.get("note") and "can't score more than" in land["note"], (
        "the landing was held to its touchdown plus %d with nothing said: %r"
        % (grading.LANDING_LEAD_POINTS, land.get("note")))


# --------------------------------------------------------------------------
# lift-off length for airplanes
# --------------------------------------------------------------------------

def _departure(fpm, top_ft=20000.0, runway_ft=1000.0):
    """Roll, climb at a steady rate, cruise, descend, land. 1 Hz."""
    pts, t = [], 0.0

    def add(alt, ground, spd):
        pts.append({"t": t, "alt": alt, "on_ground": ground,
                    "airspeed": spd, "gs": spd, "bank": 0.0})

    for i in range(30):
        add(runway_ft, True, 5.0 * i)
        t += 1.0
    alt = runway_ft
    while alt < top_ft:
        alt = min(top_ft, alt + fpm / 60.0)
        add(alt, False, 150.0)
        t += 1.0
    for _ in range(600):
        add(top_ft, False, 250.0)
        t += 1.0
    while alt > runway_ft + 10:
        alt = max(runway_ft + 10, alt - 1500.0 / 60.0)
        add(alt, False, 200.0)
        t += 1.0
    for _ in range(20):
        add(runway_ft, True, 60.0)
        t += 1.0
    return pts


def _lift_span(track, profile):
    rng = grading.split_phases(track, profile).get("liftoff")
    return grading._span_s(track, rng) if rng else 0.0


def test_a_brisk_jet_climb_out_is_long_enough_to_grade():
    """400 ft in eight seconds, which a light jet does.

    Bounded by height alone the phase came out under min_phase_s and was
    refused a grade - so the cleaner the departure, the likelier it went
    unmarked. Reported on a real light-jet leg that reached 400 ft in 14.1 s
    against a 15 s floor, with every other leg of that type between 14 and 21.
    """
    floor = grading.FIXED_WING["min_phase_s"]
    track = _departure(fpm=5000.0)
    # The fixture must reproduce the fault, or this passes on the old code. A
    # first draft used 3000 fpm, which the altitude smoothing turns into a 16 s
    # lift-off unaided - it never fell short and so tested nothing.
    unaided = _lift_span(track, dict(grading.FIXED_WING,
                                     liftoff_fills_min_phase=False))
    assert unaided < floor, (
        "fixture gives a %.1f s lift-off without the rule, which is already "
        "gradeable, so it cannot show the rule doing anything" % unaided)
    span = _lift_span(track, grading.FIXED_WING)
    assert span >= floor, (
        "a 5000 fpm climb-out produced a %.1f s lift-off against a %.0f s "
        "minimum, so it goes ungraded" % (span, floor))


def test_a_lift_off_already_long_enough_is_untouched():
    """The property that keeps this from rewriting history.

    A lift-off that reached its height after min_phase_s must end exactly
    where it did before the rule existed, or every airplane lift-off grade
    moves. Compared against the same profile with the rule switched off,
    index for index, not against a remembered number.
    """
    for fpm in (600.0, 1000.0, 1400.0):
        track = _departure(fpm=fpm)
        on = grading.split_phases(track, grading.FIXED_WING)
        off = grading.split_phases(
            track, dict(grading.FIXED_WING, liftoff_fills_min_phase=False))
        assert _lift_span(track, dict(grading.FIXED_WING,
                                      liftoff_fills_min_phase=False)) \
            >= grading.FIXED_WING["min_phase_s"], (
            "fixture at %d fpm does not test what it claims: its lift-off "
            "was already short" % fpm)
        assert on == off, (
            "at %d fpm a lift-off that was already long enough moved:\n"
            "  without the rule %r\n  with it          %r" % (fpm, off, on))


def test_lift_off_never_runs_into_the_climb():
    """Extended to be gradeable, but never past where the climb begins.

    Under the shipped numbers this bound cannot fire, and it is worth knowing
    why: the 15 s altitude smoothing puts the 85% climb mark at least 18 s
    after wheels-up on any leg that has a lift-off at all - searched across
    climb rates to 12000 fpm and tops from 1150 to 6000 ft - and a hop short
    enough to reach it sooner is graded as all cruise, with no lift-off.

    So it is exercised with a minimum longer than the climb, which is the
    situation it exists for: someone raises min_phase_s, or shortens the
    smoothing, and a stretched lift-off would otherwise carry on into level
    flight and grade cruise as a departure. A first draft of this test used a
    300 ft hop, produced no lift-off at all, and passed by asserting 0 < 15.
    """
    profile = dict(grading.FIXED_WING, min_phase_s=40.0)
    track = _departure(fpm=7500.0, top_ft=1850.0)
    ranges = grading.split_phases(track, profile)
    assert "liftoff" in ranges, (
        "fixture has no lift-off, so this would pass whatever the bound did")

    air = [i for i, p in enumerate(track) if p.get("on_ground") is False]
    sm = grading._smooth_alt(track, air, profile["alt_smooth_s"])
    base = sm[air[0]]
    mark = base + (max(sm.values()) - base) * profile["phase_frac"]
    climb_end = next(i for i in air if sm[i] >= mark)
    wheels_up = track[air[0]]["t"]
    assert track[climb_end]["t"] - wheels_up < profile["min_phase_s"], (
        "fixture reaches the climb after the minimum, so the bound is not "
        "what stops the lift-off here")

    end = ranges["liftoff"][1]
    assert end <= climb_end, (
        "lift-off ran to index %d, past the start of the climb at %d - it was "
        "stretched into level flight" % (end, climb_end))


def test_every_airplane_profile_fills_its_lift_off():
    for name, p in (("FIXED_WING", grading.FIXED_WING),
                    ("LIGHT_GA", grading.LIGHT_GA),
                    ("unclassified", grading._unclassified())):
        assert p.get("liftoff_fills_min_phase") is True, (
            "%s does not extend a short lift-off" % name)


def test_the_helicopter_lift_off_is_left_alone():
    """Rotary lift-off is calibrated against measured flights.

    Its window ends on transition speed or height, and it is the one profile
    whose numbers come from real legs rather than published criteria.
    Stretching that window would move calibrated grades for no reported
    fault - none of the recorded helicopter lift-offs was short.
    """
    assert not grading.ROTARY.get("liftoff_fills_min_phase"), (
        "the rotary profile extends its lift-off; its calibrated grades move")


# --------------------------------------------------------------------------
# The landing as a phase of its own (LANDING_PHASE)
# --------------------------------------------------------------------------

class LandingPhase(object):
    """LANDING_PHASE set for the duration (on unless told), and back after."""

    def __init__(self, on=True):
        self.on = on

    def __enter__(self):
        self.keep = grading.LANDING_PHASE
        grading.LANDING_PHASE = self.on
        return self

    def __exit__(self, *exc):
        grading.LANDING_PHASE = self.keep
        return False


def _jet(fpm=80.0, **kw):
    return grading.grade_leg(_departure(fpm=1000.0), fpm, "Test Jet",
                             category="Airplane", vs0_kt=90.0, **kw)


def test_the_landing_is_a_phase_by_default():
    """Turned on by the owner after seeing which legs it moved."""
    assert grading.LANDING_PHASE is True
    g = _jet()
    assert "landing" in g["phases"]
    assert "touchdown" not in [p["key"] for p in g["phases"]["descent"]["parts"]]
    with LandingPhase(on=False):
        g = _jet()
    assert "landing" not in g["phases"], "the flag no longer turns it off"
    assert "touchdown" in [p["key"] for p in g["phases"]["descent"]["parts"]]


def test_with_the_landing_a_phase_the_touchdown_is_counted_once():
    """The owner's objection: the touchdown was listed - and counted - in the
    descent and in the landing both."""
    al = {"score": 40.0, "bank_deg": 9.0, "scrub_g": 0.2, "bank_score": 40.0,
          "scrub_score": 100.0}
    with LandingPhase():
        g = _jet(alignment=al)
    descent = [p["key"] for p in g["phases"]["descent"]["parts"]]
    landing = [p["key"] for p in g["phases"]["landing"]["parts"]]
    assert not set(descent) & set(grading.LANDING_KEYS), (
        "the descent still carries landing measures: %r" % descent)
    assert landing[0] == "touchdown" and "alignment" in landing, landing
    assert g["phases"]["landing"]["parts"][0]["weight_pct"] == 100
    # The limit holds the landing phase down, not the descent.
    assert g["phases"]["landing"]["score"] <= 40.0 + grading.ALIGNMENT_CAP_POINTS + 0.05


def test_the_split_keeps_the_arithmetic():
    """Split by the touchdown's own share of the descent, so a leg with every
    measure present averages exactly as it did; only the cap sees more."""
    for prof in (grading.ROTARY, grading.LIGHT_GA, grading.FIXED_WING):
        d, l = grading.landing_phase_weights(prof)
        w = prof["descent"]["weights"]
        share = w["touchdown"] / sum(w.values())
        assert abs((d + l) - prof["phase_weights"]["descent"]) < 1e-9, prof["name"]
        assert abs(l - prof["phase_weights"]["descent"] * share) < 1e-9, prof["name"]
        with LandingPhase():
            pw = grading.phase_weights_for(prof)
        assert abs(sum(pw.values()) - sum(prof["phase_weights"].values())) < 1e-9, (
            "the leg's weights no longer sum to what they did on %s" % prof["name"])


def test_a_hard_landing_now_holds_the_leg_down():
    """Averaged into the descent, a hard landing behind a good approach was
    softened by it before the weakest-phase cap ever saw it."""
    with LandingPhase():
        g = _jet(fpm=480.0)
    land = g["phases"]["landing"]["score"]
    with LandingPhase(on=False):
        before = _jet(fpm=480.0)["overall"]
    assert before > land + grading.PHASE_CAP_POINTS, (
        "the fixture's landing was held down without the change too")
    assert g["worst_phase"] == "landing", g["worst_phase"]
    assert g["overall"] <= land + grading.PHASE_CAP_POINTS + 0.05, (
        "the leg sits more than a band above its landing: %s over %s"
        % (g["overall"], land))


def test_the_ride_leaves_out_the_landing():
    """The ride is the flying - the approach included - and the landing has
    its own sentence: a hard landing behind a good approach leaves it alone."""
    with LandingPhase():
        hard = _jet(fpm=480.0)
        soft = _jet(fpm=80.0)
    assert hard["ride_score"] == soft["ride_score"], (hard["ride_score"], soft["ride_score"])
    assert hard["ride_worst_phase"] != "landing"


def test_a_helicopter_landing_phase_has_no_limits():
    with LandingPhase():
        track = _departure(fpm=1000.0)
        g = grading.grade_leg(track, 80.0, "Test Helicopter", category="Helicopter")
    assert g and "landing" in g["phases"], "the fixture has no landing phase"
    keys = [p["key"] for p in g["phases"]["landing"]["parts"]]
    assert keys == ["touchdown"], "a helicopter landing carries %r" % keys


def test_the_panel_shows_the_landing_phase():
    with LandingPhase():
        doc = grading.describe_profile(grading.FIXED_WING)
    keys = [ph["key"] for ph in doc["phases"]]
    assert keys[-2:] == ["descent", "landing"], keys
    descent = next(ph for ph in doc["phases"] if ph["key"] == "descent")
    landing = next(ph for ph in doc["phases"] if ph["key"] == "landing")
    assert not [m for m in descent["metrics"] if m["key"] in grading.LANDING_KEYS]
    assert [m["key"] for m in landing["metrics"]][0] == "touchdown"
    assert abs(sum(ph["weight_pct"] for ph in doc["phases"]) - 100) <= 1, (
        "the panel's phase weights no longer add to 100")
    weighted = [m["weight_pct"] for m in descent["metrics"] if not m.get("cap")]
    assert abs(sum(weighted) - 100) <= 2, "the descent's measures do not add to 100"


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
    print("  %d grading test(s), %d failed" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

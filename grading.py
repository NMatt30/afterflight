"""Per-phase flight grading: lift-off, climb, cruise, descent/landing.

The logbook graded a leg on one number - the touchdown rate - so a leg flown
badly and landed well scored the same as one flown well throughout. This grades
each phase separately and rolls them into one figure.

WHAT IS MEASURED, AND WHY IT IS A RATE
--------------------------------------
Totals are not felt; changes are. Three synthetic cruises of equal length,
scored by spread and by the acceleration a passenger actually experiences:

    smooth 1500 ft climb over a ridge   spread 560 ft   0.003 g
    slow wander +/-300 ft over 3 min    spread 203 ft   0.008 g
    chop of only +/-20 ft every 6 s     spread  14 ft   0.440 g

The chop is the one nobody would sit through and it looked like the best
flying on the list by spread. So the metrics here are rates: vertical and
fore/aft acceleration in g, derived from the change in recorded vertical and
ground speed. Bank is left as an angle because it already IS lateral
acceleration - g times tan of the angle.

WHAT THE DATA ALLOWS, AND WHAT IT DOES NOT
------------------------------------------
Everything comes from the 1 Hz session track: lat, lon, alt, vs, gs,
airspeed, heading, on_ground, bank, pitch, height above ground, G force and
body accelerations, with the highest and lowest of each seen within every
second.

Any of the last six may be absent - the sim does not report every variable for
every aircraft, and a track can be truncated or copied in from elsewhere. Where
one is missing the grader either derives a substitute and says so, or declines
to score that measure. It never treats a missing reading as a good one.

The scores still come from differentiating vs and gs, not from the recorded
accelerations, and that is a real limit rather than an oversight - the rotary
bands were calibrated against the derived figures, so swapping the input would
change historical grades.
Differentiation omits higher-frequency motion, and bank is only a proxy for
lateral motion; rollout alignment separately uses recorded lateral ranges.

At 1 Hz nothing above 0.5 Hz is visible. Rotor vibration, turbulence texture
and control chop all live above that and cannot be seen here. The ISO 2631
comfort scale (0.315 m/s2 comfortable, 2.5 m/s2 extremely uncomfortable) is
defined for vibration in the 0.5-80 Hz band - exactly the band this data
cannot reach - so although the numbers below land in a similar range, that is
a coincidence and not a validation. These scores describe the shape of the
flying, not what the ride felt like.

ONE PROFILE PER KIND OF AIRCRAFT
-------------------------------
Every threshold lives in a profile dict, and profile_for() picks between them
on what the sim reports, never on the aircraft title - title matching is a
maintenance treadmill that silently misfiles anything not on the list.

    ROTARY        CATEGORY "Helicopter". Calibrated against real flights
                  The only calibrated profile.
    LIGHT_GA      CATEGORY "Airplane", VS0 25 to 61 kt. FAR 23 singles and
                  light twins - 14 CFR 23.49 draws that line, not this file.
    FIXED_WING    CATEGORY "Airplane", VS0 above 61 kt. Airline sources:
                  FSF stabilized approach gates, GPWS bank alerting.
    UNCLASSIFIED  CATEGORY "Airplane", VS0 below 25 kt. A gyroplane reports
                  itself as an airplane; the measures that need a wing are
                  left out rather than answered wrongly.

Only ROTARY is calibrated against measured flights. Every other profile is
built from published criteria and carries a sources line naming them, cited
again under each phase beside the numbers it backs. None of these may be
quietly tuned to look better - grades are derived, so moving a threshold
rewrites every leg already in the logbook.
"""

import math

SCHEMA = 2

# ------------------------------------------------------------------ ladder

# A, B, C, D, F. There is no E: it reads as a typo for F, which is why most
# scales dropped it.
LETTER_TABLE = (
    (90.0, "A"),
    (80.0, "B"),
    (70.0, "C"),
    (60.0, "D"),
)
LETTER_WORST = "F"

# Touchdown rate on the same 0-100 scale so the landing combines with the rest.
# The touchdown LETTER is read off this curve too (touchdown_letter), so the
# curve has to cross each letter boundary at the ladder's rate - see the end.
#
# Both ends of this curve come from published numbers rather than from feel.
#
# THE TOP. A curve that starts deducting at 1 fpm scores a difference nobody
# can aim at: below 60 fpm a touchdown is what the community calls butter, and
# there is no craft in the gap between 12 fpm and 45 fpm - they are the same
# landing. 60 is where the sources put that boundary (under 60 is butter, the
# top tenth of pilots land under 80, the average is around 150), and it is
# also exactly the A limit of the letter ladder. So the rule reads simply: a
# landing the ladder calls an A scores full marks, rather than scoring
# anywhere between 90 and 100 inside a single band.
#
# Note what the sources do NOT support: that a greaser is bad. AOPA calls it
# overrated, which is the argument that smoothness is not SUFFICIENT - speed
# control, centreline and touchdown zone matter more - not that it should be
# marked down. The doctrine that a firm arrival is better is real, but it is
# transport practice: wheel spin-up, autobrake and spoiler deployment, and
# cutting through water film on a wet runway. Light-aircraft training still
# aims as close to zero as it can get without floating. So the fix at this end
# is a plateau, not a penalty for being too soft.
#
# THE BOTTOM. Zero used to sit at 900 fpm, which is 15 ft/s - half again past
# anything the aircraft was built to absorb, so the whole bottom half of the
# scale described arrivals that are not landings. 14 CFR 23.473 builds a light
# airplane for a landing descent velocity of 7 to 10 ft/s, which is 420 to 600
# fpm, and industry flight-data monitoring treats 300 to 600 fpm as the
# uncomfortable band it wants to look at. 600 fpm is therefore the edge of the
# design envelope, and past the edge there is nothing left to grade: zero.
#
# THE A/B STEP. Giving the plateau the whole A band took the curve's A/B
# anchor away, and while the letter came from the ladder in passenger.py that
# cost nothing. Once the letter was read off the curve it cost the A limit:
# a ramp from 100 at 60 fpm to 80 at 150 passes 90 at 105, so A ran to 105
# fpm while the ladder, the EFB and the passenger prose all said 60. So the
# curve steps: full marks to 60, then the top of the B band, falling to 80 at
# 150. 89 rather than a hair under 90, because the page rounds a score to a
# whole number and 89.9 would read 90 beside a B. B/C, C/D and D/F were
# always on their boundaries.
TOUCHDOWN_PLATEAU_FPM = 60.0
TOUCHDOWN_ZERO_FPM = 600.0
TOUCHDOWN_B_TOP = 89.0

TOUCHDOWN_CURVE = (
    (0.0, 100.0),
    (TOUCHDOWN_PLATEAU_FPM, 100.0),   # butter / A limit
    (TOUCHDOWN_PLATEAU_FPM, TOUCHDOWN_B_TOP),   # just past it, a B
    (150.0, 80.0),     # B / C
    (300.0, 70.0),     # C / D
    (500.0, 60.0),     # D / F
    (TOUCHDOWN_ZERO_FPM, 0.0),        # 10 ft/s, the 23.473 design limit
)


# The touchdown curve as a band, for the reporting. The good end is the
# PLATEAU, not zero: full marks run all the way up to it, and printing "full
# marks 0 fpm" would say the opposite of the thing the plateau exists for.
TOUCHDOWN_BAND = (TOUCHDOWN_PLATEAU_FPM, TOUCHDOWN_CURVE[-1][0])

# A TRANSPORT aircraft is not flown to the same target, so it is not scored on
# the same curve. A jet crew aims at a band rather than at zero: published
# airline targets are 100 to 250 fpm, and a deliberately positive touchdown is
# correct technique, not a mistake - it spins the wheels up for the autobrakes
# and the spoilers, and it cuts through standing water instead of aquaplaning
# on top of it. Floating one on is the fault at this end: it usually means
# holding off past the touchdown zone with the speedbrakes still stowed.
#
# So full marks span the target band, and the curve tapers BOTH ways. It does
# not fall far below the band, because vertical speed alone cannot tell a
# well-flown soft landing from a float, and the taper should not pretend
# otherwise.
#
# Above the band it follows the described steps: acceptable to about 300 fpm,
# then hard and worth a flight-data look, then 600 fpm - 10 ft/s - which is
# the limit descent velocity 14 CFR 25.473 builds a transport aircraft for at
# design landing weight. Same zero as the light curve, arrived at from a
# different regulation.
TOUCHDOWN_CURVE_TRANSPORT = (
    (0.0, 85.0),       # floated on: not dangerous, not what was asked for
    (100.0, 100.0),    # bottom of the airline target band
    (250.0, 100.0),    # top of it
    (300.0, 90.0),     # firm, still acceptable
    (500.0, 60.0),     # hard
    (600.0, 0.0),      # 10 ft/s, the 25.473 design landing limit
)
TOUCHDOWN_BAND_TRANSPORT = (100.0, 250.0)


def touchdown_curve_for(profile):
    """The touchdown curve this profile is scored on."""
    return (profile or {}).get("touchdown_curve") or TOUCHDOWN_CURVE


def touchdown_band_for(profile):
    """The (best, worst) pair for this profile's touchdown, for the panel."""
    if touchdown_curve_for(profile) is TOUCHDOWN_CURVE_TRANSPORT:
        return TOUCHDOWN_BAND_TRANSPORT
    return TOUCHDOWN_BAND


def touchdown_band_text(profile):
    """What good and bad look like, in words that fit the curve's shape.

    band_text() prints "full marks X, zero Y", which reads correctly for a
    ceiling and is nonsense for a band: the transport curve gives full marks
    ACROSS 100 to 250 fpm, so printing "full marks 100, zero 250" would say
    the top of the target band scores nothing.
    """
    curve = touchdown_curve_for(profile)
    zero = curve[-1][0]
    if curve is TOUCHDOWN_CURVE_TRANSPORT:
        lo, hi = TOUCHDOWN_BAND_TRANSPORT
        return ("full marks %g to %g fpm, less either side, zero at %g"
                % (lo, hi, zero))
    return "full marks under %g fpm, zero at %g" % (TOUCHDOWN_PLATEAU_FPM, zero)


def letter_for_score(score):
    """Letter for a 0-100 score, or None if there is no score."""
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s != s:
        return None
    for limit, letter in LETTER_TABLE:
        if s >= limit:
            return letter
    return LETTER_WORST


def touchdown_letter(rate_fpm, profile=None):
    """The touchdown's A-F letter, on this aircraft type's own criteria.

    The same curve the touchdown is scored on, so the letter and the score
    are one verdict. It was a fixed rate ladder for every aircraft - A under
    60 fpm, C under 300 - which called a jet's 200 fpm, the middle of the
    airline target, "Firm", beside a touchdown score of 100.
    """
    return letter_for_score(score_for_touchdown_fpm(rate_fpm, profile))


def score_for_touchdown_fpm(rate_fpm, profile=None):
    """0-100 for a touchdown rate, sign ignored.

    Without a profile this answers on the light curve, which is the right
    default: it is what every aircraft was scored on before transport got its
    own, and a caller that does not know the type should not be guessing that
    it is a jet.
    """
    if rate_fpm is None:
        return None
    try:
        mag = abs(float(rate_fpm))
    except (TypeError, ValueError):
        return None
    if mag != mag:
        return None
    return _curve(mag, touchdown_curve_for(profile))


# ------------------------------------------------- touchdown alignment

# The landing letter was a function of vertical speed and nothing else, which
# called a wing drop a B: the gentlest arrival of the set by vertical speed,
# while rolling onto one main and sliding for several seconds after it was
# down. Two measures, both already sitting in the track:
#
#   bank_deg   peak |bank| from the contact sample through the rollout.
#              Touching down banked is correct crosswind technique, so what
#              this actually catches is bank still GROWING after the wheels
#              are down - a dropped wing rather than a held slip.
#   scrub_g    mean of (accel_x_max - accel_x_min) per rollout second, from
#              the peak-hold lateral accelerations. The CONTACT second is
#              deliberately excluded: every landing has one impulse there,
#              and including it confounds the contact impulse with rollout.
#
# Uncalibrated. Say which legs moved before touching these numbers.
# ROTARY scores neither until its alignment inputs have been validated;
# missing readings must never be interpreted as a flawless rollout.
ALIGNMENT_BANK_DEG = (4.0, 14.0)
ALIGNMENT_SCRUB_G = (0.15, 0.55)
ALIGNMENT_WEIGHTS = (("bank", 0.6), ("scrub", 0.4))
# Rollout seconds after the contact sample that scrub is averaged over.
ALIGNMENT_ROLLOUT_S = 5.0
# How far above the alignment score the landing letter may sit. One band -
# the same allowance the phase rollup gives the weakest phase.
ALIGNMENT_CAP_POINTS = 10.0

# ------------------------------------------------------------------ float
#
# How long the airplane was held off before the wheels touched. Measured as the
# air distance in its certification sense - "the distance from a height of 50
# feet above the landing surface to the point of main gear touchdown" (AC
# 25-32, 8.2.1) - and scored on the time it took, so one rule serves a 60 kt
# trainer and a 120 kt jet.
#
# The standards describe zones, not a slope, and the score follows them:
#
#   100 at 7 s. AC 25-32 8.2.4 gives "a flare time of 7 seconds" as the air
#     distance of "an average pilot who is flying in normal operations".
#   50 at the end of the touchdown zone. AC 91-79A puts the target touchdown
#     point "approximately 1,000 ft down the runway", the touchdown zone at
#     "500-3,000 ft beyond the runway threshold", and the certified distances
#     on the gear being "at a height of 50 ft over the runway threshold". From
#     a standard crossing, 2,000 ft of float beyond a normal flare puts the
#     wheels at the far end of the zone - still acceptable, and the last point
#     that is. With the cap allowance that is a D.
#   0 a further 1,000 ft on. This one is a judgment, not a published figure:
#     past the zone the approach should have been abandoned, and the score
#     should reach the bottom quickly rather than at once.
#
# A first version ran linearly from 7 s to zero at the end of the zone. Built
# against real landings it called a jet touching down 2,300 ft past the
# threshold - inside the zone AC 91-79A calls typical operation - an F. The
# standard says acceptable; the slope said fail.
#
# Distances become seconds at the speed actually flown, so every point after
# the first is worked out per landing.
#
# Heights are above the touchdown point - altitude less the altitude at
# contact - which is above the landing surface, not above whatever terrain the
# approach crossed.
#
# One rule for every airplane, and it is lenient for light ones on purpose.
# AC 91-79A's "flown onto the runway rather than being held off" is written for
# turbine airplanes; a light piston airplane is taught to hold off. At 60 kt the
# zero lands at 27 s.
FLOAT_HEIGHT_FT = 50.0
FLOAT_NORMAL_S = 7.0
FLOAT_MARGIN_FT = 2000.0          # normal flare -> end of the touchdown zone
FLOAT_ZONE_END_SCORE = 50.0
FLOAT_BEYOND_FT = 1000.0          # end of the zone -> zero (a judgment)
FLOAT_CAP_POINTS = ALIGNMENT_CAP_POINTS
# Off until the owner has seen what it does to existing landings. The float is
# measured and shown on every airplane leg either way; this decides only
# whether it may hold the landing letter and the descent phase down. It is a
# setting (grade_float), and the build signature carries it - see
# runway_tunables - so every sortie rebuilds when it changes.
GRADE_FLOAT = False

# ------------------------------------------------------------ touchdown point
#
# Where on the runway the wheels touched, measured past the landing threshold
# (runways.py) - the displaced one where there is a displacement. Only known
# where the sim has described the runway; see watcher.lookup_runways. The
# float says how long the airplane was held off; this says where that left it.
#
#   100 within 1,400 ft: AC 91-79A's target touchdown point, "approximately
#       1,000 ft down the runway", plus the Private Pilot ACS tolerance,
#       "within 400 feet beyond or on the specified point" - the loosest
#       published tolerance, applied to every airplane.
#   50  at the end of the touchdown zone: 3,000 ft, or the first third of a
#       shorter runway - AC 91-79A, "500-3,000 ft beyond the runway threshold
#       not to exceed the first one-third of the runway". On a runway short
#       enough that the third falls inside 1,400 ft, full marks stop at the
#       third and the next foot is 50: the third is the limit there.
#   0   1,000 ft beyond that - the same judgment as the float's, labelled as
#       one.
#   0   more than 50 ft before the threshold. Landing short is not good at any
#       distance; the 50 ft is for where the recording places the wheels.
TDZ_TARGET_FT = 1000.0
TDZ_TOLERANCE_FT = 400.0
TDZ_END_FT = 3000.0
TDZ_END_SCORE = 50.0
TDZ_BEYOND_FT = 1000.0
TDZ_SHORT_ALLOWANCE_FT = 50.0
TDZ_CAP_POINTS = ALIGNMENT_CAP_POINTS
# Off for the same reason as GRADE_FLOAT, and independently of it.
GRADE_TOUCHDOWN_POINT = False

# The float and touchdown-point bands are settings (settings.py, "Float and
# touchdown point"). The values above are the published figures and stay the
# defaults; PUBLISHED keeps them, so the grading panel - the one place that
# cites the ACs - can say when the bands in use are the owner's own instead.
#
# The landing ladder is not settable because its letters and the touchdown
# curve are separate constants, and a setting could move one without the
# other. These have no such split: the score, the band text and the letter cap
# all read the values below when they are called, so a setting moves all
# three together. Helicopters are untouched by any of them - scores_float
# gates every use on the profile, not on these values.
FLOAT_BANDS = ("FLOAT_NORMAL_S", "FLOAT_MARGIN_FT", "FLOAT_BEYOND_FT")
TDZ_BANDS = ("TDZ_TARGET_FT", "TDZ_TOLERANCE_FT", "TDZ_END_FT", "TDZ_BEYOND_FT")
RUNWAY_TUNABLES = ("GRADE_FLOAT",) + FLOAT_BANDS + ("GRADE_TOUCHDOWN_POINT",) + TDZ_BANDS
PUBLISHED = {_name: globals()[_name] for _name in RUNWAY_TUNABLES}


def runway_tunables():
    """The values in use. A setting changes these without changing this
    file, so the build signs them separately from grading_revision."""
    g = globals()
    return {name: g[name] for name in RUNWAY_TUNABLES}


def own_bands(names):
    """True when any of these bands is not the published figure."""
    g = globals()
    return any(g[name] != PUBLISHED[name] for name in names)

G_FT_S2 = 32.174

LETTER_ORDER = ("A", "B", "C", "D", "F")


def worse_letter(a, b):
    """The lower of two letters, ignoring either if it is missing."""
    if a not in LETTER_ORDER:
        return b if b in LETTER_ORDER else None
    if b not in LETTER_ORDER:
        return a
    return a if LETTER_ORDER.index(a) >= LETTER_ORDER.index(b) else b


def _band_score(value, band):
    """100 at or below the good end, 0 at or above the bad end, linear between."""
    if value is None:
        return None
    try:
        v = abs(float(value))
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    good, bad = band
    if v <= good:
        return 100.0
    if v >= bad:
        return 0.0
    return 100.0 * (bad - v) / (bad - good)


def _alignment_sub(al):
    """The two halves of an alignment score, each with its own band.

    The pill named the total and only ever printed the bank band, so a reader
    was shown "0.31 g slide" with nothing to say whether that was a lot or
    what it did to the number. Both halves, both bands, both weights.
    """
    if not al:
        return []
    w = dict(ALIGNMENT_WEIGHTS)
    out = []
    if al.get("bank_score") is not None:
        out.append({"key": "bank", "label": "bank",
                    "measured": "%.1f°" % al["bank_deg"],
                    "score": al["bank_score"],
                    "weight_pct": round(w["bank"] * 100),
                    "band": "full marks %g°, zero %g°" % ALIGNMENT_BANK_DEG})
    if al.get("scrub_score") is not None:
        out.append({"key": "slide", "label": "slide",
                    "measured": "%.2f g" % al["scrub_g"],
                    "score": al["scrub_score"],
                    "weight_pct": round(w["scrub"] * 100),
                    "band": "full marks %g g, zero %g g" % ALIGNMENT_SCRUB_G})
    return out


def _lateral_spread(p):
    """Peak-to-peak lateral acceleration for one sample, or None if unrecorded."""
    lo, hi = p.get("accel_x_min"), p.get("accel_x_max")
    if not (isinstance(lo, (int, float)) and isinstance(hi, (int, float))):
        return None
    return abs(float(hi) - float(lo))


# The landing event is stamped at contact; the 1 Hz track's own first
# on-ground sample can be most of a second later. Look back this far so the
# right sample is found without reaching the previous leg's rollout.
LANDING_MATCH_SEC = 1.5


def landing_index(track, t_land=None):
    """Index of the touchdown sample, or None.

    With t_land, the first on-ground sample at about that time - which is how
    a specific leg of a multi-leg sortie is addressed. Without it, the last
    airborne -> on-ground transition in the track.
    """
    if t_land is not None:
        for i, p in enumerate(track):
            t = p.get("t")
            if not isinstance(t, (int, float)):
                continue
            if t >= t_land - LANDING_MATCH_SEC and p.get("on_ground"):
                return i
        return None
    found = None
    for i in range(1, len(track)):
        if track[i - 1].get("on_ground") is False and track[i].get("on_ground") is True:
            found = i
    return found


def touchdown_alignment(track, idx=None, t_land=None):
    """How square the aircraft was when it arrived, or None if not measured.

    Returns bank in degrees, scrub in g, a 0-100 score, and the sample counts
    behind them - the counts matter because a rollout cut short by the end of
    a leg is a thinner measurement than a full one, and the UI says so.

    None means "the track does not carry this", which is every landing older
    than the accelerations. It must never be read as a clean arrival.
    """
    if not track:
        return None
    if idx is None:
        idx = landing_index(track, t_land)
    if idx is None or idx >= len(track):
        return None

    t0 = track[idx].get("t")
    if not isinstance(t0, (int, float)):
        return None

    # Bank over contact plus rollout. Present on every track that has bank.
    banks, spreads = [], []
    for p in track[idx:]:
        t = p.get("t")
        if not isinstance(t, (int, float)) or (t - t0) > ALIGNMENT_ROLLOUT_S:
            break
        if not p.get("on_ground"):
            break
        b = p.get("bank")
        if isinstance(b, (int, float)):
            banks.append(abs(float(b)))
        if p is not track[idx]:                 # contact impulse excluded
            sp = _lateral_spread(p)
            if sp is not None:
                spreads.append(sp)

    if not banks and not spreads:
        return None

    bank = max(banks) if banks else None
    scrub = (sum(spreads) / len(spreads) / G_FT_S2) if spreads else None
    parts = {"bank": _band_score(bank, ALIGNMENT_BANK_DEG),
             "scrub": _band_score(scrub, ALIGNMENT_SCRUB_G)}

    # Re-weight over whichever measures are present, so a track with bank but
    # no accelerations still says something rather than nothing.
    live = [(k, w) for k, w in ALIGNMENT_WEIGHTS if parts.get(k) is not None]
    if not live:
        return None
    total = sum(w for _k, w in live)
    score = sum(parts[k] * w for k, w in live) / total

    return {
        "bank_deg": round(bank, 2) if bank is not None else None,
        "scrub_g": round(scrub, 3) if scrub is not None else None,
        "bank_score": round(parts["bank"], 1) if parts["bank"] is not None else None,
        "scrub_score": round(parts["scrub"], 1) if parts["scrub"] is not None else None,
        "score": round(score, 1),
        "rollout_samples": len(spreads),
        "partial": len(live) < len(ALIGNMENT_WEIGHTS),
    }


def landing_alignment_for(track, aircraft=None, category=None, vs0_kt=None,
                          t_land=None):
    """Alignment for this leg's landing, or None if it is not scored.

    Profile selection lives here rather than at the call site: it is the same
    CATEGORY / VS0 routing grade_leg uses, and two copies of it would drift.
    """
    if not track:
        return None
    if not category:
        category = infer_category(track)
    if not scores_alignment(profile_for(aircraft, category=category, vs0_kt=vs0_kt)):
        return None
    return touchdown_alignment(track, t_land=t_land)


def alignment_ceiling_letter(alignment):
    """Highest letter the landing may keep, given how square it arrived."""
    if not alignment or alignment.get("score") is None:
        return None
    return letter_for_score(alignment["score"] + ALIGNMENT_CAP_POINTS)


def scores_alignment(profile):
    """Whether this profile grades touchdown alignment at all."""
    return bool((profile or {}).get("alignment"))


def _ground_ft(a, b):
    """Great-circle feet between two points carrying lat and lon."""
    lat1, lat2 = math.radians(float(a["lat"])), math.radians(float(b["lat"]))
    dlat = lat2 - lat1
    dlon = math.radians(float(b["lon"]) - float(a["lon"]))
    h = (math.sin(dlat / 2.0) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2)
    return 2.0 * 20925646.3 * math.asin(min(1.0, math.sqrt(h)))


def _threshold_crossing(points, idx, past):
    """(j, frac) where the arrival crossed the threshold before contact idx:
    between points[j] and points[j + 1], frac of the way. (None, None) if
    the recording starts past it; ("short", None) if contact was before it."""
    try:
        if past(points[idx]) < 0:
            return "short", None
        for j in range(idx - 1, -1, -1):
            a = past(points[j])
            if a < 0:
                b = past(points[j + 1])
                return j, (-a / (b - a)) if b > a else 0.0
    except (KeyError, TypeError, ValueError):
        return "short", None
    return None, None


# How far apart the two readings a slope is taken from must be, at least. The
# 10 Hz clip puts readings 15 ft apart at approach speed, where a tenth of a
# foot of rounding in the height above ground is a 0.7% slope.
SURFACE_BASE_FT = 100.0
# Which readings are this landing's: from a while before touchdown to shortly
# after, and only along the runway. A track is the whole flight, and "past
# the threshold" is a distance along a heading - every point ahead on that
# line, the departure included, is past it.
SURFACE_BEFORE_S = 120.0
SURFACE_AFTER_S = 20.0
# ...and only on it: two minutes before touchdown can be the downwind leg,
# abeam the runway and half a mile to one side of it, over other ground.
SURFACE_LATERAL_FT = 250.0


class RunwaySurface(object):
    """The runway's elevation along its length, as the sim models it.

    The sim gives a runway one elevation, and its runways are not flat: some
    rise or fall by 2% or more, which is 20 ft over the first 1,000 ft. So the
    surface is read from the aircraft itself - its altitude less its height
    above the ground beneath (PLANE ALT ABOVE GROUND), wherever it was over
    the runway. Only readings past the threshold are used: before it is
    whatever lies off the end, a field or the edge of a mesa, and an approach
    over a drop would otherwise put the runway hundreds of feet low.

    at(x) is the elevation x feet past the threshold: interpolated between
    readings, and beyond them extended along the slope of the nearest two that
    are SURFACE_BASE_FT apart. Before the threshold that extension is the
    runway's plane, which is what "50 ft above the landing surface" means.
    ref is the aircraft's own height above the ground on its wheels, the
    offset between where its altitude is measured and its wheels.
    """

    def __init__(self, xs, gs, ref):
        self.xs, self.gs, self.ref = xs, gs, ref

    def _slope(self, i, j):
        return (self.gs[j] - self.gs[i]) / (self.xs[j] - self.xs[i])

    def _far(self, i, step):
        j = i + step
        while 0 <= j < len(self.xs) and abs(self.xs[j] - self.xs[i]) < SURFACE_BASE_FT:
            j += step
        return j if 0 <= j < len(self.xs) else None

    def at(self, x):
        xs, gs = self.xs, self.gs
        if x <= xs[0]:
            j = self._far(0, 1)
            return gs[0] + (self._slope(0, j) * (x - xs[0]) if j is not None else 0.0)
        if x >= xs[-1]:
            n = len(xs) - 1
            j = self._far(n, -1)
            return gs[n] + (self._slope(j, n) * (x - xs[n]) if j is not None else 0.0)
        for k in range(1, len(xs)):
            if x <= xs[k]:
                f = (x - xs[k - 1]) / (xs[k] - xs[k - 1]) if xs[k] > xs[k - 1] else 0.0
                return gs[k - 1] + f * (gs[k] - gs[k - 1])
        return gs[-1]

    def height(self, q, past):
        """How high the wheels were above the runway, at this point."""
        return float(q["alt"]) - self.at(past(q)) - self.ref


def runway_surface(samples, past, t_land=None, length_ft=None, beside=None):
    """A RunwaySurface from samples carrying alt, agl, lat and lon, or None.

    Only readings from SURFACE_BEFORE_S before touchdown to SURFACE_AFTER_S
    after it, between the threshold and length_ft past it, and - given
    beside(point), feet from the centreline - within SURFACE_LATERAL_FT of
    the centreline, are runway.
    None without at least two of them SURFACE_BASE_FT apart, or without a
    reading on the wheels after touchdown to take ref from.
    """
    if not samples or past is None:
        return None
    pts = [q for q in samples if _finite(q.get("alt")) and _finite(q.get("agl"))
           and _finite(q.get("t"))]
    idx = landing_index(pts, t_land)
    if not idx:
        return None
    ref = [float(q["agl"]) for q in pts[idx:idx + 10] if q.get("on_ground")]
    if not ref:
        return None
    ref = sorted(ref)[len(ref) // 2]
    t_td = float(pts[idx]["t"])
    end = float(length_ft) if _finite(length_ft) else float("inf")
    over = []
    for q in pts:
        if not (t_td - SURFACE_BEFORE_S <= float(q["t"]) <= t_td + SURFACE_AFTER_S):
            continue
        try:
            x = past(q)
            if beside is not None and abs(beside(q)) > SURFACE_LATERAL_FT:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= x <= end:
            over.append((x, float(q["alt"]) - float(q["agl"])))
    over.sort()
    if len(over) < 2 or over[-1][0] - over[0][0] < SURFACE_BASE_FT:
        return None
    return RunwaySurface([x for x, _ in over], [g for _, g in over], ref)


def threshold_height(points, t_land=None, past=None, surface=None):
    """How high the wheels crossed the landing threshold, above the runway.

    Information, not a grade: the float and the touchdown point already say
    what a high or low crossing cost. Measured against the runway surface at
    the threshold (RunwaySurface). It used to be measured above where the
    wheels touched, which on a sloped runway is off by the rise between the
    two - 30 ft and more on runways that climb 2%. Without a surface it is
    None, not that guess. None too when the recording does not reach back to
    the threshold.
    """
    if not points or past is None or surface is None:
        return None
    idx = landing_index(points, t_land)
    if not idx or not _finite(points[idx].get("alt")):
        return None
    j, frac = _threshold_crossing(points, idx, past)
    if not isinstance(j, int):
        return None
    a, b = points[j], points[j + 1]
    if not (_finite(a.get("alt")) and _finite(b.get("alt"))):
        return None
    alt = float(a["alt"]) + frac * (float(b["alt"]) - float(a["alt"]))
    return round(alt - surface.at(0.0) - surface.ref, 1)


def landing_float(points, t_land=None, source=None, past=None, surface=None):
    """How long the airplane was held off over the runway, or None.

    The clock starts at whichever comes later: the 50 ft point (AC 25-32's air
    distance, which assumes 50 ft over the threshold) or the threshold. An
    arrival that crosses lower than 50 ft had its clock started over the
    approach, so time flown low before the runway counted as float, and a
    landing just past the threshold could read as a long float. past(point) is
    feet past the
    landing threshold (runways.past_threshold); without it the clock starts
    at 50 ft, which is how a caller with no runway learns nothing useful -
    the builder measures no float there at all.

    points is the landing clip at 10 Hz when there is one and the 1 Hz track
    otherwise; resolution_s says which, because a 1 Hz track places the 50 ft
    crossing and the contact each to within a second.

    None means it could not be measured - the recording starts below 50 ft,
    the aircraft was on the ground inside the window, a field is missing, or
    it touched down short of the threshold, which the touchdown point scores.
    It is never a short float.
    """
    if not points:
        return None
    idx = landing_index(points, t_land)
    if not idx:
        return None
    td = points[idx]
    if not (_finite(td.get("t")) and _finite(td.get("alt"))):
        return None
    # Height above the runway where there is one (RunwaySurface): above where
    # the wheels touched is off by the runway's rise on a sloped one.
    if surface is not None and past is not None:
        def height(q):
            return surface.height(q, past)
    else:
        ground = float(td["alt"])

        def height(q):
            return float(q["alt"]) - ground

    # Back from contact through the flare to the last sample above 50 ft.
    j = idx - 1
    while j >= 0:
        q = points[j]
        if q.get("on_ground") is not False:
            return None
        if not (_finite(q.get("t")) and _finite(q.get("alt"))):
            return None
        if height(q) > FLOAT_HEIGHT_FT:
            break
        j -= 1
    if j < 0:
        return None
    above, below = points[j], points[j + 1]
    h_a = height(above)
    h_b = height(below)
    frac = (h_a - FLOAT_HEIGHT_FT) / (h_a - h_b) if h_a > h_b else 0.0
    t_cross = float(above["t"]) + frac * (float(below["t"]) - float(above["t"]))
    start = "50 ft"
    if past is not None:
        k, kfrac = _threshold_crossing(points, idx, past)
        if k == "short":
            return None
        if isinstance(k, int):
            if not (_finite(points[k].get("t")) and _finite(points[k + 1].get("t"))):
                return None
            t_thr = (float(points[k]["t"])
                     + kfrac * (float(points[k + 1]["t"]) - float(points[k]["t"])))
            if t_thr > t_cross:
                # Below 50 ft before the runway began: start over the runway.
                j, frac, t_cross, start = k, kfrac, t_thr, "threshold"
                above, below = points[j], points[j + 1]
    seconds = float(td["t"]) - t_cross

    try:
        dist = (1.0 - frac) * _ground_ft(above, below)
        for k in range(j + 1, idx):
            dist += _ground_ft(points[k], points[k + 1])
    except (KeyError, TypeError, ValueError):
        return None
    if seconds <= 0 or dist <= 0:
        return None
    fps = dist / seconds
    if start == "threshold" and seconds < 1.0:
        # A second or less over the runway is too short to give a speed from;
        # the approach just before it says how fast the arrival was.
        k = max(0, j - 10)
        try:
            span = sum(_ground_ft(points[m], points[m + 1]) for m in range(k, idx))
            fps = span / (float(td["t"]) - float(points[k]["t"]))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None
    if fps < 10.0:                      # not an airplane arriving
        return None
    zone_end_s = FLOAT_NORMAL_S + FLOAT_MARGIN_FT / fps
    zero_s = zone_end_s + FLOAT_BEYOND_FT / fps
    gaps = [float(points[k + 1]["t"]) - float(points[k]["t"])
            for k in range(max(0, idx - 5), idx)
            if _finite(points[k].get("t")) and _finite(points[k + 1].get("t"))]
    return {
        "seconds": round(seconds, 1),
        "distance_ft": round(dist),
        "speed_kt": round(fps / 1.68781, 1),
        "zone_end_s": round(zone_end_s, 1),
        "zero_s": round(zero_s, 1),
        "score": round(float_score(seconds, zone_end_s, zero_s), 1),
        "resolution_s": round(sorted(gaps)[len(gaps) // 2], 2) if gaps else None,
        "source": source,
        # Where the clock started: "threshold" when the arrival was already
        # below 50 ft at the runway, "50 ft" otherwise.
        "from": start,
    }


def scores_float(profile):
    """Whether this profile measures the landing float at all."""
    return bool((profile or {}).get("float"))


def landing_float_for(points, aircraft=None, category=None, vs0_kt=None,
                      t_land=None, source=None, track=None, past=None,
                      surface=None):
    """The float for this leg's landing, or None if it is not measured here.

    Profile routing is the same as alignment's and for the same reason. track,
    when given, is what infers the category: a clip is too short to.
    """
    if not points:
        return None
    if not category:
        category = infer_category(track or points)
    if not scores_float(profile_for(aircraft, category=category, vs0_kt=vs0_kt)):
        return None
    return landing_float(points, t_land=t_land, source=source, past=past,
                         surface=surface)


def float_score(seconds, zone_end_s, zero_s):
    """100 through a normal flare, 50 at the end of the zone, 0 beyond it."""
    if seconds <= FLOAT_NORMAL_S:
        return 100.0
    if seconds <= zone_end_s:
        span = zone_end_s - FLOAT_NORMAL_S
        return 100.0 - (100.0 - FLOAT_ZONE_END_SCORE) * (seconds - FLOAT_NORMAL_S) / span
    if seconds >= zero_s:
        return 0.0
    return FLOAT_ZONE_END_SCORE * (zero_s - seconds) / (zero_s - zone_end_s)


def float_points_s(speed_kt):
    """(end of zone, zero) in seconds from 50 ft, at this ground speed."""
    fps = float(speed_kt) * 1.68781
    end = FLOAT_NORMAL_S + FLOAT_MARGIN_FT / fps
    return round(end, 1), round(end + FLOAT_BEYOND_FT / fps, 1)


def float_band_text(flt):
    """What good looks like for this landing - two of the points move with speed.

    In the shape of every other line in the phase tooltip, "full marks X, zero
    Y", with the 50 between because this one has a knee. The speed is there
    because it is what put the 50 and the zero where they are.
    """
    return ("Full marks up to %g s. Half marks at %g s, zero at %g s "
            "(at your %d kt)"
            % (FLOAT_NORMAL_S, flt["zone_end_s"], flt["zero_s"],
               round(flt["speed_kt"])))


def float_ceiling_letter(flt):
    """Highest letter the landing may keep, or None while the float is not graded."""
    if not GRADE_FLOAT or not flt or flt.get("score") is None:
        return None
    return letter_for_score(flt["score"] + FLOAT_CAP_POINTS)


def _cap_part(key, label, value, measured, band, counted, cap_points, score):
    """A cap's line in the descent phase, and the phase score after it.

    Shown whether or not it is counted. Until it is, it carries counted False
    and the UI says it is measured but not yet counted - a cap that simply
    never bites would read as a measurement that came out fine.
    """
    ceiling = value + cap_points
    held = counted and score is not None and ceiling < score
    part = {
        "key": key,
        "label": label,
        "score": round(value, 1),
        "measured": measured,
        "weight_pct": 0,
        "band": band,
        "cap": counted,
        "counted": counted,
        "held": bool(held),
        "held_to": round(ceiling, 1) if held else None,
        "held_from": round(score, 1) if held else None,
    }
    return part, (ceiling if held else score)


def _float_part(flt, score):
    """The float's line in the descent phase, and the score after any cap."""
    if not flt or flt.get("score") is None:
        return None, score
    return _cap_part("float", "Float", flt["score"],
                     "%.1f s from %s" % (flt["seconds"],
                                        "the runway start" if flt.get("from") == "threshold"
                                        else "50 ft up"),
                     float_band_text(flt), bool(GRADE_FLOAT), FLOAT_CAP_POINTS, score)


def touchdown_zone_end_ft(length_ft):
    """3,000 ft, or the first third of a shorter runway."""
    try:
        third = float(length_ft) / 3.0
    except (TypeError, ValueError):
        return TDZ_END_FT
    return min(TDZ_END_FT, third) if third > 0 else TDZ_END_FT


def touchdown_point_score(distance_ft, length_ft=None):
    """100 near the target, 50 at the end of the touchdown zone, 0 beyond."""
    d = float(distance_ft)
    end = touchdown_zone_end_ft(length_ft)
    full = min(TDZ_TARGET_FT + TDZ_TOLERANCE_FT, end)
    zero = end + TDZ_BEYOND_FT
    if d < -TDZ_SHORT_ALLOWANCE_FT:
        return 0.0
    if d <= full:
        return 100.0
    if d <= end:
        return (100.0 - (100.0 - TDZ_END_SCORE) * (d - full) / (end - full)
                if end > full else TDZ_END_SCORE)
    if d >= zero:
        return 0.0
    return TDZ_END_SCORE * (zero - d) / TDZ_BEYOND_FT


def touchdown_point_band_text(tp):
    """What good looks like on this runway, which a short one changes.

    "full marks X, 50 at Y, zero Z", like its neighbours in the tooltip, and
    short: the browser cuts a tooltip line off at a fixed width, and the first
    wording of this one lost its end. Where the runway's first
    third ends the zone, the runway length is named, since it is the reason.
    On a runway short enough that the third falls inside the full-marks
    distance, full marks stop at the third and the next foot is 50; saying
    "full marks 804 ft, 50 at 804 ft" read as a contradiction, so that case
    is a step and is described as one.
    """
    length = tp.get("length_ft")
    end = touchdown_zone_end_ft(length)
    full = min(TDZ_TARGET_FT + TDZ_TOLERANCE_FT, end)
    ft = lambda v: "{:,} ft".format(int(round(v)))
    third = (" (a third of this %s runway)" % ft(length)
             if end < TDZ_END_FT else "")
    if full >= end:
        return ("Full marks within %s%s. Half marks just past that, zero "
                "at %s" % (ft(end), third, ft(end + TDZ_BEYOND_FT)))
    return ("Full marks within %s. Half marks at %s%s, zero at %s"
            % (ft(full), ft(end), third, ft(end + TDZ_BEYOND_FT)))


def touchdown_point_ceiling_letter(tp):
    """Highest letter the landing may keep, or None while this is not graded."""
    if not GRADE_TOUCHDOWN_POINT or not tp or tp.get("score") is None:
        return None
    return letter_for_score(tp["score"] + TDZ_CAP_POINTS)


def _touchdown_point_part(tp, score):
    if not tp or tp.get("score") is None:
        return None, score
    d = tp["distance_ft"]
    rwy = "runway %s" % tp["runway"] if tp.get("runway") else "the runway"
    measured = ("{:,} ft short of {}".format(int(round(-d)), rwy) if d < 0
                else "{:,} ft down {}".format(int(round(d)), rwy))
    return _cap_part("touchdown_point", "Touchdown spot", tp["score"], measured,
                     touchdown_point_band_text(tp), bool(GRADE_TOUCHDOWN_POINT),
                     TDZ_CAP_POINTS, score)


# ------------------------------------------------------------------ words

# What each metric is called, how to print what was measured, and one line of
# plain English for the "how grading works" panel. Nobody outside this file
# should have to read "approach_accel_g" to learn how smooth an approach was,
# and the UI should not carry its own copy of the vocabulary.
METRICS = {
    "alignment": (
        "Alignment", "%.0f\u00b0 bank",
        "How straight it arrived. Bank is the furthest the aircraft rolled "
        "from the moment the wheels touched through the rollout that "
        "followed; slide is how hard it was still moving sideways once it "
        "was down. Full marks at 4 degrees of bank and 0.15 g of slide, "
        "nothing at 14 degrees or 0.55 g. This measure can only take points "
        "off the phase, never add them: arriving straight is what is "
        "expected of a landing, while arriving crooked costs. A landing that "
        "touches down gently and then slides is not a good landing, however "
        "soft it felt."),
    "touchdown": (
        "Touchdown", "%.0f fpm",
        "How firmly it arrived. The one thing measured directly rather than "
        "derived, and the part anyone remembers."),
    "rotation_g": (
        "Rotation", "%.2f g",
        "How hard the pull was as the nose came up, from the load factor the "
        "sim reported. Rotating is not turbulence: lifting off REQUIRES a "
        "pull, and the band is set so that a rotation flown at the published "
        "rate scores full marks. Points are lost for yanking it off, not for "
        "leaving the ground."),
    "vert_accel_g": (
        "Vertical g", "%.2f g",
        "How sharply the climb or descent rate changed - being pushed into "
        "the seat or lifted out of it."),
    "long_accel_g": (
        "Fore/aft g", "%.2f g",
        "How sharply the speed changed. Acceleration and braking."),
    "bank_p95_deg": (
        "Steepest turn", "%.0f°",
        "The steepest sustained turn, scored on how it FELT rather than on "
        "whether it was the right turn to make. Bank angle is lateral g, so "
        "this is how hard you were pushed sideways, and a firmly flown turn "
        "costs points here even when it is exactly correct. Circuit work is "
        "the clearest case: pattern turns are normally flown at 20 to 30 "
        "degrees, which is good airmanship and a firm ride, and this measure "
        "only reports the second of those."),
    "approach_accel_g": (
        "Approach g", "%.2f g",
        "Vertical g over the last 1000 ft above the landing site, where a "
        "jolt has no height left to absorb it."),
    "descent_angle_deg": (
        "Descent angle", "%.0f°",
        "How steep the descent path was."),
    "vrs_exposure_pct": (
        "Vortex ring exposure", "%.0f%% of descent",
        "Share of the descent spent sinking faster than 300 fpm below 30 kt, "
        "which is the condition that causes vortex ring state."),
    "gate_500_vs_fpm": (
        "Sink at 500 ft", "%.0f fpm",
        "Rate of descent crossing 500 ft above the landing site - the "
        "stabilized-approach gate."),
}


def band_text(key, band):
    """"full marks 3.5\u00b0, zero 7\u00b0" - what good and bad look like.

    A score of 33/100 beside "6 degrees" tells a reader the number was bad
    without telling them what would have been good, which is most of what
    they wanted to know. The panel has carried these all along; the leg
    tooltip, where people actually read them, did not.
    """
    entry = METRICS.get(key)
    if not entry or not band:
        return None
    fmt = entry[1]
    try:
        best, worst = float(band[0]), float(band[1])
    except (TypeError, ValueError, IndexError):
        return None
    if fmt is None:
        return None

    # Everything after the conversion spec is the unit: "%.0f fpm" -> " fpm",
    # "%.0f° bank" -> "° bank".
    #
    # Formatted, not sliced. The tail of a format string is still format
    # syntax, so slicing "%.0f%% of descent" gave "%% of descent" and the
    # tooltip printed "full marks 3%% of descent" - while the measurement
    # beside it, which does go through %, read "3% of descent". Taking the
    # unit from the formatted output makes it the measurement's unit by
    # construction, whatever escape a format uses.
    cut = fmt.find("f", fmt.find("%"))
    if cut < 0:
        return None
    try:
        unit = (fmt % 0.0)[len(fmt[:cut + 1] % 0.0):]
    except (TypeError, ValueError):
        return None

    def one(v):
        """The threshold itself, not a rounded-off version of it.

        The measurement format is right for a measurement - "6°" - and wrong
        for the threshold behind it: %.0f turns the light-airplane descent
        limit of 3.5 into "4", so the line meant to say what good looks like
        would say a number that is not the limit. Trim trailing zeros so the
        ones that are whole still read as whole.
        """
        return ("%.2f" % v).rstrip("0").rstrip(".") + unit

    return "full marks %s, zero %s" % (one(best), one(worst))


def describe_metric(key, value):
    """(label, measured-value-as-text) for a metric, or the key if unknown."""
    entry = METRICS.get(key)
    if not entry:
        return key, None
    label, fmt = entry[0], entry[1]
    if fmt is None or value is None or not _finite(value):
        return label, None
    return label, fmt % float(value)


PHASE_WORDS = {
    # Only shown with LANDING_PHASE on.
    "landing": ("Landing", "How the landing was flown: how firmly the "
                "aircraft touched down, how far down the runway, and how long "
                "it floated, blended together - never more than 10 points above "
                "the touchdown alone, so precision can't rescue a hard landing. "
                "Arriving crooked can only lower it."),
    "liftoff": ("Lift-off", "Leaving the ground until the aircraft is "
                            "flying away."),
    "climb": ("Climb", "From transition until level at the top of the climb."),
    "cruise": ("Cruise", "The level middle of the leg, however long that is."),
    "descent": ("Descent", "Leaving the cruise through to touchdown, "
                           "including the approach and the landing itself."),
}


# ------------------------------------------------------------------ profiles

# good: at or better than this scores 100. bad: at or worse scores 0.
# Where a phase's thresholds come from, cited once per phase rather than once
# per metric. A reader who wants to check a number should be able to find the
# document it came out of without taking anyone's word for it, and the phase is
# the smallest unit where the answer is the same for everything in it.
ROTARY_SOURCES = {
    "liftoff": "Calibrated against real helicopter flights rather than a "
               "published standard - these are the only bands here that were.",
    "climb":   "Calibrated against real helicopter flights.",
    "cruise":  "Calibrated against real helicopter flights.",
    "descent": "Calibrated against real helicopter flights. The vortex "
               "ring condition - sinking faster than 300 fpm below 30 kt - is "
               "the standard rotorcraft avoidance envelope.",
}

# The bank bands come from the FAA taxonomy and from the lateral g a bank
# actually produces, and from nothing else. An earlier version was justified
# by the spread it produced across one person's flights, which was the wrong
# test twice over: one pilot's habits are not a sample of how aircraft are
# flown, and narrowing a band to manufacture a spread is the thing this file
# tells you not to do. Full marks through
# shallow, zero where a turn stops being one anybody would fly on purpose -
# 45 degrees for a light airplane, which is the handbook's cap for the turn to
# final, and 35 for a transport, where GPWS starts alerting. For scale, 30
# degrees is 1.15 g and 45 is 1.41 g.
WING_SOURCES_LIGHT = {
    "liftoff": "Bank: FAA Airplane Flying Handbook (FAA-H-8083-3C), which "
               "calls a turn shallow below about 20 degrees and medium from "
               "20 to 45, and says the turn to final should not exceed a "
               "medium bank.",
    "climb":   "Bank: FAA Airplane Flying Handbook, shallow / medium / steep "
               "as above.",
    "cruise":  "Bank: FAA Airplane Flying Handbook, shallow / medium / steep "
               "as above.",
    "descent": "Descent angle and the 500 ft gate: general-aviation "
               "stabilized-approach guidance - a normal descent is 500 to "
               "1000 fpm and the approach is expected to be stable by 500 ft "
               "AGL in VMC. Touchdown: 14 CFR 23.473, which builds a light "
               "airplane for a landing descent velocity of 7 to 10 ft/s, or "
               "420 to 600 fpm - so an F above 500 fpm is the structural "
               "design case rather than an opinion.",
}

WING_SOURCES_TRANSPORT = {
    "liftoff": "Bank: GPWS bank-angle alerting, which begins at 35 degrees.",
    "climb":   "Bank: GPWS bank-angle alerting, from 35 degrees.",
    "cruise":  "Bank: GPWS bank-angle alerting, from 35 degrees.",
    "descent": "Stabilized approach: Flight Safety Foundation ALAR Briefing "
               "Note 7.1 - stable by 1000 ft IMC or 500 ft VMC, sink not "
               "above 1000 fpm. Touchdown: published airline targets of 100 "
               "to 250 fpm, which is why full marks span a band here rather "
               "than sitting at the gentlest possible arrival - a positive "
               "touchdown spins the wheels up for the autobrakes and the "
               "spoilers and cuts through standing water, and floating one "
               "on is the fault at the other end. Zero at 600 fpm is 10 ft/s, "
               "the limit descent velocity 14 CFR 25.473 builds a transport "
               "aircraft for at design landing weight.",
}

# The g bands are the same numbers on every profile and came from neither a
# regulator nor from measured flights, so they are not claimed as either.
G_BAND_NOTE = ("The vertical and fore/aft g bands are not from a published "
               "standard. They are what a passenger feels, set so ordinary "
               "flying lands mid-scale, and they are the same on every "
               "profile.")

ROTARY_WORDS = {
    "phase_blurbs": {
        "liftoff": "Leaving the ground until the aircraft is flying away - "
                   "the hover and the transition.",
    },
    "why": {
        "descent_angle_deg":
            "How steep the descent path was. A helicopter avoids vortex ring "
            "state entirely on paths shallower than about 30 degrees, so a "
            "steep let-down is both uncomfortable and worth avoiding.",
        "gate_500_vs_fpm":
            "Rate of descent crossing 500 ft above the landing site - the "
            "stabilized-approach gate, borrowed from fixed wing and adapted.",
    },
}

WING_WORDS = {
    "phase_blurbs": {
        "liftoff": "The takeoff roll, rotation and initial climb, until the "
                   "aircraft is climbing away.",
    },
    "why": {
        "descent_angle_deg":
            "How steep the descent path was. A normal approach is about three "
            "degrees; steeper than that means arriving high or fast and having "
            "to lose it late.",
        "gate_500_vs_fpm":
            "Rate of descent crossing 500 ft above the landing site, the point "
            "an approach is expected to be stable by. Sink beyond this is the "
            "classic reason to go around.",
    },
}


ROTARY = {
    "name": "rotary",
    "label": "Helicopter",

    # Lift-off is its own phase. Hovering, translating and climbing away have
    # genuinely different envelopes, and folding them into the climb graded a
    # hover against climb-out expectations.
    "liftoff": {
        "vert_accel_g": (0.02, 0.25),
        "long_accel_g": (0.06, 0.45),
        "bank_p95_deg": (8.0, 32.0),
        "weights": {"vert_accel_g": 0.40, "long_accel_g": 0.30,
                    "bank_p95_deg": 0.30},
    },
    "climb": {
        "vert_accel_g": (0.02, 0.22),
        "long_accel_g": (0.05, 0.42),
        "bank_p95_deg": (6.0, 30.0),
        "weights": {"vert_accel_g": 0.40, "long_accel_g": 0.25,
                    "bank_p95_deg": 0.35},
    },
    "cruise": {
        "vert_accel_g": (0.02, 0.25),
        "long_accel_g": (0.02, 0.20),
        "bank_p95_deg": (6.0, 30.0),
        "weights": {"vert_accel_g": 0.40, "long_accel_g": 0.30,
                    "bank_p95_deg": 0.30},
    },
    "descent": {
        # Judged on the approach and on the path flown, not on a bare rate of
        # descent. A helicopter's hazard is vortex ring state - sinking faster
        # than about 300 fpm below roughly 30 kt - and a plain fpm threshold
        # gets that exactly backwards: it passes a 600 fpm vertical let-down,
        # which is the dangerous case, and fails a 1200 fpm descent at 120 kt,
        # which is routine.
        # Measured over 15 real descents: approach g runs a median of
        # 0.046, descent angle 10 degrees, vortex-ring exposure 6% and sink
        # at the gate 456 fpm. Bands set so that typical flying sits high
        # and the loose end of the corpus separates.
        "approach_accel_g": (0.030, 0.15),
        "vrs_exposure_pct": (3.0, 40.0),
        "descent_angle_deg": (10.0, 35.0),
        "gate_500_vs_fpm": (350.0, 1400.0),
        "bank_p95_deg": (6.0, 30.0),
        # Same reasoning as the wing profiles: the approach turn is felt,
        # and it was the one turn in the leg nothing scored.
        "weights": {"touchdown": 0.45, "bank_p95_deg": 0.12,
                    "approach_accel_g": 0.13, "vrs_exposure_pct": 0.13,
                    "descent_angle_deg": 0.09, "gate_500_vs_fpm": 0.08},
    },

    # The landing end carries the most: it is the most measurable and the most
    # remembered. Lift-off is short and carries the least.
    "phase_weights": {"liftoff": 0.10, "climb": 0.20,
                      "cruise": 0.25, "descent": 0.45},

    # Segmentation.
    "alt_smooth_s": 15.0,
    "min_climb_ft": 150.0,
    "phase_frac": 0.85,
    # Lift-off ends at whichever comes first: flying away, or clear of the site.
    "transition_kt": 40.0,
    "liftoff_top_ft": 200.0,
    # The approach is the last 1000 ft above the landing site, measured by
    # HEIGHT rather than by a stopwatch: 30 seconds is a different place on a
    # steep approach than on a shallow one, and height is what the published
    # criteria are written in.
    "approach_gate_ft": 1000.0,
    "stabilized_gate_ft": 500.0,
    # Vortex ring state: sinking faster than this, slower than this.
    "vrs_vs_fpm": 300.0,
    "vrs_ias_kt": 30.0,
    # Below this speed a descent angle says nothing - a vertical descent is 90
    # degrees whatever the rate - so the angle metric skips it and the
    # vortex-ring metric covers that case instead.
    "angle_min_gs_kt": 12.0,
    # A phase shorter than this has too little in it to describe.
    "min_phase_s": 15.0,
}
ROTARY.update(ROTARY_WORDS)
ROTARY["phase_sources"] = ROTARY_SOURCES
# Not because a helicopter cannot land badly sideways - drift on touchdown is
# how dynamic rollover starts - but because there is no data. Every AS365
# landing without the accelerations would score as a perfect
# alignment to twelve landings nobody measured.
ROTARY["alignment"] = False

FIXED_WING = {
    "name": "fixed_wing",
    "label": "Jet / transport",
    # NOT calibrated against measured flights, unlike rotary. These come
    # from published criteria, and the panel says so rather than letting the
    # numbers imply an authority they have not earned:
    #   stabilized approach - 1000 ft IMC / 500 ft VMC, sink not above 1000 fpm,
    #     speed +10/-5 kt          (Flight Safety Foundation, ALAR 7.1)
    #   bank - GPWS alerts from 35 degrees   (SKYbrary, Bank Angle Awareness)
    #   hard landing - 1.5 to 1.9 g and around 600 fpm, Boeing and Airbus
    # The g bands are physics and carry over from rotary unchanged.
    #
    # These are transport criteria and they stay that way. A light single flown
    # against them scores 100 on bank and on the 500 ft gate every time, which
    # is why LIGHT_GA exists below rather than these being loosened.
    "calibrated": False,
    "sources": ("airline practice - Flight Safety Foundation stabilized "
                "approach gates, GPWS bank alerting from 35 degrees, and "
                "Boeing and Airbus hard-landing triggers"),
    # Rotation, not vertical g. See ROTATION_BAND_NOTE.
    "liftoff": {
        "rotation_g": (0.45, 0.90),
        "long_accel_g": (0.08, 0.45),
        "bank_p95_deg": (20.0, 35.0),
        "weights": {"rotation_g": 0.40, "long_accel_g": 0.30,
                    "bank_p95_deg": 0.30},
    },
    "climb": {
        "vert_accel_g": (0.02, 0.25),
        "long_accel_g": (0.05, 0.40),
        "bank_p95_deg": (20.0, 35.0),
        "weights": {"vert_accel_g": 0.40, "long_accel_g": 0.25,
                    "bank_p95_deg": 0.35},
    },
    "cruise": {
        "vert_accel_g": (0.02, 0.25),
        "long_accel_g": (0.02, 0.20),
        "bank_p95_deg": (20.0, 35.0),
        "weights": {"vert_accel_g": 0.40, "long_accel_g": 0.30,
                    "bank_p95_deg": 0.30},
    },
    "descent": {
        # No vortex ring state: a wing cannot have it. Three degrees is the
        # glidepath, and the gate follows the 1000 fpm stabilized limit.
        "approach_accel_g": (0.02, 0.15),
        "descent_angle_deg": (3.5, 9.0),
        "gate_500_vs_fpm": (500.0, 1200.0),
        "bank_p95_deg": (20.0, 35.0),
        # Bank belongs here as much as anywhere: in a circuit the steepest
        # turn of the whole leg is base to final, and it sits in this phase.
        # It was measured for the descent all along and then thrown away,
        # so the gentle crosswind turn was scored and the firm one was not.
        # Touchdown keeps its 45; the 12 comes off the other three.
        "weights": {"touchdown": 0.45, "bank_p95_deg": 0.12,
                    "approach_accel_g": 0.17, "descent_angle_deg": 0.14,
                    "gate_500_vs_fpm": 0.12},
    },
    "phase_weights": {"liftoff": 0.10, "climb": 0.20,
                      "cruise": 0.25, "descent": 0.45},
    "alt_smooth_s": 15.0,
    "min_climb_ft": 150.0,
    "phase_frac": 0.85,
    # A wing has no hover to leave, so lift-off ends on height, not on speed.
    "transition_kt": 1e9,
    "liftoff_top_ft": 400.0,
    # Measured on every airplane. See FLOAT_NORMAL_S.
    "float": True,
    # ...or once it has lasted min_phase_s, whichever is later. Height alone
    # left a brisk jet climb-out too short to grade. See split_phases.
    "liftoff_fills_min_phase": True,
    "approach_gate_ft": 1000.0,
    "stabilized_gate_ft": 500.0,
    "vrs_vs_fpm": 300.0,
    "vrs_ias_kt": 30.0,
    "angle_min_gs_kt": 25.0,
    "min_phase_s": 15.0,
}
FIXED_WING.update(WING_WORDS)
FIXED_WING["phase_sources"] = WING_SOURCES_TRANSPORT
# Same bands as the light profile, which is a placeholder and is labelled one:
# no transport landing has ever been recorded here, and a jet has far
# less bank available before a pod or a tip touches. Narrowing it without a
# source would be inventing a number.
FIXED_WING["alignment"] = True

PROFILES = {"rotary": ROTARY, "fixed_wing": FIXED_WING}
PHASE_ORDER = ("liftoff", "climb", "cruise", "descent")

# The landing as a phase of its own, rather than 45% of the descent.
#
# Off, the touchdown does two jobs: it sets the landing letter, and it is the
# heaviest measure in the descent phase - so the Grading tab listed it twice,
# and the descent's score mixed how the approach was flown with how the
# wheels met the runway. On, the descent is the approach - its other
# measures, in the same proportions to each other - and "landing" is a phase:
# the touchdown score, held down by alignment, the float and the touchdown
# point. The descent's weight in the leg is split between the two by the
# touchdown's own share of it (landing_phase_weights), so a leg with every
# measure present averages exactly as before. What moves is the cap: a leg is
# never more than one band above its weakest phase, and approach and landing
# are now judged separately. On since the owner reviewed which legs moved:
# grades moved down and never up.
LANDING_PHASE = True

# How the landing phase scores. The A-F landing letter is how the touchdown
# felt, and stays the touchdown rate's, with alignment, the float and the
# touchdown spot able only to lower it. The landing PHASE is how the landing
# was flown, and the pilot standards treat touching down near the aim point
# with little float as a main criterion beside a smooth arrival - not merely
# a thing not to get wrong. So the phase blends them, each counted only when
# its Settings switch is on.
#
# The weights are a judgment: nothing published weights these against each
# other. The touchdown carries half. The touchdown spot carries most of the
# rest. The float carries least, because a long float and a long touchdown
# are mostly one mistake, and weighting both fully would count it twice.
LANDING_WEIGHTS = {"touchdown": 0.50, "touchdown_point": 0.35, "float": 0.15}
# The phase never sits more than one band above the touchdown's own score:
# precision can lift a firm landing a little, and cannot rescue a hard one.
LANDING_LEAD_POINTS = 10.0
# And the mirror of that: past the end of the touchdown zone - where the
# touchdown spot or float scores below this - landing long is a fault the
# standards say to go around for, not a matter of degree. There it also caps
# the landing at its own score plus one band, so a soft touchdown cannot
# average away a landing a third of the way down the runway. Inside the zone
# it is weighed, not capped. Measured, the blend without this let a soft
# touchdown well past the zone average up to a C.
LANDING_FAULT_SCORE = 50.0

# Below this stall speed, an aircraft the sim calls an Airplane is not one in
# any sense these thresholds understand. A C172 reports 40 kt. A Magni M24
# gyroplane reports 10, while otherwise looking identical to the Cessna:
# CATEGORY "Airplane", piston, a stall alpha, and a wing area of 181 sq ft for
# an aircraft with no wing, because the sim models the rotor disc as one.
FIXED_WING_MIN_VS0_KT = 25.0

# What a gyroplane, and anything else unclassified, is graded on. The base is
# fixed wing - the sim calls it an Airplane and it needs a takeoff roll, which
# is what decides the segmentation - with the two metrics that depend on a
# fixed-wing number removed, rather than scored against one known not to apply.
# A gyro descends steeply and approaches at a third of a Cessna speed; judging
# that against a three degree glidepath would fail an approach flown perfectly.
#
# Blending thresholds from both profiles per metric was considered and
# rejected: it invents a third profile with no source of authority, which is
# the same fault as inventing one wholesale, only harder to notice.
UNCLASSIFIED_SUPPRESS = ("descent_angle_deg", "gate_500_vs_fpm")


def _unclassified():
    """Fixed wing, minus what cannot be judged without a real profile."""
    p = dict(FIXED_WING)
    p["name"] = "unclassified"
    p["label"] = "Other / unclassified"
    # A gyroplane lands with next to no roll and no flare to speak of; the
    # air-distance standard is written for airplanes.
    p["float"] = False
    d = dict(FIXED_WING["descent"])
    d["weights"] = {k: v for k, v in d["weights"].items()
                    if k not in UNCLASSIFIED_SUPPRESS}
    p["descent"] = d
    # The transport descent cites the stabilized-approach gates, and this
    # profile is the one that does not score them - so citing them here would
    # point at a standard nothing on the tab is measured against.
    src = dict(FIXED_WING.get("phase_sources") or {})
    src["descent"] = ("Touchdown: industry hard-landing triggers, around "
                      "600 fpm and 1.5 to 1.9 g. The stabilized-approach "
                      "measures are not scored on this profile, so no "
                      "approach standard applies to it.")
    p["phase_sources"] = src
    return p


# Light airplanes: FAR 23 singles and small twins, which is most of what a
# simulator logbook contains. Split from the transport profile because the
# criteria that profile is built on are for airliners, and a Cessna flown well
# needs criteria appropriate to its class.
#
# The split is at VS0 61 kt because that is where the regulation splits:
# 14 CFR 23.49 caps VS0 at 61 knots for single-engine airplanes and light
# twins. It is not a number picked to fit any particular logbook, and VS0 is
# already recorded on every flight.
#
# Sourced, not tuned:
#   bank - FAA Airplane Flying Handbook calls a turn shallow below about 20
#     degrees and medium from 20 to 45, and says the turn to final should not
#     exceed a medium bank. Full marks for an unhurried shallow turn, zero at
#     30, which is the steepest a light airplane should see in a pattern.
#   gate - GA stabilized approach guidance puts a normal descent at 500 to
#     1000 fpm with sink not to exceed 1000, stabilized by 500 ft AGL in VMC.
#     A three degree path at 70 kt is about 370 fpm, so full marks at 400 and
#     zero at the 1000 fpm limit rather than the airliner's 1200.
#   touchdown - unchanged, and the ladder turns out to sit where the
#     certification basis does: 14 CFR 23.473 requires a descent velocity of
#     7 to 10 ft/s, which is 420 to 600 fpm, so an F above 500 fpm is the
#     structural design case rather than an opinion.
#
# NOT tuned: the g bands are the transport and rotary ones unchanged. A light
# airplane measured 0.02 to 0.06 g on every axis and phase where a helicopter
# spread 0.02 to 0.22, and those metrics currently score near 100 - but that
# is a light airplane being genuinely smoother than a helicopter, not a
# threshold from the wrong class of aircraft. Narrowing them to manufacture a
# spread would be tuning grades to look busier, which is the opposite of
# measuring. They stay until there are enough flights to say otherwise.
LIGHT_GA = dict(
    FIXED_WING,
    name="light_ga",
    label="Light airplane",
    calibrated=False,
    sources=("light-airplane sources - the FAA Airplane Flying Handbook on "
             "shallow, medium and steep turns and on not exceeding a medium "
             "bank turning final; general-aviation stabilized approach "
             "guidance, which puts a normal descent at 500 to 1000 fpm and "
             "asks for stability by 500 ft; and 14 CFR 23.473, which sets the "
             "landing descent velocity a light airplane is built for at 7 to "
             "10 ft/s, or 420 to 600 fpm"),
    # A light airplane rotates at the same 3 deg/s but from a much lower
    # speed, and load factor scales with speed - 60 kt gives 0.16 g where
    # 130 kt gives 0.36 g. Handing it the transport band would mean every
    # rotation scored full marks, which measures nothing. See
    # ROTATION_BAND_NOTE.
    liftoff=dict(FIXED_WING["liftoff"], bank_p95_deg=(20.0, 45.0),
                 rotation_g=(0.20, 0.40)),
    climb=dict(FIXED_WING["climb"], bank_p95_deg=(20.0, 45.0)),
    cruise=dict(FIXED_WING["cruise"], bank_p95_deg=(20.0, 45.0)),
    descent=dict(FIXED_WING["descent"],
                 descent_angle_deg=(3.5, 7.0),
                 gate_500_vs_fpm=(400.0, 1000.0),
                 # Same band the light profile uses everywhere else, so an
                 # approach turn is judged on the scale the phase before it
                 # was judged on.
                 bank_p95_deg=(20.0, 45.0)),
    phase_sources=WING_SOURCES_LIGHT,
    # The only profile with any measured alignment data behind it.
    alignment=True,
)

# Where the regulation splits a light airplane from everything heavier.
LIGHT_GA_MAX_VS0_KT = 61.0


UNCLASSIFIED = _unclassified()

# Set AFTER the derived profiles are built. LIGHT_GA and UNCLASSIFIED both
# copy FIXED_WING["descent"], so putting this on it any earlier would hand the
# transport band to a Cessna.
FIXED_WING["touchdown_curve"] = TOUCHDOWN_CURVE_TRANSPORT
FIXED_WING["descent"]["touchdown_band"] = TOUCHDOWN_BAND_TRANSPORT
for _derived in ("LIGHT_GA", "UNCLASSIFIED"):
    _p = globals()[_derived]
    assert "touchdown_curve" not in _p, (
        "%s inherited the transport touchdown curve; it is built by copying "
        "FIXED_WING, so anything set on FIXED_WING before this point is "
        "handed to a Cessna" % _derived)
    assert "touchdown_band" not in _p["descent"], (
        "%s inherited the transport touchdown band" % _derived)
del _derived, _p


def profile_for(aircraft=None, category=None, vs0_kt=None):
    """Which thresholds to grade against.

    CATEGORY is the sim own answer and is the primary switch, confirmed against
    three aircraft: the AS365 reports "Helicopter", the C172 "Airplane", and a
    Magni M24 gyroplane also "Airplane". So CATEGORY alone is not enough - it
    files a gyro with the Cessnas - and the stall speed separates them.

    Nothing is guessed from the aircraft title. Title matching is a maintenance
    treadmill and silently misfiles anything not on the list.
    """
    if category == "Helicopter":
        return ROTARY
    if category == "Airplane":
        # Stall speed sorts the three kinds of "Airplane" the sim reports.
        # Below 25 kt is not an airplane at all - a gyroplane files itself
        # here. Up to 61 kt is a FAR 23 light single or small twin, which is
        # where 14 CFR 23.49 draws the line. Above that is everything heavier,
        # graded on the transport criteria those numbers came from.
        if vs0_kt is None:
            return FIXED_WING
        vs0 = float(vs0_kt)
        if vs0 < FIXED_WING_MIN_VS0_KT:
            return UNCLASSIFIED
        if vs0 <= LIGHT_GA_MAX_VS0_KT:
            return LIGHT_GA
        return FIXED_WING
    # No category recorded: the caller infers one from the track instead.
    # Rotary is the default only because every flight predating
    # the category being recorded was a helicopter.
    return ROTARY


# ------------------------------------------------------------------ helpers


def _finite(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return f == f and f not in (float("inf"), float("-inf"))


def _curve(x, knots):
    """Piecewise-linear lookup, clamped at both ends."""
    if x <= knots[0][0]:
        return knots[0][1]
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * ((x - x0) / (x1 - x0))
    return knots[-1][1]


def _band(value, good, bad):
    """100 when value is at or better than good, 0 at or worse than bad."""
    if value is None or not _finite(value):
        return None
    v = float(value)
    if bad == good:
        return 100.0 if v <= good else 0.0
    if v <= good:
        return 100.0
    if v >= bad:
        return 0.0
    return 100.0 * (bad - v) / (bad - good)


def _pct(values, q):
    if not values:
        return None
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _stdev(values):
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / n)


ROTATION_BAND_NOTE = """Why the rotation band is where it is.

The liftoff phase used to score vertical g on the same band as cruise - full
marks at 0.03 g, zero at 0.30 g. That band came from ride-quality work, where
the passenger discomfort threshold sits near 0.06 g. But those figures are for
VIBRATION: frequency-weighted, per ISO 2631, describing turbulence and buffet.
A rotation is not vibration. It is a deliberate manoeuvre, and pulling g is
what it IS.

Published technique asks for a rotation of about 2.8 to 3 degrees per second.
The incremental load factor that produces follows from the geometry:

    dn = V * q / g

which for the documented case of 2.8 deg/s at 120 kt gives 0.31 g - matching
the 0.3 g that case reports, so the relation is sound rather than assumed. Run
across the classes this app grades:

    transport   130 kt @ 3 deg/s   0.36 g
    light jet    90 kt @ 3 deg/s   0.25 g
    light GA     60 kt @ 3 deg/s   0.16 g

So a rotation flown exactly as published scored ZERO on the old band, and
anything gentler than a 38-second rotation was marked down. The band was not
merely tight; a correct rotation could not pass it.

Full marks now cover a correctly flown rotation for the class - 0.45 g for
transport and light jets, which spans 90 to 160 kt at the published rate, and
0.20 g for light airplanes. Zero sits at twice that, which is twice the
published pitch rate: the same guidance calls for a controlled transition and
warns against a sharp pull, so double the recommended rate is the point at
which the technique is no longer the technique.

Uncalibrated, like everything outside ROTARY. These are published criteria and
geometry, not measurements of anyone's flying.

ROTARY keeps vertical g on its old band and does not score rotation at all. A
helicopter does not rotate, the rotary bands are the only calibrated ones in
this file, and they were calibrated against the derived figure - swapping the
input would move every helicopter grade ever awarded."""


def rotation_load_factor_g(seg):
    """Peak load factor above 1 g through the rotation, as the sim reports it.

    Read from G FORCE rather than differentiated out of vertical speed. The
    derived figure understates - it is a 1 Hz difference of a rate, and it
    misses everything that happens between samples - and for this one metric
    the understatement matters, because the band below is anchored on a
    published load factor and the two have to be the same quantity.

    Peak, not p95: a rotation is one event a few seconds long, and the whole
    question is how hard that one pull was. A percentile over the phase would
    average it away against the taxi and the initial climb around it.

    None when the track carries no G FORCE. A rotation that was never
    measured must not be scored, and must certainly not score well by
    default.
    """
    peaks = []
    for p in seg:
        for key in ("gforce_max", "gforce_min", "gforce"):
            v = p.get(key)
            if _finite(v):
                peaks.append(abs(float(v) - 1.0))
    return max(peaks) if peaks else None


def _accel_p95_g(seg, key, per_unit_fts, only=None):
    """Peak acceleration in g, from the rate of change of a recorded rate.

    p95 rather than the mean: one firm push is what gets remembered, and an
    average over a long quiet phase buries it.

    per_unit_fts converts recorded units to ft/s - vertical speed is fpm,
    ground speed is knots - so both come out in g and read against the same
    intuition. Roughly: 0.05 g noticeable, 0.15 g a firm push, 0.3 g and up
    grab-the-seat.
    """
    vals = []
    for a, b in zip(seg, seg[1:]):
        if not (_finite(a.get("t")) and _finite(b.get("t"))):
            continue
        dt = float(b["t"]) - float(a["t"])
        if not (0.2 < dt < 5.0):
            continue
        if only is not None and not only(b):
            continue
        if not (_finite(a.get(key)) and _finite(b.get(key))):
            continue
        d = (float(b[key]) - float(a[key])) * per_unit_fts
        vals.append(abs(d / dt) / 32.174)
    return _pct(vals, 0.95)


def derived_bank_deg(a, b):
    """Bank implied by the turn between two track points, or None.

    Bank is not recorded outside clips. In a coordinated turn it follows from
    the turn rate and the speed, and that combination is what is felt.
    """
    if not (_finite(a.get("t")) and _finite(b.get("t"))):
        return None
    dt = float(b["t"]) - float(a["t"])
    if not (0.2 < dt < 5.0):
        return None
    if not (_finite(a.get("heading")) and _finite(b.get("heading"))):
        return None
    v = (float(a.get("gs")) if _finite(a.get("gs")) else 0.0) * 0.514444
    if v < 5.0:                      # hovering: heading means nothing here
        return None
    dh = ((float(b["heading"]) - float(a["heading"]) + 180.0) % 360.0) - 180.0
    omega = math.radians(dh) / dt
    return math.degrees(math.atan(omega * v / 9.80665))


def bank_deg(a, b):
    """Bank between two track points: measured if it was recorded, else implied.

    Prefer recorded bank; infer it from the turn only when absent. The
    inferred figure is close enough to grade a leg on when the recorded
    value is missing, and not close enough to prefer when it is there.
    """
    v = b.get("bank")
    if _finite(v):
        return float(v)
    return derived_bank_deg(a, b)


def height_ft(p, land_alt):
    """Height above the ground, measured if it was recorded.

    Otherwise the altitude less the landing site elevation, which is only the
    same thing over flat ground - the approach gates sit at 500 and 1000 feet,
    and terrain relief on the way in goes straight into that number.
    """
    v = p.get("agl")
    if _finite(v):
        return float(v)
    if land_alt is None or not _finite(p.get("alt")):
        return None
    return float(p["alt"]) - land_alt


def descent_angle_deg(p, min_gs_kt):
    """Angle of the descent path in degrees, or None if it means nothing.

    Below min_gs_kt the aircraft is descending more or less vertically and the
    angle saturates near 90 whatever the rate, which says nothing useful. That
    case is covered by the vortex-ring metric instead.
    """
    if not (_finite(p.get("vs")) and _finite(p.get("gs"))):
        return None
    gs = float(p["gs"])
    if gs < min_gs_kt:
        return None
    vs = float(p["vs"])
    if vs >= 0:
        return 0.0
    horiz_fpm = gs * 101.269        # knots to feet per minute
    return math.degrees(math.atan(abs(vs) / horiz_fpm))


def _speed_kt(p):
    """Airspeed if it was recorded, else ground speed."""
    spd = p.get("airspeed")
    if _finite(spd):
        return float(spd)
    spd = p.get("gs")
    return float(spd) if _finite(spd) else None


# ------------------------------------------------------------------ phases


def _smooth_alt(track, air, window_s):
    """Altitude with the noise taken out, so a wobble is not a descent."""
    out = {}
    for idx, i in enumerate(air):
        t = track[i].get("t")
        if not _finite(t):
            continue
        vals = []
        for j in air[max(0, idx - 30):idx + 31]:
            tj, aj = track[j].get("t"), track[j].get("alt")
            if _finite(tj) and _finite(aj) and abs(float(tj) - float(t)) <= window_s:
                vals.append(float(aj))
        if vals:
            out[i] = sum(vals) / len(vals)
    return out


def split_phases(track, profile):
    """Index ranges for lift-off, climb, cruise and descent in one leg.

    Segmented on the altitude PROFILE, not on instantaneous vertical speed.
    Keying off vertical speed assumes an airliner - climb, level, descend - and
    a helicopter lifts, hovers, climbs, pauses and climbs again. Every pause
    ended the climb, so cruise swallowed the whole leg.

    Lift-off is split out because hovering and translating are not climbing: it
    ends when the aircraft is flying away (transition speed) or is clear of the
    site, whichever comes first.
    """
    air = [i for i, p in enumerate(track) if p.get("on_ground") is False]
    if len(air) < 5:
        return {}
    lo, hi = air[0], air[-1]
    sm = _smooth_alt(track, air, profile["alt_smooth_s"])
    if len(sm) < 5:
        return {}

    base = sm[air[0]]
    top = max(sm.values())
    gain = top - base
    if gain < profile["min_climb_ft"]:
        # It never really went anywhere: no climb or descent worth naming.
        return {"cruise": (lo, hi)}

    mark = base + gain * profile["phase_frac"]
    up = [i for i in air if sm.get(i, base) >= mark]
    if not up:
        return {"cruise": (lo, hi)}
    climb_end, descent_start = up[0], up[-1]

    lift_end = lo
    for i in air:
        if i >= climb_end:
            break
        spd = _speed_kt(track[i])
        agl = sm.get(i, base) - base
        lift_end = i
        if (spd is not None and spd >= profile["transition_kt"]) \
                or agl >= profile["liftoff_top_ft"]:
            break

    # A lift-off bounded only by height is shortest when it is flown best. A
    # jet that climbs away briskly reaches 400 ft inside min_phase_s and was
    # then refused a grade as "too short" - so the cleaner the departure, the
    # likelier it went unmarked. Where the profile asks for it, the phase runs
    # on until it is long enough to grade, but never into the climb proper.
    #
    # It can only lengthen a lift-off that was already under the minimum. One
    # that reached its height after min_phase_s ends exactly where it did.
    if profile.get("liftoff_fills_min_phase") and _finite(track[lo].get("t")):
        t_lo = float(track[lo]["t"])
        for i in air:
            if i <= lift_end:
                continue
            if i >= climb_end:
                break
            if not _finite(track[lift_end].get("t")) or \
                    float(track[lift_end]["t"]) - t_lo >= profile["min_phase_s"]:
                break
            lift_end = i

    out = {"descent": (descent_start, hi)}
    if lift_end > lo:
        out["liftoff"] = (lo, lift_end)
        if climb_end > lift_end + 1:
            out["climb"] = (lift_end, climb_end)
    else:
        out["climb"] = (lo, climb_end)
    if descent_start > climb_end + 1:
        out["cruise"] = (climb_end, descent_start)
    return out


def _span_s(track, rng):
    a, b = rng
    if not (_finite(track[a].get("t")) and _finite(track[b].get("t"))):
        return 0.0
    return float(track[b]["t"]) - float(track[a]["t"])


def infer_category(track, min_hover_s=10.0, slow_kt=15.0):
    """Guess the aircraft type from how the leg was flown.

    For a leg whose track carries no CATEGORY. A helicopter hovers
    and an airplane physically cannot, so the longest airborne stretch below
    15 kt separates them. Measured across the recorded legs, all
    helicopter, the shortest was 55 s and the median 79 s - so a 10 s threshold
    is not a close call.

    Returns a CATEGORY string so callers treat it exactly like the recorded
    value, or None when there is not enough airborne data to say.
    """
    best = run = 0.0
    prev = None
    airborne = 0.0
    for p in track:
        if prev is not None:
            dt = (p.get("t") or 0) - (prev.get("t") or 0)
            if 0 < dt < 5.0:
                if p.get("on_ground") is False:
                    airborne += dt
                    if (p.get("gs") or 0.0) < slow_kt:
                        run += dt
                        best = max(best, run)
                    else:
                        run = 0.0
                else:
                    run = 0.0
        prev = p
    if airborne < 60.0:
        return None
    return "Helicopter" if best >= min_hover_s else "Airplane"


def landing_elevation(track):
    """Altitude of the landing site, so heights can be measured above it."""
    for p in reversed(track):
        if p.get("on_ground") is True and _finite(p.get("alt")):
            return float(p["alt"])
    for p in reversed(track):
        if _finite(p.get("alt")):
            return float(p["alt"])
    return None


# ------------------------------------------------------------------ metrics


def _slice_metrics(track, rng, profile, phase=None, land_alt=None):
    """Raw measurements for one phase."""
    a, b = rng
    seg = track[a:b + 1]
    if len(seg) < 3:
        return None
    banks = []
    for x, y in zip(seg, seg[1:]):
        d = bank_deg(x, y)
        if d is not None:
            banks.append(abs(d))

    m = {
        "bank_p95_deg": _pct(banks, 0.95),
        # Vertical speed is fpm and ground speed is knots; both become g.
        "vert_accel_g": _accel_p95_g(seg, "vs", 1.0 / 60.0),
        "long_accel_g": _accel_p95_g(seg, "gs", 1.68781),
    }
    if phase == "liftoff":
        m["rotation_g"] = rotation_load_factor_g(seg)
    if phase != "descent":
        return m

    angles = [descent_angle_deg(p, profile["angle_min_gs_kt"]) for p in seg]
    angles = [x for x in angles if x is not None]
    if angles:
        m["descent_angle_deg"] = _pct(angles, 0.95)

    # Vortex ring: sinking hard, slowly. Counted as a share of the descent so a
    # long gentle let-down is not diluted by its own length.
    risky = total = 0
    for p in seg:
        spd = _speed_kt(p)
        if spd is None or not _finite(p.get("vs")):
            continue
        total += 1
        if float(p["vs"]) <= -profile["vrs_vs_fpm"] and spd < profile["vrs_ias_kt"]:
            risky += 1
    if total:
        m["vrs_exposure_pct"] = 100.0 * risky / total

    if land_alt is not None or any(_finite(p.get("agl")) for p in seg):
        gate = profile["approach_gate_ft"]

        def under_gate(p):
            h = height_ft(p, land_alt)
            return h is not None and h <= gate

        m["approach_accel_g"] = _accel_p95_g(seg, "vs", 1.0 / 60.0,
                                             only=under_gate)

        # Sink rate crossing the stabilized gate, coming down through it.
        want = profile["stabilized_gate_ft"]
        prev_h = None
        for p in seg:
            h = height_ft(p, land_alt)
            if h is None or not _finite(p.get("vs")):
                continue
            if prev_h is not None and prev_h > want >= h:
                m["gate_500_vs_fpm"] = abs(float(p["vs"]))
                break
            prev_h = h
    return m


def _score_phase(metrics, spec, extra=None):
    """Weighted mean of whichever metrics could be measured.

    Returns the parts as a list carrying the label and the measurement, not
    just the score - a score of 62 says a phase was mediocre, and the reader
    still wants to know why. Ordered by weight, so the reason a phase scored
    what it did comes first.
    """
    if not metrics:
        return None, []
    scored = []
    for key, weight in spec["weights"].items():
        if key == "touchdown":
            s = (extra or {}).get("touchdown")
            measured = (extra or {}).get("touchdown_fpm")
        else:
            band = spec.get(key)
            measured = metrics.get(key)
            s = _band(measured, band[0], band[1]) if band else None
        if s is not None:
            scored.append((key, s, weight, measured))
    if not scored:
        return None, []
    total = sum(w for _, _, w, _ in scored)
    if total <= 0:
        return None, []
    score = sum(s * w for _, s, w, _ in scored) / total
    scored.sort(key=lambda r: -r[2])
    parts = []
    for key, s, _w, measured in scored:
        label, text = describe_metric(key, measured)
        band = spec.get(key)
        # Touchdown is on a curve rather than a two-point band, and the
        # curve differs by aircraft class, so it words its own.
        parts.append({"key": key, "label": label,
                      "score": round(s, 1), "measured": text,
                      "weight_pct": round(_w * 100 / total),
                      "band": (touchdown_band_text(spec.get("_profile"))
                               if key == "touchdown"
                               else band_text(key, band))})

    # Alignment is a CAP, not a weight, and it is listed here because that is
    # where a reader looks for it. Any weight on it would pay out the gap
    # between the rollout and the touchdown - measured, that is
    # w * (alignment - touchdown), which pays MOST where the touchdown was
    # worst. Measured, it upgraded a hard landing by a whole band. So it can
    # take points off this phase and can never add any.
    al = (extra or {}).get("alignment")
    if al and al.get("score") is not None:
        ceiling = al["score"] + ALIGNMENT_CAP_POINTS
        held = score is not None and ceiling < score
        bits = []
        if al.get("bank_deg") is not None:
            bits.append("%.1f\u00b0 bank" % al["bank_deg"])
        if al.get("scrub_g") is not None:
            bits.append("%.2f g slide" % al["scrub_g"])
        parts.append({
            "key": "alignment",
            "label": "Alignment",
            "score": round(al["score"], 1),
            "measured": ", ".join(bits) or None,
            "weight_pct": 0,
            # No band on the parent: it has two, and printing only
            # the bank one implied slide did not reach the score.
            "band": None,
            "cap": True,
            "held": bool(held),
            "held_to": round(ceiling, 1) if held else None,
            "held_from": round(score, 1) if held else None,
            "sub": _alignment_sub(al),
        })
        if held:
            score = ceiling

    # The float sits beside alignment for the same reason - a cap, never a
    # weight - and is applied after it, so the lower ceiling of the two wins.
    part, score = _float_part((extra or {}).get("float"), score)
    if part:
        parts.append(part)
    part, score = _touchdown_point_part((extra or {}).get("touchdown_point"), score)
    if part:
        parts.append(part)
    return score, parts


# ------------------------------------------------------------------ leg


# How far a rollup may sit above its own worst phase. One grade band.
PHASE_CAP_POINTS = 10.0


# What the ride leaves out: whatever carries the touchdown, which has its own
# sentence. With the landing a phase of its own that is the landing, and the
# descent - now the approach - is flying like any other; left out of the
# ride, an F approach sat under "smooth enough that I forgot to hold on".
def _not_ride():
    return ("landing",) if LANDING_PHASE else ("descent", "landing")


def _ride_score(scored, weights):
    """The in-flight part of the leg: everything except the descent.

    What a passenger calls "the ride" is the cruise and the maneuvering. The
    landing gets its own sentence, so it should not also color this one.

    Capped at one band above the worst in-flight phase, for exactly the reason
    the overall is: an average that hides the worst part is not describing the
    flight anyone was on. Uncapped, a leg with a good lift-off and climb and an
    F cruise averaged to a C, and C is the "ordinary flying" pool - printed
    next to an F CRUISE pill, on the same row.
    """
    keys = [k for k in scored if k not in _not_ride()]
    if not keys:
        return scored.get("descent", scored.get("landing"))
    total = sum(weights[k] for k in keys)
    if total <= 0:
        return None
    mean = sum(scored[k] * weights[k] for k in keys) / total
    return min(mean, min(scored[k] for k in keys) + PHASE_CAP_POINTS)


def landing_phase_weights(profile):
    """(descent, landing) weights in the leg, with the landing a phase.

    The descent's weight split by the touchdown's share of the descent - 45%
    on every profile so far - so the arithmetic of a leg with everything
    measured is the same as with the touchdown inside the descent.
    """
    d = profile["phase_weights"].get("descent", 0.0)
    w = profile["descent"]["weights"]
    total = sum(w.values())
    t = w.get("touchdown", 0.0) / total if total else 0.0
    return d * (1.0 - t), d * t


def phase_weights_for(profile):
    """Each phase's weight in the leg, the landing included when it is one."""
    pw = dict(profile["phase_weights"])
    if LANDING_PHASE:
        pw["descent"], pw["landing"] = landing_phase_weights(profile)
    return pw


def _landing_phase(td_score, landing_rate_fpm, profile, alignment=None,
                   landing_float=None, touchdown_point=None):
    """The touchdown score with the landing limits applied, and its parts.

    What the landing phase is, and what a descent with nothing else to measure
    falls back to. The limits are caps, never weights: each can take points
    off and none can add any, applied alignment, float, touchdown point, so
    the lowest ceiling wins.
    """
    score, parts = td_score, [
        {"key": "touchdown", "label": "Touchdown",
         "score": round(td_score, 1), "weight_pct": 100,
         "band": touchdown_band_text(profile),
         "measured": describe_metric("touchdown", landing_rate_fpm)[1]}]
    if alignment and alignment.get("score") is not None:
        ceiling = alignment["score"] + ALIGNMENT_CAP_POINTS
        held = ceiling < score
        bits = []
        if alignment.get("bank_deg") is not None:
            bits.append("%.1f\u00b0 bank" % alignment["bank_deg"])
        if alignment.get("scrub_g") is not None:
            bits.append("%.2f g slide" % alignment["scrub_g"])
        parts.append({
            "key": "alignment", "label": "Alignment",
            "score": round(alignment["score"], 1),
            "measured": ", ".join(bits) or None, "weight_pct": 0,
            "band": None,
            "cap": True, "held": bool(held),
            "held_to": round(ceiling, 1) if held else None,
            "held_from": round(score, 1) if held else None,
            "sub": _alignment_sub(alignment)})
        if held:
            score = ceiling
    part, score = _float_part(landing_float, score)
    if part:
        parts.append(part)
    part, score = _touchdown_point_part(touchdown_point, score)
    if part:
        parts.append(part)
    return score, parts


def _landing_blend(td_score, landing_rate_fpm, profile, alignment=None,
                   landing_float=None, touchdown_point=None):
    """The landing phase: (score, parts, note).

    A weighted blend of the touchdown, the touchdown spot and the float -
    each of the last two only when measured and switched on - held to no
    more than LANDING_LEAD_POINTS above the touchdown's own score, then
    lowered by alignment if it was crooked. Switched off, a measure is
    listed and shown only; it neither helps nor costs.
    """
    counted = {"touchdown": True,
               "touchdown_point": bool(GRADE_TOUCHDOWN_POINT and touchdown_point
                                       and touchdown_point.get("score") is not None),
               "float": bool(GRADE_FLOAT and landing_float
                             and landing_float.get("score") is not None)}
    scores = {"touchdown": td_score,
              "touchdown_point": (touchdown_point or {}).get("score"),
              "float": (landing_float or {}).get("score")}
    total = sum(LANDING_WEIGHTS[k] for k in counted if counted[k])
    blend = sum(scores[k] * LANDING_WEIGHTS[k] for k in counted if counted[k]) / total
    score = min(blend, td_score + LANDING_LEAD_POINTS)
    note = None
    if blend - score > 0.05:
        # Rounded as the page rounds what it shows beside this - to a
        # tenth, then half up - or the note and the pill disagree by one.
        whole = lambda v: int(math.floor(round(v, 1) + 0.5))
        note = ("Held to %d: a landing can't score more than %d points "
                "above its touchdown alone (%d)."
                % (whole(score), whole(LANDING_LEAD_POINTS), whole(td_score)))

    def share(k):
        return round(LANDING_WEIGHTS[k] * 100 / total) if counted[k] else 0

    parts = [{"key": "touchdown", "label": "Touchdown",
              "score": round(td_score, 1), "weight_pct": share("touchdown"),
              "band": touchdown_band_text(profile),
              "measured": describe_metric("touchdown", landing_rate_fpm)[1]}]
    for key, maker, value in (("touchdown_point", _touchdown_point_part, touchdown_point),
                              ("float", _float_part, landing_float)):
        part, _ignored = maker(value, None)
        if part:
            part.update({"weight_pct": share(key), "cap": False,
                         "counted": counted[key], "held": False,
                         "held_to": None, "held_from": None})
            # Past the zone: a fault, which caps as well as weighing.
            if counted[key] and scores[key] < LANDING_FAULT_SCORE:
                ceiling = scores[key] + ALIGNMENT_CAP_POINTS
                if ceiling < score:
                    part.update({"held": True, "held_to": round(ceiling, 1),
                                 "held_from": round(score, 1)})
                    score = ceiling
            parts.append(part)
    if alignment and alignment.get("score") is not None:
        ceiling = alignment["score"] + ALIGNMENT_CAP_POINTS
        held = ceiling < score
        bits = []
        if alignment.get("bank_deg") is not None:
            bits.append("%.1f\u00b0 bank" % alignment["bank_deg"])
        if alignment.get("scrub_g") is not None:
            bits.append("%.2f g slide" % alignment["scrub_g"])
        parts.append({
            "key": "alignment", "label": "Alignment",
            "score": round(alignment["score"], 1),
            "measured": ", ".join(bits) or None, "weight_pct": 0,
            "band": None,
            "cap": True, "held": bool(held),
            "held_to": round(ceiling, 1) if held else None,
            "held_from": round(score, 1) if held else None,
            "sub": _alignment_sub(alignment)})
        if held:
            score = ceiling
    return score, parts, note


def grade_leg(track, landing_rate_fpm=None, aircraft=None,
              category=None, vs0_kt=None, alignment=None, landing_float=None,
              touchdown_point=None):
    """Grade one leg's track. Returns None when there is too little to judge.

    The overall score is a weighted mean of the phases present, held to no more
    than one grade band above the worst phase score. A leg with a pleasant cruise
    and an alarming approach is not a B: a passenger remembers the worst part,
    and an average that hides it is not describing the flight they were on.
    """
    if not track or len(track) < 5:
        return None
    # Recorded type wins; otherwise work it out from the flying.
    if not category:
        category = infer_category(track)
    profile = profile_for(aircraft, category=category, vs0_kt=vs0_kt)
    ranges = split_phases(track, profile)
    if not ranges:
        return None
    land_alt = landing_elevation(track)

    # Gate once, here, rather than at each place the cap is applied. Callers
    # do filter - landing_alignment_for() returns None for rotary - but a
    # public function must not depend on its caller having done that, and
    # both the normal and the touchdown-only descent paths read this.
    if not scores_alignment(profile):
        alignment = None
    if not scores_float(profile):
        landing_float = None
        # The touchdown zone is an airplane standard too, and rides the same
        # switch: a helicopter landing on a runway is not aiming for 1,000 ft.
        touchdown_point = None

    td_score = score_for_touchdown_fpm(landing_rate_fpm, profile)
    phases = {}
    for name in PHASE_ORDER:
        rng = ranges.get(name)
        if not rng:
            continue
        span = _span_s(track, rng)
        if span < profile["min_phase_s"]:
            phases[name] = {"score": None, "letter": None,
                            "seconds": round(span, 1),
                            "note": "too short to grade"}
            continue
        metrics = _slice_metrics(track, rng, profile, phase=name,
                                 land_alt=land_alt)
        # The descent spec needs to know which profile it belongs to, so
        # the touchdown band can be worded for the right curve.
        spec = dict(profile[name], _profile=profile) if name == "descent" \
            else profile[name]
        # With the landing a phase of its own the descent gets none of the
        # landing's measures: the touchdown scores nothing here, and the
        # weights left are averaged over in their own proportions.
        score, parts = _score_phase(
            metrics, spec,
            extra=(({} if LANDING_PHASE else
                    {"touchdown": td_score, "touchdown_fpm": landing_rate_fpm,
                     "alignment": alignment, "float": landing_float,
                     "touchdown_point": touchdown_point})
                   if name == "descent" else None))
        phases[name] = {
            "score": round(score, 1) if score is not None else None,
            "letter": letter_for_score(score),
            "seconds": round(span, 1),
            "parts": parts,
        }

    # A leg that climbs to its destination has no descent to measure, and its
    # touchdown would otherwise drop out of the rollup entirely - the landing
    # simply not counted. Grade the phase on the touchdown alone.
    d = phases.get("descent")
    if LANDING_PHASE:
        if td_score is not None:
            land_score, land_parts, land_note = _landing_blend(
                td_score, landing_rate_fpm, profile, alignment, landing_float,
                touchdown_point)
            phases["landing"] = {
                "score": round(land_score, 1),
                "letter": letter_for_score(land_score),
                "seconds": None,
                "parts": land_parts,
            }
            if land_note:
                phases["landing"]["note"] = land_note
    elif td_score is not None and (d is None or d.get("score") is None):
        # Built by _landing_phase rather than through _score_phase, so the
        # limits apply here too - or a leg with no measurable descent is the
        # one place a crooked landing gets away with it.
        short_score, short_parts = _landing_phase(
            td_score, landing_rate_fpm, profile, alignment, landing_float,
            touchdown_point)
        phases["descent"] = {
            "score": round(short_score, 1),
            "letter": letter_for_score(short_score),
            "seconds": (d or {}).get("seconds", 0.0),
            "parts": short_parts,
            "note": "touchdown only; no measurable descent",
        }

    scored = {k: v["score"] for k, v in phases.items() if v.get("score") is not None}
    if not scored:
        return None

    weights = phase_weights_for(profile)
    total = sum(weights[k] for k in scored)
    overall = sum(scored[k] * weights[k] for k in scored) / total

    worst = min(scored.values())
    capped = min(overall, worst + PHASE_CAP_POINTS)
    ride = _ride_score(scored, weights)

    return {
        "schema": SCHEMA,
        "profile": profile["name"],
        "profile_label": profile["label"],
        "category": category,
        "phases": phases,
        "overall": round(capped, 1),
        "overall_uncapped": round(overall, 1),
        "letter": letter_for_score(capped),
        "worst_phase": min(scored, key=lambda k: scored[k]),
        # For the passenger paragraph, so its prose cannot contradict the
        # grades sitting next to it.
        "ride_score": None if ride is None else round(ride, 1),
        "ride_letter": letter_for_score(ride),
        # The in-flight phase that set the ride grade, so the UI can say why
        # the paragraph reads the way it does.
        "ride_worst_phase": (min([k for k in scored if k not in _not_ride()],
                                 key=lambda k: scored[k])
                             if [k for k in scored if k not in _not_ride()]
                             else None),
    }


# ------------------------------------------------------------------ explain


def describe_profile(p):
    """One profile, in the shape the UI renders."""
    phases = []
    for name in PHASE_ORDER:
        spec = p.get(name)
        if not spec:
            continue
        label, blurb = PHASE_WORDS[name]
        # A phase means something different depending on what is flying. The
        # shared blurb described a hover on every tab, the Cessna's included.
        blurb = (p.get("phase_blurbs") or {}).get(name, blurb)
        metrics = []
        for key, weight in sorted(spec["weights"].items(), key=lambda kv: -kv[1]):
            entry = METRICS.get(key)
            band = spec.get(key)
            # Same metric, different reason to care. Descent angle is vortex
            # ring avoidance on a helicopter and glidepath on a wing, and the
            # shared text explained vortex ring state on the airplane tabs.
            why = (p.get("why") or {}).get(key, entry[2] if entry else None)
            # Touchdown is scored on a curve rather than a two-point band, so
            # it had no range to show - and it is the heaviest metric in the
            # phase at 45%. Describe the curve by its ends and hand the UI the
            # breakpoints, so the one measure everyone actually cares about is
            # not the one with nothing written against it.
            steps = None
            if key == "touchdown" and band is None:
                band = touchdown_band_for(p)
                steps = [{"at": fpm, "score": sc}
                         for fpm, sc in touchdown_curve_for(p)]
            metrics.append({
                "key": key,
                "label": entry[0] if entry else key,
                "why": why,
                "weight_pct": round(weight * 100),
                "best": band[0] if band else None,
                "worst": band[1] if band else None,
                "unit": entry[1] if entry else None,
                "steps": steps,
            })
        # Not in spec["weights"], because it has no weight. It still belongs
        # on the tab: a reader looking for why a landing was marked down goes
        # to Descent, and a metric that is missing from the list reads as a
        # metric that does not exist.
        if name == "descent" and scores_alignment(p):
            entry = METRICS["alignment"]
            metrics.append({
                "key": "alignment",
                "label": entry[0],
                "why": entry[2],
                "weight_pct": 0,
                "cap": True,
                "cap_points": ALIGNMENT_CAP_POINTS,
                "best": ALIGNMENT_BANK_DEG[0],
                "worst": ALIGNMENT_BANK_DEG[1],
                "scrub_best": ALIGNMENT_SCRUB_G[0],
                "scrub_worst": ALIGNMENT_SCRUB_G[1],
                "unit": entry[1],
                "steps": None,
            })
        if name == "descent" and scores_float(p):
            metrics.append({
                "key": "float",
                "label": "Float",
                "why": ("How long the airplane floated over the runway before "
                        "the wheels touched. The clock starts 50 ft above the "
                        "runway - or at the start of the runway, if you were "
                        "already lower than that, so time spent low over the "
                        "approach doesn't count. The FAA treats about 7 "
                        "seconds as normal (AC 25-32). Floating longer still "
                        "passes while there is runway left in the touchdown "
                        "zone (AC 91-79A); beyond that, going around was the "
                        "better choice. How many seconds that is depends on "
                        "how fast you were going. Only measured where the sim "
                        "has described the runway."
                        + (" These limits are your own settings, not the "
                           "FAA's figures." if own_bands(FLOAT_BANDS) else "")
                        + (" It counts toward the landing score - a short "
                           "float helps, a long one costs - and a long float "
                           "can also lower the landing letter." if GRADE_FLOAT else
                           " Shown only for now: it doesn't change any grade "
                           "(you can switch that on in Settings).")),
                "weight_pct": 0,
                "cap": bool(GRADE_FLOAT),
                "cap_points": FLOAT_CAP_POINTS,
                "best": FLOAT_NORMAL_S,
                # A range needs two ends, and this zero moves with speed. The
                # 100 kt figure is the representative end; band says the rule
                # and works it out at three speeds, and the UI prints that.
                "worst": float_points_s(100.0)[1],
                "band": ("Full marks up to %g s. Half marks after another "
                         "{:,} ft of runway, zero {:,} ft after that. In "
                         "seconds, half marks and zero come at %s."
                         .format(int(FLOAT_MARGIN_FT), int(FLOAT_BEYOND_FT))
                         % (FLOAT_NORMAL_S,
                            "; ".join("%.0f and %.0f s at %d kt"
                                      % (float_points_s(kt) + (kt,))
                                      for kt in (60, 100, 140)))),
                "unit": "%.0f s",
                "steps": None,
            })
        if name == "descent" and scores_float(p):
            metrics.append({
                "key": "touchdown_point",
                "label": "Touchdown spot",
                "why": ("How far down the runway the wheels touched, measured "
                        "from where the runway starts. The FAA's aim point is "
                        "about 1,000 ft in, and touching down past 3,000 ft - "
                        "or past the first third of a shorter runway - is "
                        "landing long (AC 91-79A). Measured once the sim has "
                        "described the runway, which it does after you park."
                        + (" These limits are your own settings, not the "
                           "FAA's figures." if own_bands(TDZ_BANDS) else "")
                        + (" It counts toward the landing score - near the "
                           "aim point helps, far down the runway costs - and "
                           "landing long can also lower the landing letter."
                           if GRADE_TOUCHDOWN_POINT else
                           " Shown only for now: it doesn't change any grade "
                           "(you can switch that on in Settings).")),
                "weight_pct": 0,
                "cap": bool(GRADE_TOUCHDOWN_POINT),
                "cap_points": TDZ_CAP_POINTS,
                "best": TDZ_TARGET_FT + TDZ_TOLERANCE_FT,
                "worst": TDZ_END_FT + TDZ_BEYOND_FT,
                "band": ("Full marks within {:,} ft of the runway start. "
                         "Half marks at {:,} ft, or a third of the way down a "
                         "shorter runway; zero {:,} ft after that."
                         .format(int(TDZ_TARGET_FT + TDZ_TOLERANCE_FT),
                                 int(TDZ_END_FT), int(TDZ_BEYOND_FT))),
                "unit": "%.0f ft",
                "steps": None,
            })
        phases.append({
            "key": name,
            "label": label,
            "blurb": blurb,
            "source": (p.get("phase_sources") or {}).get(name),
            "weight_pct": round(p["phase_weights"].get(name, 0) * 100),
            "metrics": metrics,
        })
    notes = []
    # Each phase cites the standard its numbers came from, beside the numbers
    # themselves. A blanket caveat above them was the same citation twice on
    # one screen, and told a reader nothing the sources line does not.
    if p["name"] == "unclassified":
        notes.append(
            "This covers aircraft the sim does not classify usefully. A "
            "gyroplane reports itself as an airplane with a 10 kt stall "
            "speed, so it is graded on the airplane profile with the two "
            "measures that depend on a fixed-wing number - descent angle and "
            "the 500 ft gate - left out rather than judged against numbers "
            "that do not apply to it.")
    if LANDING_PHASE:
        phases = _split_landing(p, phases)
    return {
        "key": p["name"],
        "label": p["label"],
        "notes": notes,
        "phases": phases,
        # Whether the A-F landing letter on THIS profile can be held down by
        # touchdown alignment. False for rotary, and the tab says why.
        "alignment": scores_alignment(p),
    }


LANDING_KEYS = ("touchdown", "alignment", "float", "touchdown_point")


def _landing_source(p):
    """Where the landing's numbers came from: the touchdown's own sentence of
    the descent's sources, or its first sentence when it names none."""
    s = (p.get("phase_sources") or {}).get("descent") or ""
    i = s.find("Touchdown:")
    if i < 0:
        i = 0
    j = s.find(". ", i)
    return s[i:j + 1] if j >= 0 else s[i:]


def _split_landing(p, phases):
    """The panel's phases with the landing taken out of the descent."""
    out = []
    for ph in phases:
        if ph["key"] != "descent":
            out.append(ph)
            continue
        d_w, l_w = landing_phase_weights(p)
        rest = [m for m in ph["metrics"] if m["key"] not in LANDING_KEYS]
        total = sum(m["weight_pct"] for m in rest if not m.get("cap")) or 1
        for m in rest:
            if not m.get("cap"):
                m["weight_pct"] = round(m["weight_pct"] * 100.0 / total)
        out.append(dict(ph, weight_pct=round(d_w * 100), metrics=rest))
        land = [m for m in ph["metrics"] if m["key"] in LANDING_KEYS]
        on = {"touchdown": True, "touchdown_point": bool(GRADE_TOUCHDOWN_POINT),
              "float": bool(GRADE_FLOAT)}
        total = sum(LANDING_WEIGHTS[k] for k in on if on[k])
        for m in land:
            if m["key"] in LANDING_WEIGHTS:
                m["cap"] = False
                m["weight_pct"] = (round(LANDING_WEIGHTS[m["key"]] * 100 / total)
                                   if on[m["key"]] else 0)
        label, blurb = PHASE_WORDS["landing"]
        if not [m for m in land if m["key"] != "touchdown"]:
            blurb = "How firmly the aircraft touched down."
        out.append({
            "key": "landing",
            "label": label,
            "blurb": blurb,
            # The descent's sources cite the approach as well as the
            # touchdown; the landing measures cite their own in their text.
            "source": _landing_source(p),
            "weight_pct": round(l_w * 100),
            "metrics": land,
        })
    return out


def describe():
    """How grading works, for every aircraft type, for the UI.

    Generated from the profiles rather than written separately, so the
    explanation cannot drift away from the thresholds actually in use - which
    is how the EFB and logbook ladders came apart once before.
    """
    return {
        "schema": SCHEMA,
        "profiles": [describe_profile(ROTARY),
                     describe_profile(LIGHT_GA),
                     describe_profile(FIXED_WING),
                     describe_profile(UNCLASSIFIED)],
        "letters": ([{"letter": l, "at_least": sc} for sc, l in LETTER_TABLE]
                    + [{"letter": LETTER_WORST, "at_least": None}]),
        "detection": (
            "The sim is asked what the aircraft is (its CATEGORY) and that "
            "decides the profile. It is not guessed from the aircraft name. "
            "CATEGORY alone is not quite enough: a gyroplane reports "
            "\"Airplane\" exactly as a Cessna does, so the stall speed is "
            "checked too - 33 kt for a C152, 40 for a C172, 10 for a gyro, "
            "and above 61 kt an airplane is graded on transport criteria "
            "instead, which is where 14 CFR 23.49 draws that line. When the "
            "sim reports neither, the type is worked out from the flying "
            "instead: a helicopter hovers and an airplane cannot."),
        "g_note": G_BAND_NOTE,
        "alignment_note": (
            "The A-F landing letter comes from how fast the aircraft was "
            "descending when the wheels touched. How straight it arrived can "
            "then hold that letter down, and the %s phase with it, but "
            "can never lift either: a landing that touches gently and then "
            "slides sideways is not a good landing, while arriving straight "
            "is simply what a landing is supposed to do. Bank counts for "
            "%.0f%% and sideways slide for %.0f%%; full marks at %.0f "
            "degrees and %.2f g, nothing at %.0f degrees or %.2f g. The "
            "letter is allowed to sit one grade above the alignment score "
            "rather than falling to it. These thresholds have not been "
            "checked against measured flights yet, so treat a held-down "
            "letter as an indication rather than a verdict. Helicopters are "
            "not scored on this at all, because the sideways measurement is "
            "not available for them, and a missing reading must never pass "
            "for a good one."
            % ("landing" if LANDING_PHASE else "descent",
               ALIGNMENT_WEIGHTS[0][1] * 100, ALIGNMENT_WEIGHTS[1][1] * 100,
               ALIGNMENT_BANK_DEG[0], ALIGNMENT_SCRUB_G[0],
               ALIGNMENT_BANK_DEG[1], ALIGNMENT_SCRUB_G[1])),
        "cap_note": (
            "A leg's overall grade is the weighted average of its phase "
            "scores, then held to no more than one band - ten points - above "
            "the weakest phase score. So a leg whose descent scored 60 has an "
            "overall grade of at most 70 however good the rest was: a lovely "
            "cruise does not cancel an alarming approach. The other phase "
            "grades are never capped by this - only the overall. The landing "
            "has limits of its own, listed in its card. Most legs are not "
            "affected, and a leg's Grading tab says when that leg was "
            "capped."),
        "limits": [
            "Most grades are worked out from the flight track, which is "
            "recorded once a second, so anything briefer than that is "
            "smoothed away - vibration and the texture of turbulence are not "
            "captured. These grades describe the shape of the flying rather "
            "than how each second of it felt. The hardest g and acceleration "
            "within each second are recorded as well, and two measures read "
            "them: the rotation at lift-off, and the sideways slide after "
            "touchdown. The float is timed from the landing recording, which "
            "runs ten times a second.",
            "Bank angle and height above the terrain are read from the sim "
            "where it reports them. Where it does not, bank is worked out "
            "from how quickly the heading changes and how fast the aircraft "
            "is going, and height is measured from the landing site rather "
            "than from the ground below it - so over rising or falling "
            "terrain it is only approximate.",
        ],
    }

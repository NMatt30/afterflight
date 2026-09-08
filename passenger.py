"""Template passenger assessment.

No LLM, no network. Slots: opening, ride, landing, closer.
Inputs: grade, rate_fpm, duration, aircraft, optional bank/G.

The variant for each slot comes from a stable hash of flight_id + leg, so a leg
reads the same every time the logbook is rebuilt and never rewrites itself.

Pool sizes are deliberately large. Openings are picked from a duration band so
the phrasing fits a two-minute hop and a two-hour cruise equally, and landings
have their own pool per grade - grade A is the one most people see most often,
so it is the deepest.
"""

import hashlib
import io
import json
import os
import re

# "a 11-minute leg" reads wrong: eight, eleven and eighteen all start with a
# vowel sound. Durations here are minutes, hours and seconds, so those three
# leading numbers are the only cases that arise.
_AN_RE = re.compile('\\b(a)(\\s+)(?=(?:8|11|18)(?:\\b|-))', re.IGNORECASE)


def _fix_articles(text):
    def repl(m):
        art = "An" if m.group(1).isupper() else "an"
        return art + m.group(2)
    return _AN_RE.sub(repl, text)

# Landing grade rules on touchdown VS (section 8). Local, no cloud.
# A, B, C, D, F. There is no E: most scales dropped it because it reads as a
# typo for F, and this one is meant to be legible at a glance. The old D and
# E bands are merged, so the boundary that was 450 is now 500.
GRADE_TABLE = (
    (60.0, "A", "Butter"),
    (150.0, "B", "Smooth"),
    (300.0, "C", "Firm"),
    (500.0, "D", "Hard"),
)
GRADE_WORST = ("F", "Arrival")


def grade_for_rate(rate_fpm):
    """Return (letter, name) for a touchdown vertical speed in fpm."""
    if rate_fpm is None:
        return (None, None)
    try:
        mag = abs(float(rate_fpm))
    except (TypeError, ValueError):
        return (None, None)
    if mag != mag:  # NaN
        return (None, None)
    for limit, letter, name in GRADE_TABLE:
        if mag <= limit:
            return (letter, name)
    return GRADE_WORST


def name_for_grade(letter):
    """The word that goes with a letter.

    grade_for_rate() answers for a rate. A letter that has been held down by
    something other than the rate - touchdown alignment - needs the same word
    without going back through the ladder, or the prose says "Butter" over a
    landing that has just been marked down.
    """
    for _limit, ltr, name in GRADE_TABLE:
        if ltr == letter:
            return name
    return GRADE_WORST[1] if letter == GRADE_WORST[0] else None


def _variant(flight_id, leg, slot, count):
    """Stable per-leg, per-slot variant index."""
    if count <= 1:
        return 0
    key = "%s|%s|%s" % (flight_id or "", leg if leg is not None else "", slot)
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % count


# How many recent legs a slot will refuse to repeat. Circuits are the case
# that needs it: ten trips round the pattern are ten legs of the same length,
# the same ride and the same landing band, all drawing from one pool. Dodging
# only the leg above still repeats every few rows.
#
# Capped against the pool size below, because a pool of 12 cannot dodge 6
# neighbours and still be picking rather than cycling.
AVOID_RECENT = 6


def _pick(flight_id, leg, slot, pool, avoid):
    """Variant index for a slot, stepping off recent legs' choices.

    The hash is uniform, so on its own a pool of 26 still repeats within a
    handful of draws - and legs sit next to each other on screen, which is
    exactly where a repeat is most obvious.

    `avoid` maps a slot to either one index (the old shape) or a list of
    recent ones, newest last. Both are accepted so an old cached record does
    not have to be rebuilt to be read.
    """
    n = len(pool)
    idx = _variant(flight_id, leg, slot, n)
    if not avoid or n <= 1:
        return idx
    recent = avoid.get(slot)
    if recent is None:
        return idx
    if isinstance(recent, int):
        recent = [recent]
    # Never refuse so many that there is nothing left to choose from.
    taken = set(list(recent)[-min(AVOID_RECENT, n - 1):])
    while idx in taken:
        idx = (idx + 1) % n
    return idx


# ------------------------------------------------------------------- lines

# The prose lives in passenger_lines.json, not here. It is content rather than
# logic: adding a line should not mean editing a module, and a contributor with
# an ear for the voice should not have to read Python to help. The paragraph is
# assembled one slot at a time from independent pools, so a new line cannot
# break another one.
LINES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "passenger_lines.json")
LINES_SCHEMA = 2

# If the file is missing or malformed the app must still produce a sentence.
# Prose is a nicety; losing the logbook over it would not be. These are
# deliberately plain - if you are reading them in the UI, something is wrong
# and the log says what.
_FALLBACK = {
    "openings": {"short": ["A short leg in the {aircraft}."],
                 "medium": ["{duration_noun} in the {aircraft}."],
                 "long": ["A long leg in the {aircraft}, {duration_noun}."],
                 "unknown": ["A leg in the {aircraft}."]},
    "ride": {"smooth": ["The ride was smooth."],
             "normal": ["The ride was ordinary."],
             "busy": ["The ride was busy."]},
    "landing": {"A": ["Touchdown at {rate}."], "B": ["Touchdown at {rate}."],
                "C": ["Touchdown at {rate}."], "D": ["Touchdown at {rate}."],
                "F": ["Touchdown at {rate}."],
                "held": ["Touchdown at {rate}, but not straight."],
                "none": ["No landing recorded."]},
    "connector": {"up": ["It improved."], "down": ["It did not last."]},
    "closer": {"good": ["A good flight."], "ok": ["A fair flight."],
               "bad": ["Not a good flight."], "mixed": ["A mixed flight."]},
}


def _load_lines(path=None):
    """Read the prose file. Returns (pools, note) - note is None when clean."""
    path = path or LINES_PATH
    try:
        with io.open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        return _FALLBACK, "passenger lines unreadable (%s): using fallback" % (e,)
    if doc.get("schema") != LINES_SCHEMA:
        return _FALLBACK, ("passenger lines schema %r, expected %r: using fallback"
                           % (doc.get("schema"), LINES_SCHEMA))
    out, missing = {}, []
    for group, keys in (("openings", ("short", "medium", "long", "unknown")),
                        ("ride", ("smooth", "normal", "busy")),
                        ("landing", ("A", "B", "C", "D", "F", "held", "none")),
                        ("connector", ("up", "down")),
                        ("closer", ("good", "ok", "bad", "mixed"))):
        out[group] = {}
        src = doc.get(group) or {}
        for key in keys:
            pool = src.get(key)
            if not isinstance(pool, list) or not pool or \
                    not all(isinstance(x, str) and x.strip() for x in pool):
                missing.append("%s.%s" % (group, key))
                pool = _FALLBACK[group][key]
            out[group][key] = list(pool)
    if missing:
        return out, "passenger lines incomplete: " + ", ".join(missing)
    return out, None


LINES, LINES_NOTE = _load_lines()

OPENINGS = LINES["openings"]
RIDE = LINES["ride"]
LANDING = LINES["landing"]
CONNECTOR = LINES["connector"]
CLOSER = LINES["closer"]


# ------------------------------------------------------------------- arc

# The four sentences used to be drawn independently, each from its own grade,
# and the result read as separate verdicts stapled together: "A sporting sort
# of ride. My stomach kept up, mostly." then "Well flown, start to finish."
# Nine legs in twenty-two swung by two steps or more.
#
# So the ride and the landing are scored on one scale, and when they disagree
# the paragraph acknowledges it - with a short bridging sentence between them,
# and a closer that admits the flight had two halves instead of picking one.
RIDE_SENTIMENT = {"smooth": 1, "normal": 0, "busy": -1}
LANDING_SENTIMENT = {"A": 2, "B": 1, "C": 0, "D": -1, "F": -2}
# A landing held down for sliding is a soft touch spoiled. Whatever the rate
# said, it is a mild negative, and it must not read as the letter it became.
HELD_SENTIMENT = -1
# How far apart the two have to pull before the paragraph says so. Two steps
# is a genuine change of subject; one is just an ordinary flight.
CONTRAST_SPREAD = 2


def _sentiment(ride_kind, grade, held):
    """(ride, landing) on one scale, or (None, None) if there is no landing."""
    r = RIDE_SENTIMENT.get(ride_kind, 0)
    if held:
        return r, HELD_SENTIMENT
    if grade not in LANDING_SENTIMENT:
        return r, None
    return r, LANDING_SENTIMENT[grade]


def _fmt_duration(seconds):
    """Return (adjective form, noun form) for a block time in seconds."""
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return (None, None)
    if s <= 0:
        return (None, None)
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h and m:
        return ("%dh %dm" % (h, m), "%dh %dm" % (h, m))
    if h:
        return ("%d-hour" % h, "an hour" if h == 1 else "%d hours" % h)
    if m:
        return ("%d-minute" % m, "a minute" if m == 1 else "%d minutes" % m)
    return ("%d-second" % s, "%d seconds" % s)


def _opening_pool(duration_s):
    """Openings that fit the length of the leg."""
    try:
        s = float(duration_s)
    except (TypeError, ValueError):
        return "unknown", OPENINGS["unknown"]
    if s <= 0:
        return "unknown", OPENINGS["unknown"]
    if s <= 8 * 60:
        return "short", OPENINGS["short"]
    if s <= 45 * 60:
        return "medium", OPENINGS["medium"]
    return "long", OPENINGS["long"]


def _fmt_rate(rate_fpm):
    try:
        return "%d fpm" % round(abs(float(rate_fpm)))
    except (TypeError, ValueError):
        return "an unrecorded rate"


def _ride_pool(ride_grade, peak_vs_fpm, max_bank_deg):
    """Which pool describes the ride - NOT how it landed.

    This used to be picked from the touchdown grade, so a leg with a rough
    cruise and a greaser of a landing told the passenger the cruise was
    glassy. The paragraph sits next to the phase pills and must not
    contradict them, so it is driven by the in-flight grade instead: the
    phases except the descent, which gets its own sentence.

    peak_vs_fpm and max_bank_deg are accepted and no longer consulted. They
    used to force "busy" above 1500 fpm or 35 degrees whatever the grade
    said, and those are airframe-blind numbers: 2000 fpm is a hard pull in a
    Cessna and an ordinary climb in a light jet, and a helicopter descends
    through 1700 fpm without anyone noticing. A quarter of the legs in this
    logbook graded A or B on the ride and were then described as busy - and
    because a busy ride against a good landing clears CONTRAST_SPREAD, one
    wrong word also produced a "finished far better than it ran" connector
    and a mixed closer. Three sentences wrong from one threshold.

    The grade already accounts for bank and for vertical acceleration, per
    profile, against bands that know what class the aircraft is. Second
    guessing it here is exactly the contradiction the paragraph above forbids.
    """
    if ride_grade in ("D", "F"):
        return "busy", RIDE["busy"]
    if ride_grade in ("A", "B"):
        return "smooth", RIDE["smooth"]
    return "normal", RIDE["normal"]


def assess(flight_id, leg, aircraft=None, grade=None, rate_fpm=None,
           duration_s=None, peak_vs_fpm=None, max_bank_deg=None, avoid=None,
           ride_grade=None, overall_grade=None, held_from=None):
    """Build the four-slot passenger paragraph for one leg.

    Three different grades feed three different sentences, which is the
    point: `grade` is the touchdown and colors only the landing line,
    `ride_grade` is the flying and colors the ride line, `overall_grade`
    is the leg and sets the closing tone. Passing one grade for all three
    is what let the prose disagree with the pills beside it.

    `held_from` is the letter the touchdown rate alone would have earned, when
    alignment has since pulled it down. It matters because every landing line
    is written about how HARD the arrival was, and a letter held down for
    sliding sideways is not describing hardness: a gentle 105 fpm touchdown
    marked to C came out as "business-like", which is a sentence about a firm
    landing that this one never was. When it is set, the landing line comes
    from a pool about arriving softly and crookedly instead.

    Pass the previous leg's `picks` as `avoid` and no slot will repeat the line
    the leg above it used. Returns a dict with the slots and the joined text.
    """
    if grade is None:
        grade = grade_for_rate(rate_fpm)[0]
    if ride_grade is None:
        ride_grade = grade
    if overall_grade is None:
        overall_grade = grade

    ac = aircraft or "aircraft"
    dur_adj, dur_noun = _fmt_duration(duration_s)
    band, op_pool = _opening_pool(duration_s)
    if dur_adj is None:
        band, op_pool = "unknown", OPENINGS["unknown"]
        dur_adj = dur_noun = ""

    picks = {}
    slot = "opening." + band
    picks[slot] = _pick(flight_id, leg, slot, op_pool, avoid)
    opening = op_pool[picks[slot]].format(
        aircraft=ac, duration_adj=dur_adj, duration_noun=dur_noun
    )
    # Some slots lead with the duration ("18 minutes in the back of..."), so fix
    # the case rather than duplicating every template.
    if opening:
        opening = opening[0].upper() + opening[1:]
        opening = _fix_articles(opening)

    ride_kind, ride_pool = _ride_pool(ride_grade, peak_vs_fpm, max_bank_deg)
    slot = "ride." + ride_kind
    picks[slot] = _pick(flight_id, leg, slot, ride_pool, avoid)
    ride = ride_pool[picks[slot]]

    # Held down by alignment rather than by the rate: describe what the
    # passenger actually felt, which was a soft touch and then a slide.
    held = bool(held_from) and held_from != grade and grade is not None
    if held:
        slot = "landing.held"
        picks[slot] = _pick(flight_id, leg, slot, LANDING["held"], avoid)
        landing = LANDING["held"][picks[slot]].format(rate=_fmt_rate(rate_fpm))
    elif grade in LANDING:
        land_pool = LANDING[grade]
        slot = "landing." + grade
        picks[slot] = _pick(flight_id, leg, slot, land_pool, avoid)
        landing = land_pool[picks[slot]].format(rate=_fmt_rate(rate_fpm))
    else:
        slot = "landing.none"
        picks[slot] = _pick(flight_id, leg, slot, LANDING["none"], avoid)
        landing = LANDING["none"][picks[slot]]

    # Does the paragraph change its mind halfway through?
    r_sent, l_sent = _sentiment(ride_kind, grade, held)
    swing = 0 if l_sent is None else l_sent - r_sent
    connector = ""
    if abs(swing) >= CONTRAST_SPREAD:
        way = "up" if swing > 0 else "down"
        slot = "connector." + way
        picks[slot] = _pick(flight_id, leg, slot, CONNECTOR[way], avoid)
        connector = CONNECTOR[way][picks[slot]]

    # The closer is about the leg. When the parts disagree it has to say so:
    # a leg that was busy and then landed beautifully is not "well flown,
    # start to finish", whatever the weighted average came to.
    if abs(swing) >= CONTRAST_SPREAD:
        tone = "mixed"
    elif overall_grade in ("A", "B"):
        tone = "good"
    elif overall_grade in ("C", None):
        tone = "ok"
    else:
        tone = "bad"
    close_pool = CLOSER[tone]
    slot = "closer." + tone
    picks[slot] = _pick(flight_id, leg, slot, close_pool, avoid)
    closer = close_pool[picks[slot]]

    return {
        "opening": opening,
        "ride": ride,
        "connector": connector,
        "landing": landing,
        "closer": closer,
        "text": " ".join(x for x in (opening, ride, connector, landing, closer) if x),
        "tone": tone,
        "swing": swing,
        "graded_on": {"landing": grade, "ride": ride_grade,
                      "overall": overall_grade,
                      "landing_held_from": held_from if held else None},
        "source": "template",
        "picks": picks,
    }


def pool_stats():
    """How many distinct paragraphs the pools can produce, per case."""
    out = {}
    for band, pool in (("short", OPENINGS["short"]), ("medium", OPENINGS["medium"]),
                       ("long", OPENINGS["long"]), ("unknown", OPENINGS["unknown"])):
        for gr in ["A", "B", "C", "D", "F", "held", None]:
            ride = (RIDE["smooth"] if gr in ("A", "B") else RIDE["normal"])
            land = LANDING.get(gr, LANDING["none"])
            tone = "good" if gr in ("A", "B") else ("ok" if gr in ("C", None) else "bad")
            out["%s/%s" % (band, gr)] = len(pool) * len(ride) * len(land) * len(CLOSER[tone])
    return out


# ------------------------------------------------------------------ checks

# What each group's lines are allowed to ask for. A placeholder outside this
# set means the line renders with a literal {brace} in the logbook.
ALLOWED = {
    "openings": {"aircraft", "duration_adj", "duration_noun"},
    "ride": set(),
    "landing": {"rate"},
    "connector": set(),
    "closer": set(),
}

# A bridging sentence has one job: get from one verdict to the other. Long
# ones stop bridging and start competing with the sentences either side.
CONNECTOR_MAX_WORDS = 12

# The held pool describes a soft touchdown that then slid. A line calling it
# firm or hard contradicts the number printed beside it, which is the whole
# reason the pool exists.
_HELD_MUST_NOT_SAY = ("firm", "hard", "thump", "hit at", "slammed")


def _placeholders(text):
    import string
    return {f for _lit, f, _spec, _conv in string.Formatter().parse(text) if f}


def _test_ride_follows_the_grade():
    """The prose must not call a ride busy that the grade called good.

    The paragraph sits beside the phase pills. peak_vs and max_bank used to
    override the grade at 1500 fpm and 35 degrees - airframe-blind numbers
    that made an ordinary jet climb and an ordinary helicopter descent read
    as busy. And because busy against a good landing clears CONTRAST_SPREAD,
    it also flipped the connector and the closer: three sentences wrong from
    one threshold.
    """
    for grade, want in (("A", "smooth"), ("B", "smooth"),
                        ("C", "normal"), ("D", "busy"), ("F", "busy")):
        for pv, bank in ((None, None), (2400.0, 12.0), (-3700.0, 40.0)):
            kind, _pool = _ride_pool(grade, pv, bank)
            assert kind == want, (
                "ride %s with peak_vs=%r bank=%r gave %r, expected %r - the "
                "prose is second-guessing the grade again"
                % (grade, pv, bank, kind, want))
    r, l = _sentiment("smooth", "B", False)
    assert abs(l - r) < CONTRAST_SPREAD, (
        "an A ride and a B landing still clears the contrast threshold, so "
        "the paragraph will say it finished far better than it ran")
    return True


def self_test(path=None):
    """Validate the prose file the way a reader would be let down by it."""
    pools, note = _load_lines(path)
    assert note is None, note
    _test_ride_follows_the_grade()

    problems = []
    for group, keys in pools.items():
        for key, lines in keys.items():
            seen = {}
            for i, line in enumerate(lines):
                where = "%s.%s[%d]" % (group, key, i)
                extra = _placeholders(line) - ALLOWED[group]
                if extra:
                    problems.append("%s uses %s, which nothing substitutes"
                                    % (where, ", ".join(sorted(extra))))
                try:
                    line.format(aircraft="X", duration_adj="1-minute",
                                duration_noun="a minute", rate="0 fpm")
                except (KeyError, IndexError, ValueError) as e:
                    problems.append("%s will not format: %r" % (where, e))
                norm = " ".join(line.lower().split())
                if norm in seen:
                    problems.append("%s duplicates %s.%s[%d]"
                                    % (where, group, key, seen[norm]))
                seen[norm] = i
                if not line.strip().endswith((".", "!", "?", '"')):
                    problems.append("%s does not end a sentence: %r" % (where, line[-20:]))
    # A mixed closer is used for a swing in either direction, so it must not
    # name which half was the good one. "One good landing does not make a
    # good flight" was in this pool and got printed under a 509 fpm arrival.
    _LEANS = ("good landing", "bad landing", "good arrival", "bad arrival",
              "good cruise", "bad cruise", "saved by the landing")
    for i, line in enumerate(pools["closer"]["mixed"]):
        low = line.lower()
        for phrase in _LEANS:
            if phrase in low:
                problems.append(
                    "closer.mixed[%d] says %r, which only reads correctly when "
                    "the landing was the good half; this pool is used for a "
                    "swing either way" % (i, phrase))
    for way, lines in pools["connector"].items():
        for i, line in enumerate(lines):
            n = len(line.split())
            if n > CONNECTOR_MAX_WORDS:
                problems.append("connector.%s[%d] is %d words; a bridge should "
                                "be short enough to disappear" % (way, i, n))
    for i, line in enumerate(pools["landing"]["held"]):
        low = line.lower()
        said = [w for w in _HELD_MUST_NOT_SAY if w in low]
        if said:
            problems.append("landing.held[%d] calls the touchdown %s; it was "
                            "gentle, and the letter came down for sliding"
                            % (i, "/".join(said)))
    assert not problems, "\n  ".join([""] + problems)

    # And the grades the ladder can actually produce all have a pool.
    for _lim, letter, _n in GRADE_TABLE:
        assert letter in pools["landing"], "no landing lines for grade %s" % letter
    assert GRADE_WORST[0] in pools["landing"]
    assert "E" not in pools["landing"], (
        "grade E cannot be produced by the ladder; a pool for it is dead text")

    # A held landing must not be described in the words of the grade it was
    # pulled down to.
    held = assess("f", 1, aircraft="C172", rate_fpm=105.0, duration_s=700,
                  grade="C", held_from="B")
    plain = assess("f", 1, aircraft="C172", rate_fpm=105.0, duration_s=700,
                   grade="C")
    assert held["landing"] != plain["landing"], (
        "a held-down landing read the same as a genuinely firm one")
    assert held["graded_on"]["landing_held_from"] == "B"

    # A paragraph that changes its mind has to say so, and must not end on a
    # closer that claims the whole flight was one thing.
    swung = assess("f", 2, aircraft="AS365", rate_fpm=58.0, duration_s=315,
                   grade="A", ride_grade="D", overall_grade="D",
                   peak_vs_fpm=1800.0)
    assert swung["connector"], (
        "a busy ride followed by a greaser produced no bridging sentence; "
        "the paragraph reads as two unrelated verdicts")
    assert swung["tone"] == "mixed", (
        "a leg whose halves disagree closed on %r" % swung["tone"])
    steady = assess("f", 2, aircraft="AS365", rate_fpm=40.0, duration_s=315,
                    grade="A", ride_grade="A", overall_grade="A")
    assert not steady["connector"], (
        "a coherent flight was given a bridging sentence it does not need")
    assert steady["tone"] == "good"
    return True


if __name__ == "__main__":
    ok = self_test()
    st = pool_stats()
    print("lines: %s" % LINES_PATH)
    for group in ("openings", "ride", "landing", "closer"):
        print("  %-9s %s" % (group, {k: len(v) for k, v in sorted(LINES[group].items())}))
    print("  distinct paragraphs: %s ... %s"
          % (min(st.values()), max(st.values())))
    print("  avoid window: last %d legs per slot" % AVOID_RECENT)
    print("offline self-test:", "PASS" if ok else "FAIL")

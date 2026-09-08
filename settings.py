"""User-editable settings, applied over the module constants at run time.

The constants in watcher.py, tiles.py, mapbake.py and passenger.py stay the
source of truth: their values at import are captured as the defaults, and a
saved setting is an override on top. Delete settings.json and you are back to
known-good without touching code.

Everything here is Tier 1 - things worth changing per aircraft or per taste.
Capture rate, replay rate, camera smoothing and the map canvas stay in code
deliberately: they are easy to hurt yourself with and rarely want changing.

Nothing here is a safety invariant. Never touching the user aircraft, binding
only to loopback, and never re-anchoring a clip are guarantees, not preferences,
and making them adjustable would turn them into footguns.
"""
import json
import os
import threading
import persistence

BASE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE, "settings.json")

# The ring buffer has to hold the longest "before" window, plus room for the
# bounce filter to confirm the event and for the commit to land afterwards.
BUFFER_MARGIN_SEC = 8.0
BUFFER_MIN_SEC = 30.0

_lock = threading.Lock()
_defaults = {}
_values = {}


# key -> where it lives, how it is validated, and what it means.
# "live" says whether a change takes effect without restarting the watcher.
SPEC = [
    # ---- clip windows ----
    {"key": "takeoff_before", "module": "watcher", "attr": "TAKEOFF_BEFORE",
     "type": "float", "min": 0.0, "max": 120.0, "group": "Clip windows",
     "label": "Takeoff: seconds before liftoff", "live": True},
    {"key": "takeoff_after", "module": "watcher", "attr": "TAKEOFF_AFTER",
     "type": "float", "min": 5.0, "max": 300.0, "group": "Clip windows",
     "label": "Takeoff: seconds after liftoff", "live": True},
    {"key": "landing_before", "module": "watcher", "attr": "LANDING_BEFORE",
     "type": "float", "min": 5.0, "max": 300.0, "group": "Clip windows",
     "label": "Landing: seconds before touchdown", "live": True,
     "note": "The recording buffer grows to match; no restart needed."},
    {"key": "landing_after", "module": "watcher", "attr": "LANDING_AFTER",
     "type": "float", "min": 0.0, "max": 120.0, "group": "Clip windows",
     "label": "Landing: seconds after touchdown", "live": True},

    # ---- landing grades: deliberately NOT settings any more ----
    #
    # A, B, C and D used to be editable here, and editing one rewrote
    # passenger.GRADE_TABLE. That stopped being safe once the touchdown SCORE
    # curve in grading.py was anchored to the same numbers: full marks run to
    # the A limit, and the curve's knots sit on the letter boundaries. Both
    # are constants over there. So moving grade_a moved the letter and left
    # the score behind - a 75 fpm landing could be called an A by the ladder
    # while the curve still stopped full marks at 60 - and nothing at runtime
    # noticed. test_grading.py does notice, which meant the settings page
    # could put the app into a state its own tests reject.
    #
    # The ladder is also no longer a matter of taste. 60 fpm is where
    # published guidance puts butter, and the ends of the curve are the
    # 14 CFR 23.473 and 25.473 design limits. Those are cited by name in the
    # grading panel, and a slider that moves them makes the citation a lie.
    #
    # If per-taste grading comes back it has to move the curve and the ladder
    # together, not one of them.

    # ---- what a flight gets ----
    # The default for flights nobody has decided about individually. Changing
    # one of these moves every undecided flight and leaves the decided alone.
    {"key": "rate_flights", "module": "flightprefs", "attr": "RATING_DEFAULT",
     "type": "bool", "group": "What a flight gets",
     "label": "Rate flights by default", "live": True,
     "note": "Off hides letter grades, scores and phase grades on new "
             "flights. Any flight can be switched on its own row."},
    {"key": "passenger_notes", "module": "flightprefs", "attr": "PASSENGER_DEFAULT",
     "type": "bool", "group": "What a flight gets",
     "label": "Write passenger notes by default", "live": True,
     "note": "Independent of rating: a flight can be rated with no notes, "
             "or described with no grades."},

    # ---- maps ----
    {"key": "tile_source", "module": "tiles", "attr": "TILE_SOURCE",
     "type": "choice", "choices": ["topo", "osm"], "group": "Maps",
     "label": "Basemap", "live": True,
     "note": "topo = contours and hill shading; osm = airports and streets."},
    {"key": "map_style", "module": "mapbake", "attr": "MAP_STYLE",
     "type": "choice", "choices": ["light", "dark"], "group": "Maps",
     "label": "Basemap treatment", "live": True},

    # ---- replay ----
    {"key": "replay_start_paused", "module": "watcher", "attr": "REPLAY_START_PAUSED",
     "type": "bool", "group": "Replay",
     "label": "Open replays held at the first frame", "live": True},
    {"key": "ghost_ground_lift_ft", "module": "watcher", "attr": "GHOST_GROUND_LIFT_FT",
     "type": "float", "min": 0.0, "max": 6.0, "group": "Replay",
     "label": "Ghost ground lift (ft)", "live": True,
     "note": "Gear compression the ghost is missing at touchdown. 0.5 is "
             "confirmed on the AS365; another airframe may want more."},
    {"key": "chase_follow", "module": "watcher", "attr": "CHASE_MODE_DEFAULT",
     "type": "choice", "choices": ["follow", "place"], "group": "Replay",
     "label": "Replay camera starts", "live": True,
     "note": "follow = the camera travels with the ghost, holding your angle, "
             "which is what you want for a whole approach. place = it stays "
             "where it is put and the aircraft flies past, the cinematic "
             "flyby. Either way it only moves the replay camera."},
    {"key": "chase_distance", "module": "watcher", "attr": "CHASE_DISTANCE_DEFAULT",
     "type": "float", "min": 5.0, "max": 300.0, "group": "Replay",
     "label": "Camera distance default (m)", "live": True},
    {"key": "chase_height", "module": "watcher", "attr": "CHASE_HEIGHT_DEFAULT",
     "type": "float", "min": 0.0, "max": 120.0, "group": "Replay",
     "label": "Camera height default (m)", "live": True},
    {"key": "chase_orbit", "module": "watcher", "attr": "CHASE_ORBIT_DEFAULT",
     "type": "float", "min": 0.0, "max": 360.0, "group": "Replay",
     "label": "Camera orbit default (deg)", "live": True},
]

BY_KEY = {s["key"]: s for s in SPEC}


# The watcher runs as __main__, so sys.modules has no "watcher" entry and its
# constants were invisible here - clip windows read back empty and the derived
# buffer fell to its floor. Modules register themselves instead of being looked
# up by name.
_registry = {}


def register(name, module):
    _registry[name] = module


def _mod(name):
    import sys
    return _registry.get(name) or sys.modules.get(name)


def capture_defaults():
    """Snapshot the pristine constants. Call once, before applying anything."""
    if _defaults:
        return _defaults
    for s in SPEC:
        if s["module"] and s["attr"]:
            m = _mod(s["module"])
            if m is not None and hasattr(m, s["attr"]):
                _defaults[s["key"]] = getattr(m, s["attr"])
    return _defaults


def defaults():
    return dict(_defaults)


def coerce(spec, raw):
    """Validate one value against its spec. Raises ValueError with a reason."""
    kind = spec["type"]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)
    if kind == "choice":
        v = str(raw)
        if v not in spec["choices"]:
            raise ValueError("%s must be one of %s" % (spec["key"], ", ".join(spec["choices"])))
        return v
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % spec["key"])
    if v != v:
        raise ValueError("%s must be a number" % spec["key"])
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and v < lo:
        raise ValueError("%s must be at least %g" % (spec["key"], lo))
    if hi is not None and v > hi:
        raise ValueError("%s must be at most %g" % (spec["key"], hi))
    return v


def validate(values):
    """Coerce a whole dict, and check the rules that span several settings."""
    out = {}
    for key, raw in (values or {}).items():
        spec = BY_KEY.get(key)
        if spec is None:
            continue                      # unknown keys are ignored, not fatal
        out[key] = coerce(spec, raw)
    return out


def buffer_seconds(merged=None):
    """How deep the ring buffer must be to satisfy the clip windows.

    Derived rather than configured: LANDING_BEFORE is useless beyond what the
    buffer holds, and asking for both invites setting one and not the other.
    """
    m = merged if merged is not None else effective()
    longest = max(float(m.get("takeoff_before") or 0.0),
                  float(m.get("landing_before") or 0.0))
    return max(BUFFER_MIN_SEC, longest + BUFFER_MARGIN_SEC)


def effective():
    """Defaults with saved overrides on top."""
    m = dict(_defaults)
    m.update(_values)
    return m


def load():
    """Read settings.json. Bad or missing file means defaults."""
    global _values
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    clean = {}
    for key, raw in (doc if isinstance(doc, dict) else {}).items():
        spec = BY_KEY.get(key)
        if spec is None:
            continue
        try:
            clean[key] = coerce(spec, raw)
        except ValueError:
            continue                      # drop a bad entry, keep the rest
    with _lock:
        _values = clean
    return dict(clean)


def save(values):
    persistence.atomic_json(SETTINGS_PATH, values)


def update(values):
    """Validate, persist and return the new override set.

    A value equal to its default is dropped rather than stored. Keeping it
    would pin that default for ever - improve the constant in code later and
    the stale saved copy would silently win - and it would break the promise
    that deleting settings.json returns you to known-good.
    """
    global _values
    clean = validate(values)
    with _lock:
        out = dict(_values)
        out.update(clean)
        for key in list(out):
            if key in _defaults and out[key] == _defaults[key]:
                del out[key]
        save(out)
        _values = out
        return dict(out)


def apply(on_buffer=None):
    """Push the effective values onto the modules that read them.

    on_buffer(seconds) is called so the live ring buffer can be resized rather
    than waiting for a restart.

    There is no on_grades any more. This used to rewrite passenger.GRADE_TABLE
    and hand the caller a chance to rebuild the logbook; the ladder is a
    constant now. A settings.json left over from then still parses - unknown
    keys are dropped by load() - and the next save drops them from the file.
    """
    m = effective()
    for s in SPEC:
        if not (s["module"] and s["attr"]):
            continue
        if s["key"] not in m:
            continue
        mod = _mod(s["module"])
        if mod is not None:
            setattr(mod, s["attr"], m[s["key"]])

    secs = buffer_seconds(m)
    w = _mod("watcher")
    if w is not None:
        w.BUFFER_SEC = secs
        w.BUFFER_MAX = int(secs / max(getattr(w, "SAMPLE_SEC", 0.1), 1e-6)) + 16
        if callable(on_buffer):
            on_buffer(w.BUFFER_MAX)
    return m


def describe():
    """Spec plus current values, for the settings UI."""
    m = effective()
    d = dict(_defaults)
    out = []
    for s in SPEC:
        item = {k: v for k, v in s.items() if k not in ("module", "attr")}
        item["value"] = m.get(s["key"])
        item["default"] = d.get(s["key"])
        out.append(item)
    return {"settings": out,
            "derived": {"buffer_sec": round(buffer_seconds(m), 1)}}


def self_test():
    """Every spec resolves, and a choice rejects what is not on its list."""
    capture_defaults()
    missing = [s["key"] for s in SPEC
               if s["module"] and s["attr"] and s["key"] not in _defaults]
    assert not missing, "no default captured for: " + ", ".join(missing)
    for s in SPEC:
        if s["type"] != "choice":
            continue
        cur = _defaults.get(s["key"])
        assert cur in s["choices"], (
            "%s defaults to %r, which is not one of %s"
            % (s["key"], cur, s["choices"]))
        try:
            coerce(s, "definitely-not-a-choice")
        except ValueError:
            pass
        else:
            raise AssertionError("%s accepted a value off its list" % s["key"])
    return True


def _register_all():
    """Import and register the modules the specs write to."""
    import importlib
    names = {s["module"] for s in SPEC if s["module"]}
    for name in sorted(names):
        try:
            register(name, importlib.import_module(name))
        except ImportError:
            pass


if __name__ == "__main__":
    # AGENTS.md says this prints the resolved settings. It now does.
    _register_all()
    capture_defaults()
    load()
    eff = effective()
    print("settings.json: %s" % SETTINGS_PATH)
    print("  %-26s %-18s %s" % ("key", "value", "default"))
    for s in SPEC:
        key = s["key"]
        val, dflt = eff.get(key), _defaults.get(key)
        mark = " " if val == dflt else "*"
        print("%s %-26s %-18s %s" % (mark, key, val, dflt))
    print()
    print("  * = overridden in settings.json")
    print("offline self-test:", "PASS" if self_test() else "FAIL")

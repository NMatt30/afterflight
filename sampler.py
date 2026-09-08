"""Push-based state sampling (the fix for the ~2.8 Hz capture ceiling).

The legacy path asks python-SimConnect for one variable at a time. Each
`aq.get()` blocks in a polling loop, so a 13-variable sample costs a fixed
slice of wall time and the detect loop settles at ~2.8 Hz - which is why clips
carry roughly half the points section 6 assumes.

This module does it the way SimConnect is meant to be used: build one data
definition, subscribe once with RequestDataOnSimObject, and let the sim push
values into the dispatch thread. Reading a sample then costs nothing - it is a
dict lookup - so the loop can run at whatever rate it likes.

Nothing here talks to the sim directly. It is handed the bound function table
and handle, so the parsing and layout logic can be tested with synthetic
buffers and no sim running (see self_test at the bottom).
"""

import struct
import threading
import time

# SimConnect enums. INITPOSITION = 12 is already proven by the ghost path in
# watcher.py, which pins the rest of this list.
DATATYPE_FLOAT64 = 4
DATATYPE_STRING256 = 9
PERIOD_SIM_FRAME = 3
# Deliver every Nth sim frame rather than every frame. The detect loop consumes
# at 10 Hz; at ~42 fps every 2nd frame is ~21 Hz, which keeps 2x headroom while
# asking the sim to marshal less than half as much data for us to discard.
STATE_INTERVAL_FRAMES = 2
PERIOD_SECOND = 4
RECV_ID_SIMOBJECT_DATA = 8
OBJECT_ID_USER = 0
UNUSED = 0xFFFFFFFF

# SIMCONNECT_RECV_SIMOBJECT_DATA: 12-byte RECV header, then dwRequestID,
# dwObjectID, dwDefineID, dwFlags, dwentrynumber, dwoutof, dwDefineCount, then
# the payload. Confirmed against a live ghost: a FLOAT64 read at offset 40
# returned the object's true altitude.
DATA_OFFSET = 40
REQUEST_ID_OFFSET = 12

# Units are given explicitly. SimConnect hands back RADIANS for angles unless
# asked otherwise, which is the single most likely way for this to be subtly
# wrong, so every angle here says "degrees" and the caller does no conversion.
STATE_VARS = (
    ("PLANE LATITUDE", "degrees", "lat"),
    ("PLANE LONGITUDE", "degrees", "lon"),
    ("PLANE ALTITUDE", "feet", "alt"),
    ("VERTICAL SPEED", "feet per minute", "vs"),
    ("GROUND VELOCITY", "knots", "gs"),
    ("PLANE HEADING DEGREES TRUE", "degrees", "heading"),
    ("AIRSPEED INDICATED", "knots", "airspeed"),
    ("SIM ON GROUND", "bool", "on_ground_raw"),
    ("PLANE TOUCHDOWN NORMAL VELOCITY", "feet per second", "touchdown_fps"),
    ("PLANE PITCH DEGREES", "degrees", "pitch"),
    ("PLANE BANK DEGREES", "degrees", "bank"),
    ("IS SLEW ACTIVE", "bool", "slew_raw"),
    # Which camera the sim is showing. Flight views are 2-10; menus and other
    # UI states sit well above that, and CameraSet is refused while one of
    # those is up - the API answers every other call, so a replay looks like it
    # started and simply does nothing.
    ("CAMERA STATE", "number", "camera_state"),
    # What the pilot actually did with the gear. Replaying the recorded handle
    # beats any altitude rule we could invent: it is right for a fixed-gear
    # trainer, a retractable single and an airliner alike, with no per-airframe
    # tuning, and it reproduces the pilot's own timing.
    ("GEAR HANDLE POSITION", "percent", "gear"),
    # Same argument as gear: a fixed-wing ghost with the flaps up on every
    # approach is obviously wrong, and the recording knows what was set.
    ("FLAPS HANDLE PERCENT", "percent", "flaps"),
    # Recorded but not yet replayed. It costs one float and cannot be
    # recovered afterwards, whereas deciding how to replay it can wait for a
    # fixed-wing flight that actually shows it is wrong.
    ("SPOILERS HANDLE POSITION", "percent", "spoilers"),
    # Stall speed, which is what separates an airplane from a gyroplane when
    # the sim calls both of them "Airplane". A C172 reports 40 kt; a Magni M24
    # gyroplane reports 10, with a stall alpha and a wing area like any other
    # airplane. Zero on a helicopter.
    ("DESIGN SPEED VS0", "knots", "vs0"),
    # Height above the terrain, as the sim measures it. Grading previously
    # approximated this by subtracting the landing site elevation from the
    # altitude, which is only right over flat ground - the approach gates and
    # the lift-off height are wrong by the whole terrain relief otherwise.
    ("PLANE ALT ABOVE GROUND", "feet", "agl"),
    # Acceleration, measured rather than derived. Vertical and fore-aft
    # comfort are currently worked out by differentiating vertical speed and
    # ground speed one second apart, which can only see accelerations that
    # last longer than a second. These are sampled every push.
    ("G FORCE", "gforce", "gforce"),
    ("ACCELERATION BODY X", "feet per second squared", "accel_x"),
    ("ACCELERATION BODY Y", "feet per second squared", "accel_y"),
    ("ACCELERATION BODY Z", "feet per second squared", "accel_z"),
)

# Quantities where the extreme matters and the average hides it. The track is
# written once a second, so a single instantaneous reading of these lands on a
# random phase of whatever bump was happening; the highest and lowest seen
# since the last write is the number worth keeping.
PEAK_VARS = ("gforce", "accel_x", "accel_y", "accel_z", "vs")

# Cockpit, external, drone, fixed, environment, six-dof, gameplay, showcase,
# drone-aircraft. Anything outside this is a menu or a transition.
FLIGHT_CAMERA_STATES = (2, 3, 4, 5, 6, 7, 8, 9, 10)


def is_flight_camera(state):
    try:
        return int(round(float(state))) in FLIGHT_CAMERA_STATES
    except (TypeError, ValueError):
        return False

DEF_STATE = 40
DEF_TITLE = 41
DEF_LIVERY = 42
DEF_CATEGORY = 43
REQ_STATE = 4140
REQ_TITLE = 4141
REQ_LIVERY = 4142
REQ_CATEGORY = 4143


def parse_state(raw):
    """Decode a SIMOBJECT_DATA payload into a state dict. Pure function."""
    need = DATA_OFFSET + 8 * len(STATE_VARS)
    if raw is None or len(raw) < need:
        return None
    vals = struct.unpack_from("<%dd" % len(STATE_VARS), raw, DATA_OFFSET)
    out = {}
    for (_name, _unit, key), v in zip(STATE_VARS, vals):
        out[key] = v
    out["camera_state"] = int(round(out.get("camera_state") or 0))
    out["on_ground"] = bool(out.pop("on_ground_raw") >= 0.5)
    out["slew"] = bool(out.pop("slew_raw") >= 0.5)
    return out


def parse_title(raw):
    """Decode the STRING256 title payload."""
    if raw is None or len(raw) < DATA_OFFSET + 1:
        return None
    blob = raw[DATA_OFFSET:DATA_OFFSET + 256]
    end = blob.find(b"\0")
    if end >= 0:
        blob = blob[:end]
    try:
        return blob.decode("utf-8", "replace").strip() or None
    except Exception:
        return None


def request_id_of(raw):
    if raw is None or len(raw) < REQUEST_ID_OFFSET + 4:
        return None
    return struct.unpack_from("<I", raw, REQUEST_ID_OFFSET)[0]


class StateSampler(object):
    """Subscribes to the user aircraft's state and caches whatever arrives.

    `latest()` never blocks, so the caller's loop rate is independent of
    SimConnect round trips.
    """

    def __init__(self, fns, handle_fn, log=None):
        self.fns = fns
        self._h = handle_fn
        self._log = log or (lambda m: None)
        self._lock = threading.Lock()
        self._state = None
        self._state_at = 0.0
        self._peaks = {}
        self._title = None
        self._livery = None
        self._updates = 0
        self.subscribed = False

    # -- setup ----------------------------------------------------------

    def available(self):
        need = ("SimConnect_AddToDataDefinition", "SimConnect_RequestDataOnSimObject")
        return all(n in self.fns for n in need)

    def subscribe(self):
        """Build the definitions and start the subscription. True on success."""
        if not self.available():
            self._log("sampler: RequestDataOnSimObject not bound; staying on the legacy path")
            return False
        add = self.fns["SimConnect_AddToDataDefinition"]
        req = self.fns["SimConnect_RequestDataOnSimObject"]
        try:
            import ctypes
            from ctypes.wintypes import DWORD
            for name, unit, _key in STATE_VARS:
                hr = add(self._h(), DWORD(DEF_STATE), name.encode(), unit.encode(),
                         DWORD(DATATYPE_FLOAT64), ctypes.c_float(0.0), DWORD(UNUSED))
                if hr != 0:
                    self._log("sampler: AddToDataDefinition failed for %s hr=%s" % (name, hr))
                    return False
            hr = add(self._h(), DWORD(DEF_TITLE), b"TITLE", b"",
                     DWORD(DATATYPE_STRING256), ctypes.c_float(0.0), DWORD(UNUSED))
            if hr != 0:
                self._log("sampler: AddToDataDefinition failed for TITLE hr=%s" % hr)
                return False
            # TITLE is only the variant - "AS365 N2 - VIP" - and the livery is
            # a separate string, "EI-PRO - Executive Helicopters". A ghost
            # spawned from the title alone gets whichever livery the sim
            # fancies, so record both.
            hr = add(self._h(), DWORD(DEF_LIVERY), b"LIVERY NAME", b"",
                     DWORD(DATATYPE_STRING256), ctypes.c_float(0.0), DWORD(UNUSED))
            if hr != 0:
                self._log("sampler: LIVERY NAME unavailable hr=%s" % hr)
            # What the sim itself calls this aircraft: "Helicopter",
            # "Airplane", and so on. Confirmed against three of them rather
            # than assumed - it is the primary switch for which grading
            # profile a flight is judged on, so it is worth having in the
            # recording rather than inferred afterwards.
            hr = add(self._h(), DWORD(DEF_CATEGORY), b"CATEGORY", b"",
                     DWORD(DATATYPE_STRING256), ctypes.c_float(0.0), DWORD(UNUSED))
            if hr != 0:
                self._log("sampler: CATEGORY unavailable hr=%s" % hr)
            # Every sim frame for the hot state; the title only changes when the
            # aircraft does, so once a second is plenty.
            hr = req(self._h(), DWORD(REQ_STATE), DWORD(DEF_STATE), DWORD(OBJECT_ID_USER),
                     DWORD(PERIOD_SIM_FRAME), DWORD(0), DWORD(0),
                     DWORD(STATE_INTERVAL_FRAMES), DWORD(0))
            if hr != 0:
                self._log("sampler: RequestDataOnSimObject(state) hr=%s" % hr)
                return False
            hr = req(self._h(), DWORD(REQ_TITLE), DWORD(DEF_TITLE), DWORD(OBJECT_ID_USER),
                     DWORD(PERIOD_SECOND), DWORD(0), DWORD(0), DWORD(0), DWORD(0))
            if hr != 0:
                self._log("sampler: RequestDataOnSimObject(title) hr=%s" % hr)
            req(self._h(), DWORD(REQ_LIVERY), DWORD(DEF_LIVERY), DWORD(OBJECT_ID_USER),
                DWORD(PERIOD_SECOND), DWORD(0), DWORD(0), DWORD(0), DWORD(0))
            req(self._h(), DWORD(REQ_CATEGORY), DWORD(DEF_CATEGORY), DWORD(OBJECT_ID_USER),
                DWORD(PERIOD_SECOND), DWORD(0), DWORD(0), DWORD(0), DWORD(0))
        except Exception as e:
            self._log("sampler: subscribe failed %r" % (e,))
            return False
        self.subscribed = True
        self._log("sampler: subscribed, %d vars every %d sim frames"
                  % (len(STATE_VARS), STATE_INTERVAL_FRAMES))
        return True

    # -- receive --------------------------------------------------------

    def on_simobject_data(self, raw):
        """Feed a SIMOBJECT_DATA message in. Returns True if it was ours."""
        rid = request_id_of(raw)
        if rid == REQ_STATE:
            st = parse_state(raw)
            if st is not None:
                with self._lock:
                    self._state = st
                    self._state_at = time.time()
                    self._note_peaks(st)
                    self._updates += 1
            return True
        if rid == REQ_TITLE:
            title = parse_title(raw)
            if title:
                with self._lock:
                    self._title = title
            return True
        if rid == REQ_LIVERY:
            livery = parse_title(raw)
            if livery:
                with self._lock:
                    self._livery = livery
            return True
        if rid == REQ_CATEGORY:
            category = parse_title(raw)      # same STRING256 payload shape
            if category:
                with self._lock:
                    self._category = category
            return True
        return False

    def _note_peaks(self, st):
        """Widen the window extremes with this push. Caller holds the lock."""
        p = self._peaks
        for k in PEAK_VARS:
            v = st.get(k)
            if v is None:
                continue
            hi = p.get(k + "_max")
            if hi is None or v > hi:
                p[k + "_max"] = v
            lo = p.get(k + "_min")
            if lo is None or v < lo:
                p[k + "_min"] = v

    # -- read -----------------------------------------------------------

    def latest(self, max_age=2.0):
        """The most recent state, or None if nothing fresh has arrived."""
        with self._lock:
            st, at, title = self._state, self._state_at, self._title
            livery = self._livery
            category = getattr(self, "_category", None)
            # Handed over and the window restarted, so nothing is counted
            # twice. The caller reads faster than it writes the track, so it
            # has to keep merging these until it records a point.
            peaks, self._peaks = self._peaks, {}
        if st is None or (time.time() - at) > max_age:
            return None
        out = dict(st)
        out["aircraft"] = title
        out["livery"] = livery
        out["category"] = category
        out["peaks"] = peaks
        # When SimConnect delivered this, which is when the aircraft was
        # actually at these coordinates. The caller must timestamp the
        # sample with this rather than with its own clock: states arrive
        # on sim frames and are read on a slower poll, so the position is
        # up to one frame stale by a varying amount.
        out["at"] = at
        return out

    def stats(self):
        with self._lock:
            return {"subscribed": self.subscribed, "updates": self._updates,
                    "age": (time.time() - self._state_at) if self._state_at else None,
                    "title": self._title, "livery": self._livery}


# ---------------------------------------------------------------- offline test


def _synthetic(values, request_id=REQ_STATE):
    """Build a SIMOBJECT_DATA buffer the way the sim would."""
    body = struct.pack("<%dd" % len(values), *values)
    head = struct.pack("<III", DATA_OFFSET + len(body), 6, RECV_ID_SIMOBJECT_DATA)
    mid = struct.pack("<IIIIIII", request_id, OBJECT_ID_USER, DEF_STATE, 0, 0, 1,
                      len(values))
    return head + mid + body


def self_test():
    """Verify layout and parsing with no sim running."""
    # Keyed by name, not position: a bare list silently went stale here once
    # already, when four variables were added and this was left at thirteen.
    # Missing an entry now names the variable that is missing.
    fixture = {
        "lat": 40.2569, "lon": -105.7989, "alt": 8502.7, "vs": -66.1,
        "gs": 42.5, "heading": 4.3, "airspeed": 45.0, "on_ground_raw": 1.0,
        "touchdown_fps": -2.5, "pitch": -4.6, "bank": 0.3, "slew_raw": 0.0,
        "camera_state": 2.0, "gear": 100.0, "flaps": 0.0, "spoilers": 0.0,
        "vs0": 0.0, "agl": 312.5, "gforce": 1.08, "accel_x": 0.4,
        "accel_y": 2.1, "accel_z": -1.3,
    }
    missing = [k for _n, _u, k in STATE_VARS if k not in fixture]
    assert not missing, "test vector is missing %s" % ", ".join(missing)
    vals = [fixture[k] for _n, _u, k in STATE_VARS]
    raw = _synthetic(vals)

    assert request_id_of(raw) == REQ_STATE
    st = parse_state(raw)
    assert st is not None, "parse returned nothing"
    assert abs(st["lat"] - 40.2569) < 1e-9, st["lat"]
    assert abs(st["lon"] + 105.7989) < 1e-9, st["lon"]
    assert abs(st["alt"] - 8502.7) < 1e-9, st["alt"]
    assert abs(st["vs"] + 66.1) < 1e-9, st["vs"]
    assert abs(st["heading"] - 4.3) < 1e-9, st["heading"]
    assert st["on_ground"] is True, st["on_ground"]
    assert st["slew"] is False, st["slew"]
    assert abs(st["pitch"] + 4.6) < 1e-9, st["pitch"]

    assert abs(st["agl"] - 312.5) < 1e-9, st["agl"]
    assert abs(st["gforce"] - 1.08) < 1e-9, st["gforce"]

    # a short buffer must be refused rather than read past the end
    assert parse_state(raw[:DATA_OFFSET + 8]) is None
    assert parse_state(None) is None

    # the title arrives on its own request id
    tbody = b"AS365 N2 - VIP\0" + b"\0" * 200
    thead = struct.pack("<III", DATA_OFFSET + len(tbody), 6, RECV_ID_SIMOBJECT_DATA)
    tmid = struct.pack("<IIIIIII", REQ_TITLE, OBJECT_ID_USER, DEF_TITLE, 0, 0, 1, 1)
    assert parse_title(thead + tmid + tbody) == "AS365 N2 - VIP"

    # the cache should reject a state that has gone stale
    s = StateSampler({}, lambda: None)
    assert s.on_simobject_data(raw) is True
    assert s.latest() is not None
    assert s.on_simobject_data(_synthetic(vals, request_id=99)) is False
    # peaks span every push since the last read, and a read starts a new
    # window - the caller reads ten times per recorded point, so double
    # counting or losing a window would both corrupt the ride numbers.
    p = StateSampler({}, lambda: None)
    p.on_simobject_data(_synthetic([fixture[k] for _n, _u, k in STATE_VARS]))
    spike = dict(fixture)
    spike["gforce"] = 1.94
    p.on_simobject_data(_synthetic([spike[k] for _n, _u, k in STATE_VARS]))
    got = p.latest()["peaks"]
    assert abs(got["gforce_max"] - 1.94) < 1e-9, got
    assert abs(got["gforce_min"] - 1.08) < 1e-9, got
    assert p.latest()["peaks"] == {}, "a read must start a fresh window"

    s._state_at = time.time() - 30
    assert s.latest(max_age=2.0) is None, "stale state must not be served"
    return True


if __name__ == "__main__":
    print("offline self-test:", "PASS" if self_test() else "FAIL")
    print("variables subscribed:", len(STATE_VARS))
    for n, u, k in STATE_VARS:
        print("   %-34s %-18s -> %s" % (n, u, k))

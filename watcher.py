import bisect
import clipfile
import ctypes
import glob
import json
import math
import os
import re
import shutil
import struct
import sys
import threading
import persistence
import time
from collections import deque
from ctypes import POINTER, Structure, byref, cast, sizeof
from ctypes.wintypes import DWORD, HANDLE, HWND, LPCSTR
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
import mimetypes
import posixpath

# Where this file is, not where it was written. This was an absolute path to
# one developer's machine, which meant a second copy of the tree read and wrote
# the FIRST copy's sessions, logs and lock - and on anyone else's machine it
# pointed at a folder that does not exist. Every other module already derived
# it this way; watcher.py and snapshot.py were the two that did not.
BASE = os.path.dirname(os.path.abspath(__file__))
SESSIONS = os.path.join(BASE, "sessions")
CLIPS_DIR = os.path.join(SESSIONS, "clips")
PID_FILE = os.path.join(BASE, "watcher.pid")
LOCK_FILE = os.path.join(BASE, "watcher.lock")
CURRENT = os.path.join(BASE, "current.json")
LAST_EVENT = os.path.join(BASE, "last_event.json")
NOTIFY_STATE = os.path.join(BASE, "notify_state.json")
EVENTS_JSONL = os.path.join(BASE, "events.jsonl")
LOG_FILE = os.path.join(BASE, "watcher.log")
HTTP_HOST = "127.0.0.1"
# Bump on a release. describe_version() refines this from git when the repo is
# there, so a working copy reports exactly which commit is running rather than
# just the last version someone remembered to edit.
APP_VERSION = "0.5.0"
HTTP_PORT = 8742
# How the detect loop gets its data.
#   legacy - one blocking python-SimConnect get() per variable. ~2.8 Hz.
#   shadow - legacy still drives recording, the push sampler runs alongside and
#            its values are compared and logged. Costs the same as legacy and
#            cannot affect what is recorded. This is the default until a live
#            flight confirms the two agree.
#   fast   - the push sampler drives recording, with an automatic fall back to
#            legacy for any tick where no fresh push has arrived.
# Validated against a live sim on 31 Aug 2026: every field matched the legacy
# path to four decimals (heading 359.59 confirming degrees, not radians), the
# title decoded, and the push rate measured 42.4 Hz against a legacy ceiling of
# 1.67 Hz. "fast" still falls back to a legacy read for any tick where no fresh
# push has arrived.
SAMPLER_MODE = "fast"
SAMPLER_COMPARE_SEC = 20.0

POLL_SEC = 1.0
# 10 Hz. This was 0.2 (5 Hz) back when one sample cost ~0.6 s of blocking
# SimConnect reads; with the push sampler a sample costs nothing measurable, so
# the constant was the only thing holding capture down. Doubles the telemetry
# density behind every replay. BUFFER_MAX derives from this, so the 60 s ring
# grows to ~616 samples, which is still nothing.
SAMPLE_SEC = 0.1
# 60 s of history at 5 Hz. The landing clip reaches 55 s back, so the ring has
# to hold at least that much or the start of the approach is already gone.
BUFFER_SEC = 60.0
BUFFER_MAX = int(BUFFER_SEC / SAMPLE_SEC) + 16
RECONNECT_SEC = 5.0
CONNECT_TIMEOUT = 15.0
BOUNCE_SEC = 2.0
# A touchdown that leaves the ground again within this long is a bounce, not a
# separate landing: the arrival is the first contact, and the firmest contact is
# what the landing felt like. Longer than this and the aircraft really did get
# airborne again, which is a touch-and-go and two events.
BOUNCE_MERGE_SEC = 3.0

# PLANE TOUCHDOWN NORMAL VELOCITY reads exactly 0.0 when the sim has not
# latched a touchdown, which is not the same as arriving with no vertical
# speed. Taken literally it graded a leg that touched down at 91 fpm as
# '0 fpm, grade A, Butter' - the sim said 0.0, the recorded vertical speed
# said -91.1, and the zero won because it was merely finite.
#
# Below this the reading is treated as absent and the aircraft's own
# vertical speed is used instead. 0.01 ft/s is 0.6 fpm: far under any
# arrival worth grading, and far over the float noise on a real reading.
TOUCHDOWN_FPS_MIN = 0.01
SLEW_JUMP_NM = 0.5
RESUME_JUMP_NM = 5.0
ATOMIC_WRITE_TRIES = 5
ATOMIC_WRITE_SLEEP = 0.05
# An open clip is rewritten whole as it grows. At 10 Hz a 60 s clip is ~600
# points, so rewriting once a second is a lot of churn on the disk the sim is
# streaming from. In-progress writes are unsynced and spaced out; the write at
# close is synced. A hard crash therefore loses at most this many seconds of an
# open clip.
CLIP_WRITE_SEC = 5.0
# finish() has no next checkpoint to fall back on, so its append retries here
# instead - a small fixed number, not an unbounded loop on a thread that has a
# sortie to keep recording.
CLIP_FINISH_TRIES = 3
CLIP_FINISH_SLEEP = 0.05
# Rotate the log rather than letting it grow forever.
# The detect loop stamps this every time round. Answering on the HTTP port is
# not proof of life - the server is its own thread, so a watcher whose detect
# loop is wedged still answers while recording nothing. On 3 Sep 2026 one
# stopped logging at 22:55:44, never noticed the sim close, and was killed by
# Windows as unresponsive 46 minutes later; nothing restarted it until morning.
_HEARTBEAT = [time.time()]
_STALL_REPORTED = [False]
# Longer than any single pass should ever take. The loop turns every
# SAMPLE_SEC; a bake or a rebuild runs on other threads.
STALL_WARN_SEC = 30.0

LOG_MAX_BYTES = 4 * 1024 * 1024
LOG_KEEP = 2
# A deferred rebuild may run once the aircraft has sat still this long.
LOGBOOK_PARKED_SEC = 60.0
# 60 s per clip either way: a takeoff is mostly what happens after it, a
# landing mostly what happens before it.
TAKEOFF_BEFORE = 5.0
TAKEOFF_AFTER = 55.0
LANDING_BEFORE = 55.0
LANDING_AFTER = 5.0
CLIP_ID_RE = re.compile(r"^flt-[A-Za-z0-9T_-]+-leg\d+-(takeoff|landing)$")
USER_OBJECT_ID = 0
# EvenAR/node-simconnect PositionReferential (MSFS 2024 SDK 1.1.2 headers have no CameraAcquire/SIMOBJECT)
SIMCONNECT_POSITION_REFERENTIAL_SIMOBJECT = 1
CAMERA_MASK_POSITION = 0x01
CAMERA_MASK_ROTATION = 0x02
CAMERA_MASK_TARGETED = 0x04
CAMERA_MASK_FOV = 0x08
CAMERA_MASK_REFERENTIAL = 0x10
CAMERA_MASK_ALL_ROTATION = (
    CAMERA_MASK_POSITION | CAMERA_MASK_ROTATION | CAMERA_MASK_FOV | CAMERA_MASK_REFERENTIAL
)
CAMERA_MASK_ALL_TARGETED = (
    CAMERA_MASK_POSITION | CAMERA_MASK_TARGETED | CAMERA_MASK_FOV | CAMERA_MASK_REFERENTIAL
)
# Camera modes.
#   place  - put the camera at the event once, then do not touch it again, so
#            the sim's own camera controls (your control pad) drive it.
#   follow - re-aim every frame, a locked chase. Smooth, but it overwrites any
#            input you give 20 times a second.
CHASE_MODE_PLACE = "place"
CHASE_MODE_FOLLOW = "follow"
CHASE_MODES = (CHASE_MODE_PLACE, CHASE_MODE_FOLLOW)
# Follow by default. A replay is normally watched to see a whole takeoff or
# approach, and a planted camera has the aircraft out of frame within seconds
# of the event - so the first thing most people did was reach for the tick box.
# Planted is the deliberate choice, for a cinematic flyby, rather than the one
# you have to undo every time. Settable: see chase_follow in settings.py.
CHASE_MODE_DEFAULT = CHASE_MODE_FOLLOW

# Roomier than a tight chase: this is an exterior vantage on the event, and you
# move it from there.
CHASE_DISTANCE_DEFAULT = 45.0
CHASE_HEIGHT_DEFAULT = 15.0
CHASE_ORBIT_DEFAULT = 20.0
CHASE_FOV_RAD = 0.80

# The chase camera is aimed with TargetPosition, so the sim works out pitch
# and heading. Roll is not part of that, and no CameraSet we send carries
# CAMERA_MASK_ROTATION - so the bank written into the struct has never once
# been applied. The camera simply keeps whatever roll it had when it was
# acquired, which is why the horizon is tilted on some replays and level on
# others: it depends on the attitude the shot started from.
#
# Reading the angles back to preserve them does not work: CameraGet does
# not report the effective chase orientation. Measured over an 80 second
# replay it returned pitch=5.79 heading=25.76 bank=-0.10 on all 40 samples,
# unchanged, while the ghost's heading swung 178 to 185 and the camera was
# visibly tracking it. The reply is not the write struct either - offset 24,
# the referential when writing, comes back holding our own CameraGet request
# id, and the position comes back as small aircraft-relative meters.
#
# So the angles have to be computed rather than read, which means aiming
# with Pbh instead of TargetPosition. That is the only way to say anything
# about roll at all.
#   "angles"  compute pitch and heading, force bank to 0 - level horizon
#   "target"  hand the sim a look-at point - simpler, but roll is whatever
#             the camera happened to have, which is the tilt being fixed
CHASE_AIM_MODE = "angles"
# A locked camera is placed on a bearing off the ghost's heading, with a long
# arm, so heading noise becomes visible camera swing. Smooth the heading the
# camera uses, and stop tracking it altogether below walking pace - a hovering
# or just-lifted helicopter yaws around with no real direction of travel, which
# is exactly the first few seconds of a takeoff clip.
CAMERA_HEADING_SMOOTH = 0.06
CAMERA_HEADING_MIN_GS = 6.0

# Playback drives the ghost with OnGround=0 so the sim honors the recorded
# altitude instead of clamping to terrain. The cost is that the sim then thinks
# a retractable-gear aircraft is airborne and pulls the gear up: measured on an
# AS365 ghost, GEAR TOTAL PCT EXTENDED sat at 0% for the whole of both a
# takeoff and a landing clip, where passing the recorded flag through animated
# it 100->0 and 0->100 correctly. With the wheels stowed the airframe has
# nothing to rest on and the belly settles to where the gear should be, which
# is the touchdown sink. So keep OnGround=0 and command the gear ourselves,
# down whenever the clip is within this much of the altitude it touches the
# ground at. The sim's own AI logic keeps trying to retract it, so the command
# is repeated rather than sent once on change.
# Drive the ghost's gear from the recording. Off falls back to GEAR_DOWN_AGL_FT
# for every clip, which is also what happens for a clip carrying no gear
# captured.
# What a flap command writes on the ghost. The handle is what the pilot
# moved and is kept so anything reading the selection sees it; the two
# trailing-edge surfaces are what is actually drawn. Writing the handle alone
# was accepted with S_OK and moved nothing - the first fixed-wing replay, a
# Cessna 152, flew its whole approach with the flaps up while the recording
# held 0, 33 and 67 percent.
GHOST_FLAP_VARS = (
    b"FLAPS HANDLE PERCENT",
    b"TRAILING EDGE FLAPS LEFT PERCENT",
    b"TRAILING EDGE FLAPS RIGHT PERCENT",
)

# The gear equivalent of what the flaps needed. GEAR HANDLE POSITION is a
# selection; on a driven object the sim's own AI keeps moving it back, and what
# is DRAWN follows the per-gear positions. Repeating the handle every frame was
# enough on an AS365, where it animated 100->0 and 0->100 correctly. It is not
# enough on a Vision Jet: the gear retracts around touchdown and re-extends,
# which is the AI winning the handle for a few frames at the point its own
# state changes.
#
# Writing the positions as well means the AI can win the handle and still not
# move the wheels. They go in their OWN data definition and their own call, so
# that if an airframe refuses them the handle write - which is all this had
# before, and which works on some aircraft - is not lost with them.
#
# UNVERIFIED against a running sim at the time of writing. AddToDataDefinition
# accepts any name and SetDataOnSimObject returns S_OK either way, so neither
# proves the wheels moved. Watch a Vision Jet landing replay before believing
# this, exactly as the flaps fix had to be watched.
GHOST_GEAR_SURFACE_VARS = (
    b"GEAR CENTER POSITION",
    b"GEAR LEFT POSITION",
    b"GEAR RIGHT POSITION",
)

GHOST_GEAR_FROM_RECORDING = True
GEAR_DOWN_AGL_FT = 300.0
GEAR_REFRESH_SEC = 0.5
# How often the camera is re-asserted once a clip has finished. The sim takes
# the view back to the user aircraft the moment we stop setting it.
CAMERA_HOLD_SEC = 0.25

# The ghost is an AI aircraft, so the sim keeps integrating its motion between
# our position writes: it carries the object forward from the airspeed we hand
# it, then our next write at REPLAY_HZ snaps it back. The visible result is an
# aircraft flickering between two positions about (speed * 1/REPLAY_HZ) apart -
# nothing at a hover, worse the faster it goes - and an object that settles
# through the surface on touchdown. Position simvars cannot see this: they read
# back whatever we last wrote, which is why measuring them showed clean
# tracking while the render was visibly wrong.
#
# Two independent levers, kept separate so they can be bisected if one of them
# turns out to do nothing:
#   GHOST_FREEZE         - stop the sim integrating the object at all
#   GHOST_ZERO_AIRSPEED  - give it no velocity to carry forward with
GHOST_FREEZE = True
GHOST_ZERO_AIRSPEED = True

# Replays open held at the first frame so the shot can be set up before
# anything moves. Press play in the UI to run.
REPLAY_START_PAUSED = True

# Touchdown sits the wheels slightly under the surface, and the arithmetic says
# why. Measured at a real landing: the sim's own ground clamp rests the
# aircraft datum 5.04 ft above the terrain mesh, while the recorded resting
# altitude puts it at 6.91 ft. That 1.87 ft is the tarmac standing proud of the
# mesh, which is why OnGround=1 sank it - the clamp ignores the apron. So the
# recorded altitude is right and the datum lands where it should; what is left
# over is gear compression. The recording was taken with weight on the wheels
# and the struts squashed, but the ghost is told it is airborne, so its gear
# hangs fully extended and the tyres end up below the datum by the compression
# distance.
#
# There is no simvar for how far a strut has squashed, so this is a calibration
# knob rather than a measurement: raise the ghost by this much where it meets
# the ground, blended out over the next few feet so nothing steps.
#
# 0.5 was a first guess and it landed - confirmed by eye on the AS365 N2, no
# sinking and no hovering. Treat it as known-good for that airframe rather than
# as a universal constant; another aircraft with longer travel may want more.
GHOST_GROUND_LIFT_FT = 0.5
GHOST_GROUND_LIFT_BLEND_FT = 8.0
FREEZE_EVENTS = (
    ("FREEZE_LATITUDE_LONGITUDE_SET", 9101),
    ("FREEZE_ALTITUDE_SET", 9102),
    ("FREEZE_ATTITUDE_SET", 9103),
)
SIMCONNECT_EVENT_FLAG_GROUPID_IS_PRIORITY = 16
NATIVE_DIR = os.path.join(BASE, "native")
GAME_DLL_FILENAME = "SimConnect_internal.dll"
NATIVE_DLL = os.path.join(NATIVE_DIR, GAME_DLL_FILENAME)
CAMERA_CLIENT_NAME = b"msfs-logger-chase"
SIMCONNECT_UNUSED = 0xFFFFFFFF
SIMCONNECT_DATATYPE_INITPOSITION = 12
SIMCONNECT_RECV_ID_EXCEPTION = 1
SIMCONNECT_RECV_ID_OPEN = 2
SIMCONNECT_RECV_ID_QUIT = 3
SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID = 12
SIMCONNECT_RECV_ID_CAMERA_STATUS = 41
SIMCONNECT_RECV_ID_CAMERA = 40
SIMCONNECT_RECV_ID_SIMOBJECT_DATA = 8

# The real camera struct is 84 bytes. CameraSet's packet is 0x68 = 104 = a
# 16-byte header + an 84-byte struct + a 4-byte mask, and CameraGet replies with
# exactly 84 bytes. The old 96-byte ctypes struct was rejected by the sim with
# exception 46 on every single call - that is why the camera never moved, and
# why nothing else changed the behavior. Offsets confirmed by reading a live
# camera back and echoing it with a known delta.
CAMERA_STRUCT_SIZE = 84
CAM_OFF_POSITION = 0        # 3 doubles
CAM_OFF_REFERENTIAL = 24    # u32
CAM_OFF_UNKNOWN_28 = 28     # u32, always 0
CAM_OFF_TARGETED = 32       # 3 doubles
CAM_OFF_ROT_REFERENTIAL = 68  # u32
CAM_OFF_FOV = 76            # double
CAM_OFF_TARGET = 32         # 3 doubles, unused when we aim with Pbh
# Pbh is three FLOATS in DEGREES, not doubles in radians. Identified by reading
# the same camera in two referentials with the aircraft heading known (246.61):
#   off60 aircraft-relative = -40.445, world = -153.852
#   -153.852 + 360 + 40.445 = 246.59  -> off60 is the heading, in degrees
# Bank sits after heading, which is why the field order is pitch/heading/bank.
# Write order is Pitch, Bank, Heading - the ordinary PBH triple. The REPLY
# uses a different order (pitch, heading, bank), which is easy to be fooled by:
#   wrote 60=0  64=90   -> read 60=90  64=0
#   wrote 60=90 64=0    -> read 60=0   64=90
#   wrote 64=200        -> read 60=-160   (wraps at +/-180, so it is a heading)
CAM_OFF_PITCH = 56          # float, degrees
CAM_OFF_BANK = 60           # float, degrees
CAM_OFF_HEADING = 64        # float, degrees

# Referentials, confirmed against a live sim by asking for each in turn and
# seeing which reported the aircraft's true position:
#   0 Aircraft  - meters from the aircraft datum
#   1 Eyepoint  - meters from the eyepoint
#   2 World     - LATITUDE, LONGITUDE, ALTITUDE (degrees, degrees, METERS)
# There is no SimObject referential: the camera cannot be attached to an AI
# object. World is what lets us put it at an event miles away instead.
CAMERA_REFERENTIAL_AIRCRAFT = 0
CAMERA_REFERENTIAL_EYEPOINT = 1
CAMERA_REFERENTIAL_WORLD = 2
CAMERA_REFERENTIAL_DEFAULT = CAMERA_REFERENTIAL_WORLD

# Position and rotation have to go in SEPARATE CameraSet calls. Sending them
# together (0x0F) applies the position and silently drops the rotation, which is
# why the camera arrived at the event pointing the wrong way.
CAMERA_MASK_POSITION_ONLY = 0x01
CAMERA_MASK_ROTATION_ONLY = 0x02
# Aiming is done with TargetPosition rather than Pbh: we hand the sim the
# ghost's world coordinates and let it work out the angles. That sidesteps the
# pitch sign convention entirely - the sim aims DOWN with POSITIVE pitch, so
# computing it by hand had the camera looking 18 degrees the wrong way, which
# with a 46 degree field of view put the ghost just out of frame.
CAMERA_MASK_TARGET_ONLY = 0x04

# CameraEnableFlag / CameraDisableFlag bits. Sweeping 0-7 against a live sim,
# 1 and 2 are accepted and 0 and 4 are refused with exception 46, which matches
# the two documented flags. INTERACTION is the one that lets a control pad move
# the camera we placed; ABOVE_GROUND keeps it from sinking through terrain.
# Off: enabling interaction snaps the camera back to the aircraft (see
# camera_acquire). Left here so the finding is not lost.
CAMERA_ENABLE_INTERACTION = False
CAMERA_FLAG_INTERACTION = 0x01
CAMERA_FLAG_ABOVE_GROUND = 0x02

EARTH_RADIUS_M = 6371000.0
FEET_PER_METER = 3.280839895
CAMERA_ACQUIRED = 1
SIMCONNECT_DATATYPE_FLOAT64 = 4
SPAWN_LAT_ABS = 1.0
SPAWN_LON_90_ABS = 1.0
# Driving the ghost costs ~0.01 ms a call (SetData and CameraSet are both
# fire-and-forget), so the rate is limited by pacing, not by the sim.
# Chosen to divide evenly into the display's frame rate, which matters more
# than being merely high. A rate that does not divide evenly leaves some
# frames with one update and some with two, so the camera advances by
# different amounts frame to frame - visible as fine judder even when every
# update is on time. 90 Hz against this machine's 60 Hz monitor is 1.5
# updates per frame, which beats at 30 Hz.
#
#   rate    60 Hz monitor   90 Hz Pimax
#    60        1.00            0.67
#    90        1.50            1.00
#   120        2.00            1.33
#   180        3.00            2.00     <- exact on both
#
# 180 is the lowest rate that lands whole on the flat monitor and the
# headset, so replays look the same on either without retuning. Measured
# cost: 0.07 ms of work per pass, so 1.3% of the loop's budget.
#
# Safe to raise because the flicker the GHOST_FREEZE note above describes is
# the sim integrating the object between our writes, and that is switched
# off. Its amplitude is speed / REPLAY_HZ, so a higher rate can only shrink
# whatever is left of it.
REPLAY_HZ = 180.0
# Whether to log the rate the replay loop actually achieves. Asking for a
# rate and reaching it are different things: each pass does a pose write, a
# gear write and two CameraSet calls, so at 90 Hz that is 360 SimConnect
# calls a second. If the loop cannot keep up, raising the target makes the
# stepping worse rather than better, and only a measurement can say which.
REPLAY_RATE_LOG = False

# How often the CAMERA is moved. THIS MUST NORMALLY EQUAL REPLAY_HZ.
#
# Terrain is world-fixed, so all its apparent motion comes from the camera,
# and it was tempting to raise this alone and leave the tuned ghost rate
# untouched. Doing so was tried and made things worse: the ghost only looks
# steady because the camera is computed from its own pose, so the two step
# together and ghost-relative motion stays near zero. Decoupling them broke
# that cancellation - the terrain improved slightly and the aircraft began
# stuttering against a view that was moving twice as often as it was.
#
# Kept as its own name so the two can still be bisected deliberately, not so
# they can drift apart by accident.
CHASE_HZ = REPLAY_HZ
# Pace with time.sleep, not Event.wait: on Windows Event.wait is quantized to
# the 15.625 ms timer tick, so asking it for 50 ms actually waits 62.3 ms and
# the loop ran at 16 Hz instead of 20. time.sleep(0.05) measures 50.3 ms.
# Slice the wait so stop and pause still respond promptly.
REPLAY_SLEEP_SLICE = 0.008

# How many camera writes in a row may fail before the chase gives up and hands
# the view back. One used to be enough, and at REPLAY_HZ that is one bad round
# trip in thousands: a CameraGet baseline that times out while a slider is
# being dragged ended the shot and returned the view to the user's aircraft,
# which is what a release does. Only a camera that is genuinely gone fails
# every pass, so a run of them is the signal.
#
# The budget is in FRAMES, not seconds, because seconds are not knowable here:
# REPLAY_HZ is the rate asked for and nothing measures what a given PC
# delivers (REPLAY_RATE_LOG is off by default). 60 frames is a third of a
# second only if the loop is holding 180, and proportionally longer if it is
# not. Turn REPLAY_RATE_LOG on before quoting a wall time for this.
CHASE_FAIL_TOLERANCE = 60

os.makedirs(SESSIONS, exist_ok=True)
os.makedirs(CLIPS_DIR, exist_ok=True)

RUNTIME_LOCK = threading.RLock()
RUNTIME = {
    "connected": False,
    "sm": None,
    "flight_id": None,
    "leg": 0,
    "recent_events": deque(maxlen=24),
    "open_clip_ids": [],
    "sample_hz": None,
    # /state is polled by the EFB every 2 s from inside the sim. These are the
    # documents this process just wrote, so serve them from here rather than
    # reading our own files back off disk on every request.
    "camera_state": None,
    "current_doc": None,
    "last_event_doc": None,
    "replay": {
        "active": False,
        "clip_id": None,
        "elapsed_s": None,
        "duration_s": None,
        "aircraft": None,
        "aircraft_requested": None,
        "object_id": None,
        "error": None,
        "stop": None,
        "thread": None,
        "ghost_def": None,
        "gsc": None,
        "chase": {
            "distance": CHASE_DISTANCE_DEFAULT,
            "height": CHASE_HEIGHT_DEFAULT,
            "orbit": CHASE_ORBIT_DEFAULT,
            "camera_acquired": False,
            "mode": CHASE_MODE_DEFAULT,
            "referential": CAMERA_REFERENTIAL_DEFAULT,
            # Locked: hold the vantage relative to the ghost as it moves,
            # instead of planting the camera and letting the ghost fly away.
            #   "aircraft" - the offset turns with the aircraft, so the same
            #                face of it stays toward you.
            #   "world"    - the compass bearing is held, so the aircraft turns
            #                in front of a camera flying alongside.
            "lock_bearing": "aircraft",
            "abs_bearing": None,
            "recenter": 0,
        },
        "last_pose": None,
        "cam_hdg": None,
        # True once a clip has played out but the camera is still parked at the
        # event, waiting for Stop. The ghost stays put too.
        "holding": False,
        # Frozen mid-clip. The ghost stays where it is and the camera knobs keep
        # working, so a shot can be set up before letting it run on.
        "paused": False,
        # Where the camera was planted, in world LLA. Place mode holds this and
        # only re-aims, so the shot stays put while the ghost flies through it.
        "cam_anchor": None,
    },
}


def now_utc():
    return datetime.now(timezone.utc)

def now_iso():
    return now_utc().isoformat()

_log_lock = threading.Lock()


def _rotate_log_if_big():
    try:
        if os.path.getsize(LOG_FILE) < LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        for i in range(LOG_KEEP - 1, 0, -1):
            older = "%s.%d" % (LOG_FILE, i)
            newer = "%s.%d" % (LOG_FILE, i - 1) if i > 1 else LOG_FILE
            if os.path.isfile(newer):
                os.replace(newer, older)
    except Exception:
        pass


def beat():
    """Say the loop is still turning. Cheap enough to call every pass."""
    _HEARTBEAT[0] = time.time()
    _STALL_REPORTED[0] = False


def heartbeat_age():
    return max(0.0, time.time() - _HEARTBEAT[0])


def log(msg):
    line = now_iso() + " " + str(msg) + "\n"
    try:
        with _log_lock:
            _rotate_log_if_big()
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass
    try:
        print(line, end="", flush=True)
    except Exception:
        pass

def atomic_write(path, obj, fsync=True, compact=False):
    """Write JSON atomically.

    fsync=False still produces a complete file via the same rename, it just does
    not force the write through to physical disk. Use it for anything that is
    rewritten again moments later: fsync measured 1.0-1.8 ms here, and a stall
    on the disk the sim streams from is worth more than the durability of a
    status file.
    """
    for attempt in range(ATOMIC_WRITE_TRIES):
        try:
            return persistence.atomic_json(path, obj, fsync=fsync,
                                           compact=compact)
        except PermissionError:
            if attempt + 1 == ATOMIC_WRITE_TRIES:
                raise
            time.sleep(ATOMIC_WRITE_SLEEP * (attempt + 1))


def decode_val(v):
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace").strip("\x00").strip()
    if isinstance(v, str):
        return v.strip("\x00").strip()
    return v

def finite(n):
    try:
        return math.isfinite(float(n))
    except Exception:
        return False

def as_float(v):
    if v is None:
        return None
    try:
        x = float(v)
        if math.isfinite(x):
            return x
        return None
    except Exception:
        return None

def as_bool(v):
    if v is None:
        return None
    try:
        return bool(int(float(v)))
    except Exception:
        return bool(v)

def rad_to_deg(v):
    x = as_float(v)
    if x is None:
        return None
    if abs(x) <= (math.pi + 0.05):
        return x * 180.0 / math.pi
    return x

def wrap_deg(x):
    x = float(x) % 360.0
    if x < 0:
        x += 360.0
    return x

def heading_to_deg(v):
    """PLANE_HEADING_DEGREES_* from python-simconnect 0.4.26 arrives as radians."""
    x = as_float(v)
    if x is None:
        return None
    if abs(x) <= (2.0 * math.pi + 0.15):
        x = x * 180.0 / math.pi
    return wrap_deg(x)

def pose_heading_deg(h, att_unit=None):
    x = as_float(h)
    if x is None:
        return 0.0
    if att_unit == "deg":
        return wrap_deg(x)
    if att_unit == "rad":
        return wrap_deg(x * 180.0 / math.pi)
    if abs(x) <= (2.0 * math.pi + 0.15):
        return wrap_deg(x * 180.0 / math.pi)
    return wrap_deg(x)

def is_spawn_junk(lat, lon):
    if not finite(lat) or not finite(lon):
        return True
    if abs(float(lat)) < SPAWN_LAT_ABS and abs(float(lon) - 90.0) < SPAWN_LON_90_ABS:
        return True
    if abs(float(lat)) < 0.05 and abs(float(lon)) < 0.05:
        return True
    return False

def iso_from_unix(t):
    try:
        return datetime.fromtimestamp(float(t), tz=timezone.utc).isoformat()
    except Exception:
        return None

def interp_num(a, b, f):
    if a is None:
        return b
    if b is None:
        return a
    try:
        return float(a) + (float(b) - float(a)) * f
    except Exception:
        return b

def interp_heading_deg(a, b, f):
    a = wrap_deg(a or 0.0)
    b = wrap_deg(b or 0.0)
    d = (b - a + 180.0) % 360.0 - 180.0
    return wrap_deg(a + d * f)

def resolve_att_unit(points, att_unit=None):
    """Decide a clip's heading unit once, from the whole clip.

    Only heading was ever recorded in radians. Pitch and bank have always been
    degrees, even in a clip written before att_unit existed - such a clip
    carries heading 0.14-6.28 alongside pitch -5.8..+3.4 and bank -3.6..+1.8.
    Converting those as radians would turn 5.7 degrees of pitch into 327, so
    this resolves heading only and pitch/bank are passed through untouched.

    Deciding per sample was the hazard: "<= 2 pi means radians" is true of most
    individual degree headings too, so one clip could have some samples
    converted and others not, silently mangled mid-clip. Looking at the whole
    range instead means a single heading above 2 pi settles it for every sample.

    A degrees clip whose heading never leaves 0-6.28 for its entire length is
    genuinely ambiguous - that is an aircraft pointing within six degrees of
    north the whole time, and it resolves to radians. Anything recorded since
    att_unit exists carries the label and never reaches the guess.
    """
    if att_unit in ("deg", "rad"):
        return att_unit
    hi = 0.0
    for p in points or []:
        if not isinstance(p, dict):
            continue
        h = as_float(p.get("heading"))
        if h is not None:
            hi = max(hi, abs(h))
    return "rad" if hi <= (2.0 * math.pi + 0.15) else "deg"


def normalize_clip_points(points, att_unit=None):
    unit = resolve_att_unit(points, att_unit)
    out = []
    for p in points:
        if not isinstance(p, dict):
            continue
        if is_spawn_junk(p.get("lat"), p.get("lon")):
            continue
        q = dict(p)
        # Heading only. Pitch and bank were always degrees.
        q["heading"] = pose_heading_deg(p.get("heading"), unit)
        pitch = as_float(p.get("pitch"))
        bank = as_float(p.get("bank"))
        q["pitch"] = 0.0 if pitch is None else pitch
        q["bank"] = 0.0 if bank is None else bank
        out.append(q)
    return out

def _catmull_rom(p0, p1, p2, p3, f):
    """Uniform Catmull-Rom through p1 -> p2. Passes through every point."""
    f2 = f * f
    f3 = f2 * f
    return 0.5 * ((2.0 * p1)
                  + (-p0 + p2) * f
                  + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * f2
                  + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * f3)


def _catmull_rom_t(p0, p1, p2, p3, t0, t1, t2, t3, t):
    """Catmull-Rom parameterized by TIME, through p1 -> p2.

    The uniform form above assumes evenly spaced samples. They are not: a
    recorded clip runs 0.0998 to 0.1129 s between samples, a 13% spread.
    With uniform tangents the slope arriving at a knot does not match the
    slope leaving it, so speed breaks at every sample - measured at twice
    the frame-to-frame change on knots as between them. That is a 10 Hz
    judder no update rate can smooth out, which is why 90 Hz and 180 Hz
    looked the same.

    Scaling the tangents by the real spans makes it C1 in time: velocity is
    continuous across knots whatever the spacing.
    """
    h = t2 - t1
    if h <= 1e-9:
        return p2
    d02 = t2 - t0
    d13 = t3 - t1
    # One-sided slope at the ends, where a neighbor is the segment itself
    # and the centered difference is undefined.
    m1 = (p2 - p0) / d02 if d02 > 1e-9 else (p2 - p1) / h
    m2 = (p3 - p1) / d13 if d13 > 1e-9 else (p2 - p1) / h
    u = (t - t1) / h
    u2 = u * u
    u3 = u2 * u
    return ((2.0 * u3 - 3.0 * u2 + 1.0) * p1
            + (u3 - 2.0 * u2 + u) * h * m1
            + (-2.0 * u3 + 3.0 * u2) * p2
            + (u3 - u2) * h * m2)


def _unwrap_near(ref, ang):
    """Bring an angle onto the same branch as ref, so a spline can cross 360."""
    return ref + ((float(ang) - ref + 180.0) % 360.0 - 180.0)


def _even_spans(ta, tb, tp, tn):
    """Only spline where the samples either side are evenly spaced.

    An uneven neighbor makes a uniform Catmull-Rom overshoot, which would put
    the ghost somewhere it never was.
    """
    span = tb - ta
    if span <= 1e-6:
        return False
    for other in (ta - tp, tn - tb):
        if other <= 1e-6 or not (0.4 <= other / span <= 2.5):
            return False
    return True


def clip_timestamps(points):
    """The `t` of every point, for bisecting.

    Built here, beside the only list it may describe, because the index and
    the list must not drift apart. A replay holds one immutable list for its
    whole run - normalize_clip_points builds it once and nothing appends to
    it - which is the property that makes precomputing this safe. If a caller
    ever starts mutating a clip's points mid-playback, this has to be rebuilt
    with it or the ghost is placed from a stale index.
    """
    return [p.get("t") or 0.0 for p in points]


def interpolate_pose(points, t, ts=None):
    """Pose at time t. Pass ts from clip_timestamps() to skip the scan.

    Without ts this scanned from the first point on every call, so lookup got
    steadily more expensive as playback advanced: 9.7 us a tenth of the way
    into a clip, 73 us at 95%. The bisect is 3.9 us flat, output identical
    over 2001 probes across a real clip. ts stays optional so every other
    caller and every test keeps working unchanged.
    """
    if not points:
        return None
    if ts is None:
        ts = clip_timestamps(points)
    t0 = ts[0]
    t1 = ts[-1]
    if t <= t0:
        return dict(points[0])
    if t >= t1:
        return dict(points[-1])
    # The scan took the first i where t <= ts[i], starting at i = 1.
    for i in (max(1, bisect.bisect_left(ts, t)),):
        a = points[i - 1]
        b = points[i]
        ta = ts[i - 1]
        tb = ts[i]
        span = tb - ta
        f = 0.0 if span <= 1e-6 else max(0.0, min(1.0, (t - ta) / span))
        out = dict(b)

        # Linear interpolation makes velocity piecewise-constant, so with
        # samples ~0.35 s apart the ghost changes direction at every segment
        # boundary - that is the stutter. A spline through the neighboring
        # samples keeps the motion continuous.
        p = points[i - 2] if i >= 2 else a
        n = points[i + 1] if (i + 1) < len(points) else b
        # Bound rather than inlined: the spline is parameterized by these
        # now, not just gated on them.
        tp = ts[i - 2] if i >= 2 else ta
        tn = ts[i + 1] if (i + 1) < len(points) else tb
        smooth = _even_spans(ta, tb, tp, tn)

        for key in ("lat", "lon", "alt", "pitch", "bank", "gs", "vs"):
            va, vb = a.get(key), b.get(key)
            vp, vn = p.get(key), n.get(key)
            if smooth and None not in (va, vb, vp, vn):
                try:
                    out[key] = _catmull_rom_t(
                        float(vp), float(va), float(vb), float(vn),
                        tp, ta, tb, tn, t)
                    continue
                except Exception:
                    pass
            out[key] = interp_num(va, vb, f)

        ha, hb = a.get("heading"), b.get("heading")
        hp, hn = p.get("heading"), n.get("heading")
        if smooth and None not in (ha, hb, hp, hn):
            try:
                h1 = wrap_deg(ha)
                h0 = _unwrap_near(h1, hp)
                h2 = _unwrap_near(h1, hb)
                h3 = _unwrap_near(h2, hn)
                out["heading"] = wrap_deg(
                    _catmull_rom_t(h0, h1, h2, h3, tp, ta, tb, tn, t))
            except Exception:
                out["heading"] = interp_heading_deg(ha, hb, f)
        else:
            out["heading"] = interp_heading_deg(ha, hb, f)
        out["t"] = t
        out["on_ground"] = a.get("on_ground") if f < 0.5 else b.get("on_ground")
        return out
    return dict(points[-1])

def haversine_nm(lat1, lon1, lat2, lon2):
    r_nm = 3440.065
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2.0) ** 2
    return 2.0 * r_nm * math.asin(min(1.0, math.sqrt(a)))

def new_flight_id():
    return now_utc().strftime("flt-%Y%m%dT%H%M%SZ")

def acquire_lock():
    fh = open(LOCK_FILE, "a+")
    try:
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    return fh

def write_pid():
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()) + "\n")

def set_below_normal_priority():
    try:
        import ctypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.SetPriorityClass.restype = ctypes.c_int
        handle = k32.GetCurrentProcess()
        if not k32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS):
            log("priority Below Normal failed err=%s" % ctypes.get_last_error())
        else:
            log("priority Below Normal")
    except Exception as e:
        log("priority set failed %s" % repr(e))

def connect_sim():
    holder = {"sm": None, "err": None}

    def do_connect():
        try:
            from SimConnect import SimConnect
            holder["sm"] = SimConnect(auto_connect=True)
        except Exception as e:
            holder["err"] = repr(e)

    th = threading.Thread(target=do_connect, daemon=True)
    th.start()
    th.join(CONNECT_TIMEOUT)
    if holder["err"]:
        return None, holder["err"]
    if holder["sm"] is None:
        return None, "connect timed out after %ss" % int(CONNECT_TIMEOUT)
    return holder["sm"], None

def sample(aq):
    aircraft = decode_val(aq.get("TITLE"))
    lat = as_float(aq.get("PLANE_LATITUDE"))
    lon = as_float(aq.get("PLANE_LONGITUDE"))
    alt = as_float(aq.get("PLANE_ALTITUDE"))
    vs = as_float(aq.get("VERTICAL_SPEED"))
    gs = as_float(aq.get("GROUND_VELOCITY"))
    heading = heading_to_deg(aq.get("PLANE_HEADING_DEGREES_TRUE"))
    # PLANE_HEADING_DEGREES_MAGNETIC was fetched and never read by anything -
    # not the jsonl schema, not the clip schema. Each SimConnect get() costs a
    # fixed ~50 ms of polling, so dropping it buys back a real slice of the
    # sample loop.
    airspeed = as_float(aq.get("AIRSPEED_INDICATED"))
    on_ground = as_bool(aq.get("SIM_ON_GROUND"))
    td = as_float(aq.get("PLANE_TOUCHDOWN_NORMAL_VELOCITY"))
    pitch = bank = slew = None
    try:
        pitch = rad_to_deg(aq.get("PLANE_PITCH_DEGREES"))
    except Exception:
        pitch = None
    try:
        bank = rad_to_deg(aq.get("PLANE_BANK_DEGREES"))
    except Exception:
        bank = None
    try:
        slew = as_bool(aq.get("IS_SLEW_ACTIVE"))
    except Exception:
        slew = None
    t = time.time()
    return {
        "t": t,
        "ts": now_iso(),
        "aircraft": aircraft if aircraft else None,
        "livery": None,
        "lat": lat,
        "lon": lon,
        "alt": alt,
        "vs": vs,
        "gs": gs,
        "heading": heading,
        "airspeed": airspeed,
        "on_ground": on_ground,
        "touchdown_fps": td,
        "pitch": pitch,
        "bank": bank,
        "slew": slew,
    }

def is_valid(s):
    if not s:
        return False
    if not s.get("aircraft"):
        return False
    lat = s.get("lat")
    lon = s.get("lon")
    if not finite(lat) or not finite(lon):
        return False
    if float(lat) == 0.0 and float(lon) == 0.0:
        return False
    if is_spawn_junk(lat, lon):
        return False
    return True

def write_current(state, flight_id, started_at, s, extra=None, sortie_id=None,
                  lift_at=None):
    doc = {
        "state": state,
        "flight_id": flight_id,
        "started_at": started_at,
        "aircraft": s.get("aircraft") if s else None,
        "livery": s.get("livery") if s else None,
        # This doc is a whitelist, so a new field is invisible here until
        # it is named - which is how gear went missing once already.
        "category": s.get("category") if s else None,
        "vs0": s.get("vs0") if s else None,
        "lat": s.get("lat") if s else None,
        "lon": s.get("lon") if s else None,
        "alt": s.get("alt") if s else None,
        "on_ground": s.get("on_ground") if s else None,
        "gear": s.get("gear") if s else None,
        "flaps": s.get("flaps") if s else None,
        # Stable across reconnects; flight_id is the fragment.
        "sortie_id": sortie_id or flight_id,
        "lift_at": lift_at,
        "last_update": now_iso(),
        "airspeed": s.get("airspeed") if s else None,
        "heading": s.get("heading") if s else None,
        "vs": s.get("vs") if s else None,
        "gs": s.get("gs") if s else None,
    }
    if extra:
        doc.update(extra)
    with RUNTIME_LOCK:
        RUNTIME["current_doc"] = doc
    try:
        atomic_write(CURRENT, doc, fsync=False)   # rewritten again in a second
    except PermissionError as e:
        log("write_current PermissionError %s" % repr(e))
    except Exception as e:
        log("write_current failed %s" % repr(e))

def write_event(etype, flight_id, aircraft, extra=None, sortie_id=None):
    doc = {
        "type": etype,
        "flight_id": flight_id,
        "sortie_id": sortie_id or flight_id,
        "at": now_iso(),
        "aircraft": aircraft,
    }
    if extra:
        doc.update(extra)
    with RUNTIME_LOCK:
        RUNTIME["last_event_doc"] = doc
    try:
        atomic_write(LAST_EVENT, doc)
    except PermissionError as e:
        log("write_event PermissionError %s" % repr(e))
    except Exception as e:
        log("write_event failed %s" % repr(e))
    return doc

def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def clip_point(s):
    return {
        "t": s.get("t"),
        "lat": s.get("lat"),
        "lon": s.get("lon"),
        "alt": s.get("alt"),
        "heading": s.get("heading"),
        "pitch": s.get("pitch"),
        "bank": s.get("bank"),
        "gs": s.get("gs"),
        "vs": s.get("vs"),
        "on_ground": s.get("on_ground"),
        "gear": s.get("gear"),
        "flaps": s.get("flaps"),
        "spoilers": s.get("spoilers"),
    }

def list_clip_summaries():
    out = []
    try:
        names = sorted(os.listdir(CLIPS_DIR))
    except OSError:
        return out
    seen = set()
    for name in names:
        cid = clipfile.id_from_path(name)
        if cid == name or cid in seen or not CLIP_ID_RE.match(cid):
            continue
        path = clipfile.clip_path(CLIPS_DIR, cid)
        if not path:
            continue
        # Every field below is a header field, and the header is immutable
        # once written, so this reads the first line and stops rather than
        # parsing every point of every clip to build a list.
        doc = clipfile.read_header(path)
        if not doc:
            continue
        seen.add(cid)
        out.append({
            "id": doc.get("id") or cid,
            "kind": doc.get("kind"),
            "flight_id": doc.get("flight_id"),
            "leg": doc.get("leg"),
            "aircraft": doc.get("aircraft"),
            "t0": doc.get("t0"),
            "path": path,
        })
    return out

def clip_is_recording(path):
    """Whether this process is recording that clip right now.

    Only the watcher can answer this. Recording claims live in memory, so a
    command-line build or a restored archive sees none of them and must say
    "completion not recorded" rather than "interrupted".
    """
    try:
        with RUNTIME_LOCK:
            open_ids = list(RUNTIME.get("open_clip_ids") or [])
    except Exception:
        return False
    return clipfile.id_from_path(path) in open_ids


def load_clip(clip_id=None, path=None):
    if path:
        path = os.path.abspath(path)
        clips_abs = os.path.abspath(CLIPS_DIR)
        if not path.startswith(clips_abs + os.sep) and path != clips_abs:
            return None
        if not (path.endswith(clipfile.SUFFIX)
                or path.endswith(clipfile.LEGACY_SUFFIX)):
            return None
        doc = clipfile.read_clip(path)
        if doc is not None:
            doc["recording"] = clip_is_recording(path)
        return doc
    if not clip_id or not CLIP_ID_RE.match(clip_id):
        return None
    found = clipfile.clip_path(CLIPS_DIR, clip_id)
    doc = clipfile.read_clip(found)
    if doc is not None:
        # Only the watcher knows what it is recording, so only the watcher may
        # distinguish "still going" from "stopped part way". A build running
        # anywhere else sees none of these claims.
        doc["recording"] = clip_is_recording(found)
    return doc

_notify_cache = {"mtime": None, "doc": None}


def read_notify_cached():
    """notify_state.json is written by the agent; re-read only when it changes."""
    try:
        mt = os.path.getmtime(NOTIFY_STATE)
    except OSError:
        return None
    if _notify_cache["mtime"] != mt:
        _notify_cache["doc"] = read_json(NOTIFY_STATE)
        _notify_cache["mtime"] = mt
    return _notify_cache["doc"]


_version_cache = [None]


# The modules this process holds in memory. A watcher goes on running the
# code it started with, and an edit to any of these does nothing until it is
# restarted - which has bitten twice: once when a stale watcher overwrote a
# new-format logbook.json with the old shape and the UI went blank with no
# explanation, and repeatedly when the running process sat a commit behind
# after a change was shipped. Reported rather than remembered.
_WATCHED_MODULES = ("persistence.py", "watcher.py", "sampler.py", "grading.py",
                    "logbook_build.py", "passenger.py", "flightprefs.py",
                    "mapbake.py", "tiles.py", "settings.py", "trackexport.py")
_STARTED_AT = [None]


def note_code_mtimes():
    """Newest module timestamp at startup. Called once, before serving."""
    _STARTED_AT[0] = _newest_module_mtime()


def _newest_module_mtime():
    newest = 0.0
    for name in _WATCHED_MODULES:
        try:
            newest = max(newest, os.stat(os.path.join(BASE, name)).st_mtime)
        except OSError:
            continue
    return newest


def code_is_stale():
    """True when a module on disk is newer than the one being run.

    Compares timestamps rather than asking git: it is exact about the thing
    that actually matters - this process holds code that no longer exists on
    disk - and it costs a handful of stat calls rather than a subprocess.
    """
    if _STARTED_AT[0] is None:
        return False
    return _newest_module_mtime() > _STARTED_AT[0] + 0.5


def describe_version():
    """APP_VERSION, refined with the git description when one is available.

    Read once: a subprocess per request would be absurd, and the answer cannot
    change while the process is running.
    """
    if _version_cache[0] is not None:
        return _version_cache[0]
    text = APP_VERSION
    try:
        import subprocess
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=BASE, capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        described = (out.stdout or "").strip()
        if out.returncode == 0 and described:
            text = "%s (%s)" % (APP_VERSION, described)
    except Exception:
        pass
    _version_cache[0] = text
    return text


def load_settings_at_startup():
    """Capture the pristine constants, then lay saved overrides on top."""
    try:
        import settings as settings_mod
        import passenger, tiles, mapbake       # so capture_defaults can see them
        import flightprefs
        settings_mod.register("watcher", sys.modules[__name__])
        settings_mod.register("flightprefs", flightprefs)
        settings_mod.register("passenger", passenger)
        settings_mod.register("tiles", tiles)
        settings_mod.register("mapbake", mapbake)
        settings_mod.capture_defaults()
        saved = settings_mod.load()
        settings_mod.apply(on_buffer=resize_live_buffer)
        # RUNTIME is built at import, so its chase block already holds the code
        # defaults by the time settings load. Applying the constants alone left
        # the live camera - and therefore the UI knobs - on 45/15/20 after every
        # restart, however they were saved.
        with RUNTIME_LOCK:
            ch = RUNTIME["replay"]["chase"]
            ch["distance"] = float(CHASE_DISTANCE_DEFAULT)
            ch["height"] = float(CHASE_HEIGHT_DEFAULT)
            ch["orbit"] = float(CHASE_ORBIT_DEFAULT)
        if saved:
            log("settings: %d override(s) applied %s"
                % (len(saved), ", ".join(sorted(saved))))
        else:
            log("settings: none saved, using defaults")
        # passenger_lines.json is hand-edited content, so a broken one falls
        # back to a stub rather than taking the logbook down with it. Say so:
        # silently degraded prose is the kind of thing nobody notices for
        # weeks. py -3 passenger.py says exactly what is wrong with it.
        if getattr(passenger, "LINES_NOTE", None):
            log("passenger: %s" % passenger.LINES_NOTE)
    except Exception as e:
        log("settings load failed, using defaults: %r" % (e,))


@persistence.serialized(lambda: BASE)
def apply_settings(values):
    """Validate, persist and apply. Returns (ok, payload_or_error)."""
    try:
        import settings as settings_mod
    except Exception as e:
        return False, "settings unavailable: %r" % (e,)
    before = settings_mod.effective()
    try:
        settings_mod.update(values)
        after = settings_mod.effective()
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        return False, repr(e)
    # Compare values, not which keys were posted: the UI sends every field on
    # every save, so testing for presence made each save force a full rebuild -
    # and a topo rebuild is several minutes of tile work for nothing.
    # The landing ladder used to be in here, because changing it rewrote every
    # stored grade. It is a constant now, so the only settings that still
    # invalidate the built logbook are the two that change how a map is drawn.
    watched = ("tile_source", "map_style")
    changed = [k for k in watched if before.get(k) != after.get(k)]
    # Clip windows decide a clip's shape at commit, so they are staged rather
    # than applied when a clip is mid-capture; the buffer resize is held with
    # them so the two never disagree.
    window_keys = {"takeoff_before": "TAKEOFF_BEFORE", "takeoff_after": "TAKEOFF_AFTER",
                   "landing_before": "LANDING_BEFORE", "landing_after": "LANDING_AFTER"}
    windows_changed = {const: after[key] for key, const in window_keys.items()
                       if key in after and before.get(key) != after.get(key)}
    if windows_changed and clip_capture_in_progress():
        keep = {const: globals()[const] for const in window_keys.values()}
        settings_mod.apply(on_buffer=resize_live_buffer)
        for const, value in keep.items():          # put the old values back
            globals()[const] = value
        hold_clip_windows({const: after[key] for key, const in window_keys.items()
                           if key in after})
        log("clip window change held: a clip is still being captured")
    else:
        settings_mod.apply(on_buffer=resize_live_buffer)

    # The camera defaults are read once when RUNTIME is built, so changing the
    # constant alone left the live chase state - and therefore the UI knobs -
    # on the old numbers until a restart. Push them through, but never while a
    # replay is using them.
    cam_keys = ("chase_distance", "chase_height", "chase_orbit")
    if any(before.get(k) != after.get(k) for k in cam_keys):
        with RUNTIME_LOCK:
            if not RUNTIME["replay"].get("active"):
                ch = RUNTIME["replay"]["chase"]
                ch["distance"] = float(CHASE_DISTANCE_DEFAULT)
                ch["height"] = float(CHASE_HEIGHT_DEFAULT)
                ch["orbit"] = float(CHASE_ORBIT_DEFAULT)
                moved = True
            else:
                moved = False
        log("camera defaults %s" % ("applied" if moved
                                    else "saved; a replay is running, they apply next time"))

    # Grades and map look are baked into the logbook, so a change has to be
    # rebuilt in rather than waiting for the next flight.
    if changed:
        log("settings changed %s; rebuilding the logbook" % ", ".join(changed))
        schedule_logbook_rebuild(delay=1.0, reason="settings", force=True)
    out = settings_mod.describe()
    out["rebuild_scheduled"] = bool(changed)
    return True, out


def _live_prefs(current_doc):
    """Switches for the flight in progress, or None when nothing is flying."""
    try:
        import flightprefs
        doc = current_doc if isinstance(current_doc, dict) else read_json(CURRENT)
        sid = (doc or {}).get("sortie_id") or (doc or {}).get("flight_id")
        if not sid:
            return None
        out = flightprefs.for_sortie(sid)
        out["sortie_id"] = sid
        return out
    except Exception:
        return None


def set_flight_prefs(sortie_id, rating=None, passenger=None):
    """Set one flight's switches and rebuild if the logbook already has it."""
    try:
        import flightprefs
        got = flightprefs.set_for_sortie(sortie_id, rating=rating,
                                         passenger=passenger, at=now_iso())
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        log("flight_prefs write failed %s" % repr(e))
        return {"ok": False, "error": "could not save"}
    log("flight prefs %s rating=%s passenger=%s"
        % (sortie_id, got["rating"], got["passenger"]))
    # Index only: a switch changes what is written about a flight, never a
    # map, so there is nothing to re-render. That also lets this run while a
    # flight is in progress, where a bake would be deferred and the logbook
    # would sit stale until the aircraft was parked.
    res = rebuild_logbook_now(bake_maps=False, force=False)
    out = {"ok": True, "sortie_id": sortie_id, "prefs": got}
    if isinstance(res, dict):
        out["rebuilt"] = bool(res.get("ok"))
    return out


def build_export(sortie_id, leg_seq=None, fmt="kml", mode=None):
    """One leg or a whole sortie as KML or GPX. Returns (body, filename).

    Built from the raw session track rather than the logbook's stored
    polyline: that one is decimated to a few dozen lat/lon pairs with no
    altitude, which is exactly what this needs and does not have.
    """
    import trackexport

    detail_path = os.path.join(SESSIONS, "detail", "%s.json" % sortie_id)
    doc = read_json(detail_path)
    if not isinstance(doc, dict):
        return None, "no such flight"
    flight_ids = doc.get("flight_ids") or [sortie_id]

    t0 = t1 = None
    label = doc.get("aircraft") or "Flight"
    start_name, end_name = "Start", "End"
    if leg_seq is not None:
        leg = next((l for l in (doc.get("legs") or [])
                    if str(l.get("seq")) == str(leg_seq)), None)
        if leg is None:
            return None, "no such leg"
        # The leg is takeoff to landing. A leg missing either end - recovered
        # from the track rather than from events - exports what it has.
        t0 = (leg.get("takeoff") or {}).get("at_utc")
        t1 = (leg.get("landing") or {}).get("at_utc")
        route = leg.get("route") or {}
        label = "%s leg %s: %s to %s" % (
            doc.get("aircraft") or "Flight", leg.get("seq"),
            route.get("from") or "?", route.get("to") or "?")
        start_name, end_name = "Takeoff", "Landing"
        stem = "%s-leg%s" % (sortie_id, leg.get("seq"))
    else:
        stem = sortie_id

    points = trackexport.read_track(SESSIONS, flight_ids, t0, t1)
    if len(points) < 2:
        return None, "no track recorded for that"
    points = trackexport.decimate(points)

    if fmt == "gpx":
        body = trackexport.gpx(points, label)
    else:
        want = mode or trackexport.ALTITUDE_MODE
        body = trackexport.kml(points, label, mode=want,
                               start=start_name, end=end_name,
                               description="%s - %s points, exported by AfterFlight"
                                           % (label, len(points)))
    if not body:
        return None, "track too short to export"
    return body, "%s.%s" % (trackexport.safe_name(stem), fmt)


def state_payload():
    with RUNTIME_LOCK:
        recent = list(RUNTIME["recent_events"])
        open_ids = list(RUNTIME["open_clip_ids"])
        ch = RUNTIME["replay"]["chase"]
        replay = {
            "active": bool(RUNTIME["replay"]["active"]),
            "holding": bool(RUNTIME["replay"].get("holding")),
            "paused": bool(RUNTIME["replay"].get("paused")),
            "clip_id": RUNTIME["replay"]["clip_id"],
            "object_id": RUNTIME["replay"]["object_id"],
            "error": RUNTIME["replay"]["error"],
            # Which livery is actually on screen, and which one was asked for -
            # they differ when the recorded title is not installed and the
            # spawn falls back, which is otherwise invisible.
            "aircraft": RUNTIME["replay"].get("aircraft"),
            "aircraft_requested": RUNTIME["replay"].get("aircraft_requested"),
            # Where playback has reached, for the UI's progress bar.
            "elapsed_s": RUNTIME["replay"].get("elapsed_s"),
            "duration_s": RUNTIME["replay"].get("duration_s"),
            "livery": RUNTIME["replay"].get("livery"),
            "livery_source": RUNTIME["replay"].get("livery_source"),
            # Mirror the full chase state. This used to build its own subset
            # and omit the lock, so the checkbox could never reflect a watcher
            # that was still following - it looked locked with the box clear.
            "chase": {
                "distance": float(ch["distance"]),
                "height": float(ch["height"]),
                "orbit": float(ch["orbit"]),
                "camera_acquired": bool(ch["camera_acquired"]),
                "mode": ch.get("mode") or CHASE_MODE_DEFAULT,
                "locked": (ch.get("mode") == CHASE_MODE_FOLLOW),
                "lock_bearing": ch.get("lock_bearing") or "aircraft",
            },
        }
        connected = bool(RUNTIME["connected"])
        leg = RUNTIME["leg"]
        flight_id = RUNTIME["flight_id"]
        current_doc = RUNTIME.get("current_doc")
        last_event_doc = RUNTIME.get("last_event_doc")
    payload = {
        "ok": True,
        "pid": os.getpid(),
        "served_at": now_iso(),
        "http": "%s:%s" % (HTTP_HOST, HTTP_PORT),
        "version": describe_version(),
        "current": current_doc if current_doc is not None else read_json(CURRENT),
        "last_event": last_event_doc if last_event_doc is not None else read_json(LAST_EVENT),
        "notify": read_notify_cached(),
        "clips": {
            "recent_events": recent,
            "open_clip_ids": open_ids,
            "leg": leg,
            "flight_id": flight_id,
        },
        "replay": replay,
        # What the flight being recorded right now is set to. The switches are
        # keyed by sortie, and a sortie exists the moment recording starts, so
        # a choice made in the air is already the one the builder will read.
        "flight_prefs": _live_prefs(current_doc),
        "connected": connected,
        # Seconds since the detect loop last turned. The tray restarts the
        # watcher on this rather than on whether the port answers.
        "heartbeat_age_s": round(heartbeat_age(), 1),
        # This process is running code that has since been edited. Not an
        # error - it is normal for a few seconds after a change - but a
        # watcher left like this writes the old behavior indefinitely.
        "code_stale": code_is_stale(),
        "sampler": {"mode": SAMPLER_MODE, "sample_hz": RUNTIME.get("sample_hz"),
                    "camera_state": RUNTIME.get("camera_state")},
        "logbook": logbook_build_status(),
    }
    return payload

# ---------------------------------------------------------------- logbook

# The app owns the logbook and the maps now. The
# watcher is the only writer; the tray UI and the browser are both readers.
LOGBOOK_JSON = os.path.join(BASE, "logbook.json")
# Which flights and legs are hidden. Removal is a hide, not a delete: the
# .jsonl tracks, the clips and events.jsonl are the ground truth the logbook is
# derived from, so hiding is reversible and losing them would not be.
EXCLUDED_JSON = os.path.join(BASE, "excluded.json")
LOGBOOK_HTML = os.path.join(BASE, "logbook.html")
# Coalesce rebuild requests: a sortie fires several of these in a row.
LOGBOOK_DEBOUNCE_SEC = 20.0
# A rebuild costs ~2.4 s of CPU at 81% of a core plus megabytes of IO. It has
# no reason to run while you are flying, so a request made mid-flight is held
# and serviced at flight end, or once the aircraft has been parked a while.
_logbook_pending = [False]
_logbook_reprocess_pending = [False]
# Why the next build is running, for the UI's banner. A list so the timer
# thread and the HTTP threads share one cell without a module-level global.
_logbook_reason = [""]
# Whether the build now finishing succeeded, read by the finally clause.
_logbook_ok = [None]
_logbook_lock = threading.Lock()
_logbook_timer = None
_logbook_building = False

# What the UI needs to say 'refreshing' honestly, and to know when to reload.
# seq increments on every completed build, so a page that never saw the
# building phase can still tell that its data is stale and reload once.
_logbook_build = {
    "building": False,
    "since": None,          # epoch seconds the current build started
    "reason": "",
    "maps": True,           # whether this build is baking maps (the slow half)
    "seq": 0,
    "last": None,           # {ok, took_ms, at, totals} of the last finished build
}


def logbook_build_status():
    """A snapshot of rebuild state for /state. Cheap; called on every poll."""
    with _logbook_lock:
        d = dict(_logbook_build)
    if d["building"] and d["since"]:
        d["elapsed_s"] = round(time.time() - d["since"], 1)
    else:
        d["elapsed_s"] = None
    d.pop("since", None)
    return d


def flight_is_active():
    with RUNTIME_LOCK:
        return bool(RUNTIME["connected"] and RUNTIME["flight_id"])


def set_hidden(scope, sortie_id, key=None, hide=True):
    """Hide or restore a flight or one of its legs. Source data is untouched."""
    if not sortie_id:
        return {"ok": False, "error": "no flight given"}
    if scope not in ("sortie", "leg"):
        return {"ok": False, "error": "scope must be sortie or leg"}
    if scope == "leg" and not key:
        return {"ok": False, "error": "no leg given"}
    with persistence.document_lock(EXCLUDED_JSON):
        import logbook_build
        if logbook_build.pending_purge(scope, sortie_id, key):
            return {"ok": False, "error": "Deletion has started; retry Delete permanently."}
        doc = read_json(EXCLUDED_JSON)
        if not isinstance(doc, dict):
            doc = {}
        doc.setdefault("schema", 1)
        doc.setdefault("sorties", {})
        doc.setdefault("legs", {})
        if not isinstance(doc["sorties"], dict):
            doc["sorties"] = {}
        if not isinstance(doc["legs"], dict):
            doc["legs"] = {}

        if scope == "sortie":
            if hide:
                doc["sorties"][sortie_id] = {"at": now_iso()}
            else:
                doc["sorties"].pop(sortie_id, None)
        else:
            legs = doc["legs"].setdefault(sortie_id, {})
            if hide:
                legs[key] = {"at": now_iso()}
            else:
                legs.pop(key, None)
                if not legs:
                    doc["legs"].pop(sortie_id, None)
        try:
            atomic_write(EXCLUDED_JSON, doc)
        except Exception as e:
            log("excluded.json write failed %s" % repr(e))
            return {"ok": False, "error": "could not save"}
    log("%s %s %s%s" % ("hid" if hide else "restored", scope, sortie_id,
                        (" " + key) if key else ""))
    res = rebuild_logbook_now(force=True)
    return {"ok": True, "scope": scope, "sortie_id": sortie_id, "key": key,
            "hidden": hide, "rebuild": res}


def find_hidden_entry(scope, sortie_id, key=None):
    """The hidden record for this item, from the current index.

    Purging is only offered for something already hidden, so the index is the
    authority on what exists to delete and carries the file details.
    """
    import logbook_build
    pending = logbook_build.pending_purge(scope, sortie_id, key)
    if pending:
        return pending
    doc = read_json(LOGBOOK_JSON) or {}
    for h in doc.get("hidden") or []:
        if h.get("scope") != scope or h.get("sortie_id") != sortie_id:
            continue
        if scope == "leg" and h.get("leg_key") != key:
            continue
        return h
    return None


@persistence.serialized(lambda: BASE)
def purge_hidden(scope, sortie_id, key=None, dry_run=True):
    """Permanently delete a hidden flight or leg. Irreversible."""
    entry = find_hidden_entry(scope, sortie_id, key)
    if entry is None:
        # Deliberately strict: a thing must be removed from the logbook before
        # it can be destroyed, so this can never fire straight off the list.
        return {"ok": False, "error": "not removed",
                "detail": "Remove it from the logbook first, then delete it."}
    try:
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        import logbook_build
        plan = logbook_build.plan_purge(entry)
        if plan is None:
            return {"ok": False, "error": "nothing to delete"}
        if dry_run:
            return {"ok": True, "dry_run": True, "plan": plan, "entry": {
                "aircraft": entry.get("aircraft"), "date": entry.get("date"),
                "distance_nm": entry.get("distance_nm"), "grade": entry.get("grade")}}
        res = logbook_build.purge(entry, log=log)
        # Rebuild either way. A partial delete still moved events, tracks and
        # the caches, so the logbook on disk is stale whether or not every
        # file went - and the flight stays hidden, which is what the retained
        # exclusion says. Returning early here left the page describing a
        # world that no longer existed.
        rebuild = rebuild_logbook_now(force=True, reprocess=True)
        out = dict(res)
        out["rebuild"] = rebuild
        return out
    except Exception as e:
        log("purge failed %s" % repr(e))
        return {"ok": False, "error": repr(e)}


def maintenance_unsafe():
    """A connected aircraft must be stationary on the ground for heavy work."""
    with RUNTIME_LOCK:
        if not RUNTIME["connected"]:
            return False
        cur = RUNTIME.get("current_doc") or {}
        return not (cur.get("on_ground") and (as_float(cur.get("gs")) or 0.0) < 1.0)


def rebuild_logbook_now(bake_maps=True, force=False, reprocess=False):
    """Rebuild logbook.json (and the map PNGs). Safe to call from any thread.

    Deferred while a flight is active unless forced; the request is remembered
    and serviced later rather than dropped.

    force and reprocess are two different questions and used to be one word.
    force is scheduling: run even though a flight is in progress. reprocess is
    correctness: ignore the sortie cache and derive every sortie from source.
    The Rebuild button passed force=True and nothing else, so it bypassed the
    deferral and then reused the very cache the user pressed it to escape -
    while AGENTS.md described it as a full reprocess. Callers that only need
    to jump the queue must not silently pay for a full rebake, so this stays a
    second flag rather than being folded into the first.

    Baking maps is the expensive half - Pillow, tiles, a supersampled canvas -
    and that is what must not happen mid-sortie. Rebuilding the index alone is
    cheap enough to run right after a landing, so the leg you just flew shows
    up without waiting until you park. The map bake is still remembered and
    serviced later.
    """
    global _logbook_building
    heavy = bool(bake_maps or reprocess)
    index_only_mid_flight = flight_is_active() and not heavy
    with _logbook_lock:
        if (heavy and maintenance_unsafe()) or (bake_maps and not force and flight_is_active()):
            _logbook_pending[0] = True
            _logbook_reprocess_pending[0] |= bool(reprocess)
            return {"ok": False, "deferred": True, "error": "flight in progress"}
        if _logbook_building:
            _logbook_pending[0] = True
            _logbook_reprocess_pending[0] |= bool(reprocess)
            return {"ok": False, "deferred": True, "error": "rebuild already running"}
        reprocess = bool(reprocess or _logbook_reprocess_pending[0])
        if reprocess and maintenance_unsafe():
            _logbook_pending[0] = True
            return {"ok": False, "deferred": True, "error": "flight in progress"}
        _logbook_reprocess_pending[0] = False
        _logbook_pending[0] = index_only_mid_flight
        _logbook_building = True
        _logbook_build.update(building=True, since=time.time(),
                              reason=_logbook_reason[0] or '', maps=bool(bake_maps))
    t_started = time.time()
    try:
        # Do not rely on sys.path[0]: the watcher can be started from a service,
        # a shortcut, or an embedded loader, none of which put BASE on the path.
        if BASE not in sys.path:
            sys.path.insert(0, BASE)
        import logbook_build
        doc = logbook_build.build(bake_maps=bake_maps, log=log,
                                  allow_network=lambda: not maintenance_unsafe(),
                                  force=bool(reprocess),
                                  should_abort=maintenance_unsafe if (bake_maps or reprocess) else None)
        # Index-only runs happen mid-flight now, so their cost is worth
        # knowing rather than assuming. If this grows into the hundreds of ms
        # it belongs off this path entirely.
        if index_only_mid_flight:
            log("logbook: index-only rebuild mid-flight took %.0f ms"
                % ((time.time() - t_started) * 1000.0))
        _logbook_ok[0] = True
        return {"ok": True, "totals": doc.get("totals"), "updated_at": doc.get("updated_at")}
    except persistence.MaintenanceDeferred:
        with _logbook_lock:
            _logbook_pending[0] = True
            _logbook_reprocess_pending[0] |= bool(reprocess)
        _logbook_ok[0] = False
        return {"ok": False, "deferred": True, "error": "flight started during maintenance"}
    except Exception as e:
        _logbook_ok[0] = False
        log("logbook rebuild failed %s" % repr(e))
        return {"ok": False, "error": repr(e)}
    finally:
        with _logbook_lock:
            _logbook_building = False
            _logbook_build["building"] = False
            _logbook_build["since"] = None
            _logbook_build["seq"] += 1
            _logbook_build["last"] = {
                "at": now_iso(),
                "took_ms": int((time.time() - t_started) * 1000.0),
                "ok": _logbook_ok[0],
            }
            _logbook_ok[0] = None
            _logbook_reason[0] = ""


def run_pending_logbook_rebuild(why):
    """Service a rebuild that was held back while flying."""
    if not _logbook_pending[0]:
        return
    log("logbook: running deferred rebuild (%s)" % why)
    t = threading.Thread(target=lambda: rebuild_logbook_now(force=True), daemon=True)
    t.start()


def schedule_logbook_rebuild(delay=LOGBOOK_DEBOUNCE_SEC, reason="", force=False,
                            bake_maps=True):
    """Rebuild after the sortie settles, never in the middle of the action.

    Maps are baked here, so this deliberately runs after the clip windows have
    closed rather than during the flight.
    """
    global _logbook_timer
    with _logbook_lock:
        if _logbook_timer is not None:
            _logbook_timer.cancel()
        # Carried into the build so the UI can say why it is refreshing.
        _logbook_reason[0] = reason or _logbook_reason[0]
        t = threading.Timer(
            delay, lambda: rebuild_logbook_now(bake_maps=bake_maps, force=force))
        t.daemon = True
        _logbook_timer = t
        t.start()
    if reason:
        log("logbook rebuild scheduled in %.0fs (%s)" % (delay, reason))


# ---------------------------------------------------------------- static

# Served from BASE so the browser and the tray read one tree.
STATIC_ROUTES = {
    "/": "logbook.html",
    "/logbook.html": "logbook.html",
    "/logbook.json": "logbook.json",
    "/logbook.js": "logbook.js",
    "/replay.js": "replay.js",
    # The EFB package's icon is the canonical artwork; the browser UI
    # points at that same file rather than keeping a second copy that
    # could drift away from it.
    "/app-icon.svg": "efb-pkg/html_ui/efb_ui/efb_apps/AfterFlight/Assets/app-icon.svg",
}
STATIC_DIRS = ("sessions",)
STATIC_EXT = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
}


def resolve_static(url_path):
    """Map a URL path to a file under BASE, or None.

    Refuses anything outside BASE, any extension not on the allowlist, and the
    working files (.py, .log, .pid, .lock, backups) that share the directory.
    """
    if url_path in STATIC_ROUTES:
        rel = STATIC_ROUTES[url_path]
    else:
        clean = posixpath.normpath(url_path.lstrip("/"))
        if not clean or clean.startswith("..") or clean in (".", "/"):
            return None
        top = clean.split("/", 1)[0]
        if top not in STATIC_DIRS:
            return None
        rel = clean
    if any(part.startswith(".") for part in rel.split("/")):
        return None
    ext = os.path.splitext(rel)[1].lower()
    if ext not in STATIC_EXT:
        return None
    full = os.path.realpath(os.path.join(BASE, rel.replace("/", os.sep)))
    root = os.path.realpath(BASE)
    if full != root and not full.startswith(root + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    return full, STATIC_EXT[ext]


def serve_static(handler, url_path):
    """Return True if the request was served from disk."""
    found = resolve_static(url_path)
    if not found:
        return False
    full, ctype = found
    try:
        with open(full, "rb") as f:
            body = f.read()
    except Exception as e:
        log("static read failed %s %s" % (url_path, repr(e)))
        return False
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler._cors()
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except Exception:
        pass
    return True


def json_response(handler, code, obj):
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler._cors()
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

class StateHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _read_json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        if n > 1000000:
            return None
        raw = self.rfile.read(n)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])
        # /state keeps its full shape: the EFB tablet polls it.
        if path == "/state":
            json_response(self, 200, state_payload())
            return
        if path == "/current":
            with RUNTIME_LOCK:
                doc = RUNTIME.get("current_doc")
            json_response(self, 200, {"ok": True, "served_at": now_iso(),
                                      "current": doc if doc is not None else read_json(CURRENT)})
            return
        if path == "/last_event":
            with RUNTIME_LOCK:
                doc = RUNTIME.get("last_event_doc")
            json_response(self, 200, {"ok": True, "served_at": now_iso(),
                                      "last_event": doc if doc is not None else read_json(LAST_EVENT)})
            return
        if path == "/export":
            q = parse_qs(urlparse(self.path).query)
            def one(k, d=None):
                v = q.get(k)
                return v[0] if v else d
            fmt = (one("fmt") or "kml").lower()
            if fmt not in ("kml", "gpx"):
                json_response(self, 400, {"ok": False, "error": "fmt must be kml or gpx"})
                return
            try:
                body, name = build_export(
                    one("sortie"), one("leg"), fmt, one("mode"))
            except Exception as e:
                log("export failed %r" % (e,))
                json_response(self, 500, {"ok": False, "error": "export failed"})
                return
            if body is None:
                json_response(self, 404, {"ok": False, "error": name})
                return
            raw = body.encode("utf-8")
            self.send_response(200)
            # KML is XML; the vnd type is what makes Windows hand it to
            # Google Earth rather than opening it as text in the browser.
            self.send_header("Content-Type",
                             "application/vnd.google-earth.kml+xml"
                             if fmt == "kml" else "application/gpx+xml")
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % name)
            self.send_header("Content-Length", str(len(raw)))
            self._cors()
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/clips":
            json_response(self, 200, {"ok": True, "clips": list_clip_summaries()})
            return
        if path == "/grading":
            # Generated from the grading profile, never written out by
            # hand, so the explanation cannot drift from the thresholds
            # actually in use.
            try:
                import grading as grading_mod
                doc = grading_mod.describe()
                doc["ok"] = True
                json_response(self, 200, doc)
            except Exception as e:
                json_response(self, 500, {"ok": False, "error": repr(e)})
            return
        if path == "/settings":
            try:
                import settings as settings_mod
                doc = settings_mod.describe()
                doc["ok"] = True
                json_response(self, 200, doc)
            except Exception as e:
                json_response(self, 200, {"ok": False, "error": repr(e)})
            return
        if path.startswith("/clips/"):
            cid = path[len("/clips/"):].strip("/")
            doc = load_clip(clip_id=cid)
            if doc is None:
                json_response(self, 404, {"ok": False, "error": "clip not found"})
                return
            json_response(self, 200, doc)
            return
        # GET / serves logbook.html; the browser and the tray share one tree.
        if serve_static(self, path):
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_POST(self):
        path = unquote(self.path.split("?", 1)[0])
        if path == "/replay":
            body = self._read_json_body()
            if body is None:
                json_response(self, 200, {"ok": False, "error": "invalid json"})
                return
            json_response(self, 200, start_replay(body))
            return
        if path == "/settings":
            body = self._read_json_body()
            if body is None:
                json_response(self, 200, {"ok": False, "error": "invalid json"})
                return
            ok, out = apply_settings(body)
            if ok:
                out["ok"] = True
                json_response(self, 200, out)
            else:
                json_response(self, 200, {"ok": False, "error": out})
            return
        if path == "/replay/stop":
            json_response(self, 200, stop_replay())
            return
        if path == "/replay/pause":
            body = self._read_json_body() or {}
            json_response(self, 200, set_replay_paused(body.get("paused")))
            return
        if path == "/replay/lock":
            body = self._read_json_body() or {}
            json_response(self, 200, lock_camera(
                locked=bool(body.get("locked", True)),
                bearing_mode=body.get("bearing")))
            return
        if path == "/replay/recenter":
            json_response(self, 200, recenter_camera())
            return
        if path == "/replay/chase":
            body = self._read_json_body()
            if body is None:
                json_response(self, 200, {"ok": False, "error": "invalid json"})
                return
            json_response(self, 200, update_chase(body))
            return
        if path == "/logbook/purge":
            body = self._read_json_body()
            if body is None:
                json_response(self, 200, {"ok": False, "error": "invalid json"})
                return
            json_response(self, 200, purge_hidden(
                body.get("scope"), body.get("sortie_id"), body.get("key"),
                dry_run=bool(body.get("dry_run", True))))
            return
        if path in ("/logbook/hide", "/logbook/restore"):
            body = self._read_json_body()
            if body is None:
                json_response(self, 200, {"ok": False, "error": "invalid json"})
                return
            json_response(self, 200, set_hidden(
                body.get("scope"), body.get("sortie_id"), body.get("key"),
                hide=(path.endswith("/hide"))))
            return
        if path == "/logbook/prefs":
            body = self._read_json_body()
            if body is None:
                json_response(self, 200, {"ok": False, "error": "invalid json"})
                return
            json_response(self, 200, set_flight_prefs(
                body.get("sortie_id"), rating=body.get("rating"),
                passenger=body.get("passenger")))
            return
        if path == "/logbook/rebuild":
            body = self._read_json_body() or {}
            # An explicit press of Rebuild jumps the debounce and asks for
            # a full reprocess. It no longer runs mid-air: a reprocess bakes
            # every map, and that is the work the frame budget cannot afford.
            # It is held and serviced once the aircraft is stationary on the
            # ground, which is what README's rebuild table describes and what
            # test_forced_maps_and_reprocess_wait_while_airborne pins.
            # force therefore only decides scheduling against the debounce
            # here; maintenance_unsafe() has the final say.
            _logbook_reason[0] = "requested"
            # Pressing Rebuild is the documented way to say "you got this
            # wrong, do it again properly", so it is the one caller that asks
            # for a full reprocess as well as jumping the deferral.
            json_response(self, 200, rebuild_logbook_now(
                bake_maps=body.get("maps", True) is not False, force=True,
                reprocess=True))
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

def start_http():
    try:
        srv = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), StateHandler)
    except OSError as e:
        log("http bind failed %s:%s %s" % (HTTP_HOST, HTTP_PORT, repr(e)))
        return
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    log("http listening %s:%s (localhost only)" % (HTTP_HOST, HTTP_PORT))

def append_jsonl(path, obj):
    with persistence.document_lock(path):
        for attempt in range(ATOMIC_WRITE_TRIES):
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(obj) + "\n")
                    f.flush()
                return
            except PermissionError:
                if attempt + 1 == ATOMIC_WRITE_TRIES:
                    raise
                time.sleep(ATOMIC_WRITE_SLEEP * (attempt + 1))


def write_meta(path, meta, fsync=True):
    try:
        atomic_write(path, meta, fsync=fsync)
    except PermissionError as e:
        log("write_meta PermissionError %s %s" % (path, repr(e)))
    except Exception as e:
        log("write_meta failed %s %s" % (path, repr(e)))


def same_sortie(flight, s):
    if flight is None or not is_valid(s):
        return False
    ac = s.get("aircraft")
    if ac and flight.aircraft and ac != flight.aircraft:
        return False
    if finite(flight.last_lat) and finite(flight.last_lon) and finite(s.get("lat")) and finite(s.get("lon")):
        try:
            if haversine_nm(flight.last_lat, flight.last_lon, s["lat"], s["lon"]) > RESUME_JUMP_NM:
                return False
        except Exception:
            return False
    return True


def restore_tracker_leg(flight_id):
    best = 0
    try:
        for name in os.listdir(CLIPS_DIR):
            if not name.startswith(str(flight_id) + "-") or not name.endswith(".json"):
                continue
            m = re.search(r"-leg(\d+)-", name)
            if m:
                best = max(best, int(m.group(1)))
    except Exception:
        pass
    return best


def carry_sortie_id(s, snap):
    """The sortie a fresh fragment belongs to, or None if this is a new outing.

    Same aircraft and no spawn-sized jump from where the last sample left off,
    which is the same test a disk resume uses - only here the previous
    fragment's file cannot be reopened, so a new one is started under the old
    sortie instead.
    """
    if not isinstance(snap, dict):
        return None
    if (snap.get("aircraft") or None) != (s.get("aircraft") or None):
        return None
    prior = snap.get("sortie_id") or snap.get("flight_id")
    if not prior:
        return None
    lat, lon = s.get("lat"), s.get("lon")
    slat, slon = snap.get("lat"), snap.get("lon")
    if not (finite(lat) and finite(lon) and finite(slat) and finite(slon)):
        return None
    try:
        if haversine_nm(lat, lon, slat, slon) > RESUME_JUMP_NM:
            return None
    except Exception:
        return None
    return prior


def maybe_resume_from_disk(s, snap):
    if not is_valid(s):
        return None
    lat = s.get("lat")
    lon = s.get("lon")
    ac = s.get("aircraft")
    # A hardcoded resume for one flight at one pad used to sit here: spawn
    # within RESUME_JUMP_NM of a particular helipad with that specific
    # un-ended meta on disk and you were attached to a sortie from weeks
    # earlier. The generic resume below plus sortie_id do the same job without
    # being able to capture an unrelated spawn into the wrong outing.
    snap = snap or {}
    snap_ac = snap.get("aircraft")
    fid = snap.get("flight_id")
    if fid and snap_ac == ac:
        slat, slon = snap.get("lat"), snap.get("lon")
        if finite(slat) and finite(slon):
            try:
                d2 = haversine_nm(lat, lon, slat, slon)
            except Exception:
                d2 = None
            if d2 is not None and d2 <= RESUME_JUMP_NM:
                meta_path = os.path.join(SESSIONS, fid + ".meta.json")
                meta = read_json(meta_path) or {}
                if os.path.isfile(meta_path) and not meta.get("ended_at"):
                    log("resume %s from current.json (same aircraft, no spawn jump)" % fid)
                    return Flight.restore(fid, s)
    return None


class Flight:
    def __init__(self, s, sortie_id=None, lift_at=None):
        self.flight_id = new_flight_id()
        # The outing this fragment belongs to. A reconnect mints a new
        # flight_id, so without this the same continuous flight appears under
        # several ids and only the rebuild can tell they belong together -
        # which is why clips could sit under one id while the sortie carried
        # another. The first fragment's id names the sortie.
        self.sortie_id = sortie_id or self.flight_id
        # When this outing first left the ground. Survives a reconnect so the
        # EFB's flight timer does not restart mid-sortie.
        self.lift_at = lift_at
        self.started_at = now_iso()
        self.aircraft = s.get("aircraft")
        self.distance_nm = 0.0
        self.last_lat = s.get("lat")
        self.last_lon = s.get("lon")
        self.last_ts = time.time()
        self.landing_rate_fpm = None
        self.category = None
        self.vs0 = None
        self.landed = False
        self.points = 0
        self.jsonl = os.path.join(SESSIONS, self.flight_id + ".jsonl")
        self.meta_path = os.path.join(SESSIONS, self.flight_id + ".meta.json")
        persistence.claim_recording(self.jsonl)
        self._record_point(s)
        self._save_meta(ended=False, reason=None)

    @classmethod
    def restore(cls, flight_id, s):
        persistence.claim_recording(os.path.join(SESSIONS, flight_id + ".jsonl"))
        meta = read_json(os.path.join(SESSIONS, flight_id + ".meta.json")) or {}
        obj = cls.__new__(cls)
        obj.flight_id = flight_id
        obj.sortie_id = meta.get("sortie_id") or flight_id
        obj.lift_at = meta.get("lift_at")
        obj.started_at = meta.get("started_at") or now_iso()
        obj.aircraft = s.get("aircraft") or meta.get("aircraft")
        try:
            obj.distance_nm = float(meta.get("distance_nm") or 0.0)
        except Exception:
            obj.distance_nm = 0.0
        obj.last_lat = s.get("lat")
        obj.last_lon = s.get("lon")
        obj.last_ts = time.time()
        obj.landing_rate_fpm = meta.get("landing_rate_fpm")
        obj.category = meta.get("category")
        obj.vs0 = meta.get("vs0")
        obj.landed = bool(meta.get("landed"))
        try:
            obj.points = int(meta.get("points") or 0)
        except Exception:
            obj.points = 0
        obj.jsonl = os.path.join(SESSIONS, flight_id + ".jsonl")
        obj.meta_path = os.path.join(SESSIONS, flight_id + ".meta.json")
        obj._record_point(s)
        obj._save_meta(ended=False, reason=None)
        return obj

    def _record_point(self, s):
        pt = {
            "ts": s["ts"],
            "lat": s["lat"],
            "lon": s["lon"],
            "alt": s["alt"],
            "vs": s["vs"],
            "gs": s["gs"],
            "on_ground": s["on_ground"],
            "heading": s["heading"],
            "airspeed": s["airspeed"],
        }
        # Rounded to what the measurement is actually worth. A double written
        # in full costs about twenty characters a second each; none of these
        # mean anything past the digits kept here.
        for key, places in (("bank", 2), ("pitch", 2), ("agl", 1),
                            ("gforce", 3), ("accel_x", 3), ("accel_y", 3),
                            ("accel_z", 3), ("gear", 0), ("flaps", 0),
                            ("spoilers", 0)):
            v = s.get(key)
            if finite(v):
                # The + 0.0 turns -0.0 back into 0.0: rounding a reading of
                # -3e-06 keeps the sign, and a track full of "-0.0" is noise.
                pt[key] = (round(float(v), places) + 0.0 if places
                           else int(round(v)))
        # The worst seen since the last point rather than a single reading
        # taken on a random phase of the bump. _record_point empties the
        # accumulator it was handed, which is how the caller knows the window
        # closed - it holds the same dict and keeps merging into it.
        pk = s.get("peaks")
        if pk:
            for key, v in pk.items():
                if finite(v):
                    pt[key] = round(float(v), 3) + 0.0
            pk.clear()
        try:
            append_jsonl(self.jsonl, pt)
            self.points += 1
        except PermissionError as e:
            log("jsonl write PermissionError %s %s" % (self.flight_id, repr(e)))
        except Exception as e:
            log("jsonl write failed %s %s" % (self.flight_id, repr(e)))

    def _save_meta(self, ended, reason):
        try:
            write_meta(self.meta_path, {
                "flight_id": self.flight_id,
                "sortie_id": getattr(self, "sortie_id", self.flight_id),
                "lift_at": getattr(self, "lift_at", None),
                "started_at": self.started_at,
                "ended_at": now_iso() if ended else None,
                "end_reason": reason,
                "aircraft": self.aircraft,
                "category": self.category,
                "vs0": self.vs0,
                "distance_nm": round(self.distance_nm, 4),
                "landing_rate_fpm": self.landing_rate_fpm,
                "landed": self.landed,
                "points": self.points,
            }, fsync=ended)   # in-flight meta is rewritten every second
        except PermissionError as e:
            log("meta write PermissionError %s %s" % (self.flight_id, repr(e)))
        except Exception as e:
            log("meta write failed %s %s" % (self.flight_id, repr(e)))

    def update(self, s):
        now = time.time()
        if not self.category and s.get("category"):
            self.category = s.get("category")
        if self.vs0 is None and finite(s.get("vs0")):
            self.vs0 = float(s["vs0"])
        if finite(s.get("lat")) and finite(s.get("lon")) and finite(self.last_lat) and finite(self.last_lon):
            d = haversine_nm(self.last_lat, self.last_lon, s["lat"], s["lon"])
            if d < 20.0:
                self.distance_nm += d
        self.last_lat = s.get("lat")
        self.last_lon = s.get("lon")
        self.last_ts = now
        if s.get("aircraft"):
            self.aircraft = s.get("aircraft")
        self._record_point(s)
        self._save_meta(ended=False, reason=None)

    def end(self, reason):
        self._save_meta(ended=True, reason=reason)
        persistence.release_recording(self.jsonl)
        write_event("flight_end", self.flight_id, self.aircraft,
                    sortie_id=getattr(self, "sortie_id", None))
        log("flight_end %s reason=%s" % (self.flight_id, reason))
        # Forced: the flight is over, this is exactly when it should run.
        schedule_logbook_rebuild(reason="flight_end %s" % self.flight_id, force=True)


class OpenClip:
    def __init__(self, kind, flight_id, leg, aircraft, event_s, prefix, after_sec,
                 rate_fpm, livery=None, sortie_id=None):
        self.kind = kind
        self.flight_id = flight_id
        # The clip id is built from the fragment, so a reconnect leaves clips
        # filed under an id the sortie does not carry. Record the sortie too.
        self.sortie_id = sortie_id or flight_id
        self.leg = int(leg)
        self.aircraft = aircraft
        self.livery = livery
        self.t0 = event_s.get("t")
        self.t0_iso = event_s.get("ts") or now_iso()
        self.lat = event_s.get("lat")
        self.lon = event_s.get("lon")
        self.rate_fpm = rate_fpm
        self.clip_id = "%s-leg%s-%s" % (flight_id, self.leg, kind)
        self.path = clipfile.write_path(CLIPS_DIR, self.clip_id)
        # Claimed before the first write, not at the first checkpoint: the
        # constructor writes immediately, so a claim taken any later leaves a
        # window in which a purge could delete the file and the next append
        # recreate it - a recording resurrected after the user was told it
        # was destroyed.
        persistence.claim_recording(self.path)
        self._claimed = True
        self._committed = 0        # bytes known to be whole records
        self._written = 0          # points already appended
        self._header_written = False
        self._stopped = False      # set when the file can no longer be trusted
        self.until = self.t0 + after_sec
        self.points = list(prefix)
        self.done = False
        self._last_write = 0.0
        self.write(force=True)

    def add_sample(self, s):
        if self.done:
            return
        t = s.get("t")
        if t is None:
            return
        if t > self.until + 0.05:
            # The ordinary end of a clip, and by far the common one. This used
            # to set done and write, which was the whole of finish() before
            # the format change - so after it, a normally completed clip got
            # no end record, kept its recording claim until the watcher
            # restarted, and was refused as a source for touchdown recovery
            # because it read as incomplete. Route it through finalization.
            self.finish()
            return
        if t < (self.t0 - max(TAKEOFF_BEFORE, LANDING_BEFORE) - 1):
            return
        if self.points:
            last_t = self.points[-1].get("t")
            if last_t is not None and t <= last_t:
                return          # already covered by the buffered prefix
        self.points.append(clip_point(s))
        self.write(force=False)

    def as_doc(self):
        t1_iso = None
        if self.points:
            last_t = self.points[-1].get("t")
            t1_iso = iso_from_unix(last_t) if last_t is not None else None
        # t0 is the start of the window, not the event - t1 is the window end,
        # and having one of them mean the event made a clip look 5 s short.
        first_t = self.points[0].get("t") if self.points else None
        t0_iso = iso_from_unix(first_t) if first_t is not None else self.t0_iso
        return {
            "id": self.clip_id,
            "flight_id": self.flight_id,
            "sortie_id": getattr(self, "sortie_id", None) or self.flight_id,
            "leg": self.leg,
            "kind": self.kind,
            "aircraft": self.aircraft,
            "livery": self.livery,
            "t0": t0_iso,
            "t_event": self.t0_iso,
            "t1": t1_iso,
            "att_unit": "deg",
            "rate_fpm": self.rate_fpm,
            "lat": self.lat,
            "lon": self.lon,
            "points": self.points,
        }

    def write(self, force=True):
        """Append what has arrived since the last checkpoint.

        This used to rewrite the whole accumulated clip every time, on the
        sampling loop, so the cost grew with what had already been recorded:
        243 ms of blocking work across a takeoff clip, and 7.2 MB written to
        save 546 KB. Appending only the new points is 17.7 ms and 0.37 MB.

        Nothing advances until an append succeeds, so a failed batch is
        retried whole at the next checkpoint and a point is written once or
        not yet.
        """
        now = time.time()
        if not force and (now - self._last_write) < CLIP_WRITE_SEC:
            return True         # nothing owed, so nothing failed
        if self._stopped:
            return False
        doc = self.as_doc()
        batch = []
        if not self._header_written:
            if not self.points:
                return False    # t0 comes from the first point; wait for it
            batch.append(clipfile.header_record(doc))
        batch.extend(dict(p, k="p") for p in self.points[self._written:])
        batch.append(clipfile.meta_record(doc))
        try:
            self._committed = persistence.append_lines(
                self.path, batch, fsync=bool(force))
        except persistence.SyncFailed as e:
            # The records landed; only durability was refused. Keep going.
            log("clip sync failed %s %s" % (self.clip_id, repr(e)))
        except persistence.RollbackFailed as e:
            # Where the last whole record ends is no longer known, so writing
            # more would append past a boundary nobody can find.
            self._stopped = True
            log("clip write stopped %s %s" % (self.clip_id, repr(e)))
            return False
        except Exception as e:
            log("clip write failed %s %s" % (self.clip_id, repr(e)))
            return False
        self._header_written = True
        self._written = len(self.points)
        self._last_write = now
        return True

    def finish(self):
        """Close the clip: last points, last meta, and the end record.

        finish() has no next checkpoint to retry on - the tracker clears its
        open clips straight after - so the retry is bounded here instead. If
        it is exhausted no end record is written, which is exactly right: the
        clip is then visibly incomplete rather than quietly short.
        """
        self.done = True
        try:
            for attempt in range(CLIP_FINISH_TRIES):
                # The end record is a claim that everything before it landed.
                # Appending one over a failed data write produces a clip that
                # reads complete while points are missing, which is worse than
                # an obviously unfinished one - so the data write has to say
                # it succeeded first.
                if not self.write(force=True):
                    log("clip end withheld %s: pending records did not land "
                        "(attempt %d)" % (self.clip_id, attempt + 1))
                    if self._stopped:
                        break
                    time.sleep(CLIP_FINISH_SLEEP * (attempt + 1))
                    continue
                try:
                    self._committed = persistence.append_lines(
                        self.path, [clipfile.end_record()], fsync=True)
                    return
                except persistence.SyncFailed as e:
                    log("clip end sync failed %s %s" % (self.clip_id, repr(e)))
                    return
                except persistence.RollbackFailed as e:
                    # Retrying would append against a boundary nobody knows.
                    self._stopped = True
                    log("clip end stopped %s %s" % (self.clip_id, repr(e)))
                    break
                except Exception as e:
                    log("clip end append failed %s attempt %d %s"
                        % (self.clip_id, attempt + 1, repr(e)))
                    time.sleep(CLIP_FINISH_SLEEP * (attempt + 1))
            unwritten = len(self.points) - self._written
            log("clip left incomplete %s: no end record, %d point(s) never "
                "reached the file" % (self.clip_id, max(0, unwritten)))
        finally:
            self.release()

    def release(self):
        if getattr(self, "_claimed", False):
            persistence.release_recording(self.path)
            self._claimed = False


# The running tracker, so a settings change can resize its ring buffer in
# place instead of waiting for a restart.
LIVE_TRACKER = [None]


# A clip window change that lands while a clip is still filling would give
# that clip a window neither the old nor the new setting describes - shrinking
# the ring mid-capture is how you get a short takeoff. Hold the change and
# apply it at the next transition instead.
_pending_buffer_max = [None]
_pending_clip_windows = [None]


def clip_capture_in_progress():
    with RUNTIME_LOCK:
        return bool(RUNTIME.get("open_clip_ids"))


def resize_live_buffer(new_max, defer_if_capturing=True):
    """Grow or shrink the ring buffer without losing what is in it."""
    tr = LIVE_TRACKER[0]
    if tr is None:
        return
    if defer_if_capturing and clip_capture_in_progress():
        _pending_buffer_max[0] = new_max
        log("ring buffer change held: a clip is still being captured "
            "(applies at the next takeoff or landing)")
        return
    old = tr.buffer
    if old.maxlen == new_max:
        return
    tr.buffer = deque(old, maxlen=new_max)
    log("ring buffer resized to %d samples (%.0fs)" % (new_max, BUFFER_SEC))


def hold_clip_windows(values):
    """Stage new clip windows instead of applying them under an open clip.

    The window a clip gets is decided at commit from these constants, so
    changing them while a clip is filling produces a clip that matches neither
    the old setting nor the new one - and asking for a longer prefix than the
    ring currently holds just gives a short one.
    """
    _pending_clip_windows[0] = dict(values)


def apply_pending_buffer_resize():
    """Called once no clip is open, so held changes take effect together."""
    windows = _pending_clip_windows[0]
    if windows is not None:
        _pending_clip_windows[0] = None
        for name, value in windows.items():
            globals()[name] = value
        log("clip windows applied: takeoff %.0f/%.0f, landing %.0f/%.0f"
            % (TAKEOFF_BEFORE, TAKEOFF_AFTER, LANDING_BEFORE, LANDING_AFTER))
    pending = _pending_buffer_max[0]
    if pending is None:
        return
    _pending_buffer_max[0] = None
    resize_live_buffer(pending, defer_if_capturing=False)


class ClipTracker:
    def __init__(self):
        self.buffer = deque(maxlen=BUFFER_MAX)
        # A landing that has bounced and not yet settled.
        self.bounce = None
        # Altitude at the last sample that was actually on the ground. Bounce
        # height measures from there: by the time the aircraft reads airborne
        # it has already left, so using that sample under-reads the bounce.
        self.last_ground_alt = None
        self.flight = None
        self.leg = 0
        self.prev_on_ground = None
        self.last_lat = None
        self.last_lon = None
        self.pending = None
        self.open = []
        self.vs_air = deque(maxlen=12)
        self.airborne_since = None

    def attach(self, flight):
        self.flight = flight
        with RUNTIME_LOCK:
            RUNTIME["flight_id"] = flight.flight_id
            RUNTIME["leg"] = self.leg

    def reset(self):
        self.finish()
        self.flight = None
        self.leg = 0
        self.prev_on_ground = None
        self.last_lat = None
        self.last_lon = None
        self.pending = None
        self.airborne_since = None
        self.vs_air.clear()
        with RUNTIME_LOCK:
            RUNTIME["flight_id"] = None
            RUNTIME["leg"] = 0
            RUNTIME["open_clip_ids"] = []

    def finish(self):
        for clip in self.open:
            clip.finish()
        self.open = []
        with RUNTIME_LOCK:
            RUNTIME["open_clip_ids"] = []

    def _window(self, t0, before):
        """Buffered samples from `before` seconds ahead of t0 up to right now.

        Deliberately no upper cut at t0. A commit lands BOUNCE_SEC (~2.5 s with
        loop granularity) after the transition it describes, because the tracker
        waits to be sure a touchdown is not a bounce. Cutting the prefix at t0
        threw those seconds away and live capture only began at commit time, so
        every clip lost the couple of seconds spanning the actual event - the
        ghost jumped forward exactly at the touchdown. Those samples are sitting
        in the ring buffer; take them.
        """
        lo = t0 - before
        return [clip_point(s) for s in self.buffer if s.get("t") is not None and s["t"] >= lo]

    def _slew_or_jump(self, s):
        if s.get("slew") is True:
            return True
        if is_spawn_junk(s.get("lat"), s.get("lon")):
            return True
        if finite(s.get("lat")) and finite(s.get("lon")) and finite(self.last_lat) and finite(self.last_lon):
            try:
                if haversine_nm(self.last_lat, self.last_lon, s["lat"], s["lon"]) >= SLEW_JUMP_NM:
                    return True
            except Exception:
                return True
        return False

    def _sync_open_ids(self):
        with RUNTIME_LOCK:
            RUNTIME["open_clip_ids"] = [c.clip_id for c in self.open if not c.done]
            RUNTIME["leg"] = self.leg
            still_capturing = bool(RUNTIME["open_clip_ids"])
        # The moment nothing is being captured, a held clip-window change is
        # safe to apply.
        if not still_capturing:
            apply_pending_buffer_resize()

    def _commit(self, kind, event_s, rate_fpm, contacts=None, heights=None):
        if self.flight is None:
            return
        if replay_in_progress():
            # A replay puts a second aircraft into the world, usually on the
            # exact patch of ground the user is parked on - they landed there,
            # and that is the clip they are watching. At the ghost's touchdown
            # the sim broke the parked aircraft's ground contact for a single
            # sample, which reads as a landing. The event was committed, a clip
            # was written for it, and the clip it overwrote was the one playing.
            #
            # Nothing observed during a replay is trustworthy, so nothing
            # observed during a replay is recorded. The cost is that genuinely
            # landing while a replay runs goes unrecorded, which is a trade
            # worth making against destroying a recording that already exists.
            log("%s ignored: a replay is running, so the world has a second "
                "aircraft in it and the detector cannot be trusted" % kind)
            return
        if kind == "takeoff":
            self.leg += 1
            before, after = TAKEOFF_BEFORE, TAKEOFF_AFTER
            if getattr(self.flight, "lift_at", None) is None:
                self.flight.lift_at = event_s.get("ts") or now_iso()
        else:
            if self.leg < 1:
                self.leg = 1
            before, after = LANDING_BEFORE, LANDING_AFTER
            if self.flight:
                self.flight.landing_rate_fpm = rate_fpm
                self.flight.landed = True
        # Window around the TRANSITION sample (first airborne / first ground), not bounce-commit time.
        prefix = self._window(event_s["t"], before)
        if not any(abs((p.get("t") or 0) - event_s["t"]) < 0.25 for p in prefix):
            prefix.append(clip_point(event_s))
        clip = OpenClip(
            kind=kind,
            flight_id=self.flight.flight_id,
            leg=self.leg,
            aircraft=self.flight.aircraft or event_s.get("aircraft"),
            event_s=event_s,
            prefix=prefix,
            after_sec=after,
            rate_fpm=rate_fpm,
            livery=event_s.get("livery"),
            sortie_id=getattr(self.flight, "sortie_id", None),
        )
        self.open.append(clip)
        extra = {
            "on_ground": kind == "landing",
            "leg": self.leg,
            "lat": event_s.get("lat"),
            "lon": event_s.get("lon"),
            "clip_id": clip.clip_id,
            "clip_path": clip.path,
        }
        if kind == "landing":
            extra["landing_rate_fpm"] = rate_fpm
            # Every contact of this landing, in the order they happened, with
            # the height of the bounce that led to each. One entry is a clean
            # arrival; more than one says it bounced, how high, and how hard it
            # came back down.
            rates = [round(abs(float(r)), 1) if finite(r) else None
                     for r in (contacts or [])]
            hs = list(heights or [])
            extra["contacts"] = len(rates) or 1
            extra["bounces"] = max(0, len(rates) - 1)
            extra["contact_rates_fpm"] = rates
            extra["bounce_heights_ft"] = hs
            # Paired up, so a bounce and the touchdown that ended it stay
            # together rather than being two lists to line up by index.
            extra["bounce_detail"] = [
                {"height_ft": hs[i] if i < len(hs) else None,
                 "rate_fpm": rates[i + 1] if (i + 1) < len(rates) else None}
                for i in range(max(0, len(rates) - 1))
            ]
            firmest = [r for r in rates if r is not None]
            extra["firmest_fpm"] = max(firmest) if firmest else None
        write_event(kind, self.flight.flight_id,
                    extra.get("aircraft") or self.flight.aircraft, extra=extra,
                    sortie_id=getattr(self.flight, "sortie_id", None))
        ev_line = {
            "time": event_s.get("ts") or now_iso(),
            "kind": kind,
            "flight_id": self.flight.flight_id,
            "sortie_id": getattr(self.flight, "sortie_id", None) or self.flight.flight_id,
            "leg": self.leg,
            "aircraft": self.flight.aircraft,
            "lat": event_s.get("lat"),
            "lon": event_s.get("lon"),
            "rate_fpm": rate_fpm,
            "bounces": extra.get("bounces"),
            "contact_rates_fpm": extra.get("contact_rates_fpm"),
            "bounce_heights_ft": extra.get("bounce_heights_ft"),
            "bounce_detail": extra.get("bounce_detail"),
            "clip": clip.path,
        }
        try:
            append_jsonl(EVENTS_JSONL, ev_line)
        except Exception as e:
            log("events.jsonl write failed %s" % repr(e))
        with RUNTIME_LOCK:
            RUNTIME["recent_events"].append(ev_line)
            RUNTIME["leg"] = self.leg
        self._sync_open_ids()
        log("%s %s leg=%s clip=%s rate_fpm=%s" % (kind, self.flight.flight_id, self.leg, clip.clip_id, str(rate_fpm)))

    def feed(self, s):
        still = []
        closed = 0
        for clip in self.open:
            clip.add_sample(s)
            if not clip.done:
                still.append(clip)
            else:
                closed += 1
        self.open = still
        self._sync_open_ids()
        if closed:
            # The clip window just closed, so the leg is on disk and the
            # logbook can pick it up.
            # Index now so the leg appears in the logbook; maps when parked.
            schedule_logbook_rebuild(delay=3.0, bake_maps=False,
                                     reason="%d clip(s) closed" % closed)

        if not is_valid(s):
            return
        self.buffer.append(s)
        jumped = self._slew_or_jump(s)
        nowg = s.get("on_ground")
        if nowg is False and finite(s.get("vs")):
            self.vs_air.append(s["vs"])
        if nowg is False:
            if self.airborne_since is None:
                self.airborne_since = s.get("t")
        else:
            self.airborne_since = None

        if jumped:
            self.pending = None
            self.prev_on_ground = nowg
            self.last_lat = s.get("lat")
            self.last_lon = s.get("lon")
            return

        if nowg is True:
            ground_alt_now = as_float(s.get("alt"))
            if ground_alt_now is not None:
                self.last_ground_alt = ground_alt_now

        live_bounce = getattr(self, "bounce", None)
        if live_bounce is not None:
            if (s.get("t") or 0.0) > live_bounce.get("until", 0.0):
                self.bounce = None
            elif nowg is False:
                alt_now = as_float(s.get("alt"))
                if alt_now is not None:
                    peak = live_bounce.get("peak_alt")
                    live_bounce["peak_alt"] = (alt_now if peak is None
                                               else max(peak, alt_now))

        if self.pending is not None:
            want_air = self.pending["kind"] == "takeoff"
            stable = (nowg is False) if want_air else (nowg is True)
            if not stable:
                # A landing that bounced. Remember it rather than dropping it:
                # discarding it meant the grade came from wherever the aircraft
                # finally settled, which is always softer than the arrival, and
                # not what the landing felt like.
                if self.pending["kind"] == "landing":
                    self.bounce = {
                        "event": self.pending["event"],
                        "rate_fpm": self.pending.get("rate_fpm"),
                        "contacts": list(self.pending.get("contacts") or []),
                        "heights": list(self.pending.get("heights") or []),
                        # Ground level for this bounce is the last sample that
                        # was really on the ground, not this one - this sample
                        # already reads airborne. A later bounce can start from
                        # a different spot along the runway, so it is re-read
                        # each time rather than fixed at the first contact.
                        "ref_alt": (self.last_ground_alt
                                    if self.last_ground_alt is not None
                                    else as_float(s.get("alt"))),
                        "peak_alt": None,
                        "until": (s.get("t") or 0.0) + BOUNCE_MERGE_SEC,
                    }
                self.pending = None
            elif (s.get("t") - self.pending["since"]) >= BOUNCE_SEC:
                ev = self.pending["event"]
                rate = self.pending.get("rate_fpm")
                kind = self.pending["kind"]
                contacts = self.pending.get("contacts")
                heights = self.pending.get("heights")
                self.pending = None
                self._commit(kind, ev, rate, contacts=contacts, heights=heights)

        if self.flight is not None and self.pending is None and replay_in_progress():
            # Same reason as _commit: while a ghost is flying, a transition on
            # the user aircraft is as likely to be the sim reacting to it as
            # anything the pilot did. Refusing to open the pending means one
            # cannot outlive the replay and commit after it stops.
            pass
        elif self.flight is not None and self.pending is None and self.prev_on_ground is True and nowg is False:
            self.pending = {"kind": "takeoff", "since": s.get("t"), "event": dict(s), "rate_fpm": None}
        elif self.flight is not None and self.pending is None and self.prev_on_ground is False and nowg is True:
            rate = None
            if self.vs_air:
                rate = self.vs_air[-1]
            elif finite(s.get("vs")):
                rate = s["vs"]
            vs_rate = rate
            td = s.get("touchdown_fps")
            if finite(td) and abs(float(td)) >= TOUCHDOWN_FPS_MIN:
                rate = float(td) * 60.0
            elif finite(td):
                # Latched at zero: say so, rather than silently grading an
                # arrival as perfect.
                log("touchdown normal velocity read %.4f ft/s; using "
                    "vertical speed %s fpm instead"
                    % (float(td), "?" if not finite(vs_rate) else "%.1f" % vs_rate))
            # Store a magnitude. The sim's touchdown normal velocity is
            # already one, so a vertical-speed fallback that kept its sign
            # left the logbook with two conventions depending only on
            # which path produced the number.
            if finite(rate):
                rate = abs(float(rate))
            event = dict(s)
            contacts = [rate]
            heights = []
            bounce = getattr(self, "bounce", None)
            if bounce and (s.get("t") or 0.0) <= bounce.get("until", 0.0):
                # Same landing, another contact. Keep where and when it first
                # touched, grade on the firmest, and keep every contact so the
                # bounce itself can be reported rather than quietly absorbed.
                event = bounce["event"]
                contacts = list(bounce.get("contacts") or []) + [rate]
                ref_alt, peak_alt = bounce.get("ref_alt"), bounce.get("peak_alt")
                height = None
                if ref_alt is not None and peak_alt is not None:
                    height = round(max(0.0, float(peak_alt) - float(ref_alt)), 1)
                heights = list(bounce.get("heights") or []) + [height]
                prev_rate = bounce.get("rate_fpm")
                if finite(prev_rate) and (not finite(rate)
                                          or abs(prev_rate) > abs(rate)):
                    rate = prev_rate
                self.bounce = None
                log("bounce: %d contacts, firmest %.0f fpm, height %s ft"
                    % (len(contacts), abs(rate) if finite(rate) else 0.0,
                       "?" if height is None else ("%.1f" % height)))
            self.pending = {"kind": "landing", "since": s.get("t"), "event": event,
                            "rate_fpm": rate, "contacts": contacts, "heights": heights}

        self.prev_on_ground = nowg
        self.last_lat = s.get("lat")
        self.last_lon = s.get("lon")


def _is_hr(hr, value=0):
    try:
        return ctypes.c_ulong(ctypes.HRESULT(hr).value).value == int(value)
    except Exception:
        return False


class _SC_XYZ(Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("z", ctypes.c_double)]


class _SC_PBH(Structure):
    _fields_ = [("Pitch", ctypes.c_double), ("Bank", ctypes.c_double), ("Heading", ctypes.c_double)]


class _SC_CAMERA(Structure):
    # XYZ 3 doubles, u32 ref, u32 oid, XYZ targeted, PBH 3 doubles, u32 rot ref, u32 rot oid, double fov
    _fields_ = [
        ("Position", _SC_XYZ),
        ("PositionReferential", ctypes.c_uint32),
        ("PositionReferentialObjectId", ctypes.c_uint32),
        ("TargetedPos", _SC_XYZ),
        ("Pbh", _SC_PBH),
        ("RotationReferential", ctypes.c_uint32),
        ("RotationReferentialObjectId", ctypes.c_uint32),
        ("Fov", ctypes.c_double),
    ]


class _SC_INITPOS(Structure):
    _fields_ = [
        ("Latitude", ctypes.c_double),
        ("Longitude", ctypes.c_double),
        ("Altitude", ctypes.c_double),
        ("Pitch", ctypes.c_double),
        ("Bank", ctypes.c_double),
        ("Heading", ctypes.c_double),
        ("OnGround", ctypes.c_uint32),
        ("Airspeed", ctypes.c_uint32),
    ]


class _SC_RECV(Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("dwVersion", ctypes.c_uint32), ("dwID", ctypes.c_uint32)]


class _SC_RECV_ASSIGNED(Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("dwVersion", ctypes.c_uint32),
        ("dwID", ctypes.c_uint32),
        ("dwRequestID", ctypes.c_uint32),
        ("dwObjectID", ctypes.c_uint32),
    ]


class _SC_RECV_EXCEPTION(Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("dwVersion", ctypes.c_uint32),
        ("dwID", ctypes.c_uint32),
        ("dwException", ctypes.c_uint32),
        ("UNKNOWN_SENDID", ctypes.c_uint32),
        ("dwSendID", ctypes.c_uint32),
        ("UNKNOWN_INDEX", ctypes.c_uint32),
        ("dwIndex", ctypes.c_uint32),
    ]


class _SC_RECV_CAMERA_STATUS(Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("dwVersion", ctypes.c_uint32),
        ("dwID", ctypes.c_uint32),
        ("dwAcquiredState", ctypes.c_uint32),
        ("bGameControlled", ctypes.c_int32),
    ]


_GAME_BIND = {
    "done": False,
    "ok": False,
    "path": None,
    "dll": None,
    "missing": [],
    "reason": None,
    "fns": {},
}


def _process_image_dirs(exe_names):
    result = []
    want = {n.lower() for n in exe_names}
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class PROCESSENTRY32W(Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_int32),
                ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        k32.Process32FirstW.restype = ctypes.c_int
        k32.Process32FirstW.argtypes = [ctypes.c_void_p, POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.restype = ctypes.c_int
        k32.Process32NextW.argtypes = [ctypes.c_void_p, POINTER(PROCESSENTRY32W)]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.QueryFullProcessImageNameW.restype = ctypes.c_int
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            POINTER(ctypes.c_uint32),
        ]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return result
        pe = PROCESSENTRY32W()
        pe.dwSize = sizeof(pe)
        ok = k32.Process32FirstW(snap, byref(pe))
        while ok:
            name = (pe.szExeFile or "").lower()
            if name in want:
                h = k32.OpenProcess(0x1000, 0, pe.th32ProcessID)
                if h:
                    buf = ctypes.create_unicode_buffer(32768)
                    size = ctypes.c_uint32(32768)
                    if k32.QueryFullProcessImageNameW(h, 0, buf, byref(size)) and buf.value:
                        result.append(os.path.dirname(buf.value))
                    k32.CloseHandle(h)
            ok = k32.Process32NextW(snap, byref(pe))
        k32.CloseHandle(snap)
    except Exception as e:
        log("fs process scan failed %s" % repr(e))
    return result


def _appx_package_names(prefix):
    """Installed package full names starting with prefix, newest-looking last.

    Windows will not let a normal user *list* the WindowsApps folder - the
    ACL is TrustedInstaller - but it will happily open an exact path inside it.
    So a glob there returns nothing however the sim is installed, and asking
    the package repository for the name is the difference between a working
    lookup and one that can only ever fail.
    """
    names = []
    try:
        import winreg
    except ImportError:
        return names
    key = (r"Software\Classes\Local Settings\Software\Microsoft\Windows"
           r"\CurrentVersion\AppModel\Repository\Packages")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            count = winreg.QueryInfoKey(k)[0]
            for i in range(count):
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                if sub.startswith(prefix):
                    names.append(sub)
    except OSError as e:
        log("appx package lookup failed %s" % repr(e))
    return sorted(names)


def _windowsapps_dlls():
    """The DLL inside the installed MSFS package, with the sim not running."""
    out = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    apps = os.path.join(pf, "WindowsApps")
    # The version used to be written down here. It aged out: a machine on
    # 1.8.16.0 matched neither the hardcoded 1.8.14.0 nor the unlistable glob,
    # so with the sim closed there was no offline path at all.
    for name in _appx_package_names("Microsoft.Limitless_"):
        p = os.path.join(apps, name, GAME_DLL_FILENAME)
        if os.path.isfile(p):
            out.append(p)
    try:
        out.extend(g for g in glob.glob(
            os.path.join(apps, "Microsoft.Limitless_*", GAME_DLL_FILENAME))
            if g not in out)
    except Exception:
        pass
    return out


def _ensure_native_copy(src):
    if not src or not os.path.isfile(src):
        return None
    try:
        os.makedirs(NATIVE_DIR, exist_ok=True)
        dst = NATIVE_DLL
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        if os.path.isfile(dst):
            return dst
    except Exception as e:
        log("copy SimConnect_internal.dll to native/ failed %s src=%s" % (repr(e), src))
    return None


def _dll_has_exports(path, names):
    if not path or not os.path.isfile(path):
        return False, list(names)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryW.restype = ctypes.c_void_p
    k32.LoadLibraryW.argtypes = [ctypes.c_wchar_p]
    k32.GetProcAddress.restype = ctypes.c_void_p
    k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    k32.FreeLibrary.argtypes = [ctypes.c_void_p]
    h = k32.LoadLibraryW(path)
    if not h:
        return False, list(names)
    missing = []
    try:
        for name in names:
            if not k32.GetProcAddress(h, name.encode("ascii")):
                missing.append(name)
    finally:
        k32.FreeLibrary(h)
    return (len(missing) == 0), missing


_CAMERA_EXPORTS = (
    "SimConnect_CameraAcquire",
    "SimConnect_CameraSet",
    "SimConnect_CameraRelease",
    "SimConnect_Open",
    "SimConnect_Close",
    "SimConnect_GetNextDispatch",
    "SimConnect_AICreateNonATCAircraft",
    "SimConnect_AIRemoveObject",
    "SimConnect_AddToDataDefinition",
    "SimConnect_SetDataOnSimObject",
    "SimConnect_RequestDataOnSimObject",
)


def resolve_game_simconnect_dll():
    ordered = []
    if os.path.isfile(NATIVE_DLL):
        ordered.append(NATIVE_DLL)
    for d in _process_image_dirs(("FlightSimulator2024.exe", "FlightSimulator.exe")):
        p = os.path.join(d, GAME_DLL_FILENAME)
        if os.path.isfile(p):
            ordered.append(p)
    ordered.extend(_windowsapps_dlls())
    seen = set()
    for raw in ordered:
        path = os.path.abspath(raw)
        if path in seen:
            continue
        seen.add(path)
        ok, missing = _dll_has_exports(path, _CAMERA_EXPORTS)
        if not ok:
            log("game dll skip path=%s missing=%s" % (path, ",".join(missing)))
            continue
        if os.path.abspath(path) != os.path.abspath(NATIVE_DLL):
            copied = _ensure_native_copy(path)
            if copied:
                ok2, missing2 = _dll_has_exports(copied, _CAMERA_EXPORTS)
                if ok2:
                    return copied
        return path
    return None


def load_game_simconnect_dll():
    if _GAME_BIND["done"] and _GAME_BIND["dll"] is not None:
        return _GAME_BIND
    path = resolve_game_simconnect_dll()
    _GAME_BIND["path"] = path
    _GAME_BIND["missing"] = []
    _GAME_BIND["reason"] = None
    _GAME_BIND["fns"] = {}
    if not path:
        # Not "found one, and it lacked all eleven exports" - that is what
        # filling this with _CAMERA_EXPORTS said, and it sent a reader looking
        # for a corrupt DLL when the truth was that no file was found at all.
        _GAME_BIND["done"] = True
        _GAME_BIND["ok"] = False
        _GAME_BIND["missing"] = []
        _GAME_BIND["reason"] = "no SimConnect_internal.dll found"
        log("game SimConnect_internal.dll not resolved (no candidate found)")
        return _GAME_BIND
    try:
        dll = ctypes.WinDLL(path)
    except Exception as e:
        _GAME_BIND["done"] = True
        _GAME_BIND["ok"] = False
        _GAME_BIND["missing"] = ["WinDLL:%s" % repr(e)]
        log("WinDLL load failed path=%s %s" % (path, repr(e)))
        return _GAME_BIND
    missing = []
    fns = {}
    for name in _CAMERA_EXPORTS + (
        "SimConnect_AICreateSimulatedObject",
        "SimConnect_AIReleaseControl",
        "SimConnect_CameraGet",
        "SimConnect_CameraGetStatus",
    "SimConnect_CameraGet",
    "SimConnect_CameraEnableFlag",
    "SimConnect_MapClientEventToSimEvent",
    "SimConnect_TransmitClientEvent",
    "SimConnect_AICreateNonATCAircraft_EX1",
    ):
        try:
            fns[name] = getattr(dll, name)
        except AttributeError:
            if name in _CAMERA_EXPORTS:
                missing.append(name)
    _GAME_BIND["dll"] = dll
    _GAME_BIND["fns"] = fns
    _GAME_BIND["missing"] = missing
    _GAME_BIND["done"] = True
    if missing:
        _GAME_BIND["ok"] = False
        log("game dll missing exports path=%s missing=%s" % (path, ",".join(missing)))
        return _GAME_BIND
    HRESULT = ctypes.c_long  # not ctypes.HRESULT (that raises OSError on E_FAIL)
    c_char_p = ctypes.c_char_p
    c_void_p = ctypes.c_void_p
    c_float = ctypes.c_float
    fns["SimConnect_Open"].restype = HRESULT
    fns["SimConnect_Open"].argtypes = [POINTER(HANDLE), LPCSTR, HWND, DWORD, HANDLE, DWORD]
    fns["SimConnect_Close"].restype = HRESULT
    fns["SimConnect_Close"].argtypes = [HANDLE]
    fns["SimConnect_GetNextDispatch"].restype = HRESULT
    fns["SimConnect_GetNextDispatch"].argtypes = [HANDLE, POINTER(c_void_p), POINTER(DWORD)]
    # CameraAcquire takes a name, exactly like CameraRelease. In the game DLL
    # the two functions have byte-identical prologues:
    #     48 8b fa   mov rdi, rdx   <- arg2 is a pointer, and it is dereferenced
    #     48 8b f1   mov rsi, rcx   <- arg1 is the handle
    # Binding it as (HANDLE) alone left RDX holding whatever the last call put
    # there, so the DLL dereferenced garbage: "access violation reading 0x...",
    # intermittently "succeeding" whenever that garbage happened to be readable.
    # That is why chase never locked on. Wait for CAMERA_STATUS before CameraSet.
    fns["SimConnect_CameraAcquire"].restype = HRESULT
    fns["SimConnect_CameraAcquire"].argtypes = [HANDLE, c_char_p]
    fns["SimConnect_CameraSet"].restype = HRESULT
    fns["SimConnect_CameraSet"].argtypes = [HANDLE, c_char_p, DWORD]
    fns["SimConnect_CameraRelease"].restype = HRESULT
    fns["SimConnect_CameraRelease"].argtypes = [HANDLE, c_char_p]
    for _flagfn in ("SimConnect_CameraEnableFlag", "SimConnect_CameraDisableFlag"):
        if _flagfn in fns:
            fns[_flagfn].restype = HRESULT
            fns[_flagfn].argtypes = [HANDLE, DWORD]
    if "SimConnect_CameraGet" in fns:
        fns["SimConnect_CameraGet"].restype = HRESULT
        fns["SimConnect_CameraGet"].argtypes = [HANDLE, DWORD]
    if "SimConnect_CameraGetStatus" in fns:
        fns["SimConnect_CameraGetStatus"].restype = HRESULT
        fns["SimConnect_CameraGetStatus"].argtypes = [HANDLE]
    fns["SimConnect_AICreateNonATCAircraft"].restype = HRESULT
    fns["SimConnect_AICreateNonATCAircraft"].argtypes = [HANDLE, c_char_p, c_char_p, _SC_INITPOS, DWORD]
    if "SimConnect_AICreateNonATCAircraft_EX1" in fns:
        # Argument order established by measurement, not by the docs: the
        # livery comes third, BEFORE the tail number. Passing them the other
        # way round returns 0x80004005 and creates no object. Verified by
        # spawning and reading LIVERY NAME back off the resulting object.
        fns["SimConnect_AICreateNonATCAircraft_EX1"].restype = HRESULT
        fns["SimConnect_AICreateNonATCAircraft_EX1"].argtypes = [
            HANDLE, c_char_p, c_char_p, c_char_p, _SC_INITPOS, DWORD]
    if "SimConnect_AICreateSimulatedObject" in fns:
        fns["SimConnect_AICreateSimulatedObject"].restype = HRESULT
        fns["SimConnect_AICreateSimulatedObject"].argtypes = [HANDLE, c_char_p, _SC_INITPOS, DWORD]
    fns["SimConnect_AIRemoveObject"].restype = HRESULT
    fns["SimConnect_AIRemoveObject"].argtypes = [HANDLE, DWORD, DWORD]
    if "SimConnect_AIReleaseControl" in fns:
        fns["SimConnect_AIReleaseControl"].restype = HRESULT
        fns["SimConnect_AIReleaseControl"].argtypes = [HANDLE, DWORD, DWORD]
    fns["SimConnect_AddToDataDefinition"].restype = HRESULT
    fns["SimConnect_AddToDataDefinition"].argtypes = [HANDLE, DWORD, c_char_p, c_char_p, DWORD, c_float, DWORD]
    if "SimConnect_RequestDataOnSimObject" in fns:
        fns["SimConnect_RequestDataOnSimObject"].restype = HRESULT
        # RequestID, DefineID, ObjectID, Period, Flags, origin, interval, limit
        fns["SimConnect_RequestDataOnSimObject"].argtypes = [
            HANDLE, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD]
    fns["SimConnect_SetDataOnSimObject"].restype = HRESULT
    fns["SimConnect_SetDataOnSimObject"].argtypes = [HANDLE, DWORD, DWORD, DWORD, DWORD, DWORD, c_void_p]
    if "SimConnect_MapClientEventToSimEvent" in fns:
        fns["SimConnect_MapClientEventToSimEvent"].restype = HRESULT
        fns["SimConnect_MapClientEventToSimEvent"].argtypes = [HANDLE, DWORD, c_char_p]
    if "SimConnect_TransmitClientEvent" in fns:
        fns["SimConnect_TransmitClientEvent"].restype = HRESULT
        # ObjectID, EventID, dwData, GroupID, Flags
        fns["SimConnect_TransmitClientEvent"].argtypes = [
            HANDLE, DWORD, DWORD, DWORD, DWORD, DWORD]
    _GAME_BIND["ok"] = True
    log("game SimConnect exports bound path=%s (CameraSetRelative6DOF not used)" % path)
    return _GAME_BIND


def game_dll_status():
    info = load_game_simconnect_dll()
    return {
        "ok": bool(info.get("ok")),
        "path": info.get("path"),
        "missing": list(info.get("missing") or []),
    }


class GameSimConnect:
    """Dedicated ctypes client on the game SimConnect_internal.dll. Do not mix handles with Python-SimConnect."""

    def __init__(self):
        info = load_game_simconnect_dll()
        if not info.get("ok"):
            raise RuntimeError("chase unavailable")
        self.dll_path = info["path"]
        self.fns = info["fns"]
        self.handle = HANDLE()
        self._opened = False
        self._ok = False
        self._quit = False
        self._acquired = False
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._assigned = {}
        self._exceptions = []
        self._next_req = 1
        self._ghost_def = None
        self._closed = False
        self._att_unit = None
        self._cam_state = None
        self._cam_game = None

    def _h(self):
        return self.handle

    def new_request_id(self):
        with self._lock:
            rid = self._next_req
            self._next_req += 1
            return rid

    def open(self, timeout=CONNECT_TIMEOUT):
        try:
            hr = self.fns["SimConnect_Open"](
                byref(self.handle),
                LPCSTR(CAMERA_CLIENT_NAME),
                None,
                0,
                None,
                0,
            )
        except OSError as e:
            log("game SimConnect_Open OSError %s dll=%s" % (repr(e), self.dll_path))
            return False
        if not _is_hr(hr, 0):
            log("game SimConnect_Open failed hr=%s dll=%s" % (hr, self.dll_path))
            return False
        self._opened = True
        self._thread = threading.Thread(target=self._dispatch_loop, daemon=True, name="game-simconnect")
        self._thread.start()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._ok:
                return True
            if self._quit:
                break
            time.sleep(0.02)
        log("game SimConnect_Open produced no OPEN recv dll=%s" % self.dll_path)
        return False

    def _dispatch_loop(self):
        pp = ctypes.c_void_p()
        pcb = DWORD()
        while not self._stop.is_set() and self._opened:
            try:
                hr = self.fns["SimConnect_GetNextDispatch"](self._h(), byref(pp), byref(pcb))
            except Exception as e:
                log("GetNextDispatch exception %s" % repr(e))
                time.sleep(0.05)
                continue
            if _is_hr(hr, 0) and pp.value:
                try:
                    recv = cast(pp, POINTER(_SC_RECV)).contents
                    dwid = int(recv.dwID)
                    if dwid == SIMCONNECT_RECV_ID_OPEN:
                        self._ok = True
                    elif dwid == SIMCONNECT_RECV_ID_QUIT:
                        self._quit = True
                    elif dwid == SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID:
                        assigned = cast(pp, POINTER(_SC_RECV_ASSIGNED)).contents
                        with self._lock:
                            self._assigned[int(assigned.dwRequestID)] = int(assigned.dwObjectID)
                    elif dwid == SIMCONNECT_RECV_ID_SIMOBJECT_DATA:
                        smp = getattr(self, "state_sampler", None)
                        if smp is not None:
                            smp.on_simobject_data(
                                ctypes.string_at(pp, min(int(recv.dwSize), 4096)))
                    elif dwid == SIMCONNECT_RECV_ID_CAMERA:
                        n = min(int(recv.dwSize), 12 + CAMERA_STRUCT_SIZE)
                        blob = ctypes.string_at(pp, n)[12:]
                        if len(blob) >= CAMERA_STRUCT_SIZE:
                            with self._lock:
                                self._cam_struct = blob[:CAMERA_STRUCT_SIZE]
                    elif dwid == SIMCONNECT_RECV_ID_CAMERA_STATUS:
                        st = cast(pp, POINTER(_SC_RECV_CAMERA_STATUS)).contents
                        with self._lock:
                            self._cam_state = int(st.dwAcquiredState)
                            self._cam_game = bool(int(st.bGameControlled))
                        log(
                            "CAMERA_STATUS acquiredState=%s game_controlled=%s"
                            % (st.dwAcquiredState, int(st.bGameControlled))
                        )
                    elif dwid == SIMCONNECT_RECV_ID_EXCEPTION:
                        exc = cast(pp, POINTER(_SC_RECV_EXCEPTION)).contents
                        with self._lock:
                            self._exceptions.append(int(exc.dwException))
                        log("game simconnect exception=%s send=%s index=%s" % (exc.dwException, exc.dwSendID, exc.dwIndex))
                except Exception as e:
                    log("dispatch parse failed %s" % repr(e))
            time.sleep(0.002)

    def wait_object_id(self, request_id, timeout=4.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if request_id in self._assigned:
                    oid = self._assigned.pop(request_id)
                    if oid == USER_OBJECT_ID:
                        return None
                    return oid
            if self._quit:
                return None
            time.sleep(0.05)
        return None

    def _initpos(self, pt, force_airborne=False, zero_speed=False):
        """Build a SIMCONNECT_DATA_INITPOSITION for the ghost.

        force_airborne keeps OnGround at 0 while driving playback. With
        OnGround=1 the sim ignores the Altitude field and clamps the object to
        terrain: measured against a live ghost, the recorded 5045.4 ft became
        5043.9 ft the instant on_ground went true, and stayed pinned there
        while the recording kept descending. That is the snap-to-ground at
        touchdown. Spawning still uses the recorded flag, so an aircraft that
        starts on a runway is placed on it properly.
        """
        init = _SC_INITPOS()
        init.Latitude = float(pt.get("lat") or 0.0)
        init.Longitude = float(pt.get("lon") or 0.0)
        init.Altitude = float(pt.get("alt") or 0.0)
        init.Pitch = float(pt.get("pitch") or 0.0)
        init.Bank = float(pt.get("bank") or 0.0)
        init.Heading = pose_heading_deg(pt.get("heading"), self._att_unit)
        init.OnGround = 0 if force_airborne else (1 if pt.get("on_ground") else 0)
        if zero_speed:
            # Nothing for the sim to carry the object forward with between
            # writes. Playback sets the position every frame anyway, so the
            # airspeed field is only ever used against us.
            init.Airspeed = 0
        else:
            try:
                init.Airspeed = int(max(0, min(2000, float(pt.get("gs") or 0.0))))
            except Exception:
                init.Airspeed = 0
        return init

    def spawn_ghost(self, title, pt, livery=None):
        """Create the ghost, with the recorded livery when we have one.

        TITLE only names the variant ("AS365 N2 - VIP"); the livery is a
        separate string ("EI-PRO - Executive Helicopters"). The plain
        AICreateNonATCAircraft call takes no livery, so the sim picks one -
        which is why ghosts turned up in the wrong colors.
        """
        title_b = title.encode("utf-8", "replace")
        init = self._initpos(pt)
        oid = None
        ex1 = self.fns.get("SimConnect_AICreateNonATCAircraft_EX1")
        if ex1 and livery:
            rq0 = self.new_request_id()
            try:
                hr0 = ex1(self._h(), title_b, livery.encode("utf-8", "replace"),
                          b"GHOST", init, DWORD(rq0))
                if _is_hr(hr0, 0):
                    oid = self.wait_object_id(rq0, 4.0)
                else:
                    log("AICreateNonATCAircraft_EX1 hr=%s title=%s livery=%s"
                        % (hr0, title, livery))
            except Exception as e:
                log("AICreateNonATCAircraft_EX1 exception %s" % repr(e))
            if oid is not None and int(oid) != USER_OBJECT_ID:
                return oid
            log("livery spawn did not take, falling back title=%s" % title)
        rq = self.new_request_id()
        try:
            hr = self.fns["SimConnect_AICreateNonATCAircraft"](
                self._h(), title_b, b"GHOST", init, DWORD(rq)
            )
            if not _is_hr(hr, 0):
                log("AICreateNonATCAircraft hr=%s title=%s" % (hr, title))
        except Exception as e:
            log("AICreateNonATCAircraft exception %s title=%s" % (repr(e), title))
        oid = self.wait_object_id(rq, 4.0)
        if oid is None and "SimConnect_AICreateSimulatedObject" in self.fns:
            rq2 = self.new_request_id()
            try:
                hr2 = self.fns["SimConnect_AICreateSimulatedObject"](
                    self._h(), title_b, init, DWORD(rq2)
                )
                if not _is_hr(hr2, 0):
                    log("AICreateSimulatedObject hr=%s title=%s" % (hr2, title))
            except Exception as e:
                log("AICreateSimulatedObject exception %s title=%s" % (repr(e), title))
            oid = self.wait_object_id(rq2, 4.0)
        if oid is None or int(oid) == USER_OBJECT_ID:
            return None
        if "SimConnect_AIReleaseControl" in self.fns:
            try:
                rq3 = self.new_request_id()
                self.fns["SimConnect_AIReleaseControl"](self._h(), DWORD(int(oid)), DWORD(rq3))
            except Exception as e:
                log("AIReleaseControl failed %s" % repr(e))
        return int(oid)

    def ensure_ghost_def(self):
        if self._ghost_def is not None:
            return self._ghost_def
        def_id = 0
        hr = self.fns["SimConnect_AddToDataDefinition"](
            self._h(),
            DWORD(def_id),
            b"Initial Position",
            b"",
            DWORD(SIMCONNECT_DATATYPE_INITPOSITION),
            ctypes.c_float(0.0),
            DWORD(SIMCONNECT_UNUSED),
        )
        if not _is_hr(hr, 0):
            raise RuntimeError("AddToDataDefinition failed hr=%s" % hr)
        self._ghost_def = def_id
        return def_id

    def ensure_gear_def(self):
        """Data definition for commanding the ghost's gear handle."""
        if getattr(self, "_gear_def", None) is not None:
            return self._gear_def
        def_id = 3
        hr = self.fns["SimConnect_AddToDataDefinition"](
            self._h(),
            DWORD(def_id),
            b"GEAR HANDLE POSITION",
            b"percent",
            DWORD(SIMCONNECT_DATATYPE_FLOAT64),
            ctypes.c_float(0.0),
            DWORD(SIMCONNECT_UNUSED),
        )
        if not _is_hr(hr, 0):
            raise RuntimeError("AddToDataDefinition gear failed hr=%s" % hr)
        self._gear_def = def_id
        return def_id

    def ensure_freeze_events(self):
        """Map the freeze events once per connection."""
        got = getattr(self, "_freeze_ids", None)
        if got is not None:
            return got
        got = []
        mape = self.fns.get("SimConnect_MapClientEventToSimEvent")
        if mape and self.fns.get("SimConnect_TransmitClientEvent"):
            for name, eid in FREEZE_EVENTS:
                try:
                    hr = mape(self._h(), DWORD(eid), name.encode("ascii"))
                except Exception as e:
                    log("freeze map %s raised %s" % (name, repr(e)))
                    continue
                if _is_hr(hr, 0):
                    got.append((name, eid))
                else:
                    log("freeze map %s failed hr=%s" % (name, hr))
        self._freeze_ids = got
        return got

    def set_ghost_freeze(self, object_id, on):
        """Stop the sim flying the ghost between our position writes."""
        if object_id is None or int(object_id) == USER_OBJECT_ID:
            raise RuntimeError("refusing freeze on user aircraft")
        ids = self.ensure_freeze_events()
        if not ids:
            return False
        tx = self.fns["SimConnect_TransmitClientEvent"]
        ok = 0
        for name, eid in ids:
            try:
                hr = tx(self._h(), DWORD(int(object_id)), DWORD(eid),
                        DWORD(1 if on else 0), DWORD(0),
                        DWORD(SIMCONNECT_EVENT_FLAG_GROUPID_IS_PRIORITY))
            except Exception as e:
                log("freeze %s raised %s" % (name, repr(e)))
                continue
            if _is_hr(hr, 0):
                ok += 1
            else:
                log("freeze %s failed hr=%s" % (name, hr))
        return ok == len(ids)

    def ensure_flaps_def(self):
        """Data definition for commanding the ghost's flaps."""
        if getattr(self, "_flaps_def", None) is not None:
            return self._flaps_def
        def_id = 4
        for name in GHOST_FLAP_VARS:
            hr = self.fns["SimConnect_AddToDataDefinition"](
                self._h(), DWORD(def_id), name, b"percent",
                DWORD(SIMCONNECT_DATATYPE_FLOAT64), ctypes.c_float(0.0),
                DWORD(SIMCONNECT_UNUSED))
            if not _is_hr(hr, 0):
                raise RuntimeError("AddToDataDefinition %s failed hr=%s"
                                   % (name.decode("ascii"), hr))
        self._flaps_def = def_id
        return def_id

    def set_ghost_flaps(self, object_id, percent):
        """Replay the flaps as they were flown.

        Writes the surfaces as well as the handle. Writing the handle alone
        returned S_OK and moved nothing on a Cessna 152 replay: on a ghost the
        handle is a selection nothing acts on, and the visible flaps follow
        TRAILING EDGE FLAPS LEFT/RIGHT PERCENT. S_OK proving nothing is the
        same trap AddToDataDefinition sets by accepting any variable name.
        """
        if object_id is None or int(object_id) == USER_OBJECT_ID:
            raise RuntimeError("refusing SetData on user aircraft")
        def_id = self.ensure_flaps_def()
        pct = max(0.0, min(100.0, float(percent)))
        buf = (ctypes.c_double * len(GHOST_FLAP_VARS))(*([pct] * len(GHOST_FLAP_VARS)))
        hr = self.fns["SimConnect_SetDataOnSimObject"](
            self._h(), DWORD(def_id), DWORD(int(object_id)), DWORD(0), DWORD(0),
            DWORD(ctypes.sizeof(buf)), ctypes.cast(ctypes.pointer(buf), ctypes.c_void_p))
        if not _is_hr(hr, 0):
            raise RuntimeError("SetDataOnSimObject flaps failed hr=%s" % hr)

    def ensure_gear_surface_def(self):
        """Data definition for the gear positions that are actually drawn."""
        if getattr(self, "_gear_surface_def", None) is not None:
            return self._gear_surface_def
        def_id = 5
        for name in GHOST_GEAR_SURFACE_VARS:
            hr = self.fns["SimConnect_AddToDataDefinition"](
                self._h(), DWORD(def_id), name, b"percent",
                DWORD(SIMCONNECT_DATATYPE_FLOAT64), ctypes.c_float(0.0),
                DWORD(SIMCONNECT_UNUSED))
            if not _is_hr(hr, 0):
                raise RuntimeError("AddToDataDefinition %s failed hr=%s"
                                   % (name.decode("ascii"), hr))
        self._gear_surface_def = def_id
        return def_id

    def set_ghost_gear_surfaces(self, object_id, percent):
        """Pin the wheels themselves, whatever the handle ends up saying."""
        if object_id is None or int(object_id) == USER_OBJECT_ID:
            raise RuntimeError("refusing SetData on user aircraft")
        def_id = self.ensure_gear_surface_def()
        pct = max(0.0, min(100.0, float(percent)))
        buf = (ctypes.c_double * len(GHOST_GEAR_SURFACE_VARS))(
            *([pct] * len(GHOST_GEAR_SURFACE_VARS)))
        hr = self.fns["SimConnect_SetDataOnSimObject"](
            self._h(), DWORD(def_id), DWORD(int(object_id)), DWORD(0), DWORD(0),
            DWORD(ctypes.sizeof(buf)), ctypes.cast(ctypes.pointer(buf), ctypes.c_void_p))
        if not _is_hr(hr, 0):
            raise RuntimeError("SetDataOnSimObject gear surfaces failed hr=%s" % hr)

    def set_ghost_gear(self, object_id, percent):
        """Hold the ghost's gear where the recording had it.

        Playback lies to the sim about being airborne, so without this the sim
        retracts the gear and lands the aircraft on its belly. Takes a percent;
        True/False still work as 100/0.

        Writes the handle first, then the surfaces in a separate call. The
        surfaces are the half that survives the sim's AI moving the handle
        back; the handle is kept so anything reading the selection agrees with
        the wheels. A surface failure is reported once and does not cost the
        handle write, because on some airframes the handle alone is enough.
        """
        if object_id is None or int(object_id) == USER_OBJECT_ID:
            raise RuntimeError("refusing SetData on user aircraft")
        def_id = self.ensure_gear_def()
        if percent is True or percent is False:
            percent = 100.0 if percent else 0.0
        val = ctypes.c_double(max(0.0, min(100.0, float(percent))))
        hr = self.fns["SimConnect_SetDataOnSimObject"](
            self._h(),
            DWORD(def_id),
            DWORD(int(object_id)),
            DWORD(0),
            DWORD(0),
            DWORD(ctypes.sizeof(val)),
            ctypes.cast(ctypes.pointer(val), ctypes.c_void_p),
        )
        if not _is_hr(hr, 0):
            raise RuntimeError("SetDataOnSimObject gear failed hr=%s" % hr)
        try:
            self.set_ghost_gear_surfaces(object_id, percent)
        except Exception as e:
            if not getattr(self, "_gear_surface_warned", False):
                self._gear_surface_warned = True
                log("ghost gear surfaces unavailable, handle only: %r" % (e,))

    def set_ghost_pose(self, object_id, pt):
        if object_id is None or int(object_id) == USER_OBJECT_ID:
            raise RuntimeError("refusing SetData on user aircraft")
        def_id = self.ensure_ghost_def()
        init = self._initpos(pt, force_airborne=True, zero_speed=GHOST_ZERO_AIRSPEED)
        hr = self.fns["SimConnect_SetDataOnSimObject"](
            self._h(),
            DWORD(def_id),
            DWORD(int(object_id)),
            DWORD(0),
            DWORD(0),
            DWORD(sizeof(init)),
            ctypes.cast(ctypes.pointer(init), ctypes.c_void_p),
        )
        if not _is_hr(hr, 0):
            raise RuntimeError("SetDataOnSimObject ghost failed hr=%s" % hr)

    def remove_object(self, object_id):
        if object_id is None or int(object_id) == USER_OBJECT_ID:
            return
        if not self._opened or self._closed:
            return
        try:
            rq = self.new_request_id()
            self.fns["SimConnect_AIRemoveObject"](self._h(), DWORD(int(object_id)), DWORD(rq))
        except Exception as e:
            log("AIRemoveObject failed %s" % repr(e))

    def camera_get(self, timeout=1.5):
        """Ask the sim for the current camera; return its 84 raw bytes.

        This is the only way to know anything for certain here: CameraSet is
        fire-and-forget, so its S_OK says nothing about whether it worked.
        """
        fn = self.fns.get("SimConnect_CameraGet")
        if fn is None:
            return None
        with self._lock:
            self._cam_struct = None
        self._cam_req = getattr(self, "_cam_req", 9000) + 1
        try:
            fn(self._h(), DWORD(self._cam_req))
        except Exception as e:
            log("CameraGet exception %s" % repr(e))
            return None
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                blob = self._cam_struct
            if blob:
                return blob
            time.sleep(0.02)
        log("CameraGet timed out")
        return None

    def camera_baseline(self):
        """The sim's own camera struct, cached, used as our template.

        Starting from the sim's own bytes means every field we have not
        identified keeps a value the sim already considered valid.
        """
        base = getattr(self, "_cam_base", None)
        if base:
            return base
        base = self.camera_get()
        if base:
            self._cam_base = base
        return base

    def _smooth_heading(self, raw_hdg, pose):
        """Low-passed heading for camera placement.

        Below CAMERA_HEADING_MIN_GS the aircraft is hovering or still and its
        heading carries no information about where it is going, so the last
        good value is held rather than followed.
        """
        gs = as_float((pose or {}).get("gs")) or 0.0
        with RUNTIME_LOCK:
            prev = RUNTIME["replay"].get("cam_hdg")
        if prev is None:
            out = float(raw_hdg)
        elif gs < CAMERA_HEADING_MIN_GS:
            out = float(prev)
        else:
            delta = ((float(raw_hdg) - float(prev) + 180.0) % 360.0) - 180.0
            out = float(prev) + delta * CAMERA_HEADING_SMOOTH
        out = wrap_deg(out)
        with RUNTIME_LOCK:
            RUNTIME["replay"]["cam_hdg"] = out
        return out

    def build_chase_camera(self, object_id, pose=None, hold_anchor=False):
        """Put the camera at the event, in world coordinates.

        The camera cannot be attached to the ghost - the sim has no SimObject
        referential - so instead we compute a world position offset from the
        ghost's current lat/lon and point the camera back at it.
        """
        if object_id is None or int(object_id) == USER_OBJECT_ID:
            return None
        base = self.camera_baseline()
        if not base or len(base) < CAMERA_STRUCT_SIZE:
            log("no camera baseline from CameraGet; cannot build CameraSet")
            return None
        if pose is None:
            with RUNTIME_LOCK:
                pose = RUNTIME["replay"].get("last_pose")
        if not pose:
            return None
        lat = pose.get("lat")
        lon = pose.get("lon")
        alt_ft = pose.get("alt")
        if not (finite(lat) and finite(lon) and finite(alt_ft)):
            return None

        with RUNTIME_LOCK:
            ch = RUNTIME["replay"]["chase"]
            distance = float(ch["distance"])
            height = float(ch["height"])
            orbit_deg = float(ch["orbit"])
            referential = int(ch.get("referential", CAMERA_REFERENTIAL_DEFAULT))
            lock_bearing = ch.get("lock_bearing") or "aircraft"
            abs_bearing = ch.get("abs_bearing")

        alt_m = float(alt_ft) / FEET_PER_METER
        ghost_hdg = pose.get("heading")
        if not finite(ghost_hdg):
            ghost_hdg = 0.0

        anchor = None
        if hold_anchor:
            with RUNTIME_LOCK:
                anchor = RUNTIME["replay"].get("cam_anchor")
        if anchor:
            # Keep the shot where it was planted; only the aim changes.
            cam_lat, cam_lon, cam_alt = anchor
        else:
            if lock_bearing == "world" and abs_bearing is not None:
                # Hold the compass angle: the camera flies alongside and the
                # aircraft turns in front of it.
                bearing = wrap_deg(float(abs_bearing))
            else:
                # Sit behind the ghost, offset by the orbit knob, and look back.
                bearing = wrap_deg(self._smooth_heading(ghost_hdg, pose)
                                   + 180.0 + orbit_deg)
            cam_lat, cam_lon, cam_alt = offset_lla(
                float(lat), float(lon), alt_m, bearing, distance, height)
            with RUNTIME_LOCK:
                RUNTIME["replay"]["cam_anchor"] = (cam_lat, cam_lon, cam_alt)
        buf = bytearray(base)
        struct.pack_into("<3d", buf, CAM_OFF_POSITION, cam_lat, cam_lon, cam_alt)
        struct.pack_into("<I", buf, CAM_OFF_REFERENTIAL, referential & 0xFFFFFFFF)
        # Look at the ghost itself; the sim derives pitch/heading from this.
        struct.pack_into("<3d", buf, CAM_OFF_TARGET, float(lat), float(lon), alt_m)
        # Bank is only ever applied by a call carrying CAMERA_MASK_ROTATION,
        # which is what aim mode 'angles' sends. Under 'target' this write
        # is inert, which is exactly why that mode cannot level the horizon.
        pitch, heading = look_angles(cam_lat, cam_lon, cam_alt,
                                     float(lat), float(lon), alt_m)
        struct.pack_into("<f", buf, CAM_OFF_PITCH, pitch)
        struct.pack_into("<f", buf, CAM_OFF_BANK, 0.0)
        struct.pack_into("<f", buf, CAM_OFF_HEADING, heading)
        struct.pack_into("<I", buf, CAM_OFF_ROT_REFERENTIAL, referential & 0xFFFFFFFF)
        struct.pack_into("<d", buf, CAM_OFF_FOV, CHASE_FOV_RAD)
        return bytes(buf)

    def camera_set(self, object_id, pose=None, aim_only=False):
        if object_id is None or int(object_id) == USER_OBJECT_ID:
            log("refusing CameraSet on user aircraft")
            return False
        if not self._acquired:
            return False
        data = self.build_chase_camera(object_id, pose=pose, hold_anchor=aim_only)
        if data is None:
            return False
        # Each aspect needs its own call: bundling the masks applies the first
        # and silently drops the rest.
        fn = self.fns["SimConnect_CameraSet"]
        aim = (CAMERA_MASK_ROTATION_ONLY if CHASE_AIM_MODE == "angles"
               else CAMERA_MASK_TARGET_ONLY)
        masks = ((aim,) if aim_only else (CAMERA_MASK_POSITION_ONLY, aim))
        for mask in masks:
            try:
                hr = fn(self._h(), data, DWORD(mask))
            except Exception as e:
                log("CameraSet exception mask=%s %s" % (mask, repr(e)))
                return False
            if not _is_hr(hr, 0):
                log("CameraSet hr=%s mask=%s object_id=%s" % (hr, mask, object_id))
                return False
        return True

    def wait_camera_acquired(self, timeout=2.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                state = self._cam_state
                game = self._cam_game
            if state == CAMERA_ACQUIRED:
                return True, game
            time.sleep(0.02)
        with self._lock:
            return self._cam_state == CAMERA_ACQUIRED, self._cam_game

    def camera_acquire(self, object_id):
        if object_id is None or int(object_id) == USER_OBJECT_ID:
            log("refusing CameraAcquire for user aircraft id")
            self._acquired = False
            return False
        self._cam_state = None
        self._cam_game = None
        self._cam_struct = None
        self._cam_base = None
        try:
            # The name must be non-empty. Probing the live sim, every
            # non-empty name is granted (dwAcquiredState=1) and b"" is the only
            # one refused, which is why chase reported "acquired" but the sim
            # ignored every CameraSet that followed.
            hr = self.fns["SimConnect_CameraAcquire"](self._h(), CAMERA_CLIENT_NAME)
        except Exception as e:
            log("CameraAcquire exception %s" % repr(e))
            self._acquired = False
            return False
        if not _is_hr(hr, 0):
            log("CameraAcquire hr=%s" % hr)
            self._acquired = False
            return False
        if "SimConnect_CameraGetStatus" in self.fns:
            try:
                self.fns["SimConnect_CameraGetStatus"](self._h())
            except Exception as e:
                log("CameraGetStatus exception %s" % repr(e))
        ok, game = self.wait_camera_acquired(2.0)
        if not ok:
            log("CameraAcquire produced no ACQUIRED status; CameraSet may be ignored")
        if game:
            log("camera game_controlled=1; chase may stay on sim camera until sim releases")
        self._acquired = True
        if not self.camera_set(object_id):
            log("CameraSet after acquire failed; releasing camera")
            self.camera_release()
            return False
        # One decode of what the sim reports back, for the record.
        self.log_camera_struct("after acquire")
        # NOT enabling CAMERA_FLAG_INTERACTION: it hands the camera to the sim's
        # own controller, which immediately snaps the view back to the aircraft
        # and undoes the placement. It never gave a control pad any control
        # either, so it costs the whole feature and buys nothing.
        if CAMERA_ENABLE_INTERACTION:
            self.camera_enable_flag(CAMERA_FLAG_INTERACTION)
        log(
            "CameraAcquire ok object_id=%s dll=%s acquiredState=%s game_controlled=%s"
            % (object_id, self.dll_path, self._cam_state, self._cam_game)
        )
        return True

    def log_camera_struct(self, why):
        """Decode and log all 84 bytes under the assumed layout.

        The chase horizon is tilted while the field taken to be bank reads
        zero, so the layout itself is now the thing in question. Every
        plausible reading of the tail is logged rather than only the one
        the constants assert.
        """
        blob = self.camera_get()
        if not blob or len(blob) < CAMERA_STRUCT_SIZE:
            log("camera struct (%s): unavailable" % why)
            return None
        pos = struct.unpack_from("<3d", blob, 0)
        ref, unk28 = struct.unpack_from("<2I", blob, 24)
        tgt = struct.unpack_from("<3d", blob, 32)
        f56, f60, f64 = struct.unpack_from("<3f", blob, 56)
        rot_ref = struct.unpack_from("<I", blob, 68)[0]
        f72 = struct.unpack_from("<f", blob, 72)[0]
        fov = struct.unpack_from("<d", blob, 76)[0]
        log("camera struct (%s): pos=%.6f,%.6f,%.1f ref=%s unk28=%s"
            % (why, pos[0], pos[1], pos[2], ref, unk28))
        log("camera struct (%s): tgt=%.6f,%.6f,%.1f rot_ref=%s fov=%.4f"
            % (why, tgt[0], tgt[1], tgt[2], rot_ref, fov))
        log("camera struct (%s): f56=%.3f f60=%.3f f64=%.3f f72=%.3f"
            % (why, f56, f60, f64, f72))
        log("camera struct (%s): raw=%s" % (why, blob.hex()))
        return blob

    def camera_enable_flag(self, flags):
        """Let the sim's own controls drive the camera we placed."""
        fn = self.fns.get("SimConnect_CameraEnableFlag")
        if fn is None:
            log("CameraEnableFlag not bound; a control pad will not move the camera")
            return False
        try:
            hr = fn(self._h(), DWORD(int(flags)))
        except Exception as e:
            log("CameraEnableFlag exception %s" % repr(e))
            return False
        if not _is_hr(hr, 0):
            log("CameraEnableFlag hr=%s flags=%s" % (hr, flags))
            return False
        log("camera flags enabled: interaction+above_ground (%s)" % flags)
        return True

    def camera_release(self):
        if not self._acquired or not self._opened or self._closed:
            self._acquired = False
            return
        self._acquired = False
        try:
            # Release the same named camera we acquired.
            hr = self.fns["SimConnect_CameraRelease"](self._h(), CAMERA_CLIENT_NAME)
            if not _is_hr(hr, 0):
                log("CameraRelease hr=%s" % hr)
            else:
                log("CameraRelease ok")
        except Exception as e:
            log("CameraRelease failed %s" % repr(e))

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread is not None and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None
        if self._opened:
            try:
                self.fns["SimConnect_Close"](self._h())
            except Exception as e:
                log("SimConnect_Close failed %s" % repr(e))
        self._opened = False
        self._ok = False
        self._acquired = False


def look_angles(cam_lat, cam_lon, cam_alt_m, tgt_lat, tgt_lon, tgt_alt_m):
    """Pitch and heading that point a camera at a target.

    Deliberately the same flat-earth approximation offset_lla uses, so the
    angles agree exactly with the placement rather than disagreeing by a
    little at the edges of a long arm.

    Pitch is POSITIVE DOWNWARD, which is the sim's convention and the trap
    that had an earlier attempt looking 18 degrees the wrong way.
    """
    north = math.radians(tgt_lat - cam_lat) * EARTH_RADIUS_M
    coslat = max(math.cos(math.radians(cam_lat)), 1e-6)
    east = math.radians(tgt_lon - cam_lon) * EARTH_RADIUS_M * coslat
    horiz = math.hypot(north, east)
    heading = wrap_deg(math.degrees(math.atan2(east, north)))
    pitch = math.degrees(math.atan2(cam_alt_m - tgt_alt_m, max(horiz, 1e-6)))
    return pitch, heading


def offset_lla(lat, lon, alt_m, bearing_deg, dist_m, up_m):
    """Move dist_m along a compass bearing from (lat, lon), and up_m upward."""
    br = math.radians(bearing_deg)
    dlat = (dist_m * math.cos(br)) / EARTH_RADIUS_M
    coslat = max(math.cos(math.radians(lat)), 1e-6)
    dlon = (dist_m * math.sin(br)) / (EARTH_RADIUS_M * coslat)
    return (lat + math.degrees(dlat), lon + math.degrees(dlon), alt_m + up_m)


def open_state_sampler():
    """A dedicated connection that the sim pushes state into.

    Separate from the replay connection so a replay teardown can never take
    the recording path down with it.
    """
    if SAMPLER_MODE == "legacy":
        return None, None
    info = game_dll_status()
    if not info.get("ok"):
        log("sampler: game DLL unavailable, staying on the legacy path")
        return None, None
    try:
        import sampler as sampler_mod
    except Exception as e:
        log("sampler: import failed %r" % (e,))
        return None, None
    gsc = GameSimConnect()
    try:
        if not gsc.open():
            log("sampler: SimConnect open failed, staying on the legacy path")
            gsc.close()
            return None, None
        smp = sampler_mod.StateSampler(gsc.fns, gsc._h, log=log)
        gsc.state_sampler = smp
        if not smp.subscribe():
            gsc.close()
            return None, None
        return gsc, smp
    except Exception as e:
        log("sampler: setup failed %r" % (e,))
        try:
            gsc.close()
        except Exception:
            pass
        return None, None


def merge_peaks(into, new):
    """Widen an accumulator with a freshly read window.

    The sampler hands over its extremes and starts again on every read, so a
    reader that samples faster than it records has to keep the running worst
    itself or it writes whichever tenth of a second it happened to land on.
    """
    if not new:
        return into
    for k, v in new.items():
        cur = into.get(k)
        if cur is None:
            into[k] = v
        elif k.endswith("_max"):
            if v > cur:
                into[k] = v
        elif v < cur:
            into[k] = v
    return into


def sample_from_state(st):
    """Shape a pushed state like sample() does.

    The pushed values already carry the units we asked for, so unlike the
    legacy path there is no radian conversion here.
    """
    if not st:
        return None
    # Timestamp the sample when the SIM produced it, not when we got
    # round to shaping it. States are pushed on sim frames and read on a
    # slower poll, so "now" is 0 to 33 ms later than the position by a
    # random amount - which writes that error straight into the track as
    # position noise. Measured at up to 1.7 m per sample at 50 m/s: steps
    # between recorded points varied 0.52x to 1.35x of the distance the
    # sim's own groundspeed says was covered, while the timestamps were
    # regular to 0.1%. No interpolation or update rate can remove that.
    return {
        "t": as_float(st.get("at")) or time.time(),
        "ts": now_iso(),
        "aircraft": st.get("aircraft") or None,
        "livery": st.get("livery") or None,
        "lat": as_float(st.get("lat")),
        "lon": as_float(st.get("lon")),
        "alt": as_float(st.get("alt")),
        "vs": as_float(st.get("vs")),
        "gs": as_float(st.get("gs")),
        "heading": wrap_deg(as_float(st.get("heading")) or 0.0),
        "airspeed": as_float(st.get("airspeed")),
        "on_ground": bool(st.get("on_ground")),
        "touchdown_fps": as_float(st.get("touchdown_fps")),
        "pitch": as_float(st.get("pitch")),
        "bank": as_float(st.get("bank")),
        "slew": bool(st.get("slew")),
        "camera_state": st.get("camera_state"),
        # Gear handle as flown. Playback replays this rather than guessing at
        # an altitude, so it is right for any airframe.
        # What the sim calls this aircraft, and its stall speed. Together
        # they decide which grading profile a flight is judged on, so they
        # are recorded rather than guessed from the title afterwards.
        "category": st.get("category") or None,
        "vs0": as_float(st.get("vs0")),
        # Measured, where grading used to differentiate or approximate.
        "agl": as_float(st.get("agl")),
        "gforce": as_float(st.get("gforce")),
        "accel_x": as_float(st.get("accel_x")),
        "accel_y": as_float(st.get("accel_y")),
        "accel_z": as_float(st.get("accel_z")),
        # Extremes since the sampler was last read, which is ten times per
        # recorded point. The loop merges these until a point is written.
        "peaks": st.get("peaks") or {},
        "gear": as_float(st.get("gear")),
        "flaps": as_float(st.get("flaps")),
        "spoilers": as_float(st.get("spoilers")),
    }


# Fields worth comparing, and how far apart the two paths may drift before it
# is worth saying so. They are sampled a moment apart, so a moving aircraft
# will never agree exactly; these are set to catch unit and layout mistakes
# (radians vs degrees, a shifted struct), not small timing differences.
SAMPLER_COMPARE_FIELDS = (
    ("lat", 0.001), ("lon", 0.001), ("alt", 50.0), ("heading", 5.0),
    ("pitch", 3.0), ("bank", 3.0), ("gs", 10.0), ("airspeed", 10.0),
)


def compare_samples(legacy, fast):
    """Return a list of (field, legacy, fast, delta) that disagree."""
    bad = []
    if not legacy or not fast:
        return bad
    for key, tol in SAMPLER_COMPARE_FIELDS:
        a, b = legacy.get(key), fast.get(key)
        if a is None or b is None:
            continue
        if key == "heading":
            d = abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)
        else:
            d = abs(float(a) - float(b))
        if d > tol:
            bad.append((key, float(a), float(b), d))
    if bool(legacy.get("on_ground")) != bool(fast.get("on_ground")):
        bad.append(("on_ground", legacy.get("on_ground"), fast.get("on_ground"), 1))
    return bad


def _ghost_titles(preferred):
    titles = []
    if preferred:
        titles.append(preferred)
    for t in (
        "AS365 N2 - VIP",
        "Airbus H125",
        "Bell 407 GX",
        "Cessna Skyhawk G1000",
        "Cessna 172 SP",
    ):
        if t not in titles:
            titles.append(t)
    return titles


def _chase_snapshot():
    with RUNTIME_LOCK:
        ch = RUNTIME["replay"]["chase"]
        return {
            "distance": float(ch["distance"]),
            "height": float(ch["height"]),
            "orbit": float(ch["orbit"]),
            "camera_acquired": bool(ch["camera_acquired"]),
            "mode": ch.get("mode") or CHASE_MODE_DEFAULT,
            "locked": (ch.get("mode") == CHASE_MODE_FOLLOW),
            "lock_bearing": ch.get("lock_bearing") or "aircraft",
            "referential": int(ch.get("referential", CAMERA_REFERENTIAL_DEFAULT)),
        }


def _clamp_chase_num(v, lo, hi):
    x = as_float(v)
    if x is None:
        return None
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _teardown_game_replay(gsc, object_id):
    if gsc is None:
        return
    try:
        gsc.camera_release()
    except Exception:
        pass
    try:
        gsc.remove_object(object_id)
    except Exception:
        pass
    try:
        gsc.close()
    except Exception:
        pass
    with RUNTIME_LOCK:
        if RUNTIME["replay"].get("gsc") is gsc:
            RUNTIME["replay"]["gsc"] = None
        RUNTIME["replay"]["chase"]["camera_acquired"] = False


def update_chase(body):
    body = body or {}
    with RUNTIME_LOCK:
        ch = RUNTIME["replay"]["chase"]
        d = _clamp_chase_num(body.get("distance"), 1.0, 500.0)
        h = _clamp_chase_num(body.get("height"), -50.0, 200.0)
        o = _clamp_chase_num(body.get("orbit"), -360.0, 720.0)
        if d is not None:
            ch["distance"] = d
        if h is not None:
            ch["height"] = h
        if o is not None:
            ch["orbit"] = o
        mode = body.get("mode")
        if mode in CHASE_MODES:
            ch["mode"] = mode
        if any(k in body for k in ("distance", "height", "orbit")):
            RUNTIME["replay"]["cam_anchor"] = None   # knobs move the tripod
        ref = as_float(body.get("referential"))
        if ref is not None and 0 <= int(ref) <= 15:
            ch["referential"] = int(ref)
        gsc = RUNTIME["replay"].get("gsc")
        oid = RUNTIME["replay"]["object_id"]
        active = bool(RUNTIME["replay"]["active"])
        acquired = bool(ch["camera_acquired"])
    info = game_dll_status()
    if not info.get("ok"):
        return {"ok": False, "error": "chase unavailable", "chase": _chase_snapshot()}
    # In place mode this single CameraSet is the whole point: it moves the
    # camera to the new offset and then leaves it alone again.
    if active and acquired and oid not in (None, USER_OBJECT_ID):
        if gsc is None:
            return {"ok": False, "error": "chase unavailable", "chase": _chase_snapshot()}
        if not gsc.camera_set(oid):
            # Do NOT release. This used to, and releasing is exactly what
            # hands the view back to the user's aircraft - so one nudge of the
            # orbit slider ended the shot, both paused and running, reported
            # from the sim.
            #
            # camera_set returns False for transient reasons: CameraGet has to
            # answer for a baseline before a CameraSet can be built, and a
            # round trip that times out under a slider being dragged is enough.
            # The camera is still acquired and the chase loop writes it every
            # pass, so the next one corrects it. Treating a momentary failure
            # as a lost camera turned it into a permanent one.
            log("chase knob: CameraSet did not take, camera kept")
            return {"ok": False, "error": "could not move the camera just now",
                    "kept": True, "chase": _chase_snapshot()}
    return {"ok": True, "chase": _chase_snapshot()}


def set_replay_paused(want=None):
    """Pause, resume, or toggle. Returns the new state."""
    with RUNTIME_LOCK:
        active = bool(RUNTIME["replay"]["active"])
        if not active:
            return {"ok": False, "error": "no replay running",
                    "paused": bool(RUNTIME["replay"].get("paused"))}
        cur = bool(RUNTIME["replay"].get("paused"))
        new = (not cur) if want is None else bool(want)
        RUNTIME["replay"]["paused"] = new
        clip = RUNTIME["replay"].get("clip_id")
    return {"ok": True, "paused": new, "clip_id": clip}


def lock_camera(locked=True, bearing_mode=None):
    """Hold the current vantage as the ghost moves, or let it go again.

    Locking reads the geometry that is on screen - how far the camera sits from
    the ghost, how high, and from which side - and keeps it, so the shot you
    set up is the shot you keep instead of one the ghost flies out of.
    """
    with RUNTIME_LOCK:
        gsc = RUNTIME["replay"].get("gsc")
        oid = RUNTIME["replay"]["object_id"]
        active = bool(RUNTIME["replay"]["active"])
        anchor = RUNTIME["replay"].get("cam_anchor")
        pose = RUNTIME["replay"].get("last_pose")
        ch = RUNTIME["replay"]["chase"]
        if bearing_mode in ("aircraft", "world"):
            ch["lock_bearing"] = bearing_mode
        mode_now = ch.get("lock_bearing") or "aircraft"

    if not locked:
        with RUNTIME_LOCK:
            RUNTIME["replay"]["chase"]["mode"] = CHASE_MODE_PLACE
            RUNTIME["replay"]["cam_anchor"] = None
        log("camera unlocked; it stays where it is")
        return {"ok": True, "chase": _chase_snapshot()}

    if not active or oid in (None, USER_OBJECT_ID):
        with RUNTIME_LOCK:
            RUNTIME["replay"]["chase"]["mode"] = CHASE_MODE_FOLLOW
        return {"ok": True, "chase": _chase_snapshot(),
                "note": "will lock when a replay starts"}

    if anchor and pose and finite(pose.get("lat")) and finite(pose.get("lon")):
        cam_lat, cam_lon, cam_alt_m = anchor
        g_lat = float(pose["lat"])
        g_lon = float(pose["lon"])
        g_alt_m = (as_float(pose.get("alt")) or 0.0) / FEET_PER_METER
        g_hdg = as_float(pose.get("heading"))
        if not finite(g_hdg):
            g_hdg = 0.0
        coslat = max(math.cos(math.radians(g_lat)), 1e-6)
        dn = (cam_lat - g_lat) * 111320.0
        de = (cam_lon - g_lon) * 111320.0 * coslat
        dist = math.hypot(dn, de)
        bearing = math.degrees(math.atan2(de, dn)) % 360.0
        with RUNTIME_LOCK:
            ch = RUNTIME["replay"]["chase"]
            ch["distance"] = max(1.0, min(500.0, dist))
            ch["height"] = max(-50.0, min(200.0, cam_alt_m - g_alt_m))
            ch["orbit"] = wrap_deg(bearing - float(g_hdg) - 180.0)
            ch["abs_bearing"] = bearing
            ch["mode"] = CHASE_MODE_FOLLOW
            RUNTIME["replay"]["cam_hdg"] = float(g_hdg)
        log("camera locked to the ghost: %.0f m out, %.0f m up, bearing %.0f (%s)"
            % (dist, cam_alt_m - g_alt_m, bearing, mode_now))
    else:
        with RUNTIME_LOCK:
            RUNTIME["replay"]["chase"]["mode"] = CHASE_MODE_FOLLOW
        log("camera locked using the current distance/height/orbit")

    if gsc is not None and oid not in (None, USER_OBJECT_ID):
        gsc.camera_set(oid)
    return {"ok": True, "chase": _chase_snapshot()}


def recenter_camera():
    """Put the camera back on the ghost, once. For place mode."""
    with RUNTIME_LOCK:
        gsc = RUNTIME["replay"].get("gsc")
        oid = RUNTIME["replay"]["object_id"]
        active = bool(RUNTIME["replay"]["active"])
        acquired = bool(RUNTIME["replay"]["chase"]["camera_acquired"])
        RUNTIME["replay"]["chase"]["recenter"] += 1
        RUNTIME["replay"]["cam_anchor"] = None      # re-plant on the ghost
    if not active or not acquired or gsc is None or oid in (None, USER_OBJECT_ID):
        return {"ok": False, "error": "no replay running", "chase": _chase_snapshot()}
    if not gsc.camera_set(oid):
        # Never released the camera, so this was not the orbit-slider bug -
        # but it reported a transient miss as "chase unavailable", which reads
        # as the camera being gone when it is still acquired and the loop is
        # still driving it.
        log("recenter: CameraSet did not take, camera kept")
        return {"ok": False, "error": "could not move the camera just now",
                "kept": True, "chase": _chase_snapshot()}
    return {"ok": True, "chase": _chase_snapshot()}


def replay_in_progress():
    """True while a ghost is in the world, playing or held at the end.

    Holding counts: the ghost is still spawned and still sitting on the runway
    after the clip finishes, so it can still perturb the user aircraft.
    """
    try:
        rp = RUNTIME["replay"]
        return bool(rp.get("active") or rp.get("holding"))
    except Exception:
        return False


def _gear_target(pt, ground_alt):
    """Percent the ghost's gear handle should be at.

    Prefer what was recorded. It is what the pilot actually did, it needs no
    per-airframe tuning, and it is correct for a fixed-gear trainer and a jet
    alike. GEAR_DOWN_AGL_FT is only a fallback for clips written before gear
    was captured - guessing at a height was never the right mechanism, just the
    only thing available for clips that already existed.
    """
    if GHOST_GEAR_FROM_RECORDING:
        g = as_float((pt or {}).get("gear"))
        if g is not None:
            return max(0.0, min(100.0, g))
    if ground_alt is None:
        return None
    alt = as_float((pt or {}).get("alt"))
    if alt is None or (alt - ground_alt) < GEAR_DOWN_AGL_FT:
        return 100.0
    return 0.0


def _ground_lifted(pt, ground_alt):
    """Raise the ghost off the surface by the gear compression it is missing.

    Full lift at and below the clip's touchdown altitude, fading out over
    GHOST_GROUND_LIFT_BLEND_FT so there is no step as it climbs away.
    """
    if not pt or ground_alt is None or GHOST_GROUND_LIFT_FT <= 0:
        return pt
    alt = as_float(pt.get("alt"))
    if alt is None:
        return pt
    agl = alt - ground_alt
    if agl >= GHOST_GROUND_LIFT_BLEND_FT:
        return pt
    if agl <= 0.0:
        lift = GHOST_GROUND_LIFT_FT
    else:
        lift = GHOST_GROUND_LIFT_FT * (1.0 - agl / GHOST_GROUND_LIFT_BLEND_FT)
    out = dict(pt)
    out["alt"] = alt + lift
    return out


def _clip_ground_alt(points):
    """Altitude the clip touches the ground at, for deciding gear position.

    Prefers samples the recording marked on-ground; falls back to the lowest
    point in the clip, which for a takeoff or landing window is the same place.
    """
    on = [as_float(p.get("alt")) for p in points if p.get("on_ground")]
    on = [a for a in on if a is not None]
    if on:
        return min(on)
    alts = [as_float(p.get("alt")) for p in points]
    alts = [a for a in alts if a is not None]
    return min(alts) if alts else None


def _replay_loop(gsc, object_id, points, stop_ev, clip_id, ts=None):
    # One index for the whole run, built from the list this loop was handed.
    # points is immutable for the life of a replay - see clip_timestamps.
    if ts is None:
        ts = clip_timestamps(points)
    chase_live = True
    try:
        if not points:
            return
        t_base = points[0].get("t") or 0.0
        t_end = points[-1].get("t") or t_base
        wall0 = time.time()
        # The loop runs at the faster of the two rates and writes the ghost
        # on its own slower cadence, so raising the camera rate does not
        # change how often the ghost is touched.
        step = 1.0 / max(REPLAY_HZ, CHASE_HZ)
        # Deadlines run on a fixed grid from wall0. Re-basing each one on
        # the iteration's own start time never corrects the sleep overshoot,
        # so it compounds: 11.11 ms asked plus ~0.5 ms overshoot gave 86 Hz
        # instead of 90, measured. Against a 90 Hz headset that beats about
        # four times a second, which looks exactly like a small skip.
        tick = 0.0
        rate_t0 = time.time()
        rate_n = 0
        rate_work = 0.0
        rate_late = 0
        ghost_step = 1.0 / REPLAY_HZ
        next_ghost = 0.0
        log("replay loop start: %d points t_base=%.3f t_end=%.3f span=%.1fs"
            % (len(points), t_base, t_end, t_end - t_base))
        paused_since = None
        last_pt = None
        ground_alt = _clip_ground_alt(points)
        last_pt = _ground_lifted(points[0], ground_alt)
        gear_cmd = None
        gear_sent = 0.0
        flaps_cmd = None
        flaps_sent = 0.0
        cam_fails = 0
        while not stop_ev.is_set():
            if object_id is None or int(object_id) == USER_OBJECT_ID:
                log("replay abort: user object id")
                break
            with RUNTIME_LOCK:
                paused = bool(RUNTIME["replay"].get("paused"))
            if paused:
                # Hold the clip where it is so the knobs can be worked in peace.
                #
                # The ghost is an AI aircraft with an airspeed, and control was
                # released to the sim - we only overwrite its position 30 times
                # a second, which is what normally hides its own flying. Simply
                # not writing to it during a pause let the sim fly it on, so
                # keep pinning it to the frozen pose, at zero speed.
                if paused_since is None:
                    paused_since = time.time()
                    log("replay paused clip=%s" % clip_id)
                if last_pt is not None:
                    frozen = dict(last_pt)
                    frozen["gs"] = 0.0
                    try:
                        gsc.set_ghost_pose(object_id, frozen)
                    except Exception as e:
                        log("ghost hold failed %s" % repr(e))
                # A replay opens held, so this is where the shot gets set up -
                # possibly for a while. Keep refreshing the gear or the sim's
                # own AI logic winds it back up while we sit here.
                # Written after every pose, not on a timer. The ghost sits at
                # a few hundred feet AGL at the start of a landing clip, which
                # is where the sim's own AI logic puts an aircraft into landing
                # configuration - so it extends the gear underneath us. On a
                # timer it got several frames between our commands to do it;
                # writing every pass leaves it none.
                want_h = _gear_target(last_pt, ground_alt)
                if want_h is not None:
                    try:
                        gsc.set_ghost_gear(object_id, want_h)
                        gear_cmd = want_h
                        gear_sent = time.time()
                    except Exception as e:
                        log("ghost gear hold failed %s" % repr(e))
                        ground_alt = None
                # And the flaps, for exactly the reason the gear is held: the
                # start of a landing clip sits at a few hundred feet AGL,
                # which is where the sim's own AI puts an aircraft into
                # landing configuration. A replay opens paused, so without
                # this the flaps drift to whatever the AI wants while the shot
                # is being set up, and the first thing playback shows is the
                # wrong configuration.
                flaps_hold = as_float(last_pt.get("flaps")) if last_pt else None
                if flaps_hold is not None:
                    try:
                        gsc.set_ghost_flaps(object_id, flaps_hold)
                        flaps_cmd = flaps_hold
                        flaps_sent = time.time()
                    except Exception as e:
                        log("ghost flaps hold failed %s" % repr(e))
                        flaps_cmd = None
                if stop_ev.wait(0.1):
                    break
                continue
            if paused_since is not None:
                # Playback time is wall-clock based, so give back the time spent
                # paused instead of jumping forward on resume.
                paused = time.time() - paused_since
                wall0 += paused
                # The deadline grid is relative to wall0, so it moves too.
                tick = time.time() - wall0
                paused_since = None
                log("replay resumed clip=%s" % clip_id)
            elapsed = time.time() - wall0
            t = t_base + elapsed
            if t > t_end + 0.06:
                log("replay loop: reached end t=%.3f t_end=%.3f after %.2fs" % (t, t_end, elapsed))
                break
            pt = interpolate_pose(points, min(t, t_end), ts)
            if pt is None:
                log("replay loop: interpolate_pose returned None at t=%.3f (t_base=%.3f t_end=%.3f)"
                    % (t, t_base, t_end))
                break
            pt = _ground_lifted(pt, ground_alt)
            last_pt = pt
            span = max(t_end - t_base, 1e-6)
            with RUNTIME_LOCK:
                RUNTIME["replay"]["last_pose"] = pt
                RUNTIME["replay"]["elapsed_s"] = max(0.0, min(t - t_base, span))
                RUNTIME["replay"]["duration_s"] = span
            # Ghost writes stay at REPLAY_HZ even when the camera runs
            # faster. The pose is still interpolated for this instant, so
            # the camera aims at where the ghost actually is.
            if elapsed >= next_ghost:
                # Advance the deadline by a fixed step rather than from
                # elapsed, which has already overshot it: re-basing on
                # elapsed drifts and cost 3 ghost writes a second at
                # CHASE_HZ 60 (27/s instead of 30/s).
                next_ghost += ghost_step
                if next_ghost <= elapsed:
                    # Fell behind - resync instead of firing a burst.
                    next_ghost = elapsed + ghost_step
                try:
                    gsc.set_ghost_pose(object_id, pt)
                except Exception as e:
                    log("ghost pose failed %s" % repr(e))
                    break
            flaps_want = as_float(pt.get("flaps"))
            if flaps_want is not None:
                now_f = time.time()
                if flaps_want != flaps_cmd or now_f - flaps_sent > GEAR_REFRESH_SEC:
                    try:
                        gsc.set_ghost_flaps(object_id, flaps_want)
                        flaps_cmd = flaps_want
                        flaps_sent = now_f
                    except Exception as e:
                        log("ghost flaps failed %s" % repr(e))
                        flaps_cmd = None
            want = _gear_target(pt, ground_alt)
            if want is not None:
                # Every frame, for the same reason as the hold above: the sim's
                # AI reconfigures the aircraft on its own and a timer leaves it
                # gaps to do so. One SetData costs about 0.01 ms.
                try:
                    gsc.set_ghost_gear(object_id, want)
                    if want != gear_cmd:
                        log("ghost gear %.0f%%%s" % (
                            want, "" if pt.get("gear") is not None
                            else " (no gear recorded; from altitude)"))
                    gear_cmd = want
                    gear_sent = time.time()
                except Exception as e:
                    log("ghost gear failed %s" % repr(e))
                    ground_alt = None
            with RUNTIME_LOCK:
                mode = RUNTIME["replay"]["chase"].get("mode") or CHASE_MODE_DEFAULT
            # Place mode holds the camera still and only re-aims, so the shot
            # stays at the event while the ghost flies through it. Follow mode
            # moves the camera as well.
            if chase_live:
                try:
                    aimed = gsc.camera_set(object_id, pose=pt,
                                           aim_only=(mode != CHASE_MODE_FOLLOW))
                except Exception as e:
                    # Never let a camera bug kill the replay thread silently.
                    log("camera update raised, chase off, ghost continues: %r" % (e,))
                    aimed = False
                if aimed:
                    cam_fails = 0
                else:
                    # Transient failures are expected and recoverable: the next
                    # pass rewrites the camera a few milliseconds later. Only
                    # give up when they do not stop.
                    cam_fails += 1
                    if cam_fails == 1:
                        log("CameraSet failed during replay; retrying")
                    if cam_fails >= CHASE_FAIL_TOLERANCE:
                        with RUNTIME_LOCK:
                            still = bool(RUNTIME["replay"]["chase"]["camera_acquired"])
                        if still:
                            log("CameraSet failed %d times running; chase "
                                "stopped, ghost continues" % cam_fails)
                            gsc.camera_release()
                            with RUNTIME_LOCK:
                                RUNTIME["replay"]["chase"]["camera_acquired"] = False
                        chase_live = False
            tick += step
            nxt = wall0 + tick
            if nxt <= time.time():
                # Genuinely behind rather than merely overshooting: resync to
                # now instead of firing a burst to catch up.
                tick = time.time() - wall0 + step
                nxt = wall0 + tick
            if REPLAY_RATE_LOG:
                # Work time is everything above; late means the deadline had
                # already passed before we got here, so the loop is the
                # limit rather than the requested rate.
                now_r = time.time()
                rate_n += 1
                rate_work += now_r - (wall0 + elapsed)
                if now_r >= nxt:
                    rate_late += 1
                if now_r - rate_t0 >= 1.0:
                    log("replay rate: asked %.0f Hz, got %.1f Hz, work %.2f ms/pass, %d/%d late"
                        % (1.0 / step, rate_n / (now_r - rate_t0),
                           (rate_work / max(rate_n, 1)) * 1000.0, rate_late, rate_n))
                    rate_t0, rate_n, rate_work, rate_late = now_r, 0, 0.0, 0
            while not stop_ev.is_set():
                left = nxt - time.time()
                if left <= 0:
                    break
                time.sleep(min(left, REPLAY_SLEEP_SLICE))
    finally:
        if stop_ev.is_set():
            # Stop was pressed: hand the camera back and clean up the ghost.
            try:
                gsc.camera_release()
            except Exception:
                pass
            try:
                gsc.remove_object(object_id)
            except Exception:
                pass
            try:
                gsc.close()
            except Exception:
                pass
            with RUNTIME_LOCK:
                if RUNTIME["replay"].get("clip_id") == clip_id:
                    RUNTIME["replay"]["active"] = False
                    RUNTIME["replay"]["object_id"] = None
                if RUNTIME["replay"].get("gsc") is gsc:
                    RUNTIME["replay"]["gsc"] = None
                RUNTIME["replay"]["chase"]["camera_acquired"] = False
                RUNTIME["replay"]["holding"] = False
            log("replay stopped clip=%s" % clip_id)
        else:
            # The clip simply ran out. Leave the camera where it is and the
            # ghost where it finished, until Stop. stop_replay() does the
            # teardown, and starting another replay calls it first.
            with RUNTIME_LOCK:
                if RUNTIME["replay"].get("clip_id") == clip_id:
                    RUNTIME["replay"]["active"] = False
                    RUNTIME["replay"]["holding"] = True
                    RUNTIME["replay"]["paused"] = False
            log("replay finished clip=%s; camera held at the event until Stop" % clip_id)
            # "Held" was only ever a flag. The camera stays where we put it
            # solely because the loop above re-asserts it every frame, so once
            # the loop exited the sim took the view back to the user aircraft
            # - the opposite of what the message claimed. Keep asserting it,
            # slowly, until Stop.
            hold_pose = last_pt
            while chase_live and hold_pose is not None and not stop_ev.is_set():
                with RUNTIME_LOCK:
                    hold_mode = RUNTIME["replay"]["chase"].get("mode") or CHASE_MODE_DEFAULT
                    if not RUNTIME["replay"].get("holding"):
                        break
                try:
                    if not gsc.camera_set(object_id, pose=hold_pose,
                                          aim_only=(hold_mode != CHASE_MODE_FOLLOW)):
                        break
                except Exception as e:
                    log("camera hold ended: %r" % (e,))
                    break
                if stop_ev.wait(CAMERA_HOLD_SEC):
                    break


def stop_replay():
    with RUNTIME_LOCK:
        stop_ev = RUNTIME["replay"].get("stop")
        th = RUNTIME["replay"].get("thread")
        gsc = RUNTIME["replay"].get("gsc")
        oid = RUNTIME["replay"].get("object_id")
        RUNTIME["replay"]["active"] = False
        RUNTIME["replay"]["clip_id"] = None
        RUNTIME["replay"]["holding"] = False
        RUNTIME["replay"]["paused"] = False
    if stop_ev is not None:
        stop_ev.set()
    if th is not None and th.is_alive() and th is not threading.current_thread():
        th.join(timeout=2.0)
    _teardown_game_replay(gsc, oid)
    with RUNTIME_LOCK:
        RUNTIME["replay"]["thread"] = None
        RUNTIME["replay"]["stop"] = None
        RUNTIME["replay"]["object_id"] = None
        RUNTIME["replay"]["error"] = None
        RUNTIME["replay"]["gsc"] = None
        RUNTIME["replay"]["chase"]["camera_acquired"] = False
    return {"ok": True}


def start_replay(body):
    clip_id = (body or {}).get("clip_id")
    path = (body or {}).get("path")
    with RUNTIME_LOCK:
        connected = bool(RUNTIME["connected"])
        sm = RUNTIME["sm"]
    if not connected or sm is None:
        return {"ok": False, "error": "sim not running"}
    # CameraSet is refused while the sim shows a menu, but every other camera
    # call still answers - so without this check a replay reports success,
    # spawns a ghost, and nothing whatsoever appears on screen.
    with RUNTIME_LOCK:
        cam_state = RUNTIME.get("camera_state")
    if cam_state is not None:
        try:
            import sampler as _sampler
            in_flight_view = _sampler.is_flight_camera(cam_state)
        except Exception:
            in_flight_view = True
        if not in_flight_view:
            log("replay refused: camera state %s is not a flight view" % cam_state)
            return {"ok": False, "error": "sim is in a menu",
                    "detail": "Return to the flight view and try again "
                              "(camera state %s)." % cam_state,
                    "camera_state": cam_state}
    if getattr(sm, "quit", 0) == 1:
        return {"ok": False, "error": "sim not running"}
    doc = load_clip(clip_id=clip_id, path=path)
    if not doc or not doc.get("points"):
        return {"ok": False, "error": "clip not found"}
    att_unit = doc.get("att_unit")
    points = normalize_clip_points(doc.get("points") or [], att_unit)
    if not points:
        return {"ok": False, "error": "clip not found"}
    cid = doc.get("id") or clip_id or "clip"
    stop_replay()
    info = game_dll_status()
    if not info.get("ok"):
        log("chase unavailable: game dll not bound path=%s" % info.get("path"))
        return {"ok": False, "error": "chase unavailable", "clip_id": cid}
    try:
        gsc = GameSimConnect()
    except Exception as e:
        log("game SimConnect client failed %s" % repr(e))
        return {"ok": False, "error": "chase unavailable", "clip_id": cid}
    if not gsc.open():
        try:
            gsc.close()
        except Exception:
            pass
        log("game SimConnect_Open failed clip=%s dll=%s" % (cid, gsc.dll_path))
        return {"ok": False, "error": "chase unavailable", "clip_id": cid}
    gsc._att_unit = "deg"
    title_pref = (body or {}).get("title") or doc.get("aircraft")
    # What the aircraft was wearing when this was flown. There is no way to ask
    # for a different one: the UI used to carry a box for typing an exact
    # title, and a livery chosen by hand is a livery that can be wrong, while
    # the recording already knows the answer. Recorded, then the one loaded
    # now if it is the same variant, then whatever the sim picks by default.
    livery_pref = doc.get("livery")
    livery_src = "clip" if livery_pref else None
    if not livery_pref:
        # A clip may carry no livery. If the
        # aircraft loaded in the sim right now is the same variant the clip was
        # flown in, its livery is almost certainly the one that was worn, so
        # use it rather than letting the sim pick at random.
        with RUNTIME_LOCK:
            cur_doc = RUNTIME.get("current_doc")
        if not cur_doc:
            cur_doc = read_json(CURRENT)
        if isinstance(cur_doc, dict) and cur_doc.get("livery")                 and cur_doc.get("aircraft") == title_pref:
            livery_pref = cur_doc.get("livery")
            livery_src = "current aircraft"
            log("clip %s has no livery; using the one loaded now: %s"
                % (cid, livery_pref))
    ghost_title = None
    oid = None
    last_err = None
    for title in _ghost_titles(title_pref):
        try:
            oid = gsc.spawn_ghost(title, points[0],
                                  livery=livery_pref if title == title_pref else None)
        except Exception as e:
            last_err = repr(e)
            oid = None
        if oid is not None and int(oid) != USER_OBJECT_ID:
            ghost_title = title
            if title != title_pref:
                log("ghost livery substituted: wanted %r, spawned %r"
                    % (title_pref, title))
            log("ghost spawned title=%s object_id=%s dll=%s" % (title, oid, gsc.dll_path))
            break
        oid = None
    if oid is None or int(oid) == USER_OBJECT_ID:
        _teardown_game_replay(gsc, None)
        with RUNTIME_LOCK:
            RUNTIME["replay"]["error"] = "ghost unavailable"
        log("ghost unavailable clip=%s err=%s" % (cid, last_err))
        return {"ok": False, "error": "ghost unavailable", "clip_id": cid}
    first_pt = _ground_lifted(points[0], _clip_ground_alt(points))
    with RUNTIME_LOCK:
        RUNTIME["replay"]["last_pose"] = first_pt
    try:
        gsc.set_ghost_pose(oid, first_pt)
    except Exception as e:
        log("ghost initial pose failed %s" % repr(e))
        _teardown_game_replay(gsc, oid)
        return {"ok": False, "error": "ghost unavailable", "clip_id": cid}
    # Get the gear moving before the first frame plays; the animation takes a
    # moment and a takeoff clip opens with the aircraft already on its wheels.
    if GHOST_FREEZE:
        try:
            frozen_ok = gsc.set_ghost_freeze(oid, True)
            log("ghost freeze %s" % ("on" if frozen_ok else "unavailable"))
        except Exception as e:
            log("ghost freeze failed %s" % repr(e))
    ground_alt = _clip_ground_alt(points)
    gear0 = _gear_target(points[0], ground_alt)
    if gear0 is not None:
        try:
            gsc.set_ghost_gear(oid, gear0)
        except Exception as e:
            log("ghost initial gear failed %s" % repr(e))
    chase_ok = False
    if int(oid) != USER_OBJECT_ID:
        with RUNTIME_LOCK:
            RUNTIME["replay"]["cam_anchor"] = None
            RUNTIME["replay"]["cam_hdg"] = None
        chase_ok = gsc.camera_acquire(oid)
        with RUNTIME_LOCK:
            RUNTIME["replay"]["chase"]["camera_acquired"] = bool(chase_ok)
        if not chase_ok:
            log("chase unavailable clip=%s object_id=%s (ghost still playing)" % (cid, oid))
    stop_ev = threading.Event()
    th = threading.Thread(
        target=_replay_loop,
        args=(gsc, int(oid), points, stop_ev, cid),
        daemon=True,
        name="ghost-replay",
    )
    with RUNTIME_LOCK:
        RUNTIME["replay"]["active"] = True
        RUNTIME["replay"]["holding"] = False
        RUNTIME["replay"]["paused"] = bool(REPLAY_START_PAUSED)
        RUNTIME["replay"]["clip_id"] = cid
        RUNTIME["replay"]["elapsed_s"] = 0.0
        RUNTIME["replay"]["duration_s"] = max(
            (points[-1].get("t") or 0.0) - (points[0].get("t") or 0.0), 0.0)
        RUNTIME["replay"]["aircraft"] = ghost_title
        RUNTIME["replay"]["aircraft_requested"] = title_pref
        RUNTIME["replay"]["livery"] = livery_pref
        RUNTIME["replay"]["livery_source"] = livery_src
        RUNTIME["replay"]["object_id"] = int(oid)
        RUNTIME["replay"]["error"] = None
        RUNTIME["replay"]["stop"] = stop_ev
        RUNTIME["replay"]["thread"] = th
        RUNTIME["replay"]["gsc"] = gsc
    th.start()
    if REPLAY_START_PAUSED:
        log("replay opens held at the first frame clip=%s" % cid)
    out = {"ok": True, "clip_id": cid, "object_id": int(oid),
           "aircraft": ghost_title, "aircraft_requested": title_pref,
           "livery": livery_pref, "livery_source": livery_src,
           "paused": bool(REPLAY_START_PAUSED), "chase": _chase_snapshot()}
    if not chase_ok:
        out["chase_error"] = "chase unavailable"
    return out


def _begin_new_flight(s, tracker, sortie_id=None, lift_at=None):
    """Start a fragment. With sortie_id it continues an outing, not begins one."""
    tracker.finish()
    tracker.reset()
    flight = Flight(s, sortie_id=sortie_id, lift_at=lift_at)
    tracker.attach(flight)
    if sortie_id:
        # Continuity held, so this is not a new outing. Saying flight_start
        # here is what made one flight look like several in the event log.
        write_event("reconnect", flight.flight_id, flight.aircraft,
                    sortie_id=flight.sortie_id)
        log("reconnect: sortie %s continues as %s aircraft=%s"
            % (flight.sortie_id, flight.flight_id, flight.aircraft))
    else:
        write_event("flight_start", flight.flight_id, flight.aircraft,
                    sortie_id=flight.sortie_id)
        log("flight_start %s aircraft=%s" % (flight.flight_id, flight.aircraft))
    return flight


def _keep_existing_flight(flight, tracker, s, why):
    tracker.attach(flight)
    if tracker.leg < 1:
        tracker.leg = restore_tracker_leg(flight.flight_id)
        with RUNTIME_LOCK:
            RUNTIME["leg"] = tracker.leg
    log("keeping flight_id %s started_at=%s (%s, no flight_start)" % (flight.flight_id, flight.started_at, why))
    flight.update(s)
    return flight


def run_connected(sm, flight=None, tracker=None, resume_snap=None):
    from SimConnect import AircraftRequests
    aq = AircraftRequests(sm, _time=200)
    for key in ("PLANE_PITCH_DEGREES", "PLANE_BANK_DEGREES", "IS_SLEW_ACTIVE"):
        try:
            aq.get(key)
        except Exception as e:
            log("optional var %s not subscribed %s" % (key, repr(e)))
    if tracker is None:
        tracker = ClipTracker()
        LIVE_TRACKER[0] = tracker
    announced_keep = False
    if flight is not None:
        tracker.attach(flight)
        announced_keep = True
        log("reconnect with surviving flight_id %s started_at=%s" % (flight.flight_id, flight.started_at))
    stale = 0
    last_detect = 0.0
    sampler_conn, state_sampler = open_state_sampler()
    last_compare = 0.0
    loop_count = 0
    loop_mark = time.time()
    parked_since = None
    fast_used = 0
    fast_missing = 0
    # Extremes since the last recorded point. Handed to the recorder by
    # reference and emptied by it, so this survives the ten reads that happen
    # between one track point and the next.
    peak_acc = {}
    with RUNTIME_LOCK:
        RUNTIME["connected"] = True
        RUNTIME["sm"] = sm
        RUNTIME["replay"]["ghost_def"] = None
        RUNTIME["replay"]["chase"]["camera_acquired"] = False
    info = game_dll_status()
    log(
        "simconnect session ready pid=%s python-simconnect detect; game_dll=%s camera_exports=%s"
        % (os.getpid(), info.get("path"), info.get("ok"))
    )
    try:
        while True:
            loop_t = time.time()
            beat()
            if getattr(sm, "quit", 0) == 1:
                if flight is not None:
                    tracker.finish()
                    flight.end("simconnect_quit")
                    flight = None
                tracker.reset()
                write_current("idle", None, None, None, extra={"connected": False, "error": "sim quit"})
                return "quit", None, tracker
            try:
                s = sample(aq) if SAMPLER_MODE != "fast" else None
                if state_sampler is not None:
                    fast = sample_from_state(state_sampler.latest())
                    merge_peaks(peak_acc, (fast or {}).get("peaks"))
                    if SAMPLER_MODE == "fast":
                        if fast is not None:
                            s = fast
                            fast_used += 1
                        else:
                            # nothing fresh pushed: fall back rather than skip
                            s = sample(aq)
                            fast_missing += 1
                    elif fast is not None and (loop_t - last_compare) >= SAMPLER_COMPARE_SEC:
                        last_compare = loop_t
                        bad = compare_samples(s, fast)
                        st = state_sampler.stats()
                        if bad:
                            log("sampler shadow MISMATCH updates=%s %s" % (
                                st.get("updates"),
                                "; ".join("%s legacy=%.4f fast=%.4f d=%.4f" % b for b in bad)))
                        else:
                            log("sampler shadow ok: %d pushes, all fields agree" %
                                (st.get("updates") or 0))
                    # The recorder needs the running accumulator, not the
                    # single window just read, and it empties this one.
                    if s is not None:
                        s["peaks"] = peak_acc
            except Exception as e:
                log("sample error %s" % repr(e))
                try:
                    write_current(
                        "idle",
                        flight.flight_id if flight else None,
                        flight.started_at if flight else None,
                        None,
                        extra={"connected": False, "error": repr(e)},
                        sortie_id=getattr(flight, "sortie_id", None) if flight else None,
                    )
                except Exception:
                    pass
                return "error", flight, tracker
            try:
                tracker.feed(s)
            except PermissionError as e:
                log("tracker feed PermissionError %s" % repr(e))
            except Exception as e:
                log("tracker feed error %s" % repr(e))
            if loop_t - last_detect >= POLL_SEC:
                last_detect = loop_t
                valid = is_valid(s)
                extra = {
                    "connected": True,
                    "leg": tracker.leg,
                    "open_clip_ids": [c.clip_id for c in tracker.open if not c.done],
                }
                try:
                    if valid:
                        stale = 0
                        if flight is None:
                            restored = maybe_resume_from_disk(s, resume_snap)
                            if restored is not None:
                                flight = _keep_existing_flight(restored, tracker, s, "disk resume")
                            else:
                                # A reconnect that cannot resume the fragment can
                                # still belong to the same outing: same aircraft,
                                # no spawn jump, and the previous sortie known.
                                carry = carry_sortie_id(s, resume_snap)
                                flight = _begin_new_flight(
                                    s, tracker, sortie_id=carry,
                                    lift_at=(resume_snap or {}).get("lift_at") if carry else None)
                        elif not same_sortie(flight, s):
                            log(
                                "new spawn: aircraft/position jump vs surviving %s; minting new flight"
                                % flight.flight_id
                            )
                            tracker.finish()
                            flight.end("new_spawn_or_aircraft")
                            flight = _begin_new_flight(s, tracker)
                        else:
                            if announced_keep:
                                announced_keep = False
                                log(
                                    "keeping flight_id %s started_at=%s (reconnect, no flight_start)"
                                    % (flight.flight_id, flight.started_at)
                                )
                            flight.update(s)
                        extra["leg"] = tracker.leg
                        write_current("in_flight", flight.flight_id, flight.started_at, s,
                                      extra=extra, sortie_id=getattr(flight, "sortie_id", None),
                                      lift_at=getattr(flight, "lift_at", None))
                    else:
                        stale += 1
                        if flight is not None and stale >= 3:
                            tracker.finish()
                            flight.end("invalid_aircraft_or_position")
                            flight = None
                            tracker.reset()
                        write_current(
                            "idle",
                            flight.flight_id if flight else None,
                            flight.started_at if flight else None,
                            s,
                            extra={"connected": True},
                            sortie_id=getattr(flight, "sortie_id", None) if flight else None,
                        )
                except PermissionError as e:
                    log("detect write PermissionError %s" % repr(e))
            cs = s.get("camera_state")
            if cs is not None:
                with RUNTIME_LOCK:
                    RUNTIME["camera_state"] = cs
            if _logbook_pending[0]:
                still = bool(s.get("on_ground")) and (as_float(s.get("gs")) or 0.0) < 1.0
                if not still:
                    parked_since = None
                elif parked_since is None:
                    parked_since = loop_t
                elif (loop_t - parked_since) >= LOGBOOK_PARKED_SEC:
                    parked_since = None
                    run_pending_logbook_rebuild("parked")
            loop_count += 1
            if loop_t - loop_mark >= 5.0:
                hz = loop_count / (loop_t - loop_mark)
                loop_count = 0
                loop_mark = loop_t
                with RUNTIME_LOCK:
                    RUNTIME["sample_hz"] = round(hz, 2)
            elapsed = time.time() - loop_t
            time.sleep(max(0.0, SAMPLE_SEC - elapsed))
    except PermissionError as e:
        log("run PermissionError (not ending flight) %s" % repr(e))
        return "error", flight, tracker
    except Exception as e:
        log("run error %s (flight kept)" % repr(e))
        return "error", flight, tracker
    finally:
        if SAMPLER_MODE == "fast" and (fast_used or fast_missing):
            log("sampler: %d ticks from pushes, %d fell back to legacy" %
                (fast_used, fast_missing))
        if sampler_conn is not None:
            try:
                sampler_conn.close()
            except Exception:
                pass
        stop_replay()
        with RUNTIME_LOCK:
            RUNTIME["connected"] = False
            RUNTIME["sm"] = None
            RUNTIME["replay"]["ghost_def"] = None

def start_stall_watchdog():
    """Leave evidence when the loop stops turning.

    It cannot unwedge the process - whatever is blocking holds the thread - but
    a stalled watcher used to leave an empty log and no explanation. The tray
    watches the same heartbeat over HTTP and is what actually restarts it.
    """
    def run():
        while True:
            time.sleep(5.0)
            age = heartbeat_age()
            if age >= STALL_WARN_SEC and not _STALL_REPORTED[0]:
                _STALL_REPORTED[0] = True
                log("WEDGED: detect loop has not turned for %.0fs - the "
                    "watcher is blocked, not idle" % age)
    threading.Thread(target=run, daemon=True, name="stall-watchdog").start()


def main():
    lock = acquire_lock()
    if lock is None:
        log("already running, exit")
        sys.exit(0)
    write_pid()
    note_code_mtimes()
    set_below_normal_priority()
    log("watcher start pid=%s version=%s" % (os.getpid(), describe_version()))
    load_settings_at_startup()
    info = load_game_simconnect_dll()
    if info.get("ok"):
        log("game SimConnect loaded path=%s CameraAcquire/CameraSet/CameraRelease/Open bound" % info.get("path"))
    else:
        log(
            "game SimConnect chase unavailable path=%s missing=%s"
            % (info.get("path"), ",".join(info.get("missing") or []) or "not-found")
        )
    resume_snap = read_json(CURRENT)
    # Make sure the logbook and maps exist before anyone opens the UI.
    schedule_logbook_rebuild(delay=2.0, reason="startup")
    start_http()
    write_current("idle", None, None, None, extra={"connected": False, "error": "connecting"})
    flight = None
    tracker = None
    first_connect_snap = resume_snap
    start_stall_watchdog()
    while True:
        beat()
        sm, err = connect_sim()
        if sm is None:
            log("connect failed: %s" % err)
            write_current("idle", None, None, None, extra={"connected": False, "error": err})
            time.sleep(RECONNECT_SEC)
            continue
        log("connected")
        try:
            reason, flight, tracker = run_connected(
                sm, flight=flight, tracker=tracker, resume_snap=first_connect_snap
            )
            if flight is not None:
                first_connect_snap = None
            log("session ended reason=%s" % reason)
            if reason == "quit":
                flight = None
                if tracker is not None:
                    tracker.reset()
                tracker = None
        except PermissionError as e:
            log("run error %s (flight kept)" % repr(e))
        except Exception as e:
            log("run error %s (flight kept)" % repr(e))
        try:
            stop_replay()
        except Exception:
            pass
        try:
            sm.exit()
        except Exception:
            pass
        with RUNTIME_LOCK:
            RUNTIME["connected"] = False
            RUNTIME["sm"] = None
        time.sleep(RECONNECT_SEC)

if __name__ == "__main__":
    main()

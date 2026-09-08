import json
import math
import os
import sys
import threading
import time
from datetime import datetime, timezone

# Derived, not hardcoded - see the note in watcher.py.
BASE = os.path.dirname(os.path.abspath(__file__))
CURRENT = os.path.join(BASE, "current.json")

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

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

def idle_doc(error):
    return {
        "state": "idle",
        "flight_id": None,
        "started_at": None,
        "aircraft": None,
        "lat": None,
        "lon": None,
        "alt": None,
        "on_ground": None,
        "last_update": now_iso(),
        "connected": False,
        "error": error,
    }

result = {
    "connected": False,
    "error": None,
    "at": now_iso(),
    "aircraft": None,
    "lat": None,
    "lon": None,
    "alt": None,
    "on_ground": None,
    "airspeed": None,
    "heading": None,
    "vs": None,
    "gs": None,
}

holder = {"sm": None, "err": None}

def do_connect():
    try:
        from SimConnect import SimConnect
        holder["sm"] = SimConnect(auto_connect=True)
    except Exception as e:
        holder["err"] = repr(e)

th = threading.Thread(target=do_connect, daemon=True)
th.start()
th.join(20)

if holder["err"]:
    result["error"] = holder["err"]
    atomic_write(CURRENT, idle_doc(result["error"]))
    print(json.dumps(result, indent=2))
    sys.exit(2)

if holder["sm"] is None:
    result["error"] = "SimConnect connect timed out after 20s"
    atomic_write(CURRENT, idle_doc(result["error"]))
    print(json.dumps(result, indent=2))
    sys.exit(3)

result["connected"] = True
sm = holder["sm"]

from SimConnect import AircraftRequests
aq = AircraftRequests(sm, _time=200)

for _i in range(30):
    title = decode_val(aq.get("TITLE"))
    lat = aq.get("PLANE_LATITUDE")
    lon = aq.get("PLANE_LONGITUDE")
    if title and finite(lat) and finite(lon) and not (float(lat) == 0.0 and float(lon) == 0.0):
        break
    time.sleep(0.4)

result["aircraft"] = decode_val(aq.get("TITLE"))

def as_float(key):
    v = aq.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None

result["lat"] = as_float("PLANE_LATITUDE")
result["lon"] = as_float("PLANE_LONGITUDE")
result["alt"] = as_float("PLANE_ALTITUDE")
result["airspeed"] = as_float("AIRSPEED_INDICATED")
result["heading"] = as_float("PLANE_HEADING_DEGREES_TRUE")
result["vs"] = as_float("VERTICAL_SPEED")
result["gs"] = as_float("GROUND_VELOCITY")

og = aq.get("SIM_ON_GROUND")
if og is None:
    result["on_ground"] = None
else:
    try:
        result["on_ground"] = bool(int(float(og)))
    except Exception:
        result["on_ground"] = bool(og)

valid = (
    bool(result["aircraft"])
    and finite(result["lat"])
    and finite(result["lon"])
    and not (result["lat"] == 0.0 and result["lon"] == 0.0)
)

flight_id = datetime.now(timezone.utc).strftime("flt-%Y%m%dT%H%M%SZ") if valid else None
current = {
    "state": "in_flight" if valid else "idle",
    "flight_id": flight_id,
    "started_at": now_iso() if valid else None,
    "aircraft": result["aircraft"],
    "lat": result["lat"],
    "lon": result["lon"],
    "alt": result["alt"],
    "on_ground": result["on_ground"],
    "last_update": now_iso(),
    "connected": True,
    "airspeed": result["airspeed"],
    "heading": result["heading"],
    "vs": result["vs"],
    "gs": result["gs"],
}
result["flight_id"] = flight_id
result["state"] = current["state"]
atomic_write(CURRENT, current)
print(json.dumps(result, indent=2))

try:
    sm.exit()
except Exception:
    pass

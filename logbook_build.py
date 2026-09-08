"""Build logbook.json from the session tree.

The watcher owns this job, so the logbook and the maps exist whether or not
anything else is running.

Model:
  flight  - one .jsonl + .meta.json written by the watcher. A sim reconnect or
            a position jump starts a new one, so a single outing can produce
            many of these.
  sortie  - consecutive flights on the same airframe with no real gap between
            them. This is what a human calls "a flight".
  leg     - a takeoff paired with the next landing in time order, within a
            sortie. Pairing is chronological rather than by the raw leg
            counter, because after a reconnect a leg's takeoff and its landing
            can land in two different flight records.
"""

import glob
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone

import flightprefs
import grading
import mapbake
import passenger
import clipfile
import persistence
import tiles

BASE = os.path.dirname(os.path.abspath(__file__))
SESSIONS = os.path.join(BASE, "sessions")
CLIPS_DIR = os.path.join(SESSIONS, "clips")
EVENTS_JSONL = os.path.join(BASE, "events.jsonl")
LOGBOOK_JSON = os.path.join(BASE, "logbook.json")
CACHE_JSON = os.path.join(BASE, "logbook.cache.json")
PLACES_JSON = os.path.join(BASE, "places.json")

# A sortie continues across a reconnect if the next flight starts within this
# many seconds of the previous one's last sample...
SORTIE_GAP_SEC = 240.0
# ...and did not teleport somewhere else entirely.
SORTIE_JUMP_NM = 25.0
# Ignore flight records that never produced a usable fix.
MIN_USEFUL_POINTS = 2

SCHEMA = 2
# Bump when the shape of a built sortie changes, so old cache entries are
# discarded rather than served as if they were current.
# Bump this whenever mapbake's drawing logic changes - marker placement,
# segment splitting, anything that alters the picture without touching canvas,
# style, tiles or supersample. Those have their own signature entries; drawing
# logic has none, so this is what stops a stale PNG being served as current.
# Bumped for: markers at the leg events, jump-splitting, stop markers.
BUILDER_VERSION = 32
EXCLUDED_JSON = os.path.join(BASE, "excluded.json")
DETAIL_DIR = os.path.join(SESSIONS, "detail")

# Maps are shown as baked PNGs; these polylines are only the fallback for when
# a PNG is missing (no Pillow, or nothing drawable). They were the largest
# single contributor to logbook.json, which has to stay small because the whole
# document is fetched by the browser on every load.
SORTIE_TRACK_POINTS = 240
LEG_TRACK_POINTS = 120


def _finite(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and abs(f) != float("inf")


def parse_ts(s):
    """ISO-8601 (Z or offset) -> unix seconds. None if unparseable."""
    if not s:
        return None
    try:
        txt = str(s)
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def iso_local(unix_ts):
    if unix_ts is None:
        return None
    try:
        return datetime.fromtimestamp(unix_ts).astimezone().isoformat(timespec="seconds")
    except Exception:
        return None


def local_tz_name():
    try:
        return datetime.now().astimezone().tzname() or "local"
    except Exception:
        return "local"


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def atomic_write_json(path, obj):
    return persistence.atomic_json(path, obj)


def read_track(path):
    """Load a session .jsonl, dropping spawn junk and unusable fixes."""
    pts = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                except Exception:
                    continue
                if mapbake.is_spawn_junk(p.get("lat"), p.get("lon")):
                    continue
                t = parse_ts(p.get("ts"))
                if t is None:
                    continue
                p["t"] = t
                pts.append(p)
    except Exception:
        return []
    pts.sort(key=lambda p: p["t"])
    return pts


def haversine_nm(lat1, lon1, lat2, lon2):
    r = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------- places


def load_places():
    """Optional offline gazetteer: [{name, lat, lon, radius_nm}]. No network."""
    doc = read_json(PLACES_JSON)
    if isinstance(doc, list):
        rows = doc
    elif isinstance(doc, dict):
        rows = doc.get("places") or []
    else:
        rows = []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if not (_finite(r.get("lat")) and _finite(r.get("lon"))):
            continue
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "radius_nm": float(r.get("radius_nm") or 5.0),
        })
    return out


def name_point(lat, lon, places):
    if not usable_fix(lat, lon):
        return None
    best, best_d = None, None
    for p in places:
        d = haversine_nm(float(lat), float(lon), p["lat"], p["lon"])
        if d <= p["radius_nm"] and (best_d is None or d < best_d):
            best, best_d = p["name"], d
    return best


def usable_fix(lat, lon):
    """A plottable position: finite, and not the pre-world-load spawn slot."""
    return _finite(lat) and _finite(lon) and not mapbake.is_spawn_junk(lat, lon)


def coord_label(lat, lon):
    if not usable_fix(lat, lon):
        return None
    return "%.4f, %.4f" % (float(lat), float(lon))


# ---------------------------------------------------------------- flights


def summarize_track(flight_id, track):
    """Per-track stats. Airborne time and distance only count in-air motion."""
    rec = {
        "flight_id": flight_id,
        "points": len(track),
        "t_start": None, "t_end": None,
        "lat_start": None, "lon_start": None,
        "lat_end": None, "lon_end": None,
        "distance_nm": 0.0, "airborne_s": 0.0,
        "max_alt_ft": None, "max_gs_kt": None, "peak_vs_fpm": None,
        "aircraft": None,
    }
    if not track:
        return rec

    rec["t_start"] = track[0]["t"]
    rec["t_end"] = track[-1]["t"]
    rec["lat_start"], rec["lon_start"] = track[0].get("lat"), track[0].get("lon")
    rec["lat_end"], rec["lon_end"] = track[-1].get("lat"), track[-1].get("lon")

    dist = 0.0
    airborne = 0.0
    max_alt = None
    max_gs = None
    peak_vs = None
    prev = None
    for p in track:
        if prev is not None:
            dt = p["t"] - prev["t"]
            if 0.0 < dt <= 10.0:
                if prev.get("on_ground") is False or p.get("on_ground") is False:
                    airborne += dt
                if all(_finite(v) for v in (prev.get("lat"), prev.get("lon"),
                                            p.get("lat"), p.get("lon"))):
                    d = haversine_nm(prev["lat"], prev["lon"], p["lat"], p["lon"])
                    if d < 5.0:  # ignore teleports
                        dist += d
        if _finite(p.get("alt")):
            max_alt = p["alt"] if max_alt is None else max(max_alt, p["alt"])
        if _finite(p.get("gs")):
            max_gs = p["gs"] if max_gs is None else max(max_gs, p["gs"])
        if _finite(p.get("vs")) and p.get("on_ground") is False:
            if peak_vs is None or abs(p["vs"]) > abs(peak_vs):
                peak_vs = p["vs"]
        prev = p

    rec["distance_nm"] = round(dist, 4)
    rec["airborne_s"] = round(airborne, 1)
    rec["max_alt_ft"] = round(max_alt) if max_alt is not None else None
    rec["max_gs_kt"] = round(max_gs, 1) if max_gs is not None else None
    rec["peak_vs_fpm"] = round(peak_vs, 1) if peak_vs is not None else None
    return rec


def _rel_to_base(path):
    """A path relative to BASE, or an absolute one when that is impossible.

    os.path.relpath raises on Windows when the two are on different drives,
    which is not a hypothetical: a CI runner checks the repository out on D:
    and hands tempfile a directory on C:, and the whole build died on a
    ValueError rather than on anything to do with flying. An absolute path is
    a perfectly good answer to "where is this file" - it is only longer.
    """
    try:
        return os.path.relpath(path, BASE).replace(os.sep, "/")
    except ValueError:
        return os.path.abspath(path).replace(os.sep, "/")


def scan_flights(force=False):
    """One record per session .jsonl, with track stats. Cached on mtime+size."""
    cache = read_json(CACHE_JSON) or {}
    cached_flights = cache.get("flights") if isinstance(cache, dict) else None
    if force or not isinstance(cached_flights, dict):
        cached_flights = {}
    fresh = {}
    flights = []

    for jsonl_path in sorted(glob.glob(os.path.join(SESSIONS, "*.jsonl"))):
        flight_id = os.path.basename(jsonl_path)[: -len(".jsonl")]
        meta_path = os.path.join(SESSIONS, flight_id + ".meta.json")
        try:
            st = os.stat(jsonl_path)
            sig = "%d:%d" % (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
        # The meta is part of what makes a flight record current, so it is
        # part of the signature. Without it the meta was read for every flight
        # on every rebuild - the one cost here that grew with the size of the
        # logbook rather than with what had just been flown. A read is 105 us
        # against 39 for the stat, which is 200 ms a rebuild once there are a
        # couple of thousand flight records, spent re-learning what had not
        # changed.
        sig += "|" + file_sig(meta_path)

        hit = cached_flights.get(flight_id)
        cached = (isinstance(hit, dict) and hit.get("sig") == sig
                  and isinstance(hit.get("rec"), dict))
        if cached:
            rec = dict(hit["rec"])
            # The cached rec already carries everything the meta contributed,
            # because the signature above says the meta has not moved.
            fresh[flight_id] = {"sig": sig, "rec": rec}
            rec["jsonl"] = _rel_to_base(jsonl_path)
            flights.append(rec)
            continue

        meta = read_json(meta_path) or {}
        rec = summarize_track(flight_id, read_track(jsonl_path))
        fresh[flight_id] = {"sig": sig, "rec": rec}

        rec["aircraft"] = meta.get("aircraft") or rec.get("aircraft")
        # The watcher records this. A track without one falls back to being
        # grouped by the timing and position rules below.
        rec["sortie_id"] = meta.get("sortie_id")
        rec["meta_ended_at"] = meta.get("ended_at")
        rec["end_reason"] = meta.get("end_reason")
        # What the sim said this aircraft is, and its stall speed. These decide
        # which profile grades the flight. They were written to the meta but
        # never copied here, so grade_leg saw None for both and fell back to
        # inferring the type from the flying - which happened to agree, because
        # a Cessna does not hover, and hid the gap completely.
        rec["category"] = meta.get("category")
        rec["vs0"] = meta.get("vs0")
        rec["jsonl"] = _rel_to_base(jsonl_path)
        flights.append(rec)

    try:
        # Merge, do not replace: the sortie cache lives in the same file and is
        # written later in the build. Overwriting it here meant every sortie
        # missed on the next run.
        doc = read_json(CACHE_JSON)
        if not isinstance(doc, dict):
            doc = {}
        doc["schema"] = SCHEMA
        doc["flights"] = fresh
        atomic_write_json(CACHE_JSON, doc)
    except Exception:
        pass

    flights.sort(key=lambda r: (r.get("t_start") or 0.0))
    return flights


def _jumped_away(prev, f):
    """True if the next record starts somewhere unrelated to the previous one."""
    if not (_finite(f.get("lat_start")) and _finite(f.get("lon_start"))):
        return False
    dists = []
    for lat_key, lon_key in (("lat_start", "lon_start"), ("lat_end", "lon_end")):
        if _finite(prev.get(lat_key)) and _finite(prev.get(lon_key)):
            dists.append(haversine_nm(prev[lat_key], prev[lon_key],
                                      f["lat_start"], f["lon_start"]))
    if not dists:
        return False
    return min(dists) > SORTIE_JUMP_NM


def group_sorties(flights):
    """Chain flight records that are really one outing.

    Records can overlap rather than run back to back: the watcher keeps
    appending to a resumed flight while also opening new ones after a jump. So
    the group is tracked by its running end time, and a negative gap (an
    overlap or a record fully contained in another) still counts as the same
    sortie.
    """
    sorties = []
    cur = None
    group_end = None
    for f in flights:
        if f["points"] < MIN_USEFUL_POINTS:
            continue
        if cur is None:
            cur = [f]
            group_end = f.get("t_end")
            continue
        prev = cur[-1]
        gap = (f.get("t_start") or 0.0) - (group_end or 0.0)
        same_ac = (f.get("aircraft") or None) == (prev.get("aircraft") or None)
        # The watcher knows whether a fragment continued an outing, so when
        # both records say, believe them rather than re-deriving it. Guessing
        # from timing and distance is only the fallback for a track that
        # carries no sortie id.
        declared = f.get("sortie_id")
        declared_prev = prev.get("sortie_id")
        if declared and declared_prev:
            joins = declared == declared_prev
        else:
            joins = same_ac and gap <= SORTIE_GAP_SEC and not _jumped_away(prev, f)
        if joins:
            cur.append(f)
            if f.get("t_end") is not None:
                group_end = f["t_end"] if group_end is None else max(group_end, f["t_end"])
        else:
            sorties.append(cur)
            cur = [f]
            group_end = f.get("t_end")
    if cur:
        sorties.append(cur)
    return sorties


def merge_tracks(tracks):
    """Merge overlapping flight records into one track, dropping re-samples.

    Overlapping records sample the same moments twice; keeping both would
    double-count jitter in the distance total.
    """
    merged = []
    for tr in tracks:
        merged.extend(tr)
    merged.sort(key=lambda p: p["t"])
    out = []
    last_key = None
    for p in merged:
        key = round(p["t"], 1)
        if key == last_key:
            continue
        last_key = key
        out.append(p)
    return out


# ---------------------------------------------------------------- events


def leg_key(leg):
    """A leg identity that survives rebuilds.

    Not the sequence number: that renumbers the moment a leg is hidden or the
    event log changes. The takeoff instant does not move.
    """
    for side in ("takeoff", "landing"):
        d = leg.get(side) or {}
        if d.get("at_utc"):
            return "%s:%s" % (side, d["at_utc"])
    return "seq:%s" % leg.get("seq")


def month_of(entry):
    """The YYYY-MM a flight belongs to, or None if it has no usable date."""
    d = entry.get("date") or ""
    return d[:7] if len(d) >= 7 else None


def write_months(summaries, say):
    """One file per month, and the small list the UI navigates by.

    The index used to carry every flight, so opening the logbook grew with the
    whole history: measured at 956 bytes a flight, which is 650 KB and 700 rows
    after a year at the rate this one is filling up. Months are the unit
    because that is how the UI already groups and filters.

    Returns the month list for the index. Files are only rewritten when their
    contents actually change, so a rebuild does not touch every month.
    """
    out_dir = os.path.join(SESSIONS, "months")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        say("months: could not create %s (%r)" % (out_dir, exc))
        return []

    by_month = {}
    for entry in summaries:
        ym = month_of(entry)
        if ym:
            by_month.setdefault(ym, []).append(entry)

    months = []
    for ym in sorted(by_month, reverse=True):
        rows = by_month[ym]
        rel = "sessions/months/%s.json" % ym
        path = os.path.join(BASE, rel.replace("/", os.sep))
        doc = {"schema": SCHEMA, "ym": ym, "sorties": rows}
        old = read_json(path)
        if old != doc:
            atomic_write_json(path, doc)
        months.append({
            "ym": ym,
            "file": rel,
            "sorties": len(rows),
            "legs": sum(r["legs"] for r in rows),
            "landings": sum(r["landings"] or 0 for r in rows),
            "distance_nm": round(sum(r["distance_nm"] or 0.0 for r in rows), 2),
            "airborne_s": round(sum(r["airborne_s"] or 0.0 for r in rows)),
            "days": len(set(r["date"] for r in rows if r.get("date"))),
        })

    # A month whose last flight was removed leaves a file behind that the UI
    # would still list. Prune, the same way stale maps are pruned.
    keep = set("%s.json" % m["ym"] for m in months)
    try:
        for path in glob.glob(os.path.join(out_dir, "*.json")):
            if os.path.basename(path) not in keep:
                os.remove(path)
                say("pruned empty month %s" % os.path.basename(path))
    except OSError as exc:
        say("month prune failed %r" % exc)
    return months


def find_rows(summaries):
    """The least a flight needs to be filtered and searched without its month.

    Short keys on purpose: this is the one list that still carries every
    flight, so it is the one that has to stay small. About 90 bytes a flight
    against 956 for the full entry.
    """
    rows = []
    for e in summaries:
        rows.append({
            "i": e.get("sortie_id"),
            "m": month_of(e),
            "d": e.get("date"),
            "a": e.get("aircraft"),
            "f": e.get("route_from"),
            "t": e.get("route_to"),
            "s": e.get("started_at"),
        })
    return rows


def load_exclusions():
    """{sorties: {id: {...}}, legs: {sortie_id: {leg_key: {...}}}}"""
    doc = read_json(EXCLUDED_JSON)
    if not isinstance(doc, dict):
        return {"sorties": {}, "legs": {}}
    sorties = doc.get("sorties") if isinstance(doc.get("sorties"), dict) else {}
    legs = doc.get("legs") if isinstance(doc.get("legs"), dict) else {}
    return {"sorties": sorties, "legs": legs}


def file_sig(path):
    """mtime+size, the cheap way to notice a file changed."""
    try:
        st = os.stat(path)
        return "%d:%d" % (st.st_mtime_ns, st.st_size)
    except OSError:
        return "-"


def load_events():
    rows = []
    try:
        with open(EVENTS_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                t = parse_ts(e.get("time"))
                if t is None:
                    continue
                e["t"] = t
                rows.append(e)
    except FileNotFoundError:
        return []
    except Exception:
        return []
    rows.sort(key=lambda e: e["t"])
    return rows


def clip_id_from_event(e):
    """Prefer the real file basename; fall back to the documented naming."""
    clip = e.get("clip")
    if clip:
        base = os.path.basename(str(clip).replace("\\", "/"))
        if base.endswith(".json"):
            return base[: -len(".json")]
    fid, leg, kind = e.get("flight_id"), e.get("leg"), e.get("kind")
    if fid and leg is not None and kind:
        return "%s-leg%s-%s" % (fid, leg, kind)
    return None


def clip_exists(clip_id):
    if not clip_id:
        return False
    return clipfile.clip_path(CLIPS_DIR, clip_id) is not None


# A leg has to be something that actually happened. The watcher can log a
# takeoff while the world is still loading, at the (0, 90) spawn slot, which is
# not a leg - it has no landing, no track and no distance.
MIN_REAL_LEG_NM = 0.05
MIN_REAL_LEG_SEC = 10.0


def is_real_leg(leg, to_e, ld_e):
    """False for phantom legs: a spurious takeoff that never went anywhere."""
    if ld_e is not None:
        return True                      # a landing is always real
    if to_e is None:
        return False
    if not usable_fix(to_e.get("lat"), to_e.get("lon")):
        return False                     # takeoff logged at the spawn slot
    moved = (leg.get("distance_nm") or 0.0) >= MIN_REAL_LEG_NM
    flew = (leg.get("airborne_s") or 0.0) >= MIN_REAL_LEG_SEC
    return bool(moved or flew)


def pair_legs(events):
    """Takeoff paired with the next landing, chronologically."""
    legs = []
    pending = None
    for e in events:
        kind = e.get("kind")
        if kind == "takeoff":
            if pending is not None:
                legs.append({"takeoff": pending, "landing": None})
            pending = e
        elif kind == "landing":
            legs.append({"takeoff": pending, "landing": e})
            pending = None
    if pending is not None:
        legs.append({"takeoff": pending, "landing": None})
    return legs


def slice_track(track, t0, t1):
    lo = t0 if t0 is not None else -math.inf
    hi = t1 if t1 is not None else math.inf
    return [p for p in track if lo <= p["t"] <= hi]


# Below this the "track" is just GPS jitter on a parked aircraft. Drawing it
# autoscales the noise into a convincing-looking flight path, so don't.
MIN_TRACK_EXTENT_NM = 0.05


def track_extent_nm(track):
    """Diagonal of the track's bounding box, in nautical miles."""
    pts = [p for p in track if usable_fix(p.get("lat"), p.get("lon"))]
    if len(pts) < 2:
        return 0.0
    lats = [float(p["lat"]) for p in pts]
    lons = [float(p["lon"]) for p in pts]
    return haversine_nm(min(lats), min(lons), max(lats), max(lons))


def drawable(track):
    """The track, or empty if there is nothing real to draw."""
    if track_extent_nm(track) < MIN_TRACK_EXTENT_NM:
        return []
    return track


# Same bounce filter the watcher uses, so a skip on touchdown is not two legs.
BOUNCE_SEC = 2.0
# Look back this far for the touchdown rate, matching the watcher's window.
TOUCHDOWN_LOOKBACK_SEC = 2.0


def touchdown_rate_fpm(track, idx):
    """Steepest descent rate in the moments before touchdown, as a magnitude.

    Positive, because that is how a touchdown rate is spoken and printed - "91
    fpm" - and because the watcher's own path returns the sim's touchdown
    normal velocity, which is already a magnitude. Returning a signed value
    here left two conventions in the stored logbook: six legs negative, ten
    positive, purely by which code path produced them.
    """
    t_land = track[idx]["t"]
    worst = None
    i = idx
    while i >= 0 and (t_land - track[i]["t"]) <= TOUCHDOWN_LOOKBACK_SEC:
        vs = track[i].get("vs")
        if _finite(vs) and (worst is None or vs < worst):
            worst = float(vs)
        i -= 1
    return None if worst is None else abs(worst)


def legs_from_track(track):
    """Derive legs from on_ground transitions.

    Used for a sortie with no entry in the event log, or any stretch the log
    never saw - the watcher not running for part of it, say. Events remain
    authoritative when they exist, because they carry the clip ids.
    """
    if not track:
        return []

    # Collapse transitions shorter than the bounce filter.
    transitions = []
    prev = None
    for i, p in enumerate(track):
        g = p.get("on_ground")
        if g is None:
            continue
        if prev is not None and g != prev[0]:
            transitions.append((i, prev[0], g))
        prev = (g, i)

    kept = []
    for tr in transitions:
        if kept and (track[tr[0]]["t"] - track[kept[-1][0]]["t"]) < BOUNCE_SEC:
            kept.pop()  # a bounce: drop both sides of the blip
            continue
        kept.append(tr)

    legs = []
    open_to = None
    for idx, was, now in kept:
        if was is True and now is False:      # lift-off
            open_to = idx
        elif was is False and now is True:    # touchdown
            legs.append((open_to, idx))
            open_to = None
    if open_to is not None:
        legs.append((open_to, None))
    return legs


def synth_events_from_track(track, flight_id):
    """Build takeoff/landing pseudo-events so track legs join the normal path."""
    out = []
    for to_idx, ld_idx in legs_from_track(track):
        to_e = ld_e = None
        if to_idx is not None:
            p = track[to_idx]
            to_e = {
                "kind": "takeoff", "flight_id": flight_id, "leg": None,
                "time": p.get("ts"), "t": p["t"],
                "lat": p.get("lat"), "lon": p.get("lon"),
                "rate_fpm": None, "clip": None, "derived": True,
            }
        if ld_idx is not None:
            p = track[ld_idx]
            ld_e = {
                "kind": "landing", "flight_id": flight_id, "leg": None,
                "time": p.get("ts"), "t": p["t"],
                "lat": p.get("lat"), "lon": p.get("lon"),
                "rate_fpm": touchdown_rate_fpm(track, ld_idx),
                "clip": None, "derived": True,
            }
        if to_e or ld_e:
            out.append({"takeoff": to_e, "landing": ld_e})
    return out


# ---------------------------------------------------------------- build


def _tile_source_name():
    """Which basemap the bake will draw on, for the cache signature."""
    try:
        import tiles
        return getattr(tiles, "TILE_SOURCE", "")
    except Exception:
        return ""


# How far either side of a recorded landing to look for its other contacts.
DERIVE_BOUNCE_BEFORE_SEC = 4.0
DERIVE_BOUNCE_AFTER_SEC = 6.0


def clip_status(clip_id):
    """What the clip file says about itself, for the leg to report.

    Returns None when there is no clip - that is the ordinary case for older
    flights and is not a fault. Otherwise: whether capture was recorded as
    finished, how many records would not parse, and whether the last one was
    an interrupted append.

    This reader may not say "interrupted". Recording claims are held in the
    watcher's memory, so a build running anywhere else cannot tell a
    recording that stopped from one still in progress. It reports that
    completion was not recorded and leaves the conclusion alone.
    """
    path = clipfile.clip_path(CLIPS_DIR, clip_id)
    if not path:
        return None
    doc = clipfile.read_clip(path)
    if doc is None:
        return {"unreadable": True, "complete": False, "incomplete": True,
                "damaged_lines": 0, "torn_tail": False}
    return {"unreadable": False,
            "complete": bool(doc.get("complete")),
            "incomplete": bool(doc.get("incomplete")),
            "damaged_lines": int(doc.get("damaged_lines") or 0),
            "torn_tail": bool(doc.get("torn_tail"))}


def clip_points(clip_id):
    """The recorded points of a clip, or []."""
    if not clip_exists(clip_id):
        return []
    doc = clipfile.read_clip(clipfile.clip_path(CLIPS_DIR, clip_id)) or {}
    # A clip with holes in it is not a clip to derive a landing rate from.
    # Recovering one from a recording that is missing records exactly where
    # they might matter is the same error as reading an unlatched zero as a
    # greaser, arrived at from the other end.
    if doc.get("incomplete"):
        return []
    pts = doc.get("points") or []
    return pts if isinstance(pts, list) else []


def _recovered_contacts(contacts, rate, recovered):
    """Keep the contact list agreeing with the headline rate.

    A landing whose rate was a latched zero recorded that zero as its single
    contact too, so recovering one without the other left a leg reading
    91 fpm with a contact of 0. Only the lone zero is replaced: a real
    reading, or a list describing the several contacts of a bounce, is left
    exactly as the watcher recorded it.
    """
    if not recovered or rate is None:
        return contacts
    if (isinstance(contacts, list) and len(contacts) == 1
            and _finite(contacts[0]) and abs(float(contacts[0])) < 1e-6):
        return [round(rate, 1)]
    return contacts


def clip_touchdown_rate_fpm(clip_id):
    """Touchdown rate from a clip, for landings the sim never latched.

    PLANE TOUCHDOWN NORMAL VELOCITY reads exactly 0.0 when the sim has not
    latched a value, and the watcher used to take that literally: one leg
    that arrived at 91 fpm was stored as 0 fpm and graded A, Butter. The
    watcher no longer does that, but landings already recorded still carry
    the zero, and the clip still holds the vertical speed it arrived with.

    Vertical speed is a different measure from touchdown normal velocity, so
    this is only used where the stored rate is a zero that means 'no
    reading' - never to second-guess a real one.
    """
    window = clip_points(clip_id)
    if len(window) < 3:
        return None
    for i in range(1, len(window)):
        if window[i - 1].get("on_ground") is False and window[i].get("on_ground") is True:
            t_land = window[i].get("t")
            worst = None
            j = i
            while j >= 0 and _finite(window[j].get("t")) and _finite(t_land) \
                    and (t_land - window[j]["t"]) <= TOUCHDOWN_LOOKBACK_SEC:
                vs = window[j].get("vs")
                if _finite(vs) and (worst is None or float(vs) < worst):
                    worst = float(vs)
                j -= 1
            return None if worst is None else abs(worst)
    return None


def derive_contacts(clip_id):
    """Reconstruct a landing's contacts from its clip.

    Landings written before the watcher captured bounces still have the
    evidence: on_ground says when it touched, alt says how high it came back
    off, vs says how fast each contact was. The one thing that cannot be
    recovered is PLANE TOUCHDOWN NORMAL VELOCITY, which was never recorded and
    is what new landings grade on - so derived rates are vertical speed, a
    different measure, and this never touches the stored grade.

    It reads the clip rather than the flight log: the log is written at 1 Hz
    and a bounce lasting under a second simply is not in it, while clips are
    the full 10 Hz around the event.
    """
    window = clip_points(clip_id)
    if len(window) < 3:
        return None

    rates, heights = [], []
    prev_ground = None
    last_ground_alt = None
    peak_alt = None
    airborne = False
    for p in window:
        og = p.get("on_ground")
        alt = p.get("alt")
        if og is True:
            if prev_ground is False:
                vs = p.get("vs")
                rates.append(round(abs(float(vs)), 1) if _finite(vs) else None)
                if airborne and peak_alt is not None and last_ground_alt is not None:
                    heights.append(round(max(0.0, peak_alt - last_ground_alt), 1))
                airborne = False
                peak_alt = None
            if _finite(alt):
                last_ground_alt = float(alt)
        elif og is False and prev_ground is True:
            airborne = True
            peak_alt = float(alt) if _finite(alt) else None
        elif og is False and airborne and _finite(alt):
            peak_alt = float(alt) if peak_alt is None else max(peak_alt, float(alt))
        prev_ground = og

    if len(rates) < 2:
        return None                       # a clean landing; nothing to report
    detail = [{"height_ft": heights[i] if i < len(heights) else None,
               "rate_fpm": rates[i + 1] if (i + 1) < len(rates) else None}
              for i in range(len(rates) - 1)]
    return {
        "contacts": len(rates),
        "bounces": len(rates) - 1,
        "contact_rates_fpm": rates,
        "bounce_heights_ft": heights,
        "bounce_detail": detail,
        # Vertical speed, not touchdown normal velocity - say so rather than
        # let it look like the same measurement the grade used.
        "contacts_derived": True,
    }


def event_point(leg, which):
    """(lat, lon) of a leg's takeoff or landing, when it has a usable fix."""
    ev = (leg or {}).get(which) or {}
    lat, lon = ev.get("lat"), ev.get("lon")
    if usable_fix(lat, lon):
        return (float(lat), float(lon))
    return None


def hms(seconds):
    """Compact airborne time for a map legend."""
    try:
        t = int(round(float(seconds or 0)))
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return "%dh %02dm" % (h, m) if h else "%dm %02ds" % (m, sec)


def map_legend(title, aircraft, distance_nm, airborne_s):
    """The lines that go in a baked map's legend card, in reading order."""
    rows = [title]
    if aircraft:
        rows.append(str(aircraft))
    stat = []
    try:
        if distance_nm:
            stat.append("%.1f nm" % float(distance_nm))
    except (TypeError, ValueError):
        pass
    t = hms(airborne_s)
    if t:
        stat.append(t)
    if stat:
        rows.append("  ·  ".join(stat))
    return rows


def sortie_order_key(s):
    """Newest-first sort key that survives the cache.

    started_at_unix is set when a sortie is built and popped again once the
    sort is done - but the object popped is the same one held in the sortie
    cache, so the key only ever survived a single build. Every later build
    reused cached docs, scored them all 0.0, and the descending sort collapsed
    into insertion order, which is oldest-first. Hence a logbook that silently
    flipped to ascending after the first rebuild.

    So fall back through values that are actually persisted: the ISO start
    time, then the sortie id, which is a UTC stamp and sorts chronologically
    as text.
    """
    t = s.get("started_at_unix")
    if t:
        try:
            return (1, float(t), "")
        except (TypeError, ValueError):
            pass
    iso = s.get("started_at")
    if iso:
        try:
            return (1, datetime.fromisoformat(str(iso)).timestamp(), "")
        except Exception:
            pass
    return (0, 0.0, str(s.get("sortie_id") or ""))


def grading_revision():
    """Content identity of the grading algorithms, independent of Git."""
    h = hashlib.sha256()
    for module in (grading, passenger):
        with open(module.__file__, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def global_signature(bake_maps):
    """Inputs that affect every sortie, not just one."""
    return "|".join([
        "v%d.%d" % (SCHEMA, BUILDER_VERSION),
        # events.jsonl is deliberately NOT here. It is one file for every
        # flight, so a single landing changed its signature and invalidated
        # every sortie in the book: measured at 5 of 5 cache misses and a
        # 15 s rebake of every PNG, twice over once an index-only pass ran
        # first. Events are scoped into the sortie that owns them instead.
        #
        # bake_maps is not here either. It made the index-only and full builds
        # disagree by construction, so neither could ever reuse the other's
        # work. Whether a cached doc still needs maps is answered by looking
        # at the doc, below.
        "places:" + file_sig(PLACES_JSON),
        # A cached sortie must not survive a change to what is hidden.
        "grading:" + grading_revision(),
        "prose:" + file_sig(os.path.join(BASE, "passenger_lines.json")),
        "ss:%d" % getattr(mapbake, "SUPERSAMPLE", 1),
        # Everything that changes how a map looks belongs here. Without it a
        # canvas or style change leaves every cached sortie valid and the old
        # picture on disk for ever - the same trap that kept blank basemaps
        # alive until the network gate was fixed.
        "canvas:%s/%s/%s" % (getattr(mapbake, "MAP_LONG_SIDE_LEG", 0),
                             getattr(mapbake, "MAP_LONG_SIDE_SORTIE", 0),
                             getattr(mapbake, "MAP_ASPECT_MIN", 0)) +
          "/%s" % getattr(mapbake, "MAP_ASPECT_MAX", 0),
        "style:%s" % getattr(mapbake, "MAP_STYLE", ""),
        "tiles:%s" % _tile_source_name(),
    ])


def _event_sig(label, events):
    """Sign a whole event, not three of its fields.

    time|kind|leg was the old blob. Derivation reads more than that - the
    touchdown rate, the clip a leg is anchored to, the coordinates a route is
    named from - so an event could be corrected in place and the sortie built
    from it stay cached. Canonical JSON costs a hash of a few hundred bytes
    per sortie and removes the whole class.
    """
    if not events:
        return "%s:0" % label
    blob = ";".join(json.dumps(e, sort_keys=True, default=str) for e in events)
    return "%s:%s:%s" % (label, len(events),
                         hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12])


def sortie_signature(group, gsig, events_by_flight=None, sortie_id=None,
                     prefs=None, exclusions=None):
    """A sortie is unchanged when every flight record behind it is unchanged.

    That includes the events belonging to this sortie's own fragments - and
    only those, so a landing on one outing does not invalidate the rest.

    The rating and passenger switches come in RESOLVED rather than as a file
    signature, for the same reason events do: flight_prefs.json is one file for
    every flight, so signing the file rebuilt all 8 sorties when one switch
    moved. Resolved values also cover a change of default on their own, since
    that changes the resolved value of every undecided flight and nothing else.
    """
    parts = [gsig]
    exclusions = exclusions if exclusions is not None else load_exclusions()
    parts.append(json.dumps({"sortie": exclusions.get("sorties", {}).get(sortie_id),
                             "legs": exclusions.get("legs", {}).get(sortie_id)},
                            sort_keys=True))
    if prefs:
        parts.append("pref:%d%d" % (bool(prefs.get("rating")),
                                    bool(prefs.get("passenger"))))
    idx = events_by_flight or {}
    # An event logged against a fragment too small to be a group of its own
    # still belongs to this outing when it names the sortie, so look under both
    # keys rather than only the fragment id.
    keys = [f["flight_id"] for f in group]
    if sortie_id and sortie_id not in keys:
        keys.append(sortie_id)
    seen_ids = set()
    for f in group:
        parts.append("%s:%s" % (f["flight_id"],
                                file_sig(os.path.join(BASE, f["jsonl"].replace("/", os.sep)))))
        # The meta as well as the track. scan_flights already signs both, so a
        # corrected category or vs0 refreshed the flight record - and then the
        # cached sortie, keyed on the track alone, won anyway and kept the
        # grade the old profile produced. Measured on a fixture: moving vs0
        # across the 61 kt profile boundary left this signature byte-identical.
        parts.append("meta:%s" % file_sig(
            os.path.join(SESSIONS, f["flight_id"] + ".meta.json")))
        evs = idx.get(f["flight_id"]) or []
        parts.append(_event_sig("ev", evs))
        seen_ids.add(f["flight_id"])
    extra = [e for k in keys if k not in seen_ids for e in (idx.get(k) or [])]
    if extra:
        parts.append(_event_sig("evs", extra))
    # Contents of a referenced clip can be corrected without moving its event.
    clips = {clip_id_from_event(e) for k in keys for e in (idx.get(k) or [])}
    for cid in sorted(c for c in clips if c):
        parts.append("clip:%s:%s" % (cid, file_sig(_clip_path(cid))))
    return "|".join(parts)


def carry_maps_forward(doc, prev):
    """Keep a rebuilt sortie's existing maps when this run is not baking.

    An index-only rebuild produces a doc with no map fields. Letting that
    overwrite the cached doc threw away perfectly good PNGs that are still on
    disk, made the UI report that no maps existed, and left the next full build
    to bake them all again. The pictures have not changed - only the index has.
    """
    if not isinstance(prev, dict):
        return doc

    def keep(dst, src):
        rel = src.get("map")
        if not rel:
            return
        if not os.path.isfile(os.path.join(BASE, rel.replace("/", os.sep))):
            return
        dst["map"] = rel
        if "map_basemap" in src:
            dst["map_basemap"] = src["map_basemap"]

    keep(doc, prev)
    prev_legs = {leg_key(l): l for l in (prev.get("legs") or [])}
    for leg in doc.get("legs") or []:
        old_leg = prev_legs.get(leg_key(leg))
        if old_leg:
            keep(leg, old_leg)
    return doc


def maps_missing(doc):
    """A cached sortie that should carry maps but does not.

    Index-only builds legitimately produce docs without them, so this is what
    tells a later full build which sorties still owe a bake - instead of
    invalidating the whole book through the signature.
    """
    if (doc.get("points") or 0) >= 2 and not doc.get("map"):
        return True
    for leg in doc.get("legs") or []:
        if (leg.get("points") or 0) >= 2 and not leg.get("map"):
            return True
    return False


def detail_rel(sortie_id):
    return "sessions/detail/%s.json" % sortie_id


def maps_missing_basemap(doc):
    """True if a baked map in this sortie went out with no OSM backing.

    That happens when the bake ran while the aircraft was flying and the tile
    cache could not be filled. Nothing about the recording changes afterwards,
    so the signature stays identical and the blank map would be reused for
    ever - re-bake it the first time the network is available again.
    """
    if doc.get("map") and not doc.get("map_basemap"):
        return True
    for leg in doc.get("legs") or []:
        if leg.get("map") and not leg.get("map_basemap"):
            return True
    return False


def cached_maps_present(doc):
    """A cache hit is only valid if the files it points at still exist."""
    for rel in [doc.get("map")] + [l.get("map") for l in doc.get("legs", [])]:
        if rel and not os.path.isfile(os.path.join(BASE, rel.replace("/", os.sep))):
            return False
    d = os.path.join(BASE, detail_rel(doc.get("sortie_id") or "").replace("/", os.sep))
    return os.path.isfile(d)


def summarize_sortie(sortie):
    """What the browse view needs: no tracks, no legs, no prose.

    Roughly 400 bytes per sortie instead of 16 KB, so the index stays small
    however much history builds up.
    """
    legs = sortie.get("legs") or []
    # The row now shows each leg's overall grade, not just how it landed.
    # Falls back to the landing grade for legs with too little track to
    # judge, so a sortie never loses its pills.
    grades = [l.get("grade") or l.get("landing_grade") for l in legs
              if (l.get("grade") or l.get("landing_grade"))]
    scores = [l.get("score") for l in legs if l.get("score") is not None]
    started = sortie.get("started_at") or ""
    first_from = None
    last_to = None
    for l in legs:
        r = l.get("route") or {}
        if first_from is None and r.get("from"):
            first_from = r.get("from")
        if r.get("to"):
            last_to = r.get("to")
    has_clip = any((l.get("takeoff") or {}).get("clip_id")
                   or (l.get("landing") or {}).get("clip_id") for l in legs)
    return {
        "sortie_id": sortie.get("sortie_id"),
        "date": started[:10],
        "started_at": started,
        "ended_at": sortie.get("ended_at"),
        "aircraft": sortie.get("aircraft"),
        "status": sortie.get("status"),
        "end_reason": sortie.get("end_reason"),
        "fragments": sortie.get("fragments"),
        "airborne_s": sortie.get("airborne_s"),
        "distance_nm": sortie.get("distance_nm"),
        "max_alt_ft": sortie.get("max_alt_ft"),
        "max_gs_kt": sortie.get("max_gs_kt"),
        "landings": sortie.get("landings"),
        "bounced": sum(1 for l in legs
                       if ((l.get("landing") or {}).get("bounces") or 0) > 0),
        "legs": len(legs),
        "grades": grades,
        "scores": scores,
        "route_from": first_from,
        "route_to": last_to,
        "edited": bool(sortie.get("edited")),
        "prefs": sortie.get("prefs") or {"rating": True, "passenger": True},
        "has_clips": has_clip,
        "map": sortie.get("map"),
        "detail": detail_rel(sortie.get("sortie_id")),
    }


@persistence.serialized(lambda: os.path.dirname(CACHE_JSON))
def build(bake_maps=True, log=None, allow_network=True, force=False, should_abort=None):
    """Rebuild logbook.json (and the map PNGs). Returns the document.

    allow_network=False keeps the basemap to tiles already cached, which is
    what the watcher passes while a flight is live.
    """

    def say(msg):
        if log:
            try:
                log("logbook: " + msg)
            except Exception:
                pass

    def check():
        if should_abort and should_abort():
            raise BuildDeferred("flight started during maintenance")
    check()
    t_begin = time.time()
    excluded = load_exclusions()
    prefs_doc = flightprefs.load()
    hidden_sorties = excluded["sorties"]
    hidden_legs = excluded["legs"]
    hidden_out = []
    places = load_places()
    flights = scan_flights(force=force)
    groups = group_sorties(flights)
    events = load_events()
    events_by_flight = {}
    for _e in events:
        events_by_flight.setdefault(_e.get("flight_id"), []).append(_e)
        _sid = _e.get("sortie_id")
        if _sid and _sid != _e.get("flight_id"):
            events_by_flight.setdefault(_sid, []).append(_e)

    by_id = {f["flight_id"]: f for f in flights}
    sorties_out = []

    # Re-rendering every map on every rebuild was 84% of the cost, and it grew
    # with the size of the logbook rather than with what was just flown. A
    # sortie whose flight records have not changed is reused wholesale: no
    # track re-read, no re-render.
    cache = read_json(CACHE_JSON) or {}
    cached_sorties = cache.get("sorties") if isinstance(cache, dict) else None
    if not isinstance(cached_sorties, dict) or force:
        cached_sorties = {}
    fresh_sorties = {}
    gsig = global_signature(bake_maps)
    revision = grading_revision()
    reused = 0

    for group in groups:
        if should_abort and should_abort():
            raise BuildDeferred("flight started during maintenance")
        ids = [f["flight_id"] for f in group]
        # Prefer the id the watcher recorded; every fragment in the group
        # carries the same one, and it is the first fragment's flight id.
        sortie_id = next((g.get("sortie_id") for g in group if g.get("sortie_id")),
                         None) or ids[0]
        starts = [f["t_start"] for f in group if f["t_start"] is not None]
        ends = [f["t_end"] for f in group if f["t_end"] is not None]
        t_start = min(starts) if starts else None
        t_end = max(ends) if ends else None
        aircraft = next((f.get("aircraft") for f in reversed(group) if f.get("aircraft")), None)
        # Recorded by the watcher from the sim itself. Absent on every flight
        # that carries none, in which case grade_leg infers it from the
        # flying.
        category = next((f.get("category") for f in reversed(group)
                         if f.get("category")), None)
        vs0_kt = next((f.get("vs0") for f in reversed(group)
                       if f.get("vs0") is not None), None)
        # What this flight is set to. Both default on; the switches gate what
        # is emitted, never what was recorded.
        prefs = flightprefs.for_sortie(sortie_id, prefs_doc)

        if sortie_id in hidden_sorties:
            info = hidden_sorties.get(sortie_id) or {}
            hidden_out.append({
                "scope": "sortie", "sortie_id": sortie_id,
                "flight_ids": ids,
                "aircraft": next((f.get("aircraft") for f in reversed(group)
                                  if f.get("aircraft")), None),
                "date": iso_local(min([f["t_start"] for f in group
                                       if f["t_start"] is not None] or [None]) or 0) or "",
                "hidden_at": info.get("at"),
            })
            continue

        sig = sortie_signature(group, gsig, events_by_flight, sortie_id,
                               prefs=prefs, exclusions=excluded)
        hit = cached_sorties.get(sortie_id)
        if (isinstance(hit, dict) and hit.get("sig") == sig
                and isinstance(hit.get("doc"), dict) and cached_maps_present(hit["doc"])
                # Signature alone no longer says whether maps are present, so
                # ask the doc: a sortie built index-only still owes a bake.
                and not (bake_maps and maps_missing(hit["doc"]))
                # Resolved, not tested for truth: the watcher passes a
                # callable here so permission is re-asked per request, and a
                # callable is always truthy. Left bare, this rejected a cached
                # sortie for a missing basemap while the gate below was
                # refusing the very fetch that would fill it - a rebake that
                # could not succeed, on the path where frames matter most.
                and not (tiles.net_allowed(allow_network) and bake_maps
                         and maps_missing_basemap(hit["doc"]))):
            sorties_out.append(hit["doc"])
            fresh_sorties[sortie_id] = hit
            reused += 1
            continue

        track = merge_tracks([
            read_track(os.path.join(BASE, f["jsonl"].replace("/", os.sep))) for f in group
        ])

        id_set = set(ids)
        sortie_events = [e for e in events if e.get("flight_id") in id_set]
        raw_legs = pair_legs(sortie_events)
        legs_derived = False
        if not raw_legs:
            # No event log for this sortie - the watcher was not running
            # for it, or its entries were removed. Recover from the track.
            raw_legs = synth_events_from_track(track, sortie_id)
            legs_derived = bool(raw_legs)
            if legs_derived:
                say("%s: %d leg(s) derived from track, no events" % (sortie_id, len(raw_legs)))

        legs_out = []
        # Slot -> the last few indices used, newest last, so a run of similar
        # legs (circuits especially) does not cycle through the same handful
        # of lines. Carried across the whole sortie, not just one leg back.
        recent_picks = {}
        for i, pair in enumerate(raw_legs, start=1):
            to_e, ld_e = pair.get("takeoff"), pair.get("landing")
            # Bound here rather than further down: the rate recovery below
            # needs it, and it used to be defined after its first use.
            ld_clip = clip_id_from_event(ld_e) if ld_e else None
            t0 = to_e["t"] if to_e else None
            t1 = ld_e["t"] if ld_e else None
            # A leg with no landing (the sim quit, or the next takeoff came
            # first) must still stop somewhere, or it swallows the rest of the
            # sortie and reports another leg's distance as its own.
            t_bound = t1
            if t_bound is None:
                nxt = raw_legs[i] if i < len(raw_legs) else None
                nxt_to = (nxt or {}).get("takeoff")
                if nxt_to is not None:
                    t_bound = nxt_to["t"]
            seg = slice_track(track, t0, t_bound)
            stats = summarize_track(sortie_id, seg)

            # abs(): events written before the watcher settled on a
            # magnitude still hold a signed vertical speed. The event log
            # is ground truth and is not rewritten, so it is normalized
            # here instead.
            rate = (abs(float(ld_e["rate_fpm"]))
                    if (ld_e and _finite(ld_e.get("rate_fpm"))) else None)
            # A stored rate of exactly zero is the sim's unlatched touchdown
            # reading, not a landing with no vertical speed. Recover it from
            # the clip so old landings stop showing as 0 fpm and grade A.
            #
            # And when the clip cannot supply one, the zero must not simply be
            # left standing: grade_for_rate(0.0) returns ("A", "Butter"), so an
            # unrecoverable reading was being awarded the best landing grade in
            # the book. That is the failure this project refuses everywhere
            # else - a missing measurement must never read as a flawless one -
            # and it was reachable by deleting a clip, which the UI offers.
            #
            # None is the answer, not zero. It is already what a leg with no
            # landing event carries, so every consumer below already guards it
            # and the UI already renders it as a dash rather than a number.
            phase_grade = None
            rate_recovered = False
            if rate is not None and abs(rate) < 1e-6:
                recovered = clip_touchdown_rate_fpm(ld_clip)
                if recovered is not None:
                    rate = recovered
                    rate_recovered = True
                else:
                    rate = None
            grade, grade_name = passenger.grade_for_rate(rate)
            # A gentle arrival that is still sliding sideways is not a good
            # landing. Alignment can only hold the letter DOWN, never lift it,
            # and is None for any track without the lateral accelerations -
            # those keep the letter the rate alone gave them, because
            # "not measured" must not read as "arrived square".
            alignment = None
            try:
                # The whole track, not seg: seg stops at touchdown and the
                # scrub being measured is in the seconds after it. t1 is what
                # picks this leg's landing out of a multi-leg sortie.
                alignment = grading.landing_alignment_for(
                    track, aircraft, category=category, vs0_kt=vs0_kt,
                    t_land=t1)
                held = grading.worse_letter(
                    grade, grading.alignment_ceiling_letter(alignment))
                if alignment and held and held != grade:
                    alignment["held_from"] = grade
                    grade = held
                    grade_name = passenger.name_for_grade(held) or grade_name
            except Exception as e:
                # Same rule as phase grading: a bug here must not cost the
                # whole logbook, and the landing keeps its rate-only letter.
                say("%s leg %d: alignment failed %r" % (sortie_id, i, e))
                alignment = None
            # Graded after the rate is settled, so a recovered touchdown
            # feeds the descent phase rather than the latched zero.
            try:
                # Still computed when only the notes are wanted: assess()
                # picks its wording from the grades. It is dropped from the
                # leg below rather than never worked out.
                if not (prefs["rating"] or prefs["passenger"]):
                    phase_grade = None
                else:
                    phase_grade = grading.grade_leg(
                        seg, rate, aircraft, category=category, vs0_kt=vs0_kt,
                        alignment=alignment)
            except Exception as e:
                # A grading bug must not cost the whole logbook.
                say("%s leg %d: phase grading failed %r" % (sortie_id, i, e))
                phase_grade = None

            if t0 is not None and t1 is not None and t1 > t0:
                airborne_s = round(t1 - t0, 1)
            else:
                airborne_s = stats["airborne_s"] or None

            to_clip = clip_id_from_event(to_e) if to_e else None

            from_name = name_point(to_e.get("lat"), to_e.get("lon"), places) if to_e else None
            to_name = name_point(ld_e.get("lat"), ld_e.get("lon"), places) if ld_e else None

            leg = {
                "seq": i,
                "sortie_id": sortie_id,
                "aircraft": aircraft,
                "airborne_s": airborne_s,
                "distance_nm": stats["distance_nm"],
                "max_alt_ft": stats["max_alt_ft"],
                "max_gs_kt": stats["max_gs_kt"],
                "peak_vs_fpm": stats["peak_vs_fpm"],
                "landing_rate_fpm": round(rate, 1) if rate is not None else None,
                "landing_grade": grade,
                "landing_grade_name": grade_name,
                # None means the track carries no lateral accelerations,
                # not that the aircraft arrived square.
                "landing_alignment": alignment,
                # Per-phase grading. seg is this leg's own slice of the
                # track, the only place cruise is visible: clips cover the
                # takeoff and landing windows and nothing in between.
                "phase_grade": phase_grade,
                "grading_revision": revision,
                # The headline for the leg. Falls back to the touchdown
                # grade when there was not enough track to judge phases.
                "grade": (phase_grade or {}).get("letter") or grade,
                "score": (phase_grade or {}).get("overall"),
                "ride_grade": (phase_grade or {}).get("ride_letter"),
                "profile": (phase_grade or {}).get("profile"),
                "profile_label": (phase_grade or {}).get("profile_label"),
                "route": {
                    "from": from_name or (coord_label(to_e.get("lat"), to_e.get("lon")) if to_e else None),
                    "to": to_name or (coord_label(ld_e.get("lat"), ld_e.get("lon")) if ld_e else None),
                    "from_named": bool(from_name),
                    "to_named": bool(to_name),
                },
                "takeoff": None,
                "landing": None,
                "derived": legs_derived,
                "track": mapbake.polyline(drawable(seg), max_points=LEG_TRACK_POINTS),
            }
            # Ratings off: every judgment leaves the leg. The landing rate
            # stays - that is a measurement, not an opinion, and the UI still
            # shows the fpm it touched down at.
            if not prefs["rating"]:
                for k in ("landing_grade", "landing_grade_name", "phase_grade",
                          "grade", "score", "ride_grade", "profile",
                          "profile_label"):
                    leg[k] = None
            if to_e:
                to_ok = usable_fix(to_e.get("lat"), to_e.get("lon"))
                leg["takeoff"] = {
                    "at": iso_local(t0),
                    "at_utc": to_e.get("time"),
                    "lat": to_e.get("lat") if to_ok else None,
                    "lon": to_e.get("lon") if to_ok else None,
                    "flight_id": to_e.get("flight_id"),
                    "raw_leg": to_e.get("leg"),
                    "clip_id": to_clip if clip_exists(to_clip) else None,
                    # A takeoff recording can be cut short exactly as a
                    # landing one can. Carrying the status for only half of
                    # them meant a truncated takeoff replayed short with
                    # nothing anywhere saying why.
                    "clip_status": clip_status(to_clip) if to_clip else None,
                }
            if ld_e:
                ld_ok = usable_fix(ld_e.get("lat"), ld_e.get("lon"))
                leg["landing"] = {
                    "at": iso_local(t1),
                    "at_utc": ld_e.get("time"),
                    "lat": ld_e.get("lat") if ld_ok else None,
                    "lon": ld_e.get("lon") if ld_ok else None,
                    "flight_id": ld_e.get("flight_id"),
                    "raw_leg": ld_e.get("leg"),
                    "rate_fpm": round(rate, 1) if rate is not None else None,
                    # How many times it touched, and how hard each time. Absent
                    # on a landing whose track does not carry them.
                    "bounces": ld_e.get("bounces"),
                    # What the recording behind this landing looks like.
                    # None when there is no clip, which is ordinary.
                    "clip_status": clip_status(ld_clip) if ld_clip else None,
                    "contact_rates_fpm": _recovered_contacts(
                        ld_e.get("contact_rates_fpm"), rate, rate_recovered),
                    "bounce_heights_ft": ld_e.get("bounce_heights_ft"),
                    "bounce_detail": ld_e.get("bounce_detail"),
                    "contacts_derived": None,
                    "clip_id": ld_clip if clip_exists(ld_clip) else None,
                }

            # A landing with no recorded bounce count still has the
            # evidence in the track; recover it rather than leave the leg
            # blank. Never overwrites what the watcher recorded, and never
            # touches the grade.
            ld = leg.get("landing")
            if ld and ld.get("bounces") is None:
                derived = derive_contacts(ld_clip)
                if derived:
                    ld.update(derived)

            leg["passenger"] = None if not prefs["passenger"] else passenger.assess(
                sortie_id, i,
                aircraft=aircraft,
                grade=grade,
                rate_fpm=rate,
                duration_s=airborne_s,
                peak_vs_fpm=stats["peak_vs_fpm"],
                # Three grades for three sentences: the touchdown colors
                # the landing line, the flying colors the ride line, the
                # leg sets the closing tone. One grade for all three is
                # what let the prose contradict the pills beside it.
                ride_grade=(phase_grade or {}).get("ride_letter"),
                overall_grade=(phase_grade or {}).get("letter") or grade,
                # So the landing line can say "soft, then it slid" rather
                # than describing a firmness the touchdown never had.
                held_from=(alignment or {}).get("held_from"),
                avoid=recent_picks,
            )

            if should_abort and should_abort():
                raise BuildDeferred("flight started during maintenance")
            if bake_maps and len(drawable(seg)) >= 2:
                rel = "sessions/maps/%s-leg%d.png" % (sortie_id, i)
                out = os.path.join(BASE, rel.replace("/", os.sep))
                try:
                    route = leg.get("route") or {}
                    # Only name the ends when the place is really named.
                    from_place = route.get("from") if route.get("from_named") else None
                    to_place = route.get("to") if route.get("to_named") else None
                    res = mapbake.bake_track_png(
                        seg, out, long_side=mapbake.MAP_LONG_SIDE_LEG,
                        label="%s leg %d" % (sortie_id, i),
                        legend=map_legend("%s  leg %d" % (sortie_id, i), aircraft,
                                          leg.get("distance_nm"),
                                          leg.get("airborne_s")),
                        start_label=from_place or "", end_label=to_place or "",
                        start_at=event_point(leg, "takeoff"),
                        end_at=event_point(leg, "landing"),
                        base_dir=BASE, allow_network=allow_network, log=say, check=check)
                    if res:
                        leg["map"] = rel
                        leg["map_basemap"] = bool(res.get("basemap"))
                except persistence.MaintenanceDeferred:
                    raise
                except Exception as exc:
                    say("leg map failed %s leg%d %r" % (sortie_id, i, exc))
            key = leg_key(leg)
            if key in (hidden_legs.get(sortie_id) or {}):
                info = (hidden_legs.get(sortie_id) or {}).get(key) or {}
                hidden_out.append({
                    "scope": "leg", "sortie_id": sortie_id, "leg_key": key,
                    "aircraft": aircraft,
                    "date": (leg.get("takeoff") or {}).get("at")
                            or (leg.get("landing") or {}).get("at") or "",
                    "distance_nm": leg.get("distance_nm"),
                    "airborne_s": leg.get("airborne_s"),
                    "grade": leg.get("landing_grade"),
                    "hidden_at": info.get("at"),
                    # what a permanent delete would have to remove
                    "flight_ids": ids,
                    "t0_utc": (leg.get("takeoff") or {}).get("at_utc"),
                    "t1_utc": (leg.get("landing") or {}).get("at_utc"),
                    "clips": [c for c in ((leg.get("takeoff") or {}).get("clip_id"),
                                          (leg.get("landing") or {}).get("clip_id")) if c],
                })
                say("%s: leg %s hidden" % (sortie_id, key))
                continue

            if not is_real_leg(leg, to_e, ld_e):
                say("%s: dropped phantom leg %d (takeoff at the spawn slot, "
                    "no landing, no track)" % (sortie_id, i))
                continue
            leg["seq"] = len(legs_out) + 1
            leg["key"] = key
            for _slot, _idx in ((leg.get("passenger") or {}).get("picks")
                                or {}).items():
                seen = recent_picks.setdefault(_slot, [])
                seen.append(_idx)
                del seen[:-passenger.AVOID_RECENT]
            legs_out.append(leg)

        s_stats = summarize_track(sortie_id, track)

        # Hiding a leg has to move everything derived from it. The sortie's own
        # figures come from the whole recorded track, which still contains the
        # hidden flying, so recompute them from the legs that remain - and draw
        # the map from those segments only, kept separate so the picture does
        # not bridge the gap with a line that was never flown.
        kept_keys = set(l.get("key") for l in legs_out)
        dropped_here = [h for h in hidden_out
                        if h.get("scope") == "leg" and h.get("sortie_id") == sortie_id]
        edited = bool(dropped_here)
        segments = None
        if edited and legs_out:
            segments = []
            for l in legs_out:
                t0 = parse_ts((l.get("takeoff") or {}).get("at_utc"))
                t1 = parse_ts((l.get("landing") or {}).get("at_utc"))
                seg = drawable(slice_track(track, t0, t1))
                if len(seg) >= 2:
                    segments.append(seg)
            s_stats = {
                "distance_nm": round(sum(l.get("distance_nm") or 0.0 for l in legs_out), 4),
                "airborne_s": round(sum(l.get("airborne_s") or 0.0 for l in legs_out), 1),
                "max_alt_ft": max([l.get("max_alt_ft") for l in legs_out
                                   if l.get("max_alt_ft") is not None] or [None]),
                "max_gs_kt": max([l.get("max_gs_kt") for l in legs_out
                                  if l.get("max_gs_kt") is not None] or [None]),
            }
        elif edited:
            segments = []
            s_stats = {"distance_nm": 0.0, "airborne_s": 0.0,
                       "max_alt_ft": None, "max_gs_kt": None}

        sortie = {
            "sortie_id": sortie_id,
            "flight_ids": ids,
            "aircraft": aircraft,
            "started_at": iso_local(t_start),
            "ended_at": iso_local(t_end),
            "started_at_unix": t_start,
            "airborne_s": s_stats["airborne_s"],
            "distance_nm": s_stats["distance_nm"],
            "max_alt_ft": s_stats["max_alt_ft"],
            "max_gs_kt": s_stats["max_gs_kt"],
            "points": len(track),
            "fragments": len(ids),
            "status": "closed" if any(by_id[i].get("meta_ended_at") for i in ids) else "open",
            "end_reason": next((by_id[i].get("end_reason") for i in reversed(ids)
                                if by_id[i].get("end_reason")), None),
            "legs": legs_out,
            "landings": sum(1 for l in legs_out if l.get("landing")),
            "edited": edited,
            # Carried so the UI can draw the switches from the index alone,
            # without opening the detail file for every row.
            "prefs": prefs,
            "track": (mapbake.polyline_segments(segments, max_points=SORTIE_TRACK_POINTS)
                      if segments is not None
                      else mapbake.polyline(drawable(track), max_points=SORTIE_TRACK_POINTS)),
        }
        map_source = segments if segments is not None else drawable(track)
        drawable_points = (sum(len(x) for x in map_source) if segments is not None
                           else len(map_source))
        # Ends of the whole sortie: where the first leg departed and where the
        # last one arrived, so the overview names the same pads its legs do.
        def _named(route, key):
            return route.get(key) if route.get("%s_named" % key) else None
        first_place = (_named(legs_out[0].get("route") or {}, "from") or ""
                       if legs_out else None)
        last_place = (_named(legs_out[-1].get("route") or {}, "to") or ""
                      if legs_out else None)
        # Where the aircraft stopped between legs. One marker per place: a leg
        # ends and the next begins at the same pad, so the landing position
        # alone says "there was a stop here". The final landing is the LANDING
        # marker and the first takeoff is START, so neither is repeated.
        stops = []
        for leg in legs_out[:-1]:
            here = event_point(leg, "landing")
            if here and not any(haversine_nm(here[0], here[1], p[0], p[1]) < 0.05
                                for p in stops):
                stops.append(here)
        if should_abort and should_abort():
            raise BuildDeferred("flight started during maintenance")
        if bake_maps and drawable_points >= 2:
            rel = "sessions/maps/%s-sortie.png" % sortie_id
            out = os.path.join(BASE, rel.replace("/", os.sep))
            try:
                res = mapbake.bake_track_png(
                    map_source, out, long_side=mapbake.MAP_LONG_SIDE_SORTIE,
                    label="%s sortie" % sortie_id, max_points=2000,
                    legend=map_legend(sortie_id, sortie.get("aircraft"),
                                      sortie.get("distance_nm"),
                                      sortie.get("airborne_s")),
                    start_label=first_place, end_label=last_place,
                    # The first takeoff and the last landing, not the ends of
                    # the recording - a merged reconnect can start the track
                    # well before the aircraft ever left the ground.
                    start_at=(event_point(legs_out[0], "takeoff") if legs_out else None),
                    end_at=(event_point(legs_out[-1], "landing") if legs_out else None),
                    stops=stops,
                    base_dir=BASE, allow_network=allow_network, log=say, check=check)
                if res:
                    sortie["map"] = rel
                    sortie["map_basemap"] = bool(res.get("basemap"))
            except persistence.MaintenanceDeferred:
                raise
            except Exception as exc:
                say("sortie map failed %s %r" % (sortie_id, exc))
        if not bake_maps:
            # Nothing was baked this pass, so anything the previous build drew
            # is still valid and still on disk.
            prev_doc = (hit or {}).get("doc") if isinstance(hit, dict) else None
            if prev_doc is None:
                prev_doc = read_json(os.path.join(
                    BASE, detail_rel(sortie_id).replace("/", os.sep)))
            carry_maps_forward(sortie, prev_doc)
        sorties_out.append(sortie)
        fresh_sorties[sortie_id] = {"sig": sig, "doc": sortie}

    sorties_out.sort(key=sortie_order_key, reverse=True)
    for s in sorties_out:
        s.pop("started_at_unix", None)

    # Full detail goes to its own file per sortie, fetched only when opened.
    os.makedirs(DETAIL_DIR, exist_ok=True)
    wanted_detail = set()
    for s in sorties_out:
        rel = detail_rel(s["sortie_id"])
        wanted_detail.add(os.path.basename(rel))
        path = os.path.join(BASE, rel.replace("/", os.sep))
        try:
            existing = read_json(path)
            if existing != s:
                atomic_write_json(path, s)
        except Exception as exc:
            say("detail write failed %s %r" % (s["sortie_id"], exc))
    try:
        for path in glob.glob(os.path.join(DETAIL_DIR, "*.json")):
            if os.path.basename(path) not in wanted_detail:
                os.remove(path)
                say("pruned stale detail %s" % os.path.basename(path))
    except Exception as exc:
        say("detail prune failed %r" % (exc,))

    summaries = [summarize_sortie(s) for s in sorties_out]

    airframes = {}
    for s in summaries:
        ac = s.get("aircraft") or "unknown"
        a = airframes.setdefault(ac, {
            "aircraft": ac, "sorties": 0, "legs": 0, "landings": 0,
            "distance_nm": 0.0, "airborne_s": 0.0,
            "max_alt_ft": None, "max_gs_kt": None,
            "latest_landing_grade": None, "latest_landing_rate_fpm": None,
        })
        a["sorties"] += 1
        a["legs"] += s["legs"]
        a["landings"] += s["landings"] or 0
        a["distance_nm"] += s["distance_nm"] or 0.0
        a["airborne_s"] += s["airborne_s"] or 0.0
        for key, val in (("max_alt_ft", s.get("max_alt_ft")), ("max_gs_kt", s.get("max_gs_kt"))):
            if val is not None:
                a[key] = val if a[key] is None else max(a[key], val)
        if a["latest_landing_grade"] is None and s.get("grades"):
            a["latest_landing_grade"] = s["grades"][0]
        a.setdefault("first_flight", s.get("date"))
        a["first_flight"] = min(a["first_flight"] or s.get("date"), s.get("date") or "")
        a["last_flight"] = max(a.get("last_flight") or "", s.get("date") or "")
    for a in airframes.values():
        a["distance_nm"] = round(a["distance_nm"], 2)
        a["airborne_s"] = round(a["airborne_s"])

    if bake_maps:
        # Regrouping a sortie renames its maps; drop the ones nothing points at.
        wanted = set()
        for s in sorties_out:
            if s.get("map"):
                wanted.add(os.path.basename(s["map"]))
            for leg in s["legs"]:
                if leg.get("map"):
                    wanted.add(os.path.basename(leg["map"]))
        try:
            for path in glob.glob(os.path.join(SESSIONS, "maps", "*.png")):
                if os.path.basename(path) not in wanted:
                    os.remove(path)
                    say("pruned stale map %s" % os.path.basename(path))
        except Exception as exc:
            say("map prune failed %r" % exc)

    pending = pending_purges()
    pending_keys = {purge_key(h) for h in pending}
    hidden_out = [h for h in hidden_out if purge_key(h) not in pending_keys] + pending
    if should_abort and should_abort():
        raise BuildDeferred("flight started during maintenance")
    doc = {
        "schema": SCHEMA,
        "generated_by": "watcher",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "timezone": local_tz_name(),
        # Whether maps are actually available to the UI, not whether this
        # particular run baked them. An index-only rebuild bakes nothing but
        # leaves every cached map in place, and reporting that as "no maps"
        # is how the UI came to claim Pillow was missing mid-flight.
        "maps_baked": bool(mapbake.HAVE_PIL
                           and not any(maps_missing(s) for s in sorties_out)),
        "pillow": bool(mapbake.HAVE_PIL),
        # True when a bake is still owed - the aircraft is flying, so only the
        # index was rebuilt and the maps follow once it is parked.
        "maps_pending": bool(mapbake.HAVE_PIL
                             and any(maps_missing(s) for s in sorties_out)),
        "maps_pending_basemap": sum(
            1 for s in sorties_out
            if (s.get("map") and not s.get("map_basemap"))
            or any(l.get("map") and not l.get("map_basemap") for l in s["legs"])
        ),
        "places_loaded": len(places),
        "totals": {
            "sorties": len(summaries),
            "legs": sum(s["legs"] for s in summaries),
            "landings": sum(s["landings"] or 0 for s in summaries),
            "distance_nm": round(sum(s["distance_nm"] or 0.0 for s in summaries), 2),
            "airborne_s": round(sum(s["airborne_s"] or 0.0 for s in summaries)),
            "days": len(set(s["date"] for s in summaries if s.get("date"))),
        },
        "airframes": sorted(airframes.values(), key=lambda a: -a["airborne_s"]),
        # One entry per month, each naming the file that holds its flights.
        "months": write_months(summaries, say),
        # Every flight, but only what filtering and search need. The rows
        # themselves live in the month files and are fetched when opened.
        "find": find_rows(summaries),
        "hidden": hidden_out,
    }
    atomic_write_json(LOGBOOK_JSON, doc)
    try:
        cache_doc = read_json(CACHE_JSON) or {}
        if not isinstance(cache_doc, dict):
            cache_doc = {}
        cache_doc["sorties"] = fresh_sorties
        atomic_write_json(CACHE_JSON, cache_doc)
    except Exception as exc:
        say("cache write failed %r" % (exc,))
    say("rebuilt %d sorties / %d legs in %.2fs (%d reused from cache)"
        % (len(sorties_out), doc["totals"]["legs"], time.time() - t_begin, reused))
    try:
        say("index %.1f KB, detail %.1f KB across %d files"
            % (os.path.getsize(LOGBOOK_JSON) / 1024.0,
               sum(os.path.getsize(p) for p in glob.glob(os.path.join(DETAIL_DIR, "*.json"))) / 1024.0,
               len(summaries)))
    except Exception:
        pass
    return doc


def _clip_path(clip_id):
    """For signing. Falls back to the write path so a clip that does not
    exist yet still has a stable name to sign as absent."""
    return (clipfile.clip_path(CLIPS_DIR, clip_id)
            or clipfile.write_path(CLIPS_DIR, clip_id))


def _size_of(paths):
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def plan_purge(entry):
    """What a permanent delete would destroy. Reads only; changes nothing."""
    scope = entry.get("scope")
    sortie_id = entry.get("sortie_id")
    flight_ids = entry.get("flight_ids") or []
    files = []
    notes = []

    if scope == "sortie":
        for fid in flight_ids:
            files.append(os.path.join(SESSIONS, fid + ".jsonl"))
            files.append(os.path.join(SESSIONS, fid + ".meta.json"))
        for cid, paths in clipfile.iter_clip_files(CLIPS_DIR):
            if any(cid.startswith(fid + "-") for fid in flight_ids):
                # Every file this id occupies, not just the one a reader
                # would prefer: deleting one of two leaves the other on disk
                # after the user was told the recording was destroyed.
                files.extend(paths)
        files.append(os.path.join(BASE, detail_rel(sortie_id).replace("/", os.sep)))
        files.extend(glob.glob(os.path.join(SESSIONS, "maps", "%s-*.png" % sortie_id)))
        notes.append("%d flight recording(s) erased" % len(flight_ids))
        notes.append("its takeoff and landing events removed from events.jsonl")
    elif scope == "leg":
        for cid in entry.get("clips") or []:
            files.extend(clipfile.paths_for(CLIPS_DIR, cid))
        notes.append("the recorded track between takeoff and landing is cut "
                     "out of the flight's .jsonl")
        notes.append("its events are removed from events.jsonl")
    else:
        return None

    files.extend(os.path.join(BASE, rel.replace("/", os.sep))
                 for rel in entry.get("purge_files") or [])
    files = list({os.path.normcase(os.path.abspath(f)): os.path.abspath(f)
                  for f in files if os.path.isfile(f)}.values())
    rels = [_rel_to_base(f) for f in files]
    return {"scope": scope, "sortie_id": sortie_id, "leg_key": entry.get("leg_key"),
            "files": rels, "bytes": _size_of(files), "notes": notes}


BuildDeferred = persistence.MaintenanceDeferred


def purge_key(entry):
    return json.dumps([entry.get("scope"), entry.get("sortie_id"),
                       entry.get("leg_key")], separators=(",", ":"))


def pending_purges():
    with persistence.document_lock(EXCLUDED_JSON):
        doc = read_json(EXCLUDED_JSON) or {}
        return list((doc.get("purges") or {}).values())


def pending_purge(scope, sortie_id, key=None):
    wanted = purge_key({"scope": scope, "sortie_id": sortie_id, "leg_key": key})
    return next((h for h in pending_purges() if purge_key(h) == wanted), None)


def _rewrite_events(drop):
    return persistence.filter_jsonl(EVENTS_JSONL, drop)


def _excise_track(jsonl_path, t0, t1):
    if t0 is None or t1 is None:
        return 0
    def drop(p):
        t = parse_ts(p.get("ts"))
        return t is not None and t0 <= t <= t1
    return persistence.filter_jsonl(jsonl_path, drop)


@persistence.serialized(lambda: os.path.dirname(CACHE_JSON))
def purge(entry, log=None):
    paths = [os.path.join(SESSIONS, fid + ".jsonl")
             for fid in entry.get("flight_ids") or []]
    try:
        with persistence.deleting(paths), persistence.document_lock(EXCLUDED_JSON):
            entry = pending_purge(entry.get("scope"), entry.get("sortie_id"),
                                  entry.get("leg_key")) or entry
            plan = plan_purge(entry)
            if plan is None:
                return {"ok": False, "error": "nothing to delete"}
            doc = read_json(EXCLUDED_JSON) or {"schema": 1, "sorties": {}, "legs": {}}
            hidden = (doc.get("sorties") or {}).get(entry.get("sortie_id")) if entry.get("scope") == "sortie" else ((doc.get("legs") or {}).get(entry.get("sortie_id")) or {}).get(entry.get("leg_key"))
            if hidden is None and not pending_purge(entry.get("scope"), entry.get("sortie_id"), entry.get("leg_key")):
                return {"ok": False, "error": "item is no longer hidden"}
            entry = dict(entry, purge_pending=True, purge_files=plan["files"])
            doc.setdefault("purges", {})[purge_key(entry)] = entry
            # Commit intent before deleting any byte. Survives exceptions/restarts.
            atomic_write_json(EXCLUDED_JSON, doc)
            return _purge(entry, log)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "retryable": True}


def _purge(entry, log=None):
    """Permanently delete what `entry` describes. There is no undo."""
    def say(m):
        if log:
            log("purge: " + m)

    plan = plan_purge(entry)
    if plan is None:
        return {"ok": False, "error": "nothing to delete"}

    scope = entry.get("scope")
    sortie_id = entry.get("sortie_id")
    flight_ids = set(entry.get("flight_ids") or [])
    deleted = []

    if scope == "leg":
        t0 = parse_ts(entry.get("t0_utc"))
        t1 = parse_ts(entry.get("t1_utc"))
        clips = set(entry.get("clips") or [])
        cut = 0
        for fid in flight_ids:
            cut += _excise_track(os.path.join(SESSIONS, fid + ".jsonl"), t0, t1)

        def drop_leg_event(e):
            cid = clipfile.id_from_path(e.get("clip") or "")
            if cid and cid in clips:
                return True
            if e.get("flight_id") in flight_ids and t0 is not None and t1 is not None:
                t = parse_ts(e.get("time"))
                return t is not None and (t0 - 1) <= t <= (t1 + 1)
            return False

        removed = _rewrite_events(drop_leg_event)
        say("leg %s: %d track points cut, %d events removed"
            % (entry.get("leg_key"), cut, removed))
    else:
        removed = _rewrite_events(lambda e: e.get("flight_id") in flight_ids)
        say("flight %s: %d events removed" % (sortie_id, removed))

    # Count what actually went, not what was planned to go. A Windows file
    # lock is enough to leave a recording on disk, and this used to report the
    # planned total either way.
    failed = []
    freed = 0
    for rel in plan["files"]:
        path = os.path.join(BASE, rel.replace("/", os.sep))
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        try:
            os.remove(path)
        except OSError as exc:
            say("could not delete %s (%s)" % (rel, exc))
            failed.append({"file": rel, "error": str(exc)})
            continue
        deleted.append(rel)
        freed += size

    # The exclusion entry is the commit step, so it is contingent on the
    # delete having happened. Removing it after a partial failure un-hid the
    # records that survived: the user was told the flight was destroyed and
    # then watched it reappear in the logbook.
    if not failed:
        doc = read_json(EXCLUDED_JSON)
        if isinstance(doc, dict):
            (doc.get("purges") or {}).pop(purge_key(entry), None)
            if scope == "sortie":
                (doc.get("sorties") or {}).pop(sortie_id, None)
            else:
                legs = (doc.get("legs") or {}).get(sortie_id) or {}
                legs.pop(entry.get("leg_key"), None)
                if not legs:
                    (doc.get("legs") or {}).pop(sortie_id, None)
            atomic_write_json(EXCLUDED_JSON, doc)
    else:
        say("%d file(s) survived; leaving this hidden so the delete can be "
            "retried" % len(failed))

    # the caches still describe the world as it was a moment ago
    try:
        cache = read_json(CACHE_JSON)
        if isinstance(cache, dict):
            cache["sorties"] = {}
            cache["flights"] = {}
            atomic_write_json(CACHE_JSON, cache)
    except Exception:
        pass

    say("deleted %d of %d file(s), %.1f KB"
        % (len(deleted), len(plan["files"]), freed / 1024.0))
    res = {"ok": not failed, "deleted": deleted, "bytes": freed,
           "planned_bytes": plan["bytes"], "failed": failed}
    if failed:
        # Retrying is safe: os.remove on an already-deleted file raises and is
        # recorded, and the events and track were rewritten before this point.
        res["error"] = ("%d of %d file(s) could not be deleted; the flight is "
                        "still hidden, and Delete can be run again"
                        % (len(failed), len(plan["files"])))
    return res


if __name__ == "__main__":
    import sys
    d = build(bake_maps="--no-maps" not in sys.argv, log=print,
              allow_network="--offline" not in sys.argv,
              force="--force" in sys.argv)
    print(json.dumps(d["totals"], indent=2))

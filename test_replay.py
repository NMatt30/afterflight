"""Pose lookup during replay: what the ghost is placed from.

interpolate_pose scanned from the first point on every call, so the cost grew
as playback advanced - measured on a real clip at 9.7 us a tenth of the way in,
37 us at the halfway mark and 73 us at 95%. It now bisects a timestamp index
built once per replay.

Two things have to stay true, and only one of them is about speed:

  The index must never change an answer. A ghost placed from a stale or
  mismatched index is a ghost somewhere the aircraft never was, which is the
  one class of replay bug that matters.

  The index and the point list must not drift apart. Precomputing is only safe
  because a replay holds one immutable list for its whole run.

Synthetic points throughout - no clip off disk, so this runs the same on a
fresh checkout with no flights in it.

    py -3 test_replay.py
"""
import math
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import watcher                                            # noqa: E402
watcher.log = lambda *a, **k: None      # never append to the operational log


def clip(n=1300, dt=0.1):
    """A plausible flown arc: turning, descending, decelerating."""
    pts = []
    for i in range(n):
        f = i / float(n)
        pts.append({
            "t": 1000.0 + i * dt,
            "lat": 40.0 + 0.02 * math.sin(f * 3.1),
            "lon": -105.0 + 0.02 * f,
            "alt": 6000.0 - 900.0 * f,
            "heading": (350.0 + 40.0 * f) % 360.0,
            "pitch": -2.0 + 3.0 * math.sin(f * 7.0),
            "bank": 12.0 * math.sin(f * 5.0),
            "gs": 110.0 - 40.0 * f,
            "vs": -600.0 + 200.0 * math.sin(f * 4.0),
            "on_ground": f > 0.97,
        })
    return pts


PTS = clip()
TS = watcher.clip_timestamps(PTS)
T0, T1 = TS[0], TS[-1]
PROBES = [T0 + (T1 - T0) * i / 2000.0 for i in range(2001)]


def reference_pose(points, t):
    """The linear scan this replaced, kept as an independent oracle.

    Comparing interpolate_pose against itself with and without an index
    proves nothing: both paths run the same bisect, so a wrong search is
    wrong on both sides and they agree. Two mutations - bisect_right for
    bisect_left, and dropping the max(1, ...) floor - passed a test written
    that way. This is a transcription of the algorithm from before the
    change, and it is the thing the new one has to match.
    """
    if not points:
        return None
    t0 = points[0].get("t") or 0.0
    t1 = points[-1].get("t") or t0
    if t <= t0:
        return dict(points[0])
    if t >= t1:
        return dict(points[-1])
    for i in range(1, len(points)):
        a = points[i - 1]
        b = points[i]
        ta = a.get("t") or 0.0
        tb = b.get("t") or ta
        if t > tb:
            continue
        span = tb - ta
        f = 0.0 if span <= 1e-6 else max(0.0, min(1.0, (t - ta) / span))
        out = dict(b)
        p = points[i - 2] if i >= 2 else a
        n = points[i + 1] if (i + 1) < len(points) else b
        tp = p.get("t") or ta
        tn = n.get("t") or tb
        smooth = watcher._even_spans(ta, tb, tp, tn)
        for key in ("lat", "lon", "alt", "pitch", "bank", "gs", "vs"):
            va, vb = a.get(key), b.get(key)
            vp, vn = p.get(key), n.get(key)
            if smooth and None not in (va, vb, vp, vn):
                try:
                    out[key] = watcher._catmull_rom_t(
                        float(vp), float(va), float(vb), float(vn),
                        tp, ta, tb, tn, t)
                    continue
                except Exception:
                    pass
            out[key] = watcher.interp_num(va, vb, f)
        ha, hb = a.get("heading"), b.get("heading")
        hp, hn = p.get("heading"), n.get("heading")
        if smooth and None not in (ha, hb, hp, hn):
            try:
                h1 = watcher.wrap_deg(ha)
                h0 = watcher._unwrap_near(h1, hp)
                h2 = watcher._unwrap_near(h1, hb)
                h3 = watcher._unwrap_near(h2, hn)
                out["heading"] = watcher.wrap_deg(
                    watcher._catmull_rom_t(h0, h1, h2, h3, tp, ta, tb, tn, t))
            except Exception:
                out["heading"] = watcher.interp_heading_deg(ha, hb, f)
        else:
            out["heading"] = watcher.interp_heading_deg(ha, hb, f)
        out["t"] = t
        out["on_ground"] = a.get("on_ground") if f < 0.5 else b.get("on_ground")
        return out
    return dict(points[-1])


def test_the_index_never_changes_an_answer():
    """The indexed lookup must match the scan it replaced, everywhere.

    Not "close enough": these are the numbers a ghost aircraft is positioned
    from, and the spline reads neighbours either side of the segment, so an
    off-by-one in the search moves the aircraft rather than raising.
    """
    diffs = [t for t in PROBES
             if watcher.interpolate_pose(PTS, t, TS) != reference_pose(PTS, t)]
    assert not diffs, (
        "%d of %d probes disagreed with the reference scan; first at t=%.4f"
        % (len(diffs), len(PROBES), diffs[0]))


def test_the_unindexed_path_agrees_too():
    """Callers that pass no index must get the same answers."""
    diffs = [t for t in PROBES
             if watcher.interpolate_pose(PTS, t) != reference_pose(PTS, t)]
    assert not diffs, (
        "%d probes disagreed when no index was supplied" % len(diffs))


def test_the_ends_and_the_outside_still_hold():
    """Before the first point, after the last, and exactly on a sample."""
    for t in (T0 - 5.0, T0, TS[1], TS[len(TS) // 2], TS[-2], T1, T1 + 5.0):
        assert watcher.interpolate_pose(PTS, t, TS) == reference_pose(PTS, t), (
            "indexed lookup disagrees with the reference scan at t=%.4f" % t)


def test_an_empty_clip_is_still_none():
    assert watcher.interpolate_pose([], 1000.0) is None
    assert watcher.interpolate_pose([], 1000.0, []) is None


def test_a_single_point_clip_does_not_bisect_off_the_end():
    one = PTS[:1]
    ts = watcher.clip_timestamps(one)
    for t in (999.0, one[0]["t"], 1001.0):
        assert watcher.interpolate_pose(one, t, ts) == dict(one[0])


def test_the_index_describes_the_list_it_was_built_from():
    """The two must not drift apart.

    Precomputing is safe only because a replay holds one immutable list for
    its whole run. If a caller ever starts appending to a clip mid-playback,
    the index has to be rebuilt with it - this pins the shape so that change
    cannot be made silently.
    """
    assert len(TS) == len(PTS)
    for i, p in enumerate(PTS):
        assert TS[i] == p.get("t"), (
            "index and points disagree at %d: %r vs %r" % (i, TS[i], p.get("t")))


def test_the_replay_loop_builds_an_index_when_it_is_not_given_one():
    """The signature keeps ts optional, so it must default rather than fail."""
    import inspect
    params = inspect.signature(watcher._replay_loop).parameters
    assert "ts" in params, "_replay_loop lost its index parameter"
    assert params["ts"].default is None, (
        "ts must default to None so the loop builds its own")
    src = inspect.getsource(watcher._replay_loop)
    assert "clip_timestamps(points)" in src, (
        "the replay loop no longer builds an index from the list it was "
        "handed; passing a stale one would place the ghost from the wrong "
        "timestamps")


def test_lookup_cost_no_longer_grows_with_position():
    """The defect was the shape of the cost, not its size.

    A flat cost is the whole point: the old scan was cheap at the start of a
    clip and worst at the end, which is exactly the wrong way round for a
    landing. Timing is machine-dependent, so this asserts the shape - the end
    of a clip must not cost multiples of the start - rather than any absolute
    figure.
    """
    def at(frac):
        t = T0 + (T1 - T0) * frac
        watcher.interpolate_pose(PTS, t, TS)
        runs = []
        for _ in range(200):
            s = time.perf_counter()
            watcher.interpolate_pose(PTS, t, TS)
            runs.append(time.perf_counter() - s)
        return statistics.median(runs)

    early, late = at(0.05), at(0.95)
    assert late < early * 4.0, (
        "lookup at the end of a clip costs %.1fx what it costs at the start "
        "(%.2f us against %.2f us); the scan is back"
        % (late / early, late * 1e6, early * 1e6))


# --------------------------------------------------------------------------
# who owns a replay (SME review R3)
# --------------------------------------------------------------------------

class FakeGame(object):
    """A stand-in for the sim's own connection: records what it is asked to
    do, and can be made to wait - in open(), or in camera_set() - so two
    requests can be made to overlap exactly where they used to race."""
    instances = []
    gate = None            # open() waits on this when set
    spawn_none = False     # spawn_ghost() fails
    on_acquire = None      # called inside camera_acquire(), mid-start
    next_oid = [100]

    def __init__(self):
        self.dll_path = "stand-in"
        self.closed = False
        self.removed = []
        self.events = []
        self.oid = None
        self.block = None          # camera_set() waits on this when set
        self.blocked = False
        FakeGame.instances.append(self)

    def open(self, timeout=None, quiet=False):
        if FakeGame.gate is not None:
            FakeGame.gate.wait(5)
        return True

    def close(self):
        self.closed = True

    def spawn_ghost(self, title, pt, livery=None):
        if FakeGame.spawn_none:
            return None
        FakeGame.next_oid[0] += 1
        self.oid = FakeGame.next_oid[0]
        return self.oid

    def set_ghost_pose(self, oid, pt):
        return True

    def set_ghost_freeze(self, oid, on):
        return True

    def set_ghost_gear(self, oid, value):
        return True

    def set_ghost_flaps(self, oid, value):
        return True

    def camera_acquire(self, oid):
        self.events.append("acquire")
        if FakeGame.on_acquire is not None:
            FakeGame.on_acquire(self)
        return True

    def camera_set(self, oid, pose=None, aim_only=False):
        if self.block is not None:
            self.blocked = True
            self.block.wait(10)
        return True

    def camera_release(self):
        self.events.append("release")
        return True

    def remove_object(self, oid):
        self.removed.append(oid)


class Stage(object):
    """The real start_replay / stop_replay / worker, on FakeGame."""

    def __init__(self, paused=True):
        import types
        self.keep = {n: getattr(watcher, n) for n in (
            "GameSimConnect", "game_dll_status", "load_clip", "REPLAY_START_PAUSED")}
        with watcher.RUNTIME_LOCK:
            self.keep_rt = (watcher.RUNTIME["connected"], watcher.RUNTIME["sm"],
                            watcher.RUNTIME.get("camera_state"))
            watcher.RUNTIME["connected"] = True
            watcher.RUNTIME["sm"] = types.SimpleNamespace(quit=0)
            watcher.RUNTIME["camera_state"] = None
        FakeGame.instances = []
        FakeGame.gate = None
        FakeGame.spawn_none = False
        FakeGame.on_acquire = None
        watcher.GameSimConnect = FakeGame
        watcher.game_dll_status = lambda: {"ok": True}
        points = clip(n=200)
        watcher.load_clip = lambda clip_id=None, path=None: {
            "id": "same-clip", "aircraft": "Test Type",
            "points": [dict(p) for p in points]}
        watcher.REPLAY_START_PAUSED = paused

    def close(self):
        import threading
        FakeGame.gate = None
        FakeGame.on_acquire = None
        for g in FakeGame.instances:
            if g.block is not None:
                g.block.set()
        watcher.stop_replay()
        for th in threading.enumerate():
            if th.name == "ghost-replay":
                th.join(5)
        for n, v in self.keep.items():
            setattr(watcher, n, v)
        with watcher.RUNTIME_LOCK:
            (watcher.RUNTIME["connected"], watcher.RUNTIME["sm"],
             watcher.RUNTIME["camera_state"]) = self.keep_rt

    @staticmethod
    def live():
        """Connections a replay opened and nothing has closed."""
        return [g for g in FakeGame.instances if g.oid is not None and not g.closed]

    @staticmethod
    def workers():
        import threading
        return [th for th in threading.enumerate()
                if th.name == "ghost-replay" and th.is_alive()]


def test_two_replays_started_at_once_leave_one_that_stop_can_stop():
    """Both used to get past the stop before either published, both spawned,
    and the second overwrote the only record of the first - which Stop then
    never reached: a ghost, a connection and a worker left running."""
    import threading
    s = Stage()
    try:
        FakeGame.gate = threading.Event()
        out = []
        ths = [threading.Thread(target=lambda: out.append(
            watcher.start_replay({"clip_id": "same-clip"}))) for _ in range(2)]
        for th in ths:
            th.start()
        time.sleep(0.4)                  # both requests are in flight
        FakeGame.gate.set()
        for th in ths:
            th.join(10)
        assert len(out) == 2 and all(r.get("ok") for r in out), out
        live = Stage.live()
        assert len(live) == 1, (
            "%d replays left running after two starts; at most one may be"
            % len(live))
        with watcher.RUNTIME_LOCK:
            owned = watcher.RUNTIME["replay"].get("gsc")
        assert owned is live[0], "the running replay is not the one Stop owns"
        watcher.stop_replay()
        assert not Stage.live(), "Stop left a replay running"
        for g in FakeGame.instances:
            if g.oid is not None:
                assert g.oid in g.removed, "ghost %s was never removed" % g.oid
        time.sleep(0.3)
        assert not Stage.workers(), "a replay worker outlived Stop"
    finally:
        s.close()


def test_a_stop_during_a_start_stops_what_the_start_made():
    """A Stop that arrives while a start is still spawning used to find
    nothing to stop, and the start then published a replay nobody stopped."""
    import threading
    s = Stage()
    try:
        FakeGame.gate = threading.Event()
        starter = threading.Thread(target=lambda: watcher.start_replay({"clip_id": "same-clip"}))
        starter.start()
        time.sleep(0.3)                  # the start is inside open()
        stopper = threading.Thread(target=watcher.stop_replay)
        stopper.start()
        time.sleep(0.3)
        FakeGame.gate.set()
        starter.join(10)
        stopper.join(10)
        assert not Stage.live(), "Stop returned and a replay was left running"
        with watcher.RUNTIME_LOCK:
            assert not watcher.RUNTIME["replay"].get("active")
            assert watcher.RUNTIME["replay"].get("gsc") is None
    finally:
        s.close()


def test_a_worker_that_outlived_its_stop_cannot_touch_the_next_replay():
    """Stop joins the worker for 2 s and then tears down anyway. A worker
    stuck in a camera call then woke after a newer replay of the SAME clip
    had started - the clip id was the only ownership check - and cleared the
    new replay's object id and camera flag, and released the camera again."""
    import threading
    s = Stage(paused=False)
    try:
        assert watcher.start_replay({"clip_id": "same-clip"}).get("ok")
        old = FakeGame.instances[-1]
        old.block = threading.Event()
        for _ in range(100):
            if old.blocked:
                break
            time.sleep(0.02)
        assert old.blocked, "fixture: the worker never reached camera_set"
        before = set(Stage.workers())
        t0 = time.time()
        watcher.stop_replay()
        assert time.time() - t0 < 5, "Stop deadlocked against its worker"
        watcher.REPLAY_START_PAUSED = True
        assert watcher.start_replay({"clip_id": "same-clip"}).get("ok")
        new = FakeGame.instances[-1]
        releases = old.events.count("release")
        old.block.set()                     # the old worker wakes up now
        for th in before:
            th.join(5)
        with watcher.RUNTIME_LOCK:
            rep = watcher.RUNTIME["replay"]
            state = (rep.get("object_id"), rep.get("gsc"), rep.get("active"),
                     rep["chase"].get("camera_acquired"))
        assert state == (new.oid, new, True, True), (
            "the old worker changed the new replay's state: object_id, gsc, "
            "active, camera_acquired = %r" % (state[:1] + state[2:],))
        assert old.events.count("release") == releases, (
            "the old worker released the camera after a newer replay took it")
        assert not new.closed and new.oid not in new.removed
    finally:
        s.close()


def test_an_old_worker_waking_during_the_next_start_cannot_touch_it():
    """The narrower window: the stopped worker wakes while the next start is
    still running - after it has taken the camera, before it has published.
    Only Stop moving the generation on keeps the old worker out of it."""
    import threading
    s = Stage(paused=False)
    try:
        assert watcher.start_replay({"clip_id": "same-clip"}).get("ok")
        old = FakeGame.instances[-1]
        old.block = threading.Event()
        for _ in range(100):
            if old.blocked:
                break
            time.sleep(0.02)
        assert old.blocked, "fixture: the worker never reached camera_set"
        before = set(Stage.workers())
        watcher.stop_replay()
        releases = old.events.count("release")
        state_seen = []

        def wake_the_old_one(new):
            old.block.set()
            for th in before:
                th.join(5)
            with watcher.RUNTIME_LOCK:
                state_seen.append(watcher.RUNTIME["replay"]["chase"].get("camera_acquired"))
        FakeGame.on_acquire = wake_the_old_one
        watcher.REPLAY_START_PAUSED = True
        assert watcher.start_replay({"clip_id": "same-clip"}).get("ok")
        assert state_seen, "fixture: the old worker was never woken mid-start"
        assert old.events.count("release") == releases, (
            "the old worker released the camera while the next replay was taking it")
        new = FakeGame.instances[-1]
        with watcher.RUNTIME_LOCK:
            rep = watcher.RUNTIME["replay"]
            assert (rep.get("object_id"), rep.get("gsc"), rep.get("active")) == (new.oid, new, True)
    finally:
        s.close()


def test_a_start_that_fails_partway_leaves_nothing_and_does_not_wedge():
    s = Stage()
    try:
        FakeGame.spawn_none = True
        r = watcher.start_replay({"clip_id": "same-clip"})
        assert not r.get("ok") and r.get("error") == "ghost unavailable", r
        assert all(g.closed for g in FakeGame.instances), "a failed start left a connection open"
        FakeGame.spawn_none = False
        t0 = time.time()
        assert watcher.start_replay({"clip_id": "same-clip"}).get("ok")
        assert time.time() - t0 < 3, "the lifecycle lock was left held"
    finally:
        s.close()


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
    print("  %d replay test(s), %d failed" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

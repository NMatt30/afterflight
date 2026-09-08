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

"""The sim connection without Python-SimConnect (SIM_CONNECTION = "native").

What running without a Python install needs: nothing from pip. These cover
the three things the Python-SimConnect object did for the recording loop -
the connection, a clean quit, and noticing a sim that has gone - with the
package made unimportable, so a stray import fails the test rather than
passing on a machine that happens to have it.

What they cannot cover is the sim itself: whether RequestSystemState is
answered while paused, and what a killed sim looks like from this side. That
is a live test, recorded in HANDOFF.md.

    py -3 test_native.py
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import watcher                                            # noqa: E402
watcher.log = lambda *a, **k: None      # never append to the operational log


class FakeConn(object):
    """The parts of GameSimConnect the native path touches."""

    def __init__(self, answers=True, sends=True, quit_after=None):
        self._quit = False
        self.quit_after = quit_after
        self._last_pong = None
        self.answers, self.sends = answers, sends
        self.pings = 0
        self.closed = False
        self.now = None            # the test's clock, when it runs one

    def ping(self):
        self.pings += 1
        if self.quit_after is not None and self.pings >= self.quit_after:
            self._quit = True
        if not self.sends:
            return False
        if self.answers:
            self._last_pong = self.now if self.now is not None else time.time()
        return True

    def close(self):
        self.closed = True


class FakeSampler(object):
    def __init__(self, states=()):
        self.states = list(states)

    def latest(self):
        return self.states.pop(0) if self.states else None


class NoSimConnect(object):
    """Python-SimConnect made unimportable for the duration."""

    def __enter__(self):
        self.keep = sys.modules.get("SimConnect", "absent")
        sys.modules["SimConnect"] = None          # import raises ImportError
        return self

    def __exit__(self, *exc):
        if self.keep == "absent":
            sys.modules.pop("SimConnect", None)
        else:
            sys.modules["SimConnect"] = self.keep
        return False


def test_a_clean_quit_is_reported_as_one():
    sim = watcher.NativeSim(FakeConn(), FakeSampler())
    assert sim.quit == 0
    sim.gsc._quit = True
    assert sim.quit == 1, "the sim said it was quitting and the loop was not told"


def test_a_sim_that_answers_is_not_lost():
    sim = watcher.NativeSim(FakeConn(), FakeSampler())
    t = time.time()
    for k in range(10):
        sim.gsc.now = t + k * watcher.NATIVE_PING_SEC
        assert sim.lost(sim.gsc.now) is None, "lost at %d s while answering" % (k * watcher.NATIVE_PING_SEC)
    assert sim.gsc.pings == 10, "the heartbeat was not sent every NATIVE_PING_SEC"


def test_a_sim_that_stops_answering_is_lost():
    """Killed, crashed: no QUIT arrives, the answers just stop."""
    conn = FakeConn(answers=False)
    sim = watcher.NativeSim(conn, FakeSampler())
    t = sim.started
    assert sim.lost(t + 1.0) is None, "lost before the sim had any time to answer"
    why = sim.lost(t + watcher.NATIVE_LOST_SEC + 1.0)
    assert why and "no answer" in why, why


def test_a_sim_that_cannot_be_asked_is_lost():
    sim = watcher.NativeSim(FakeConn(sends=False), FakeSampler())
    t = sim.started
    first = sim.lost(t + watcher.NATIVE_PING_SEC)
    assert first is None, "one failed send is not yet a lost sim"
    why = sim.lost(t + 2 * watcher.NATIVE_PING_SEC)
    assert why and "could not be asked" in why, why


def test_a_pause_holds_the_last_state():
    """The sim stops pushing while paused; the one-at-a-time read returned the
    same values with the time moving on, and so does this."""
    fresh = {"t": 100.0, "lat": 47.0, "alt": 5000.0, "peaks": {"g": (0.9, 1.2)}}
    s, last = watcher.native_sample(fresh, None, now=100.0)
    assert s is fresh and last is fresh
    s, last = watcher.native_sample(None, last, now=105.0)
    assert s["t"] == 105.0 and s["alt"] == 5000.0 and s.get("held"), s
    assert "peaks" not in s, "a held state carried the extremes of the push it copies"
    assert last is fresh, "holding replaced the last fresh state"
    s, last = watcher.native_sample(None, None, now=1.0)
    assert s is None and last is None, "something was held before anything arrived"


def test_the_recording_loop_never_imports_python_simconnect():
    """Through run_connected itself: a sim that answers nothing, quit."""
    keep = (watcher.write_current, watcher.stop_replay)
    watcher.write_current = lambda *a, **k: None
    watcher.stop_replay = lambda *a, **k: None
    try:
        with NoSimConnect():
            conn = FakeConn()
            conn._quit = True
            sim = watcher.NativeSim(conn, FakeSampler())
            reason, flight, _tracker = watcher.run_connected(sim)
        assert reason == "quit", reason
    finally:
        watcher.write_current, watcher.stop_replay = keep


class Logs(object):
    """watcher.log captured for the duration."""

    def __enter__(self):
        self.lines, self.keep = [], watcher.log
        watcher.log = lambda m, *a, **k: self.lines.append(str(m))
        return self

    def __exit__(self, *exc):
        watcher.log = self.keep
        return False


def test_waiting_for_the_first_push_keeps_the_session():
    """Connected, nothing pushed yet. The loop assumed a sample and ended the
    session on its first tick - a native connection would have reconnected
    for ever without recording anything."""
    keep = (watcher.write_current, watcher.stop_replay, watcher.NATIVE_PING_SEC)
    watcher.write_current = lambda *a, **k: None
    watcher.stop_replay = lambda *a, **k: None
    watcher.NATIVE_PING_SEC = 0.0
    try:
        with NoSimConnect(), Logs() as logs:
            sim = watcher.NativeSim(FakeConn(quit_after=10), FakeSampler())
            reason, flight, _tracker = watcher.run_connected(sim)
        errors = [l for l in logs.lines if "error" in l.lower()]
        assert reason == "quit" and not errors, (
            "waiting for the first push ended %r: %r" % (reason, errors))
    finally:
        watcher.write_current, watcher.stop_replay, watcher.NATIVE_PING_SEC = keep


def test_a_lost_sim_ends_the_session_and_keeps_the_flight():
    """The loop runs a tick with nothing pushed yet, then the heartbeat finds
    the sim gone - handled as a failed read was: "error", flight kept."""
    keep = (watcher.write_current, watcher.stop_replay, watcher.NATIVE_PING_SEC)
    watcher.write_current = lambda *a, **k: None
    watcher.stop_replay = lambda *a, **k: None
    watcher.NATIVE_PING_SEC = 0.0
    try:
        with NoSimConnect(), Logs() as logs:
            sim = watcher.NativeSim(FakeConn(sends=False), FakeSampler())
            reason, flight, _tracker = watcher.run_connected(sim, flight=None)
        assert reason == "error", reason
        assert any(l.startswith("sim connection lost") for l in logs.lines), (
            "the session ended, but not because the heartbeat found the sim "
            "gone: %r" % logs.lines[-3:])
    finally:
        watcher.write_current, watcher.stop_replay, watcher.NATIVE_PING_SEC = keep


def test_the_default_is_still_python_simconnect():
    """Until the native path has been verified against a running sim."""
    if os.environ.get("AFTERFLIGHT_SIM_CONNECTION"):
        return
    assert watcher.SIM_CONNECTION == "python-simconnect"


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
    print("  %d native test(s), %d failed" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

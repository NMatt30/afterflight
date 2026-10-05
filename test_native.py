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


def test_native_is_the_default():
    """Verified against a running sim: a circuit, the Escape menu, a quit and
    a reconnect."""
    if os.environ.get("AFTERFLIGHT_SIM_CONNECTION"):
        return
    assert watcher.SIM_CONNECTION == "native"


def test_without_the_dll_the_package_is_the_fallback():
    """Native needs the sim's own DLL. Without it, an install that still has
    Python-SimConnect keeps recording through that; one without it tries
    native and says why it cannot connect."""
    import importlib.util
    keep = (watcher.SIM_CONNECTION, watcher.game_dll_status, importlib.util.find_spec,
            watcher._FALLBACK_SAID[0])
    try:
        watcher.SIM_CONNECTION = "native"
        watcher.game_dll_status = lambda: {"ok": True}
        assert watcher.use_native(), "native with the DLL present"
        watcher.game_dll_status = lambda: {"ok": False}
        importlib.util.find_spec = lambda name, *a: object() if name == "SimConnect" else keep[2](name, *a)
        assert not watcher.use_native(), "no DLL, package present: should fall back"
        importlib.util.find_spec = lambda name, *a: None if name == "SimConnect" else keep[2](name, *a)
        assert watcher.use_native(), "no DLL, no package: nothing to fall back to"
        watcher.SIM_CONNECTION = "python-simconnect"
        assert not watcher.use_native(), "the switch no longer chooses"
    finally:
        (watcher.SIM_CONNECTION, watcher.game_dll_status, importlib.util.find_spec,
         watcher._FALLBACK_SAID[0]) = keep


# --------------------------------------------------------------------------
# refreshing the sim's DLL (SME review R4)
# --------------------------------------------------------------------------

class Dlls(object):
    """native/ and two "sim" copies in a temporary folder. A real DLL cannot
    be made here, so the export check reads a marker instead: a file passes
    if it starts with b"MZ-ok"."""

    def __init__(self, native=b"MZ-ok old", sim=b"MZ-ok new"):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.native = os.path.join(self.dir, "native", "SimConnect_internal.dll")
        self.sim = os.path.join(self.dir, "sim", "SimConnect_internal.dll")
        os.makedirs(os.path.dirname(self.sim))
        if native is not None:
            os.makedirs(os.path.dirname(self.native))
            self.write(self.native, native)
        if sim is not None:
            self.write(self.sim, sim)
        self._keep = {n: getattr(watcher, n) for n in (
            "NATIVE_DIR", "NATIVE_DLL", "NATIVE_DLL_STAGED", "_sim_dll_sources",
            "_dll_has_exports", "_process_image_dirs", "_windowsapps_dlls",
            "_install_dll")}
        watcher.NATIVE_DIR = os.path.dirname(self.native)
        watcher.NATIVE_DLL = self.native
        watcher.NATIVE_DLL_STAGED = self.native + ".new"
        watcher._sim_dll_sources = lambda: [self.sim] if os.path.isfile(self.sim) else []
        watcher._process_image_dirs = lambda names: [os.path.dirname(self.sim)]
        watcher._windowsapps_dlls = lambda: []

        def exports(path, names):
            try:
                with open(path, "rb") as f:
                    ok = f.read(5) == b"MZ-ok"
            except OSError:
                ok = False
            return ok, ([] if ok else list(names))
        watcher._dll_has_exports = exports

    @staticmethod
    def write(path, data):
        with open(path, "wb") as f:
            f.write(data)

    @staticmethod
    def read(path):
        with open(path, "rb") as f:
            return f.read()

    def close(self):
        import shutil
        for n, v in self._keep.items():
            setattr(watcher, n, v)
        shutil.rmtree(self.dir, ignore_errors=True)


def test_startup_keeps_using_the_copy_in_native():
    """The ordinary resolution is unchanged: a working copy in native/ is
    used, and nothing is copied over it on a normal start."""
    d = Dlls()
    try:
        assert watcher.resolve_game_simconnect_dll() == d.native
        assert d.read(d.native) == b"MZ-ok old"
    finally:
        d.close()


def test_a_refresh_takes_the_sims_copy_and_says_so():
    """-ResolveDll went through that same resolver and said RESOLVED on the
    old copy, however different the sim's was."""
    d = Dlls()
    try:
        r = watcher.refresh_game_simconnect_dll()
        assert r["status"] == "updated" and r["source"] == d.sim, r
        assert d.read(d.native) == b"MZ-ok new", "native/ still holds the old copy"
        assert not os.path.isfile(d.native + ".new")
        again = watcher.refresh_game_simconnect_dll()
        assert again["status"] == "unchanged", again
    finally:
        d.close()


def test_a_copy_in_use_is_staged_and_taken_up_at_the_next_start():
    """A running watcher has the DLL loaded, and Windows will not replace a
    file in use. The new copy waits beside it - not reported as done - and
    replaces it when the watcher next starts."""
    d = Dlls()
    try:
        def in_use(staged):
            raise PermissionError(13, "The process cannot access the file")
        watcher._install_dll = in_use
        r = watcher.refresh_game_simconnect_dll()
        assert r["status"] == "staged", r
        assert d.read(d.native) == b"MZ-ok old", "the copy in use was touched"
        assert d.read(d.native + ".new") == b"MZ-ok new"
        watcher._install_dll = d._keep["_install_dll"]      # the watcher restarts
        assert watcher.resolve_game_simconnect_dll() == d.native
        assert d.read(d.native) == b"MZ-ok new", "the staged copy was not taken up"
        assert not os.path.isfile(d.native + ".new")
    finally:
        d.close()


def test_a_staged_copy_that_fails_its_check_is_not_promoted():
    d = Dlls()
    try:
        d.write(d.native + ".new", b"garbage")
        assert watcher.promote_staged_dll() == "rejected"
        assert d.read(d.native) == b"MZ-ok old" and not os.path.isfile(d.native + ".new")
    finally:
        d.close()


def test_a_bad_source_leaves_the_working_copy_alone():
    d = Dlls(sim=b"not a dll")
    try:
        r = watcher.refresh_game_simconnect_dll()
        assert r["status"] == "no source", r
        assert d.read(d.native) == b"MZ-ok old"
    finally:
        d.close()


def test_with_no_sim_copy_the_refresh_says_so():
    d = Dlls(sim=None)
    try:
        assert watcher.refresh_game_simconnect_dll()["status"] == "no source"
        assert d.read(d.native) == b"MZ-ok old"
    finally:
        d.close()


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

"""The tray must notice a watcher that has stopped working.

On 3 Sep 2026 a watcher stopped logging at 22:55:44, never noticed the sim
close, and was killed by Windows as unresponsive 46 minutes later. Nothing
brought it back: the tray polled every 3 s, drew the "down" icon, and did
nothing else, so it was still dead the next morning.

Killing the process is the easy half and is easy to test by hand. The half
that needs a test is the wedged watcher - detect loop stopped, HTTP thread
still answering - because from outside it looks perfectly healthy.

    py -3 test_supervision.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import tray                                             # noqa: E402


def fake_tray():
    """A Tray with no window, no icons and no real watcher behind it."""
    t = object.__new__(tray.Tray)
    t.nid = None
    t.state = "connected"
    t.tip = ""
    t.icons = None
    t.proc = None
    t.update_icon = lambda: None
    t.decided = []
    t.consider_restart = lambda why: t.decided.append(why)
    return t


def state(age):
    return {"ok": True, "connected": True, "heartbeat_age_s": age,
            "current": {"state": "in_flight", "aircraft": "AS365 N2 - VIP"},
            "replay": {"active": False}, "clips": {"leg": 1}}


def _decide(doc):
    t = fake_tray()
    real = tray.http_get
    tray.http_get = lambda *a, **k: doc
    try:
        t.refresh_state()
    finally:
        tray.http_get = real
    return t.decided


def test_a_turning_loop_is_left_alone():
    assert not _decide(state(2.0))
    assert not _decide(state(60.0)), (
        "a slow pass is not a wedge; restarting on it would interrupt "
        "recording for no reason")


def test_a_stopped_loop_is_restarted_even_though_it_answers():
    assert _decide(state(900.0)), (
        "SUPERVISION: a watcher whose detect loop has stopped still answers "
        "on the port. Liveness has to be the heartbeat, not the socket.")
    assert _decide(state(2760.0)), "the 46 minutes that actually happened"


def test_a_watcher_that_does_not_answer_is_restarted():
    assert _decide(None)


def test_an_older_watcher_without_a_heartbeat_is_not_restart_looped():
    """A state document with no heartbeat field is not a wedged watcher."""
    assert not _decide({"ok": True, "connected": True, "current": {},
                        "replay": {}, "clips": {}})


def test_restarting_is_a_flag():
    assert hasattr(tray, "WATCHER_AUTO_RESTART")
    assert tray.WATCHER_STALL_SEC > 0


# --------------------------------------------------------------------------
# ctypes prototypes: a 64-bit handle must survive the round trip
# --------------------------------------------------------------------------

def test_handle_arguments_are_not_squeezed_into_an_int():
    """The tray menu stopped working because DestroyMenu had no argtypes.

    Without them ctypes converts every argument to c_int, and Windows hands
    back menu handles above 2**31 often enough that it worked all morning and
    then threw. It threw from the cleanup line, which used to sit BEFORE the
    command was dispatched, so the visible symptom was "Open logbook does
    nothing" rather than a crash - three days of tray.log say WNDPROC error
    and no dispatch line.

    Checked by conversion rather than by reading the declarations: the
    question is not whether argtypes exists, it is whether a real handle
    fits through it.
    """
    import ctypes
    HANDLE_TAKERS = [
        (tray.user32, "DestroyMenu"),
        (tray.user32, "AppendMenuW"),
        (tray.user32, "TrackPopupMenu"),
        (tray.user32, "DestroyWindow"),
        (tray.user32, "DefWindowProcW"),
        (tray.user32, "SetForegroundWindow"),
        (tray.user32, "PostMessageW"),
        (tray.user32, "SetTimer"),
        (tray.kernel32, "CloseHandle"),
    ]
    big = 0x0000021700FF0000          # a plausible 64-bit handle
    problems = []
    for lib, name in HANDLE_TAKERS:
        fn = getattr(lib, name, None)
        if fn is None:
            problems.append("%s is not bound at all" % name)
            continue
        argtypes = getattr(fn, "argtypes", None)
        if not argtypes:
            problems.append("%s has no argtypes, so every argument becomes "
                            "c_int and a real handle overflows it" % name)
            continue
        try:
            argtypes[0](big)
        except Exception as e:
            problems.append("%s cannot take a 64-bit handle: %r" % (name, e))
    assert not problems, "; ".join(problems)


def test_a_failed_cleanup_cannot_swallow_the_menu_command():
    """DestroyMenu runs in a finally, and the dispatch happens after it."""
    import inspect
    src = inspect.getsource(tray.Tray.show_menu)
    d = src.index("DestroyMenu")
    c = src.index("self.on_command")
    assert d < c, (
        "on_command runs before DestroyMenu; the menu handle would leak on "
        "every click")
    assert "finally" in src[:d], (
        "DestroyMenu is not in a finally block, so anything that throws "
        "before it leaks the handle")
    tail = src[d:c]
    assert "except" in tail, (
        "DestroyMenu is not wrapped, so a failure there still costs the user "
        "the click it was cleaning up after")


# --------------------------------------------------------------------------
# Stopping the watcher on purpose is not a crash
# --------------------------------------------------------------------------

def test_a_deliberate_stop_is_not_undone():
    """Stop watcher appeared to do nothing: it stopped, then came straight back.

    The supervisor cannot tell a deliberate stop from a crash - both look like
    nothing answering on the port - so the tray restarted it on the next 3 s
    tick, every time.
    """
    t = fake_tray()
    t.user_stopped = False
    t.consider_restart = tray.Tray.consider_restart.__get__(t)
    t._restart_at = 0.0
    t._restarting = False
    t._restart_fails = 0
    started = []
    t.stop_watcher = lambda: "stopped"
    t.watcher_pid = lambda: None
    t.start_watcher = lambda force=False: started.append(force) or "started"

    # A crash: nothing was asked for, so it comes back.
    real = tray.http_get
    tray.http_get = lambda *a, **k: None
    try:
        t.refresh_state()
        import time as _t
        for _ in range(50):
            if started: break
            _t.sleep(0.02)
        assert started, "a watcher that died on its own was not restarted"

        # A deliberate stop: it stays stopped.
        #
        # Reset the whole backoff, not just the clock. The crash above left
        # _restarting True and the backoff armed, either of which makes
        # consider_restart return early - so without this the test passes even
        # with the user_stopped guard deleted, which a mutation proved.
        started[:] = []
        t.user_stopped = True
        t._restart_at = 0.0
        t._restarting = False
        t._restart_fails = 0
        t.refresh_state()
        _t.sleep(0.3)
        assert not started, (
            "the watcher was restarted after the user stopped it; that is the "
            "bug where Stop watcher looked like it did nothing")
        assert "start it" in t.tip, (
            "the tooltip should say it is stopped on purpose, not just down: "
            "%r" % t.tip)
    finally:
        tray.http_get = real


def test_the_intent_is_not_recorded_inside_stop_watcher():
    """consider_restart calls stop_watcher on its way to relaunching.

    If the flag were set there, the first automatic restart would mark the
    watcher user-stopped and it would never come back.
    """
    import inspect
    src = inspect.getsource(tray.Tray.stop_watcher)
    assert "user_stopped" not in src, (
        "stop_watcher sets the user_stopped flag, so an automatic restart - "
        "which calls it first - would permanently disable supervision")
    handler = inspect.getsource(tray.Tray.on_command)
    assert "user_stopped = True" in handler and "user_stopped = False" in handler, (
        "the menu handler is where intent is known, and where the flag belongs")


def test_stop_is_offered_for_a_watcher_this_tray_did_not_start():
    """A watcher started at logon left self.proc None and greyed out Stop."""
    import inspect
    src = inspect.getsource(tray.Tray.show_menu)
    i = src.index("ID_STOP_WATCHER")
    window = src[max(0, i - 260):i]
    assert "0 if running else MF_GRAYED" in window, (
        "Stop watcher is enabled from self.proc rather than from whether a "
        "watcher is actually answering")


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
    print("  %d supervision test(s), %d failed" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

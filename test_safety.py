"""The safety invariants, as tests rather than as prose.

AGENTS.md states these rules and explains why they exist. A rule written only
in Markdown is advisory: a contributor - or an agent working from a prompt -
can read it and still send a patch that breaks it, and the only thing standing
in the way is whoever reviews the pull request. These make the rules binding.

They are deliberately behavioral where it matters. The question is never
"does this function say the word refusing", it is "can a SimConnect call reach
the user's aircraft", so the tests watch for the call rather than reading the
prose around it. Two of the eight guards return early instead of raising, and
a test that looked for a raise would have missed both.

No pip dependencies, to match the rest of the project. Run it directly:

    py -3 test_safety.py

pytest will also collect it if you happen to have pytest.
"""
import inspect
import ipaddress
import io
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import logbook_build                                    # noqa: E402
import tiles                                            # noqa: E402
import watcher                                          # noqa: E402


# Importing watcher makes watcher.log() append to the real watcher.log, and
# these tests deliberately drive code paths that log. That put invented lines
# into the operational log - "refusing CameraSet on user aircraft" from a test
# reads exactly like the watcher refusing one - which made a later incident
# harder to read, not easier. Tests observe; they do not write to the log the
# running system keeps.
_LOGGED = []
watcher.log = lambda msg: _LOGGED.append(str(msg))


def _source(mod):
    path = os.path.join(BASE, mod + ".py")
    return io.open(path, encoding="utf-8").read()


class CallSpy(dict):
    """Stands in for the SimConnect export table and records every use.

    Subclasses dict because that is what the real one is, and a lookup alone
    is treated as a call: a guard that resolves the function pointer before
    deciding has already got closer to the user's aircraft than it should.
    """

    def __init__(self):
        dict.__init__(self)
        self.calls = []

    def __getitem__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            return 0
        self.calls.append(name)
        return call

    def __contains__(self, name):
        return True

    def get(self, name, default=None):
        return self[name]


def _offline_connection():
    """A GameSimConnect that is wired to a spy instead of to the sim."""
    g = object.__new__(watcher.GameSimConnect)
    import threading
    g.fns = CallSpy()
    g.handle = None
    g._h = lambda: 1
    g._opened = True
    g._closed = False
    g._ok = True
    g._quit = False
    g._acquired = False
    g._stop = threading.Event()
    g._thread = None
    g._lock = threading.Lock()
    g._assigned = {}
    g._exceptions = []
    g._next_req = 1
    g._ghost_def = None
    return g


def object_id_methods():
    """Every method that takes an object id, found rather than listed.

    Listing them would only ever be right on the day it was written. A new
    setter that forgets the guard has to fail these tests without anyone
    remembering to add it here.
    """
    out = []
    for name, fn in sorted(vars(watcher.GameSimConnect).items()):
        if name.startswith("__") or not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        if "object_id" in sig.parameters:
            out.append((name, fn, sig))
    return out


# --------------------------------------------------------------------------
# 1. Replay drives an AI ghost. It never touches the user's aircraft.
# --------------------------------------------------------------------------

def test_no_call_reaches_the_user_aircraft():
    methods = object_id_methods()
    assert methods, "found no methods taking object_id - has the API moved?"

    broken, unusable = [], []
    for name, fn, sig in methods:
        g = _offline_connection()
        args = []
        for pname, p in list(sig.parameters.items())[1:]:
            if pname == "object_id":
                args.append(watcher.USER_OBJECT_ID)
            elif p.default is not inspect.Parameter.empty:
                args.append(p.default)
            elif pname in ("percent", "on"):
                args.append(0)
            else:
                args.append(None)
        try:
            fn(g, *args)
        except RuntimeError:
            pass                      # an explicit refusal is the loudest form
        except (AttributeError, TypeError) as e:
            # The stand-in is missing something this method needs. That is a
            # gap in this test, not a pass: say so rather than count it.
            unusable.append("%s (%s)" % (name, e))
            continue
        except Exception:
            pass                      # any other failure still made no call
        if g.fns.calls:
            broken.append("%s called %s" % (name, ", ".join(sorted(set(g.fns.calls)))))

    assert not unusable, (
        "could not exercise: " + "; ".join(unusable)
        + " - extend _offline_connection() so these are actually covered")
    assert not broken, (
        "SAFETY: a SimConnect call was made for object id 0, the user's own "
        "aircraft: " + "; ".join(broken))


def test_every_object_id_method_checks_the_user_id():
    """The same rule read statically, for a clearer failure message.

    The behavioral test above is the real one. This one names the method that
    forgot, which is what someone adding a setter needs to be told.
    """
    missing = []
    for name, fn, _sig in object_id_methods():
        src = inspect.getsource(fn)
        if "USER_OBJECT_ID" not in src:
            missing.append(name)
    assert not missing, (
        "SAFETY: these take an object id without checking it against "
        "USER_OBJECT_ID: " + ", ".join(missing)
        + ". Replay drives an AI ghost only; object id 0 is the user.")


def test_these_tests_can_actually_fail():
    """Prove the two tests above bite, every run.

    They both work by finding methods rather than from a list, which is what
    makes them catch a setter nobody remembered to declare - and also what
    would make them pass silently for ever if the discovery ever stopped
    working. A parameter rename would be enough. So: add a deliberately
    unguarded setter, check both tests reject it, take it away again.
    """
    def set_ghost_trim(self, object_id, value):
        self.fns["SimConnect_SetDataOnSimObject"](self._h(), object_id, value)

    watcher.GameSimConnect.set_ghost_trim = set_ghost_trim
    try:
        found = [n for n, _f, _s in object_id_methods()]
        assert "set_ghost_trim" in found, (
            "the discovery in object_id_methods() no longer finds methods "
            "taking object_id, so the guard tests above prove nothing")
        for check in (test_no_call_reaches_the_user_aircraft,
                      test_every_object_id_method_checks_the_user_id):
            try:
                check()
            except AssertionError:
                pass
            else:
                raise AssertionError(
                    "%s passed an unguarded setter - it is not enforcing "
                    "anything" % check.__name__)
    finally:
        del watcher.GameSimConnect.set_ghost_trim


def test_camera_set_relative_6dof_is_never_called():
    """It is user-aircraft relative, so it is not bound and not called."""
    src = _source("watcher")
    for pattern in ('fns["SimConnect_CameraSetRelative6DOF"]',
                    "fns['SimConnect_CameraSetRelative6DOF']",
                    "CameraSetRelative6DOF("):
        assert pattern not in src, (
            "SAFETY: CameraSetRelative6DOF is user-aircraft relative and must "
            "not be used. The chase camera is placed in world coordinates.")
    assert "SimConnect_CameraSetRelative6DOF" not in watcher._CAMERA_EXPORTS


# --------------------------------------------------------------------------
# 2. This talks to a game on the user's own machine. It is not a service.
# --------------------------------------------------------------------------

def test_http_binds_only_to_loopback():
    addr = ipaddress.ip_address(watcher.HTTP_HOST)
    assert addr.is_loopback, (
        "SAFETY: the HTTP server must bind to loopback. %s is reachable from "
        "the network." % watcher.HTTP_HOST)


def test_no_wildcard_bind_anywhere():
    src = _source("watcher")
    for bad in ('"0.0.0.0"', "'0.0.0.0'", '"::"'):
        assert bad not in src, (
            "SAFETY: %s binds every interface. SimConnect and the HTTP server "
            "stay on loopback." % bad)


# --------------------------------------------------------------------------
# 3. Nothing costs the user frames while they are flying.
# --------------------------------------------------------------------------

def test_detect_loop_stays_cheap():
    assert watcher.POLL_SEC >= 1.0, (
        "SAFETY: detect work runs at most about once a second. POLL_SEC=%s "
        "would run it %.1f times a second."
        % (watcher.POLL_SEC, 1.0 / max(watcher.POLL_SEC, 1e-9)))
    assert "BELOW_NORMAL_PRIORITY_CLASS" in _source("watcher"), (
        "SAFETY: the watcher runs at Below Normal priority so the sim keeps "
        "the CPU it needs.")


def test_map_bake_is_deferred_while_a_flight_is_active():
    """Baking is Pillow, tiles and a supersampled canvas. Not mid-sortie."""
    real_active = watcher.flight_is_active
    real_build = logbook_build.build
    built = []

    def spy_build(*a, **k):
        built.append(k)
        return {"totals": {}}

    watcher.flight_is_active = lambda: True
    logbook_build.build = spy_build
    try:
        res = watcher.rebuild_logbook_now(bake_maps=True, force=False)
    finally:
        watcher.flight_is_active = real_active
        logbook_build.build = real_build

    assert not built, (
        "SAFETY: a map bake ran while a flight was in progress. It must be "
        "deferred until the aircraft is parked.")
    assert isinstance(res, dict) and res.get("deferred"), (
        "a deferred bake must say so, so the request is serviced later "
        "rather than dropped: got %r" % (res,))


def test_tiles_never_reach_the_network_when_offline():
    """allow_network=False is what keeps a rebuild off the wire mid-flight."""
    import urllib.request
    real = urllib.request.urlopen
    reached = []

    def spy(*a, **k):
        reached.append(a[:1])
        raise AssertionError("network reached")

    urllib.request.urlopen = spy
    try:
        # A tile that cannot be in the cache, so the only way to answer is the
        # network - which must not be tried.
        got = tiles.fetch_tile(os.path.join(BASE, "sessions", "tiles"),
                               19, 123456, 654321, allow_network=False)
    finally:
        urllib.request.urlopen = real

    assert not reached, (
        "SAFETY: a tile was fetched with allow_network=False. Baking must not "
        "burst-fetch while the sim is flying.")
    assert got is None


# --------------------------------------------------------------------------
# 4. A replay must never record a flight.
# --------------------------------------------------------------------------

def test_a_replay_cannot_commit_an_event():
    """Replaying a landing overwrote the clip it was replaying.

    The user was parked on the spot they had landed on, which is exactly where
    the replay puts the ghost. At the ghost's touchdown the sim broke the
    parked aircraft's ground contact for a single sample - one on_ground=False
    out of 1295 - and the detector read that as a landing. It committed the
    event, wrote a clip for it, and the clip it wrote over was the one playing.

    A replay puts a second aircraft into the world, usually on top of the
    first. Nothing observed while that is true can be trusted, so nothing
    observed while that is true is recorded.
    """
    tracker = object.__new__(watcher.ClipTracker)
    tracker.flight = object()          # a flight is in progress
    tracker.leg = 1
    tracker.pending = None
    tracker.bounce = None

    committed = []
    real_window = getattr(watcher.ClipTracker, "_window", None)
    tracker._window = lambda *a, **k: committed.append("windowed") or []

    keep = dict(watcher.RUNTIME["replay"])
    try:
        watcher.RUNTIME["replay"]["active"] = True
        watcher.RUNTIME["replay"]["holding"] = False
        tracker._commit("landing", {"t": 1.0, "ts": "x"}, 105.4)
        assert not committed, (
            "a landing was committed while a replay was running; that is how "
            "a clip gets overwritten by the replay of itself")

        watcher.RUNTIME["replay"]["active"] = False
        watcher.RUNTIME["replay"]["holding"] = True
        tracker._commit("landing", {"t": 1.0, "ts": "x"}, 105.4)
        assert not committed, (
            "holding still leaves the ghost spawned on the runway, so it can "
            "still perturb the user aircraft")
    finally:
        watcher.RUNTIME["replay"].clear()
        watcher.RUNTIME["replay"].update(keep)
    assert real_window is not None


def test_replay_in_progress_is_true_for_both_states():
    keep = dict(watcher.RUNTIME["replay"])
    try:
        watcher.RUNTIME["replay"]["active"] = False
        watcher.RUNTIME["replay"]["holding"] = False
        assert watcher.replay_in_progress() is False
        for flag in ("active", "holding"):
            watcher.RUNTIME["replay"]["active"] = False
            watcher.RUNTIME["replay"]["holding"] = False
            watcher.RUNTIME["replay"][flag] = True
            assert watcher.replay_in_progress() is True, (
                "%s does not count as a replay, but the ghost is spawned" % flag)
    finally:
        watcher.RUNTIME["replay"].clear()
        watcher.RUNTIME["replay"].update(keep)


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 5. A copy of this tree must work as a copy of this tree.
# --------------------------------------------------------------------------

import glob as _glob
import re as _re


def test_no_module_hardcodes_a_path_to_one_machine():
    """watcher.py and snapshot.py pinned BASE to the author's own folder.

    Two things went wrong at once. A second copy of the tree read and wrote
    the FIRST copy's sessions, logs and lock file, so a clean-install test
    would have quietly driven the original install. And on anyone else's
    machine the path does not exist at all, which is a release blocker for a
    repo meant to be downloaded.

    It also put a username into a repo meant to be published.
    """
    drive_users = _re.compile(r"[A-Za-z]:[/\\]+Users[/\\]+")
    bad = []
    for path in sorted(_glob.glob(os.path.join(BASE, "*.py"))):
        name = os.path.basename(path)
        if name.startswith("test_"):
            continue
        for n, line in enumerate(io.open(path, encoding="utf-8",
                                         errors="replace"), 1):
            if line.lstrip().startswith("#"):
                continue
            if drive_users.search(line):
                bad.append("%s:%d %s" % (name, n, line.strip()[:60]))
    assert not bad, (
        "absolute paths to one machine, in files other people will run: "
        + "; ".join(bad))


def test_base_is_derived_from_the_file_not_written_down():
    """Every module with a BASE derives it from its own location."""
    wrong = []
    for path in sorted(_glob.glob(os.path.join(BASE, "*.py"))):
        name = os.path.basename(path)
        src = io.open(path, encoding="utf-8", errors="replace").read()
        m = _re.search(r"^BASE\s*=\s*(.+)$", src, _re.M)
        if not m:
            continue
        if "__file__" not in m.group(1):
            wrong.append("%s: BASE = %s" % (name, m.group(1)[:60]))
    assert not wrong, (
        "these set BASE to something other than their own location, so a copy "
        "of the tree would not be self-contained: " + "; ".join(wrong))


# --------------------------------------------------------------------------
# 6. This repo is meant to go public.
# --------------------------------------------------------------------------

import subprocess as _sp

# Ids that exist only to show the format, and are safe because no flight was
# recorded at that moment. Keep this list short.
FABRICATED_IDS = ("flt-20260601T033000Z",)


def _tracked_files():
    """What git would publish. Raises Skipped when git cannot tell us.

    Returning [] here read as "nothing tracked carries an identifier", which
    is the same answer a clean repository gives. The two must not look alike.
    """
    try:
        out = _sp.run(["git", "ls-files", "-z"], cwd=BASE,
                      stdout=_sp.PIPE, stderr=_sp.PIPE)
    except OSError as e:
        raise Skipped("git is not available here (%s), so no tracked file was "
                      "inspected" % e.__class__.__name__)
    if out.returncode != 0:
        why = (out.stderr or b"").decode("utf-8", "replace").strip()
        raise Skipped("git would not list this repository, so no tracked file "
                      "was inspected: %s" % (why.splitlines()[0] if why
                                             else "exit %d" % out.returncode))
    names = [n for n in out.stdout.decode("utf-8", "replace").split(chr(0)) if n]
    if not names:
        raise Skipped("git lists no tracked files here; nothing was inspected")
    return names


def test_no_tracked_file_names_a_user_or_their_flights():
    """Nothing published may name whoever ran it, or where they flew.

    A home folder carries a username. A machine name identifies a computer. A
    flight id is a date and a time, and published beside an airframe and a
    route it says where somebody was - which is not what a logbook app owes
    anyone. All three have been committed here by accident before, because
    nothing was checking.

    Untracking a file is enough to satisfy this. It does not rewrite history -
    see "Before this goes public" in DESIGN-NOTES.md.
    """
    bs = chr(92)
    # Assembled, not written out, so this file does not match itself. The
    # trailing letter is what separates a real home folder from the generic
    # placeholder DESIGN-NOTES.md uses while discussing this very problem.
    home = _re.compile("[A-Za-z]:[" + bs + bs + "/]Users[" + bs + bs
                       + "/][A-Za-z]")
    machine = _re.compile(r"DESKTOP-[A-Z0-9]{7}")
    flight = _re.compile(r"flt-20[0-9]{6}T[0-9]{6}Z")

    tracked = _tracked_files()   # raises Skipped rather than passing blindly

    bad = []
    for name in tracked:
        path = os.path.join(BASE, name)
        if not os.path.isfile(path):
            continue
        try:
            src = io.open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for hit in set(home.findall(src)):
            bad.append("%s names a home folder (%s)" % (name, hit))
        for hit in set(machine.findall(src)):
            bad.append("%s names a machine (%s)" % (name, hit))
        for hit in set(flight.findall(src)):
            if hit not in FABRICATED_IDS:
                bad.append("%s names a real flight (%s)" % (name, hit))

    assert not bad, (
        "tracked files carry identifiers that should not go public - "
        "genericise them, or untrack the file: " + "; ".join(sorted(set(bad))))


class Skipped(Exception):
    """A test that could not run, which is not a test that passed.

    The privacy check reads git ls-files. When git refused the repository -
    an ownership check, in the review that found this - the helper turned the
    failure into an empty file list and the check returned happily. The runner
    printed "ok" for a test that had inspected nothing. A release gate that
    cannot tell "clean" from "never looked" is worse than no gate.
    """


def main(allow_skips=False):
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    skipped = 0
    for name, fn in tests:
        try:
            fn()
        except Skipped as e:
            skipped += 1
            print("  SKIP  %s" % name)
            print("        %s" % e)
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
    print("  %d safety test(s), %d failed, %d skipped"
          % (len(tests), failed, skipped))
    if skipped and not allow_skips:
        print()
        print("  A SKIP is not a pass: something above could not be checked at")
        print("  all. This exits non-zero so a release gate cannot go green on")
        print("  it. Re-run where the check can run, or pass --allow-skips to")
        print("  say you know what is not being checked.")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    import sys as _sys
    raise SystemExit(main(allow_skips="--allow-skips" in _sys.argv))

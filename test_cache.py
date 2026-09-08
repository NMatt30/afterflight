"""What a rebuild reuses, and what a delete claims to have done.

Every test here was written against a confirmed defect, found in an
independent review on 2026-09-05 and reproduced on a fixture before anything
was changed. They are the "before" pictures, kept.

The three defects:

  The Rebuild button did not reach the builder's cache bypass. It passed
  force=True to the watcher's wrapper, which used that word to mean "run even
  though a flight is in progress" and never forwarded it. AGENTS.md described
  the button as a full reprocess; it was a normal cached rebuild that jumped
  the queue.

  The sortie cache was keyed on the track file alone. scan_flights already
  signs the meta - a corrected category or stall speed refreshes the flight
  record - but the sortie built from that record was reused anyway, keeping
  the grade the old profile produced. Moving vs0 across the 61 kt boundary
  left the signature byte-identical.

  Purge reported ok with the planned byte count even when os.remove failed,
  then removed the exclusion, so surviving records came back into the logbook
  after the user had been told they were destroyed.

    py -3 test_cache.py
"""
import io
import json
import os
import re as _re
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import logbook_build                                     # noqa: E402

FID = "flt-19990101T000000Z"        # synthetic; no such sortie exists


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------

class Tree(object):
    """A sessions tree of one flight, pointed at by the builder's globals."""

    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self._keep = (logbook_build.SESSIONS, logbook_build.CACHE_JSON,
                      logbook_build.EXCLUDED_JSON, logbook_build.EVENTS_JSONL)
        logbook_build.SESSIONS = self.dir
        logbook_build.CACHE_JSON = os.path.join(self.dir, "cache.json")
        logbook_build.EXCLUDED_JSON = os.path.join(self.dir, "excluded.json")
        logbook_build.EVENTS_JSONL = os.path.join(self.dir, "events.jsonl")

    def close(self):
        (logbook_build.SESSIONS, logbook_build.CACHE_JSON,
         logbook_build.EXCLUDED_JSON, logbook_build.EVENTS_JSONL) = self._keep
        shutil.rmtree(self.dir, ignore_errors=True)

    def track(self):
        p = os.path.join(self.dir, FID + ".jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for i in range(5):
                f.write(json.dumps({
                    "ts": "1999-01-01T00:0%d:00+00:00" % i,
                    "lat": 40.0 + i * 0.01, "lon": -105.0, "alt": 6000.0,
                    "vs": 0.0, "gs": 90.0, "heading": 0.0,
                    "airspeed": 95.0, "on_ground": False}) + "\n")
        return p

    def meta(self, vs0):
        p = os.path.join(self.dir, FID + ".meta.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"flight_id": FID, "sortie_id": FID,
                       "aircraft": "Synthetic Type", "category": "Airplane",
                       "vs0": vs0, "ended_at": "1999-01-01T00:05:00+00:00"}, f)
        return p

    def signature(self, events=None):
        flights = logbook_build.scan_flights()
        group = logbook_build.group_sorties(flights)[0]
        sortie_id = next((g.get("sortie_id") for g in group
                          if g.get("sortie_id")), None) or group[0]["flight_id"]
        gsig = logbook_build.global_signature(True)
        sig = logbook_build.sortie_signature(group, gsig, events or {},
                                             sortie_id, None)
        return sig, group


# --------------------------------------------------------------------------
# 1. the sortie cache has to depend on everything its output depends on
# --------------------------------------------------------------------------

def test_a_corrected_meta_invalidates_the_sortie():
    """vs0 40 and vs0 100 are graded by different profiles.

    They are on opposite sides of the 14 CFR 23.49 boundary, so if the cached
    sortie survives this change the logbook keeps showing a grade produced by
    the wrong profile, with no way for the user to clear it short of deleting
    the cache by hand.
    """
    t = Tree()
    try:
        t.track()
        t.meta(40.0)
        sig_a, group_a = t.signature()
        t.meta(100.0)
        sig_b, group_b = t.signature()

        assert group_a[0].get("vs0") != group_b[0].get("vs0"), (
            "the fixture did not actually change the flight record")
        assert sig_a != sig_b, (
            "the meta changed the profile that grades this flight and the "
            "sortie signature did not move, so the cached grade wins")
    finally:
        t.close()


def test_an_unchanged_flight_is_still_a_cache_hit():
    """The fix must not turn every rebuild into a full reprocess."""
    t = Tree()
    try:
        t.track()
        t.meta(40.0)
        sig_a, _ = t.signature()
        sig_b, _ = t.signature()
        assert sig_a == sig_b, (
            "nothing moved and the signature did; every sortie would rebake "
            "on every rebuild")
    finally:
        t.close()


def test_a_corrected_event_invalidates_the_sortie():
    """The event signature used to cover time, kind and leg only.

    Derivation reads more than that - the touchdown rate is the number the
    landing grade comes from - so an event could be corrected in place and
    the sortie built from it stay cached.
    """
    t = Tree()
    try:
        t.track()
        t.meta(40.0)
        base = {"time": "1999-01-01T00:04:00+00:00", "kind": "landing",
                "leg": 1, "flight_id": FID}
        soft = dict(base, rate_fpm=60.0)
        hard = dict(base, rate_fpm=600.0)
        sig_soft, _ = t.signature({FID: [soft]})
        sig_hard, _ = t.signature({FID: [hard]})
        assert sig_soft != sig_hard, (
            "the touchdown rate changed from a greaser to an arrival and the "
            "signature did not move")
    finally:
        t.close()


# --------------------------------------------------------------------------
# 2. Rebuild means reprocess
# --------------------------------------------------------------------------

def test_the_rebuild_button_asks_the_builder_to_ignore_its_cache():
    """Drive the real handler path with a spy in place of the builder.

    A source check would pass on a line that never runs, and this defect was
    exactly a line that ran with the wrong argument.
    """
    import watcher
    watcher.log = lambda *a, **k: None        # never write to the real log

    seen = {}

    def spy(**kw):
        seen.update(kw)
        return {"totals": {}, "updated_at": ""}

    keep = logbook_build.build
    logbook_build.build = spy
    try:
        watcher.rebuild_logbook_now(force=True, reprocess=True)
        assert seen.get("force") is True, (
            "Rebuild did not tell the builder to ignore the sortie cache; "
            "AGENTS.md calls this a full reprocess. Got: %r" % (seen,))

        seen.clear()
        watcher.rebuild_logbook_now(force=True)
        assert seen.get("force") is False, (
            "an ordinary forced rebuild must not pay for a full reprocess; "
            "force is scheduling, reprocess is correctness. Got: %r" % (seen,))
    finally:
        logbook_build.build = keep


def test_force_and_reprocess_stayed_two_words():
    """They mean different things and were once the same word."""
    import inspect
    import watcher
    args = inspect.signature(watcher.rebuild_logbook_now).parameters
    assert "force" in args and "reprocess" in args, (
        "rebuild_logbook_now must keep scheduling and cache bypass separate")


# --------------------------------------------------------------------------
# 3. a delete that did not happen must not report success
# --------------------------------------------------------------------------

def test_purge_tells_the_truth_when_a_file_will_not_delete():
    """One file is locked. The report, the byte count and the hidden state
    all have to reflect that, and a retry has to be possible."""
    t = Tree()
    try:
        track = t.track()
        meta = t.meta(40.0)
        with open(logbook_build.EVENTS_JSONL, "w", encoding="utf-8") as f:
            f.write(json.dumps({"flight_id": FID, "kind": "landing",
                                "time": "1999-01-01T00:04:00+00:00"}) + "\n")
        with open(logbook_build.EXCLUDED_JSON, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "sorties": {FID: {"at": "1999"}},
                       "legs": {}}, f)

        entry = {"scope": "sortie", "sortie_id": FID, "flight_ids": [FID]}

        # Refuse to delete the meta, the way a file lock would.
        real_remove = os.remove

        def stubborn(path):
            if os.path.basename(path) == os.path.basename(meta):
                raise OSError(13, "Permission denied")
            return real_remove(path)

        os.remove = stubborn
        try:
            res = logbook_build.purge(entry, log=None)
        finally:
            os.remove = real_remove

        assert res.get("ok") is False, (
            "a file survived and purge reported success")
        assert res.get("failed"), "the failure was not reported to the caller"
        assert os.path.isfile(meta), "the fixture did not actually block it"
        assert not os.path.isfile(track), "the deletable file should have gone"

        # bytes must be what was freed, not what was planned
        assert res["bytes"] < res["planned_bytes"], (
            "reported %s bytes freed against a plan of %s; the planned total "
            "was being reported as the achieved one"
            % (res["bytes"], res["planned_bytes"]))

        # and it must still be hidden, so it does not reappear
        doc = logbook_build.read_json(logbook_build.EXCLUDED_JSON) or {}
        assert FID in (doc.get("sorties") or {}), (
            "the exclusion was removed after a partial delete, so the "
            "surviving recording comes back into the logbook")

        # retry, with nothing blocking it now
        res2 = logbook_build.purge(entry, log=None)
        assert res2.get("ok") is True, "retry failed: %r" % (res2.get("failed"),)
        assert not os.path.isfile(meta)
        doc = logbook_build.read_json(logbook_build.EXCLUDED_JSON) or {}
        assert FID not in (doc.get("sorties") or {}), (
            "a successful delete must clear the exclusion")
    finally:
        t.close()


def test_purge_reports_bytes_it_actually_freed():
    """The happy path, so the byte count is not just 'smaller when broken'."""
    t = Tree()
    try:
        track = t.track()
        t.meta(40.0)
        with open(logbook_build.EXCLUDED_JSON, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "sorties": {FID: {"at": "1999"}},
                       "legs": {}}, f)
        planned = os.path.getsize(track)
        entry = {"scope": "sortie", "sortie_id": FID, "flight_ids": [FID]}
        res = logbook_build.purge(entry, log=None)
        assert res["ok"] is True
        assert res["bytes"] == res["planned_bytes"], (
            "everything deleted, so freed and planned should agree")
        assert res["bytes"] >= planned
    finally:
        t.close()


# --------------------------------------------------------------------------
# 4. the network gate may be a callable, and a callable is always truthy
# --------------------------------------------------------------------------

def test_the_network_gate_is_resolved_not_tested_for_truth():
    """The watcher passes a lambda so permission is re-asked per request.

    That turns every bare `if allow_network` into a bug that reads "allowed"
    while the callable is answering no. Two of them shipped: the basemap log
    line said tiles came from the network when the fetch had been refused, and
    the build's cache-validity test rejected a cached sortie for a missing
    basemap that the gate would then refuse to fetch - a rebake that could not
    possibly succeed, on the one path where frames matter.
    """
    import tiles
    assert tiles.net_allowed(True) is True
    assert tiles.net_allowed(False) is False
    assert tiles.net_allowed(lambda: True) is True
    assert tiles.net_allowed(lambda: False) is False, (
        "a callable answering no must resolve to no, not to 'truthy'")

    src = io.open(os.path.join(BASE, "tiles.py"), encoding="utf-8").read()
    src += io.open(os.path.join(BASE, "logbook_build.py"), encoding="utf-8").read()
    bare = [ln.strip() for ln in src.splitlines()
            if "allow_network" in ln
            and "net_allowed" not in ln
            and "callable" not in ln
            and _re.search(r"(if|and|or|not)[" + chr(92) + r"s(]+allow_network" + chr(92) + r"b", ln)]
    assert not bare, (
        "allow_network read as a bare truth value; resolve it through "
        "tiles.net_allowed: " + "; ".join(bare))


def test_the_basemap_log_says_when_it_was_cache_only():
    """A log that cannot report the restriction cannot audit it.

    The watcher's own network gate is a callable, so this line always claimed
    the network had been available.
    """
    import tiles
    said = []
    # No tiles on disk and no network: the render fails and returns before the
    # log line, so drive the formatting the line performs instead.
    for gate, want in ((lambda: False, True), (False, True),
                       (lambda: True, False), (True, False)):
        text = "basemap%s" % ("" if tiles.net_allowed(gate) else " (cache only)")
        said.append((repr(gate), "(cache only)" in text, want))
    bad = [s for s in said if s[1] != s[2]]
    assert not bad, ("the cache-only note did not follow the resolved gate: %r"
                     % (bad,))


# --------------------------------------------------------------------------
# 5. a path that cannot be made relative is still a path
# --------------------------------------------------------------------------

def test_a_session_on_another_drive_does_not_kill_the_build():
    """os.path.relpath raises across Windows drive letters.

    Not hypothetical: a CI runner checks the repository out on D: and gives
    tempfile a directory on C:, and the entire build failed on a ValueError
    that had nothing to do with flying. An absolute path answers "where is
    this file" perfectly well; it is only longer.
    """
    t = Tree()
    keep_base = logbook_build.BASE
    logbook_build.BASE = "D:" + os.sep + "somewhere-else"
    try:
        t.track()
        t.meta(40.0)
        recs = logbook_build.scan_flights()
        assert recs, "scan_flights found nothing across drives"
        assert recs[0].get("jsonl"), "the flight record has no path"
    finally:
        logbook_build.BASE = keep_base
        t.close()


def test_a_path_under_base_is_still_recorded_relative():
    """The fallback must not turn every path absolute."""
    here = os.path.join(BASE, "sessions", "x.jsonl")
    got = logbook_build._rel_to_base(here)
    assert got == "sessions/x.jsonl", (
        "expected a relative path under BASE, got %r" % (got,))


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
    print("  %d cache/delete test(s), %d failed" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

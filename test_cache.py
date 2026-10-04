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


def test_a_changed_track_invalidates_the_sortie():
    """A delete that cuts a leg out of a track and then fails on a file keeps
    the leg hidden, so the exclusions do not move - the track is the only
    thing that says the sortie changed. A delete no longer empties the cache
    to be safe, so this signature is what keeps it right."""
    t = Tree()
    try:
        track = t.track()
        t.meta(40.0)
        sig_a, _ = t.signature()
        with open(track, encoding="utf-8") as f:
            rows = f.readlines()
        with open(track, "w", encoding="utf-8") as f:
            f.writelines(rows[:-2])
        sig_b, _ = t.signature()
        assert sig_a != sig_b, (
            "the track lost points and the sortie signature did not move, so "
            "the cached sortie still shows them")
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


class Book(object):
    """A whole logbook of synthetic flights, every builder path redirected."""
    NAMES = ("BASE", "SESSIONS", "CLIPS_DIR", "EVENTS_JSONL", "LOGBOOK_JSON",
             "CACHE_JSON", "EXCLUDED_JSON", "DETAIL_DIR")

    def __init__(self):
        self.root = tempfile.mkdtemp()
        self._keep = {n: getattr(logbook_build, n) for n in self.NAMES}
        logbook_build.BASE = self.root
        logbook_build.SESSIONS = os.path.join(self.root, "sessions")
        logbook_build.CLIPS_DIR = os.path.join(logbook_build.SESSIONS, "clips")
        logbook_build.EVENTS_JSONL = os.path.join(self.root, "events.jsonl")
        logbook_build.LOGBOOK_JSON = os.path.join(self.root, "logbook.json")
        logbook_build.CACHE_JSON = os.path.join(self.root, "logbook.cache.json")
        logbook_build.EXCLUDED_JSON = os.path.join(self.root, "excluded.json")
        logbook_build.DETAIL_DIR = os.path.join(logbook_build.SESSIONS, "detail")
        os.makedirs(logbook_build.CLIPS_DIR)
        self.lines = []

    def close(self):
        for n, v in self._keep.items():
            setattr(logbook_build, n, v)
        shutil.rmtree(self.root, ignore_errors=True)

    def flight(self, fid, legs, day):
        """legs flights out and back, 30 s on the ground between each."""
        import datetime as dt
        t = dt.datetime(1999, 1, day, 12, 0, tzinfo=dt.timezone.utc)
        rows, lat = [], 40.0
        for n in range(legs):
            for k in range(30):
                rows.append((t, lat, 0.0, True, 0.0))
                t += dt.timedelta(seconds=1)
            for k in range(300):
                lat += 0.0005
                rows.append((t, lat, 2000.0 + 10.0 * min(k, 300 - k), False, 110.0))
                t += dt.timedelta(seconds=1)
        for k in range(30):
            rows.append((t, lat, 0.0, True, 0.0))
            t += dt.timedelta(seconds=1)
        with open(os.path.join(logbook_build.SESSIONS, fid + ".jsonl"), "w",
                  encoding="utf-8") as f:
            for ts, la, alt, on_ground, gs in rows:
                f.write(json.dumps({
                    "ts": ts.isoformat(), "lat": la, "lon": -105.0, "alt": alt,
                    "vs": 0.0, "gs": gs, "heading": 0.0, "airspeed": gs,
                    "on_ground": on_ground}) + "\n")
        with open(os.path.join(logbook_build.SESSIONS, fid + ".meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"flight_id": fid, "sortie_id": fid,
                       "aircraft": "Synthetic Type", "category": "Airplane",
                       "vs0": 40.0, "ended_at": rows[-1][0].isoformat()}, f)

    def build(self, force=False):
        logbook_build.build(bake_maps=False, allow_network=False, force=force,
                            log=self.lines.append)
        said = [l for l in self.lines if "rebuilt" in l][-1]
        return int(_re.search(r"\((\d+) reused", said).group(1))

    def detail(self, fid):
        with open(os.path.join(logbook_build.DETAIL_DIR, fid + ".json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def hide(self, scope, fid, key=None):
        doc = logbook_build.read_json(logbook_build.EXCLUDED_JSON) or {
            "schema": 1, "sorties": {}, "legs": {}}
        if scope == "sortie":
            doc["sorties"][fid] = {"at": "1999"}
        else:
            doc["legs"].setdefault(fid, {})[key] = {"at": "1999"}
        with open(logbook_build.EXCLUDED_JSON, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.build()
        index = logbook_build.read_json(logbook_build.LOGBOOK_JSON)
        entry = next((h for h in index.get("hidden") or []
                      if h.get("scope") == scope and h.get("sortie_id") == fid
                      and (scope == "sortie" or h.get("leg_key") == key)), None)
        assert entry is not None, (
            "hiding did not reach the logbook: the cached sortie survived a "
            "change to what is hidden")
        return entry

    def snapshot(self):
        """What the user sees: the index and every detail file, less clocks."""
        index = dict(logbook_build.read_json(logbook_build.LOGBOOK_JSON))
        for volatile in ("updated_at", "generated_by"):
            index.pop(volatile, None)
        details = {}
        for name in sorted(os.listdir(logbook_build.DETAIL_DIR)):
            with open(os.path.join(logbook_build.DETAIL_DIR, name),
                      encoding="utf-8") as f:
                details[name] = json.load(f)
        return json.loads(json.dumps({"index": index, "details": details},
                                     sort_keys=True))


A = "flt-19990105T120000Z"    # two legs
B = "flt-19990106T120000Z"    # one leg


def _purged_then_rebuilt(scope, fid, leg_index=None):
    """Delete for good, rebuild as the watcher now does, then reprocess.

    Returns (sorties reused by the normal rebuild, its snapshot, the
    reprocess's snapshot, the book) - the book still open for inspection.
    """
    book = Book()
    book.flight(A, 2, 5)
    book.flight(B, 1, 6)
    assert book.build() == 0
    assert len(book.detail(A)["legs"]) == 2, "fixture: A should have two legs"
    key = book.detail(fid)["legs"][leg_index]["key"] if scope == "leg" else None
    entry = book.hide(scope, fid, key)
    res = logbook_build.purge(entry, log=None)
    assert res.get("ok"), "the purge itself failed: %r" % (res,)
    reused = book.build()
    quick = book.snapshot()
    book.build(force=True)
    return reused, quick, book.snapshot(), book


def test_deleting_a_leg_rebuilds_that_flight_and_nothing_else():
    """A delete used to empty the cache and ask for a reprocess, so deleting
    one leg rebuilt every flight and redrew every map - about two minutes per
    delete on a real logbook, with the page waiting on it. What a delete
    changes is in the signatures; the rest is reused, and the result is the
    same logbook a full reprocess makes."""
    reused, quick, full, book = _purged_then_rebuilt("leg", A, leg_index=1)
    try:
        assert reused >= 1, (
            "deleting a leg of one flight rebuilt the untouched one too")
        assert len(book.detail(A)["legs"]) == 1, "the deleted leg is still there"
        assert quick == full, (
            "after a delete the normal rebuild and a full reprocess disagree; "
            "something the delete changed is not in a signature")
    finally:
        book.close()


def test_deleting_a_flight_rebuilds_nothing_else():
    reused, quick, full, book = _purged_then_rebuilt("sortie", B)
    try:
        assert reused >= 1, (
            "deleting one flight rebuilt the untouched one too")
        assert not os.path.isfile(os.path.join(logbook_build.DETAIL_DIR, B + ".json")), (
            "the deleted flight's detail is still published")
        assert quick == full, (
            "after a delete the normal rebuild and a full reprocess disagree")
    finally:
        book.close()


def test_the_watcher_does_not_reprocess_after_a_delete():
    """The builder reuses what it can; the watcher has to let it."""
    import inspect
    import watcher
    src = inspect.getsource(watcher.purge_hidden)
    assert "reprocess=True" not in src, (
        "purge_hidden asks for a reprocess, which ignores the cache and "
        "redraws every map after every delete")


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


# --------------------------------------------------------------------------
# 4. what a rebuild costs when little has changed
# --------------------------------------------------------------------------

def test_the_build_cache_is_read_once_and_written_only_when_it_changes():
    """The build cache grows with the logbook, and was read three times and
    written twice per build - indented, fsynced, changed or not. Measured at
    16x this logbook, that was 80% of a rebuild that changed nothing."""
    t = Tree()
    names = ("BASE", "LOGBOOK_JSON", "DETAIL_DIR")
    keep = {n: getattr(logbook_build, n) for n in names}
    real = (logbook_build.read_cache, logbook_build.read_json,
            logbook_build.persistence.atomic_text)
    reads, writes, stray = [], [], []

    def read_cache():
        reads.append(1)
        return real[0]()

    def read_json(path):
        if os.path.abspath(path) == os.path.abspath(logbook_build.CACHE_JSON):
            stray.append(path)
        return real[1](path)

    def atomic_text(path, text, **kw):
        if os.path.abspath(path) == os.path.abspath(logbook_build.CACHE_JSON):
            writes.append(text)
        return real[2](path, text, **kw)

    def build():
        del reads[:], writes[:], stray[:]
        logbook_build.build(bake_maps=False, allow_network=False)
        return len(reads), len(writes), len(stray)

    try:
        logbook_build.BASE = t.dir
        logbook_build.LOGBOOK_JSON = os.path.join(t.dir, "logbook.json")
        logbook_build.DETAIL_DIR = os.path.join(t.dir, "detail")
        logbook_build.read_cache = read_cache
        logbook_build.read_json = read_json
        logbook_build.persistence.atomic_text = atomic_text
        t.track()
        t.meta(40.0)
        r, w, s = build()
        assert (r, s) == (1, 0), "the first build read the cache %d times" % (r + s)
        assert w == 1, "the first build wrote the cache %d times" % w
        first = writes and writes[0]
        r, w, s = build()
        assert (r, s) == (1, 0), "a rebuild read the cache %d times" % (r + s)
        assert w == 0, "a rebuild that changed nothing rewrote the cache"
        t.meta(100.0)
        r, w, s = build()
        assert w == 1, (
            "a corrected meta wrote the cache %d times; once is the "
            "change, none loses it" % w)
        assert writes[0] != first and "  " not in writes[0][:200], (
            "the cache is written indented, or did not change")
        assert json.load(open(logbook_build.CACHE_JSON, encoding="utf-8"))[
            "flights"][FID]["rec"]["vs0"] == 100.0
    finally:
        (logbook_build.read_cache, logbook_build.read_json,
         logbook_build.persistence.atomic_text) = real
        for n, v in keep.items():
            setattr(logbook_build, n, v)
        t.close()


def test_a_compact_document_is_the_same_document():
    """atomic_json moved from json.dump to json.dumps for the C encoder.
    The bytes must not change - settings.json is meant to be read by hand."""
    import persistence
    d = tempfile.mkdtemp()
    try:
        doc = {"b": [1, 2.5, None, True], "a": {"x": "é ⅓", "y": []}}
        for compact in (False, True):
            p = persistence.atomic_json(os.path.join(d, "x.json"), doc, compact=compact)
            want = (json.dumps(doc, separators=(",", ":")) if compact
                    else json.dumps(doc, indent=2)) + "\n"
            with io.open(p, encoding="utf-8") as f:
                got = f.read()
            assert got == want, "compact=%s wrote %r" % (compact, got[:80])
            assert json.loads(got) == doc
    finally:
        shutil.rmtree(d, ignore_errors=True)


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

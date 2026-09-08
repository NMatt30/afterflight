"""Failure and concurrency regressions, using only disposable synthetic data."""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import clipfile as cf
import logbook_build as b
import persistence as p
import settings
import watcher as w

w.log = lambda *args: None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Integrity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / 'sessions'
        (self.sessions / 'clips').mkdir(parents=True)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for module in (b, w):
            for key, value in {'BASE': self.root, 'SESSIONS': self.sessions,
                               'CLIPS_DIR': self.sessions / 'clips',
                               'EXCLUDED_JSON': self.root / 'excluded.json',
                               'EVENTS_JSONL': self.root / 'events.jsonl',
                               'LOGBOOK_JSON': self.root / 'logbook.json'}.items():
                self.stack.enter_context(patch.object(module, key, str(value)))
        for key, value in {'CACHE_JSON': self.root / 'logbook.cache.json',
                           'DETAIL_DIR': self.sessions / 'detail',
                           'PLACES_JSON': self.root / 'places.json'}.items():
            self.stack.enter_context(patch.object(b, key, str(value)))
        self.stack.enter_context(patch.dict(w.RUNTIME, connected=False, flight_id=None,
                                           current_doc=None))
        self.stack.enter_context(patch.object(w, '_logbook_pending', [False]))
        self.stack.enter_context(patch.object(w, '_logbook_reprocess_pending', [False]))
        self.stack.enter_context(patch.object(w, '_logbook_building', False))
        import flightprefs
        self.stack.enter_context(patch.object(flightprefs, 'PREFS_PATH', str(self.root / 'prefs.json')))

    def flight(self):
        fid = 'synthetic-flight'
        track = self.sessions / (fid + '.jsonl')
        points = [{'ts': '1999-01-01T00:0%d:00Z' % i, 'lat': 40 + i * .01,
                   'lon': -105, 'alt': 6000, 'gs': 90, 'vs': 0,
                   'on_ground': False, 'heading': 0, 'airspeed': 95} for i in range(5)]
        track.write_text(''.join(json.dumps(row) + '\n' for row in points))
        meta = self.sessions / (fid + '.meta.json')
        p.atomic_json(str(meta), {'sortie_id': fid, 'category': 'Airplane', 'vs0': 40,
                                  'aircraft': 'Synthetic Type', 'ended_at': '1999-01-01T00:05:00Z'})
        return fid, track, meta

    def test_reprocess_bypasses_poisoned_flight_cache(self):
        self.flight()
        b.scan_flights()
        doc = b.read_json(b.CACHE_JSON)
        for hit in doc['flights'].values():
            hit['rec']['vs0'] = 999
        p.atomic_json(b.CACHE_JSON, doc)
        b.build(bake_maps=False, allow_network=False, force=True)
        fresh = b.read_json(b.CACHE_JSON)
        self.assertEqual(next(iter(fresh['flights'].values()))['rec']['vs0'], 40)

    def test_clip_change_invalidates_only_its_sortie(self):
        fid, _, _ = self.flight()
        group = b.scan_flights()
        event = {'flight_id': fid, 'leg': 1, 'kind': 'landing', 'clip': 'synthetic-clip.json'}
        clip = self.sessions / 'clips' / 'synthetic-clip.json'
        clip.write_text('{}')
        sig = b.sortie_signature(group, 'fixed', {fid: [event]}, fid)
        other = b.sortie_signature(group, 'fixed', {}, 'unrelated')
        clip.write_text('{"points": []}')
        self.assertNotEqual(sig, b.sortie_signature(group, 'fixed', {fid: [event]}, fid))
        self.assertEqual(other, b.sortie_signature(group, 'fixed', {}, 'unrelated'))

    def test_exclusion_change_does_not_invalidate_other_sortie(self):
        fid, _, _ = self.flight()
        group = b.scan_flights()
        before = b.sortie_signature(group, b.global_signature(False), {}, fid)
        p.atomic_json(b.EXCLUDED_JSON, {'schema': 1, 'sorties': {'other': {}}, 'legs': {}})
        self.assertEqual(before, b.sortie_signature(group, b.global_signature(False), {}, fid))

    def test_partial_delete_survives_rebuild_and_handler_retry(self):
        fid, track, meta = self.flight()
        p.atomic_json(b.EXCLUDED_JSON, {'schema': 1, 'sorties': {fid: {}}, 'legs': {}})
        b.build(bake_maps=False, allow_network=False)
        remove = os.remove
        def fail(path):
            if p.identity(path) == p.identity(str(meta)):
                raise PermissionError('synthetic locked metadata')
            return remove(path)
        with patch('os.remove', side_effect=fail):
            result = w.purge_hidden('sortie', fid, dry_run=False)
        self.assertFalse(result['ok'])
        self.assertFalse(track.exists())
        self.assertTrue(meta.exists())
        hidden = b.read_json(b.LOGBOOK_JSON)['hidden']
        self.assertEqual(len(hidden), 1)
        self.assertTrue(hidden[0]['purge_pending'])
        self.assertFalse(w.set_hidden('sortie', fid, hide=False)['ok'])
        retry = w.purge_hidden('sortie', fid, dry_run=False)
        self.assertTrue(retry['ok'], retry)
        self.assertFalse(meta.exists())
        self.assertEqual(b.read_json(b.LOGBOOK_JSON)['hidden'], [])

    def test_an_unrecoverable_touchdown_zero_is_not_a_greaser(self):
        """The sim's unlatched zero, with no clip to recover it from.

        grade_for_rate(0.0) returns ("A", "Butter"), so leaving the zero in
        place awarded the best landing grade in the book to a reading that
        does not exist. Reachable by deleting a clip, which the UI offers.

        Asserted on the leg the user sees - its rate, its landing grade and
        its passenger text - because a test on the recovery function's
        return value passes while the leg still says Butter.
        """
        fid, track, _ = self.flight()
        Path(b.EVENTS_JSONL).write_text(''.join(json.dumps(row) + chr(10) for row in [
            {'type': 'takeoff', 'kind': 'takeoff', 'flight_id': fid, 'sortie_id': fid,
             'leg': 1, 'time': '1999-01-01T00:00:30Z',
             'lat': 40.0, 'lon': -105.0},
            {'type': 'landing', 'kind': 'landing', 'flight_id': fid, 'sortie_id': fid,
             'leg': 1, 'time': '1999-01-01T00:04:00Z', 'rate_fpm': 0.0,
             'lat': 40.04, 'lon': -105.0,
             'clip': 'clips/no-such-clip.json'},
        ]))
        b.build(bake_maps=False, allow_network=False, force=True)
        # Legs live in the per-sortie detail files, not in the index.
        legs = []
        for name in sorted(os.listdir(b.DETAIL_DIR)):
            detail = b.read_json(os.path.join(b.DETAIL_DIR, name)) or {}
            legs.extend(detail.get('legs') or [])
        self.assertTrue(legs, 'the fixture produced no leg to grade')
        leg = legs[0]

        self.assertIsNone(leg.get('landing_rate_fpm'),
                          'an unrecoverable zero was reported as a measured rate')
        self.assertIsNone(leg.get('landing_grade'),
                          'a touchdown that was never measured was given the '
                          'grade %r' % (leg.get('landing_grade'),))
        text = json.dumps(leg.get('passenger') or {}).lower()
        for word in ('butter', 'greaser'):
            self.assertNotIn(word, text,
                             'the passenger note calls an unmeasured landing a '
                             '%s' % word)

    def test_compact_is_opt_in_and_only_the_clip_writer_asks(self):
        """Hand-editable documents must stay hand-editable.

        settings.json is documented as something you can delete or edit to
        get back to known-good. A compact writer flipped on by default would
        quietly take that away, so compactness is a per-call argument and the
        default must stay off.
        """
        doc = {'a': 1, 'points': [{'t': 1, 'lat': 2}, {'t': 2, 'lat': 3}]}
        plain = self.root / 'plain.json'
        tight = self.root / 'tight.json'
        p.atomic_json(str(plain), doc)
        p.atomic_json(str(tight), doc, compact=True)

        plain_text = plain.read_text(encoding='utf-8')
        tight_text = tight.read_text(encoding='utf-8')
        self.assertIn('  ', plain_text, 'the default writer stopped indenting')
        self.assertNotIn('  ', tight_text, 'compact output is not compact')
        self.assertLess(len(tight_text), len(plain_text))
        self.assertEqual(json.loads(plain_text), json.loads(tight_text))
        self.assertEqual(json.loads(tight_text), doc)

        # The clip writer no longer goes through atomic_json at all - it
        # appends records - but its output must stay compact for the same
        # reason, and every record must be one line.
        clip = w.OpenClip('landing', 'synthetic-flight', 1, 'Synthetic Type',
                          {'t': 1000.0, 'ts': '1999-01-01T00:04:00Z',
                           'lat': 40.0, 'lon': -105.0},
                          [{'t': 999.0, 'lat': 40.0, 'lon': -105.0,
                            'alt': 100.0, 'on_ground': False}],
                          10.0, 123.0)
        try:
            written = Path(clip.path).read_text(encoding='utf-8')
            self.assertNotIn('  ', written,
                             'the clip writer is not writing compactly')
            records = [json.loads(line) for line in written.splitlines()]
            self.assertEqual(records[0]['k'], 'hdr')
            self.assertEqual(records[0]['id'], clip.clip_id)
        finally:
            clip.release()

    # -- append_lines: the four failure paths -------------------------------

    def _boom_after(self, nbytes, how='raise'):
        """An append handle that gets nbytes in and then goes wrong.

        It honours the buffering argument, which is the point: a buffered
        handle holds what it was given and flushes on close, so bytes written
        during the failure reappear *after* the rollback has cut the file
        back. That is the resurrection this code opens unbuffered to avoid,
        and a mock that ignored buffering could not tell the two apart.

        how='raise' fails outright. how='short' returns a count smaller than
        the blob without raising, which is the quieter version and the one a
        writer that trusts its return value will miss.
        """
        real_open = open
        class Boom(object):
            def __init__(self, handle, buffered):
                self.handle = handle
                self.buffered = buffered
                self.held = b''
            def write(self, blob):
                if self.buffered:
                    # Nothing reaches the file yet; it lands on close.
                    self.held = blob
                    if how == 'short':
                        return nbytes
                    raise IOError('synthetic disk error')
                self.handle.write(blob[:nbytes])
                self.handle.flush()
                if how == 'short':
                    return nbytes
                raise IOError('synthetic disk error')
            def fileno(self):
                return self.handle.fileno()
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                if self.buffered and self.held:
                    self.handle.write(self.held)   # flush-on-close
                self.handle.close()
                return False
        def opener(path, mode='r', *args, **kwargs):
            buffering = kwargs.get('buffering')
            if buffering is None and len(args) >= 1:
                buffering = args[0]
            handle = real_open(path, mode)
            if 'a' in mode:
                return Boom(handle, buffered=(buffering != 0))
            return handle
        return opener

    def test_a_short_write_is_not_taken_for_a_whole_one(self):
        """A write can report fewer bytes than it was given without raising.

        That is the quiet version of a partial append: no exception, just a
        file that is a few bytes shorter than the writer believes. The count
        has to be checked, or the next append starts mid-record.
        """
        path = str(self.root / 'clip.jsonl')
        p.append_lines(path, [{'k': 'hdr', 'v': 1}])
        before = Path(path).read_bytes()
        with patch('builtins.open', self._boom_after(6, how='short')):
            with self.assertRaises(IOError):
                p.append_lines(path, [{'k': 'p', 't': 1}])
        self.assertEqual(Path(path).read_bytes(), before,
                         'a short write was accepted and left a partial record')

    def test_the_file_length_is_restored_however_the_append_failed(self):
        """Whatever the handle did on the way out, the file goes back.

        The rollback runs after the handle is closed, so even a buffered
        flush-on-close is cut away - which is why buffering is not what makes
        this safe. It is worth pinning the outcome anyway: the property that
        matters is that the file returns to a length that was whole lines,
        by whatever route.
        """
        path = str(self.root / 'clip.jsonl')
        p.append_lines(path, [{'k': 'hdr', 'v': 1}])
        before = Path(path).read_bytes()
        for how in ('raise', 'short'):
            with patch('builtins.open', self._boom_after(5, how=how)):
                with self.assertRaises(IOError):
                    p.append_lines(path, [{'k': 'p', 't': 1}])
            self.assertEqual(Path(path).read_bytes(), before,
                             'file not restored after a %s failure' % how)

    def test_a_partial_append_leaves_the_file_exactly_as_it_was(self):
        """Several whole records and half of another can reach the disk.

        Retrying then duplicates what landed and splices JSON onto the
        damaged line; advancing past it drops what did not. Neither is
        acceptable, so the append is all or nothing.
        """
        path = str(self.root / 'clip.jsonl')
        p.append_lines(path, [{'k': 'hdr', 'v': 1}])
        p.append_lines(path, [{'k': 'p', 't': i} for i in range(3)])
        before = Path(path).read_bytes()

        batch = [{'k': 'p', 't': 99}, {'k': 'p', 't': 100}]
        with patch('builtins.open', self._boom_after(9)):
            with self.assertRaises(IOError):
                p.append_lines(path, batch)
        self.assertEqual(Path(path).read_bytes(), before,
                         'a failed append left bytes behind')

        # the same batch retried lands exactly once
        p.append_lines(path, batch)
        rows = [json.loads(line) for line in
                Path(path).read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r.get('t') for r in rows if r['k'] == 'p'],
                         [0, 1, 2, 99, 100])

    def test_a_sync_failure_keeps_the_records(self):
        """The bytes are written. Discarding them is the worse error."""
        path = str(self.root / 'clip.jsonl')
        p.append_lines(path, [{'k': 'hdr', 'v': 1}])
        before = Path(path).read_bytes()
        def no_durability(fd):
            raise OSError('synthetic sync failure')
        with patch('os.fsync', side_effect=no_durability):
            with self.assertRaises(p.SyncFailed):
                p.append_lines(path, [{'k': 'end', 'v': 1}], fsync=True)
        after = Path(path).read_bytes()
        self.assertGreater(len(after), len(before))
        rows = [json.loads(line) for line in after.decode('utf-8').splitlines()]
        self.assertEqual(rows[-1], {'k': 'end', 'v': 1})

    def test_a_failed_rollback_says_so_rather_than_guessing(self):
        """If the cut back fails, where the last whole line ends is unknown.

        Continuing to append past a boundary nobody can locate is how a clip
        becomes quietly unparseable, so this raises its own error and the
        caller is expected to stop writing that file.
        """
        path = str(self.root / 'clip.jsonl')
        p.append_lines(path, [{'k': 'hdr', 'v': 1}])
        real_open = open
        def opener(target, mode='r', *args, **kwargs):
            if 'a' in mode:
                return self._boom_after(4)(target, mode, *args, **kwargs)
            if '+' in mode:
                raise OSError('synthetic truncate failure')
            return real_open(target, mode, *args, **kwargs)
        with patch('builtins.open', opener):
            with self.assertRaises(p.RollbackFailed):
                p.append_lines(path, [{'k': 'p', 't': 1}])

    def test_an_empty_batch_reports_the_length_and_writes_nothing(self):
        path = str(self.root / 'clip.jsonl')
        length = p.append_lines(path, [{'k': 'hdr', 'v': 1}])
        before = Path(path).read_bytes()
        self.assertEqual(p.append_lines(path, []), length)
        self.assertEqual(Path(path).read_bytes(), before)

    def test_the_returned_length_is_the_committed_offset(self):
        """It is what a writer keeps as 'known to be whole lines'."""
        path = str(self.root / 'clip.jsonl')
        n = p.append_lines(path, [{'k': 'hdr', 'v': 1}])
        self.assertEqual(n, os.path.getsize(path))
        n = p.append_lines(path, [{'k': 'p', 't': 1}, {'k': 'p', 't': 2}])
        self.assertEqual(n, os.path.getsize(path))

    # -- the clip file format ----------------------------------------------

    def _clip(self, points=3, end=True, meta=True):
        """A new-format clip on disk, written the way the writer writes one."""
        cid = 'synthetic-flight-leg1-landing'
        path = cf.write_path(str(self.sessions / 'clips'), cid)
        doc = {'id': cid, 'flight_id': 'synthetic-flight',
               'sortie_id': 'synthetic-flight', 'leg': 1, 'kind': 'landing',
               'aircraft': 'Synthetic Type', 'livery': None,
               't0': '1999-01-01T00:04:00Z', 't_event': '1999-01-01T00:04:00Z',
               'att_unit': 'deg', 't1': '1999-01-01T00:04:10Z',
               'rate_fpm': 123.0, 'lat': 40.0, 'lon': -105.0}
        batch = [cf.header_record(doc)]
        batch += [{'k': 'p', 't': 1000.0 + i, 'lat': 40.0, 'lon': -105.0,
                   'alt': 100.0, 'on_ground': False} for i in range(points)]
        if meta:
            batch.append(cf.meta_record(doc))
        if end:
            batch.append(cf.end_record())
        p.append_lines(path, batch)
        return cid, path, doc

    def test_a_clip_round_trips_through_the_line_format(self):
        cid, path, doc = self._clip()
        back = cf.read_clip(path)
        self.assertEqual(back['id'], cid)
        self.assertEqual(back['kind'], 'landing')
        self.assertEqual(back['rate_fpm'], 123.0)
        self.assertEqual(len(back['points']), 3)
        self.assertTrue(back['complete'])
        self.assertFalse(back['incomplete'])
        self.assertEqual(back['damaged_lines'], 0)

    def test_the_last_metadata_snapshot_wins_including_its_nulls(self):
        """A snapshot replaces the mutable state; it does not merge into it."""
        cid, path, doc = self._clip(end=False, meta=False)
        p.append_lines(path, [cf.meta_record(dict(doc, rate_fpm=999.0))])
        p.append_lines(path, [cf.meta_record(dict(doc, rate_fpm=None)),
                              cf.end_record()])
        back = cf.read_clip(path)
        self.assertIsNone(back['rate_fpm'],
                          'an explicit null did not replace the earlier value')

    def test_a_torn_final_line_is_not_counted_as_damage(self):
        """An append interrupted mid-write. Ordinary, not corruption."""
        cid, path, _ = self._clip(end=False)
        with open(path, 'ab') as f:
            f.write(b'{"k":"p","t":9999,"la')       # no newline: torn
        back = cf.read_clip(path)
        self.assertEqual(len(back['points']), 3, 'the torn record was kept')
        self.assertEqual(back['damaged_lines'], 0,
                         'a torn tail was counted as corruption')
        self.assertTrue(back['torn_tail'])
        self.assertTrue(back['incomplete'], 'no end record, so not complete')

    def test_a_complete_but_unparseable_final_line_is_damage(self):
        """It was not interrupted - it terminated. Something wrote garbage."""
        cid, path, _ = self._clip(end=False)
        with open(path, 'ab') as f:
            f.write(b'{"k":"p","t":9999,"la' + chr(10).encode())
        back = cf.read_clip(path)
        self.assertEqual(back['damaged_lines'], 1,
                         'a terminated bad record was treated as a torn tail')
        self.assertFalse(back['torn_tail'])
        self.assertTrue(back['incomplete'])

    def test_a_damaged_interior_record_is_skipped_and_counted(self):
        cid, path, _ = self._clip()
        raw = Path(path).read_bytes().split(chr(10).encode())
        raw[2] = b'{"k":"p","t":not json}'
        Path(path).write_bytes(chr(10).encode().join(raw))
        back = cf.read_clip(path)
        self.assertEqual(back['damaged_lines'], 1)
        self.assertEqual(len(back['points']), 2)
        self.assertTrue(back['incomplete'])

    def test_a_clip_without_a_header_is_unreadable_not_empty(self):
        """There is nothing to identify or place. Do not synthesize one."""
        path = cf.write_path(str(self.sessions / 'clips'), 'headerless')
        p.append_lines(path, [{'k': 'p', 't': 1.0}, {'k': 'end', 'v': 1}])
        self.assertIsNone(cf.read_clip(path))

    def test_no_end_record_means_completion_was_not_recorded(self):
        """Parsing cannot tell a finished clip from one that stopped."""
        cid, path, _ = self._clip(end=False)
        back = cf.read_clip(path)
        self.assertFalse(back['complete'])
        self.assertTrue(back['incomplete'])
        self.assertEqual(back['damaged_lines'], 0,
                         'an unfinished clip is not a damaged one')

    def test_an_incomplete_clip_declines_touchdown_recovery(self):
        """Deriving a landing rate from a recording with holes in it is the
        unlatched-zero error arrived at from the other end."""
        cid, path, _ = self._clip(end=False)
        with patch.object(b, 'CLIPS_DIR', str(self.sessions / 'clips')):
            self.assertEqual(b.clip_points(cid), [],
                             'an incomplete clip supplied points to derive from')

    def test_a_legacy_clip_is_still_found_and_read(self):
        """Lookup has to probe, not just parse: a reader that understands the
        old format is useless if the path builder only makes .jsonl."""
        cid = 'synthetic-flight-leg2-takeoff'
        legacy = os.path.join(str(self.sessions / 'clips'), cid + cf.LEGACY_SUFFIX)
        p.atomic_json(legacy, {'id': cid, 'kind': 'takeoff', 'leg': 2,
                               'flight_id': 'synthetic-flight',
                               'aircraft': 'Synthetic Type', 't0': 'T0',
                               'points': [{'t': 1.0}, {'t': 2.0}]})
        found = cf.clip_path(str(self.sessions / 'clips'), cid)
        self.assertEqual(found, legacy)
        back = cf.read_clip(found)
        self.assertEqual(len(back['points']), 2)
        self.assertTrue(back['complete'],
                        'a whole document was written by one atomic replace')
        header = cf.read_header(found)
        self.assertEqual(header['kind'], 'takeoff')

    def test_purge_deletes_both_formats_of_one_clip(self):
        """Preferring one for reading and deleting only that one is how an
        orphan survives a delete the user was told succeeded."""
        cid = 'synthetic-flight-leg1-landing'
        clips = str(self.sessions / 'clips')
        p.append_lines(cf.write_path(clips, cid), [{'k': 'hdr', 'v': 1, 'id': cid}])
        p.atomic_json(os.path.join(clips, cid + cf.LEGACY_SUFFIX), {'id': cid})
        self.assertEqual(len(cf.paths_for(clips, cid)), 2)
        with patch.object(b, 'CLIPS_DIR', clips):
            plan = b.plan_purge({'scope': 'leg', 'sortie_id': 'synthetic-flight',
                                 'leg_key': 'k', 'flight_ids': ['synthetic-flight'],
                                 'clips': [cid]})
        names = [os.path.basename(f) for f in plan['files']]
        self.assertIn(cid + cf.SUFFIX, names)
        self.assertIn(cid + cf.LEGACY_SUFFIX, names)

    def test_an_open_clip_cannot_be_deleted(self):
        """The claim is taken before the first write, so a purge between the
        constructor and the first checkpoint cannot slip through - otherwise
        the next append recreates a file that was just deleted."""
        clip = w.OpenClip('landing', 'synthetic-flight', 1, 'Synthetic Type',
                          {'t': 1000.0, 'ts': '1999-01-01T00:04:00Z',
                           'lat': 40.0, 'lon': -105.0},
                          [{'t': 999.0, 'lat': 40.0, 'lon': -105.0,
                            'alt': 100.0, 'on_ground': False}],
                          10.0, 123.0)
        try:
            with self.assertRaises(RuntimeError):
                with p.deleting([clip.path]):
                    pass
        finally:
            clip.release()
        # released, so it can be deleted now
        with p.deleting([clip.path]):
            pass

    def test_the_listing_reads_one_line_not_the_whole_clip(self):
        cid, path, _ = self._clip(points=500)
        reads = {'bytes': 0}
        real_open = open
        class Counting(object):
            def __init__(self, handle):
                self.handle = handle
            def readline(self, *a):
                data = self.handle.readline(*a)
                reads['bytes'] += len(data)
                return data
            def read(self, *a):
                data = self.handle.read(*a)
                reads['bytes'] += len(data)
                return data
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                self.handle.close()
                return False
        def opener(target, mode='r', *args, **kwargs):
            handle = real_open(target, mode, *args, **kwargs)
            if str(target) == path:
                return Counting(handle)
            return handle
        with patch('builtins.open', opener):
            cf.read_header(path)
        whole = os.path.getsize(path)
        self.assertLess(reads['bytes'], whole / 4.0,
                        'the listing read %d of %d bytes; it is scanning the '
                        'whole clip' % (reads['bytes'], whole))

    # -- the clip lifecycle, which the format tests did not drive -----------

    def _open_clip(self, after=5.0, points=3):
        pts = [{'t': 1000.0 + i, 'lat': 40.0, 'lon': -105.0, 'alt': 100.0,
                'on_ground': False} for i in range(points)]
        return w.OpenClip('landing', 'synthetic-flight', 1, 'Synthetic Type',
                          {'t': 1000.0, 'ts': '1999-01-01T00:04:00Z',
                           'lat': 40.0, 'lon': -105.0},
                          pts, after, 123.0)

    def test_a_clip_that_simply_expires_is_finalized(self):
        """The ordinary end of a clip, and the one the format tests missed.

        add_sample used to set done and write - which had been the whole of
        finish() before the format change. Afterwards a normally completed
        clip got no end record, held its recording claim until the watcher
        restarted, and was refused as a source for touchdown recovery because
        it read as incomplete.
        """
        clip = self._open_clip(after=5.0)
        clip.add_sample({'t': 1006.0, 'lat': 40.0, 'lon': -105.0,
                         'alt': 100.0, 'on_ground': True})
        self.assertTrue(clip.done)
        doc = cf.read_clip(clip.path)
        self.assertTrue(doc['complete'], 'a normally expired clip has no end record')
        self.assertFalse(doc['incomplete'])
        self.assertFalse(clip._claimed, 'the recording claim was never released')
        # and it can therefore be deleted without a watcher restart
        with p.deleting([clip.path]):
            pass

    def test_the_tracker_lifecycle_finalizes_every_clip(self):
        """Driven through feed(), the way the sampling loop does it."""
        clip = self._open_clip(after=5.0)
        tracker = type('T', (), {'open': [clip]})()
        for t in (1001.0, 1006.0):
            for c in list(tracker.open):
                c.add_sample({'t': t, 'lat': 40.0, 'lon': -105.0,
                              'alt': 100.0, 'on_ground': t > 1005})
                if c.done:
                    tracker.open.remove(c)
        self.assertEqual(tracker.open, [], 'the clip was never closed out')
        self.assertTrue(cf.read_clip(clip.path)['complete'])

    def test_an_end_record_is_withheld_when_the_data_write_failed(self):
        """An end record claims everything before it landed.

        Appending one over a failed data write produces a clip that reads
        complete while points are missing, which is worse than an obviously
        unfinished one.
        """
        clip = self._open_clip(after=60.0, points=2)
        clip.points.append({'t': 1002.0, 'lat': 40.1, 'lon': -105.0,
                            'alt': 90.0, 'on_ground': False})
        real = p.append_lines
        def failing(path, objs, fsync=False):
            if any(o.get('k') == 'p' for o in objs):
                raise IOError('synthetic disk error')
            return real(path, objs, fsync=fsync)
        with patch.object(p, 'append_lines', side_effect=failing):
            clip.finish()
        doc = cf.read_clip(clip.path)
        self.assertFalse(doc['complete'],
                         'an end record was appended over a failed write')
        self.assertTrue(doc['incomplete'])
        self.assertLess(len(doc['points']), len(clip.points))
        self.assertFalse(clip._claimed, 'the claim outlived a failed finish')

    def test_a_failed_rollback_during_finish_is_terminal(self):
        """Retrying would append against a boundary nobody can locate."""
        clip = self._open_clip(after=60.0, points=2)
        real = p.append_lines
        calls = {'n': 0}
        def failing(path, objs, fsync=False):
            if any(o.get('k') == 'end' for o in objs):
                calls['n'] += 1
                raise p.RollbackFailed('synthetic unknown boundary')
            return real(path, objs, fsync=fsync)
        with patch.object(p, 'append_lines', side_effect=failing):
            clip.finish()
        self.assertEqual(calls['n'], 1,
                         'the end append was retried after a failed rollback')
        self.assertTrue(clip._stopped)
        self.assertFalse(cf.read_clip(clip.path)['complete'])

    def test_an_unsupported_format_version_is_refused(self):
        """A newer writer's records may not mean what these do."""
        path = cf.write_path(str(self.sessions / 'clips'), 'from-the-future')
        p.append_lines(path, [{'k': 'hdr', 'v': 999, 'id': 'from-the-future'},
                              {'k': 'p', 't': 1.0},
                              {'k': 'end', 'v': 999}])
        self.assertIsNone(cf.read_clip(path),
                          'a version this reader does not know was interpreted')
        self.assertIsNone(cf.read_header(path))
        self.assertTrue(cf.supported(cf.VERSION))
        self.assertFalse(cf.supported(cf.VERSION + 1))

    def test_the_leg_reports_an_incomplete_recording(self):
        """Promised in the design and absent from the first implementation."""
        clips = str(self.sessions / 'clips')
        cid = 'synthetic-flight-leg1-landing'
        path = cf.write_path(clips, cid)
        p.append_lines(path, [{'k': 'hdr', 'v': 1, 'id': cid, 'kind': 'landing'},
                              {'k': 'p', 't': 1.0}])          # no end record
        with patch.object(b, 'CLIPS_DIR', clips):
            status = b.clip_status(cid)
        self.assertIsNotNone(status)
        self.assertTrue(status['incomplete'])
        self.assertFalse(status['complete'])
        self.assertFalse(status['unreadable'])
        with patch.object(b, 'CLIPS_DIR', clips):
            self.assertIsNone(b.clip_status('no-such-clip'),
                              'a missing clip is not an incomplete one')

    def test_a_takeoff_recording_reports_its_status_too(self):
        """Status was carried for landings only, so a truncated takeoff
        replayed short with nothing anywhere saying why."""
        clips = str(self.sessions / 'clips')
        cid = 'synthetic-flight-leg1-takeoff'
        path = cf.write_path(clips, cid)
        p.append_lines(path, [{'k': 'hdr', 'v': 1, 'id': cid, 'kind': 'takeoff'},
                              {'k': 'p', 't': 1.0}])          # no end record
        with patch.object(b, 'CLIPS_DIR', clips):
            status = b.clip_status(cid)
        self.assertIsNotNone(status)
        self.assertTrue(status['incomplete'])
        src = io.open(os.path.join(BASE_DIR, 'logbook_build.py'),
                      encoding='utf-8').read()
        self.assertEqual(src.count('"clip_status": clip_status('), 2,
                         'clip_status is attached to only one of the two clips')
        js = io.open(os.path.join(BASE_DIR, 'logbook.js'), encoding='utf-8').read()
        self.assertIn('["takeoff", "landing"].forEach', js,
                      'the UI still reports one clip kind only')

    def test_the_incomplete_explanation_matches_the_fault(self):
        """A finished clip with unreadable records is not the same news as an
        unfinished one, and the tooltip said the latter for both."""
        js = io.open(os.path.join(BASE_DIR, 'logbook.js'), encoding='utf-8').read()
        start = js.index('["takeoff", "landing"].forEach')
        block = js[start:start + 2000]
        self.assertIn('if (damaged)', block)
        self.assertIn('if (!cs.complete)', block,
                      'the unfinished-capture wording is not conditional')

    def test_only_the_watcher_says_a_clip_is_recording(self):
        """Claims are in memory; a build elsewhere cannot see them."""
        clip = self._open_clip(after=60.0)
        try:
            # by path: CLIP_ID_RE only admits real flt- ids, and the point
            # here is the recording flag rather than the id grammar
            with patch.dict(w.RUNTIME, {'open_clip_ids': [clip.clip_id]}):
                doc = w.load_clip(path=clip.path)
                self.assertTrue(doc['recording'])
            with patch.dict(w.RUNTIME, {'open_clip_ids': []}):
                doc = w.load_clip(path=clip.path)
                self.assertFalse(doc['recording'])
            # the module that any process can run reports no such thing
            self.assertNotIn('recording', cf.read_clip(clip.path))
        finally:
            clip.release()

    def test_active_recording_cannot_be_deleted(self):
        fid, track, _ = self.flight()
        p.claim_recording(str(track))
        try:
            result = b.purge({'scope': 'sortie', 'sortie_id': fid, 'flight_ids': [fid]})
            self.assertFalse(result['ok'])
            self.assertTrue(track.exists())
            self.assertFalse(Path(b.EXCLUDED_JSON).exists())
        finally:
            p.release_recording(str(track))

    def test_deleted_recording_cannot_resume_during_commit(self):
        _, track, _ = self.flight()
        with p.deleting([str(track)]):
            with self.assertRaises(RuntimeError):
                p.claim_recording(str(track))

    def test_append_during_filter_preparation_survives(self):
        p.atomic_json(b.EXCLUDED_JSON, {})
        Path(b.EVENTS_JSONL).write_text('{"drop": true}\n')
        preparing, appended = threading.Event(), threading.Event()
        def drop(row):
            if row.get('drop'):
                preparing.set()
                self.assertTrue(appended.wait(3))
            return row.get('drop', False)
        def append():
            self.assertTrue(preparing.wait(3))
            w.append_jsonl(b.EVENTS_JSONL, {'keep': True})
            appended.set()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            writer = pool.submit(append)
            with p.maintenance(str(self.root)):
                self.assertEqual(b._rewrite_events(drop), 1)
            writer.result(timeout=3)
        self.assertEqual(json.loads(Path(b.EVENTS_JSONL).read_text()), {'keep': True})

    def test_concurrent_hides_keep_both_changes(self):
        with patch.object(w, 'rebuild_logbook_now', return_value={'ok': True}):
            with concurrent.futures.ThreadPoolExecutor() as pool:
                results = list(pool.map(lambda fid: w.set_hidden('sortie', fid), ['one', 'two']))
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual(set(b.read_json(b.EXCLUDED_JSON)['sorties']), {'one', 'two'})

    def test_failed_atomic_write_is_not_reported_as_saved(self):
        with patch.object(w, 'ATOMIC_WRITE_TRIES', 1), patch('os.replace', side_effect=PermissionError('disk')):
            with self.assertRaises(PermissionError):
                w.atomic_write(str(self.root / 'test.json'), {})
            self.assertFalse(w.set_hidden('sortie', 'one')['ok'])
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_settings_failed_save_keeps_memory_and_disk(self):
        path = str(self.root / 'settings.json')
        with patch.object(settings, 'SETTINGS_PATH', path), patch.object(settings, '_values', {}):
            p.atomic_json(path, {})
            with patch('os.replace', side_effect=PermissionError('disk')):
                with self.assertRaises(PermissionError):
                    settings.update({'landing_before': 30})
            self.assertEqual(settings._values, {})
            self.assertEqual(json.loads(Path(path).read_text()), {})

    def test_forced_maps_and_reprocess_wait_while_airborne(self):
        w.RUNTIME.update(connected=True, flight_id='synthetic', current_doc={'on_ground': False, 'gs': 100})
        with patch.object(b, 'build') as build:
            result = w.rebuild_logbook_now(force=True, reprocess=True)
        self.assertTrue(result['deferred'])
        build.assert_not_called()
        self.assertTrue(w._logbook_reprocess_pending[0])

    def test_network_permission_rechecked_for_each_request(self):
        import tiles
        with patch('urllib.request.urlopen') as network:
            tiles.fetch_tile(str(self.root), 19, 123456, 654321, allow_network=lambda: False)
        network.assert_not_called()

    def test_maintenance_excludes_other_job_but_not_append(self):
        entered = threading.Event()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            def job():
                with p.maintenance(str(self.root)):
                    entered.set()
            with p.maintenance(str(self.root)):
                waiting = pool.submit(job)
                pool.submit(w.append_jsonl, b.EVENTS_JSONL, {'keep': 1}).result(timeout=3)
                self.assertFalse(entered.is_set())
            waiting.result(timeout=3)
        self.assertTrue(entered.is_set())

    def test_backup_restore_and_same_size_corruption(self):
        self.flight()
        output = self.root / 'backups'
        # Source is the sessions fixture's parent; destination must be separate.
        with tempfile.TemporaryDirectory() as dest:
            command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                       str(Path(__file__).with_name('backup.ps1')), '-From', str(self.root),
                       '-To', dest, '-RequireQuiet', '-Verify']
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archive = next(Path(dest).glob('afterflight-backup-*'))
            manifest = json.loads((archive / 'manifest.json').read_text(encoding='utf-8-sig'))
            self.assertEqual(manifest['consistency'], 'quiescent')
            with tempfile.TemporaryDirectory() as restored:
                for row in manifest['files']:
                    target = Path(restored) / row['path']
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(archive / row['path'], target)
                with patch.object(b, 'SESSIONS', str(Path(restored) / 'sessions')):
                    self.assertEqual(len(b.scan_flights(force=True)), 1)
            file = archive / manifest['files'][0]['path']
            data = file.read_bytes()
            file.write_bytes(b'X' + data[1:])
            bad = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                  str(Path(__file__).with_name('verify-backup.ps1')), '-Backup', str(archive)],
                                 capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)


if __name__ == '__main__':
    unittest.main()

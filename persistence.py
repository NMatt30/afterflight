"""Small persistence primitives shared by the recorder and maintenance jobs.

Document locks never cover grading or map rendering. Maintenance jobs serialize
with one another, including a separate command-line builder, not with capture.
"""
import contextlib
import functools
import json
import os
import tempfile
import threading
import time

class MaintenanceDeferred(RuntimeError):
    pass


class SyncFailed(IOError):
    """The append landed but could not be forced to disk. The records stand -
    they are complete and readable - they are just not durable against a power
    loss. Never a reason to roll them back."""
    pass


class RollbackFailed(IOError):
    """An append failed and the file could not be cut back to a known
    boundary. Stop appending to it; where the last whole line ends is no
    longer known."""
    pass


_guard = threading.RLock()
_locks = {}
_active = set()
_reserved = set()
_local = threading.local()


def identity(path):
    return os.path.normcase(os.path.abspath(path))


def document_lock(path):
    with _guard:
        return _locks.setdefault(identity(path), threading.RLock())


def atomic_json(path, obj, fsync=True, compact=False):
    """Publish a complete document, or raise. Never share a staging name.

    compact drops the indentation and the spaces after separators. It is off
    by default and has to be asked for per call, never flipped here: most of
    what goes through this function is meant to be opened and read by a
    person - settings.json is documented as something you can delete or edit
    to get back to known-good, and a compact settings file would make that
    promise harder to keep. Only the clip writer asks for it, because a clip
    is machine-written, machine-read, and the largest thing this app produces.
    """
    path = os.path.abspath(path)
    with document_lock(path):
        fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.',
                                   suffix='.tmp', dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                if compact:
                    json.dump(obj, f, separators=(',', ':'))
                else:
                    json.dump(obj, f, indent=2)
                f.write('\n')
                f.flush()
                if fsync:
                    os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return path


def append_lines(path, objs, fsync=False):
    """Append objects as JSON lines, or leave the file exactly as it was.

    Returns the file length after the append, which the caller keeps as the
    last offset known to be whole lines. Pass it nothing and it reports the
    current length without touching the file.

    An append that fails part way is the whole problem this solves. Several
    complete lines and half of another can reach the disk before an error;
    retrying the batch would then duplicate the ones that landed and splice
    valid JSON onto the damaged line, while advancing past them would drop the
    ones that did not. So a failure rolls back to where the file started and
    raises, and the caller retries the same batch unchanged. A line is written
    once or not yet.

    The handle is unbuffered and the payload is one write. That is not a
    micro-optimization: a buffered handle flushes on close, so bytes could be
    pushed out during the unwind and reappear past the point the rollback had
    just removed.

    Single writer per file, under the document lock, so seeking to a recorded
    length is safe - nothing else appends between the measurement and the
    truncate.
    """
    path = os.path.abspath(path)
    with document_lock(path):
        try:
            start = os.path.getsize(path)
        except OSError:
            start = 0
        if not objs:
            return start
        blob = b''.join(
            (json.dumps(o, separators=(',', ':')) + chr(10)).encode('utf-8')
            for o in objs)
        # A failed write is rolled back. A failed sync is not: those bytes are
        # written, and throwing away a complete record because the disk would
        # not promise durability is the worse of the two errors. The caller is
        # told which happened so it can keep going rather than retry a batch
        # that already landed.
        wrote = False
        try:
            with open(path, 'ab', buffering=0) as f:
                written = f.write(blob)
                if written != len(blob):
                    raise IOError('short append: %d of %d bytes'
                                  % (written or 0, len(blob)))
                wrote = True
                if fsync:
                    os.fsync(f.fileno())
        except Exception as exc:
            if not wrote:
                _rollback(path, start)
                raise
            raise SyncFailed('%s: %d bytes appended but not synced (%s)'
                             % (path, len(blob), exc))
        return start + len(blob)


def _rollback(path, length):
    """Cut the file back to a length that was whole lines.

    A failure here is worse than the append failure that caused it: the file
    may now end mid-line and nobody knows where the boundary was. Say so by
    raising RollbackFailed, so the caller stops appending to this file rather
    than writing past a boundary it cannot locate.
    """
    try:
        with open(path, 'r+b') as f:
            f.truncate(length)
    except OSError as exc:
        raise RollbackFailed('%s: could not cut back to %d bytes (%s)'
                             % (path, length, exc))


def claim_recording(path):
    with _guard:
        key = identity(path)
        if key in _reserved:
            raise RuntimeError('recording is being deleted; cannot resume it')
        _active.add(key)


def release_recording(path):
    with _guard:
        _active.discard(identity(path))


@contextlib.contextmanager
def deleting(paths):
    keys = {identity(p) for p in paths}
    with _guard:
        if keys & (_active | _reserved):
            raise RuntimeError('cannot delete a recording that is still active')
        _reserved.update(keys)
    try:
        yield
    finally:
        with _guard:
            _reserved.difference_update(keys)


@contextlib.contextmanager
def maintenance(base):
    """One build/purge per tree. Reentrant within a thread; Windows OS lock
    also excludes a CLI rebuild. The recorder never takes this lock.
    """
    path = os.path.join(base, '.maintenance.lock')
    with document_lock(path):
        held = getattr(_local, 'held', set())
        key = identity(path)
        if key in held:
            yield
            return
        with open(path, 'a+b') as f:
            if os.path.getsize(path) == 0:
                f.write(b'0')
                f.flush()
            import msvcrt
            deadline = time.monotonic() + 120
            while True:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('another maintenance job is still running')
                    time.sleep(0.05)
            _local.held = held | {key}
            try:
                yield
            finally:
                _local.held = held
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def serialized(base):
    def decorate(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            with maintenance(base()):
                return fn(*args, **kwargs)
        return run
    return decorate


def filter_jsonl(path, drop):
    """Prepare outside the append lock; preserve any tail appended meanwhile.

    Callers hold the maintenance lock, so only appends can change this file
    between preparation and commit. Parsing and fsync of the old prefix do
    not hold up capture.
    """
    lock = document_lock(path)
    with lock:
        try:
            with open(path, 'rb') as f:
                original = f.read()
        except FileNotFoundError:
            return 0
    removed = 0

    def keep(raw):
        nonlocal removed
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return True
        if isinstance(obj, dict) and drop(obj):
            removed += 1
            return False
        return True

    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.',
                               suffix='.tmp', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'wb') as out:
            for raw in original.splitlines(keepends=True):
                if keep(raw):
                    out.write(raw)
            out.flush()
            os.fsync(out.fileno())
            with lock:
                with open(path, 'rb') as current:
                    current.seek(len(original))
                    tail = current.read()
                for raw in tail.splitlines(keepends=True):
                    if keep(raw):
                        out.write(raw)
                out.flush()
                os.fsync(out.fileno())
                out.close()  # Windows replacement requires the handle closed
                os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return removed

"""Where a clip lives on disk, and how to read one back.

The one place that knows the clip file layout. The `.json` suffix used to be
spelled out in seven places across two modules; every one of them now comes
through here, so a format change is a change to this file and not a hunt.

THE FORMAT
----------
One file per clip, `.jsonl`, appended from open to finish, one JSON object per
line with a `k` discriminator:

    {"k":"hdr","v":1,"id":...,"flight_id":...,"kind":...,"t0":...}
    {"k":"p","t":...,"lat":...,...}
    {"k":"meta","t1":...,"rate_fpm":...,"lat":...,"lon":...}
    {"k":"end","v":1}

`hdr` is written once, on the first point, and never rewritten - `t0` comes
from that point, so writing the header any earlier would mean revising it
later. Everything in it is immutable from then on, which is what lets the
listing read one line and stop.

`meta` is a complete snapshot of the mutable state, not a patch: each one
replaces the previous entirely, explicit nulls included, so there is no
partial-merge ambiguity.

`end` is the only evidence that capture finished. Parsing cannot tell a clip
that completed from one that stopped at a checkpoint - both are a run of valid
lines - so completion is recorded rather than inferred.

WHAT A READER MAY SAY
---------------------
Only that completion was, or was not, recorded. Claims of "currently
recording" live in the watcher, which is the only process that knows what it
is recording; recording claims are in-memory and invisible to a command-line
build or to a restored archive. An offline reader that finds no `end` line
must say "completion not recorded", never "interrupted".
"""
import glob
import json
import os

SUFFIX = ".jsonl"           # what is written now
LEGACY_SUFFIX = ".json"     # single-document clips, still read
VERSION = 1


def supported(version):
    """Versions this reader understands.

    A file from a newer writer is refused rather than interpreted: its records
    may not mean what these do, and a clip read wrongly places a ghost
    somewhere the aircraft never was.
    """
    return version == VERSION


def write_path(clips_dir, clip_id):
    """Where a new clip is written. Always the current format."""
    return os.path.join(clips_dir, clip_id + SUFFIX)


def clip_path(clips_dir, clip_id):
    """The clip on disk, whichever format it is in, or None.

    A reader that understands the old format is useless if lookup only ever
    builds a new-format path, so this probes. New format wins when both exist,
    which should not happen and is reported by paths_for().
    """
    for suffix in (SUFFIX, LEGACY_SUFFIX):
        candidate = os.path.join(clips_dir, clip_id + suffix)
        if os.path.isfile(candidate):
            return candidate
    return None


def paths_for(clips_dir, clip_id):
    """Every file this clip id occupies. Purge deletes all of them.

    Preferring one for reading and deleting only that one is how an orphan
    gets in: the survivor is invisible to the logbook and still on the disk
    after the user was told the recording was destroyed.
    """
    out = []
    for suffix in (SUFFIX, LEGACY_SUFFIX):
        candidate = os.path.join(clips_dir, clip_id + suffix)
        if os.path.isfile(candidate):
            out.append(candidate)
    return out


def iter_clip_files(clips_dir):
    """Every clip file in the directory, both formats, sorted by id."""
    found = {}
    for suffix in (SUFFIX, LEGACY_SUFFIX):
        for path in glob.glob(os.path.join(clips_dir, "*" + suffix)):
            cid = os.path.basename(path)[: -len(suffix)]
            found.setdefault(cid, []).append(path)
    return sorted((cid, paths) for cid, paths in found.items())


def id_from_path(path):
    base = os.path.basename(str(path).replace("\\", "/"))
    for suffix in (SUFFIX, LEGACY_SUFFIX):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


HEADER_KEYS = ("id", "flight_id", "sortie_id", "leg", "kind", "aircraft",
               "livery", "t0", "t_event", "att_unit")
META_KEYS = ("t1", "rate_fpm", "lat", "lon")


def header_record(doc):
    """The immutable half of a clip document, as its `hdr` line."""
    out = {"k": "hdr", "v": VERSION}
    for key in HEADER_KEYS:
        out[key] = doc.get(key)
    return out


def meta_record(doc):
    """The mutable half, as a whole snapshot rather than a patch."""
    out = {"k": "meta"}
    for key in META_KEYS:
        out[key] = doc.get(key)
    return out


def end_record():
    return {"k": "end", "v": VERSION}


def read_clip(path):
    """A clip document, or None if there is no usable header.

    Returns the same shape the rest of the app already consumes, plus:

        complete       True only if an `end` record was found
        damaged_lines  records that were present and would not parse

    A missing or unparseable header is not a clip with holes in it - there is
    nothing to identify or place. Return None rather than synthesize one.
    """
    if not path or not os.path.isfile(path):
        return None
    if str(path).endswith(LEGACY_SUFFIX):
        return _read_legacy(path)

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None

    lines = raw.split(b"\n")
    # A final line with no newline after it was interrupted mid-write. That is
    # an ordinary torn append and is dropped without being counted as damage.
    # A final line WITH its newline that will not parse was not interrupted -
    # it is corruption, and counting it as a torn tail would swallow it.
    torn_tail = bool(lines and lines[-1].strip())
    if lines and not lines[-1].strip():
        lines.pop()
    elif torn_tail:
        lines.pop()

    doc = None
    points = []
    complete = False
    damaged = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            damaged += 1
            continue
        if not isinstance(rec, dict):
            damaged += 1
            continue
        kind = rec.get("k")
        if kind == "hdr":
            if not supported(rec.get("v")):
                # Written by something newer. Its records may mean something
                # else entirely, so reading them as though they did not would
                # be worse than admitting the file cannot be read here.
                return None
            doc = {key: rec.get(key) for key in HEADER_KEYS}
        elif kind == "p":
            points.append({k: v for k, v in rec.items() if k != "k"})
        elif kind == "meta":
            if doc is not None:
                # A snapshot replaces the previous mutable state entirely.
                for key in META_KEYS:
                    doc[key] = rec.get(key)
        elif kind == "end":
            complete = True
        else:
            damaged += 1

    if doc is None:
        return None
    doc["points"] = points
    doc["complete"] = complete
    doc["damaged_lines"] = damaged
    doc["torn_tail"] = torn_tail
    doc["incomplete"] = (not complete) or bool(damaged)
    return doc


def _read_legacy(path):
    """A single-document clip, as everything before the format change."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    doc.setdefault("points", [])
    # A whole document was written by one atomic replace: it is either the
    # complete clip or it is not there at all.
    doc["complete"] = True
    doc["damaged_lines"] = 0
    doc["torn_tail"] = False
    doc["incomplete"] = False
    return doc


def read_header(path):
    """Just the identifying fields, without reading the points.

    The clip listing wants id, kind, flight_id, leg, aircraft and t0 - all of
    them header fields, and the header is immutable once written. So this
    reads one line and stops.
    """
    if not path or not os.path.isfile(path):
        return None
    if str(path).endswith(LEGACY_SUFFIX):
        doc = _read_legacy(path)
        return {key: doc.get(key) for key in HEADER_KEYS} if doc else None
    try:
        with open(path, "rb") as f:
            first = f.readline()
    except OSError:
        return None
    if not first.strip():
        return None
    try:
        rec = json.loads(first.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(rec, dict) or rec.get("k") != "hdr":
        return None
    if not supported(rec.get("v")):
        return None
    return {key: rec.get(key) for key in HEADER_KEYS}

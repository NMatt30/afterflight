"""Per-flight switches for ratings and passenger notes.

Two independent choices per sortie. Both default on, and the default itself is
a setting, so someone who never wants either can say so once instead of per
flight.

Only explicit choices are stored. A sortie absent from the file follows the
default, which means changing the default moves every flight that was never
decided individually and leaves the decided ones alone - the behavior you
want, and the reason this is not written as a full table of every sortie.

The switches gate what the builder EMITS, not what it records. Grades and
passenger prose are both derived from the track and the events, so turning a
switch back on regenerates them exactly: passenger variants are picked from a
hash of sortie_id plus leg, not from a counter, so the same flight gets the
same words it had before. Nothing here can lose data.
"""
import json
import os
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
PREFS_PATH = os.path.join(BASE, "flight_prefs.json")

SCHEMA = 1

# The two switches, and what each suppresses when off.
KEYS = ("rating", "passenger")

# Defaults for a flight nobody has decided about. Exposed as settings, so
# these constants are the source of truth and a saved setting overrides them.
RATING_DEFAULT = True
PASSENGER_DEFAULT = True

_lock = threading.Lock()


def defaults():
    """The current default for each switch, honoring any saved setting."""
    return {"rating": bool(RATING_DEFAULT),
            "passenger": bool(PASSENGER_DEFAULT)}


def load():
    """The stored overrides. Never raises; a damaged file reads as empty."""
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {"schema": SCHEMA, "sorties": {}}
    if not isinstance(doc, dict):
        return {"schema": SCHEMA, "sorties": {}}
    sorties = doc.get("sorties")
    if not isinstance(sorties, dict):
        sorties = {}
    return {"schema": doc.get("schema", SCHEMA), "sorties": sorties}


def for_sortie(sortie_id, doc=None):
    """Both switches for one flight, resolved against the defaults."""
    out = defaults()
    if not sortie_id:
        return out
    doc = doc if doc is not None else load()
    saved = (doc.get("sorties") or {}).get(sortie_id)
    if isinstance(saved, dict):
        for k in KEYS:
            if isinstance(saved.get(k), bool):
                out[k] = saved[k]
    return out


def set_for_sortie(sortie_id, rating=None, passenger=None, at=None):
    """Record a choice. Passing None for a switch leaves it as it was.

    Returns the resolved switches after the write, so a caller can report what
    the flight is actually set to rather than what it asked for.
    """
    if not sortie_id:
        raise ValueError("no flight given")
    wanted = {"rating": rating, "passenger": passenger}
    if all(v is None for v in wanted.values()):
        raise ValueError("nothing to change")
    for k, v in wanted.items():
        if v is not None and not isinstance(v, bool):
            raise ValueError("%s must be true or false" % k)
    with _lock:
        doc = load()
        doc["schema"] = SCHEMA
        entry = doc["sorties"].get(sortie_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        for k, v in wanted.items():
            if v is not None:
                entry[k] = v
        if at:
            entry["at"] = at
        # A flight set back to the defaults is forgotten rather than stored as
        # a row that says "same as default" - otherwise the file grows one
        # entry per flight ever looked at, and a later change of default
        # would silently skip them.
        d = defaults()
        if all(entry.get(k, d[k]) == d[k] for k in KEYS):
            doc["sorties"].pop(sortie_id, None)
        else:
            doc["sorties"][sortie_id] = entry
        tmp = PREFS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PREFS_PATH)
    return for_sortie(sortie_id)


def signature():
    """Cheap fingerprint for the build cache.

    A cached sortie must not survive a change to its own switches, the same
    trap the hidden list already has to avoid. The defaults are in here too:
    changing a default moves every undecided flight.
    """
    try:
        st = os.stat(PREFS_PATH)
        stamp = "%d:%d" % (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = "-"
    d = defaults()
    return "prefs:%s/%d%d" % (stamp, d["rating"], d["passenger"])


def self_test():
    """Round-trip the store against a temporary file."""
    global PREFS_PATH, RATING_DEFAULT
    import tempfile
    keep_path, keep_default = PREFS_PATH, RATING_DEFAULT
    PREFS_PATH = os.path.join(tempfile.mkdtemp(), "flight_prefs.json")
    try:
        assert for_sortie("a") == {"rating": True, "passenger": True}

        # one switch moves, the other is left alone
        assert set_for_sortie("a", rating=False) == {"rating": False,
                                                     "passenger": True}
        assert for_sortie("a")["passenger"] is True
        assert for_sortie("b") == {"rating": True, "passenger": True}, \
            "one flight must not move another"

        # back to the defaults, and the row is dropped rather than kept
        set_for_sortie("a", rating=True)
        assert load()["sorties"] == {}, load()["sorties"]

        # a changed default moves undecided flights and leaves decided ones
        set_for_sortie("c", rating=False)
        RATING_DEFAULT = False
        assert for_sortie("d")["rating"] is False
        set_for_sortie("e", rating=True)
        assert for_sortie("e")["rating"] is True
        RATING_DEFAULT = keep_default

        sig = signature()
        set_for_sortie("f", passenger=False)
        assert signature() != sig, "a change must bust the build cache"

        for bad in ("rating", "passenger"):
            try:
                set_for_sortie("g", **{bad: "yes"})
            except ValueError:
                pass
            else:
                raise AssertionError("%s accepted a non-boolean" % bad)
        return True
    finally:
        PREFS_PATH, RATING_DEFAULT = keep_path, keep_default


if __name__ == "__main__":
    print("offline self-test:", "PASS" if self_test() else "FAIL")

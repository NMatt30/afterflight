"""What the release zip carries, and what it must never carry.

build_release.py puts the app and a Python in one zip for people with no
Python installed. These check its decisions offline - no download, no build:

  - nothing of a user's is ever packed: an update is a release unpacked over
    an install, so one that carried sessions/ or settings.json would
    overwrite somebody's flights
  - nothing the app imports is left out with the developer files
  - the bundled Python finds the app, and the launcher starts the tray
  - a release with no .git still says which version it is

The build itself - the downloads, their checksums, the bundled Python
running every test - is run by the release workflow and by hand.

    py -3 test_release.py
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import build_release                                        # noqa: E402
import watcher                                              # noqa: E402
watcher.log = lambda *a, **k: None      # never append to the operational log


def _tracked():
    try:
        return build_release.tracked_files()
    except (OSError, subprocess.CalledProcessError):
        return None


def test_no_user_data_is_ever_packed():
    for path in ("sessions/flt-19990101T000000Z.jsonl", "sessions/clips/x.jsonl",
                 "settings.json", "flight_prefs.json", "events.jsonl",
                 "logbook.json", "logbook.cache.json", "current.json",
                 "watcher.log", "watcher.lock", ".maintenance.lock",
                 "native/SimConnect_internal.dll", "CALIBRATION.md"):
        try:
            build_release.check_no_user_data(["watcher.py", path])
        except SystemExit:
            continue
        raise AssertionError("%s would have been packed into a release" % path)
    build_release.check_no_user_data(["watcher.py", "logbook.html", "efb-pkg/x.json"])


def test_the_tracked_tree_carries_no_user_data():
    tracked = _tracked()
    if tracked is None:
        return                                  # not a git checkout
    build_release.check_no_user_data(build_release.app_files(tracked))


def test_developer_files_stay_out_and_the_app_goes_in():
    tracked = _tracked()
    if tracked is None:
        return
    files = set(build_release.app_files(tracked))
    for out in ("test_safety.py", "test_release.py", "build_release.py",
                "AGENTS.md", "snapshot.py", ".github/workflows/offline.yml"):
        assert out not in files, "%s is for developers and was packed" % out
    for need in ("watcher.py", "tray.py", "logbook.html", "logbook.js",
                 "passenger_lines.json", "install.ps1", "LICENSE", "README.md"):
        assert need in files, "%s was left out of the release" % need


def test_everything_the_app_imports_is_shipped():
    """Excluding developer files must never exclude a module the app needs."""
    tracked = _tracked()
    if tracked is None:
        return
    shipped = set(build_release.app_files(tracked))
    local = {p[:-3] for p in tracked if p.endswith(".py") and "/" not in p}
    missing = set()
    for path in shipped:
        if not path.endswith(".py") or "/" in path:
            continue
        with open(os.path.join(BASE, path), encoding="utf-8-sig") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in local and name + ".py" not in shipped:
                    missing.add("%s imports %s" % (path, name))
    assert not missing, "left out of the release: " + "; ".join(sorted(missing))


def test_the_bundled_python_can_find_the_app():
    """The embeddable Python ignores the script's folder and PYTHONPATH; only
    its ._pth counts, so the app folder - one up - must be in it."""
    text = build_release.pth_text(14)
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert lines[0] == "python314.zip", lines
    assert ".." in lines, "the app folder is not on the bundled Python's path"
    assert "Lib\\site-packages" in lines, "Pillow would not be found"
    assert "import site" not in lines, "site enabled: it would read the user's own Python setup"


def test_the_launcher_starts_the_tray_with_the_bundled_python():
    text = build_release.LAUNCHER
    assert "runtime\\pythonw.exe" in text and "tray.py" in text, text
    assert "%~dp0" in text, "the launcher depends on the folder it is run from"


def test_a_release_without_git_says_its_version():
    d = tempfile.mkdtemp()
    keep = (watcher.BASE, watcher._version_cache[0])
    try:
        with open(os.path.join(d, "VERSION"), "w", encoding="utf-8") as f:
            f.write("v9.9.9-1-gabcdef0\n")
        watcher.BASE = d
        watcher._version_cache[0] = None
        text = watcher.describe_version()
        assert "v9.9.9-1-gabcdef0" in text, (
            "a release with no .git reported %r, not the version it was built as" % text)
    finally:
        watcher.BASE, watcher._version_cache[0] = keep
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
    print("  %d release test(s), %d failed" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

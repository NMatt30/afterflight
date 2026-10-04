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


class Skipped(Exception):
    """A test that could not run, which is not a test that passed.

    The same convention as test_safety.py. _tracked() used to turn a git
    failure into None, the three tests that need the tracked tree returned
    early, and the runner printed "ok" for each - so a ZIP checkout, a
    missing git or an ownership refusal reported the release's privacy
    check as passed having inspected nothing.
    """


def _tracked():
    """What git tracks - the release's inventory. Raises Skipped, saying
    why, when git cannot tell us."""
    try:
        names = build_release.tracked_files()
    except OSError as e:
        raise Skipped("git is not available here (%s), so no tracked file "
                      "was inspected" % e.__class__.__name__)
    except subprocess.CalledProcessError as e:
        why = (e.stderr or b"").decode("utf-8", "replace").strip()
        raise Skipped("git would not list this repository, so no tracked "
                      "file was inspected: %s"
                      % (why.splitlines()[0] if why else "exit %d" % e.returncode))
    if not names:
        raise Skipped("git lists no tracked files here; nothing was inspected")
    return names


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
    build_release.check_no_user_data(build_release.app_files(_tracked()))


def test_developer_files_stay_out_and_the_app_goes_in():
    files = set(build_release.app_files(_tracked()))
    for out in ("test_safety.py", "test_release.py", "build_release.py",
                "AGENTS.md", "snapshot.py", ".github/workflows/offline.yml"):
        assert out not in files, "%s is for developers and was packed" % out
    for need in ("watcher.py", "tray.py", "logbook.html", "logbook.js",
                 "passenger_lines.json", "install.ps1", "LICENSE", "README.md"):
        assert need in files, "%s was left out of the release" % need


def test_everything_the_app_imports_is_shipped():
    """Excluding developer files must never exclude a module the app needs."""
    tracked = _tracked()
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


def _samples(patterns):
    """A concrete path for each inventory pattern: '*' becomes a name."""
    return [p.replace("*", "sample") for p in patterns]


def test_every_user_file_is_ignored_and_refused():
    """The inventory is the one list. Everything in it must be ignored by git,
    so an add-all cannot track it, and refused by the release guard, so a
    release cannot carry it even if something was tracked by force. The
    user's places.json was in neither, and a rotated log passed the guard."""
    _tracked()                    # the same git availability the others need
    samples = []
    for group in build_release.DATA.values():
        samples.extend(_samples(group))
    # NUL-separated bytes: in text mode Windows turns each newline into CRLF
    # on the way in, git is asked about "places.json\r", and nothing matches.
    out = subprocess.run(["git", "check-ignore", "--no-index", "--stdin", "-z"],
                         cwd=BASE, input="\0".join(samples).encode("utf-8"),
                         capture_output=True)
    ignored = set(n for n in out.stdout.decode("utf-8").split("\0") if n)
    not_ignored = [s for s in samples if s not in ignored]
    assert not not_ignored, (
        "user data git would track: %s - add it to .gitignore" % ", ".join(not_ignored))
    shipped = []
    for s in samples:
        try:
            build_release.check_no_user_data([s])
        except SystemExit:
            continue
        shipped.append(s)
    assert not shipped, "the release guard would pack: %s" % ", ".join(shipped)


def _backup_patterns():
    """The default and -Full lists backup.ps1 copies, as inventory paths."""
    import re
    with open(os.path.join(BASE, "backup.ps1"), encoding="utf-8-sig") as f:
        src = f.read()
    m_default = re.search(r"\$patterns = @\((.*?)\)", src, re.S)
    m_full = re.search(r"\$patterns \+= @\((.*?)\)", src, re.S)
    assert m_default and m_full, "backup.ps1 no longer has the two pattern lists"

    def norm(block):
        items = re.findall(r"'([^']+)'", block)
        out = set()
        for item in items:
            item = item.replace(chr(92), "/")
            # a bare directory copies everything under it
            if "." not in item.rsplit("/", 1)[-1]:
                item += "/*"
            out.add(item)
        return out
    return norm(m_default.group(1)), norm(m_full.group(1))


def test_the_backup_carries_what_the_inventory_says():
    """backup.ps1 cannot import the inventory, so it is checked against it.
    The runway cache and places.json were in no backup at all, and a restore
    then regraded every landing that had been held down by its runway."""
    default, full = _backup_patterns()
    want_default = set(build_release.DATA["backed_up"])
    want_full = set(build_release.DATA["full_only"])
    assert default == want_default, (
        "the default backup and the inventory disagree - missing from the "
        "backup: %s; not in the inventory: %s"
        % (sorted(want_default - default), sorted(default - want_default)))
    assert full == want_full, (
        "-Full and the inventory disagree - missing: %s; extra: %s"
        % (sorted(want_full - full), sorted(full - want_full)))
    never = set(build_release.DATA["never"])
    assert not (default | full) & never, "a backup copies runtime state"


def test_the_apps_own_json_still_ships():
    """Refusing user data must not refuse the app's own data files."""
    files = set(build_release.app_files(_tracked()))
    for need in ("passenger_lines.json", "efb-pkg/layout.json",
                 "efb-pkg/manifest.json"):
        assert need in files, "%s was left out of the release" % need
    build_release.check_no_user_data(sorted(files))


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


def test_a_git_failure_is_never_printed_as_a_pass():
    """Drive the runner with git refusing, the way an ownership check or a
    ZIP checkout does: the inventory tests must report SKIP with the reason,
    never ok, and the run must fail unless the gap is accepted on purpose."""
    real = build_release.tracked_files

    def refusing():
        raise subprocess.CalledProcessError(
            128, ["git", "ls-files"],
            stderr=b"fatal: detected dubious ownership in repository")

    needs_git = [(n, globals()[n]) for n in (
        "test_the_tracked_tree_carries_no_user_data",
        "test_developer_files_stay_out_and_the_app_goes_in",
        "test_everything_the_app_imports_is_shipped")]
    build_release.tracked_files = refusing
    try:
        code, lines = _run(needs_git)
        code_allowed, _ = _run(needs_git, allow_skips=True)
    finally:
        build_release.tracked_files = real
    said = "\n".join(lines)
    assert not [l for l in lines if l.strip().startswith("ok")], (
        "a test that inspected nothing was printed as ok:\n" + said)
    assert len([l for l in lines if l.strip().startswith("SKIP")]) == 3, said
    assert "dubious ownership" in said, "the skip does not say why:\n" + said
    assert code != 0, "a run that checked nothing exited 0"
    assert code_allowed == 0, "--allow-skips should accept the stated gap"


def _run(tests, allow_skips=False):
    """Run tests; return (exit code, printed lines)."""
    lines = []
    failed = skipped = 0
    for name, fn in tests:
        try:
            fn()
        except Skipped as e:
            skipped += 1
            lines.append("  SKIP  %s" % name)
            lines.append("        %s" % e)
        except AssertionError as e:
            failed += 1
            lines.append("  FAIL  %s" % name)
            lines.extend("        %s" % line for line in str(e).splitlines())
        except Exception as e:
            failed += 1
            lines.append("  ERROR %s: %r" % (name, e))
        else:
            lines.append("  ok    %s" % name)
    lines.append("")
    lines.append("  %d release test(s), %d failed, %d skipped"
                 % (len(tests), failed, skipped))
    if skipped and not allow_skips:
        lines.append("")
        lines.append("  A SKIP is not a pass: something above could not be checked")
        lines.append("  at all. Re-run where git can list the repository, or pass")
        lines.append("  --allow-skips to say you know what is not being checked.")
        return 1, lines
    return (1 if failed else 0), lines


def main(allow_skips=False):
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    code, lines = _run(tests, allow_skips)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main(allow_skips="--allow-skips" in sys.argv))

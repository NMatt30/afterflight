"""Build the AfterFlight release zip: the app, with Python inside it.

    py -3 build_release.py               dist/AfterFlight-<version>.zip
    py -3 build_release.py --no-smoke    skip running the built Python

So that someone with no Python installed can run AfterFlight. In the zip,
under one AfterFlight/ folder:

  runtime/      python.org's embeddable Python and Pillow, both pinned and
                checked against their published SHA-256
  the app       every file git tracks, less what is only for developers:
                the tests, the CI workflow, the agent docs, this script, and
                snapshot.py (a probe built on the Python-SimConnect package,
                which is not bundled)
  VERSION       what `git describe` said, since a release has no .git
  Start AfterFlight.cmd

What never goes in is anything of a user's. The app keeps its data beside
its code - sessions/, settings.json and the rest - and an update is a release
extracted over an existing install, so a release carrying any of it would
overwrite somebody's flights. Files come from `git ls-files`, which cannot
list them (they are ignored), and check_no_user_data() refuses the build if
one appears anyway.

The source stays plain readable .py: what is in the zip is what runs.
"""
import fnmatch
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(BASE, "build")
CACHE = os.path.join(BUILD, "cache")
DIST = os.path.join(BASE, "dist")
ROOT = "AfterFlight"

# The newest Python with an embeddable build when this was written; python.org
# publishes those only while a version takes bug fixes. The SHA-256 is the one
# in python.org's own bill of materials for the file (the .spdx.json beside it).
PYTHON_VERSION = "3.14.8"
PYTHON_URL = ("https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip"
              % (PYTHON_VERSION, PYTHON_VERSION))
PYTHON_SHA256 = "a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310"
# Maps. Optional in the app, bundled here. SHA-256 as published on PyPI.
PILLOW_VERSION = "12.3.0"
PILLOW_URL = ("https://files.pythonhosted.org/packages/f1/e0/"
              "492879f69d94f91f60fc8cd05ba03650e9520afebb2fb7aa12777d7c7f38/"
              "pillow-12.3.0-cp314-cp314-win_amd64.whl")
PILLOW_SHA256 = "fdafc9cce40277e0f7a0feabce0ee50dd2fa1800f3b38015e51296b5e814048d"

# Tracked, but only for developers.
DEV_ONLY = ("test_*.py", ".github/*", "AGENTS.md", "CLAUDE.md", "ENGINEERING.md",
            "DESIGN-NOTES.md", "HANDOFF.md", "build_release.py", ".gitattributes",
            ".gitignore", "snapshot.py")

# A user's: never in a release. Mirrors the data entries in .gitignore.
USER_DATA = ("sessions/*", "native/*", "logbook.json", "logbook.cache.json",
             "current.json", "last_event.json", "events.jsonl", "excluded.json",
             "flight_prefs.json", "notify_state.json", "settings.json",
             "watcher.log", "watcher.lock", "watcher.pid", ".maintenance.lock",
             "CALIBRATION.md", "*.log", "*.tmp", "*.bak")

LAUNCHER = ('@echo off\r\n'
            'rem Starts the AfterFlight tray with the Python that came with it.\r\n'
            'start "" "%~dp0runtime\\pythonw.exe" "%~dp0tray.py"\r\n')


def _matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def app_files(tracked):
    """The tracked paths a release carries, in order."""
    return sorted(p for p in tracked if not _matches(p, DEV_ONLY))


def check_no_user_data(paths):
    """Refuse the build if anything of a user's is about to be shipped."""
    bad = sorted(p for p in paths if _matches(p.replace("\\", "/"), USER_DATA))
    if bad:
        raise SystemExit("refusing to build: user data in the release: %s"
                         % ", ".join(bad[:10]))


def pth_text(minor):
    """The embeddable Python's path file. It ignores PYTHONPATH and the
    script's own folder, so the app folder - one up - is listed outright."""
    return "\r\n".join([
        "python3%d.zip" % minor,
        ".",
        "Lib\\site-packages",
        "..",
        "# 'import site' stays off: nothing is pip-installed into this Python.",
        "",
    ])


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=BASE, capture_output=True,
                         check=True)
    return [p for p in out.stdout.decode("utf-8").split("\0") if p]


def version_label():
    try:
        out = subprocess.run(["git", "describe", "--tags", "--match", "v*",
                              "--always", "--dirty"], cwd=BASE,
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def fetch(url, sha256):
    """The file at url, from the cache when it is there, verified either way."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, url.rsplit("/", 1)[-1])
    if not os.path.isfile(path):
        print("  downloading %s" % url)
        with urllib.request.urlopen(url, timeout=120) as r, open(path + ".part", "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(path + ".part", path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != sha256:
        os.remove(path)
        raise SystemExit("checksum mismatch for %s: got %s, expected %s"
                         % (url, h.hexdigest(), sha256))
    return path


def stage(stage_dir, files, label):
    if os.path.isdir(stage_dir):
        shutil.rmtree(stage_dir)
    runtime = os.path.join(stage_dir, "runtime")
    os.makedirs(runtime)

    with zipfile.ZipFile(fetch(PYTHON_URL, PYTHON_SHA256)) as z:
        z.extractall(runtime)
    minor = int(PYTHON_VERSION.split(".")[1])
    pth = os.path.join(runtime, "python3%d._pth" % minor)
    if not os.path.isfile(pth):
        raise SystemExit("the embeddable package has no %s" % os.path.basename(pth))
    with open(pth, "w", encoding="utf-8", newline="") as f:
        f.write(pth_text(minor))

    site = os.path.join(runtime, "Lib", "site-packages")
    os.makedirs(site)
    with zipfile.ZipFile(fetch(PILLOW_URL, PILLOW_SHA256)) as z:
        z.extractall(site)

    for rel in files:
        src = os.path.join(BASE, rel.replace("/", os.sep))
        dst = os.path.join(stage_dir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    with open(os.path.join(stage_dir, "VERSION"), "w", encoding="utf-8") as f:
        f.write(label + "\n")
    with open(os.path.join(stage_dir, "Start AfterFlight.cmd"), "w",
              encoding="utf-8", newline="") as f:
        f.write(LAUNCHER)


def smoke(stage_dir):
    """Run the built Python: it must find the app and every module must load."""
    exe = os.path.join(stage_dir, "runtime", "python.exe")
    code = ("import sys, PIL, watcher, tray, logbook_build, grading, passenger, "
            "settings, sampler, runways, mapbake, tiles; "
            "assert mapbake.HAVE_PIL, 'Pillow did not load'; "
            "print(sys.version.split()[0], 'PIL', PIL.__version__, "
            "'SimConnect package:', 'SimConnect' in sys.modules)")
    out = subprocess.run([exe, "-c", code], cwd=stage_dir, capture_output=True,
                         text=True, timeout=120)
    if out.returncode != 0:
        raise SystemExit("smoke test failed:\n%s%s" % (out.stdout, out.stderr))
    print("  smoke: %s" % out.stdout.strip())


def pack(stage_dir, label):
    os.makedirs(DIST, exist_ok=True)
    zpath = os.path.join(DIST, "%s-%s.zip" % (ROOT, label))
    shipped = []
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for folder, _dirs, names in os.walk(stage_dir):
            for name in sorted(names):
                full = os.path.join(folder, name)
                rel = os.path.relpath(full, stage_dir).replace(os.sep, "/")
                if "__pycache__" in rel.split("/"):
                    continue
                shipped.append(rel)
                z.write(full, "%s/%s" % (ROOT, rel))
    check_no_user_data([p for p in shipped if not p.startswith("runtime/")])
    h = hashlib.sha256()
    with open(zpath, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    with open(zpath + ".sha256", "w", encoding="utf-8") as f:
        f.write("%s  %s\n" % (h.hexdigest(), os.path.basename(zpath)))
    return zpath, h.hexdigest(), len(shipped)


def main(argv):
    label = version_label()
    files = app_files(tracked_files())
    check_no_user_data(files)
    stage_dir = os.path.join(BUILD, ROOT)
    print("AfterFlight %s: %d app files, Python %s, Pillow %s"
          % (label, len(files), PYTHON_VERSION, PILLOW_VERSION))
    stage(stage_dir, files, label)
    if "--no-smoke" not in argv:
        smoke(stage_dir)
    zpath, digest, count = pack(stage_dir, label)
    print("  %s  %.1f MB, %d files" % (os.path.relpath(zpath, BASE),
                                       os.path.getsize(zpath) / 1e6, count))
    print("  sha256 %s" % digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

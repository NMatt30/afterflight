"""Deploy efb-pkg into the sim, and keep layout.json honest.

The EFB app lives in two places: efb-pkg/ in this repo, which is the source of
truth, and a copy under the sim's Community2024 folder, which is what MSFS
actually loads. Editing one and forgetting the other is the obvious failure, and
it is silent - the sim keeps showing the old panel and nothing says why.

layout.json is the second trap. It records every file's byte size and timestamp
and the sim validates them, so a hand-edited CSS with a stale layout entry can
leave a package MSFS refuses to load. It is generated here, never hand-written.

    py -3 deploy_efb.py --check     compare without writing anything
    py -3 deploy_efb.py             deploy, then verify

Restart MSFS afterwards: it reads these files at startup, and reloading the EFB
app alone does not always pick up a change.
"""
import argparse
import filecmp
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "efb-pkg")
PACKAGE_NAME = "afterflight-efb"

# If this package is ever renamed, put the previous folder name here. MSFS
# loads every folder under Community2024, so a copy left behind under the old
# name registers a second, stale EFB app alongside this one - and the sim
# gives no clue which of the two you are looking at. Anything listed is
# reported by --check and by a normal deploy, and only ever deleted when the
# user asks with --remove-legacy.
LEGACY_PACKAGE_NAMES = ()

# Where MSFS 2024 keeps its packages. Read from the sim's own config rather than
# hardcoded, because it moves with a Store reinstall.
USERCFG = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Packages", "Microsoft.Limitless_8wekyb3d8bbwe", "LocalCache", "UserCfg.opt")

SKIP_SUFFIXES = (".bak", ".map")


def installed_packages_path():
    """InstalledPackagesPath out of UserCfg.opt, or None."""
    try:
        with open(USERCFG, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip().startswith("InstalledPackagesPath"):
                    return line.split(None, 1)[1].strip().strip('"')
    except OSError:
        return None
    return None


def deployed_root():
    root = installed_packages_path()
    if not root:
        return None
    for community in ("Community2024", "Community"):
        cand = os.path.join(root, community, PACKAGE_NAME)
        if os.path.isdir(cand):
            return cand
    # Not deployed yet - offer the 2024 folder if it exists.
    cand = os.path.join(root, "Community2024")
    return os.path.join(cand, PACKAGE_NAME) if os.path.isdir(cand) else None


def legacy_roots():
    """Deployed folders from a previous package name, if any survive."""
    root = installed_packages_path()
    if not root:
        return []
    out = []
    for community in ("Community2024", "Community"):
        for name in LEGACY_PACKAGE_NAMES:
            cand = os.path.join(root, community, name)
            if os.path.isdir(cand):
                out.append(cand)
    return out


def report_legacy(stale, removing):
    """Warn about - or, if asked, delete - packages under the old name."""
    ok = True
    for path in stale:
        if not removing:
            print("")
            print("  LEGACY PACKAGE STILL DEPLOYED: %s" % path)
            print("  MSFS loads every folder in Community, so this registers a")
            print("  second, stale EFB app. Delete it, or re-run with")
            print("  --remove-legacy to have this script delete it.")
            ok = False
            continue
        try:
            shutil.rmtree(path)
            print("  removed legacy package: %s" % path)
        except OSError as e:
            print("  could not remove %s: %r" % (path, e))
            ok = False
    return ok


def payload_files(root):
    """Every file that belongs to the package, relative, forward-slashed."""
    out = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name == "layout.json" or name == "manifest.json":
                continue
            if any(s in name for s in SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append(rel)
    return sorted(out)


def filetime(path):
    """Windows FILETIME: 100ns ticks since 1601, which is what layout.json uses."""
    return int((os.path.getmtime(path) + 11644473600) * 10000000)


def write_layout(root):
    """Regenerate layout.json from what is actually on disk."""
    content = []
    for rel in payload_files(root):
        full = os.path.join(root, rel.replace("/", os.sep))
        content.append({
            "path": rel.lower(),
            "size": os.path.getsize(full),
            "date": filetime(full),
        })
    doc = {"content": content}
    path = os.path.join(root, "layout.json")
    with open(path, "w", encoding="utf-8", newline="") as f:
        json.dump(doc, f, indent=2)
    return len(content)


def verify_layout(root):
    """Every layout entry must match the file it points at."""
    path = os.path.join(root, "layout.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        return ["layout.json unreadable: %r" % (e,)]
    problems = []
    for ent in doc.get("content", []):
        full = os.path.join(root, ent.get("path", "").replace("/", os.sep))
        if not os.path.isfile(full):
            problems.append("missing on disk: %s" % ent.get("path"))
            continue
        real = os.path.getsize(full)
        if real != ent.get("size"):
            problems.append("size mismatch: %s layout=%s disk=%s"
                            % (ent.get("path"), ent.get("size"), real))
    for rel in payload_files(root):
        if not any(e.get("path") == rel.lower() for e in doc.get("content", [])):
            problems.append("not listed in layout.json: %s" % rel)
    return problems


def compare(src, dst):
    """Which payload files differ between the two trees."""
    differing, missing = [], []
    for rel in payload_files(src):
        a = os.path.join(src, rel.replace("/", os.sep))
        b = os.path.join(dst, rel.replace("/", os.sep))
        if not os.path.isfile(b):
            missing.append(rel)
        elif not filecmp.cmp(a, b, shallow=False):
            differing.append(rel)
    return differing, missing


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report differences without writing anything")
    ap.add_argument("--remove-legacy", action="store_true",
                    help="delete deployed packages left over from the old name")
    args = ap.parse_args()

    if not os.path.isdir(SRC):
        sys.exit("no efb-pkg/ at %s" % SRC)
    dst = deployed_root()
    if not dst:
        sys.exit("could not locate the sim's Community folder via %s" % USERCFG)

    print("source  : %s" % SRC)
    print("deployed: %s" % dst)

    if args.check:
        problems = verify_layout(SRC)
        if not os.path.isdir(dst):
            print("\nnot deployed yet")
            clean = report_legacy(legacy_roots(), args.remove_legacy)
            return 1 if (problems or not clean) else 0
        differing, missing = compare(SRC, dst)
        problems += verify_layout(dst)
        if differing or missing:
            print("\nout of sync:")
            for rel in differing:
                print("  differs : %s" % rel)
            for rel in missing:
                print("  missing : %s" % rel)
        else:
            print("\nin sync")
        if problems:
            print("\nlayout.json problems:")
            for p in problems:
                print("  %s" % p)
        clean = report_legacy(legacy_roots(), args.remove_legacy)
        return 1 if (differing or missing or problems or not clean) else 0

    # Source first: its own layout has to describe its own files.
    n = write_layout(SRC)
    print("\nlayout.json regenerated in efb-pkg (%d files)" % n)

    os.makedirs(dst, exist_ok=True)
    copied = 0
    for rel in payload_files(SRC):
        a = os.path.join(SRC, rel.replace("/", os.sep))
        b = os.path.join(dst, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(b), exist_ok=True)
        if not os.path.isfile(b) or not filecmp.cmp(a, b, shallow=False):
            shutil.copy2(a, b)
            copied += 1
            print("  copied %s" % rel)
    for name in ("manifest.json",):
        a, b = os.path.join(SRC, name), os.path.join(dst, name)
        if os.path.isfile(a) and (not os.path.isfile(b)
                                  or not filecmp.cmp(a, b, shallow=False)):
            shutil.copy2(a, b)
            print("  copied %s" % name)

    # Regenerate against the deployed copies, whose mtimes are their own.
    write_layout(dst)
    print("layout.json regenerated in the deployed package")
    print("%d file(s) copied" % copied)

    problems = verify_layout(SRC) + verify_layout(dst)
    differing, missing = compare(SRC, dst)
    if problems or differing or missing:
        print("\nPROBLEMS AFTER DEPLOY:")
        for p in problems:
            print("  %s" % p)
        for rel in differing + missing:
            print("  still out of sync: %s" % rel)
        return 1
    print("\nverified: trees match and both layout.json files agree with disk")
    if not report_legacy(legacy_roots(), args.remove_legacy):
        return 1

    print("restart MSFS to pick this up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

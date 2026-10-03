"""The EFB package's layout.json in the repo, and when a deploy may rewrite it.

layout.json records each file's size and a date, and deploy_efb.py regenerated
it on every run. The dates are file modification times, which git does not
keep - a checkout sets them to the moment of the checkout - so every deploy
restamped them and left the committed file modified in every working tree:
a -dirty version string, and a git pull that refuses to run the next time a
commit touches the file. Nothing reads those dates. The sim loads the
deployed copy, whose layout is regenerated against its own files, and the
script's own check verifies paths and sizes only.

A fake package in a temporary folder throughout; the real efb-pkg and the
sim's Community folder are never touched.

    py -3 test_efb.py
"""
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import deploy_efb                                          # noqa: E402


class Pkg(object):
    """A minimal package: two payload files, a manifest, and its layout."""

    def __init__(self):
        self.root = tempfile.mkdtemp()
        self.write("html_ui/efb_ui/efb_apps/Test/Test.js", "var a = 1;\n")
        self.write("html_ui/efb_ui/efb_apps/Test/Test.css", "body{}\n")
        self.write("manifest.json", "{}\n")
        deploy_efb.write_layout(self.root)
        self.layout = os.path.join(self.root, "layout.json")

    def write(self, rel, text):
        full = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def checkout_later(self):
        """What a git checkout does: same bytes, new modification times."""
        later = os.path.getmtime(self.layout) + 86400.0
        for rel in deploy_efb.payload_files(self.root):
            os.utime(os.path.join(self.root, rel.replace("/", os.sep)),
                     (later, later))

    def layout_bytes(self):
        with open(self.layout, "rb") as f:
            return f.read()

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def test_a_checkout_does_not_dirty_the_committed_layout():
    p = Pkg()
    try:
        before = p.layout_bytes()
        p.checkout_later()

        # The fixture has to reproduce the fault, or this proves nothing:
        # regenerating unconditionally, as every deploy used to, changes it.
        q = Pkg()
        try:
            q.checkout_later()
            original = q.layout_bytes()
            deploy_efb.write_layout(q.root)
            assert q.layout_bytes() != original, (
                "fixture does not reproduce the fault: an unconditional "
                "regenerate left the layout byte-identical")
        finally:
            q.close()

        changed = deploy_efb.refresh_source_layout(p.root)
        assert changed == [], "rewrote a layout whose files had not changed: %r" % changed
        assert p.layout_bytes() == before, (
            "the committed layout.json was rewritten after a checkout that "
            "changed nothing but modification times")
    finally:
        p.close()


def test_a_real_change_is_still_written():
    """The file must still follow its files, or MSFS refuses the package."""
    p = Pkg()
    try:
        p.write("html_ui/efb_ui/efb_apps/Test/Test.js", "var a = 1; var b = 2;\n")
        changed = deploy_efb.refresh_source_layout(p.root)
        assert changed and any("size mismatch" in c for c in changed), (
            "a file that changed size did not trigger a rewrite: %r" % changed)
        assert deploy_efb.verify_layout(p.root) == [], (
            "after the rewrite the layout still disagrees with its files")
    finally:
        p.close()


def test_a_new_file_is_still_listed():
    p = Pkg()
    try:
        p.write("html_ui/efb_ui/efb_apps/Test/Assets/icon.svg", "<svg/>\n")
        changed = deploy_efb.refresh_source_layout(p.root)
        assert any("not listed" in c for c in changed), (
            "a new payload file did not trigger a rewrite: %r" % changed)
        assert deploy_efb.verify_layout(p.root) == []
    finally:
        p.close()


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
    print("  %d efb test(s), %d failed" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

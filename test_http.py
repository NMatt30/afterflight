"""Who may command the watcher over HTTP (SME review R1).

The watcher binds to loopback, which says where a request comes from and not
who sent it: a web page in any browser tab can address 127.0.0.1. Every
response used to grant every origin everything, and nothing checked a
command's Origin, Host or content type, so a page could hide, delete, change
settings or start a replay.

These run the real request handler on a spare loopback port, with every
action a command could reach replaced by a recorder - nothing is hidden,
deleted, saved or started. A refused request must reach no action at all.

    py -3 test_http.py
"""
import http.client
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import watcher                                              # noqa: E402
watcher.log = lambda *a, **k: None      # never append to the operational log

# Every function a POST route calls. A refused request must reach none of them.
ACTIONS = ("start_replay", "apply_settings", "stop_replay", "set_replay_paused",
           "lock_camera", "recenter_camera", "update_chase", "purge_hidden",
           "set_hidden", "set_flight_prefs", "rebuild_logbook_now")
COMMANDS = ("/replay", "/settings", "/replay/stop", "/replay/pause",
            "/replay/lock", "/replay/recenter", "/replay/chase", "/logbook/purge",
            "/logbook/hide", "/logbook/restore", "/logbook/prefs",
            "/logbook/rebuild")


class Server(object):
    """The real StateHandler on a spare port, the actions recorded."""

    def __init__(self):
        self.calls = []
        self._keep = {n: getattr(watcher, n) for n in ACTIONS}
        self._keep_port = watcher.HTTP_PORT
        for name in ACTIONS:
            setattr(watcher, name, self._recorder(name))
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), watcher.StateHandler)
        self.port = self.srv.server_address[1]
        watcher.HTTP_PORT = self.port       # the Host check follows the port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def _recorder(self, name):
        def record(*a, **k):
            self.calls.append(name)
            if name == "apply_settings":
                return True, {}
            return {"ok": True, "recorded": name}
        return record

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        for name, fn in self._keep.items():
            setattr(watcher, name, fn)
        watcher.HTTP_PORT = self._keep_port

    def request(self, method, path, headers=None, body=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            # skip_host so a test can send a wrong Host, or none at all
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host is not False:
                conn.putheader("Host", host or "127.0.0.1:%d" % self.port)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            data = body.encode("utf-8") if body is not None else b""
            if method == "POST":
                conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data if method == "POST" else None)
            r = conn.getresponse()
            return r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read()
        finally:
            conn.close()


JSON = {"Content-Type": "application/json"}


def _refused_everywhere(s, why, headers=None, body="{}", host=None):
    """Send every command this way; each must be refused and reach nothing."""
    for path in COMMANDS:
        before = len(s.calls)
        status, hdrs, raw = s.request("POST", path, headers, body, host)
        assert status == 403, "%s %s: got %d, not refused" % (why, path, status)
        assert len(s.calls) == before, (
            "%s %s reached %s before it was refused" % (why, path, s.calls[before:]))
        assert "access-control-allow-origin" not in hdrs, (
            "%s %s: a refusal still grants an origin" % (why, path))


def test_a_cross_site_simple_request_is_refused():
    """text/plain needs no preflight, so a page can send it to anything it can
    address. It reached the action and answered 200 with
    Access-Control-Allow-Origin: * before this."""
    s = Server()
    try:
        _refused_everywhere(s, "text/plain from another site",
                            {"Content-Type": "text/plain",
                             "Origin": "https://example.invalid"},
                            body='{"scope": "sortie", "sortie_id": "flt-19990101T000000Z"}')
    finally:
        s.close()


def test_a_command_from_another_origin_is_refused_even_as_json():
    s = Server()
    try:
        _refused_everywhere(s, "JSON from another origin",
                            dict(JSON, Origin="https://example.invalid"))
        _refused_everywhere(s, "JSON from another local port",
                            dict(JSON, Origin="http://127.0.0.1:9999"))
    finally:
        s.close()


def test_a_null_origin_is_not_the_absent_one():
    """Origin: null is what a sandboxed document sends. It is not a client
    without an origin, and must not be treated as one."""
    s = Server()
    try:
        _refused_everywhere(s, "Origin: null", dict(JSON, Origin="null"))
    finally:
        s.close()


def test_an_unexpected_host_is_refused_for_reads_too():
    """The DNS-rebinding guard: a name that resolves to 127.0.0.1 is still not
    this server's name, and reads are guarded as well as commands."""
    s = Server()
    try:
        _refused_everywhere(s, "a rebinding Host", JSON, host="attacker.example:%d" % s.port)
        _refused_everywhere(s, "no Host", JSON, host=False)
        for path in ("/state", "/logbook.json", "/settings"):
            status, _, _ = s.request("GET", path, host="attacker.example:%d" % s.port)
            assert status == 403, "GET %s with a foreign Host: %d" % (path, status)
    finally:
        s.close()


def test_a_command_that_is_not_json_is_refused():
    """Bodyless commands - Stop, Rebuild - are checked too: they never reach
    _read_json_body, which is where a check placed per route would sit."""
    s = Server()
    try:
        _refused_everywhere(s, "no Content-Type", {}, body="")
        _refused_everywhere(s, "a form post", {"Content-Type":
                            "application/x-www-form-urlencoded"}, body="a=1")
    finally:
        s.close()


def test_a_preflight_grants_nothing():
    """A browser asks before a cross-site JSON POST. The answer used to be
    every origin, every method, every header."""
    s = Server()
    try:
        status, hdrs, _ = s.request(
            "OPTIONS", "/logbook/hide",
            {"Origin": "https://example.invalid",
             "Access-Control-Request-Method": "POST",
             "Access-Control-Request-Headers": "content-type"})
        granted = [h for h in hdrs if h.startswith("access-control-allow")]
        assert not granted, "the preflight granted %s" % granted
        assert not s.calls
    finally:
        s.close()


def test_the_tray_and_the_page_are_still_let_in():
    """The tray posts JSON with no Origin (tray.http_post); the logbook page
    posts JSON from its own origin, 127.0.0.1 or localhost."""
    import tray
    s = Server()
    try:
        url = "http://127.0.0.1:%d/logbook/rebuild" % s.port
        got = tray.http_post(url, timeout=5)
        assert got and got.get("recorded") == "rebuild_logbook_now", (
            "the tray's own request was refused: %r" % (got,))
        for host in ("127.0.0.1:%d" % s.port, "localhost:%d" % s.port):
            for path in COMMANDS:
                before = len(s.calls)
                status, _, raw = s.request("POST", path,
                                           dict(JSON, Origin="http://" + host),
                                           "{}", host=host)
                assert status == 200, "the page at %s was refused %s: %s" % (host, path, raw)
                assert len(s.calls) == before + 1, "%s reached nothing" % path
    finally:
        s.close()


def test_state_stays_readable_for_the_tablet_and_nothing_else_does():
    """The EFB polls /state from inside the sim, under an origin not known
    here, so /state keeps Access-Control-Allow-Origin: * - an interim choice,
    and it does carry the current flight. Nothing else is shared with another
    origin. Every response keeps Cache-Control: no-store."""
    s = Server()
    try:
        status, hdrs, _ = s.request("GET", "/state", {"Origin": "coui://html_ui"})
        assert status == 200
        assert hdrs.get("access-control-allow-origin") == "*", (
            "the EFB tablet could no longer read /state")
        assert hdrs.get("cache-control") == "no-store"
        for path in ("/logbook.json", "/current", "/last_event", "/clips",
                     "/grading", "/settings"):
            status, hdrs, _ = s.request("GET", path, {"Origin": "https://example.invalid"})
            assert "access-control-allow-origin" not in hdrs, (
                "GET %s is shared with every origin" % path)
            assert hdrs.get("cache-control") == "no-store", path
    finally:
        s.close()


def test_the_page_sends_every_command_as_same_origin_json():
    """The browser half, read from the source: replay.js used an absolute
    127.0.0.1 address - a cross-origin request when the page was opened as
    localhost - and Stop and Rebuild posted with no Content-Type."""
    import re
    for name in ("logbook.js", "replay.js"):
        with open(os.path.join(BASE, name), encoding="utf-8") as f:
            src = f.read()
        assert not re.search(r"""LOGGER\s*=\s*["']http""", src), (
            "%s addresses the watcher absolutely" % name)
        for m in re.finditer(r"fetch\([^;]*?method:\s*\"POST\"[^;]*?\)", src, re.S):
            assert "application/json" in m.group(0), (
                "%s posts without JSON: %s" % (name, m.group(0)[:80]))


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
    print("  %d http test(s), %d failed" % (len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

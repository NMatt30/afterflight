"""AfterFlight tray application.

One tray process. It supervises the watcher (Below Normal), shows what the
watcher is doing, and opens the logbook.

Deliberately not an embedded browser. There is no frame budget for Chromium
compositing while the sim is flying, and a browser window left open behind the
sim is a real cost. "Open logbook" hands http://127.0.0.1:8742/ to the default
browser instead, which is the same HTML the tray would have embedded - and one
the user can close.

Pure ctypes against user32/shell32 so there is nothing to pip install.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import datetime
import traceback

import trayicon
from ctypes import POINTER, Structure, WINFUNCTYPE, byref, c_int, c_uint, c_void_p, sizeof
from ctypes.wintypes import (
    DWORD, HICON, HINSTANCE, HMENU, HWND, LPARAM, LPCWSTR, MSG, POINT, UINT, WPARAM, WCHAR,
)

BASE = os.path.dirname(os.path.abspath(__file__))
WATCHER = os.path.join(BASE, "watcher.py")
LOGBOOK_URL = "http://127.0.0.1:8742/"
STATE_URL = "http://127.0.0.1:8742/state"
REBUILD_URL = "http://127.0.0.1:8742/logbook/rebuild"
STOP_REPLAY_URL = "http://127.0.0.1:8742/replay/stop"

APP_NAME = "AfterFlight"

# The tray already polled every 3 s and knew when the watcher was down; it just
# did nothing about it, so a watcher that stopped at 22:55 was still stopped the
# next morning. Now it brings it back.
WATCHER_AUTO_RESTART = True
# A watcher whose detect loop has not turned in this long is wedged, not busy.
# It can still be answering on the port - the HTTP server is its own thread -
# which is exactly why liveness is measured on the loop instead.
WATCHER_STALL_SEC = 120.0
# Wait longer after each failure, so a watcher that cannot start is not
# relaunched every three seconds for ever.
WATCHER_RESTART_BACKOFF = (10, 30, 120, 600)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "AfterFlight"

# If this app is ever renamed, put the previous autostart value here. Leaving
# one behind would launch a second tray at logon, and simply deleting it would
# silently turn off an autostart the user had asked for - so anything listed
# here is migrated, not dropped.
LEGACY_RUN_VALUES = ()

# Single-instance guard. Any previous mutex name belongs in the list below: a
# tray still running the older code holds only that one and cannot know about
# this one, so without the check a rename would put two icons in the tray.
MUTEX_NAME = "Global\\AfterFlightTray"
LEGACY_MUTEX_NAMES = ()

SYNCHRONIZE = 0x00100000
# AllowSetForegroundWindow(ASFW_ANY): hand our foreground rights to whatever
# we are about to launch. A background tray has no right to change which
# window is in front, so a browser it starts can come up behind everything.
# Hardening, not a diagnosis: it does not explain a click that starts no
# browser at all, which has been reported and is not yet understood.
ASFW_ANY = 0xFFFFFFFF

# Held for the life of the process; releasing it would let a second tray in.
_mutex = None

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_NULL = 0x0000
WM_TRAYICON = WM_APP + 1

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
NIF_INFO = 0x10

IDI_APPLICATION = 32512   # fallback only; state icons come from trayicon

MF_STRING, MF_SEPARATOR, MF_GRAYED, MF_CHECKED = 0x0000, 0x0800, 0x0001, 0x0008
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

# Menu command ids
ID_OPEN = 1001
ID_REBUILD = 1002
ID_STOP_REPLAY = 1003
ID_START_WATCHER = 1004
ID_STOP_WATCHER = 1005
ID_FOLDER = 1006
ID_AUTOSTART = 1007
ID_EXIT = 1099


# On x64 the default restype is c_int, which truncates every returned handle.
# These have to be declared before any of them is called.
user32.CreateWindowExW.restype = HWND
user32.CreateWindowExW.argtypes = [DWORD, LPCWSTR, LPCWSTR, DWORD, c_int, c_int, c_int,
                                   c_int, HWND, HMENU, HINSTANCE, c_void_p]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
user32.LoadIconW.restype = HICON
user32.LoadIconW.argtypes = [HINSTANCE, c_void_p]
user32.CreatePopupMenu.restype = HMENU
user32.AppendMenuW.argtypes = [HMENU, UINT, ctypes.c_size_t, LPCWSTR]
# DestroyMenu had no argtypes, so ctypes converted the menu handle to c_int
# and threw OverflowError whenever Windows returned one above 2**31. That is
# intermittent by nature - the same code works all morning and then does not -
# and because the throw happened BEFORE the menu command was dispatched, the
# visible symptom was "Open logbook does nothing", not a crash.
user32.DestroyMenu.argtypes = [HMENU]
user32.DestroyMenu.restype = ctypes.c_int
user32.TrackPopupMenu.restype = c_int
user32.TrackPopupMenu.argtypes = [HMENU, UINT, c_int, c_int, c_int, HWND, c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [HWND, ctypes.c_size_t, UINT, c_void_p]
user32.RegisterClassW.restype = ctypes.c_ushort
user32.DestroyWindow.argtypes = [HWND]
user32.SetForegroundWindow.argtypes = [HWND]
user32.SetForegroundWindow.restype = ctypes.c_int
user32.PostMessageW.argtypes = [HWND, UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.GetForegroundWindow.restype = HWND
user32.AllowSetForegroundWindow.argtypes = [DWORD]
user32.AllowSetForegroundWindow.restype = ctypes.c_int
kernel32.GetModuleHandleW.restype = HINSTANCE
kernel32.GetModuleHandleW.argtypes = [LPCWSTR]
kernel32.CreateMutexW.restype = c_void_p
kernel32.OpenMutexW.restype = c_void_p
kernel32.OpenMutexW.argtypes = [DWORD, ctypes.c_int, LPCWSTR]
kernel32.CloseHandle.argtypes = [c_void_p]
shell32.Shell_NotifyIconW.restype = ctypes.c_int


TRAY_LOG = os.path.join(BASE, "tray.log")
_log_lock = threading.Lock()


def log(msg):
    """Under pythonw there is no console, so the file is the only record.

    Menu actions were previously invisible: a click that did nothing left
    nothing behind to say whether the handler ran, what it decided, or what
    the shell returned.
    """
    line = "%s %s" % (datetime.datetime.now().isoformat(timespec="seconds"), msg)
    try:
        with _log_lock:
            if os.path.isfile(TRAY_LOG) and os.path.getsize(TRAY_LOG) > 512 * 1024:
                os.replace(TRAY_LOG, TRAY_LOG + ".1")
            with open(TRAY_LOG, "a", encoding="utf-8") as f:
                f.write(line + chr(10))
    except Exception:
        pass


def load_stock_icon(idi):
    """LoadIconW with MAKEINTRESOURCE semantics: the id travels as a pointer."""
    return user32.LoadIconW(None, c_void_p(idi))


class NOTIFYICONDATAW(Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("hWnd", HWND),
        ("uID", UINT),
        ("uFlags", UINT),
        ("uCallbackMessage", UINT),
        ("hIcon", HICON),
        ("szTip", WCHAR * 128),
        ("dwState", DWORD),
        ("dwStateMask", DWORD),
        ("szInfo", WCHAR * 256),
        ("uVersion", UINT),
        ("szInfoTitle", WCHAR * 64),
        ("dwInfoFlags", DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", HICON),
    ]


class WNDCLASS(Structure):
    _fields_ = [
        ("style", UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", c_int),
        ("cbWndExtra", c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", LPCWSTR),
        ("lpszClassName", LPCWSTR),
    ]


# ---------------------------------------------------------------- helpers


def http_get(url, timeout=2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def http_post(url, timeout=30.0):
    try:
        req = urllib.request.Request(url, data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def autostart_command():
    """pythonw.exe so there is no console window at logon."""
    exe = sys.executable or "python.exe"
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(pyw):
        exe = pyw
    return '"%s" "%s"' % (exe, os.path.join(BASE, "tray.py"))


def migrate_autostart():
    """Carry an autostart entry from a previous product name across.

    Runs once at startup. If a legacy value is present, its intent is
    reproduced under the current name and the old value is removed, so the
    rename neither duplicates the tray at logon nor loses the setting.
    """
    import winreg
    moved = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as k:
            for name in LEGACY_RUN_VALUES:
                try:
                    winreg.QueryValueEx(k, name)
                except FileNotFoundError:
                    continue
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ,
                                  autostart_command())
                winreg.DeleteValue(k, name)
                moved.append(name)
    except OSError:
        pass
    return moved


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(val)
    except OSError:
        return False


def set_autostart(enabled):
    import winreg
    try:
        if enabled:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- tray app


class Tray(object):
    def __init__(self):
        self.hwnd = None
        self.nid = None
        self.proc = None            # watcher child, if we started it
        # Set only by Stop watcher on the menu, cleared only by Start watcher.
        # The supervisor cannot tell a deliberate stop from a crash - both look
        # like nothing answering on the port - so intent has to be recorded
        # when it is expressed. It deliberately does NOT live in stop_watcher():
        # consider_restart() calls that itself on the way to relaunching, and a
        # restart would then mark the watcher as user-stopped and never come
        # back.
        self.user_stopped = False
        self.state = "starting"
        self.tip = APP_NAME
        self.icon_kind = None
        self.icons = trayicon.IconSet()
        self._wndproc = WNDPROC(self._on_message)  # keep a reference alive

    # -- watcher ---------------------------------------------------------

    def watcher_alive(self):
        """The watcher answers on the port, whoever started it."""
        return http_get(STATE_URL, timeout=1.0) is not None

    def watcher_pid(self):
        """The pid the watcher reports for itself, or None.

        Taken from /state rather than the pid file: it is the process actually
        answering, which is the one that has to go.
        """
        d = http_get(STATE_URL, timeout=1.5)
        try:
            return int((d or {}).get("pid"))
        except (TypeError, ValueError):
            return None

    def kill_watcher_pid(self, pid):
        """Force a wedged watcher to stop.

        stop_watcher() only handles a process this tray started and asks it
        politely; a wedged one is not answering its message queue and may have
        been started by an earlier tray, by autostart, or by hand. Killing the
        pid /state just reported is still not killing a stranger - it is the
        watcher naming itself.
        """
        if not pid:
            return "no pid"
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
            return "killed %d" % pid
        except Exception as e:
            return "kill failed: %r" % (e,)

    def start_watcher(self, force=False):
        # force skips the already-running check: a wedged watcher answers on
        # the port, so without this a restart would decide there was nothing
        # to do and leave it wedged for ever, reporting success each time.
        if not force and self.watcher_alive():
            return "already running"
        exe = sys.executable or "python.exe"
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pyw):
            exe = pyw
        try:
            # Below Normal, no console: section 13's budget.
            self.proc = subprocess.Popen(
                [exe, WATCHER], cwd=BASE,
                creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS,
            )
            return "started"
        except Exception as e:
            return "failed: %r" % (e,)

    def stop_watcher(self):
        """Only stops a watcher this tray started; never kills a stranger."""
        if self.proc is None or self.proc.poll() is not None:
            self.proc = None
            return "not started by tray"
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None
        return "stopped"

    # -- state -----------------------------------------------------------

    def refresh_state(self):
        d = http_get(STATE_URL, timeout=1.5)
        if d is None:
            self.state = "down"
            if getattr(self, "user_stopped", False):
                self.tip = APP_NAME + "\nwatcher stopped - start it from this menu"
            else:
                self.tip = APP_NAME + "\nwatcher not running"
            self.consider_restart("not answering")
            self.update_icon()
            return
        # Answering is not the same as working. A wedged detect loop records
        # nothing while the HTTP thread keeps replying, which looks healthy
        # from outside and is how one sat dead overnight.
        age = d.get("heartbeat_age_s")
        if isinstance(age, (int, float)) and age > WATCHER_STALL_SEC:
            self.state = "down"
            self.tip = "%s\nwatcher wedged (%.0fs)" % (APP_NAME, age)
            self.consider_restart("wedged for %.0fs" % age)
            self.update_icon()
            return
        self._restart_fails = 0
        cur = d.get("current") or {}
        replay = d.get("replay") or {}
        if replay.get("active"):
            self.state = "replay"
            chase = (replay.get("chase") or {}).get("camera_acquired")
            self.tip = "%s\nreplaying %s%s" % (
                APP_NAME, replay.get("clip_id") or "clip",
                "\nchase locked on ghost" if chase else "")
        elif not d.get("connected"):
            self.state = "idle"
            self.tip = APP_NAME + "\nwatcher up, sim not connected"
        elif cur.get("state") == "in_flight":
            self.state = "recording"
            self.tip = "%s\nrecording %s\nleg %s" % (
                APP_NAME, cur.get("aircraft") or "", (d.get("clips") or {}).get("leg"))
        else:
            self.state = "connected"
            self.tip = APP_NAME + "\nconnected, idle"
        self.update_icon()

    def consider_restart(self, why):
        """Bring back a watcher that has died or stopped turning.

        Runs off the message loop: Popen and an HTTP probe are both far slower
        than a WNDPROC should be, and ctypes swallows exceptions raised inside
        one, so a failure here would otherwise vanish without trace.
        """
        if not WATCHER_AUTO_RESTART:
            return
        if getattr(self, "user_stopped", False):
            # Asked for. Restarting here is how Stop watcher used to appear to
            # do nothing at all: the watcher went away and the next 3 s tick
            # brought it straight back.
            return
        n = getattr(self, "_restart_fails", 0)
        wait = WATCHER_RESTART_BACKOFF[min(n, len(WATCHER_RESTART_BACKOFF) - 1)]
        if time.time() - getattr(self, "_restart_at", 0.0) < wait:
            return
        if getattr(self, "_restarting", False):
            return
        self._restart_at = time.time()
        self._restarting = True

        def run():
            try:
                log("watcher %s - restarting (attempt %d)" % (why, n + 1))
                # Whatever this tray started, stop properly. Then make sure
                # nothing is still holding the port: a wedged watcher answers
                # but never recovers, and start_watcher would see it as fine.
                self.stop_watcher()
                pid = self.watcher_pid()
                if pid and pid != os.getpid():
                    log("watcher still answering as pid %s - %s"
                        % (pid, self.kill_watcher_pid(pid)))
                    time.sleep(1.0)
                log("watcher restart: %s" % self.start_watcher(force=True))
                # Only a watcher that answers counts as recovered; otherwise
                # back off further rather than declaring success.
                for _ in range(10):
                    time.sleep(1.0)
                    if http_get(STATE_URL, timeout=1.0) is not None:
                        self._restart_fails = 0
                        self.notify(APP_NAME,
                                    "Watcher was %s. Restarted it." % why)
                        return
                self._restart_fails = n + 1
                log("watcher restart did not take (attempt %d)" % (n + 1))
            except Exception as e:
                self._restart_fails = n + 1
                log("watcher restart failed: %r" % (e,))
            finally:
                self._restarting = False

        threading.Thread(target=run, daemon=True, name="watcher-restart").start()

    def update_icon(self):
        if not self.nid:
            return
        # IconSet caches, so this is a dict lookup on all but the first visit
        # to a state - it must be, since refresh_state runs every 3 s.
        hicon = self.icons.get(self.state) or load_stock_icon(IDI_APPLICATION)
        self.nid.hIcon = hicon
        self.nid.szTip = self.tip[:127]
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        shell32.Shell_NotifyIconW(NIM_MODIFY, byref(self.nid))
        self.icon_kind = self.state

    # -- menu ------------------------------------------------------------

    def show_menu(self):
        menu = user32.CreatePopupMenu()
        running = self.watcher_alive()

        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "Open logbook")
        user32.AppendMenuW(menu, MF_STRING | (0 if running else MF_GRAYED),
                           ID_REBUILD, "Rebuild logbook + maps")
        user32.AppendMenuW(menu, MF_STRING | (0 if running else MF_GRAYED),
                           ID_STOP_REPLAY, "Stop replay")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING | (MF_GRAYED if running else 0),
                           ID_START_WATCHER, "Start watcher")
        # Enabled whenever a watcher is ANSWERING, not just when this tray
        # started one. A watcher launched at logon, or by a previous tray,
        # left self.proc None and greyed out the only way to stop it.
        user32.AppendMenuW(menu, MF_STRING | (0 if running else MF_GRAYED),
                           ID_STOP_WATCHER, "Stop watcher")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_FOLDER, "Open session folder")
        user32.AppendMenuW(
            menu, MF_STRING | (MF_CHECKED if autostart_enabled() else 0),
            ID_AUTOSTART, "Start with Windows")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "Exit")

        pt = POINT()
        user32.GetCursorPos(byref(pt))
        # KB135788. The menu belongs to the owner window, and a menu whose
        # owner is not the foreground window can be dismissed without
        # reporting the item that was clicked - TrackPopupMenu returns 0 and
        # the selection is simply lost. SetForegroundWindow can fail here,
        # so its result is recorded rather than assumed.
        fg_ok = user32.SetForegroundWindow(self.hwnd)
        cmd = 0
        try:
            cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                        pt.x, pt.y, 0, self.hwnd, None)
            log("menu: setforeground=%s trackpopup=%s" % (fg_ok, cmd))
            # The other half of KB135788: without a message posted to the
            # owner after the menu closes, the menu can misbehave on the next
            # open.
            user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            # Tidying up must never cost the user their click. This threw for
            # months of handles and swallowed every menu command with it,
            # because the dispatch below used to sit after it.
            try:
                user32.DestroyMenu(menu)
            except Exception as e:
                log("menu: DestroyMenu failed, handle leaked: %r" % (e,))
        if cmd:
            log("menu: dispatching cmd=%s" % cmd)
            self.on_command(cmd)

    def on_command(self, cmd):
        if cmd == ID_OPEN:
            if not self.watcher_alive():
                self.notify("Watcher is not running", "Start it first, then open the logbook.")
                return
            self.open_logbook()
        elif cmd == ID_REBUILD:
            threading.Thread(target=self._rebuild, daemon=True).start()
        elif cmd == ID_STOP_REPLAY:
            threading.Thread(target=lambda: http_post(STOP_REPLAY_URL, timeout=10),
                             daemon=True).start()
        elif cmd == ID_START_WATCHER:
            self.user_stopped = False
            self.notify(APP_NAME, "Watcher: " + self.start_watcher())
        elif cmd == ID_STOP_WATCHER:
            # Record the intent before stopping, so the 3 s timer cannot fire
            # in between and read the stop as a crash.
            self.user_stopped = True
            result = self.stop_watcher()
            pid = self.watcher_pid()
            if pid and pid != os.getpid():
                # Not one this tray started - an autostarted one, or a
                # leftover. Stop still has to mean stop.
                result = self.kill_watcher_pid(pid)
            self.notify(APP_NAME, "Watcher: " + result + " (stays stopped "
                        "until you start it)")
        elif cmd == ID_FOLDER:
            os.startfile(BASE)
        elif cmd == ID_AUTOSTART:
            want = not autostart_enabled()
            ok = set_autostart(want)
            self.notify(APP_NAME, ("Start with Windows: " + ("on" if want else "off"))
                        if ok else "Could not change the Run key.")
        elif cmd == ID_EXIT:
            user32.DestroyWindow(self.hwnd)

    def open_logbook(self):
        """Open the logbook, and leave a record of what happened.

        There is an open report of this doing nothing at all, with no
        browser running and none started. That is NOT explained by the
        foreground grant below, and is not yet understood - which is why
        the outcome is logged rather than assumed.

        What is known: webbrowser.open resolves to os.startfile here, and
        the old code discarded its return value, so a failure to launch
        anything was silent. It is logged and reported now, and tried a
        second way before giving up.
        """
        user32.AllowSetForegroundWindow(ASFW_ANY)
        why = None
        try:
            opened = bool(webbrowser.open(LOGBOOK_URL))
            if not opened:
                why = "webbrowser.open returned False"
        except Exception as e:
            opened, why = False, repr(e)
        # os.startfile can report success while the shell quietly does
        # nothing, so the fallback is tried on its own merits rather than
        # only after an error.
        if not opened:
            try:
                os.startfile(LOGBOOK_URL)
                opened = True
                why = (why or "") + " (recovered via os.startfile)"
            except Exception as e2:
                why = "%s; os.startfile: %r" % (why, e2)
        log("open logbook: opened=%s%s" % (opened, "" if why is None else " - " + why))
        if not opened:
            self.notify("Could not open the logbook",
                        "%s - browse to %s yourself." % (why, LOGBOOK_URL))
        return opened

    def _rebuild(self):
        r = http_post(REBUILD_URL, timeout=120)
        if r and r.get("ok"):
            t = r.get("totals") or {}
            self.notify("Logbook rebuilt",
                        "%s sorties, %s legs, %s landings"
                        % (t.get("sorties"), t.get("legs"), t.get("landings")))
        else:
            self.notify("Rebuild failed", (r or {}).get("error") or "watcher did not answer")

    def notify(self, title, text):
        if not self.nid:
            return
        self.nid.uFlags = NIF_INFO | NIF_MESSAGE | NIF_ICON | NIF_TIP
        self.nid.szInfoTitle = title[:63]
        self.nid.szInfo = text[:255]
        self.nid.dwInfoFlags = 0
        shell32.Shell_NotifyIconW(NIM_MODIFY, byref(self.nid))
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP

    # -- window ----------------------------------------------------------

    def _on_message(self, hwnd, msg, wparam, lparam):
        """Guard the callback boundary.

        ctypes discards exceptions raised inside a WNDPROC, and pythonw has
        no stderr for the traceback to reach, so an error in here was
        completely invisible: the tray kept running and the action simply
        did not happen, leaving no record of why.
        """
        try:
            return self._dispatch(hwnd, msg, wparam, lparam)
        except Exception:
            log("WNDPROC error on msg=%s" % msg)
            for line in traceback.format_exc().splitlines():
                log("  " + line)
            return 0

    def _dispatch(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            if lparam == WM_RBUTTONUP:
                log("tray: right-click")
                self.show_menu()
            elif lparam == WM_LBUTTONDBLCLK:
                log("tray: double-click -> open logbook")
                self.on_command(ID_OPEN)
            return 0
        if msg == WM_COMMAND:
            self.on_command(wparam & 0xFFFF)
            return 0
        if msg == WM_TIMER:
            self.refresh_state()
            return 0
        if msg == WM_DESTROY:
            shell32.Shell_NotifyIconW(NIM_DELETE, byref(self.nid))
            self.icons.close()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self, start_watcher=True):
        # Before anything else, so a rename never leaves two logon entries.
        migrate_autostart()
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASS()
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "AfterFlightTray"
        atom = user32.RegisterClassW(byref(cls))
        if not atom:
            raise ctypes.WinError(ctypes.get_last_error())

        self.hwnd = user32.CreateWindowExW(
            0, cls.lpszClassName, APP_NAME, 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        self.nid = NOTIFYICONDATAW()
        self.nid.cbSize = sizeof(NOTIFYICONDATAW)
        self.nid.hWnd = self.hwnd
        self.nid.uID = 1
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self.nid.uCallbackMessage = WM_TRAYICON
        self.nid.hIcon = (self.icons.get("starting")
                          or load_stock_icon(IDI_APPLICATION))
        self.nid.szTip = APP_NAME
        shell32.Shell_NotifyIconW(NIM_ADD, byref(self.nid))

        if start_watcher and not self.watcher_alive():
            self.start_watcher()

        user32.SetTimer(self.hwnd, 1, 3000, None)
        self.refresh_state()

        msg = MSG()
        while user32.GetMessageW(byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))

        # Leave the watcher running if it was already there when we started.
        if self.proc is not None:
            self.stop_watcher()


def another_tray_running():
    """True if a tray already holds our mutex, or a pre-rename one."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return True
    for name in LEGACY_MUTEX_NAMES:
        h = kernel32.OpenMutexW(SYNCHRONIZE, False, name)
        if h:
            kernel32.CloseHandle(h)
            return True
    return False


def main():
    # A second tray would mean two icons; the watcher has its own lock.
    if another_tray_running():
        return 0
    Tray().run(start_watcher="--no-watcher" not in sys.argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())

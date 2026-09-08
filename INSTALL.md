# Installing AfterFlight

From a fresh download to a running tray. Windows only — the tray, the chase
camera and the SimConnect layer are all Win32.

Everything runs **in the folder you unpack it into**. There is no installer
that copies files elsewhere, no service, and no registry beyond one optional
"start with Windows" entry you have to ask for.

---

## What you need first

| | | |
|---|---|---|
| **Windows 10 or 11** | required | the tray is `ctypes` against Win32 |
| **Microsoft Flight Simulator 2024** | required | installed and run at least once |
| **Python 3.10 or newer** | required | 3.12 is what this is developed on |

There is no build step and no package manager. The UI is plain HTML and
JavaScript on purpose, and the tray has no pip dependencies at all.

> **Why Python at all?** Shipping without it is a known gap, written up in
> `DESIGN-NOTES.md`. Until that lands you need an interpreter on the machine.

### Install Python

Get it from [python.org](https://www.python.org/downloads/windows/) and **tick
"Add python.exe to PATH"** in the installer. Then check:

```powershell
py -3 --version
```

If that prints a version you are set. If `py` is not found, the Microsoft Store
build of Python also works but sometimes omits the launcher; use `python`
instead everywhere below.

---

## 1. Get the code

Either clone it:

```powershell
git clone <repository-url> afterflight
cd afterflight
```

or download the ZIP and unpack it. Put it somewhere you own — your user folder
is fine. **Avoid `C:\Program Files`**: the app writes recordings, the logbook
and its settings next to itself, and that folder needs elevation to write to.

---

## 2. Install the two dependencies

```powershell
py -3 -m pip install SimConnect==0.4.26
py -3 -m pip install Pillow
```

**`SimConnect` is required.** The version is pinned because that is what has
been tested; newer ones may work and have not been tried here.

**`Pillow` is optional.** Without it everything still records and grades — you
just lose the baked track-map PNGs, and the logbook draws maps in the page
instead.

---

## 3. Run the installer check

```powershell
.\install.ps1
```

This **only looks and reports** by default. It does not change anything until
you pass it a switch. You should see `[ok]` for Python, both dependencies, and
every application file. It creates `sessions\`, `sessions\clips\`,
`sessions\maps\` and `native\` if they are missing.

If PowerShell refuses to run it:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

---

## 4. Stage the SimConnect DLL

The ghost aircraft and the chase camera need a DLL that ships with the sim.
**It is not in this repository** — it is Microsoft's binary, and redistributing
it is not ours to do. The installer copies it out of your own sim install:

```powershell
.\install.ps1 -ResolveDll
```

Expect `RESOLVED` and a path. It looks in three places, in order: the `native\`
folder, the installed MSFS package, and any **running** sim.

**If it says `UNRESOLVED`, start Microsoft Flight Simulator and run it again.**
With the sim running the DLL is read straight out of the sim's own process,
which works whatever the install layout is — that is the answer whenever the
package lookup comes up empty. The sim does not need to be in a flight; sitting
at the main menu is enough.

Recording and grading work without the DLL; replay and the chase camera do not.

> **Re-run this after a sim update.** The path moves, and the symptom is the
> ghost silently failing to spawn.

---

## 5. Start it

```powershell
pyw tray.py
```

`pyw` is the Python launcher's windowed twin — it runs the tray with no
console window. Use it rather than `pythonw.exe`: the launcher lives in a
folder that is always on PATH, while `pythonw.exe` sits in the interpreter's
own folder, which is only on PATH if you ticked that box during install.

> **`pythonw.exe : The term ... is not recognized`** — either the box was not
> ticked, or your terminal was opened before Python was installed and is still
> holding the old PATH. Opening a new terminal fixes the second. `pyw` avoids
> both. If neither works, `.\install.ps1` prints the exact full path to use, on
> its last few lines.

An icon appears in the system tray. Right-click it for the menu; **Open
logbook** opens <http://127.0.0.1:8742/> in your browser.

`py -3 tray.py` also works and is useful when something is wrong — same thing
with a console window attached, so you see errors as they happen.

To have it start with Windows, and to get a Start Menu entry:

```powershell
.\install.ps1 -EnableAutostart -CreateShortcut
```

Both are reversible — `-DisableAutostart`, and delete the shortcut.

---

## 6. Fly something

Nothing appears until there is something to show. Start a flight in MSFS, take
off, land, and park. The tray icon reflects what the watcher can see, and the
logbook fills in a few seconds after you stop.

**The first thing to check** if nothing appears: the tray menu's `Open session
folder`. If there are no `.jsonl` files in it, the watcher never saw the sim.

---

## Optional: the in-sim tablet app

There is an EFB panel that shows the last landing rate inside the sim.

```powershell
py -3 deploy_efb.py --check     # compare, write nothing
py -3 deploy_efb.py             # deploy, then verify
```

**Restart MSFS afterwards.** It reads packages at startup, and reloading the
EFB app alone does not always pick up a change.

---

## Checking it works

None of these need the sim running:

```powershell
py -3 test_safety.py          # the hard safety rules. Run this one.
py -3 test_grading.py         # which profile grades what
py -3 test_supervision.py     # the tray notices a dead watcher
py -3 passenger.py            # the prose file and its stats
py -3 settings.py             # every setting, its value and its default
```

`test_safety.py` is the one that matters. It enforces the guarantees below.

---

## What it does to your machine

- **Writes only inside its own folder.** Recordings, clips, maps, the logbook
  and `settings.json` all live beside the code.
- **Listens on `127.0.0.1:8742` and nowhere else.** It is not a network
  service and cannot be reached from another machine.
- **Never writes to your aircraft.** Replay drives a separate AI ghost. Object
  id 0 — you — is refused by every write path, and `test_safety.py` proves it
  by driving each one and failing if any call is made.
- **One registry value**, only if you ask for autostart:
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
- **Fetches OpenStreetMap tiles** when it bakes maps, after a flight, never
  while you are flying. `--offline` keeps it to tiles already cached.

---

## Uninstalling

```powershell
.\install.ps1 -DisableAutostart
```

Exit the tray, delete the Start Menu shortcut if you made one, and delete the
folder. Nothing else was touched.

**Back up first if you want to keep your flights** — `.\backup.ps1` copies the
recordings and settings out. See below.

---

## Moving to another machine, or reinstalling

```powershell
.\backup.ps1            # recordings and settings, about 85 MB per few weeks
.\backup.ps1 -Full      # also the baked maps and tile cache
```

It writes a timestamped folder next to the tree with a `RESTORE.txt` inside
saying how to put it back. In short: install fresh per this document, copy the
backup over the top, then

```powershell
py -3 logbook_build.py --force
```

The maps, the tile cache and the built logbook are all derived and are left out
of a default backup — that rebuild recreates them.

---

## When something is wrong

| Symptom | Look at |
|---|---|
| Tray icon absent | run `py -3 tray.py` from a terminal so you can see the error; check `tray.log` |
| `pythonw.exe` not recognized | use `pyw tray.py`, or open a new terminal — see step 5 |
| Logbook page will not open | is the watcher up? `http://127.0.0.1:8742/state` |
| Page opens but is empty | no flights recorded yet — see step 6 |
| No maps | `Pillow` not installed, or the tiles were never fetched |
| Replay does nothing | `native\SimConnect_internal.dll` missing — step 4, with the sim running |
| Grades look wrong after an update | `py -3 logbook_build.py --force` |
| Everything looks stale | the watcher runs the code it started with; restart it |

`watcher.log` and `tray.log` sit in the folder and are the first place to look.
They record what was attempted and what the sim answered, which is usually
enough to see the problem without reading any code.

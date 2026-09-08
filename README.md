# AfterFlight

**A flight recorder, logbook and instant replay for Microsoft Flight
Simulator 2024.** It sits in the Windows tray, notices when you fly, and turns
each sortie into a logbook entry with a graded breakdown of how it went - then
lets you watch your own takeoff and landing back inside the sim, from outside
the aircraft.

Everything runs on the machine the sim runs on. Nothing is uploaded anywhere.

**How this was built.** AfterFlight is a collaboration between a human and an
AI. The direction is human: what to build, what a grade should mean, which
answers were wrong, and the flying that every claim about the simulator was
checked against. Nearly all of the code is AI-written to that direction, and
reviewed the same way - including by a second AI acting as an independent
reviewer, which found real defects in work the first one thought was finished.

That is stated here because it is a fact about the software you would otherwise
have to guess at, and because it shows in the source: the comments explain why a
number is what it is far more often than is usual, because the reasoning would
otherwise have been lost between sessions.

---

## What it does

**Records every sortie, without being told to.** The tray watches for the sim
and starts recording when you do. There is no start button and nothing to
remember. A flight becomes a *sortie*; a sortie splits into *legs* at each
takeoff and landing.

**Grades each leg, and shows its working.** Every leg gets a letter and a
score, broken into four phases - liftoff, climb, cruise and descent - and each
phase lists the measurements behind it with the band they were scored against:

```
liftoff   A   96.5
    Rotation       0.23 g   full marks 0.45 g, zero 0.9 g
    Fore/aft g     0.13 g   full marks 0.08 g, zero 0.45 g
    Steepest turn  3°       full marks 20°, zero 35°
```

You are told what was measured, what good looks like, and how far off it was.
A score with no band beside it tells you a number was bad without telling you
what would have been good, so the app refuses to show one.

**Judges the aircraft it is actually flying.** A Cessna is not graded on
airliner criteria. Four profiles - helicopter, light airplane, jet/transport,
and one for aircraft the sim does not classify usefully - are chosen from what
the sim reports about the airframe, not from its name. The split between light
and transport is at a 61 kt stall speed because that is where 14 CFR 23.49
draws it.

**Grades the landing separately.** Touchdown vertical speed sets an A-F letter.
How *square* the arrival was - bank through the rollout, and sideways
acceleration after the wheels are down - can hold that letter down but never
lift it. A gentle arrival that is still sliding sideways is not a good landing;
a perfectly square arrival at 600 fpm is still an arrival.

**Writes a passenger's note.** A short paragraph, in the voice of someone
sitting in the back, about what the flight felt like. Templates and a hash of
the leg id - no language model, no network - so the same leg reads the same way
every time.

**Draws a map of every leg and every sortie**, over an OpenStreetMap or
topographic basemap, baked after you land and never during the flight.

**Replays your takeoffs and landings in the sim.** This is the part that is
hard to describe and easy to demonstrate: pick a leg, press Replay, and the
recorded aircraft flies the approach again as an AI ghost while the camera
follows it from outside. You can pause, resume, and move the camera - distance,
height and orbit - while it plays.

**Never touches the aircraft you are flying.** The replay drives a separate AI
object. Your aircraft is refused by every write path in the code, and
`test_safety.py` proves it by driving each one with your object id and failing
if a single call is made.

**Shows the last landing on the in-sim tablet.** An optional EFB panel puts the
touchdown rate on a screen inside the cockpit, so you see it without leaving
the sim.

**Exports a track** as KML or GPX, built on demand, for anything that reads
those.

**Hides or deletes what you would rather not keep.** Hiding a leg removes it
from totals and grades and can be undone. Deleting is permanent, tells you
exactly which files it will destroy first, and reports what it actually managed
to delete rather than what it intended to.

---

## What it needs

Windows 10 or 11, MSFS 2024, and Python 3.10 or newer. Two pip packages:
`SimConnect` (required) and `Pillow` (optional - without it you lose the baked
map images and nothing else).

There is no build step and no package manager. The interface is plain HTML and
JavaScript on purpose, and the tray is `ctypes` with no dependencies at all.

Replay and the chase camera additionally need a SimConnect DLL that ships with
the sim. It is not in this repository - it is Microsoft's binary - and
`install.ps1 -ResolveDll` copies it out of your own installation. Recording and
grading work without it.

[INSTALL.md](INSTALL.md) is the step-by-step, written for someone who has just
downloaded this and has none of it set up.

---

## What it does to your machine

- **Writes only inside its own folder.** Recordings, clips, maps, the logbook
  and your settings all live beside the code. Nothing is written to your
  documents, your sim install, or anywhere else.
- **Listens on `127.0.0.1:8742` and nowhere else.** It is not a network
  service and cannot be reached from another machine.
- **Uploads nothing.** The only outbound traffic is map tiles from
  OpenStreetMap when it draws a map, after a flight, never during one.
- **One registry value**, and only if you ask for it: the autostart entry
  under `HKCU\...\Run`.
- **Never writes to your aircraft.** See above; it is the one rule in this
  project that could hurt someone, and it is enforced by a test rather than by
  a comment.

---

## Run it

What follows assumes the dependencies are already installed; see
[INSTALL.md](INSTALL.md) if they are not.

```powershell
.\install.ps1
```

That only checks. To actually wire it into Windows:

```powershell
.\install.ps1 -EnableAutostart -CreateShortcut
```

Start the tray by hand:

```powershell
pyw tray.py
```

`pyw` is the Python launcher's windowed twin, and it is always on PATH -
`pythonw.exe` only is if you ticked that box when you installed Python.

Then the logbook is at **http://127.0.0.1:8742/** — or double-click the tray icon.

---

## Working on this with an AI agent

`AGENTS.md` is the short brief an agent should read first: the hard rules that
are deliberately not settings, how to verify a claim about the sim, and the
traps that otherwise cost an afternoon. `CLAUDE.md` imports it rather than
holding a second copy, so the two cannot drift.

That file is the *rules*. [ENGINEERING.md](ENGINEERING.md) is the
*reasoning* — read the section there that covers whatever you are changing.

The rules that could actually hurt someone are not left as prose. `test_safety.py`
enforces them — never writing to the user's aircraft, never
`CameraSetRelative6DOF`, loopback only, a cheap detect loop, and no map or tile
fetching mid-flight. It discovers the methods it checks rather than working from
a list, so a new setter that forgets the guard fails without being registered
anywhere, and it injects an unguarded setter on every run to prove it can still
fail. Each test was checked by breaking its invariant on purpose: nine
mutations, nine caught.

## The pieces

| File | Job |
|---|---|
| `watcher.py` | SimConnect sampling, clip freeze, ghost + chase, HTTP. The only writer. |
| `logbook_build.py` | Builds `logbook.json` from the session tree. |
| `mapbake.py` | Bakes the track-map PNGs. |
| `tiles.py` | OSM basemap tiles, cached on disk. Never fetched during a flight. |
| `grading.py` | Splits a leg into phases and scores each one. Owns every threshold. |
| `flightprefs.py` | The per-flight rating and passenger-note switches. |
| `trackexport.py` | KML and GPX of a track, built on demand. |
| `passenger.py` | Template passenger assessment and the A–F landing grade. |
| `passenger_lines.json` | The prose itself, as data. Edit this to add lines, not the module. |
| `clipfile.py` | Where a clip lives on disk and how to read one. The only place that knows the layout. |
| `persistence.py` | Document, maintenance and recording locks; atomic and append-only writes. |
| `sampler.py` | The pushed state block — one data definition fetched on sim frames. |
| `settings.py` | Reads and validates the user settings file. |
| `snapshot.py` | Point-in-time copies of the session tree. |
| `tray.py` | Tray icon, watcher supervision, menu. Pure ctypes, no pip deps. |
| `trayicon.py` | Draws the per-state tray icons via GDI. No pip deps. |
| `logbook.html` + `logbook.js` | The UI shell. Renders `logbook.json`; writes nothing. |
| `replay.js` | Replay and chase-camera controls. |
| `deploy_efb.py` | Deploys the in-sim EFB panel, and verifies it byte for byte. |
| `install.ps1` | Checks, autostart toggle, Start Menu shortcut, DLL re-resolve. |
| `backup.ps1` / `verify-backup.ps1` | Copy what git deliberately does not, with a SHA-256 manifest, and check one. |

Tests, none of which need the sim:

| File | Job |
|---|---|
| `test_safety.py` | The hard rules, enforced. Run this one. |
| `test_grading.py` | Which profile grades what, and whether the UI can explain it. |
| `test_cache.py` | What a rebuild reuses, and what a delete claims to have done. |
| `test_replay.py` | Pose lookup during replay, against the scan it replaced. |
| `test_integrity.py` | Locks, partial deletes, clip damage, backup round trip. |
| `test_supervision.py` | The tray restarts a dead or wedged watcher. |

And the documentation:

| File | Job |
|---|---|
| [INSTALL.md](INSTALL.md) | From a fresh download to a running tray. Start here. |
| **README.md** | What this is and what it does. You are reading it. |
| [ENGINEERING.md](ENGINEERING.md) | Why each part works the way it does, and what was measured. |
| [AGENTS.md](AGENTS.md) | The rules you cannot discover from the source. Short. |
| [DESIGN-NOTES.md](DESIGN-NOTES.md) | Decisions written down and deliberately not built yet. |

### Tray icon states

The icon is distinguished by **shape** as well as color, so it stays readable
in grayscale and for a colorblind user.

| Icon | State | Meaning |
|---|---|---|
| gray slashed ring | `down` | The watcher is not answering on the port. |
| gray ring | `starting` | Tray is up, first poll not back yet. |
| amber ring | `idle` | Watcher up, sim not connected. |
| teal ring with a center dot | `connected` | Connected, nothing being recorded. |
| red filled disc | `recording` | A leg is being recorded. |
| green triangle | `replay` | A clip is playing. |

Hover the icon for the detail: aircraft and leg number while recording, clip id
and whether the chase camera has locked on the ghost while replaying.

Everything binds to `127.0.0.1` only.

---

## Browsing the logbook

The logbook used to be one document holding every track, leg and passenger
paragraph, rendered as one long scroll. That is ~16 KB per flight, so a hundred
flights would have been a 1.5 MB page load and an unusable list.

**The index and the detail are now separate.** `logbook.json` carries one
summary per flight - about **1 KB each**, so a hundred flights is ~96 KB - and
each flight's full detail lives in `sessions/detail/<sortie_id>.json`, fetched
only when that flight is opened.

The browse view is built on that:

- **Grouped by day**, and each day carries its own summary in the same
  value-over-label tiles the whole-logbook summary uses: flights, legs,
  airborne, distance, landings, with the aircraft flown that day underneath.
  The totals are for that day *within what is on screen*, so they follow the
  open month and any filter rather than describing flights that are not shown.
  Max altitude, max ground speed and a grade spread were tried here and taken
  out again: eight tiles on a repeating header is a wall of numbers, and those
  three are the ones nobody was reading.
- **Airframe chips** filter to one aircraft, with a flight count on each.
- **Month selector** and a **search** across aircraft, route, date and flight id.
- **Flights are collapsed** to one row - time, aircraft, route, airborne,
  distance, landing grades, and whether clips exist - and open on click.
- **Expand all** for when you want everything at once.

### What the ghost does and does not replay

**Flaps are driven from the surfaces, not the handle.** `GHOST_FLAP_VARS`
writes `FLAPS HANDLE PERCENT` and both `TRAILING EDGE FLAPS LEFT/RIGHT
PERCENT`. Writing the handle alone returned S_OK and moved nothing: the first
fixed-wing replay, a Cessna 152, flew its whole approach with the flaps up
while the recording held 0, 33 and 67 percent. They are held on pause too, for
the same reason the gear is - a landing clip opens a few hundred feet AGL,
which is where the sim's own AI reconfigures the aircraft, and a replay opens
paused.

**Spoilers are recorded and not driven.** They are sampled, written to the
track and stored in clips, but nothing writes them to the ghost, so a spoiler
deployment replays with the panels stowed. The flaps fix is the reason
this needs observation against a spoiler-equipped aircraft: commanding `FLAPS HANDLE PERCENT` returned S_OK and moved nothing,
because a driven object animates from the surfaces rather than the handle.
Spoilers will have the same split, and guessing which variable to write
without an aircraft to watch would most likely reproduce that bug rather than
avoid it. It wants a spoiler-equipped aircraft and one replay.

### The ghost wears what was recorded

There is no way to ask for a different livery. The UI used to carry a box for
typing an exact aircraft title, and a livery chosen by hand is a livery that
can be wrong while the recording already knows the answer. The order is now:
the livery recorded with the clip; failing that, the one loaded in the sim
right now if it is the same variant, which is almost certainly what was worn;
failing that, whatever the sim picks by default.

## Settings

A **Settings** button in the logbook opens Tier 1 settings: clip windows,
what a flight gets by default (rating, passenger
notes), basemap source and treatment, and replay defaults (start-held, ground
lift, camera distance/height/orbit).

Defaults are captured from the module constants at import, so the code stays
the source of truth. `settings.json` holds **only overrides** — a value equal to
its default is dropped rather than stored, so improving a constant in code later
is not silently overridden by a stale saved copy, and deleting the file returns
you to known-good.

Everything applies live. Clip windows take effect on the next event, map
changes trigger a rebuild, and the ring buffer is resized in place.

**`BUFFER_SEC` is derived, not configured.** `LANDING_BEFORE` is useless beyond
what the buffer holds, and offering both invites setting one and forgetting the
other, so the buffer is sized to `max(before) + 8 s` whenever the windows change.

Deliberately left in code: capture rate, replay rate, camera smoothing, map
canvas size. Easy to hurt yourself with, rarely want changing.

Deliberately **not** settings at all: never touching the user aircraft,
loopback-only binding, never re-anchoring a clip. Those are guarantees, and
making them adjustable would turn them into footguns.

## Removing a flight or a leg

Open a flight and use **Remove leg** on any leg, or **Remove flight** in the
footer. Both ask first, and the dialog states what will change rather than just
asking twice.

The two steps are deliberately told apart. Removing from the logbook is
reversible, so its dialog has a plain button and says where the item goes.
Only the permanent delete is red, lists the files, and holds its button
disabled behind an acknowledgement. (`[hidden]` loses to any author
`display` rule, which is why `.ack[hidden]` is declared explicitly - without it
the acknowledgement leaked onto the reversible dialog and contradicted it.)

**Removal hides; it does not delete.** The `.jsonl` tracks, the clips and
`events.jsonl` are the ground truth the logbook is derived from - deleting them
would make the removal unrecoverable, and a rebuild could never bring the flight
back. Hidden items live behind a **Removed** button in the header, which only
appears when something is actually hidden and carries the count - it used to be
a section pinned above the flights on every visit, including for anyone who had
never removed anything. Opening it lists each item with a
**Restore** button, and the exclusions live in `excluded.json`.

Everything derived follows the removal, which is the part that actually matters:

| | |
|---|---|
| **Flight totals** | Recomputed from the legs that remain, not from the whole recorded track - which still contains the hidden flying. Distance, airborne time, landings, max altitude and max ground speed all move. |
| **Logbook totals** | Flights, legs, landings, distance, airborne and the day-group headers all exclude it. |
| **Airframe summary** | Counts and the chip totals follow. |
| **The flight map** | Re-baked from the kept legs only, drawn as **separate segments** with their own start and end markers - so a removed middle leg leaves a visible gap instead of a straight line the aircraft never flew. The in-page canvas fallback does the same. Map URLs carry the build time (`?v=`), because the path never changes and a browser that had already decoded the old picture would keep showing it. |

Removing a leg usually moves the map's extent, and during a flight tiles cannot
be fetched (section 10). Rather than render half a basemap against black, a map
with less than 75% tile coverage falls back to the plain graticule; the next
rebuild after the flight fills it in properly.
| **Leg numbering** | Remaining legs renumber, but the identity used for exclusions is the takeoff instant, not the sequence number, so hiding leg 2 does not silently re-target leg 3. |

A flight with a removed leg is tagged **edited** in the browse list.

Hiding a middle leg must remove it from totals and grades and leave disjoint
map segments; restoring it must recover those derived values.

`POST /logbook/hide` and `POST /logbook/restore` take
`{scope: "sortie"|"leg", sortie_id, key}`.

### Deleting for good

The **Removed** panel offers each item two ways out: **Restore**, or **Delete
permanently**. Permanent deletion is only reachable from there - the endpoint
refuses anything that is not currently hidden:

```json
{"ok": false, "error": "not removed",
 "detail": "Remove it from the logbook first, then delete it."}
```

So a flight has to be taken out of the logbook, and seen out of it, before it
can be destroyed. The dialog is built from a **dry run** against the real files,
so it states what will actually go rather than a guess - the file list, the
byte count, and what happens to the recordings - and the confirm button stays
disabled until the acknowledgement is ticked.

| Deleting | What is erased |
|---|---|
| **A flight** | Every `.jsonl` and `.meta.json` behind it, its clips, its detail file, its maps, and its lines in `events.jsonl`. |
| **A leg** | Its clips, its lines in `events.jsonl`, and the recorded track between its takeoff and landing is cut out of the flight's `.jsonl`. The rest of the flight is untouched. |

Both clear the sortie cache so the next rebuild starts from what is left.

`POST /logbook/purge` takes `{scope, sortie_id, key, dry_run}`; `dry_run: true`
returns the plan and changes nothing.

There is no undo. That is the point of it being a second, separate step.

## Version

`APP_VERSION` in `watcher.py` is the release number. At startup the watcher
refines it with `git describe --tags --always --dirty`, so a working copy
reports exactly which commit is running — `0.4.0 (v0.4-7-gc892e43-dirty)` —
rather than whatever number was last remembered. It is read once, not per
request, and falls back to the bare constant when git or the repo is absent.

It appears in `/state`, in the startup log line, and in the logbook footer.

## What is not in the repository

Your flying is not. `sessions/` holds the recordings, clips, maps and tile
cache; `settings.json` holds your settings; `logbook.json` and the month and
detail files are built from them. None of it is tracked, so nothing you fly
can be committed by accident.

`native/` is not tracked either. That is the SimConnect DLL, which ships with
the sim and is Microsoft's to distribute - `install.ps1 -ResolveDll` copies it
out of your own installation.

## Not proven yet

**The ghost's gear surfaces are confirmed.** A retractable-gear jet replayed
with the gear retracting around touchdown and re-extending, while the
recording held 100% across all 1,295 points of the clip - so the value was
right and the sim was not honoring it. The gear command wrote
`GEAR HANDLE POSITION` alone, and on a driven object the handle is a
selection the sim's own AI keeps moving back; what is drawn follows the
per-gear positions. `GEAR CENTER/LEFT/RIGHT POSITION` are written too now, in
their own data definition so an airframe that refuses them does not cost the
handle write. **Watched in the sim on a Vision Jet: the gear stays down
through the landing.** Same shape as the flaps fix, and confirmed the same
way - by looking, not by S_OK.

Whether a control pad actually moves the placed camera is confirmed only as far
as the API goes (the flag is accepted and interaction is enabled); it still
wants a look in the sim.

Live clip capture and replay have been exercised against the sim.

The airplane grading profiles are uncalibrated. Their thresholds are
published criteria rather than anything measured here. A handful of
light-airplane legs have been recorded, but a handful is not a calibration and
almost all of them are good landings, so the bottom of the ladder is rarely
exercised in practice. Both profiles are built from published criteria rather
than from measured flights, and each phase cites the standard behind it.

`G FORCE` and body accelerations are recorded. Most ride scores still use
derived speed changes; rollout alignment uses the recorded lateral range.
Changing those inputs requires explicit recalibration and a grade comparison.
The measured axis convention is X lateral, Y vertical, Z longitudinal.

After a sim update the Store DLL path moves ( risk 2):

```powershell
.\install.ps1 -ResolveDll
```

---

## License

`LICENSE` is the **PolyForm Noncommercial License 1.0.0**, unmodified, with a
`Required Notice:` copyright line and one additional permission above it.

**You may** use it, read it, change it, build on it, and pass it on - including
changed versions - for any noncommercial purpose. Personal use, hobby projects,
study and amateur pursuits are named explicitly, as is use by charities,
schools, public research bodies and government.

**You may not** sell it, or sell anything derived from it.

**Streaming and video are allowed, including monetised.** Running this while
producing screenshots, video or live streams, and earning from that content on
a channel carrying advertising, sponsorship, subscriptions or memberships, is
permitted by the additional permission at the top of `LICENSE`. That covers the
content you make; it does not let you sell the software or bundle it into
something you sell.

**Noncommercial means more than "not sold".** Using this as part of your job is
outside the license even if nothing is being sold and nothing is being charged
for. If your use is commercial in that sense, ask.

**Redistributing it carries an obligation.** Anyone passing on any part of this,
modified or not, must include the license terms and every line beginning with
`Required Notice:` that came with the software:

```
Required Notice: Copyright 2026 Nathan Matthews. Built as AfterFlight.
```

That notice has to travel with the copy. Nothing in the license requires a
credit in your own interface or documentation.

**This is source-available, not open source.** The license is not OSI-approved,
so GitHub shows "Other" rather than a license badge, and this is not eligible
for anything that requires an open-source license.

*This summary is not the license and is not legal advice. `LICENSE` is what
governs.*

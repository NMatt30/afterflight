# AGENTS.md

Context for an AI agent working on **AfterFlight**, a Windows tray application
that records Microsoft Flight Simulator 2024 sorties, builds a logbook from
them, and replays takeoffs and landings in the sim with a camera on an AI
ghost aircraft.

`ENGINEERING.md` is the long-form reasoning: what was tried, what was measured, why
each number is what it is. Read it for any area you are changing. This file is
the short version — the rules you cannot discover from the source, and the
traps that will otherwise cost you an afternoon.

---

## Hard rules

These are guarantees, not preferences. They are deliberately **not settings**,
because making them adjustable would turn them into footguns. Do not weaken
one to make a feature easier.

| Rule | Why | Where it is enforced |
|---|---|---|
| **Never `SetDataOnSimObject`, freeze, or slew the user aircraft.** Replay drives an AI ghost only. Refuse object id `0`. | The user is flying. Writing to their aircraft mid-flight is the one bug in this project that could hurt someone. | `watcher.py` — every method taking an `object_id` checks it against `USER_OBJECT_ID` first: most raise `RuntimeError("refusing SetData on user aircraft")`, two return early. `test_safety.py` enforces it |
| **Never call `CameraSetRelative6DOF`.** | It is user-aircraft relative. The chase camera is placed in world coordinates instead. | `watcher.py`, and the startup log line says `CameraSetRelative6DOF not used` |
| **SimConnect and HTTP stay on loopback.** Never bind to `0.0.0.0` or a LAN address. | This talks to a running game on the user's own machine. It is not a network service. | `HTTP_HOST = "127.0.0.1"` |
| **The detect loop stays cheap** — about 1 Hz of real work, process priority Below Normal. No heavy work while the sim is flying if it costs frames. | Dropped frames in VR are the thing this must never cause. | `POLL_SEC`, and the priority set at startup |
| **Never burst-fetch map tiles mid-flight.** Bake after the sortie, or pass `allow_network=False`. | Same reason. | `logbook_build.build(allow_network=...)`; the watcher defers map bakes while a flight is active |

If a change appears to need one of these relaxed, that is the signal to stop
and ask, not to relax it.

**These are tests, not just prose.** `test_safety.py` enforces every row above.
It is behavioral where it matters: it does not check that a function says the
word "refusing", it drives each one with object id 0 through a stand-in
SimConnect table and fails if *any* call is made. Two of the eight guards
return early rather than raising, so a test looking for an exception would have
missed both.

The methods it checks are **discovered, not listed** — anything taking an
`object_id` parameter is exercised — so a new setter that forgets the guard
fails without anyone remembering to register it. One test injects a deliberately unguarded setter on every run and fails if
the other two accept it, because discovery that silently stopped working would
leave everything passing for ever.

Each test was verified by breaking the invariant it covers and confirming it
catches it: an unguarded setter, a guard placed *after* the call, a
`CameraSetRelative6DOF` call, a `0.0.0.0` bind, a 10 Hz detect loop, a bake
that runs mid-flight, and a tile fetched with `allow_network=False`. Nine
mutations, nine caught.

---

## How to be right here

**The sim is the only authority, and binding proves nothing.**
`AddToDataDefinition` accepts *any* simvar name and returns `S_OK` — it
accepted `STALL ALPHA` on a helicopter. `MapClientEventToSimEvent` is the same.
Only the **values** discriminate. Every simvar this project reads was checked
against a running sim with more than one aircraft loaded. Do the same before
you believe a new one.

**Measure before and after, and measure the thing the user sees.** A long list
of this project's bugs were changes that looked correct and were not: a metric
that scored every flight identically, a CSS rule that set `el.hidden` while the
element stayed on screen, an interpolation improvement measured against an
already-broken baseline. Numbers in commit messages here are real measurements;
keep it that way, and say plainly when something was *not* measured.

**State the standard, not the sample.** Source may say a profile is
uncalibrated and where its numbers come from in principle - a regulation, a
published guide, or "calibrated against real flights". It must **not** carry
counts of anyone's landings, their airframes, their flight ids, dates or
scores: this is a published repository and a stranger reading it has no
business with somebody's flying record. Aircraft names are fine where they are
*sim evidence* - "a C172 reports VS0 40 kt" is a fact about MSFS, not about a
person. If you measure something to justify a threshold, keep the reasoning in
the code and the personal data out of it.

Every profile carries a `sources` line naming where its numbers came from.
Only the helicopter profile is calibrated against measured flights; the rest
are built from published criteria, cited per phase.

Grades are derived, so changing a threshold silently rewrites history. If you
change one, say what data moved you - and say which legs moved.

**Match the source to the aircraft class.** Light airplanes and airliners were
once graded on one profile built from FSF and GPWS numbers, and a Cessna scored
100 on bank every flight because it never banks 35 degrees. They are split at
VS0 61 kt, the 14 CFR 23.49 boundary. Saturation is the symptom to watch for:
a metric returning the same answer for every flight is measuring nothing.

**The landing letter is two things, and only one of them can lift it.**
Touchdown vertical speed sets it; touchdown *alignment* - peak bank through
the rollout, and sideways acceleration after contact - can only hold it down.
Alignment caps the Descent *phase* on the same terms, which is how it reaches
the leg grade. It is listed with the Descent metrics at weight 0: giving it a
weight moves `w x (alignment - touchdown)` into every descent, which pays most
where the touchdown was worst - measured, it upgraded a hard landing by a
whole band. Four splits were tried against real legs and every one of them
raised most of the sample.
That asymmetry is deliberate: a landing that arrives gently while sliding
sideways is not a good landing, but a perfectly square arrival at 600 fpm is
still an arrival. `ROTARY` scores no alignment at all, because every
helicopter track may carry no lateral accelerations, and a missing
reading would grade as a flawless one.

**This logbook is one person's habits, not a sample of how aircraft are
flown.** The owner is a sim pilot, not a rated one, and their flying is
evidence about them - not about what a correct number looks like. So it can
tell you a threshold is *reachable*, and it can never tell you a threshold is
*right*. Set bands from published criteria and from the physics; use the
logbook to report which legs moved, and for nothing else. A band was once
narrowed because the wide one gave full marks to every flight here - the
flights just never banked hard enough to test it, and the fix made the app
mark down a correct pattern turn.

**But saturation is not always a wrong threshold.** Fore/aft g stays saturated
for light airplanes because they really are smoother than helicopters -
0.02-0.06 g against 0.02-0.22. Narrowing a band to manufacture a spread is
tuning grades to look busier. Leave it and say why.

**Every reported metric says what good looks like.** A leg's phase tooltip
prints the band beside the measurement - "6 degrees, 33/100, full marks 3.5,
zero 7" - because a score without a band tells a reader the number was bad
without telling them what would have been good. `test_grading.py` fails if any
reported part ships without one.

**Prefer a flag to a hardcoded choice**, with the existing behavior as the
default, placed near the other tunables at the top of its module.

---

## Layout

| File | Job |
|---|---|
| `watcher.py` | SimConnect sampling, clip capture, ghost + chase camera, HTTP server. The only writer. Runs as `pythonw watcher.py`. |
| `sampler.py` | The pushed state block — one data definition fetched on sim frames. |
| `logbook_build.py` | Builds `logbook.json` and the month files from the session tree. |
| `grading.py` | Splits a leg into phases and scores it. **Owns every threshold.** |
| `passenger.py` | Assembles the passenger paragraph and owns the A–F landing grade. |
| `passenger_lines.json` | The prose itself, as data. Edit this to add lines, not the module. `py -3 passenger.py` validates it. |
| `flightprefs.py` | The per-flight rating / passenger-note switches. |
| `mapbake.py`, `tiles.py` | Track map PNGs and OSM basemap tiles. |
| `trackexport.py` | KML and GPX of a track, built on demand. |
| `settings.py` | User-editable Tier 1 settings, applied over module constants. |
| `install.ps1` | Checks the machine, stages the sim's DLL, autostart and shortcut. Checks only unless given a switch. |
| `persistence.py` | Shared document, maintenance and recording locks plus atomic JSON/JSONL helpers. |
| `clipfile.py` | Where a clip lives on disk and how to read one. The only place that knows the layout. |
| `test_integrity.py` | Disposable fixtures for cache, persistence, deletion and backup recovery. |
| `test_replay.py` | Pose lookup during replay, against the scan it replaced. |
| `backup.ps1` | Copies what git deliberately does not, with a SHA-256 manifest and consistency status. |
| `verify-backup.ps1` | Verifies a backup manifest and every archived file hash. |
| `logbook.html` / `logbook.js` | The UI. Renders `logbook.json`; writes nothing directly. |
| `efb-pkg/` | The in-sim EFB app. Byte-exact — see `.gitattributes`. |

`DESIGN-NOTES.md` holds decisions taken far enough to write down and deliberately
not built yet - running without a Python install, a shareable debug bundle, and
why pattern work needs a flight-regime concept rather than another threshold.
Read it before designing either; it records what was measured and what is still
unverified.

`LICENSE` is the PolyForm Noncommercial License 1.0.0. Anyone may use, modify
and redistribute this; nobody may sell it or a derivative of it. Do not add a
dependency whose license is incompatible with redistributing this source, and
do not vendor code that cannot be shipped under those terms.

Data lives in `sessions/` and is **not** in git: flight tracks, clips, maps,
`logbook.json`, `excluded.json`, `flight_prefs.json`, `settings.json`.

---

## Traps that will cost you time

- **The watcher holds modules in memory.** Editing `logbook_build.py` or
  `grading.py` does nothing until you restart the watcher — and a stale watcher
  will happily overwrite a new-format `logbook.json` with the old shape.
  Restart it after touching any module it imports.
- **Kill the watcher by name *and* command line.** `CommandLine -like
  '*watcher.py*'` alone also matches the shell running your command. Filter on
  `Name = 'pythonw.exe'` as well.
- **`sed -i` rewrites the whole file as LF.** `.gitattributes` normalizes on
  commit so the repo is unaffected, but the working tree ends up inconsistent.
  Prefer a Python patch script or an editor tool.
- **Stale `.pyc`.** An edit of the same byte-length in the same second as a
  `py_compile` can leave the old bytecode in place. Delete `__pycache__` when a
  change appears to have no effect.
- **A probe will race the watcher's dispatch thread.** `GameSimConnect.open()`
  starts a background loop that consumes every message. Call `g._stop.set()`
  before reading replies yourself, or you will see most of them vanish.
- **CSS: an author `display` rule beats `[hidden]`.** There is a global
  `[hidden] { display: none !important; }` guard for this. Verify what
  *renders*, not what the DOM property says — three separate bugs here were
  elements that reported `hidden === true` while still on screen.
- **A clip is appended, never rewritten.** `clipfile.py` owns the filename
  rule and the reader; `OpenClip` appends through `persistence.append_lines`.
  A failed append rolls the file back and the same batch is retried, so a
  record is written once or not yet. An `end` record is the only evidence
  capture finished - parsing cannot tell a clip that completed from one
  that stopped at a checkpoint. Only the watcher may call a clip
  *interrupted*: recording claims are in memory, so a command-line build
  can only say completion was not recorded.
- **Nothing unchanged is reprocessed, and that is a default not a mode.** A
  rebuild reads a flight's track *and* its meta only when one of them has
  moved. **There are two signatures and both need the input.** The per-flight
  one in `scan_flights` had the meta; the per-sortie one did not, so a
  corrected `vs0` refreshed the flight record and the cached *sortie* - with
  the grade the old profile produced - still won. If you add a new input to a
  flight record, put it in `sortie_signature` too, or the thing built from it
  is stale for ever. `test_cache.py` covers it.
- **`force` and `reprocess` are different questions.** In
  `rebuild_logbook_now`, `force` is scheduling - run even though a flight is
  in progress - and `reprocess` is correctness: ignore the sortie cache. They
  were one word, so the Rebuild button jumped the deferral and then reused the
  very cache the user pressed it to escape. Only the Rebuild button asks for
  both.
- **Build-cache signatures are scoped on purpose.** Anything that is one file
  for every flight — `events.jsonl`, `flight_prefs.json` — must **not** go in
  the global signature, or one change rebuilds every sortie. Fold the resolved
  per-sortie value into `sortie_signature()` instead.
- **The filter bar is sticky.** Anything added to it costs viewport on every
  screen for ever. Check it at 1024 px wide with many airframes and months
  before adding a control.
- **A watcher runs the code it started with, and says so.** `/state` reports
  `code_stale` when any imported module on disk is newer than the process, and
  the logbook shows it beside the version. Comparing `/state` to `git describe`
  is the wrong test: a commit moves the description without moving a byte the
  process is executing.
  **It means "restart for Python", not "the repo is dirty."** `logbook.html`
  and `logbook.js` are read from disk per request, so editing them needs a
  reload and never a restart - and the flag correctly stays false for them.
- **Answering on the port is not proof the watcher works.** The HTTP server is
  its own thread, so a watcher whose detect loop has wedged still replies to
  `/state` while recording nothing. Liveness is `heartbeat_age_s`, which the
  detect loop stamps every pass, and the tray restarts on that.
- **Tests must not write to `watcher.log`.** Importing `watcher` makes
  `watcher.log()` append to the real operational log, so a test that drives a
  refusal path writes "refusing CameraSet on user aircraft" into it. That is
  indistinguishable from the watcher refusing one, and it made a real incident
  harder to read. `test_safety.py` replaces `watcher.log` on import.

---

## Running and checking

```bash
py -3 test_safety.py        # the hard rules above. Run this one.
py -3 test_supervision.py   # the tray notices a dead or wedged watcher
py -3 test_grading.py       # which profile grades what, and can the UI explain it
py -3 test_cache.py         # what a rebuild reuses, and what a delete claims
py -3 test_replay.py        # the ghost is placed where the aircraft was
py -3 test_integrity.py     # locks, partial deletes, backup round trip
py -3 sampler.py            # offline self-test: state block layout and peaks
py -3 flightprefs.py        # offline self-test: the per-flight switches
py -3 passenger.py          # offline self-test: the prose file, and its stats
py -3 trackexport.py        # offline self-test: KML and GPX output
py -3 settings.py           # prints the resolved settings
py -3 logbook_build.py --no-maps --offline --force   # full rebuild, no network
py -3 -m py_compile watcher.py sampler.py grading.py logbook_build.py
```

`--offline` keeps the basemap to already-cached tiles, so a rebuild is safe to
run at any time. `--force` ignores the sortie cache.

The UI is served by the watcher at `http://127.0.0.1:8742/`. There is no build
step and no package manager — the UI is plain HTML and JavaScript on purpose,
and the tray is pure `ctypes` with no pip dependencies.

**Before opening a pull request**

1. `py -3 test_safety.py` passes. If you changed something it covers,
   say why the invariant still holds.
2. Both self-tests pass, and everything compiles.
3. A full rebuild produces the same grades for existing legs, unless changing
   grades is the point — and if it is, say which legs moved and why.
4. Anything user-visible was checked in a browser, not only in the DOM.
5. Say what you measured. "Should be faster" is not a result.

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
| **Browser pages cannot command the watcher.** Every request needs this server's Host; every POST needs no Origin or the page's own, and JSON. Only `GET /state` is shared with other origins, for the EFB. | Loopback says where a request comes from, not who sent it: any web page can address 127.0.0.1. | `caller_refusal` in `watcher.py`, before dispatch; `test_http.py` |
| **The detect loop stays cheap** — about 1 Hz of real work, process priority Below Normal. No heavy work while the sim is flying if it costs frames. | Dropped frames in VR are the thing this must never cause. | `POLL_SEC`, and the priority set at startup |
| **Never burst-fetch map tiles mid-flight.** Bake after the sortie, or pass `allow_network=False`. | Same reason. | `logbook_build.build(allow_network=...)`; the watcher defers map bakes while a flight is active |

If a change appears to need one of these relaxed, that is the signal to stop
and ask, not to relax it.

**These are tests, not just prose.** `test_safety.py` enforces every row above
except the browser one, which `test_http.py` enforces through the real handler.
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

**The landing has one grade, its phase, and the touchdown is a word.** There
used to be a second A-F letter for the touchdown alone, which alignment, the
float and the touchdown spot could lower, beside the landing phase's grade -
a firm arrival on the aim point read "Landing B" and "C" at once. Now the
landing phase is the landing's grade, and the touchdown is described:
`grading.touchdown_word`, on the type's own scale - Butter, Smooth, Firm,
Hard, Very hard; for a transport Soft, On target, Firm, Hard, Very hard,
named for the published 100-250 fpm target band - and its rate. A leg too
short to grade in phases falls back to `touchdown_letter`, the letter of the
touchdown's score. The EFB tablet shows the same word: the watcher sends it
with each landing event (`touchdown_word`), since the tablet cannot work out
the aircraft type without a second copy of the thresholds.

**Alignment can only hold the landing down.** Peak bank through the rollout,
and sideways acceleration after contact, cap the landing phase, which is how
they reach the leg grade. Alignment is listed with the landing metrics at
weight 0: giving it a weight moves `w x (alignment - touchdown)` into every
landing, which pays most where the touchdown was worst - measured, it
upgraded a hard landing by a whole band. Four splits were tried against real
legs and every one of them raised most of the sample.
That asymmetry is deliberate: a landing that arrives gently while sliding
sideways is not a good landing, but a perfectly square arrival at 600 fpm is
still an arrival. `ROTARY` scores no alignment at all, because every
helicopter track may carry no lateral accelerations, and a missing
reading would grade as a flawless one.

**The float counts only when switched on** (`GRADE_FLOAT`, off by default). Time
from 50 ft above the runway to main-gear touchdown - the certification air
distance, AC 25-32 - measured from the 10 Hz landing clip on airplane
profiles. **The clock starts at the later of 50 ft and the threshold**: the
AC assumes 50 ft over the threshold, and an arrival crossing lower had its
approach counted as float. So the float needs the runway, and an
off-airport landing has none. The height over the threshold is recorded
beside it as information and graded nowhere.
**Runways are not flat, and the sim gives them one elevation.** Heights for
both - the threshold crossing and the float's 50 ft point - are above the
runway's surface, read from PLANE ALT ABOVE GROUND as the aircraft passed
over it (`grading.RunwaySurface`). Above the touchdown spot was off by 20-40
ft on runways that climb 2%. Only this landing's readings, over this runway:
a track is the whole flight, and "past the threshold" is a distance along a
heading - the departure and a downwind leg are past it too. It is scored in
steps, not a slope: 100 for a 7 s flare (AC 25-32's normal flare), 50 after a
further 2,000 ft of floating at the speed flown, 0 a further 1,000 ft on. The
2,000 and the 1,000 are AfterFlight's allowance, not a published scale - and
half marks are not the end of the touchdown zone, which they reach only near
85 kt; the touchdown spot measures the zone against the real runway. A first
version sloped linearly to zero and failed a jet touching down inside the
zone, which AC 91-79A calls typical. It is
measured and shown everywhere but counts for nothing until `GRADE_FLOAT` is
turned on.
**The touchdown point likewise** (`GRADE_TOUCHDOWN_POINT`, off by default):
distance past the landing threshold, 100 within 1,400 ft, 50 at the end of the
touchdown zone (3,000 ft or the runway's first third), 0 a further 1,000 ft on.
It needs the runway, which only the sim knows - see the facility-data trap.
**Both switches and their bands are settings; the touchdown curve and its
words are not.** The float and touchdown-point score, band and cap all read
the same values when called, so a setting moves them together. The defaults
are the published figures, kept in `grading.PUBLISHED`, and the grading panel
says when the bands in use are the owner's own instead. The build signature
carries the values (`runway_tunables`), because a setting changes them without
changing `grading.py`. No setting reaches a helicopter: every use is gated on
the profile, and the watcher asks the sim for no runway after one lands.

**The landing is a phase of its own** (`LANDING_PHASE`, on). The descent is
the approach; "landing" holds the touchdown and the landing limits. The
descent's weight is split by the touchdown's share of it, so the averages
are what they were with the touchdown inside the descent, and only the
weakest-phase cap sees more. Measured when it went on, that moved grades
down and never up: an approach no longer hides behind a good touchdown.
Off, the touchdown is 45% of the descent, and the landing grade falls back
to the touchdown alone.

**The landing phase is a blend.** It is how the landing was flown:
touchdown, touchdown spot and float blended (`LANDING_WEIGHTS`, a
judgment and labelled one), each only when its Settings switch is on. Two
limits keep the blend honest: it never sits more than a band above the
touchdown alone, so precision cannot rescue a hard landing; and a spot
past the touchdown zone, or a float past half marks, caps it as well as
weighing in, because a
soft touchdown otherwise averaged a landing far down the runway up to a C.

**The light touchdown curve steps at 60 fpm**, from 100 to 89. It was added
when the touchdown had a letter of its own, read off the curve, whose A had
drifted to 105 fpm; it still keeps a helicopter's landing grade - the
touchdown alone - at A only up to 60 fpm, where "Butter" ends.
`test_grading` checks `touchdown_letter` against `passenger.GRADE_TABLE` at
every rate. Change a curve, run it.
**The passenger's landing sentence is about how the arrival felt**
(`touchdown_feel`), and when the landing grade came out worse than that, it
says why - it slid, it floated, it landed long - read from the landing
phase's own record of which limit held it (`landing_marked_down_by`). A
second reconstruction, in a different order from the one the caps ran in,
named the wrong measure whenever two of them bit.
**And the ride is everything flown except the landing**, the approach
included: the passenger paragraph once called a ride smooth beside an F
approach. Check the prose against the pills when grading changes - an
audit of every leg, not a sample.

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

**Every reported metric says what good looks like.** A leg's Grading tab
prints the band beside the measurement - "6 degrees, 33/100, full marks 3.5,
zero 7" - because a score without a band tells a reader the number was bad
without telling them what would have been good. `test_grading.py` fails if any
reported part ships without one. The phase pills' tooltips are a summary
only: they once carried every band, a dozen lines the browser cut off at a
fixed width. Detail goes in the tab, not back into the tooltip.

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
| `install.ps1` | Checks the machine, stages the sim's DLL, autostart and shortcut. Checks only unless given a switch. Prefers a release's bundled `runtime\python.exe`. |
| `build_release.py` | The release zip: tracked app files, python.org's embeddable Python and Pillow (pinned, SHA-256 checked), `VERSION` and a launcher. Never anything of a user's - an update is a zip unpacked over an install. `.github/workflows/release.yml` builds it on a `v*` tag. **`DATA` is the inventory of user and runtime files**: `.gitignore`, the release guard and `backup.ps1` are checked against it. Add a new data file there first. |
| `persistence.py` | Shared document, maintenance and recording locks plus atomic JSON/JSONL helpers. |
| `clipfile.py` | Where a clip lives on disk and how to read one. The only place that knows the layout. |
| `runways.py` | Which runway a touchdown was on and how far past its threshold - geometry, and the per-airport runway cache under `sessions/runways/`. |
| `test_integrity.py` | Disposable fixtures for cache, persistence, deletion and backup recovery. |
| `test_replay.py` | Pose lookup during replay, against the scan it replaced; and who owns a replay when starts and stops overlap. |
| `test_arming.py` | When a reported aircraft becomes a flight, and what a rebuild publishes. |
| `test_map.py` | What the route map draws as one line, and where it breaks - and so where it puts markers. |
| `test_efb.py` | When a deploy may rewrite the EFB package's committed `layout.json` - only when its files changed. |
| `test_release.py` | What the release zip carries and must never carry: no user data, no missing module, a Python that finds the app, a version without git. |
| `test_runways.py` | The touchdown point end to end: geometry, the zone score, facility-message parsing in the measured layout, the builder, and the cache signature. |
| `test_native.py` | The sim connection without Python-SimConnect: quit, heartbeat, a held pause, and the recording loop run with the package unimportable. |
| `test_http.py` | Who may command the watcher: the real handler on a spare port, every action recorded rather than run, a refused request reaching none. |
| `backup.ps1` | Copies what git deliberately does not, with a SHA-256 manifest and consistency status. |
| `verify-backup.ps1` | Verifies a backup manifest and every archived file hash. |
| `logbook.html` / `logbook.js` | The UI. Renders `logbook.json`; writes nothing directly. |
| `efb-pkg/` | The in-sim EFB app. Byte-exact — see `.gitattributes`. |

`DESIGN-NOTES.md` holds decisions taken far enough to write down and deliberately
not built yet - a shareable debug bundle, and why pattern work needs a
flight-regime concept rather than another threshold - plus the reasoning behind
running without a Python install, which is now built. Read it before designing
either; it records what was measured and what is still unverified.

`LICENSE` is the PolyForm Noncommercial License 1.0.0. Anyone may use, modify
and redistribute this; nobody may sell it or a derivative of it. Do not add a
dependency whose license is incompatible with redistributing this source, and
do not vendor code that cannot be shipped under those terms.

Data lives in `sessions/` and is **not** in git: flight tracks, clips, maps,
`logbook.json`, `excluded.json`, `flight_prefs.json`, `settings.json`.

---

## Traps that will cost you time

- **The privacy guard only sees tracked files.** `test_safety`'s check for
  usernames, machine names and flight ids runs over `git ls-files`, so a new
  file passes it while untracked and can fail on the commit that adds it. Run
  the safety suite *after* `git add`. Fixture flight ids belong in the 19xx
  form `test_cache.py` uses: that is outside the guard's `flt-20...` pattern by
  construction, where an allowlist entry is one more thing to go stale.
- **A valid aircraft is not a flight.** `is_valid` asks for a title and a
  plausible lat/lon, and the sim answers both while its own menus are up, with
  the chosen aircraft parked at the departure position. Choosing an aircraft
  therefore used to mint a flight, and a session spent choosing several minted
  one each. A fresh aircraft is now held as a `PendingFlight` - points in a
  ring, nothing on disk - until it is seen to move 50 m in 60 s or to leave the
  ground, and only then is the ring replayed into a track. `arm_or_begin` is
  the whole decision and `test_arming.py` drives it.
  **Do not gate this on a speed.** Airspeed on a parked aircraft is the wind,
  and ground speed jitters to within a hair of 1 kt while the aircraft settles
  on its gear. Displacement over a *sliding* window is the form that works; a
  running total from where the candidate armed lets slow drift accumulate past
  any threshold given a long enough sit in the menus.
  **And being somewhere else is not having gone there.** Picking a different
  parking spot moves the aircraft without it travelling, and the continuity
  check only catches repositions over `RESUME_JUMP_NM` - so a change of stand
  sat between that and the 50 m trigger and minted a flight. Each step is now
  checked against the ground speed the sim reported for it. That is
  corroboration and not a speed gate: displacement still has to pass on its
  own, so wind arms nothing.
  **And "not on the ground" is not flying.** The sim can report SIM ON
  GROUND false for a few ticks while loading an aircraft in, frozen on its
  pad at 0.0 g. Airborne only arms on no positive evidence of a frozen sim,
  and a missing g reading is not that evidence - legacy ticks carry none.
- **A landing needs a flight to land from.** The tracker debounced the takeoff
  side (airborne for `BOUNCE_SEC` before it counts) but not the landing side,
  which checked the aircraft had been down long enough and never that it had
  been up. Any blip off the ground became a landing with no takeoff. A landing
  now requires `flown`, set by the same `BOUNCE_SEC` and cleared when a
  landing commits - and set on reattaching in the air, or a watcher restarted
  just before touchdown would refuse a real landing. **The tracker runs at
  10 Hz and the track is written at 1 Hz**, so a fault in detection has to be
  reproduced at 10 Hz: the recording can flatten it away entirely.
- **Facility data was measured, not read from the SDK.** The runway lookup
  asks the sim for its airport list (message 18: every airport in the world,
  84,000 of them in 75 parts, 36-byte entries of a 9-byte ident, 3-byte region
  and three doubles) and per-airport runway data (28 per record, 29 when
  done). Lengths and widths are **metres**; headings are true, for the primary
  end; displaced thresholds arrive primary first. Each was checked live -
  Seattle's runways within 0.1% of their published lengths, San Diego's
  displaced 27 at 1,808 ft against 1,810, and every recorded airplane
  touchdown located on a runway centreline to within 10 ft. `test_runways.py` encodes the layout
  so drifting from it fails offline. The lookup runs only when parked - checked
  before each request, not once at the start - on its own short-lived
  connection, never the one the recording rides on. It reports done, more,
  cancelled or failed, and only done takes a landing off the queue.
  **A landing's runway is known when a cached runway contains the touchdown**,
  not when a cached airport is near it: a heliport cached 2.5 nm from a
  landing once hid the airport it was really at from every later lookup.
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
- **A dialog stays open until its work is done.** The four panels are
  `<dialog>`s opened with `showModal()`, so the page behind is inert to
  keyboard as well as mouse. A confirm that starts work runs it from the
  dialog (`confirmRemoval`'s `action`) and closes on the result; closing on
  the click left a delete running for two minutes behind a page that showed
  nothing and still took clicks. Let the browser handle Escape - closing the
  dialog from its `cancel` event passed the key press on to the Removed list
  beneath. **Test it with real clicks:** Chrome groups dialogs opened without
  a user gesture, and one Escape then closes all of them, so a scripted
  `.click()` reports a bug that a person never sees.
- **A clip is appended, never rewritten.** `clipfile.py` owns the filename
  rule and the reader; `OpenClip` appends through `persistence.append_lines`.
  A failed append rolls the file back and the same batch is retried, so a
  record is written once or not yet. An `end` record is the only evidence
  capture finished - parsing cannot tell a clip that completed from one
  that stopped at a checkpoint. Only the watcher may call a clip
  *interrupted*: recording claims are in memory, so a command-line build
  can only say completion was not recorded.
  **Ask `clipfile` for names, never a suffix.** A resume counted its legs
  from clip files ending `.json` after clips had become `.jsonl`, so every
  resumed flight restarted at leg 1 and appended its next leg to leg 1's
  recordings. Opening a clip now also refuses a name already on disk and
  takes the next free leg, so a miscount costs a number, not a recording.
- **Nothing unchanged is reprocessed, and that is a default not a mode.** A
  rebuild reads a flight's track *and* its meta only when one of them has
  moved. **There are two signatures and both need the input.** The per-flight
  one in `scan_flights` had the meta; the per-sortie one did not, so a
  corrected `vs0` refreshed the flight record and the cached *sortie* - with
  the grade the old profile produced - still won. If you add a new input to a
  flight record, put it in `sortie_signature` too, or the thing built from it
  is stale for ever. `test_cache.py` covers it.
  **A delete is not a reprocess either.** It used to empty the cache and ask
  for one, to be safe - every flight rebuilt and every map redrawn, about
  two minutes per delete. What a delete changes is all in the
  signatures, and `test_cache.py` checks the result against a reprocess.
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
  before adding a control. Its height, and the header's, are watched, not
  measured once: the bar wraps after the flights load, and a height taken at
  start pinned the camera bar behind it.
- **A reload must not move the reader.** Every rebuild reloads the list, and
  rebuilds come after each takeoff, landing, runway lookup and watcher start.
  `load()` keeps the flight at the top of the screen where it was, and open
  flights show their old detail until the new arrives. Verify with the page
  scrolled and flights open - a reload at the top of the page proves nothing.
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
- **A replay is started and stopped under `_replay_lifecycle`, and a worker
  checks its generation.** `start_replay` and `stop_replay` each hold the lock
  for the whole operation; inside a start, stop the old replay with
  `_stop_replay_locked`, or the lock deadlocks against itself. A worker
  writes `RUNTIME["replay"]` or releases the camera only while
  `_replay_is(gen)` - a clip id is not ownership, since two replays of one
  clip share it. `test_replay.py` covers both.
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
py -3 test_arming.py        # a menu is not a flight; a parked aircraft is not one either
py -3 test_map.py           # a sim skip is not a landing and a takeoff
py -3 test_efb.py           # a deploy does not dirty the committed EFB layout
py -3 test_runways.py       # where on the runway, and what the sim really sends
py -3 test_release.py       # the release zip: no user data in it, nothing missing
py -3 test_native.py        # the sim connection with no Python-SimConnect
py -3 test_http.py          # a web page cannot command the watcher
py -3 test_integrity.py     # locks, partial deletes, backup round trip
py -3 sampler.py            # offline self-test: state block layout and peaks
py -3 flightprefs.py        # offline self-test: the per-flight switches
py -3 passenger.py          # offline self-test: the prose file, and its stats
py -3 trackexport.py        # offline self-test: KML and GPX output
py -3 settings.py           # prints the resolved settings
py -3 logbook_build.py --no-maps --offline --force   # full rebuild, no network
py -3 -m py_compile watcher.py sampler.py grading.py logbook_build.py
```

CI discovers `test_*.py` rather than keeping a list, so a new test file runs
there without anyone remembering to add it. A hand-kept list let
`test_arming.py` ship in v0.6 behind green checks that never ran it.

`--offline` keeps the basemap to already-cached tiles, so a rebuild is safe to
run at any time. `--force` ignores the sortie cache.

The UI is served by the watcher at `http://127.0.0.1:8742/`. There is no build
step and no package manager — the UI is plain HTML and JavaScript on purpose,
and the tray is pure `ctypes` with no pip dependencies.

**Branches.** Work lands on `develop`. `main` holds releases only, and is
promoted from `develop` by a pull request merged with a **merge commit**,
after the change has been flown in the sim - not squash or rebase, which
rewrite every commit and leave the two branches sharing no history after
each release. Afterwards `develop` fast-forwards to the merge commit. Open
pull requests against `develop`.

**A release tag builds the zip.** `.github/workflows/release.yml` runs on a
`v*` tag: it builds `AfterFlight-<tag>.zip` with `build_release.py`, runs every
test on the Python inside it, and keeps the zip as a workflow artifact. It
publishes nothing - the zip goes on the GitHub release by hand, once checked.

**Before opening a pull request**

1. `py -3 test_safety.py` passes. If you changed something it covers,
   say why the invariant still holds.
2. Every test file and the offline self-tests listed above pass, and
   everything compiles.
3. A full rebuild produces the same grades for existing legs, unless changing
   grades is the point — and if it is, say which legs moved and why.
4. Anything user-visible was checked in a browser, not only in the DOM.
5. Say what you measured. "Should be faster" is not a result.

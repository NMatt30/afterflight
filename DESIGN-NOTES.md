# Design notes — parked

Decisions taken far enough to be worth writing down, and deliberately not
implemented yet. Each one records what was measured, what was chosen, and what
is still unverified, so picking it up later does not mean re-deriving it.

Nothing here is committed to. If something moves from here into the code, the
entry should say so or be deleted.

---

## 1. Running on a machine with no Python

**Discussed 2026-09-04. Needed before a public 1.0.**

### The measured starting point

The whole host codebase has **two** third-party imports:

| Dependency | Where | Status |
|---|---|---|
| Pillow | `mapbake.py`, `tiles.py` | Already optional behind `HAVE_PIL`; degrades to no maps |
| `SimConnect` | `watcher.py`, `snapshot.py` | The one hard dependency |

The `SimConnect` dependency is shallower than it looks: the connection object
plus `AircraftRequests` as the fallback poll. The project already carries a
complete ctypes SimConnect layer of its own in `GameSimConnect`, which does the
real work.

### The plan

1. **Drop Python-SimConnect first**, letting `GameSimConnect` own the
   connection outright. That leaves zero hard dependencies.
2. **Ship the python.org Windows embeddable package** (~10 MB) beside the
   source, with a small launcher.

This keeps the source plain readable `.py`, which the public-repo and
contributor-PR plan depends on — what you read is what runs, one artifact for
both audiences.

### Why not freeze it (PyInstaller / Nuitka), for now

This codebase would fight a freeze in specific, identifiable ways:

- `tray.py` launches the watcher as `[sys.executable, WATCHER]`. Under a freeze
  that means "relaunch the exe" and needs argv dispatch.
- Eight modules compute `BASE` from `__file__`.
- `git describe` versioning would need baking at build time.
- `sessions/`, `logbook.html`, `logbook.js` and `efb-pkg/` are writable data
  that must sit beside the binary — so onedir, not onefile. A folder either
  way, which is most of what a freeze was meant to avoid.
- Antivirus false positives on a sideloaded flight-sim utility.

Worth revisiting if a signed single-file installer is ever wanted. **The argv
dispatch and the `BASE` changes are small and worth doing regardless** — they
cost nothing now and remove two blockers later.

### Not verified

Whether the ctypes layer can fully own connection lifecycle and quit detection
in place of Python-SimConnect. Small surface, but it needs a real test against
a running sim before the plan is committed to.

---

## 2. Sharing a flight for debugging

**Discussed 2026-09-05.** The user picks a flight, the app builds a zip, the
user shares it. For diagnosing a reported bug, and for looking at real flights
the way legs get examined here — reading the numbers, not training anything.

### The shape

**The app prepares a bundle. It never sends one.**

That is not squeamishness, it is the hard rule in `AGENTS.md`: this talks to a
running game on the user's own machine and is not a network service. An
uploader would be the first outbound path in the app that sends rather than
fetches. Keeping the send in the user's hands means:

- no endpoint to run or secure, no privacy policy, no retention obligations
- the trust model is "open the zip and read it", not "trust the vendor"
- a fork cannot quietly redirect a phone-home that does not exist
- it lands where a public repo's bugs already land — an issue tracker

### What goes in

A "Share this flight" action on a sortie, producing
`afterflight-<sortie>-<date>.zip` and opening the folder it landed in.

| Always | Why |
|---|---|
| `manifest.json` | Every file in the bundle and why it is there. Read this first. |
| `/state` output, app version, `code_stale` | Which code was actually running |
| `settings.json` | Thresholds and clip windows in force |
| Environment | Windows build, Python version, Store or Steam DLL path |
| The chosen sortie's `.meta.json`, track and its events | The flight itself |
| Log excerpts | Head **and** tail: startup carries the DLL path and binding, the tail carries the failure |

| Opt-in inside the bundle | Why not default |
|---|---|
| Clips | ~500 KB each. Ghost and camera bugs need them; nothing else does. |
| Other sorties | One flight is the unit. |

### Two details that matter more than they look

**Path redaction must be the default, not a tickbox.** `watcher.log` is full of
`C:\Users\<name>\...`, so the Windows username is a direct identifier in every
path. People will send the bundle without reading it, so it has to be right
before they do.

**Do not ship the whole log.** It runs to tens of thousands of lines. Head plus
tail, with the middle elided and the elision stated.

### If a corpus of flights is ever gathered

Same "prepare, never send" rule, but a **different and much smaller file** — a
derived record per landing rather than a track:

> touchdown fpm · peak bank through rollout · lateral scrub · g peaks ·
> category and VS0 · time on the ground · bounces · phase durations · the
> metric **inputs**

Not lat/lon, not timestamps, not sortie ids, not file paths, and **not the
aircraft title** — the title names whatever payware someone bought and adds
nothing `category` and `VS0` do not already give. Routing already ignores
titles because they are unreliable; they are also unnecessary here.

**Store the inputs, never the scores.** Thresholds moved four times in one
session; a file of scores is stale the moment a band moves, while the raw
measurements stay good. Give it a `schema` and an app version.

A **random install id** — generated once, not derived from hardware or
username — separates twenty landings from one pilot from twenty pilots'
landings, without identifying anyone.

Concretely this is what would let the light-airplane and transport profiles
stop being uncalibrated. See the per-phase sources for what is behind
them, which is not much.

### What not to build

- **Continuous background telemetry**, even anonymous. It turns a local tool
  into a data collector and someone then has to answer for it.
- **One "help improve AfterFlight" toggle covering both.** The debug bundle is
  deliberately identifying — it is about your machine and your flight. A
  corpus record is deliberately not. One switch misrepresents both.

### The thing to get right first

**Show the person the exact bytes before they send them.** Everything else —
redaction rules, field lists, opt-in defaults — follows from that. If it is
true you can be relaxed about the rest; if it is not, no policy compensates.

---

## 3. Pattern work is a flight regime, and grading has no concept of one

**Discussed 2026-09-05, after three circuits in a C172.**

Everything in `grading.py` assumes a leg goes somewhere: climb out, cruise,
descend, land. A circuit does none of that, and the grader has no way to know
it is looking at one.

### What the circuits showed

Leg detection itself was fine — three circuits became three legs, with 19.3 s
and 15.7 s on the ground between them, comfortably clear of the 3 s bounce
merge. That part needs nothing.

The grading is where the regime shows. Two problems, one fixed and one not.

**Fixed:** the descent phase scored no bank at all, on any profile. In a
circuit the steepest turn of the whole leg is base to final, and it sits in
that phase — so the gentle crosswind turn was graded and the firm one was
invisible. Bank now carries 12% of the descent everywhere.

**Not fixed, and not fixable with a threshold:** the bank band penalises
correct pattern flying. Published guidance puts circuit turns at 20–30
degrees. The light-airplane band gives 20 degrees half marks and 30 degrees
zero. Widening it is not the answer — the steepest bank ever recorded in a
light airplane here is 18 degrees, so a band matching the handbook would put
every measurement at full marks and the metric would measure nothing.

The band is not really wrong. It measures **how the turn felt**, and a 30
degree turn is firm whether or not it was the right turn to make. The
description now says so. What is missing is any notion that the same number
means different things in different flying.

### What a fix would need

Not a threshold. A regime, detected and then applied:

- **Detection.** Repeated legs from and to the same place, short, low, with
  the altitude profile of a circuit rather than a journey. Probably: N legs
  in one sortie whose start and end are within a few hundred metres of each
  other and of the previous leg's.
- **A different phase model.** There is no cruise in a circuit — the phase
  the grader calls cruise is the downwind. Splitting a circuit into
  lift-off / climb / cruise / descent is already the wrong shape.
- **Different bands, or different weights.** Once the regime is known, bank
  can be judged against what a circuit is supposed to look like.

### Why it is parked

It is a new concept in the one file that owns every threshold, and there is
one pattern session to calibrate against. Detection that is wrong is worse
than no detection: a cross-country leg mistaken for a circuit would be graded
against numbers meant for something else, silently.

This is the same class of error as the touchdown curve was before 2026-09-05
— numbers appropriate to one kind of flying applied to another where
different answers are correct. That one was fixable by splitting a constant.
This one is not.

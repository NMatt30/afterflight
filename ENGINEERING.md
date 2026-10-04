# AfterFlight — engineering record

Why each part works the way it does: what was tried, what was measured, what
broke, and what the numbers actually are. [README.md](README.md) says what the
software does; this says why.

It is long, and deliberately so. Most of it exists because something was once
wrong in a way that was not obvious, and the reasoning is worth more than the
conclusion. If you are changing an area, read its section first -
[AGENTS.md](AGENTS.md) lists the rules you cannot discover from the source, and
this file is the evidence behind them.

---

## Data model

`sessions/*.jsonl` are **flight records**. A reconnect or position jump
starts a new fragment. Fragments can overlap in time rather than running
back to back; sortie grouping must account for that.

### What a track row holds

One row a second. Three kinds of field:

| | Fields | Note |
|---|---|---|
| Position and motion | `ts` `lat` `lon` `alt` `vs` `gs` `heading` `airspeed` `on_ground` | The original set. |
| Measured, as at that instant | `bank` `pitch` `agl` `gforce` `accel_x` `accel_y` `accel_z` `gear` `flaps` `spoilers` | Read from the sim rather than worked out afterwards. |
| Worst within the second | `gforce_max/min` `accel_x/y/z_max/min` `vs_max/min` | The extremes over ~30 samples, not one reading taken on a random phase of the bump. |

The sampler collects extremes on every push and hands them over when read; the
detect loop reads ten times per recorded row, so it merges those windows and
`_record_point` empties the accumulator it was given. Losing or double counting
a window would both corrupt the ride figures, so `sampler.self_test()` covers it.

`bank` and `agl` replace estimates. Grading prefers recorded bank and height
above ground, falling back to turn-derived bank and landing-site elevation
only for a track without those fields.

Two measures read the peaks directly: the **rotation** at lift-off is the
largest departure of `G FORCE` from 1 g over the phase, and landing
**alignment** reads the lateral peak-to-peak per rollout second (`accel_x`).
The comfort measures - vertical and fore/aft g in each phase - are still
derived from the once-a-second rates their bands were set against: swapping in
a directly measured figure without re-deriving the bands would move every
grade for no honest reason.

Cost: 247 -> ~370 bytes a row, about 1.3 MB per flight hour, and 0.003% of a
core for the peak tracking.

### The index is split by month

`logbook.json` used to carry a summary of every flight, so opening the logbook
grew with the whole history — 956 bytes and one DOM row per flight, which is
650 KB and 700 rows after a year at the rate this one fills up.

It now carries three things instead:

| Key | What it is |
|---|---|
| `months` | One line per month: counts, totals, and the file holding its flights. |
| `find` | Every flight, but only what filtering and search need — id, month, date, aircraft, both route ends. Short keys on purpose: this is the one list that still carries every flight, so it is the one that has to stay small. 173 bytes against 956. |
| `totals`, `airframes`, `hidden` | Unchanged. |

The flights themselves live in `sessions/months/YYYY-MM.json`, fetched the
first time that month is opened and kept for the session. The UI opens on the
newest month with flights in it; a search runs against `find` and pulls in only
the months that actually have matches.

| | Page load | Rows drawn |
|---|---|---|
| Before, after 1 year | 654 KB | 700 |
| After, after 1 year | 120 KB | one month |
| After, after 3 years | 357 KB | one month |

### The filter bar has to stay one row

It is sticky, so anything it grows costs viewport on every screen for ever.
Two controls in it scale with the logbook rather than with the window, and both
were measured against a fabricated 20-airframe, 16-month, 2-year logbook rather
than reasoned about:

**Months are one control, not a chip per month.** A chip each was fine at two
months and took a whole row at sixteen. With the airframe strip above it the
bar reached **147px across three rows**, which put the first flight below half
the viewport permanently, and left the year select stranded on a different row
from the months it drove. It is now a stepper - `‹ September 2026 ›` - beside a
picker showing every month of every year at once, so two years back is one
click rather than twenty-four steps. Months with no flying are shown but
dimmed and unclickable, so the gaps in a year stay visible.

**Airframes turn into a select past `AIRFRAME_CHIP_MAX` (8, counting All).**
Chips read well while they fit and badly once they do not: at twenty the strip
is a thin sideways-scrolling gutter where the one you want is almost always out
of sight. The threshold keeps chips for the common case of a few airframes.

The bar measures **57px on one row** at 1440, 1280 and 1024 px wide with that
same 20-airframe, 16-month logbook loaded.

While a search is running there is no single month to show, so the control
reads "All months" with the count of months that have matches, and the stepper
arrows are disabled.

Month files are only rewritten when their contents change, and a month whose
last flight was removed has its file pruned, the same way stale maps are.

Months are keyed on the flight's **local** date, not its id. A sortie recorded
at 03:30 UTC on 1 June is `flt-20260601T033000Z` but belongs to 31 May in
Mountain time, and it is filed under May — the same date the day headings group
by, so the two cannot disagree.

So the builder groups them:

- **sortie** — consecutive flight records, same airframe, no real gap (and no
  teleport). What a human calls "a flight".
- **leg** — a takeoff paired with the **next landing in time order**, within a
  sortie. Pairing is chronological, not by the raw `leg` counter, because after
  a reconnect a leg's takeoff and its landing land in two *different* flight
  records. A takeoff and its landing can therefore carry different fragment
  ids and leg counters.

If a sortie has no events in `events.jsonl` at all, legs
are recovered from `on_ground` transitions in the track, with the same 2.0 s
bounce filter the watcher uses. Those legs are tagged `from track` in the UI and
have no replay clips, because none were ever recorded.

### Landing grade

A landing that bounces is one landing, graded on the **firmest** contact and
timed from the **first**, and it is reported as one. The landing record carries:

| Field | Meaning |
|---|---|
| `contacts` | How many times it touched |
| `bounces` | Contacts after the first |
| `contact_rates_fpm` | Rate of each contact, in the order they happened |
| `bounce_heights_ft` | Peak height of each bounce |
| `bounce_detail` | Each bounce paired with the touchdown that ended it |
| `firmest_fpm` | The one it was graded on |

**A landing with no recorded bounces is recovered, not left blank.** One without
bounces were captured still has the evidence in its clip, so `derive_contacts()`
reconstructs the count, the heights and the per-contact rates and marks them
`contacts_derived`. It reads the **clip**, not the flight log: the log is 1 Hz
and a bounce lasting under a second is simply not in it, while clips are the
full 10 Hz around the event. What cannot be recovered is
`PLANE TOUCHDOWN NORMAL VELOCITY`, which was never recorded — derived rates are
vertical speed, a different measure, so the recovery never touches the stored
grade and the UI says where the numbers came from.

Height is measured from the **last sample that was really on the ground**, not
from the first sample that reads airborne — by then the aircraft has already
left, and the bounce reads short. The logbook shows a pill beside the grade
whose tooltip reads the whole sequence: arrival rate, then each bounce as
"up N ft, back down at M fpm". Collapsing a bounce
into a single number without saying so is how a two-contact arrival came to look
like one clean landing. The bounce filter used to drop the pending landing
outright when the aircraft left the ground again, so the grade came from
wherever it finally settled - always softer than the arrival, and not what the
landing felt like. The arrival must remain the graded contact when a bounce is softer.
The private calibration record holds the original comparison. A contact more than `BOUNCE_MERGE_SEC` after the aircraft
lifts is a touch-and-go, and stays two events.


**The letter is judged on the aircraft type's own criteria.** It is the
touchdown's score on its type's curve, read through the phase letters (A 90,
B 80, C 70, D 60) - `grading.touchdown_letter` - so a letter and the score
beside it are one verdict. On touchdown vertical speed, local, no cloud:

| Rate, fpm | Light airplane, helicopter, other | Jet / transport |
|---|---|---|
| up to 33 | A | B - held off; a jet aims at a firm arrival |
| 33 - 60 | A | A |
| 60 - 150 | B | A |
| 150 - 300 | C | A - the airline target is 100 to 250 |
| 300 - 367 | D | B |
| 367 - 433 | D | C |
| 433 - 500 | D | D |
| over 500 | F | F |

The names go with the letter - Butter, Smooth, Firm, Hard, Arrival - from
`passenger.GRADE_TABLE`, which was once the ladder for every aircraft and now
only names the letters and pins the light curve's boundaries. That one ladder
showed a jet's 200 fpm, the middle of the airline target, as a touchdown
score of 100 beside "Firm (C)".

**The light curve has to cross each boundary at the ladder's rate,** because
the letter is read off it. Its full-marks plateau to 60 fpm had taken the A/B
anchor, which cost nothing while the letter came from the ladder; reading the
letter off the curve then moved A to 105 fpm unnoticed, since a ramp from 100
at 60 fpm to 80 at 150 passes 90 at 105. The curve now steps to 89 just past
60 - not a hair under 90, because the page rounds a score to a whole number -
and `test_off_the_curve_the_letter_is_still_the_ladder` checks the letter
against the ladder at every rate from 0 to 700 fpm.

There is no E. Most grading scales people have seen do not have one, and a
five-band scale spent two of its bands on landings nobody walks away from
describing differently.

**The EFB tablet has its own copy of the light ladder** - `gradeFpm()`, since
it cannot call back into the watcher - and applies it to every aircraft. For
a light airplane or a helicopter it agrees with the logbook. For a jet it does
not: the tablet calls a 200 fpm landing C and the logbook calls it A. Teaching
the tablet the aircraft type is a change to verify in the sim, and has not
been made. The copies have drifted before, too: they were once 450 in one and
500 in the other. The score-to-letter ladder for phase grades
(`grading.LETTER_TABLE`) is served over `/grading`, so the logbook UI has no
copy of that one.

### Leg grading

The landing grade above judges one moment. A leg is also graded as a whole, in
`grading.py`, which owns every threshold in the system.

A leg is split into five phases by its altitude profile, each scored out of 100
and given a letter, and the leg grade is the weighted average. For the two
airplane profiles:

| Phase | Weight | Scored on |
|---|---|---|
| Lift-off | 10% | rotation 40, fore/aft g 30, steepest turn 30 |
| Climb | 20% | vertical g 40, steepest turn 35, fore/aft g 25 |
| Cruise | 25% | vertical g 40, fore/aft g 30, steepest turn 30 |
| Descent - the approach | 25% | approach g 31, descent angle 25, steepest turn 22, sink at 500 ft 22 |
| Landing | 20% | the touchdown, blended with the touchdown spot and the float when they are switched on; alignment and the landing limits can only lower it |

The helicopter profile grades lift-off on vertical g rather than rotation and
adds vortex ring exposure to its descent; the unclassified profile drops the
descent angle and the 500 ft gate, which depend on a fixed-wing figure.
`grading.describe()` - the How grading works panel - is the authority on the
current numbers; these were its numbers when this was written.

The leg grade is then **held to no more than one band above the worst phase**.
A lovely cruise does not cancel an alarming approach.

**The landing is a phase of its own** (`LANDING_PHASE`, on). The touchdown
used to do two jobs: it set the landing letter, and it was 45% of the
descent, so the Grading tab listed it twice and the descent mixed how the
approach was flown with how the wheels met the runway. Now the descent is the
approach and the landing is the fifth phase. The descent's 45% is split by the
touchdown's own share of it, 25 and 20, so with every measure present a leg
averages exactly as before; what changes is that the weakest-phase cap sees
the approach and the landing separately. Measured when it went on: the
averages were unchanged, no landing letter or ride grade moved, and a
minority of legs dropped a letter and none rose - an approach no longer hides
behind a good touchdown.

**The landing phase is a blend; the landing letter is not.** The letter is
how the touchdown felt. The phase is how the landing was flown, and the pilot
standards treat touching down near the aim point with little float as a main
criterion beside a smooth arrival - something a cap, which can only punish,
could never credit. So the phase blends the touchdown (50), the touchdown spot
(35) and the float (15): `LANDING_WEIGHTS`, a judgment and labelled as one,
since nothing published weights them against each other. The float carries
least because a long float and a long touchdown are mostly one mistake. Each
of the last two counts only while its Settings switch is on. Two limits keep
the blend honest:

- the phase never sits more than 10 points above the touchdown alone
  (`LANDING_LEAD_POINTS`), so precision can lift a firm landing a little and
  cannot rescue a hard one;
- a spot or float scoring under 50 - past the end of the touchdown zone, a
  fault the standards say to go around for - caps the phase at its own score
  plus one band as well as weighing in (`LANDING_FAULT_SCORE`). Without that,
  the blend let a soft touchdown well past the zone average up to a C.

**The ride is everything flown except the landing**, the approach included.
It once left out the whole descent, which made sense while the descent
carried the touchdown - and then the passenger paragraph called a ride smooth
beside an F approach. An audit of every paragraph against the grades beside
it found that on several legs before the change and on none after.

**A brisk climb-out is long enough to grade.** Airplane lift-off ran from
wheels-up to 400 ft, and a phase under 15 s is not graded. A light jet reaches
400 ft in roughly 14 to 21 s, so whether its departure was graded was a coin
toss - and backwards, since a phase bounded by height is shortest when it is
flown best. Lift-off for airplanes now ends at 400 ft or once it has lasted
`min_phase_s`, whichever is later, and never past the start of the climb.
Helicopters are unchanged.

Two design points, both from getting it wrong first:

**Rate of change, not total change.** An early cruise metric scored the spread
of altitude and speed over the phase, which punished climbing 560 ft to clear
terrain (0/100) and a smooth deliberate deceleration of 23 kt (10/100). Neither
is bad flying. Every metric now measures how fast something changed, not how
far it moved. Detrending alone was not enough - it still scored a 0.44 g chop
at 100/100, because the trend it removed was the chop.

**Every threshold was measured against real flights before being believed.**
A first attempt at phase splitting on vertical speed graded every leg F. A
climb metric on ground-speed standard deviation scored every leg 0, because the
median leg sat at 34 kt against a band maximum of 25. Numbers that produce the
same answer for every flight are not measuring anything.

The helicopter profile is calibrated against real recorded flights.
Nothing else is, and an uncalibrated grade says so on the grade itself - a
dashed pill with a "?" - rather than only in this panel. Vortex ring exposure
is a helicopter metric and is absent from every fixed-wing profile.

**A light airplane is not a small airliner.** Profiles use criteria appropriate
to the aircraft class. Repeated full marks are a prompt to inspect the inputs
and sources, not a reason to narrow bands to fit a personal logbook.

So there are two fixed-wing profiles, split at **VS0 61 kt** because that is
where the regulation splits - 14 CFR 23.49 caps VS0 at 61 knots for
single-engine airplanes and light twins - and VS0 is already recorded on every
flight. The transport numbers are unchanged; they were always right for
transports.

| | Light airplane | Jet / transport |
|---|---|---|
| Rotation | full marks 0.2 g, zero 0.4 g | 0.45 / 0.9 g |
| Steepest turn | full marks 20°, zero 45° | 20° / 35° |
| Sink at 500 ft | 400 / 1000 fpm | 500 / 1200 fpm |
| Descent angle | 3.5° / 7° | 3.5° / 9° |
| Touchdown | full marks to 60 fpm, zero at 600 | full marks 100 to 250 fpm, less either side, zero at 600 |
| Touchdown bank | full marks 4°, zero 14° | same - uncalibrated |
| Rollout slide | full marks 0.15 g, zero 0.55 g | same - uncalibrated |
| Float, touchdown spot | the published zones - see Where on the runway | same |

A steepest-turn band was once narrowed because the wide one gave full marks
to every flight in the logbook. Those flights had simply never banked hard
enough to test it, and the narrow band marked down a correct pattern turn; it
is wide again.

**The landing letter is not vertical speed alone any more.** A light airplane
arrived at the gentlest vertical speed of its set while rolling onto one main
and sliding for several seconds after it was down. It scored a B, and the
pilot said so. Touchdown vertical speed still sets the letter; alignment
can now hold it down, and can never lift it. That asymmetry is the point: a
gentle arrival that is still sliding sideways is not a good landing, while a
perfectly square arrival at 600 fpm is still an arrival.

Two measures, both already in the track and neither previously read:

- **bank** — peak roll from the contact sample through the rollout. Touching
  down banked is correct crosswind technique, so what this catches is bank
  still *growing* after the wheels are down — a dropped wing, not a held slip.
- **scrub** — mean peak-to-peak lateral acceleration per rollout second.

The letter may then sit **one band above the alignment score**, the same
allowance the leg rollup gives its weakest phase. Alignment caps the
**landing phase score** on the same terms (the descent's, with
`LANDING_PHASE` off), which is how it reaches the leg grade. The float and the
touchdown spot can hold the letter down the same way, when switched on.

**Alignment is listed with the landing metrics but carries no weight, and
that was measured rather than assumed.** Weighting it was the obvious design
and it is wrong, exactly:

```
delta descent = w x (alignment - touchdown)
```

Moving weight `w` off touchdown pays out the gap between rollout and
touchdown, rewarding the poorest touchdown most. Alignment therefore caps
the descent score instead of contributing positive weight.

**The contact second is excluded from the slide measure, and that was not the
first guess.** Peak lateral acceleration was, and it ranked the quietest
rollout of the set as the worst of them — its peak was a single sample at
contact, an impulse every landing has. The landing that actually slid peaked
lower and then *kept* scrubbing for several seconds. Averaging the rollout and
throwing away the contact impulse is what separates them.

**Uncalibrated alignment.** The transport bands currently match the light
airplane bands. Changing them requires an appropriate source and validation;
the private calibration record documents the available evidence.

**Helicopters are not scored on alignment at all.** Not because a helicopter
cannot land badly sideways — drift on touchdown is how dynamic rollover
starts — but because there is no data: the rotary tracks behind it carry no
lateral accelerations being recorded. Scoring them would award a flawless
alignment to landings nobody measured. A missing reading must never pass for
a good one.

**What moved.** One landing letter, and no other.

**The tablet does not know about this.** The in-sim EFB grades from
`PLANE TOUCHDOWN NORMAL VELOCITY` alone, so it will still show B for a landing
the logbook holds at C - and it uses the light ladder for every aircraft (see
Landing grade). That is a real divergence and it is here rather than
hidden: the tablet is a live instrument reading one simvar at the moment of
contact, and the logbook is the record, which can look at the five seconds
afterwards. Making them agree means teaching the EFB to sample the rollout.


**Every phase cites the standard behind its numbers, in the panel.** Once per
phase rather than once per metric - the phase is the smallest unit where the
answer is the same for everything in it, and a citation on every row would be
noise rather than provenance. So the light-airplane descent section names GA
stabilized-approach guidance for the angle and the 500 ft gate, and 14 CFR
23.473 for the touchdown ladder; the transport descent names FSF ALAR Briefing
Note 7.1 and the industry hard-landing triggers; the helicopter phases say
plainly that they were calibrated against real helicopter flights and not
against any published standard. The g bands carry their own note saying they
come from neither.

That means a reader who doubts a number can go and find the document it came
out of, rather than taking the tool's word for it - which matters more for the
profiles that are *not* calibrated here, since a published criterion is the
only thing standing behind them.

Re-sourcing those three took the light-airplane profile from **five saturated
metrics to two**. The two left - fore/aft g in climb and lift-off - were
deliberately not touched. A light airplane measured 0.02 to 0.06 g on every
axis where a helicopter spread 0.02 to 0.22, and that is a Cessna being
genuinely smoother than an AS365, not a threshold from the wrong class of
aircraft. Narrowing them to manufacture a spread would be tuning grades to look
busier, which is the opposite of measuring.

### Which profile a flight is graded on

The sim is asked, rather than the aircraft name being pattern-matched:

1. `CATEGORY` - the sim's own word for the aircraft: `Helicopter`, `Airplane`.
2. `DESIGN SPEED VS0` - the stall speed, because `CATEGORY` alone is not
   enough. A Magni M24 **gyroplane reports itself as `Airplane`**, with a stall
   alpha and a wing area like any other, and is separated only by stall speed:
   40 kt for a C172, 10 kt for the gyro. Zero on a helicopter.
3. Under 25 kt with `Airplane` falls to a third profile that grades on the
   airplane numbers minus the two measures that depend on a fixed-wing figure -
   descent angle and the 500 ft gate - rather than judging an aircraft against
   numbers that do not apply to it.

A leg whose track carries no `CATEGORY` falls to `infer_category()`, which
works it out from the flying: a helicopter hovers and an airplane cannot, so
the longest airborne stretch below 15 kt separates them. A helicopter leg
spends the better part of a minute below that speed, against a threshold of
10 s, so the two do not sit close together.

Note that `AddToDataDefinition` accepting a variable proves nothing - it
accepted `STALL ALPHA` on a helicopter. Only the values discriminate, so every
one of these was checked against three aircraft in a running sim.

### Turning ratings or notes off

Both are switches per flight, independent of each other, and both default on.
The default is itself a setting, so someone who never wants either says so
once instead of per flight.

They gate what the builder **emits**, never what was recorded. Grades and prose
are both derived from the track, and passenger variants are picked from a hash
of sortie id plus leg rather than a counter, so switching back on regenerates
exactly what was there before — verified by capturing ten grade pills and three
notes, switching off, switching on, and comparing. The landing rate stays
visible when ratings are off: that is a measurement, not an opinion.

The same pair of switches appears in two places: an in-flight bar while a
flight is recording, and each flight's own footer afterwards. Both write the
same store, keyed by sortie, so a choice made in the air is already the one the
builder reads on landing.

Only explicit choices are stored. A flight set back to the defaults is
forgotten rather than written as a row saying "same as default" — otherwise the
file grows an entry per flight ever looked at, and a later change of default
would silently skip them.

**Where the switches go in the build signature matters.** Shared preference
files must not invalidate every sortie when one switch changes. Each sortie
signs its resolved switches, which also covers changes in defaults.

A switch change rebuilds the index alone. It cannot change a map, and asking
for maps meant the rebuild was deferred behind an in-progress flight and the
logbook sat stale until the aircraft was parked.

### Passenger text

Templates only - no LLM, no network. Four slots (opening, ride, landing,
closer). The variant is picked from a hash of `sortie_id + leg`, so the same leg
never rewrites itself between rebuilds.

Pool sizes: openings 24 per leg length (short / medium / long, so the
phrasing fits the length; 12 when the length is unknown), rides 28 per mood,
landings 26 **per letter**, closers 22 to 26 per tone - 69,888 to 454,272
distinct paragraphs. `py -3 passenger.py` prints the current counts.

Details that matter more than pool size:

- **Nearby legs never repeat a line.** The hash is uniform, but a pool still
  draws the same line for two legs in a row now and then - and they sit next
  to each other on screen, which is where a repeat is most obvious. Each leg
  is passed the picks of the last six legs, per slot, and steps off them.
- **The landing line says why a letter was lowered.** The word for the letter
  comes from the letter, not the rate, so a gentle touchdown held down for
  something else is not described in the words of the grade it was pulled to.
  Three pools exist for exactly that - `held` (it slid), `held_float` and
  `held_long` - chosen by what last lowered it, and none of them may call the
  touchdown firm. The self-test enforces both.
- **The ride is the flown phases except the landing** (see Leg grading), so it
  cannot call a ride smooth beside a weak approach.
- **`a` vs `an`.** Eight, eleven and eighteen start with a vowel sound, so
  "a 11-minute leg" is fixed up after formatting.

## Where on the runway

**The touchdown spot and the float** are measured on every airplane landing
on a runway the sim has described, and shown on the leg. Each counts toward
the landing only while its Settings switch is on (`GRADE_TOUCHDOWN_POINT`,
`GRADE_FLOAT`, both off by default). Helicopters are never measured on
either, and the watcher asks the sim for no runway after one lands.

- **Touchdown spot:** distance past the landing threshold - the displaced one
  where there is one. 100 within 1,400 ft (AC 91-79A's target touchdown
  point, about 1,000 ft down, plus the Private Pilot ACS tolerance of 400 ft),
  50 at the end of the touchdown zone (3,000 ft, or the first third of a
  shorter runway), 0 a further 1,000 ft on - a judgment, labelled as one - and
  0 more than 50 ft short of the threshold.
- **Float:** the time from 50 ft above the runway to main-gear touchdown, the
  air distance of AC 25-32, timed from the 10 Hz landing clip. 100 for a 7 s
  flare (AC 25-32's average pilot in normal operations), 50 at the end of the
  touchdown zone, 0 a further 1,000 ft on, with distances turned into seconds
  at the speed actually flown so one rule serves a trainer and a jet. A first
  version sloped to zero at the end of the zone and failed a jet touching down
  inside it, which AC 91-79A calls typical; the test that pins the zones is
  that version, kept. **The clock starts at the later of 50 ft and the
  threshold:** the AC assumes 50 ft over the threshold, and an arrival
  crossing lower had its approach counted as float. So the float needs the
  runway too.

Both bands are settings, defaulting to the published figures
(`grading.PUBLISHED`). The score, the band text and the letter cap read the
same values when called, so a setting moves all three together, and the
build signs the values (`runway_tunables`) because a setting changes them
without changing `grading.py`. The letter ladder is not a setting.

**The runway comes from the sim,** the only authority on its own scenery, and
nothing about what it sends was taken from the SDK on trust: the message
layout was measured against a running sim first, and the facility-data trap
in AGENTS.md records it. Once parked after a landing - never in flight - the
watcher opens a short-lived connection of its own, asks for the airport list
and the runways of every airport within 3 nm of the touchdown, and caches them
under `sessions/runways/`. Read-only throughout. Every recorded airplane
touchdown located on a runway centreline to within 10 ft. A sortie signs the
runway files near its own landings, so a runway cached later rebuilds that
sortie and nothing else.

**A landing's runway is known when a cached runway contains the touchdown,**
not when a cached airport is near it. A heliport cached 2.5 nm from a landing
once made that landing count as covered, and the airport it was really at was
never asked about.

**Heights are above the runway's surface.** The sim gives a runway one
elevation, and runways are not flat: on one climbing 2%, a 30 ft threshold
crossing measured above the touchdown point read 10 ft. The threshold
crossing and the float's 50 ft point are now read from the aircraft itself -
altitude less PLANE ALT ABOVE GROUND while it was over the runway
(`grading.RunwaySurface`) - from this landing's readings only: a while before
touchdown to shortly after, between the threshold and the far end, within 250
ft of the centreline. Without those limits the departure and the downwind
count as runway, and a first version put a crossing at 2,390 ft. Ground
before the threshold is never used, so a runway on a mesa is not moved down
by the drop off its end. The height over the threshold is shown as
information and graded nowhere.

---

## Maps

Baked with Pillow after the sortie, never during the flight. Each
leg and each sortie gets a PNG under `sessions/maps/`, drawn over an
OpenStreetMap basemap, with tiles cached on disk so a rebuild does not fetch
them again.

Maps are **supersampled 2x** (`mapbake.SUPERSAMPLE`): a sortie map is baked at
2800x1240 and a leg map at 2000x860. That pulls the basemap from one zoom level
deeper, so clicking a map opens something you can actually zoom into rather than
a blurry upscale. Zoom ceiling is 17, enough to read taxiways and helipads.

### Canvas and style

`choose_canvas()` converts the bounding box to nautical miles first — so a
degree of longitude counts for less the further north you are — then follows
the track's own aspect where it can and **clamps it into a landscape range**.
Width is always the long side.

Fitting the canvas to the track was tried first and was worse. A north-south
flight produced a portrait map, which then hit `max-height: 70vh` and was
letterboxed inside the UI's full-width slot — the empty bars either side were
the thing being fixed. Clamping costs nothing, because the basemap window is
derived from the canvas: widening it fetches more tiles, so the extra space
fills with real map and more context rather than blank canvas.

| Flag | Default | Effect |
|---|---|---|
| `MAP_LONG_SIDE_LEG` | 1280 | Logical long side for a leg; ×`SUPERSAMPLE` = 2560 px. |
| `MAP_LONG_SIDE_SORTIE` | 1400 | Same for the sortie overview → 2800 px. |
| `MAP_ASPECT_MIN` | 1.80 | Tallest allowed, e.g. 1280×711. |
| `MAP_ASPECT_MAX` | 2.40 | Widest allowed, e.g. 1280×533. Also the shape used when a track has under two usable points. |
| `MAP_STYLE` | `light` | `light` keeps OSM near as-published; `dark` is the old dimmed look that suits the dark UI but washes out terrain. |

`MAP_STYLES` holds the dim/desaturate values and the ink, card and halo colors
for each style, so a new style is one dict entry rather than scattered edits.

### Basemap source

`tiles.TILE_SOURCE` picks which basemap the bake draws on:

| Source | Good for | Max zoom |
|---|---|---|
| `topo` (default) | OpenTopoMap — contours and hill shading, which is what mountain and canyon flying wants | 16 |
| `osm` | OpenStreetMap standard — airports, taxiways, street detail; terrain reads flat | 17 |

`TILE_SOURCES` carries each source's URL, subdomains, zoom ceiling and
attribution string, so adding one is a dict entry.

Two things that have to stay true when adding a source. **The cache is one
directory per source** (`sessions/tilecache/<source>/`) — the same z/x/y means a
different picture, so a shared cache would serve the wrong basemap. And the
**failure set is keyed by source**, so a missing tile on one does not suppress
the other.

OpenTopoMap is a small volunteer service with a tighter usage policy than OSM's.
It gets a lower zoom ceiling, the same fetch-only-when-parked gate, the same
polite `User-Agent` and inter-request gap, and tiles are cached for ever — an
area is fetched once. Its attribution requires crediting the style as well as
the data, which `attribution()` returns per source.

Each map carries a legend card (id, aircraft, distance, airborne time) and
labeled `START` / `LANDING` markers. The card is placed in whichever corner the
track uses least, and the compass moves to the opposite corner — a fixed
top-left card buried the START marker on any leg that began up there. Unnamed
pads get a bare `START` rather than a coordinate string, since the coordinates
only repeat what the map already shows.

**An index-only rebuild must not throw away maps.** It produces a doc with no
map fields, and letting that overwrite the cached doc discarded PNGs that were
still on disk, made the UI report that no maps existed, and left the next full
build to bake them all again. `carry_maps_forward()` keeps the previous doc's
map references when this run is not baking, checking the file is still there
first. `maps_baked` in `logbook.json` now means "maps are available", not "this
run baked them" — reporting the latter is how the UI came to claim Pillow was
missing during a flight.

**Bump `BUILDER_VERSION` when mapbake's drawing logic changes.** Canvas, style,
tile source and supersample all have their own signature entries; the drawing
logic itself has none, so a change to marker placement or segment splitting
would otherwise leave every cached sortie valid and its old PNG on disk. That is
what the version is for, and it is the reason `bake_maps` does not belong in the
signature.

**What must NOT be in `global_signature`.** `events.jsonl` is one file for
every flight, so putting its signature there meant a single landing invalidated
every sortie in the book — measured at 5 of 5 cache misses, a 15 s rebake of
every PNG, and twice over once an index-only pass ran first. Events are scoped
into the sortie that owns them via `sortie_signature`. `bake_maps` was there
too, which made index-only and full builds disagree by construction so neither
could reuse the other's work; whether a cached doc still owes maps is answered
by looking at the doc (`maps_missing`). After the fix a landing reuses 4 of 5
and takes 0.4 s.

**Bake settings are part of `global_signature`.** Without that, changing the
canvas or style leaves every cached sortie valid and the old picture on disk
for ever — the same trap that kept blank basemaps alive.

**Tiles are only fetched while the aircraft is not moving.** The watcher passes
`allow_network=False` to the builder while the aircraft is actually flying, so a
mid-sortie rebuild uses only tiles already on disk. Tiles are cached forever
under `sessions/tilecache/` (~16 KB each), so an area is fetched once. Roughly
10–25 tiles per map.

The test is motion, not whether a flight record is open. It used to be the
latter, and that was a bug: a record stays open for as long as the sim is
running, so with MSFS up the network was *never* allowed, the tile cache never
filled, and the "filled in on a later rebuild" promise never came true — every
map baked plain. Parked counts as not flying, which is consistent with the
rebuild itself, since that only runs after `LOGBOOK_PARKED_SEC` parked and
costs far more than a handful of tile fetches.

A sortie whose maps were baked with no basemap is re-baked the first time the
network is available again. Its signature alone would never invalidate it —
nothing in the recording changed — so `maps_missing_basemap()` is checked
alongside the cache hit.

Degrades cleanly, in this order:

1. basemap + track (normal)
2. plain graticule + track — no tiles cached and no network, which is exactly
   the polyline-only v1
3. in-page canvas polyline — no Pillow at all

`logbook.json` also carries a downsampled polyline, so the page can always draw
the track itself with zero requests. It is the fallback if a PNG is missing.

Cost for the current tree: 237 tiles / 2.9 MB cached, 3.5 MB of maps. A cold
bake at 2x took 25 s; warm, 2.4 s. It runs post-flight on a Below Normal
background thread.

Force an offline rebuild:

```bash
py -3 logbook_build.py --offline
```

Attribution is drawn onto every basemap image, as the OSM tile policy
requires, and requests carry an identifying User-Agent.

A track whose bounding box is under 0.05 nm is not drawn: that is a parked
aircraft's GPS jitter, and autoscaling it produces a convincing-looking flight
path that never happened.

### A sim skip is not a landing and a takeoff

The route map splits a track wherever two consecutive samples are further
apart than flying could account for, so it does not draw a straight line
across a reposition - and each piece gets its own start and end marker. A sim
that skips ahead mid-flight, after a sim-rate change or a stutter it catches
up on, jumps forward along its own route, and the map drew a landing and a
start a third of the way through a flight that did neither. A jump is now
joined when it was plainly flown across: airborne both sides, no slew,
altitude within 500 ft, under 25 nm, and within 15 degrees of the ground
track of the step before - ground track rather than heading, because at
altitude the two differ by the wind correction angle. Anything that cannot be
checked splits, as before. `test_map.py` covers it.

### Naming places

Optional and offline. Create `places.json`:

```json
[
  { "name": "Home strip",   "lat": 40.0000, "lon": -105.0000, "radius_nm": 3 },
  { "name": "The lake",     "lat": 40.2500, "lon": -105.5000, "radius_nm": 3 }
]
```

Endpoints inside `radius_nm` get named; everything else shows coordinates.

---

## HTTP

Localhost only.

| Method | Path | |
|---|---|---|
| GET | `/` `/logbook.html` `/logbook.json` `/logbook.js` `/replay.js` | the UI |
| GET | `/sessions/maps/*.png` `/sessions/months/*.json` `/sessions/detail/*.json` | maps, month files, flight detail |
| GET | `/state` `/current` `/last_event` | `/state` shape is frozen (EFB) |
| GET | `/clips` `/clips/<id>` | |
| GET | `/grading` | How grading works, from `grading.describe()` |
| GET | `/settings` | settings, with their defaults |
| GET | `/export` | a track as KML or GPX |
| POST | `/replay` `/replay/stop` `/replay/pause` `/replay/chase` `/replay/lock` `/replay/recenter` | |
| POST | `/settings` | save settings |
| POST | `/logbook/rebuild` | |
| POST | `/logbook/hide` `/logbook/restore` | remove from the logbook, or put back |
| POST | `/logbook/purge` | delete for good; with `dry_run`, the default, only say what would go |
| POST | `/logbook/prefs` | the per-flight rating and passenger switches |

Static serving is allowlisted: only those routes and `sessions/` with an
approved extension. `watcher.py`, `watcher.log`, `*.jsonl`, the DLL, dotfiles
and `..` traversal are all refused.

**Loopback is not a caller check.** Binding to 127.0.0.1 says where a request
comes from, and any web page open in a browser can address 127.0.0.1. Every
response used to grant every origin every method, and nothing checked a
command's Origin, Host or content type, so a page could hide, delete, change
settings or start a replay. Now, before a request is dispatched
(`caller_refusal`):

- **Host** must be `127.0.0.1` or `localhost` at this port, for reads as well
  as commands - the DNS-rebinding guard.
- **A command** - every POST, the bodyless Stop and Rebuild included - must
  carry no Origin or the page's own, and must be JSON. A browser cannot send
  a cross-site JSON POST without a preflight, and a preflight is granted
  nothing. `Origin: null` is refused: it is what any sandboxed document sends.
- **Only `GET /state` is shared** with other origins, because the EFB tablet
  polls it from inside the sim under an origin not known here. It carries the
  current flight - position, ids, recent events, replay state - so it is not
  private from other pages. That is an interim choice; restricting it starts
  with observing the EFB's real origin across reloads, not with trusting one
  value.

This keeps browser pages out. It does not identify the tray: the tray posts
JSON with no Origin, and so could any other local program. Telling local
programs apart would need authentication, which this is not. The page's own
scripts are same-origin and relative for this reason - `replay.js` used an
absolute 127.0.0.1, which made every replay control a cross-origin request
when the page was opened as `localhost`. Refusals are logged once a minute per
reason. `test_http.py` drives the real handler.

---

## Flight performance

Acted on the audit of 31 Aug 2026. Measured steady state before any of it was
already small - 0.16% of one core, 54 MB, ~1 KB/s - so these target the spikes
and the growth curve, not the baseline.

| Change | Effect |
|---|---|
| Heavy rebuilds never run during a flight | Removes a 2.4 s CPU spike at 81% of a core that used to land ~75 s after every takeoff. Held requests run at flight end, or after the aircraft has been parked 60 s. An explicit **Rebuild** also waits while airborne or moving. An index-only refresh does run mid-flight, so the list stays current; it reuses every unchanged sortie and logs its own time each run. |
| Per-sortie signature cache | A sortie whose flight records have not changed is reused whole: no track re-read, no map re-render. Warm rebuild went **2.98 s -> 0.03 s**. Stops the cost growing with the size of the logbook. |
| Per-flight meta in that signature | The track read was cached; the flight's `meta.json` was read anyway, for every flight, on every rebuild - the one term left that grew with the whole logbook rather than with what had just been flown. A read is 105 us against 39 for a stat, so at ~1,900 flight records it was 200 ms a rebuild spent re-learning what had not changed. Now 1 read instead of 24 on a warm build, and the one still read is the flight being recorded. |
| Sampler throttled to every 2nd sim frame | ~21 Hz delivered against a 10 Hz consumer. The sim marshals less than half as much data for us to discard. |
| fsync policy | In-progress clip writes, `current.json` and in-flight `meta.json` are no longer synced to physical disk; the write at clip close still is. fsync measured 1.0-1.8 ms per call. |
| Clip write cadence 1 s -> 5 s | A clip is appended, never rewritten (`clipfile.py`): each checkpoint appends the points since the last. A crash loses at most 5 s of an open clip. |
| Build cache read once | It was read three times and written twice per build, indented and fsynced, whether or not anything had changed - 80% of a rebuild that changed nothing. Now read once, written only when its text changed, and compact: a no-change rebuild 0.18 -> 0.07 s and a 1.4 -> 0.6 MB cache file on the install's data, 2.67 -> 1.11 s on a logbook sixteen times that size. Still linear in the logbook. |
| `/state` served from memory | The EFB polls it every 2 s from inside the sim. `current`/`last_event` come from RUNTIME; `notify_state.json` is written by the agent so it is re-read only when its mtime changes. |
| Log rotation | Rotates at 4 MB, keeps 2 files. |

### Capture is now 10 Hz

`SAMPLE_SEC` is `0.1`. That constant was the only thing holding capture down
once sampling became push-based - measured **9.96 Hz** sustained, double the
telemetry density behind every replay. The 60 s ring buffer grows to ~616
samples, which is nothing.

## Taking a track somewhere else

The baked PNGs answer "where did I go". Opening the same track over real
terrain wants a 3D viewer and a basemap this project is not going to draw, so
each leg and each flight offers **Google Earth (KML)** and **GPX**, from
`/export`.

Built on demand rather than baked like the maps: nothing on disk to go stale,
nothing to invalidate. It reads the raw session track, not the logbook's stored
polyline - that one is decimated to a few dozen lat/lon pairs with no altitude,
which is exactly what a 3D view needs and has not got. Long tracks are thinned
to `EXPORT_MAX_POINTS` (3000) with the endpoints kept; a 6155-point sortie
comes out as 86 KB of KML.

Three constraints shaped this, and they are worth knowing before changing it.

**Google Maps cannot draw a flight path**, and the first attempt at working
around that was worse than nothing. Its URL API carries a pin, or road-snapped
directions capped around ten waypoints, and there is no way to hand it a
polyline or even a second marker. An "Area in Maps" link was shipped using
`map_action=map&center=`, which Google's own documentation describes as
returning "a map with no markers or directions" - so it opened a map of
nowhere, with no track, no endpoints, and nothing to look at. It was removed.

What replaced it is smaller and actually answers a question the logbook cannot:
each end of a leg's route is a link to a **dropped pin** at that point, using
the `maps/search/?api=1&query=lat,lon` form, which does show a marker. A leg
that ends at a bare coordinate is a field, a strip or somebody's pasture,
and only a real map knows which - and this logbook shows raw coordinates
whenever `places.json` has no name for a point. The link lives on the
coordinates themselves rather than in a button somewhere else. The flight path
goes to KML, which is the only thing that can draw it.

**Google Earth on the web cannot open a KML from a URL** - import is a local
file or Google Drive only. So this is a download, not a deep link. With Google
Earth Pro installed and `.kml` associated, that is a double-click; in the
browser it is Projects, Import KML file. Worth saying in the UI rather than
assuming, since not everyone will have Pro.

**The altitude datum is a real choice, not a detail.** `alt` is MSL as the sim
recorded it, but MSFS terrain and Google Earth terrain do not agree, so an
`absolute` path can visibly clip a hillside or float above a ridge in
mountainous country - which reads as a bug in the export rather than a
difference of elevation data. `ALTITUDE_MODE` can be `relativeToGround`, which
uses the recorded AGL and hugs whatever terrain the viewer has, at the cost of
no longer being the altitude actually flown. Absolute is the default because it
is the honest one. A track may carry no AGL, so
asking for `relativeToGround` on one falls back to absolute rather than laying
the whole track on the deck.

KML is deliberately not treated as a Google feature. The same file opens in
Google Earth, and the GPX in ForeFlight, SkyDemon, QGIS and most
flight-tracking tools; exporting the track is worth more than linking to one
site, and it keeps working if Google changes something.

## Keeping the watcher alive

The watcher is a background process with no window. On 3 Sep 2026 one stopped
logging at 22:55:44, never noticed the sim close - no `session ended
reason=quit` line, which the reconnect loop would have written - and was killed
by Windows as unresponsive 46 minutes later. Windows recorded it as
`AppHangB1`, hang type Quiesce, on the watcher's own PID. There was no
shutdown, sleep or restart in that window, so the machine was up the whole
time. Nothing brought it back until it was restarted by hand the next morning.

Two things were wrong, and neither is the hang itself.

**Liveness was measured on the wrong thing.** The tray asked whether the
watcher answered on its port. The HTTP server is a separate thread, so a
watcher whose detect loop has stopped still answers while recording nothing.
The detect loop now stamps a heartbeat every pass, `/state` reports
`heartbeat_age_s`, and that is what is judged. With no sim to talk to the loop
turns every `RECONNECT_SEC`, so the age sawtooths to about 5 s - measured -
well under the 120 s the tray treats as wedged.

**Nothing restarted it.** The tray polled every 3 s and drew the "down" icon,
which is how it looked all night. It now restarts a watcher that is not
answering or whose heartbeat has stopped, on a widening backoff (10 s, 30 s,
2 min, 10 min) so a watcher that cannot start is not relaunched for ever, and
only counts a restart as successful once the new one actually answers.
Measured: killed the watcher, the tray had it back in 6 s.

**A watcher also runs the code it started with.** `/state` reports
`code_stale` when any module it imported is newer on disk than the process,
and the logbook shows it beside the version in the warning color. The scope is
the imported Python modules and nothing else: the flag says "restart the
watcher", not "the working tree has changed". `logbook.html` and `logbook.js`
are served from disk on every request, so an edit to either needs a browser
reload and no restart, and the flag stays false for them - correctly, though an
operator expecting it to track any repo change will read that as a miss.
Comparing
`/state` to `git describe` is the wrong test for this: a commit moves the
description without moving a byte the process is executing, while the case
that bites - running code that no longer exists on disk - can be true with the
two strings agreeing. A stale watcher once overwrote a new-format
`logbook.json` with the old shape and the UI rendered an empty page with no
explanation.

The watcher also logs `WEDGED: detect loop has not turned for Ns`. It cannot
unwedge itself - whatever is blocking holds that thread - but the next
occurrence will leave evidence rather than an empty log.

**What is still unknown:** where it blocked. There is no stack from the hang
and no reproduction, so the specific blocking call has not been identified.
The recovery above is deliberately independent of the cause.

## Logbook order

The browse list has a **Newest first / Oldest first** control next to the
filters. The choice is remembered per browser.

The UI sorts the list itself rather than trusting the order in `logbook.json`,
so a stale index cannot flip the logbook around underneath you, and it parses
`started_at` rather than comparing the ISO strings as text — those carry a local
UTC offset, so across a DST change two flights an hour apart would otherwise
sort by their literal digits.

The builder still emits newest-first, and that ordering had a bug worth
remembering: `started_at_unix` was set when a sortie was built, used as the sort
key, then popped off — but the object popped was the same one held in the sortie
cache. The key survived exactly one build. Every later build reused cached docs,
scored them all `0.0`, and the descending sort collapsed into insertion order,
which is oldest-first. `sortie_order_key()` now falls back through values that
are actually persisted: `started_at`, then the sortie id, which is a UTC stamp
that sorts chronologically as text.

## Clip windows

60 s per clip, weighted the way each event actually reads:

| Clip | Before | After |
|---|---|---|
| Takeoff | 5 s | 55 s |
| Landing | 55 s | 5 s |

The ring buffer holds **60 s** at 5 Hz (316 samples) - it has to cover the
landing's 55 s lookback, or the start of the approach has already fallen out of
the buffer by the time the touchdown is detected. This widens the doc's 45 s
buffer in section 6.

Clips already on disk keep the length they were recorded at (the old 25 s / 30 s
windows); the new windows apply to everything recorded from now on.

### The ghost used to jump at the touchdown

Every clip had a ~2.5 s gap starting at exactly `t0 + 0.00`, so the ghost
skipped forward right at the event - worst at a landing, where the touchdown
itself was the missing part.

Cause: the tracker waits `BOUNCE_SEC` (2 s, ~2.5 s with loop granularity) before
committing a transition, to be sure a touchdown is not a bounce. The commit then
described the *original* transition sample, but `_window` cut the buffered
prefix at `t0 + 0.05` and live capture only started at commit time. The seconds
spanning the event were sitting in the ring buffer and were thrown away.

`_window` now runs from `t0 - before` through the present, with a monotonic
guard in `add_sample` so the prefix and live capture cannot overlap. Clips
without it keep their gap.

### Sample rate: ~2.6 Hz, not the intended 5 Hz

*History: the push sampler below replaced this, and capture now runs at
10 Hz.*

Measured against the live sim, every `aq.get()` costs a flat ~50 ms - that is the
python-SimConnect polling tick, not sim latency - and `sample()` makes one call
per variable:

```
full sample() cost: 0.649 s  -> 1.54 Hz ceiling   (13 vars x ~50 ms)
```

The `_time=200` cache softens this to a measured ~0.38 s per clip sample
(~2.6 Hz), which is why clips carry roughly half the points section 6 assumes.
`PLANE_HEADING_DEGREES_MAGNETIC` was being fetched and never read, and is gone.

The real fix is one SimConnect data definition fetched as a single struct
(`RequestDataOnSimObject`) instead of 13 blocking gets, which would reach 5 Hz
comfortably. That is a rewrite of the sampling core - the part everything else
depends on - so it has not been done.

## Playback smoothness

Five causes, each measured rather than guessed. They were found in this
order, and the first three were not the one that mattered most:

**The replay loop ran at 16 Hz, not 20.** `threading.Event.wait()` on Windows is
quantized to the 15.625 ms timer tick, so asking it for 50 ms actually waits
62.3 ms:

```
time.sleep(0.05)   mean  50.3 ms
Event.wait(0.05)   mean  62.3 ms     <- what the loop was using
```

Pacing now uses `time.sleep` in short slices (stop and pause still respond in
under 10 ms). Driving the ghost costs ~0.01 ms a call, since
`SetDataOnSimObject` and `CameraSet` are both fire-and-forget, so the rate was
never limited by the sim. `REPLAY_HZ` is now **180**, which divides evenly into
both 60 and 90 Hz displays; `CHASE_HZ` equals it deliberately (see below).

**180 is the rate asked for, not a rate proven on this machine.** The deadline
grid below fixed the drift that made 90 Hz deliver 86, but nothing fails if the
loop cannot hold 180 on a given PC - `REPLAY_RATE_LOG` is off by default and
there is no test gating it. Turn it on to see what a run actually achieved.

**Interpolation was linear.** With samples ~0.35 s apart, linear interpolation
makes velocity piecewise-constant and the ghost changes direction at every
segment boundary. Position, attitude and heading now use a Catmull-Rom spline
through the neighboring samples, with heading unwrapped first so it can cross
360 degrees. p99 heading jerk drops ~2.9x.

The spline passes exactly through every recorded sample (verified: 0.0000 m
deviation), so it smooths the path between points without inventing positions.
It falls back to linear wherever the neighboring samples are unevenly spaced,
since a uniform Catmull-Rom would overshoot there.

**The camera rate cannot be raised on its own.** Terrain is world-fixed, so
all of its apparent motion comes from the camera, and raising `CHASE_HZ` alone
while leaving the tuned ghost rate untouched looks like the obvious fix. Tried,
and it made things worse: the ghost and the camera have to step together for
their errors to cancel. Both were raised instead, which is why `CHASE_HZ =
REPLAY_HZ` carries a comment saying it must normally stay that way.

**The deadline grid drifted.** Re-basing each deadline on the iteration's own
start time never gives back the sleep overshoot, so it compounds: 11.11 ms
asked plus ~0.5 ms of overshoot delivered **86 Hz instead of 90**. Against a
90 Hz headset that beats about four times a second, which reads as a small
regular skip. Deadlines now run on a fixed grid from the start of playback,
and a loop that falls behind resyncs rather than firing a burst.

**And the one that actually mattered: samples were timestamped when we got
round to shaping them, not when the sim produced them.** States are pushed on
sim frames and read on a slower poll, so `time.time()` at shaping is 0 to 33 ms
later than the position it is labeling, by a random amount - and that error is
written straight into the track as position noise. Measured at up to **1.7 m
per sample at 50 m/s**: the step between recorded points varied from 0.52x to
1.35x of the distance the sim's own ground speed says was covered, while the
timestamps themselves were regular to 0.1%. No interpolation scheme and no
update rate can remove that, because the data is wrong before playback sees
it. The fix is one line - the sampler reports the arrival time and the track
uses it - and it is what made replays smooth.

**The snap at touchdown was a separate thing again.** Even on a gap-free clip
the ghost popped downward the instant it landed. The ghost is driven with
`SIMCONNECT_DATA_INITPOSITION`, which carries an `OnGround` field, and the
watcher was setting it from the recorded sample. With `OnGround = 1` the sim
**ignores the Altitude field** and clamps the object to terrain. Reading the
ghost's real altitude back while driving it through a recorded touchdown:

```
idx  recorded   OnGround from data   OnGround forced 0
154   5045.9    5045.9  (+0.0)       5045.9  (+0.0)
155   5045.4    5043.9  (-1.5) <-td  5045.4  (+0.0)
156   5045.4    5043.9  (stuck)      5045.4  (+0.0)
157   5045.4    5043.9  (stuck)      5045.4  (+0.0)
```

An instantaneous 1.5 ft drop at exactly the touchdown frame, after which the
altitude is pinned to terrain and the recorded settling is lost. Playback now
forces `OnGround = 0`; spawning still uses the recorded flag so an aircraft that
starts on a runway is placed on it properly. Verified through the shipped code
path: 0.0 ft error across the touchdown.

Note the clip filenames use the watcher's internal leg counter while the UI
renumbers per sortie, so on-screen "Leg 3" can be `...-leg4-*` on disk.

### The sampling core: push instead of poll

`sampler.py` replaces the 13 blocking `aq.get()` calls with the way SimConnect
is meant to be used - one data definition, one `RequestDataOnSimObject`
subscription, and the sim pushes values into the dispatch thread. Reading a
sample then costs a dict lookup, so the loop rate stops depending on SimConnect
round trips.

**Validated against a live sim on 31 Aug 2026** (see the table below); the
warning that follows is kept because it is why the mode switch exists. Every wrong assumption in
this project was settled by measurement, and this one has the same hazards:
SimConnect returns radians unless the unit says otherwise, the payload offset
has to be right, and a wrong sampler silently corrupts recordings of real
flights. So it ships behind `SAMPLER_MODE` in `watcher.py`:

| Mode | Behavior |
|---|---|
| `legacy` | The old path. |
| **`shadow`** (default) | Legacy still drives recording. The push sampler runs alongside and its values are compared every 20 s; agreement or mismatch is logged. Cannot affect recordings. |
| `fast` | The push sampler drives recording, falling back to legacy for any tick with no fresh push. |

The comparison is tuned to catch mistakes rather than timing noise - verified
offline that it flags radians-instead-of-degrees, a shifted struct, and an
inverted `on_ground`, while identical samples report clean.

### Validation result

Read side by side against the legacy path with the aircraft loaded:

| field | legacy | push |
|---|---|---|
| lat / lon | 40.2796 / -112.0156 | identical to 4 dp |
| **heading** | 359.59 | **359.59** (radians cannot exceed 6.29, so this is degrees) |
| pitch / bank | 2.32 / 0.04 | identical |
| on_ground, title | True, AS365 N2 - VIP | identical |

Rates: **42.4 Hz pushed** against a legacy ceiling of **1.67 Hz**, and
`latest()` costs nothing measurable. Shadow mode also logged `all fields agree`
across ~10,000 pushes before the switch.

`SAMPLER_MODE` is now `fast`. The detect loop measures **4.99 Hz** sustained -
the 5 Hz that section 6 specifies, up from the 2.84 Hz the clips were actually
recording at. `/state` reports `sampler.mode` and `sampler.sample_hz`, so the
rate can be checked at any time without flying.

**On the native connection - the default since 0.7.0 - there is no legacy
path.** These modes apply only with `SIM_CONNECTION = "python-simconnect"`.
Natively the push sampler is the connection, and a tick with no fresh push
holds the last reading (see Running without a Python install).

**If the old connection ever needs backing out to:** set `SAMPLER_MODE =
"legacy"` in `watcher.py` with that connection, and restart. `shadow`
re-enables the comparison logging.

**Originally, to adopt it:** fly one leg with `shadow`, then check the log:

```bash
grep "sampler shadow" watcher.log
```

`sampler shadow ok` lines mean the two paths agree; set `SAMPLER_MODE = "fast"`
and restart. Any `MISMATCH` line names the field and both values - send it over
rather than switching.

`sampler.py` has an offline self-test covering layout, parsing, short buffers,
request-id routing and staleness:

```bash
py -3 sampler.py
```


## Pause / Resume

Each leg has a **Pause** button next to Stop; it toggles to **Resume**. There is
only ever one replay, so every copy of the button shows the same state, driven
by the `/state` poll, and they are disabled when nothing is running.

Playback time is wall-clock based, so pausing records when it started and
**gives that time back on resume** rather than letting the clip run on
underneath. Verified: a 59.6 s clip paused for 12 s took 71.4 s end to end, and
the camera aim was frozen to two decimal places throughout the pause (the ghost
holds its pose). The camera knobs keep working while paused, which is the point
- set the shot up, then let it run.

`POST /replay/pause` toggles; send `{"paused": true|false}` to set it
explicitly. `/state` reports `replay.paused`.

**The ghost has to be pinned while paused.** It is an `AICreateNonATCAircraft`
object with an airspeed, and control is released to the sim - overwriting its
position 30 times a second is the only thing that normally hides its own
flying. Simply not writing to it during a pause let the sim fly it on, and
resuming snapped it back. The paused loop now keeps re-sending the frozen pose
at zero ground speed. Measured against the object in the sim: **0.0 m of drift
over 10 s paused**, where before it wandered off.

## Replay ends with the camera still there

The camera stays where it was put **only because something keeps setting it**.
When the clip runs out the loop stops, and the sim takes the view back to the
user aircraft within a frame or two — so "held until Stop" was a log line and a
flag, not a behavior. A hold loop now re-asserts the camera every
`CAMERA_HOLD_SEC` until Stop, which is what the message always claimed.


When a clip plays out the camera **stays at the event** and the ghost stays
where it finished. Only **Stop** releases the camera and removes the ghost, so
the view comes back to the aircraft when you ask for it and not before.
Starting another replay tears the previous one down first.

`/state` reports this as `replay.holding`.

## Performance

Unchanged from the budget: detect 1 Hz, buffer 5 Hz / 45 s, watcher
Below Normal. The logbook rebuild is the only addition — ~0.4 s for the current
tree, debounced 20 s, on a background thread, and it never runs during a clip
window.

The tray opens your **default browser** rather than embedding one.  budgets
no Chromium compositing in VR and  lists a left-open Chromium as a risk, so
there is no browser to leave open by accident.

The UI used to tell you to close the tab before flying in VR. That was inherited
caution about an embedded browser, not a measured property of this page, and it
was giving advice that was not warranted:

- No `setInterval` anywhere — polling is a self-scheduling timeout.
- `pollState()` returns immediately when the tab is not visible, so a
  backgrounded tab issues **no fetches at all**; all that survives is a no-op
  timer every 5 s.
- No animation. The only CSS transition is a 0.12 s transform on the disclosure
  arrow, and that fires on click.
- Maps are static PNGs, loaded once.

So a tab left open behind the sim, or hidden by a headset, costs essentially
nothing. The one case that does work is a **visible** window during a replay,
when polling rises to 1 Hz to drive the progress bar — and a replay is not
something you do while flying.

---

## The camera / chase bug (fixed)

Chase never moved the camera. Two faults in the binding of the game DLL's
internal camera API, both now fixed in `watcher.py`:

1. **`SimConnect_CameraAcquire` was bound as `(HANDLE)`.** In the game DLL it
   has a *byte-identical prologue* to `SimConnect_CameraRelease`, which was
   already correctly bound as `(HANDLE, const char*)`:

   ```
   48 8b fa   mov rdi, rdx    <- arg2 is a pointer, and it is dereferenced
   48 8b f1   mov rsi, rcx    <- arg1 is the handle
   ```

   With only one argument declared, RDX held whatever the previous call left
   there, so the DLL dereferenced garbage — `access violation reading 0x...`.
   It appeared to "work" intermittently, whenever that garbage happened to be
   a readable address.

2. **The name may not be empty.** Probing the live sim, every non-empty name is
   granted (`dwAcquiredState=1`); `b""` is the only one refused. So acquire
   silently returned `S_OK` with the camera never actually granted, and every
   `CameraSet` after it was ignored. Both acquire and release now pass
   `CAMERA_CLIENT_NAME`.

Verified end to end against the running sim: acquire -> `acquiredState=1` ->
`CameraSet` -> 25 s replay -> `CameraRelease ok` -> stop, with the camera
handed back when the clip ends or you press Stop.

### Camera modes

3. **The replay loop was re-aiming the camera 20 times a second.** Even once
   acquire worked, any input you gave was overwritten within 50 ms. There are
   now two modes, selected in the camera bar:

   | Mode | Behavior |
   |---|---|
   | **Place & let go** (default) | One `CameraSet` puts the camera at the event, then the watcher does not touch it again — the sim's own camera controls, i.e. your control pad, own it. |
   | **Locked follow** | The old behavior: re-aims every frame. Smooth, but it fights any input. |

   `POST /replay/recenter` (the **Recenter on ghost** button) re-places the
   camera on the ghost once, for when you have flown the view somewhere else
   and want to get back.

   Moving Distance / Height / Orbit also re-places the camera once, so the
   sliders now set the *initial vantage* rather than driving a live chase.
   Defaults are 45 m back, 15 m up.

4. **The camera struct was the wrong size, so the sim rejected every
   `CameraSet`.** This was the real reason nothing ever moved, and why fixes
   1-3 changed nothing visible. The watcher log held 473 `exception=46`
   messages arriving at 20 Hz - exactly the replay loop's rate.

   `CameraSet` is fire-and-forget, so its `S_OK` proves nothing. The packet
   sizes the DLL builds give the layout away:

   | Export | Packet | Header + payload |
   |---|---|---|
   | `CameraGetStatus(HANDLE)` | `0x10` = 16 | 16 + 0 |
   | `AIRemoveObject(HANDLE, DWORD, DWORD)` | `0x18` = 24 | 16 + 8 |
   | `CameraAcquire(HANDLE, char*)` | `0x810` = 2064 | 16 + 2048-byte name |
   | `CameraSet(HANDLE, struct*, DWORD)` | `0x68` = 104 | 16 + **88** |

   So the struct is 88 - 4 (mask) = **84 bytes**. The old ctypes struct was 96.
   `CameraGet` confirms it: the sim replies with exactly 84 bytes.

   Verified by echoing the sim's own struct back with a known delta - asked for
   +10 up / -30 back, read back +10.2 / -28.4. Exceptions went from 20 per
   second to **zero**.

   The watcher now uses **echo-and-modify**: `CameraGet` at acquire time gives a
   baseline, and only the fields we have identified are patched, so everything
   unidentified keeps a value the sim already accepted.

   | Offset | Field |
   |---|---|
   | 0 | position, 3 doubles |
   | 24 | referential (u32) |
   | 28 | object id (u32) |
   | 32 | targeted position, 3 doubles |
   | 68 | rotation object id (u32) |
   | 76 | fov (double) |

### A replay that starts but shows nothing

`CameraSet` is refused while the sim is showing a menu, and **every other camera
call still answers normally** - acquire reports success, `CameraGetStatus` says
ACQUIRED, `CameraGet` returns a struct, `CameraEnableFlag` is accepted. So a
replay reported `ok`, spawned a ghost, and nothing appeared on screen.

The tell is exception 46 arriving once per `CameraSet`, with
`index = 0xFFFFFFFF` - the sim objecting to the call itself rather than to any
parameter. Confirmed by echoing the sim's own struct back verbatim: still
refused, at every mask and every referential.

`CAMERA STATE` names the condition. Flight views are 2-10 (cockpit, external,
drone, fixed, environment, six-dof, gameplay, showcase, drone-aircraft);
anything above that is a menu or a transition. The case that produced this was
**state 30**.

The sampler now carries `CAMERA STATE` as a 13th variable - no extra request,
it rides in the existing struct - and `POST /replay` refuses up front:

```json
{"ok": false, "error": "sim is in a menu",
 "detail": "Return to the flight view and try again (camera state 30).",
 "camera_state": 30}
```

`/state` reports it as `sampler.camera_state`.

### Placing the camera at the event: WORLD / LLA

The MSFS dev-support Camera API thread supplied the missing piece:

```
SimConnect_CameraGet(HANDLE hSimConnect, DWORD referential);   // arg2 is the REFERENTIAL
```

That argument is *not* a request id, which is what an earlier reading assumed.
The values at offsets 24 and 68 were never "request id echoes" - they are the
Position and Rotation **referential** fields, faithfully echoing whatever was
asked for.

Asking the sim for each referential in turn, with the aircraft at a known spot:

| Referential | Reply | Meaning |
|---|---|---|
| 0 | `(-0.66, -0.15, 3.24)` | Aircraft datum, meters |
| 1 | `(-0.40, 0.32, 1.86)` | Eyepoint, meters |
| **2** | `(lat, lon, 2591.91)` | **World: latitude, longitude, altitude in METERS** |

2591.91 m = 8503 ft, exactly the aircraft's altitude. There is **no SimObject
referential** - the camera cannot be attached to an AI object, which is why
every attempt to target the ghost failed. World is the answer instead: the
watcher computes a lat/lon offset from the ghost's live position and places the
camera there, pointing back at it.

Verified: replaying a takeoff while parked 12 nm from the airfield put the
camera at the airfield, at the altitude the clip held, with FOV reading back
0.800 - our value.

Note the clip's altitude is in **feet** and the World referential wants
**meters**.

### Aiming: use TargetPosition, not Pbh

Two more traps, both of which produced a camera in the right place pointing the
wrong way:

- **Each aspect needs its own CameraSet call.** Sending position and rotation
  together (mask 0x0F) applies the position and *silently drops* the rotation -
  S_OK, no exception. The watcher now issues one call per mask.
- **The sim aims DOWN with POSITIVE pitch.** Computing the angles by hand and
  writing -18.43 pointed the camera 18 degrees *up*: a 37 degree error against a
  46 degree field of view, which puts the ghost just outside the frame.

Rather than hand-compute angles at all, the watcher writes the ghost's world
coordinates into **TargetPosition** (offset 32, 3 doubles, LLA) and sends
`mask 0x04`. The sim then derives the angles itself. Verified: asking it to look
at a ghost 45 m away and 15 m below produced pitch +18.44, heading 28.09 -
matching the true bearing of 28.00 and depression of 18.42.

Note the reply's Pbh field order (pitch, heading, bank) differs from the write
order (pitch, bank, heading), which is easy to be fooled by.

### One failed camera write must not end the shot

Moving the orbit, distance or height slider during a replay used to send the
view back to the user's aircraft. All three knobs run through `update_chase`,
which issued one `CameraSet` and, if it did not take, called `camera_release()`
- and releasing is exactly what hands the view back. The replay loop did the
same on one failed pass out of the thousands it makes at `REPLAY_HZ`.

`camera_set` returns false for transient reasons: it needs a `CameraGet`
baseline before it can build a `CameraSet`, and a round trip that times out
while a slider is being dragged is enough. The log carried 2511 SimConnect
`DATA_ERROR` exceptions from one afternoon of replays, about 7 a second, none
of which stopped the ghost or the playback.

So the knob keeps the camera and logs the miss, and the loop tolerates
`CHASE_FAIL_TOLERANCE` consecutive failures - about a third of a second at
180 Hz - before giving up. A camera that is genuinely gone fails every pass and
still ends the chase. Lock and recenter never released and were not at risk.

### The tilted horizon, and why aiming went back to angles

TargetPosition solves pitch and heading and says nothing about roll. No
`CameraSet` the watcher sent carried `CAMERA_MASK_ROTATION`, so **the bank
written into the struct had never once been applied**. The camera kept whatever
roll it held when it was acquired, which is why the horizon was tilted on some
replays and level on others - it depended on the attitude the shot started
from, so it looked intermittent rather than broken.

Reading the angles back to preserve them does not work either. `CameraGet` does
not report the effective chase orientation: over an 80 second replay it returned
pitch 5.79, heading 25.76, bank -0.10 on all 40 samples, unchanged, while the
ghost's heading swung from 178 to 185 and the camera was visibly tracking it.
The reply is not the write struct - offset 24, the referential on the way out,
comes back holding our own request id, and the position comes back as small
aircraft-relative meters.

So roll can only be controlled by computing the angles ourselves and aiming
with Pbh, which is what `CHASE_AIM_MODE = "angles"` does: `look_angles()`
derives pitch and heading with the same flat-earth approximation the placement
uses, so the two agree exactly, and bank is forced to zero for a level horizon.
`"target"` is kept as the other setting, and is the behavior with the tilt.

### A control pad cannot move the placed camera

```
SimConnect_CameraEnableFlag(HANDLE, DWORD flag);
```

Sweeping 0-7 against a live sim: 1 and 2 are accepted, 0 and 4 are refused with
exception 46 - matching the two documented flags. The watcher now enables both
right after acquiring:

- `CAMERA_FLAG_INTERACTION` (1) - the sim's own camera controls, and therefore a
  control pad, can move the camera we placed
- `CAMERA_FLAG_ABOVE_GROUND` (2) - keeps it from sinking through terrain

**This does not give a control pad control of the camera.** Tested in the sim:
with the camera acquired the pad cannot move it, and releasing it after placing
just snaps the view straight back to the aircraft. Placing and user-articulating
are mutually exclusive through this API - an acquired camera belongs to the
add-on, and a released one goes home.

`CAMERA_FLAG_INTERACTION` is accepted by the sim but does not help, so it is
left off (`CAMERA_ENABLE_INTERACTION = False`).

So articulation is the Distance / Height / Orbit knobs in the camera bar, which
re-plant the camera live during a replay.

### Lock to ghost

**Lock to ghost** in the camera bar holds the vantage you have set up as the
ghost moves, instead of planting the camera and letting the ghost fly out of
the shot.

Ticking it reads the geometry that is actually on screen - how far the camera
sits from the ghost, how high, and from which side - and keeps that. So the
angle you dialled in with Distance / Height / Orbit is the angle you keep,
rather than something you have to restore with **Recenter on ghost** every few
seconds.

A second control picks what "relative" means:

| | |
|---|---|
| **turns with the aircraft** | The offset rotates with it, so the same face of the aircraft stays toward you - an ordinary chase view. |
| **holds its bearing** | The compass angle is held, so the camera flies alongside and the aircraft turns in front of it. |

Measured against the sim, camera-to-ghost separation during a takeoff:

```
planted:  61 m -> 59 m -> 68 m -> 103 m     (the ghost flies away)
locked:   61 m -> 61 m -> 61 m ->  61 m     (the vantage holds)
```

Unticking it leaves the camera where it is, back to the tripod below.
`POST /replay/lock` takes `{locked, bearing}`.

Two things this needed to be usable:

- **The lock survives between replays**, so `/state` has to report it or the
  checkbox cannot mirror it. It was building its own chase subset that omitted
  the lock, which left a following watcher looking locked with the box clear.
  The UI now mirrors the watcher whenever it answers, not only mid-clip.
- **The heading is smoothed before the camera is placed on it.** A locked
  camera sits on a bearing off the ghost's heading with a long arm, so heading
  noise becomes visible swing - and at the start of a takeoff a hovering
  helicopter yaws with no direction of travel at all. The heading is low-passed
  and, below 6 kt, simply held. Measured through a hover: the ghost wandered
  229.1 -> 221.9 degrees while the camera bearing held at 94.8 with **0.0
  degrees of swing**.

### Place mode is a tripod, not a fixed stare

Position and aim are separate mask calls, so place mode holds the camera at the
event and re-sends only the target each frame. The shot stays put while the
ghost flies through it, panning to follow - which is what you want for a
takeoff, where a fixed stare would lose the aircraft in seconds. Moving a knob
re-plants the tripod. Follow mode moves the camera itself.

Verified over a takeoff: position held constant to the metre while pitch went
18.15 -> 14.78 and heading tracked around 28 degrees.

## Ghost playback: what the sim does to a driven object

The ghost is an AI aircraft driven with `SIMCONNECT_DATA_INITPOSITION` at
`REPLAY_HZ`. Four findings, each measured against a live sim rather than taken
from documentation:

| Constant | Why it exists |
|---|---|
| `GHOST_FREEZE` | The sim keeps integrating the object between our writes, carrying it forward from the airspeed we hand it; the next write snaps it back. On screen the aircraft flickers between two positions about `speed × 1/REPLAY_HZ` apart — nothing at a hover, worse the faster it goes. `FREEZE_LATITUDE_LONGITUDE_SET` / `_ALTITUDE_SET` / `_ATTITUDE_SET` stop it. |
| `GHOST_ZERO_AIRSPEED` | The other half of the same problem: with no velocity there is nothing to carry forward. Kept separate from the freeze so either can be switched off to bisect. |
| `GHOST_GEAR_FROM_RECORDING` | Playback forces `OnGround=0` so the recorded altitude is honored instead of being clamped to terrain. The sim then thinks a retractable-gear aircraft is flying and stows the gear for the whole clip, so it lands on its belly. The gear handle is therefore commanded explicitly and refreshed every `GEAR_REFRESH_SEC`, including while paused — **from the recorded handle position**, which is what the pilot actually did. |
| `GEAR_DOWN_AGL_FT` | Fallback only, for a clip carrying no gear position: gear down within this height of the clip's touchdown altitude. |
| `GHOST_GROUND_LIFT_FT` | The recording was taken with weight on the wheels and the struts compressed; the ghost's gear hangs fully extended, so the tyres sit below the datum. This is a calibration knob — there is no simvar for strut compression — blended out over `GHOST_GROUND_LIFT_BLEND_FT`. |

**Position simvars cannot see any of this.** Reading `PLANE LATITUDE` back on an
object you are writing to echoes your own write: lateral tracking measured 5 mm,
heading 0.003°, altitude 0.01 ft, while the render was visibly wrong. A symptom
that scales with speed, or that is visible on screen while every measurement
looks clean, lives in what the sim draws on top of the pose.

**`MapClientEventToSimEvent` does not validate names.** It returns success for
any string, including deliberate nonsense, so it cannot be used to discover
whether an event exists. Any probe built on it needs a known-bogus control.

Replays open held at the first frame (`REPLAY_START_PAUSED`) so the shot can be
set up before anything moves.

### Gear and flaps come from the recording

`GEAR HANDLE POSITION` and `FLAPS HANDLE PERCENT` are sampled with the rest of
the state and stored on every clip point, and playback replays them. Flaps
matter for the same reason gear did: a fixed-wing ghost flying every approach
flaps-up is obviously wrong, and the recording already knows what was set. That is right for a fixed-gear trainer, a
retractable single and an airliner alike, with no per-airframe tuning, and it
reproduces the pilot's own timing — gear down on the downwind rather than at
some invented height on short final.

An altitude rule (`GEAR_DOWN_AGL_FT`) remains as the fallback for clips written
before gear was captured. Guessing at a height was never the right mechanism,
only the one available for clips that already existed.

`interpolate_pose` copies the next sample wholesale and splines just the named
numeric keys, so `gear` rides through as a step value — correct for a discrete
control, no spline overshoot between up and down.

**Spoilers are recorded but not yet replayed.** `SPOILERS HANDLE POSITION` costs
one float and cannot be recovered afterwards, whereas deciding how to replay it
can wait for a fixed-wing flight that shows it is actually wrong. Known gap,
recorded deliberately.

Note there are three field whitelists between the sim and a clip:
`sample_from_state()`, `clip_point()`, and the current-state document. A new
variable has to be added to the sampler *and* threaded through them, or it is
sampled and silently dropped.

## Opening the logbook from the tray

`webbrowser.open` resolves to `os.startfile` on Windows, and the tray used
to discard its return value — so a failure to launch anything was silent.
It is reported now.

The subtler half is the foreground. A background tray has no right to change
which window is in front, so when a logbook tab was already open the browser
would be told to show it, focus the tab internally, and leave the window
exactly where it was — behind the sim. Clicking *Open logbook* looked like
it did nothing. `AllowSetForegroundWindow(ASFW_ANY)` is called first, which
hands that right to whatever is about to launch.

Note that a rebuild is **not** a cause of this. `/state` was measured at a
median of 1 ms and a maximum of 198 ms across 374 samples taken during a
20.7 s full rebake — nothing over 1 s, no failures. `ThreadingHTTPServer`
keeps it answering, and `_logbook_lock` is held only around the flag, never
for the build. The tray's 1 s liveness probe is in no danger from it.

## Logbook freshness

Measured, not assumed: an index-only rebuild takes about 1.4 s cold and
50–240 ms once the sortie cache is warm, which is the mid-flight case since only
the active sortie has changed. It runs on a timer thread, not the detect thread,
in a process set to Below Normal — so it is not competing with the sim for the
frame budget. The elapsed time is logged on every mid-flight run, so a
regression would be visible rather than inferred.


Baking maps is the expensive half of a rebuild — Pillow, tiles, a supersampled
canvas — and that is what must not run mid-sortie. Rebuilding the index alone is
cheap, so a landing now refreshes the logbook straight away and the leg you just
flew appears without waiting until you park. The map bake stays deferred and is
serviced once the aircraft is parked.

The pending flag has to survive the index-only run. Clearing it there would not
defer the maps, it would forget them.

### Saying so in the UI

`/state` carries a `logbook` block: `building`, `elapsed_s`, `reason`, `maps`,
and `seq` (a counter of completed builds) plus `last` with the duration and
outcome of the previous one. The page shows a strip under the header while a
build runs — *Refreshing the logbook and maps... (12s) — you asked for it* — and
reloads its data when the build finishes.

It is a strip rather than a full-page interstitial on purpose: a rebuild
replaces the index, it does not invalidate what is already on screen, so
there is no reason to take the logbook away from you while it runs. The one
exception is a first run with no `logbook.json` yet, where the page says it
is building rather than asking whether the watcher is running.

The reload keys off `seq`, not off catching `building` in the act. Two cases
have to work and only one of them is obvious:

- The page was **closed** during the build. On open it adopts the current
  `seq` and does not reload — `load()` has just run anyway.
- The page was **open before the build started**. There is no earlier `seq`
  to compare against, so the counter alone would never fire. A `sawBuild`
  flag covers it: having seen `building` go true, the page reloads when it
  goes false.

The banner is also withdrawn whenever the last confirmation goes stale, by a
watchdog on its own interval rather than inside the poll loop — if that loop
is the thing that has stopped, it cannot be the thing that notices. The
banner is a claim about right now, so it is not made on evidence older than
ten seconds. A real build keeps refreshing that evidence every second and is
unaffected.

This matters because no JavaScript fix can repair a page that is already
open: it keeps running whatever it loaded. The watchdog is what makes a
stranded banner clear itself rather than wait for a reload.

Polling drops to 1 s while a build is running so the strip clears promptly,
and `pollState` still opts out entirely while the tab is in the background —
the existing `visibilitychange` handler polls at once on return.

This replaced a fixed `setTimeout(load, 4000)` after a settings save, which
was a guess at how long a rebuild takes. A full rebake measures ~20 s.

### A reload must not move the reader

Every rebuild reloads the list, and rebuilds come after each takeoff,
landing, runway lookup and watcher start. A reload used to clear every open
flight back to "Loading", the page shrank, and the browser put the reader at
the top. Open flights now keep their previous detail until the new one
arrives, the month is refetched behind the list on screen, and the flight at
the top of the screen is put back where it was; the reader's own scrolling
cancels that. Measured scrolled 9,000 px down a fully expanded month: a
reload moved the page to 0 before, and by 0 px after. The camera bar's offset
is watched with a ResizeObserver rather than measured once, because the
filter bar wraps after the flights are drawn and the bar had pinned 40 px
behind it. Verify this with the page scrolled and flights open: a reload at
the top of the page proves nothing.

### The Grading tab

A phase pill's tooltip once carried every measure with its score, weight and
band - a dozen lines, which the browser cut off at a fixed width. The pill
now gives its grade, the two measures that cost the most and any limit that
held it down. The detail is a Grading tab beside each leg's Overview: one card
per phase in the order flown, lift-off to landing, then the overall, each
measure beside its band. Nothing in the page knows a threshold; it shows the
leg as built. The tab is built when first opened, and the chosen tab is kept
per leg across a reload.

## Sortie identity across a reconnect

A reconnect mints a new `flight_id`. That is fine as a record of the fragment,
but it meant one continuous outing appeared under several ids, and only the
rebuild could tell they belonged together — which is how clips ended up filed
under `flt-...033411Z` while the sortie was named `flt-...033005Z`.

The watcher now carries a **`sortie_id`**, set to the first fragment's flight
id and inherited by every fragment that continues the same outing. It is
written to `current.json` / `/state`, every clip, every event, and each flight's
`.meta.json`. `flight_id` still identifies the fragment; `sortie_id` identifies
the outing, and the built sortie keeps `flight_ids[]` for provenance.

Continuity uses the same test as a disk resume — same aircraft, no spawn-sized
jump — via `carry_sortie_id()`. When it holds, the watcher emits a **`reconnect`**
event rather than `flight_start`, so the event log stops implying a new flight
began.

`lift_at` records when the outing first left the ground and is inherited across
reconnects, so a flight timer does not restart mid-sortie.

**The builder trusts a recorded `sortie_id` over its own inference.** When two
fragments both declare one, that decides whether they group; the timing and
distance rules remain only for a track carrying no sortie id, so that data
still groups exactly as it did.

## When an aircraft becomes a flight

**A valid aircraft is not a flight.** The sim answers with a valid title and
a plausible position while its own menus are up, with the chosen aircraft
parked at the departure position, so choosing an aircraft used to mint a
flight: a few minutes of a stationary aeroplane, no takeoff, no distance. A
fresh aircraft is now held as a `PendingFlight` - points in a ring, nothing on
disk, no recording claim - and promoted when it leaves the ground or nets 50 m
of displacement in a sliding 60 s window. The ring is then replayed into the
track, so a cold-and-dark start keeps its prefix. `arm_or_begin` is the whole
decision, and `test_arming.py` drives it.

Not a speed: airspeed on a parked aircraft is the wind, and ground speed
jitters to 0.99 kt while the aircraft settles on its gear. Measured across 12
hours of stationary aircraft, the worst 60 s drift was 3.6 m, so 50 m clears
it about fourteen times over. The window slides rather than accumulating from
where the candidate armed, or slow drift adds up past any threshold given a
long enough sit in the menus.

**Being somewhere else is not having gone there.** Picking a different
parking spot moves the aircraft in one sample, with the sim reporting no
ground speed for it, and the continuity check only rejects jumps over
`RESUME_JUMP_NM`. So a change of stand minted a flight - 90 m, 550 m, 1 nm and
4 nm were each measured to arm one. Each step is now checked against the
ground speed reported for it. That corroborates the displacement rather than
replacing it: wind rocking a parked aircraft never nets 50 m, so it still arms
nothing.

**"Not on the ground" is not flying.** While loading an aircraft the sim can
report SIM ON GROUND false for a few ticks, with the aircraft frozen on its
pad at 0.0 g. Every recorded lift-off read 0.997 to 1.5 g on its first
airborne sample. Airborne arms unless there is positive evidence of a frozen
sim - every g reading present and under 0.8 - and a missing reading is not
that evidence.

**A landing needs a flight to land from.** A takeoff had to stay airborne for
`BOUNCE_SEC` before it counted; a landing only had to stay down, so that
loading blip became a one-second, zero-distance landing. A landing now needs
`flown`, set by the same `BOUNCE_SEC` airborne and cleared when a landing
commits - and set on reattaching in the air, or a watcher restarted just
before touchdown would refuse a real landing. The tracker runs at 10 Hz and
the track is written at 1 Hz: driven at 1 Hz the fault did not reproduce even
on the old code, and at 10 Hz the old code logged the phantom for any blip of
three ticks or more.

**A resumed flight keeps counting its legs.** A flight resumed from disk got
its leg count from clip names ending `.json`. Clips had become `.jsonl`, so the
count came back 0, and the next leg appended its clips to leg 1's - replaying
either played leg 1. `restore_tracker_leg` now asks `clipfile` for names, and
opening a clip refuses a name already on disk and takes the next free leg, so
a miscount costs a leg number rather than a recording.

## Liveries

Clips record `TITLE` and `LIVERY NAME` separately; ghosts spawn through
`AICreateNonATCAircraft_EX1`, whose argument order was established by
measurement — **livery third, before the tail number**. The other order returns
`0x80004005` and creates nothing.

A clip may carry no livery. Playback then borrows
the livery of the aircraft loaded right now, but only when it is the same
variant the clip was flown in, so it cannot guess across aircraft. The UI's
**Livery** field covers what that cannot. Every replay reports the livery used
and its source (`clip`, `current aircraft`, or `typed`).

## The EFB package

`efb-pkg/` is the source of truth. It is deployed to
`Community2024/afterflight-efb` under the sim's `InstalledPackagesPath`,
which is what MSFS actually loads. Never edit the deployed copy — use the
deploy script, which finds the sim's package folder from `UserCfg.opt` rather
than a hardcoded path:

```powershell
py -3 deploy_efb.py --check   # compare, write nothing
py -3 deploy_efb.py           # deploy, regenerate layout.json, verify
py -3 deploy_efb.py --remove-legacy   # also delete the pre-rename package
```

From the release zip, which has no `py`: `.\runtime\python.exe deploy_efb.py`.

Restart MSFS afterwards. It reads these files at startup and reloading the EFB
app alone does not always pick up a change.

**`layout.json` is generated, never hand-written.** It records every file's
byte size and timestamp and the sim validates them, so a stale entry leaves a
package MSFS refuses to load with nothing to say why. `deploy_efb.py`
regenerates the deployed copy's on every deploy and then verifies every entry
against disk. The committed `efb-pkg/layout.json` is rewritten only when a
path or size no longer matches: git does not keep modification times, so
restamping the dates on every deploy left every working tree modified - it
showed as `-dirty` in the version, and would have made `git pull` refuse.
Nothing reads those dates; the verification checks paths and sizes.
`test_efb.py` covers it.

For the same reason `.gitattributes` pins `efb-pkg/** -text`. With
`core.autocrlf=true` — the Windows default — git would rewrite line endings on
checkout, change the byte sizes, and produce exactly that silent failure on a
fresh clone.

## A replay used to be able to record a flight

Replaying a landing overwrote the clip being replayed. The pilot was parked on
the spot they had landed on - which is exactly where a replay puts the ghost.
At the ghost's touchdown the sim broke the parked aircraft's ground contact for
a single sample, one `on_ground=False` out of 1,295, and the detector read that
as an airborne-to-ground transition. It committed a landing, wrote a clip for
it, and the clip it wrote over was the one playing.

The user's own aircraft never moved: ground speed 0.0 and no transition at all
in the 1 Hz track across the whole window. One sample at the 10 Hz the ring
buffer runs at was enough, because a landing needs only one airborne reading
followed by two seconds on the ground.

**A replay puts a second aircraft into the world, usually on top of the first.
Nothing observed while that is true is recorded now** - no events committed, no
pending events opened, and holding counts as well as playing, because the ghost
is still sitting on the runway after the clip ends. The cost is that genuinely
landing during a replay goes unrecorded, which is the right way round: losing a
recording that has not happened yet beats destroying one that has.
`test_safety.py` covers it.

## Running without a Python install

Two steps, both shipped in 0.7.0; DESIGN-NOTES.md section 1 holds the
reasoning that chose them.

**No pip package.** The Python-SimConnect package did three things for the
recording loop, and the watcher's own ctypes layer now does each
(`SIM_CONNECTION = "native"`, the default; the `AFTERFLIGHT_SIM_CONNECTION`
environment variable overrides it): the connection, which is the push
sampler's own; a clean quit, from the QUIT message; and a sim gone without
saying so. The package's one-variable-at-a-time read failed when the sim died
and ended the session, and that was the only crash detection there was.
Natively a heartbeat - `RequestSystemState` every 5 s, answered even while
paused - notices it: silent for 15 s, or two failed sends, and the session
ends as "error" with the flight kept. When the sim stops pushing - a pause, a
loading screen - the last state is held, timestamped now. Building it found
a bug a live test would have hit at once: with nothing pushed yet the loop
assumed a sample and ended the session on its first tick.

Verified against a running sim with the owner flying: connecting parked, a
full circuit with both clips, a minute in the Escape menu with the heartbeat
answered and the track continuous, a clean quit, and a reconnect. A sim killed
outright was not tested live; `test_native.py` covers it, run with the
package made unimportable. Native needs the sim's own SimConnect DLL, which
the watcher finds in the install or reads from the running sim and copies to
`native/`. Where it cannot, and the package is installed, it falls back to the
package and says so once.

**A zip that carries its own Python.** `build_release.py` packs the tracked
app files with python.org's embeddable Python and Pillow, both pinned and
checked against their published SHA-256, plus a `VERSION` file and
`Start AfterFlight.cmd`. The embeddable Python ignores PYTHONPATH and the
script's own folder, so its `._pth` names the app folder outright. User data
stays in the app folder and an update is the zip unpacked over it, so the
build takes files only from `git ls-files` and refuses one that matches a data
path; `test_release.py` checks that and that nothing the app imports is left
out with the developer files. `.github/workflows/release.yml` builds the zip
on a `v*` tag and runs every test on the bundled Python. It publishes nothing:
the zip is attached to the GitHub release by hand, after the change has been
flown.

Verified: the zip, unpacked fresh and started from its launcher, recorded a
circuit, baked its maps and replayed a landing in the sim; separately, with
no Python or git on PATH, it ran, read its version and rebuilt a logbook. Not
verified: a machine that has never had Python installed.

**The version.** `describe_version()` refines `APP_VERSION` from
`git describe --tags --match "v*"` - release tags only, since a working marker
tag once became the version every user saw - and a release, which has no
`.git`, reads the `VERSION` file the build wrote.

Why not PyInstaller or Nuitka: the tray relaunches `[sys.executable,
WATCHER]`, modules find `BASE` from `__file__`, the data is writable files
beside the code (a folder either way), and frozen sideloaded utilities draw
antivirus false positives. Worth revisiting only for a signed single-file
installer.

## Maintenance integrity and recovery

Builds and purges share a maintenance lock, including command-line rebuilds.
Capture does not acquire it. Event filtering prepares its replacement outside
its append lock and preserves the tail appended before commit. Active tracks
cannot be purged or resumed during deletion. Hide/restore changes serialize
through the exclusions document lock; failed writes propagate to the caller.

A purge records its intent in `excluded.json` before deleting anything. If a
file is locked or a write fails, the record remains in Removed even when all
tracks have gone. It is marked **Deletion incomplete** and can only be finished,
not restored. Retry is safe after a restart. A stale browser cannot purge an
item that has since been restored. This is recovery metadata, not an undo copy.

Rebuild requests distinguish scheduling from full reprocessing. Full
reprocessing bypasses both flight and sortie caches. Routine signatures include
metadata, complete events, referenced clips, resolved exclusions/preferences,
prose inputs, and a content revision of the grading code. Each graded leg carries
`grading_revision`; thresholds and scoring behavior are unchanged by this work.

**A delete is not a reprocess.** It used to empty the build cache and ask for
one, to be safe after a partial delete - so every delete rebuilt every flight
and redrew every map, 102 to 144 s each on a real logbook, while the page
showed nothing. Everything a delete changes is in the sortie signatures -
the flight's track, cut for a leg and gone for a flight; its own events; its
exclusions - so it now runs a normal rebuild: 2.7 s on a copy of the same
logbook, every untouched flight reused. `test_cache.py` checks the result against a full
reprocess, after a leg and after a flight.

**The dialogs that remove and delete stay open until the work is done.** They
are `<dialog>`s opened with `showModal()`, so the page behind is inert to
keyboard as well as mouse, and Escape closes the top one only. A confirm runs
its work itself, its buttons off and saying what it is doing, and closes on
the result, or shows the error and stays. It used to close on the click, and a
second click opened a second confirm on the first one's buttons. Test this
with real clicks: Chrome groups dialogs opened without a user gesture, and
one Escape then closes all of them, so a scripted click reports a fault a
person never sees.

Heavy rebuilds wait while the connected aircraft is moving or airborne, even
when explicitly requested. A parked build checks for renewed movement between
sorties, maps, and tile requests. A native image operation or request already in
progress cannot be preempted; cancellation occurs at the next checkpoint.
Incremental index-only refreshes remain available during a flight. Deferred full
reprocessing is remembered. No frame-performance claim follows from offline tests.

### Backups and restore verification

**What a backup carries is one inventory,** `build_release.DATA`: what cannot
be recreated goes in every backup, what is rebuilt from it only in a `-Full`
one, and the process's own state in none. The same list is what `.gitignore`
must ignore and what a release must refuse, and `test_release.py` checks all
three against it, because they had drifted: the runway cache and the user's
`places.json` were in no backup, so a verified, quiet restore regraded every
landing its runway had held down - a long one came back from F to A - and
lost its place names. `test_runways.py` now backs a graded landing up,
restores it into an empty tree and rebuilds offline, both switches on, and
requires the same letters, scores, measurements, names and passenger text.

**The runway cache is evidence, and it is shared.** One file per airport,
kept and backed up, and never replaced automatically. Historical calculations
use the runway evidence currently available; the application does not keep a
separate runway version for each landing, so replacing or restoring a
different airport file would change every landing at that airport together,
and a landing first looked up today is measured against today's scenery.
Before any refresh feature: decide whether it rewrites history or only
applies to new landings, keep the old evidence, and report what moved.

`backup.ps1 -Verify` records SHA-256 hashes, checks archived bytes, and checks
whether source files changed during copying. `-Full` also includes derived data.
`-RequireQuiet` fails unless watcher and maintenance locks can both be held for
the entire copy. Stop the watcher before requesting a guaranteed quiet copy.
Without that switch, a live copy is allowed but labeled `live-uncoordinated`.
An unresponsive HTTP server is never treated as proof that nothing is writing.

The backup contains `manifest.json` and `RESTORE.txt`. Before restoring, run
`verify-backup.ps1 -Backup <folder>`. Restore to a separate stopped tree first,
then rebuild offline. Pending deletion records are included through exclusions.
Locks exclude AfterFlight writers, not unrelated editors. Hash equality verifies
bytes; it does not turn an uncoordinated live copy into a multi-file snapshot.

`test_integrity.py` covers concurrent writes, partial deletion through the HTTP
handler, full cache bypass, active recording guards, and a temporary-tree backup
restore with same-size corruption detection. `.github/workflows/offline.yml`
runs the offline checks on Windows with Python 3.10 and 3.12, discovering
`test_*.py` rather than keeping a list, and `release.yml` runs them again on
the bundled Python when it builds a release. Hosted CI and live simulator
acceptance are separate checks; a local pass does not claim either.

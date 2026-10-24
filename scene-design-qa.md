# Pushed scenes - design QA

Covers the four apps added 2026-09-10: Desk Companion, Commute Radar, GitHub
Sentinel (CI and PR radar) and Meeting Sentinel. Sixteen reachable lane states.

These four shipped without a preview renderer, unlike every other app in this
folder. That is the whole story: the panel absorbs bad geometry in silence,
because `put_pixel` clamps. A glyph drawn past the last row is discarded, the
lane still encodes to a well-formed 23x8 bitmap of exactly the right byte
length, and every existing test passes. Nothing in the suite could see it, and
nobody had ever looked at a rendered frame.

## What changed

| | before | after |
| --- | --- | --- |
| text lines | two, at y=1 and y=6 | one, at y=1 |
| bottom line | 3 of its 5 rows off-panel | n/a |
| accent rail | row 4, drawn *after* the text | row 7, drawn first |
| lane content | label above, value below | value, label kept while it fits |
| "1" glyph | 1px, identical to "I" | 3px, distinct |
| "I" glyph | 1px bare column | 3px serifed |
| "W" glyph | 3px, one pixel from "H" | 5px |
| SLEEP avatar | DIM_GRAY (45, 52, 64) | ICE outline |

## Findings

- **P0, fixed - the bottom data line was three-fifths off the panel.** The
  font is five rows and the panel is eight, so a second line at `y=6` runs to
  row 10. Measured with `put_pixel` instrumented: drawing `24M` at y=6 attempts
  29 glyph pixels and lands 12 of them, discarding 17 across rows 8, 9 and 10.
  At y=1 all 29 land. This hit 11 of the 16 states, and the casualty was always
  the primary value - the ETA, the build status, the PR count, the call timer.
- **P0, fixed - the accent rail was drawn through the type.** `render_commute_lane`,
  `render_ci_lane_frame` and `render_pr_lane_frame` each painted a rail across
  row 4 *after* drawing the line occupying rows 1..5. 9 of 16 states. This one
  is invisible to an off-panel audit, because those pixels land legally; only
  looking at a rendered frame shows it. The rail now sits on row 7 and is
  drawn first, the arrangement `timepiece_preview.py` already used.
- **P1, fixed - two 5-row lines cannot coexist on an 8-row panel.** The root
  cause of both P0s. Every design-reviewed app here draws exactly one text line
  at `y0=1` - `focus_sprint_preview.py:539`, `network_sentinel_preview.py:254`,
  `pc_stats_preview.py:330`, `aqi_brief_preview.py:229`. The four new apps
  invented a layout the hardware cannot render. The icon carries identity, so
  the lane now carries the value.
- **P1, fixed - "W" was one pixel from "H", and that is the WORK/HOME axis.**
  Caught only in the one-bit render, where `W 24M` and `H 52M` both read as an
  H. The destination initial was ambiguous between precisely the two values it
  exists to distinguish. The font is proportional, so "W" is now 5px; `W 24M`
  measures 19px against a 21px budget and still centres.
- **P2, fixed - the SLEEP avatar disappeared at low brightness.** Its body was
  DIM_GRAY, dominant channel 64, far under the panel's 174 one-bit threshold.
  The whole avatar dropped out and left two eye pixels floating in black - and
  SLEEP is the mood shown when brightness is lowest. It is now an ICE outline,
  which also satisfies the "meaning lives in silhouette" rule better than the
  filled block did.
- **P2, fixed - "1" was a 1px bar identical to "I".** `01:01` rendered as
  `0|:0|` and `#414` as `#4|4`. Both are 3px and distinct now, in the three
  fonts these scenes use.

  **The sweep claim originally written here was wrong, and is corrected.** It
  said the only single-column glyphs "remaining anywhere" were `" "`, `":"` and
  `"."`. It swept four fonts; the repository has **seven**. Still live and
  untouched by this work: `next_up_preview.py:99,116` defines `"1"` and `"I"`
  as byte-identical 1px bars - precisely the defect this finding claims to have
  eliminated - plus a 1px `"1"` in `focus_sprint_preview.py:156` and a 1px
  `"I"` in `weather/aqi_brief_preview.py:102`.
- **Not changed: 288 lit lane pixels fall below the one-bit threshold.** These
  are the TRACK rails and guides. They are decorative by intent and are
  *supposed* to vanish at brightness 1; the one-bit sheets confirm the type
  survives alone. Left as is deliberately, not overlooked.
- **Not changed: the companion faces.** The filled heads read as blobs at one
  bit, but they read well in colour, nightkeeper blacks the panel out at night,
  and rewriting five moods of character art is a subjective change that should
  be its own pass with the gallery in front of you.

## Evidence

- Before, four contact sheets at 32x8, nearest-neighbour 14x, one row per state
  and one column per animation phase - captured before any edit, and not
  reproducible from source now that the geometry is corrected.
- After, the same sheets from `scene_preview.py`.
- One-bit sheets for both, thresholding every channel at 174.

## Normalization and state

- Target viewport: one physical 32 x 8 RGB LED matrix.
- Both sets use the same scale, gap, background, states and phase ordering, so
  only the art differs between them.
- Icon occupies x0..7, column 8 is the black gutter, lane occupies x9..31.
- States: 5 companion moods, 3 traffic conditions, 3 CI outcomes, 3 PR ages,
  2 call durations.

## Verification

1. `pytest -q` from the awtrix root: **971 passed**, up from 966. The five new
   tests are the layout guards; no existing test changed behaviour except the
   typography assertion noted below.
2. Off-panel draws across all 16 states: **0**, down from 15-26 on each of 11
   states.
3. `test_instrumentation_detects_the_layout_that_shipped` reproduces the
   original fault and asserts it is seen, so the guard cannot silently stop
   working.
4. Lane widths measured against the 21px budget: `W 24M` 19, `H 52M` 17,
   `ON AIR` 21, `READY`/`FOCUS`/`SLEEP` 19, `REST` 15. Nothing clips.
5. `WORK 24M` measures 31px and correctly degrades to `W 24M`; `A PR:12`
   measures 23px and correctly degrades to `PR:12`.

**One existing test was deliberately rewritten.** `test_visual_system.py`
asserted `measure_text("1") == 1`, which encoded the 1px glyph as intended
behaviour. It now asserts 3px and that "1" and "I" are different shapes.

## Implementation checklist

- [x] One text line only, on rows 1..5.
- [x] Rails and pips on rows 0 and 7, drawn before the type.
- [x] Column 8 black in every state.
- [x] Every payload under the 2KB budget; icons 8x8 and on the 120ms beat.
- [x] No glyph drawn outside the 32x8 panel, asserted per state.
- [ ] **Live panel verification - not done.** None of these four apps is in the
      saved rotation (`Time, weatherbrief, nowplaying, Time, ocean, moon,
      aqibrief, pcstats0, networksentinel, nextup, focus, nightkeeper`), so
      none of this has been seen on hardware. No push was made.

## Second pass - residual items

The residual notes below were revisited. Four were applied, two were declined
on measured evidence. The declines matter more than the fixes.

**A sweep for glyph pairs within one pixel** returned 13 pairs in
`visual_system`, 14 in `github_sentinel`, 10 in `meeting_sentinel` and 7 in
`weather_brief_preview` - including `"0"` and `"O"`, which are byte-identical in
all four.

Two corrections to what was first written here. The sweep covered **four of the
repository's seven** `FONT_3X5` dicts. And the claim that "thirty-six glyphs
cannot be made mutually distinct in a 3x5 cell" is **false**: a 3x5 cell holds
2**15 = 32,768 distinct patterns, so distinctness is never the binding
constraint. The real limit is *legibility* - a glyph must still read as its
letter - which is a weaker and more arguable claim than the one made, and does
not on its own justify declining a fix.

The rule that survives: **disambiguate a pair only when both members can occupy
the same field position with different meaning.** `"W"`/`"H"` met that test - the
commute destination slot holds exactly WORK or HOME. `"M"`/`"N"` does not; there
is no `24N`.

| item | outcome |
| --- | --- |
| `github_sentinel` 3px "W" | superseded - resolved by unification, below |
| "M"/"N" one pixel apart | **declined, then REVERSED in the third pass** - see below |
| four divergent `FONT_3X5` copies | **applied** - `github_sentinel` and `meeting_sentinel` now import from `visual_system` |
| commute road on row 6 vs rail on row 7 | **applied** - the car sits one row lower, road now on row 7 |
| `custom_text[:7]` overflow | **applied** - truncates by measured width |
| stale ROADMAP phase 5 | **applied** |

**Font unification, with one accepted consequence.** `meeting_sentinel`'s local
font was byte-identical to `visual_system`'s for every glyph it defined, and
defined strictly fewer, so importing changes nothing. `github_sentinel` differed
in exactly one glyph - `"W"` - so it inherits the 5px W. That pushes `W PASS` to
23px against a 21px budget and `_fit_label` degrades it to bare `PASS`. This was
initially judged a regression and that judgement was wrong: in that font a repo
initial is ambiguous three ways at once (`H`/`W`, `N`/`W`, `M`/`H`), so a W-repo
was rendering an initial that reads as `H`. Showing no prefix is less
information; showing the wrong letter is worse.

The degradation is **wider than first recorded**: `W PASS`, `W FAIL` and
`W #414` all measure 23px, so a W-initial repo loses its prefix in every CI
state, not only on `PASS`.

**One latent bug fixed in passing.** `meeting_sentinel.draw_text` and
`text_width` looked glyphs up with `FONT_3X5.get(char, ...)` - no `.upper()`,
unlike the other two modules - so any lowercase input silently rendered as
spaces. Not live today (the lane renders only digits and a colon) but wrong, and
`ActiveMicSession.app_name` is mixed-case by construction.

**Verification of the second pass:** 971 tests still pass, unchanged. Truncation
measured with the corner pips excluded, since those sit at column 31 and mask a
naive extent check: `MEETING` 27px -> `MEETI` 19px, `WWWWWWW` 41px -> `WWW`
17px, `STANDUP` -> `STAND`, `HELLO` unchanged, empty string safe.

## Residual notes after the second pass

- **Confusable glyph pairs remain by design**, per the rule above. `"0"`/`"O"`
  are identical in every font here; context disambiguates digits from letters.
- **`weather/weather_brief_preview.py` keeps its own font.** Older design
  lineage, its own palette and tests, and `test_weather_brief_push.py:425`
  asserts on its `FONT_3X5["M"]`. Left alone deliberately.
- **`test_scene_layout.py` now checks one shared dict three times** under three
  module names. Harmless, and it still guards that each module exposes a sane
  font.
- **The companion faces** are still filled blocks that read as blobs at one bit.
  Unchanged - a subjective art pass that deserves its own review.

## Third pass - adversarial review

Seven agents reviewed the second pass: four trying to refute each applied
change, three sweeping for what the record missed. Five of their claims were
independently reproduced before anything was acted on. All five held.

**The M/N decline was reversed.** The rule adopted in the second pass -
disambiguate only when both members share a field position with different
meaning - was sound, but it was applied to the wrong pair.
`GitHubSentinelController.tick` rotates every configured repo through a *single*
app slot, distinguishing them by one initial. With `home-assistant` and
`miku-jarvis` both configured, the two rendered lanes differed by **exactly one
pixel**, at (11, 2). `"H"`/`"M"` met the rule, so the decline did not survive
it. `"M"` is now 5px, taking the font from 13 confusable pairs to 11 and adding
none. Verified: those two lanes now differ by 69 pixels. The accepted cost is
that `M PASS` measures 23px, so M-initial repos lose their prefix exactly as
W-initial ones already did.

**Three real defects were found, two of them in this pass's own work.**

- **P0 - `_fit_label` could light the black gutter.** Its final `return value`
  had no width check, and `run_number` arrives unfiltered from the GitHub API.
  At `#100000` the label measured 27px, the centring arithmetic went negative,
  the label drew from x=7, seven glyph pixels were clamped off-panel and
  **column 8 was lit on rows 2 and 4** - the one invariant every test asserts.
  Fixed twice over: `_fit_label` clamps to the budget as a backstop, and an
  oversized run number now renders `RUN` rather than a truncated number,
  because `#100000` clamped to `#1000` reads as a different, real run.
- **P1 - the rail guard was a silent no-op for two states.**
  `test_no_rail_crosses_the_text_band` built its rail-colour set per scene and
  `continue`d when that set was empty. `meeting_sentinel` declares no `TRACK`,
  so both meeting states were skipped outright, and a rail painted across that
  lane's type would have left the suite green. It now checks against the union
  of every rail colour in the family.
- **P2 - the commute road did not actually meet the rail.** The second pass
  moved the car down one row so its road would align with the lane rail, and
  the comment claimed continuity "across the gutter". Row 7 in fact read
  `########..#####################.` - a two-column break beside the icon and a
  dark stub at the far end - because the rail was inset one column each side.
  The rail now spans the full lane and row 7 reads
  `########.#######################`.

New guards: `test_unbounded_values_stay_on_panel_and_off_the_gutter` drives run
numbers from 0 to 2**31-1, and `test_open_pr_count_stays_on_panel` does the same
for PR counts. 973 tests pass, up from 971.

## Open findings, not addressed

The sweep raised substantial issues beyond the scope of "apply the residual
items". They are recorded here rather than silently fixed or silently dropped.

**Reachability - several features cannot occur in production at all:**

- `utility_hub.build_controllers` never passes a `mood_detector`, so
  `DeskCompanionController` falls back to `lambda: CompanionMood.IDLE`. Four of
  the five companion moods are unreachable on hardware.
- It never passes a commute `estimator` either, so only `CLEAR` at 25 minutes
  can render; the amber and coral traffic states are dead code.
- `GitHubSentinelController.tick` only ever calls `build_ci_payload`. The PR
  radar, the Octocat icon and all three badge tiers are unreachable, while the
  PR endpoint is still polled every cycle.
- ~~`scan_windows_microphone_registry`'s 24-hour sanity clamp~~ **FIXED in the
  fourth pass.** `LastUsedTimeStart` is fixed for the life of a session, so the
  clamp re-fired on every detection pass and a call open longer than a day
  rendered `00:00` forever. The window is now 30 days, which is a sanity bound
  rather than a normal long call.

**These three reachability gaps cut directly against the fourth pass, and that
is worth stating plainly.** The traffic-severity gauge and the Octocat face
were both fixed on this pass, and as the hub is wired today *neither can appear
on the panel*: the commute estimator is never passed, so only CLEAR is
reachable, and `build_pr_payload` is never called, so the Octocat is never
pushed. The fixes are correct and verified, but they are currently improvements
to code that production cannot reach. Wiring them up is a behavioural change to
what the panel displays - a product decision, not a visual one - so it is
flagged here rather than made unilaterally.

**Visual - all four of these are now FIXED, see the fourth pass below:**

- ~~`format_duration` switches from MM:SS to HH:MM with no unit marker~~
- ~~`draw_octocat_pr_icon` writes its face pixels onto an unfilled interior~~
- ~~PR-age escalation and traffic severity collapse at brightness 1~~
- ~~the companion trim loop is quadratic~~
- The palette was never unified the way the font was: `TRACK` is (32,40,54) in
  `visual_system` and (40,50,65) in `github_sentinel`, and `RED_HOT` differs
  between `visual_system` and `meeting_sentinel`.
- Four scenes still implement four different row-7 treatments.

**Record-keeping:**

- The repository has **no commits** (`git rev-list --all --count` returns 0), so
  every "before" number in this document is unverifiable from source. The before
  contact sheets are the only record, as noted above.
- Of the 288 sub-threshold lit lane pixels, 4 are `meeting_sentinel`'s DARK_RED
  row-7 pips, not TRACK.

## Fourth pass - meaning that survives the dark

The third pass recorded a set of open findings. The four visual ones are now
fixed, and they shared one root cause worth stating plainly.

**This panel's own rule was being broken.** `CLAUDE.md` says colour-only
distinctions vanish in the dark, so icon meaning must live in silhouette. Three
of these scenes encoded a *severity* purely in hue, and AMBER (255,145,45),
CORAL (255,76,91) and RED_HOT (255,25,35) all threshold to (255,0,0) at
brightness 1. So a moderate delay looked identical to severe congestion, and a
25-hour-old PR looked identical to a 50-hour-old one, exactly when the panel is
dimmest. The one-bit sheets taken earlier confirmed that *type survives* - they
never checked that *meaning stays distinguishable*, which is a different
question and the one that mattered.

| fix | carried by | verified at 1-bit |
| --- | --- | --- |
| Traffic severity | a congestion gauge above the car: 0 / 2 / 4 lit pixels | 14 / 16 / 18 lit pixels, all three distinct |
| PR age | badge size: 1 pixel / 3 pixels / a 2x2 block | 1 / 3 / 4 badge pixels, 24h no longer equals 48h |

**The Octocat had no face.** `draw_octocat_pr_icon` drew only a head outline and
left the interior black, then wrote its three "face" pixels as BLACK onto that
black interior - three no-ops. The icon shipped as a hollow 19-pixel ring and
read as a box. It now fills the head first and cuts the features out of it, the
way `desk_companion` already did.

**The call timer was ambiguous.** `format_duration` switched from MM:SS to HH:MM
at the hour boundary with no unit marker, and the timer is the entire content of
that lane - so a 125-second call and a 7,500-second call both rendered `02:05`,
and the preview's own 61-minute frame read as `01:01`. It now renders `NhMM`
above an hour: `02:05`, `1H01`, `2H05` are mutually unmistakable. Hours are
capped at 99 so the string cannot outgrow the lane (`99H00` is 19px).
`test_format_duration` was deliberately updated; it had encoded the ambiguous
form as intended behaviour.

**One wheel was always invisible.** The car's wheels alternated PEARL against
DIM_GRAY (45,52,64) - dominant channel 64, the same sub-threshold value that
made the SLEEP avatar vanish - so what shipped was a single dot teleporting
between two positions rather than wheels turning. Both wheels now alternate
PEARL against ICE, and both clear the threshold.

**And a latent trap removed.** The second pass's width-based trim re-measured
the whole string on every character dropped: 50,000 characters took 99.4s of
CPU, well past a 15-second controller tick. A one-line pre-clamp makes it
0.0001s. No caller passes `custom_text` today, so this was never reachable -
but the loop should not have been a trap waiting for the first caller.

973 tests pass throughout.

## Fifth pass - four of four fourth-pass fixes refuted

An adversarial review was pointed at the fourth pass, one agent per change,
each told to break it. It refuted **all four**, and every refutation
reproduced on independent measurement here before anything was changed. The
green suite constrained none of them: 973 passing tests could not see any of
this, because a scene that draws the wrong thing still encodes to a
well-formed 23x8 bitmap.

**The traffic gauge duplicated the car.** For MODERATE the two-pixel bar lit
x3-x4 on row 1; the car's own roof lights x3-x4 on row 3, in the same colour,
which at brightness 1 is the same (255,0,0). Measured: pixel-identical, in the
one state the change existed to separate - MODERATE from HEAVY. It read as the
car wearing a hat, not as a meter. Three further faults compounded it: the bar
was centred, so length had no fixed reference to be judged against; CLEAR drew
nothing, which is indistinguishable from the feature not existing and leaves
hue as CLEAR's only signal; and row 1 is the composite panel's text cap-line,
so the bar was colinear and colour-identical with the lane's own top row.

It is now anchored at x0, never empty (1 / 3 / 5 pixels), and on row 0 with the
rails. Verified: lengths 1/3/5, no state equal to the roof, row 1 clear.

**The wheel fix was self-refuting.** The fourth pass replaced the sub-threshold
DIM_GRAY wheel with ICE so both wheels would clear the 174 threshold - and that
is exactly why the animation disappeared: PEARL (255,247,232) and ICE
(196,239,255) both threshold to (255,255,255), so at brightness 1 they are the
*same colour* and the two frames are pixel-identical. Measured: **zero**
changing pixels in all three traffic states, against two in the version it
replaced. The comment claiming "the shimmer reads as motion" asserted the
opposite of what its own premise implies.

Motion needs displacement, not recolouring. The road now carries moving GOLD
dashes under a static car: 4 changing pixels at 1-bit in every state. GOLD
rather than a white because a white dash under the white wheel at x=5 merged
the two into one blob on alternating frames.

**The duration cap was a width fix applied as a truth fix.** `format_duration`
capped the hours at 99 but computed the minutes from the uncapped total, so
150 hours rendered `99H00` - identical to a genuine 99-hour call - and 1e9
seconds rendered `99H46`. An out-of-range value came out looking like a
precise reading. It now saturates as `99H--` (19px, inside the 21px budget),
which cannot be mistaken for one. `test_format_duration` was **deliberately
updated for the second time**; the fourth pass's own assertion,
`format_duration(200 * 3600) == "99H00"`, had encoded the fabrication as
intended behaviour.

**And widening the registry clamp made a stale key lie.** `Stop == 0 and the
exe is alive` is the shape of a live capture *and* of a crashed one - Teams,
Zoom and Discord sit resident for weeks, and a force-killed capture leaves Stop
at 0 indefinitely - so an implausible elapsed is the expected failure here, not
a rarity. Under the old 24h bound such an entry collapsed to `00:00`: wrong,
but visibly null. Under 30 days a three-day-old stale key renders `72H00`: an
authoritative fiction, under a beacon that asserts you are on air.

Both numeric answers are lies, so the session is now reported with
`elapsed_known=False` and the lane renders `--:--`. The mic really is open;
the duration is what is unknown, and the lane says so.

## Fifth pass - the four scenes now read as one system

Row 7 is the one element all four share, and it was rendered four different
ways: full width in one, inset two columns with bright pips on rows 0 *and* 7
in another, inset one column in a **locally redefined `TRACK`** that shadowed
`visual_system`'s by one shade in the third, and simply absent in the fourth.
The rotation advances every seven seconds, so the ground changed length, colour
and existence between pages of what is meant to be one system.

There is now one `visual_system.draw_lane_rail`, and all four call it: full
lane width in the shared `TRACK`, with the scene's accent as a single end-cap
at the far edge - far edge, so the ground still meets the icon's road across
the gutter. `github_sentinel`'s shadowing `TRACK` is deleted.

Three reachable icons were redrawn for the same reason:

| icon | was | now |
|---|---|---|
| CI PASS | a green stroke descending the wrong way beside a white tick - read as `1` next to a check | one two-pixel-wide connected checkmark, 12 lit, clear of the gutter |
| CI FAIL | all 12 pixels recoloured every beat, and at 1-bit both colours are (255,0,0), so nothing moved at all | the four outer tips extinguish and relight: the X pulses by size, 4 changing pixels at 1-bit |
| CI gear | four phases at two different radii with a wandering tooth count - it broke apart and reassembled twice a cycle | two 4-tooth sets at one radius, each 90-degree symmetric, B one cell around from A: a real rotation step |

And the companion's SLEEP mood, which had thrown away the family silhouette -
four moods share a 6x5 head with tinted ear pips, SLEEP had a three-row curl
with no ears, so the companion looked *replaced* rather than asleep. It now
uses the family head with violet ears and violet lids where the other moods put
pupils, and its `z` rises and resets instead of hopping around a 2x2 box.

**979 tests pass.** Six new guards, each confirmed to fail against the
behaviour it replaces: the rail guard fails on the inset-by-one rail, and the
motion guard fails on the PEARL/ICE wheel swap.

## Open findings, still not addressed

- **The Octocat reads as a skull, not a cat.** Its ears are PEARL sitting on
  PEARL head columns with no valley between, so the top edge is a crenellated
  rectangle. `desk_companion` solves this by tinting its ears against the head;
  `github_sentinel` does not. Left alone because the PR radar is unreachable -
  see below.
- **The PR badge tiers are one pixel apart.** 24h and 48h differ by exactly one
  LED out of 256, at (6,1), which is orthogonally adjacent to the ear - so the
  pixel meaning "a PR has waited two days" reads as the ear having grown. The
  48h "pulse" is a no-op at 1-bit for the same reason CI FAIL's was: RED_HOT
  and CORAL both threshold to (255,0,0). It costs 67 bytes to deliver nothing.
- **The meeting mic capsule inverts PEARL/RED_HOT every 120ms** with no shape
  change - six of the icon's brightest pixels, full-intensity hue inversion,
  which reads as an electrical fault rather than a live mic, and it fights the
  acoustic-wave animation underneath that does read as motion.
- **Three of five companion moods do not animate at all.** FOCUS and ON_AIR
  draw one frame; REST's only change is one near-white pixel. They no longer
  *pay* for four frames (see the sixth pass), but in a rotation where the
  meeting mic radiates and the car's road moves, two of the companion's moods
  are still frozen.
  (An earlier version of this line also claimed CI PASS was "static across four
  identical frames". That was wrong: `github_sentinel.py:418` has always built
  it with `frame_count=1, animated=False`. It is a still by construction and
  was never paying for repeats.)
- The REST mug still sits inside the head.

**And the reachability gaps are unchanged, which bounds what any of this is
worth.** `utility_hub.build_controllers` still passes no `mood_detector` (4 of
5 companion moods unreachable), no commute `estimator` (only CLEAR reachable,
so the congestion bar rebuilt above **cannot appear on the panel**), and
`GitHubSentinelController.tick` still never calls `build_pr_payload` (the
Octocat and its badge are unreachable while still being polled). Wiring any of
them changes what the panel displays, which is a product decision rather than a
visual one, so it stays flagged here rather than made unilaterally.

## Sixth pass - one palette, and no frames paid for twice

**Two colours had already drifted.** `github_sentinel`'s locally declared
`TRACK` was the fifth pass's finding; sweeping the rest of the palette the same
way found two more in `meeting_sentinel`: `RED_HOT` was (255,20,35) against the
shared (255,25,35), and `DIM_GRAY` was (40,45,55) against (45,52,64). Neither
is visible on its own, which is the whole problem - ON AIR red rendered one
shade off the companion's ON AIR red on the adjacent page of the rotation, and
nothing in a diff would ever have shown it. `GOLD`, `AMBER` and `DIM_GRAY`
turned out to be dead in that module; only their own definitions referenced
them. Both modules now import their colours, so there is no second copy left to
drift, and a guard asserts it.

**One accent was below the threshold.** The meeting lane's rail end-cap was
`DARK_RED` (130,10,20) - every channel under 174, so it was the one scene in
the rotation whose ground simply ended in nothing at brightness 1. It is
`RED_HOT` now, and a guard checks every scene's end-cap survives the dark.

**The mic was strobing.** The capsule inverted PEARL <-> RED_HOT every 120ms:
the six brightest pixels in the icon, full-intensity hue inversion with no
shape change, which reads as an electrical fault rather than a live
microphone. Worse, it destroyed the silhouette on half the frames - a RED_HOT
capsule is the same (255,0,0) as the CORAL body around it at brightness 1, so
the mic collapsed into a featureless red blob every other frame. The capsule
is a constant PEARL now; the acoustic waves, which animate by displacement and
therefore survive the 1-bit collapse, carry the motion on their own.

**And four icons were paying for frames they did not have.** The companion's
FOCUS and ON_AIR avatars drew one frame four times, REST drew two, and the
Octocat drew one. PIL already merges *consecutive* duplicates - which is why
IDLE's three identical open-eye frames were already encoding as one at 360ms -
but it cannot merge a cycle like REST's A,B,A,B. `visual_system.cycle_length`
now trims each icon to its real period before encoding:

| icon | was | now |
|---|---|---|
| companion/FOCUS | 4 frames, 100B | 1 frame, 73B |
| companion/ON_AIR | 4 frames, 102B | 1 frame, 75B |
| companion/REST | 4 frames, 234B | 2 frames, 148B |
| github/Octocat | 4 frames, 97B | 1 frame, 70B |
| github/gear | 4 frames, 271B | 2 frames, 155B |

Every cycle time is unchanged and every frame duration is still a multiple of
the 120ms beat. FOCUS and ON_AIR being stills is now explicit in the encoding
rather than disguised as an animation.

**983 tests pass.** The four new guards were each falsified against the
behaviour they replace - including the frame guard, whose first falsification
attempt was itself wrong: it patched `cycle_length`, the function the
assertion calls, which made the assertion trivially true. Patching only the
builders' bindings, it fails with `companion/rest ships 4 frames but repeats
after 2`.

## Seventh pass - the fonts, all of them

The open findings said `1px` glyphs remained "in the three fonts this work
does not own". Sweeping every font in the repository instead of the three I
had noticed found something worse, and in the font these four scenes actually
use.

**`0` and `O` were byte-identical in every font here.** Including
`visual_system.FONT_3X5` - the one I had already been through twice, fixing
`1`/`I` and widening `M` and `W`, without ever comparing the digit zero to the
letter O. Eight fonts, the same collision in each.

The fix is the convention `now_playing_push` was already using: keep the digit
square and round the letter, `("010","101","101","101","010")`. It costs no
width, so no lane budget moves, and in `visual_system` it also dissolves
`O~8`, `O~G` and `O~U`, which were each a single pixel apart. That font now
has **no identical pairs at all** and its near-collisions drop from nine to
six.

Two more identical pairs went with it: `1`/`I` in `next_up_preview` (both bare
one-pixel columns, so a title reading `1ST` drew exactly the pixels of one
reading `IST`), and `5`/`S` in `preview_sunset`, fixed with a rounded S that
also separates it from `6` and `9`.

**One fix was not affordable and was reverted.** Widening `I` in
`weather/aqi_brief_preview` pushes `AQI 126` to 25px against that app's 23px
lane - and that module *raises* on overflow rather than clipping, which is the
behaviour the four scenes here lacked and the reason their faults shipped
unseen. Its `1` is already three pixels wide, so `I` is not ambiguous there.
It keeps the single column, with a comment saying why.

**The completeness guard earned its keep immediately.** `test_font_legibility`
checks not just that no font has two identical glyphs, but that no module
declaring glyphs escapes the list. It failed on first run against two modules
my own sweep had missed - `now_playing_push`, which is in the live rotation,
and `pc_alert_native_concepts`. The first turned out to be the only font in
the repo that was already correct. The second had `2` and `3` byte-identical
in a 3x3 font whose entire job is rendering two-digit values, so `23`, `22`
and `33` all drew the same pixels.

`test_font_is_proportional_not_fake_fixed_width` in `test_next_up_preview` was
**deliberately updated** - the fifth such change, all stated. It asserted
`text_width("III") == 5`, which only held while `I` was a bare column. The
intent, that the font is proportional, is unchanged; punctuation is the narrow
exemplar now, and the test additionally asserts `1` and `I` differ.

Verified after the change: widest next_up ETA 15px, widest focus timer 17px,
every companion label unmoved (`FOCUS` 19, `ON AIR` 21), all against a 23px
lane with a 21px practical budget. **986 tests pass.**

## What is still open

- The Octocat silhouette and the one-pixel PR badge tiers, both unreachable.
- FOCUS and ON_AIR are stills. Honest ones now, but still frozen next to
  scenes that move.
- `next_up_preview` keeps `H~K`, `H~M`, `H~W`, `K~X`, `M~N`, `N~W` a single
  pixel apart, the same ambiguity that justified widening `M` and `W` in
  `visual_system`. It is not the same case: that font widens a repo *initial*
  rotating alone through one slot, where there is no context to read from,
  while these are letters inside whole words. Widening them would cost two
  pixels each out of a title budget that is already the binding constraint.
- The REST mug still sits inside the head.
- **The three reachability gaps are unchanged**: no `mood_detector`, no
  commute `estimator`, no call to `build_pr_payload`. Wiring them changes what
  the panel displays, which is a product decision rather than a visual one.

final result: passed in preview, unverified on hardware

## 2026-10-03 visual pass (live panel review)

Method: every page brought forward with `PUT /apps/active` and captured from
`GET /display/screen` (4 frames each), so the review is of what the panel
draws, not of previews. The user picked the pages to work on.

| Page | Problem seen | Change |
| --- | --- | --- |
| Time (twice per loop) | White 8x7 calendar tile was the brightest thing on the panel; the clock came second | Settings: tried slate (C), then set to the user's pick D: `calendarBodyColor #C4EFFF`, `calendarTextColor #202836` (header unchanged). Old values saved in `%LOCALAPPDATA%\AWTRIX\backups\settings-before-calendar-20261003.json` |
| Weather | Gold 2x2 degree mark read as a second sun; full-strength rainbow rail was busy | Degree in MIST; rail full colour up to the needle, the rest of the day's range at 35% |
| Sky Watch | Diagonal headings drew a one-pixel "7" (airliner) | Diagonals redrawn mirror-symmetric across the fuselage; test enforces the symmetry |
| Quake Watch | Strong-quake frames jumped 0 -> 6 between columns and filled into bars | Wave profiles step at most 2-3 rows per column, so the trace stays a line |
| Desk Companion | Filled pale head was the brightest block on its page; focus mood never moved | Outlined face, dark inside; every mood animates (blink, glasses glint, talking, steam, z) |
| Moon, Sunset | Reviewed; no defect worth changing | none. Moon had been made night-only earlier the same day; the user missed it, so it is back in the loop all day (v2.3) |
| AQI | Three drifting gusts wrapped round the 8px edge and broke into fragments every frame; a bare number | IQAir-style face in the category colour: smile, flat, uneasy, frown, frown (purple), mask; blinks, with a highlight travelling the ring. Lane reads "AQI 72" when the label fits |
| Next Up | Titles shortened to a lone initial ("D 45M") | Labels under three letters are dropped. Only 10px sit beside the countdown, so in practice the lane is the countdown; hub `aliases` remain for naming |

Still open: Ocean reads muddy in captures, but the panel's gamma 2.2 changes
dark colours, so it needs judging on the real LEDs before any change.

## 2026-10-03 rebuild to the Now Playing standard

The user rejected the flat calm-glance icons ("it still sucks... I want
everything at the Now Playing level"). Every pushed page was redrawn,
previewed as an animated GIF, and approved before going live.

| Page | Now |
| --- | --- |
| Rails (all pages) | Fill rising into a PEARL leader, tinted (never grey) track |
| AQI | Two-gust wind glyph in the category colour, comets running the gusts; "AQI 72" lane, EPA-graded rail. (Rejected first: an IQAir-style face.) |
| Weather | Sun with rotating ray glow; shaded crescent with twinkling stars; three-tone clouds with an edge comet; drops with heads and tails; storm cloud lit by the bolt |
| PC stats | CPU with a load-coloured glowing core and pin comet; RAM with contact comet; GPU with a spinning fan |
| Game mode | Shaded controller, all buttons lit, presses flash |
| Planes | Red/green wingtip lights, tail strobe, brighter fuselage spine |
| Calendar | Solid page, urgency-coloured header, comet orbiting the date |
| Focus | Pomodoro tomato with a dial bead; shaded mug with steam |
| Notifications | Shaded envelope with edge comet, chat bubble, swinging bell |
| Companion | Round creature: big eyes, breathing, glances, blinks; moods are props. (Rejected first: a blue block with tiny eyes.) |
| Quake | Shockwave rings from a bright epicentre. (Rejected first: a scrolling seismograph.) |
| ISS | Textured arrays, glint, twinkling stars |
| Network | One Wi-Fi glyph whose motion shows how far the signal gets |
| Time page | Calendar tile option D (ice body, dark date) |


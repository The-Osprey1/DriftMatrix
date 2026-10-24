# Sunset page - design QA

---

# v3.0 - the redesign

v2 shipped and worked, but it looked like a diagram of a sunset rather than a
sunset. Five independent art directions were generated and judged before any
code changed; all five, without collaborating, reached the same four
conclusions, which is the strongest signal in this record:

1. **Delete the 16/16 split.** A permanent readout column is self-inflicted.
2. **Three colour stops, not two.** A top-to-horizon lerp makes rows 1-5 one
   flat slab, which was v2's single worst visual failure.
3. **Light clouds from underneath.** The lit belly is why a sunset photograph
   works, and it costs one extra row per deck.
4. **Make text a tenant of time, not of columns.** The glyphs cost 5 of 8 rows
   whatever you do; the fix is for them not to be on screen most of the time.

## What changed

| | v2 | v3 |
| --- | --- | --- |
| sky | 2 stops, flat rows 1-5 | 3 stops + checkerboard dither |
| scene width | 16 columns | all 32 |
| sun travel | x3..x12 | x2..x27, refraction-flattened |
| clouds | 1-row streaks, uniform | 2 ragged decks, counter-drifting, lit by distance from the sun, draining to silhouette after sunset |
| readout | always on, 16 cols | hidden 5 of every 9 seconds, fades in over the scene with a 1px shadow |
| countdown when text is hidden | nothing | row 7 doubles as a faint progress bar |

**Dither.** Eight rows can only hold eight tones, so any gradient bands. Each
row draws one `line()` at its base tone plus a checkerboard of the next tone -
16 extra pixels per row, not 32 - which doubles the apparent depth.

**Motion.** The sun crosses 25 columns in 75 minutes, about one column every
three minutes, so it can never appear to move inside a nine-second visit. That
is a property of the data, not a bug, and no amount of sub-pixel interpolation
fixes it. All perceptible motion therefore comes from `now_ms()`: the two decks
drift in **opposite** directions, and each column's warmth falls off with
distance from the sun, so a deck ignites as it slides past and cools as it
leaves. That last one is both the animation and the physics.

## Findings

- **P1, fixed - the disc collapsed into a bar of light.** The first refraction
  squash scaled the vertical falloff by 55% per row below row 3, which flattened
  the sun to a single row. Real refraction flattens a low sun by about a third;
  the squash is now 16% per row, capped at 150%.
- **P1, fixed - the progress bar read as a pasted-on widget.** At 34% lift it
  was a bright bar competing with the sky. At 15% it reads as ground that
  happens to be warmer where the sun has already been.
- **P2, fixed - azimuthal falloff was too aggressive.** At 6% per column with a
  floor of 20, clouds more than a few columns from the sun went invisible. Now
  4% with a floor of 45.
- **Rejected: crepuscular rays.** Attempted twice, at two intensities. At this
  resolution they read as diagonal noise near the sun, not as rays, and they
  cost heap. Dropped rather than shipped weak.
- **Rejected: a terrain ridgeline.** On eight rows a one-row hill is 12.5% of
  the frame, so hills punch black rectangles into the horizon burn - the best
  part of the image. Row 7 stays flat.
- **Rejected: the green flash.** Real, and two directions proposed it, but a
  lone green pixel on an 8-row panel reads as a dead pixel, not as physics.

## Heap cost, and one regression it caused

v3 costs **19,613 bytes of VM heap against v2's 16,859** - about 2.8 KB for the
richer art. That mattered: at the moment `timepiece` was also resident the panel
sat at 78%, and `nowplaying` began reporting `out of memory`. Its PC bridge
re-reads the installed script to decide whether to rewrite it, and a heap-tight
panel cannot serve a 10 KB body (finding §1), so the bridge rewrote it and the
rewrite could not allocate. A reboot restored it.

Two things were given back rather than shipping over the line:

- The Newton-iteration integer square root became an eight-entry lookup table,
  the approach v2 already used. Same curve, no loop, no method.
- The twilight stars were cut. Three pixels are not worth the margin.

`timepiece` was later removed from the panel by its author, which restored the
headroom independently. Final: **69,845 of 98,304, 71%.**

## Verification

Prototyped and iterated in Python across seven passes before any Berry was
written, then ported and confirmed on hardware:

```
[script:sunset] vm heap +19613 bytes (shared 69845)   71% of 98304
fps 42 / 42 / 35 across apps      nowplaying, moon, nightkeeper, sunset all null
sunset: ok set=1171
```

The draw path was verified on the real panel by installing a temporary build
that swept `t` across the whole window and forced a cloud scenario, since the
natural window had already closed for the day. The live framebuffer confirmed
the ragged decks, the lit undersides, the three-stop sky, and the readout fade
- and showed the clouds read *better* on physical LEDs than in preview.

The per-pixel dither costs nothing measurable: 42 fps, unchanged.

---


## Evidence

- Contact sheets, nine moments each at 32 x 8, nearest-neighbour 14x:
  `sunset_epic.png` (q=95), `sunset_clear.png` (q=32), `sunset_blocked.png` (q=24)
- Readout cycle held at one moment: `sunset_readout.png`
- Full descent, one frame per minute: `sunset_descent.gif`
- Renderer: `preview_sunset.py`, an exact integer mirror of `sunset.ax`
- Logic: `test_sunset_logic.py`, 35 checks

## Normalization and state

- Target viewport: one physical 32 x 8 RGB LED matrix.
- Moments compared: T-75, T-50, T-30, T-15, T-5, sunset, T+8, T+18, T+26.
- Cloud profiles are `(low, mid, high)` percentages at the hour containing
  sunset: epic `(0, 10, 50)`, clear `(0, 0, 0)`, blocked `(85, 40, 55)`.
- All three sheets use the same crop, scale, background, moments and readout
  phase, so only the sky model differs between them.
- Source and implementation share one 32 x 8 logical density; the review scale
  is 14x on both.

## Layout

```
y0      cirrus, full width (second layer x0..15 only), stars in twilight
y0-6    sky gradient, top colour to horizon colour
y1-5    sun disc and halo in x0..15   |   readout in x16..31
y6      horizon burn; low cloud bank draws here when it exists
y7      land
```

The scene owns x0..15 and the readout owns x16..31. The sun's x is capped at 8
table units so its widest halo pixel lands on x=15, which means neither element
is ever drawn over the other and no occlusion rule is needed.

## Findings

Two P1 issues were found and corrected during review; no P0 remains.

- **P1, fixed - cirrus was lit from the wrong side.** The first implementation
  tinted the streaks from the top-of-sky colour, so they rendered as a flat grey
  bar that never changed through the window. Cirrus is lit from *underneath* by
  light already below the horizon, so the streaks now take the horizon hue and
  visibly burn from pale blue through gold to salmon as the sun drops.
- **P1, fixed - the sun read as a smudge.** A squared elliptical falloff alone
  produced no solid centre. Lifting the result by 1.6x and clamping creates a
  3 x 3 core with an intact soft halo, so it reads as a disc at every height.
- **P2, fixed - the quality model under-penalised a blocking deck.** 85% low
  cloud with good cirrus above originally scored 49 ("GOOD"), which is wrong:
  a deck on the western horizon blocks the under-lighting whatever sits above
  it. The low-cloud penalty slope went from 0.9 to 1.6 per percent above 50,
  and that case now scores 24 ("DULL").
- Fonts and typography: the readout is the panel's native font at baseline 5,
  so glyphs occupy rows 1..5 and sit directly on the horizon burn.
- Spacing and layout rhythm: verified by test across every minute of the window
  and every label the page can produce - no label exceeds the 16px readout, and
  the sun halo never reaches x=16.
- Colours and visual tokens: seven keyframes interpolate two anchor colours;
  row weights `[0,5,12,22,36,58,100]` keep rows 1..5 dark enough that cream ink
  holds at least 60 luminance of contrast against the brightest sky the model
  can produce. Tested across the whole window.
- Motion and behaviour: the descent is monotonic in both axes, the disc is
  below the land at every t >= 0, and the sky's total luminance at T+28 is under
  a third of its value at T-70.
- Copy and content: three-phase readout, 3s each, over a 9s app duration, so
  exactly one full cycle shows per appearance. Before sunset it reads countdown,
  sunset clock, verdict; after sunset it reads DUSK, minutes to dark, dark time.
- Accessibility and polish: a poor forecast desaturates the whole gradient
  toward its own luminance, so the panel agrees with the window rather than
  contradicting it with a cheerful sky next to a "DULL" caption.

## Comparison history

- Pass 1: rendered the nine-moment sheet for the epic profile. Rejected. The
  cirrus was a grey slab, the sun was a blur, and the readout crowded the scene.
- Correction 1: rebalanced the split from 12/20 to 16/16, widened the sun's
  travel from 5 to 9 columns, added the disc lift, relit the cirrus from the
  horizon, and shortened the verdicts to four letters so they fit the half.
- Pass 2: re-rendered all three profiles plus the readout cycle. The epic and
  blocked sheets are distinguishable at a glance at the same moment, which was
  the goal of showing the deck rather than only naming it.

## Residual P3 notes

- The preview font is a fixed 3 x 5 cell on a 4px advance, the convention
  `preview_moon.py` set. The firmware font is proportional, so glyph shapes are
  indicative and centring is accurate to about one pixel. Layout conclusions
  hold; exact glyph rendering is a panel-only check.
- Civil dusk is derived from day length rather than fetched, which is accurate
  to a couple of minutes at this latitude across the year.
- The countdown resolves to whole minutes, so the disc steps rather than glides
  through its final descent. Sub-minute interpolation would need a `second()`
  call that has not been confirmed on this firmware.

## Implementation checklist

- [x] Keep the scene and the readout in disjoint column ranges.
- [x] Verify every label fits the readout at every minute of the window.
- [x] Keep cream ink legible against the brightest sky the model can produce.
- [x] Show a poor forecast in the sky, not only in a word.
- [x] Keep the page hidden outside its window via `should_show()`.
- [x] Keep the page's own HTTP count at zero; read the shared feed only.
- [x] Stay inside the weather folder's script size guard.
- [x] **Live panel verification - installed and running.** See below.

## Live verification attempt, 2026-08-20

The panel became reachable at the end of this work (it had never been off; this
PC could not route to it - uptime was already 9.6 days, `resetReason: poweron`).

**The page is installed and running.** Getting there took four attempts and
three design changes, each forced by a measured limit rather than a guess:

1. **v1.0 read `wx` + `wxfeed`.** Those two plus the page came to 24,736 bytes
   of source and would not fit the 98304-byte script heap; a clean boot with
   all seven scripts pushed `wxfeed` out of memory. Neither was installed on
   this panel anyway, so there was nothing to share with.
   **v2.0 is self-contained** and fetches the four fields it needs: 11,485
   bytes, one script instead of three.
2. **Compiling needs roughly `2 x source` of *contiguous* heap.** 11,125 bytes
   was refused (`largest contiguous block 10740`). `minify_ax.py` strips it to
   8,506 for upload while the readable source stays in the repo.
3. **HTTPS could not run.** With five scripts loaded the panel logged
   `script http: heap too tight for TLS (22944 free)` - it compiled, loaded and
   ran, but the handshake could not allocate. The endpoint is public,
   unauthenticated and read-only, so the page uses plain `http://`.

`moon` was removed to free its 23,452 bytes of VM heap, preserved verbatim as
`moon-panel.ax`. Final state, stable across a cold boot:

```
nowplaying -> null    ocean -> null    nightkeeper -> null    sunset -> null
vm heap 65,835 / 98,304 (67%)   minFreeHeap 39,488   fps 33
14:08:18 [script:sunset] sunset: ok set=1171 low=0 mid=0 high=100
```

That log line is the page fetching successfully nine seconds after boot:
sunset at 19:31, no low or mid cloud, 100% high cloud - which scores 68, `GOOD`.

Still unverified: the **draw path has never been seen on hardware**, because
the window had not opened yet at install time. Compile, instantiation, fetch,
parse and scoring are all confirmed; the gradient, sun, cirrus and readout are
confirmed only in preview.

Full detail in `../panel-log-2026-08-20.md`.

Panel state at the time of the attempt:

| Metric | Value |
| --- | --- |
| uptime | 829251 s (9.6 days) |
| freeHeapBytes | 32848, down from 36184 ninety seconds earlier |
| largestFreeBlockBytes | 17396, down from 18420 in the same window |
| minFreeHeapBytes | 5712 |
| installed scripts | `nowplaying`, `ocean`, `moon`, `nightkeeper` |

Two consequences worth carrying forward:

1. **No weather script is installed on this panel.** `wx`, `wxfeed`, `wxnow`,
   `wxsky`, `wxsun`, `wxrange`, `aqi`, `soon` and `moon`'s weather variant all
   return 404; weather is currently served by a *pushed* app, `weatherbrief`.
   `sunset.ax` reads `shared["wxfeed.data"]`, so it needs `wx` and `wxfeed`
   installed alongside it - three scripts, not one.
2. **Heap, not source size, is the real gate.** The device refused a 7.5 KB
   upload with 17 KB of contiguous free block, and the free block was visibly
   shrinking under the current app set. A reboot before installing is likely
   required, and the three-script weather stack may not coexist with
   `nowplaying` + `ocean` + `moon` without removing something.

final result: passed in preview, unverified on hardware

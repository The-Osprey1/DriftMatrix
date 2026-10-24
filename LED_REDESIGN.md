# Colorful LED redesign — 2026-10-07

Requested direction: **more colorful and animated**. This implementation is
ready for visual review. No sender was restarted and no new artwork was sent
to the physical panel during this pass.

## What changed

- Weather: amber/coral sunshine, blue/lavender moonlight, ice-blue clouds with
  violet undersides, moving crown highlights, falling rain, recognizable snow
  and sleet, bounded fog drift, and a continuously visible lightning bolt.
- Air quality: category-colored wind with complementary curl accents and
  moving pearl highlights. Shared GIF color tables preserve the pixels and
  reduce the animated page from 1,967 to 1,603 bytes. The sender's preview now
  depicts its actual wind artwork rather than the retired lungs design.
- Desk Companion: five coordinated mood palettes, deeper shading, eight
  distinct poses per mood, expressive glasses/mug/headset/nightcap details,
  and a dim decorative ribbon. The ribbon is not a progress indicator.
- PC alerts: colorful CPU, RAM, and GPU artwork with repeating activity
  highlights and fan motion. Numeric values and load rails stay still.

All animation remains inside 8×8 icons. The black separator column and
23×8 value lane are preserved. Animations use multiples of the panel's
120 ms cadence; static fallback and existing recovery logic remain in place.
This pass does not replace the resident Moon, Ocean, or Now Playing scripts.

## Review artifacts

- `figures/led-redesign/redesign.gif`: six-screen animated overview.
- `figures/led-redesign/redesign.png`: a still of the same overview.
- `figures/led-redesign/index.html`: self-contained before/after gallery with
  scene filters, pause/play, reduced-motion support, and a one-bit silhouette
  view. Open this saved HTML file manually in a browser.
- `figures/led-redesign/before.json` and `after.json`: exact decoded source
  pixels, GIF timings, loop settings, and measured payload sizes.

These are offline previews with illustrative values. The comparison gallery
decodes the real GIF and static bitmap payloads in their draw order. It shows
source colors and a threshold model, not a calibrated photograph of the LEDs.
The browser UI itself could not be exercised: local preview server startup
was blocked by automatic approval review, and browser policy blocks file URLs.

To regenerate the artifacts after a source change:

```powershell
& 'C:/Users/devyn/anaconda3/python.exe' -X utf8 led_redesign_preview.py --before figures/led-redesign/before.json
```

## Rollback evidence

- Source/configuration/task backup:
  `C:/Users/devyn/AppData/Local/AWTRIX/backups/visual-redesign-20261006-230148`
- Read-only panel snapshot, including all four exact resident script sources:
  `C:/Users/devyn/AppData/Local/AWTRIX/backups/panel-snapshot-20261006-230236`

The source backup includes pre-existing uncommitted stability work. Preserve
that work when deploying or reverting the redesign; do not reset the checkout
to HEAD as a visual rollback.

## Release step

The project visual rule in `CLAUDE.md` calls for review of the animated design
before it goes live. After approval, refresh rollback evidence, restart only
the affected senders/Hub, verify one process per service, inspect device/app
health and payload delivery, and confirm the physical appearance with the
user. Software tests and payload delivery do not establish long-term hardware
stability or prove what the physical LEDs look like.

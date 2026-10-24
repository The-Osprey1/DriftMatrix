"""Contract tests for the PC-pushed Jewelbox Weather Brief.

These tests deliberately exercise the wire artifact, not only helper return
values.  The sender is allowed to change its internal structure, but the panel
must still receive one tiny, truthful 32x8 composition: an animated 8x8 icon
and a static 23x8 information lane.  No test in this module contacts the panel
or Open-Meteo.
"""

from __future__ import annotations

import ast
import base64
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path
import sys
import json
import unittest

from visual_system import TRACK as GLANCE_TRACK
from unittest.mock import Mock, patch

from PIL import Image
import requests


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import weather_brief_preview as preview  # noqa: E402
import weather_brief_push as app  # noqa: E402


NOW = datetime(2026, 8, 20, 12, 12, tzinfo=timezone.utc)


def open_meteo_payload(
    probabilities: list[int] | None = None,
    precipitation: list[float] | None = None,
    weather_codes: list[int] | None = None,
) -> dict[str, object]:
    """Return a realistic, deterministic Open-Meteo minutely_15 response."""

    probabilities = probabilities or [5, 10, 15, 20, 25, 20, 10, 5]
    precipitation = precipitation or [0.0] * 8
    weather_codes = weather_codes or [2] * 8
    return {
        "latitude": 33.87529,
        "longitude": -117.56644,
        "timezone": "UTC",
        "current": {
            "time": "2026-08-20T12:12",
            "temperature_2m": 72.4,
            "weather_code": 2,
            "is_day": 1,
        },
        "current_units": {
            "temperature_2m": "\N{DEGREE SIGN}F",
            "weather_code": "wmo code",
            "is_day": "",
        },
        "minutely_15": {
            "time": [
                "2026-08-20T12:00",
                "2026-08-20T12:15",
                "2026-08-20T12:30",
                "2026-08-20T12:45",
                "2026-08-20T13:00",
                "2026-08-20T13:15",
                "2026-08-20T13:30",
                "2026-08-20T13:45",
            ],
            "precipitation_probability": probabilities,
            "precipitation": precipitation,
            "weather_code": weather_codes,
        },
        "minutely_15_units": {
            "precipitation_probability": "%",
            "precipitation": "inch",
            "weather_code": "wmo code",
        },
        "daily": {
            "time": ["2026-08-20"],
            "temperature_2m_max": [84.1],
            "temperature_2m_min": [63.3],
            "weather_code": [2],
            "sunrise": ["2026-08-20T05:16"],
            "sunset": ["2026-08-20T18:31"],
        },
        "daily_units": {
            "temperature_2m_max": "\N{DEGREE SIGN}F",
            "temperature_2m_min": "\N{DEGREE SIGN}F",
        },
    }


def field(value: object, name: str) -> object:
    """Read one public result field from either a dataclass or a mapping."""

    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def gif_frames(data: bytes) -> tuple[list[Image.Image], list[int], int]:
    frames: list[Image.Image] = []
    durations: list[int] = []
    with Image.open(BytesIO(data)) as image:
        loop = int(image.info.get("loop", 0))
        for index in range(image.n_frames):
            image.seek(index)
            frames.append(image.convert("RGB").copy())
            durations.append(int(image.info.get("duration", 0)))
    return frames, durations, loop


def decode_lane(payload: dict[str, object]) -> bytes:
    draw = payload.get("draw")
    if not isinstance(draw, list) or len(draw) != 1:
        raise AssertionError("weather payload must have exactly one draw command")
    operation = draw[0]
    if not isinstance(operation, list) or operation[:5] != ["bitmap", 9, 0, 23, 8]:
        raise AssertionError(f"unexpected weather lane command: {operation!r}")
    if len(operation) != 6 or not isinstance(operation[5], str):
        raise AssertionError("weather lane must contain base64 RGB888 data")
    return base64.b64decode(operation[5], validate=True)


def wire_body_size(payload: dict[str, object]) -> int:
    body = requests.Request(
        "PUT", f"{app.AWTRIX.rstrip('/')}{app.PUSH_PATH}", json=payload
    ).prepare().body
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    assert body is not None
    return len(body)


def lit_components(image: Image.Image) -> list[set[tuple[int, int]]]:
    """Find 8-neighbour components after exact panel gamma/brightness grading."""

    lit: set[tuple[int, int]] = set()
    for y in range(image.height):
        for x in range(image.width):
            source = image.getpixel((x, y))
            graded = preview.grade_color(source, gamma=1.8, brightness=45, saturation=90)
            if graded != preview.BLACK:
                lit.add((x, y))

    components: list[set[tuple[int, int]]] = []
    while lit:
        seed = lit.pop()
        component = {seed}
        stack = [seed]
        while stack:
            x, y = stack.pop()
            for nx in range(x - 1, x + 2):
                for ny in range(y - 1, y + 2):
                    if (nx, ny) in lit:
                        lit.remove((nx, ny))
                        component.add((nx, ny))
                        stack.append((nx, ny))
        components.append(component)
    return components


class OpenMeteoParsingTests(unittest.TestCase):
    def test_exact_timestamps_produce_an_eighteen_minute_rain_eta(self) -> None:
        raw = open_meteo_payload(
            probabilities=[5, 10, 70, 80, 60, 20, 10, 5],
            precipitation=[0.0, 0.0, 0.02, 0.04, 0.01, 0.0, 0.0, 0.0],
            weather_codes=[2, 2, 61, 61, 51, 2, 2, 2],
        )
        scene = app.choose_scene(app.parse_weather(raw, now=NOW), now=NOW)
        self.assertEqual(field(scene, "kind"), "rain")
        self.assertEqual(field(scene, "eta_minutes"), 18)
        self.assertIn("18", str(field(scene, "text")))

    def test_eta_uses_returned_times_instead_of_assuming_array_offsets(self) -> None:
        raw = open_meteo_payload(
            probabilities=[5, 10, 15, 70, 80, 60, 20, 10],
            precipitation=[0.0, 0.0, 0.0, 0.02, 0.04, 0.01, 0.0, 0.0],
            weather_codes=[2, 2, 2, 61, 61, 51, 2, 2],
        )
        # Introduce an irregular timestamp: semantic time, not index*15, wins.
        minutely = raw["minutely_15"]
        assert isinstance(minutely, dict)
        times = minutely["time"]
        assert isinstance(times, list)
        times[3] = "2026-08-20T12:52"
        scene = app.choose_scene(app.parse_weather(raw, now=NOW), now=NOW)
        self.assertEqual(field(scene, "eta_minutes"), 40)

    def test_low_probability_dry_forecast_remains_current_conditions(self) -> None:
        raw = open_meteo_payload(probabilities=[29] * 8, precipitation=[0.0] * 8)
        scene = app.choose_scene(app.parse_weather(raw, now=NOW), now=NOW)
        self.assertEqual(field(scene, "kind"), "current")
        self.assertIsNone(field(scene, "eta_minutes"))
        self.assertIn("72", str(field(scene, "text")))

    def test_active_interval_is_rain_now_not_a_negative_eta(self) -> None:
        raw = open_meteo_payload(
            probabilities=[80, 80, 50, 20, 10, 5, 5, 5],
            precipitation=[0.05, 0.04, 0.01, 0.0, 0.0, 0.0, 0.0, 0.0],
            weather_codes=[61, 61, 51, 2, 2, 2, 2, 2],
        )
        scene = app.choose_scene(app.parse_weather(raw, now=NOW), now=NOW)
        self.assertEqual(field(scene, "kind"), "rain")
        self.assertEqual(field(scene, "eta_minutes"), 0)
        self.assertIn("NOW", str(field(scene, "text")).upper())

    def test_high_probability_alone_in_dry_current_slot_is_not_rain_now(self) -> None:
        raw = open_meteo_payload(
            probabilities=[90, 5, 5, 5, 5, 5, 5, 5],
            precipitation=[0.0] * 8,
            weather_codes=[2] * 8,
        )
        scene = app.choose_scene(app.parse_weather(raw, now=NOW), now=NOW)
        self.assertEqual(field(scene, "kind"), "current")
        self.assertIsNone(field(scene, "eta_minutes"))
        self.assertNotIn("NOW", str(field(scene, "text")).upper())

    def test_observation_age_boundary_accepts_45_minutes_and_rejects_46(self) -> None:
        fresh_enough = open_meteo_payload()
        current = fresh_enough["current"]
        assert isinstance(current, dict)
        current["time"] = "2026-08-20T11:27"
        snapshot = app.parse_weather(fresh_enough, now=NOW)
        self.assertEqual(
            field(app.choose_scene(snapshot, now=NOW), "kind"), "current"
        )

        stale = open_meteo_payload()
        current = stale["current"]
        assert isinstance(current, dict)
        current["time"] = "2026-08-20T11:26"
        with self.assertRaises(ValueError):
            snapshot = app.parse_weather(stale, now=NOW)
            app.choose_scene(snapshot, now=NOW)

    def test_mismatched_minutely_arrays_fail_closed_without_index_errors(self) -> None:
        raw = open_meteo_payload()
        minutely = raw["minutely_15"]
        assert isinstance(minutely, dict)
        minutely["precipitation_probability"] = [0, 10]
        try:
            snapshot = app.parse_weather(raw, now=NOW)
        except ValueError:
            return
        scene = app.choose_scene(snapshot, now=NOW)
        self.assertIn(field(scene, "kind"), ("current", "rain"))

    def test_missing_required_current_block_is_rejected(self) -> None:
        raw = open_meteo_payload()
        del raw["current"]
        with self.assertRaises(ValueError):
            app.parse_weather(raw, now=NOW)


class TimelineTests(unittest.TestCase):
    def test_timeline_is_exactly_twenty_three_deterministic_rgb_columns(self) -> None:
        probabilities = [0, 10, 30, 70, 100, 50, 20, 0]
        columns = app.timeline_columns(probabilities)
        self.assertEqual(columns, app.timeline_columns(probabilities))
        self.assertEqual(len(columns), 23)
        self.assertTrue(
            all(
                isinstance(color, tuple)
                and len(color) == 3
                and all(isinstance(channel, int) and 0 <= channel <= 255 for channel in color)
                for color in columns
            )
        )

    def test_timeline_keeps_now_on_the_left_and_future_on_the_right(self) -> None:
        early = app.timeline_columns([100, 0, 0, 0, 0, 0, 0, 0])
        late = app.timeline_columns([0, 0, 0, 0, 0, 0, 0, 100])

        def strongest_index(colors: object) -> int:
            assert isinstance(colors, (list, tuple))
            graded = [
                preview.grade_color(color, gamma=1.8, brightness=45, saturation=90)
                for color in colors
            ]
            return max(range(len(graded)), key=lambda index: sum(graded[index]))

        self.assertLess(strongest_index(early), 6)
        self.assertGreater(strongest_index(late), 16)

    def test_timeline_survives_panel_gamma_at_day_brightness(self) -> None:
        columns = app.timeline_columns([0, 10, 30, 70, 100, 50, 20, 0])
        graded = [
            preview.grade_color(color, gamma=1.8, brightness=45, saturation=90)
            for color in columns
        ]
        self.assertTrue(all(color != preview.BLACK for color in graded))
        self.assertGreater(len(set(graded)), 2)


class TemperatureRailTests(unittest.TestCase):
    """The rail's colour must encode the day, not the column index.

    Before this, `temperature_rail` built the same SKY->MINT->GOLD ramp from
    `index / (width - 1)` alone: `low`, `current` and `high` only moved the
    marker, so a 55-62F day and a 40-110F day drew a byte-identical bar.
    """

    def test_a_narrow_day_and_an_extreme_day_look_different(self) -> None:
        mild, _ = preview.temperature_rail(55, 60, 62)
        extreme, _ = preview.temperature_rail(40, 75, 110)
        self.assertNotEqual(mild, extreme)

    def test_a_narrow_span_stays_within_one_colour_family(self) -> None:
        mild, _ = preview.temperature_rail(55, 60, 62)
        spread = max(max(c) - min(c) for c in zip(*mild))
        self.assertLess(spread, 60, "a 7F day should not sweep the whole scale")

    def test_an_extreme_span_sweeps_cold_to_hot(self) -> None:
        extreme, _ = preview.temperature_rail(40, 75, 110)
        self.assertGreater(extreme[-1][0], extreme[0][0], "hot end should be redder")
        self.assertGreater(extreme[0][2], extreme[-1][2], "cold end should be bluer")

    def test_absolute_colours_are_stable_regardless_of_span(self) -> None:
        """65F is the same colour whether the day is narrow or wide."""

        narrow, _ = preview.temperature_rail(60, 65, 70, width=11)
        wide, _ = preview.temperature_rail(20, 65, 110, width=91)
        self.assertEqual(narrow[5], wide[45])

    def test_marker_still_tracks_the_current_reading(self) -> None:
        _, low_marker = preview.temperature_rail(40, 40, 110)
        _, high_marker = preview.temperature_rail(40, 110, 110)
        self.assertEqual(low_marker, 0)
        self.assertEqual(high_marker, 22)


class SampleBinningTests(unittest.TestCase):
    def test_twenty_four_columns_divide_eight_samples_exactly(self) -> None:
        widths = [stop - start for start, stop in app.sample_bounds(8, 24)]
        self.assertEqual(widths, [3] * 8)

    def test_the_remainder_is_spread_not_dumped_on_the_last_bin(self) -> None:
        """Truncation gave [3,3,3,3,3,3,3,2]; the short bin was always last."""

        widths = [stop - start for start, stop in app.sample_bounds(8, 23)]
        self.assertEqual(sum(widths), 23)
        self.assertLessEqual(max(widths) - min(widths), 1)
        self.assertNotEqual(widths.index(min(widths)), len(widths) - 1)

    def test_bins_are_contiguous_and_cover_the_strip(self) -> None:
        for count in (3, 5, 8, 12):
            for width in (23, 24, 32):
                with self.subTest(count=count, width=width):
                    spans = app.sample_bounds(count, width)
                    self.assertEqual(spans[0][0], 0)
                    self.assertEqual(spans[-1][1], width)
                    for (_, stop), (start, _) in zip(spans, spans[1:]):
                        self.assertEqual(stop, start)


class OpenLayoutTests(unittest.TestCase):
    @classmethod
    def current_and_rain(cls) -> tuple[object, object]:
        current = app.choose_scene(
            app.parse_weather(open_meteo_payload(), now=NOW), now=NOW
        )
        rain = app.choose_scene(
            app.parse_weather(
                open_meteo_payload(
                    probabilities=[5, 10, 70, 80, 60, 20, 10, 5],
                    precipitation=[0.0, 0.0, 0.2, 0.4, 0.1, 0.0, 0.0, 0.0],
                    weather_codes=[2, 2, 61, 61, 51, 2, 2, 2],
                ),
                now=NOW,
            ),
            now=NOW,
        )
        return current, rain

    def test_selected_layout_keeps_the_gutter_and_open_lane_black(self) -> None:
        """x8 stays a deliberate 1px separator even though the timeline could use it.

        Giving the timeline the gutter would divide 8 samples into exactly 3
        columns each, but the icon would then touch the graph - which a viewer
        notices far more than one bin being a pixel narrower.
        """

        for scene in self.current_and_rain():
            frame = app.render_scene_frame(scene)
            with self.subTest(kind=field(scene, "kind")):
                self.assertTrue(all(frame[y][8] == preview.BLACK for y in range(8)))
                # No capsule roof: text floats in true black.
                self.assertTrue(all(frame[0][x] == preview.BLACK for x in range(9, 32)))

    def test_current_uses_temperature_needle_gauge(self) -> None:
        current, _ = self.current_and_rain()
        frame = app.render_scene_frame(current)
        # Row 7 is the continuous temperature rail (all columns lit).
        self.assertTrue(all(frame[7][x] != preview.BLACK for x in range(9, 32)))
        # Row 6 has exactly one needle pixel pointing down at the current reading.
        needle = [x for x in range(9, 32) if frame[6][x] != preview.BLACK]
        self.assertEqual(len(needle), 1)
        self.assertEqual(frame[6][needle[0]], preview.PEARL)
        self.assertEqual(frame[7][needle[0]], preview.PEARL)
        # Digits in PEARL; the degree mark in label grey so it reads as a
        # unit, not as a second gold sun beside the icon.
        text = {frame[y][x] for y in range(1, 6) for x in range(9, 32)} - {preview.BLACK}
        self.assertEqual(text, {preview.PEARL, app.MIST})
        # Left of the needle the rail is full colour; right of it, dimmed.
        x = needle[0]
        if x < 31:
            ahead = frame[7][31]
            self.assertLess(max(ahead), 140, "the range ahead of the needle should be dim")

    def test_rain_strip_is_two_rows_with_height_encoding_amount(self) -> None:
        """Row 7 always carries probability; row 6 rises only where it will pour.

        The old strip was one row plus a two-pixel PEARL "NOW" cap.  Two rows
        let height encode amount, so a 90%-chance drizzle and a 90%-chance
        downpour stop rendering identically - which they previously did.
        """

        _, rain = self.current_and_rain()
        frame = app.render_scene_frame(rain)
        self.assertTrue(all(frame[7][x] != preview.BLACK for x in range(9, 32)))
        raised = [x for x in range(9, 32) if frame[6][x] != preview.BLACK]
        # Anything raised on row 6 must also be lit on row 7 - no floating bars.
        self.assertTrue(all(frame[7][x] != preview.BLACK for x in raised))

    def test_rain_eta_renders_with_ice_unit_and_active_timeline(self) -> None:
        _, rain = self.current_and_rain()
        self.assertEqual(field(rain, "text"), "18M")
        frame = app.render_scene_frame(rain)
        text = {frame[y][x] for y in range(1, 6) for x in range(9, 32)} - {preview.BLACK}
        self.assertEqual(text, {preview.PEARL, preview.ICE})
        # Row 7 carries the full precipitation timeline.
        self.assertTrue(all(frame[7][x] != preview.BLACK for x in range(9, 32)))


class IconGifTests(unittest.TestCase):
    @classmethod
    def scenes(cls) -> tuple[object, object]:
        dry = app.choose_scene(app.parse_weather(open_meteo_payload(), now=NOW), now=NOW)
        rain = app.choose_scene(
            app.parse_weather(
                open_meteo_payload(
                    probabilities=[5, 10, 70, 80, 60, 20, 10, 5],
                    precipitation=[0.0, 0.0, 0.02, 0.04, 0.01, 0.0, 0.0, 0.0],
                    weather_codes=[2, 2, 61, 61, 51, 2, 2, 2],
                ),
                now=NOW,
            ),
            now=NOW,
        )
        return dry, rain

    @classmethod
    def all_condition_scenes(cls) -> tuple[object, ...]:
        scenes: list[object] = []
        precipitating = {"rain", "snow", "storm"}
        for icon_kind in ("clear", "part", "cloud", "fog", "rain", "snow", "storm"):
            for is_day in (False, True):
                is_precipitating = icon_kind in precipitating
                scenes.append(
                    app.WeatherScene(
                        kind="rain" if is_precipitating else "current",
                        text="NOW" if is_precipitating else "72\N{DEGREE SIGN}",
                        icon_kind=icon_kind,
                        is_day=is_day,
                        temperature_f=72.0,
                        low_f=63.0,
                        high_f=84.0,
                        eta_minutes=0 if is_precipitating else None,
                    )
                )
        return tuple(scenes)

    def test_every_canvas_is_a_looping_full_width_gif(self) -> None:
        """The canvas is the whole panel now, not an 8x8 corner.

        A full-width icon is blitted by the firmware as the background with
        text composited on top.  Only 8px and exactly 32px are safe widths -
        anything between is blitted last and paints over the text.
        """

        for scene in self.scenes():
            with self.subTest(kind=field(scene, "kind")):
                encoded = app.build_icon_gif(scene)
                self.assertTrue(encoded.startswith((b"GIF87a", b"GIF89a")))
                self.assertLessEqual(len(encoded), app.ICON_MAX_BYTES)
                with Image.open(BytesIO(encoded)) as source:
                    self.assertNotIn("transparency", source.info)
                frames, durations, loop = gif_frames(encoded)
                self.assertEqual(loop, 0)
                self.assertTrue(all(frame.size == (8, 8) for frame in frames))
                self.assertLessEqual(len(frames), app.ICON_MAX_FRAMES)

    def test_every_frame_delay_lands_on_the_120ms_beat(self) -> None:
        """The render tick is 24ms and GIF delays quantise to 10ms.

        They first coincide at 120ms.  Any other delay lands on a 2-tick /
        3-tick alternation, and that judder is the single thing that makes LED
        animation look cheap.
        """

        for scene in self.all_condition_scenes():
            with self.subTest(icon=field(scene, "icon_kind"), day=field(scene, "is_day")):
                _, durations, _ = gif_frames(app.build_icon_gif(scene))
                for duration in durations:
                    self.assertEqual(duration % 120, 0, f"{duration}ms judders")

    def test_the_icon_well_keeps_a_connected_silhouette(self) -> None:
        """Scoped to x0..7.

        The strip and the glyphs legitimately contain isolated pixels, but the
        weather symbol itself must never break into specks - that is what made
        the old set unreadable at a glance.
        """

        for scene in self.scenes():
            frames, _, _ = gif_frames(app.build_icon_gif(scene))
            for index, image in enumerate(frames):
                with self.subTest(kind=field(scene, "kind"), frame=index):
                    well = image.crop((0, 0, 8, 8))
                    components = lit_components(well)
                    self.assertTrue(components)
                    self.assertGreaterEqual(max(map(len, components)), 6)

    def test_only_one_system_moves_between_frames(self) -> None:
        """A GIF frame costs about 100 bytes, which buys 8-12 changed pixels.

        Budget and taste agree here: on 256 pixels, two things moving at once
        reads as noise rather than weather.  So a frame must change something,
        and must not change most of the panel.
        """

        for scene in self.scenes():
            with self.subTest(kind=field(scene, "kind")):
                frames, _, _ = gif_frames(app.build_icon_gif(scene))
                if len(frames) < 2:
                    continue
                self.assertGreater(len({frame.tobytes() for frame in frames}), 1)
                for before, after in zip(frames, frames[1:] + frames[:1]):
                    changed = sum(
                        before.getpixel((x, y)) != after.getpixel((x, y))
                        for y in range(8)
                        for x in range(8)
                    )
                    # A held frame is legitimate and nearly free once GIF delta
                    # encoding has run - the lightning flash depends on holding
                    # dark between strikes - so only the upper bound is checked.
                    self.assertLessEqual(changed, 40, "more than one moving system")

    def test_every_supported_condition_and_day_night_variant_is_panel_safe(self) -> None:
        for scene in self.all_condition_scenes():
            with self.subTest(icon=field(scene, "icon_kind"), day=field(scene, "is_day")):
                encoded = app.build_icon_gif(scene)
                self.assertLessEqual(len(encoded), app.ICON_MAX_BYTES)
                with Image.open(BytesIO(encoded)) as source:
                    self.assertNotIn("transparency", source.info)
                frames, durations, loop = gif_frames(encoded)
                self.assertEqual(loop, 0)
                self.assertTrue(1 <= len(frames) <= app.ICON_MAX_FRAMES)
                self.assertTrue(all(frame.size == (8, 8) for frame in frames))
                self.assertTrue(all(duration >= 120 for duration in durations))
                for image in frames:
                    # Total lit area, not one connected blob: fog is deliberately
                    # separated bars and drizzle is deliberately isolated pixels.
                    well = image.crop((0, 0, 8, 8))
                    lit = sum(1 for px in well.getdata() if px != (0, 0, 0))
                    self.assertGreaterEqual(lit, 8, "icon well is nearly empty")

    def test_static_safety_icons_are_one_frame_opaque_and_full_width(self) -> None:
        for scene in self.all_condition_scenes():
            with self.subTest(icon=field(scene, "icon_kind"), day=field(scene, "is_day")):
                encoded = app.build_icon_gif(scene, animated=False)
                self.assertLessEqual(len(encoded), app.ICON_MAX_BYTES)
                with Image.open(BytesIO(encoded)) as source:
                    self.assertNotIn("transparency", source.info)
                frames, _, _ = gif_frames(encoded)
                self.assertEqual(len(frames), 1)
                self.assertEqual(frames[0].size, (8, 8))
                components = lit_components(frames[0].crop((0, 0, 8, 8)))
                self.assertGreaterEqual(max(map(len, components)), 6)


class PayloadContractTests(unittest.TestCase):
    @classmethod
    def rain_scene(cls) -> object:
        return app.choose_scene(
            app.parse_weather(
                open_meteo_payload(
                    probabilities=[5, 10, 70, 80, 60, 20, 10, 5],
                    precipitation=[0.0, 0.0, 0.02, 0.04, 0.01, 0.0, 0.0, 0.0],
                    weather_codes=[2, 2, 61, 61, 51, 2, 2, 2],
                ),
                now=NOW,
            ),
            now=NOW,
        )

    def test_payload_is_an_eight_pixel_icon_plus_a_static_lane(self) -> None:
        """A full-width GIF works, and is deliberately not used.

        Measured on the panel, one resident full-width animation costs up to
        10 KB of the largest contiguous free block - the exact resource a Berry
        script install and a TLS handshake need.  Free heap was never the
        problem; contiguity was.
        """

        payload = app.build_payload(self.rain_scene())
        icon = base64.b64decode(payload["icon"], validate=True)
        frames, _, _ = gif_frames(icon)
        self.assertTrue(all(frame.size == (8, 8) for frame in frames))
        self.assertEqual(len(decode_lane(payload)), 23 * 8 * 3)

    def test_payload_is_small_self_expiring_and_does_not_wake_the_panel(self) -> None:
        payload = app.build_payload(self.rain_scene())
        self.assertEqual(
            set(payload),
            {"icon", "iconMode", "iconOffsetX", "draw", "backgroundColor",
             "durationMs", "lifetimeMs", "lifetimeExpiry"},
        )
        self.assertLessEqual(wire_body_size(payload), app.MAX_BODY_BYTES)
        self.assertEqual(payload["durationMs"], 7_000)
        self.assertEqual(payload["lifetimeMs"], 900_000)
        self.assertEqual(payload["lifetimeExpiry"], "remove")
        self.assertFalse(payload.get("wakeup", False))
        self.assertNotIn("stack", payload)
        self.assertNotIn("repeat", payload)
        for forbidden in ("text", "progress", "effect", "overlay", "wakeup"):
            self.assertNotIn(forbidden, payload)

    def test_payload_uses_a_black_background(self) -> None:
        payload = app.build_payload(self.rain_scene())
        self.assertEqual(payload["backgroundColor"], "#000000")

    def test_building_art_never_performs_network_io(self) -> None:
        scene = self.rain_scene()
        with patch.object(app.requests, "get") as get:
            with patch.object(app.requests, "put") as put:
                with patch.object(app.requests, "delete") as delete:
                    app.build_payload(scene)
        get.assert_not_called()
        put.assert_not_called()
        delete.assert_not_called()

    def test_all_condition_payloads_stay_inside_the_same_physical_box(self) -> None:
        for scene in IconGifTests.all_condition_scenes():
            with self.subTest(icon=field(scene, "icon_kind"), day=field(scene, "is_day")):
                payload = app.build_payload(scene)
                self.assertLessEqual(wire_body_size(payload), app.MAX_BODY_BYTES)
                icon = base64.b64decode(payload["icon"], validate=True)
                frames, _, _ = gif_frames(icon)
                self.assertTrue(all(frame.size == (8, 8) for frame in frames))

    def test_every_condition_leaves_real_headroom_under_the_body_cap(self) -> None:
        """8192 is the firmware's cap; staying far under it is the safety margin."""

        worst = max(
            wire_body_size(app.build_payload(scene))
            for scene in IconGifTests.all_condition_scenes()
        )
        self.assertLess(worst, app.MAX_BODY_BYTES // 2, f"worst payload {worst}B")

    def test_static_safety_payload_is_a_single_frame(self) -> None:
        for scene in IconGifTests.all_condition_scenes():
            with self.subTest(icon=field(scene, "icon_kind"), day=field(scene, "is_day")):
                animated = app.build_payload(scene, animated=True)
                static = app.build_payload(scene, animated=False)
                self.assertEqual(set(static), set(animated))
                self.assertEqual(static["lifetimeMs"], animated["lifetimeMs"])
                self.assertEqual(static["durationMs"], animated["durationMs"])
                self.assertLessEqual(wire_body_size(static), app.MAX_BODY_BYTES)
                frames, _, _ = gif_frames(
                    base64.b64decode(static["icon"], validate=True)
                )
                self.assertEqual(len(frames), 1)


class CadenceAndSafetyTests(unittest.TestCase):
    def test_refresh_and_lifetime_contract(self) -> None:
        self.assertEqual(app.AWTRIX, "http://192.168.1.90")
        self.assertEqual(app.APP_NAME, "weatherbrief")
        self.assertEqual(app.REFRESH_SECONDS, 300)
        self.assertEqual(app.LIFETIME_MS, 900_000)
        self.assertGreaterEqual(app.LIFETIME_MS, 2 * app.REFRESH_SECONDS * 1000)

    def test_source_contains_no_destructive_http_delete_path(self) -> None:
        source_path = ROOT / "weather_brief_push.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        destructive_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.lower() == "delete"
        ]
        self.assertEqual(destructive_calls, [])
        self.assertNotIn("DELETE_URL", source)

    def test_build_payload_is_deterministic_and_cannot_drive_http_animation(self) -> None:
        scene = app.choose_scene(
            app.parse_weather(open_meteo_payload(), now=NOW), now=NOW
        )
        first = app.build_payload(scene)
        second = app.build_payload(scene)
        self.assertEqual(first, second)
        # All motion lives inside the icon GIF and loops on the device, so the
        # sender never needs high-frequency PUT requests to animate.
        self.assertEqual(len(decode_lane(first)), 23 * 8 * 3)

    def test_open_meteo_query_requests_only_the_needed_two_hour_contract(self) -> None:
        params = app.request_params()
        self.assertEqual(
            params["current"], "temperature_2m,weather_code,is_day"
        )
        self.assertEqual(
            params["minutely_15"],
            "precipitation_probability,precipitation,weather_code",
        )
        self.assertIn("temperature_2m_max", params["daily"])
        self.assertIn("temperature_2m_min", params["daily"])
        self.assertEqual(params["forecast_minutely_15"], 8)
        self.assertEqual(params["temperature_unit"], "fahrenheit")
        self.assertEqual(params["precipitation_unit"], "mm")
        self.assertEqual(params["timezone"], "auto")

    def test_one_cycle_fetches_once_and_pushes_once(self) -> None:
        snapshot = app.parse_weather(open_meteo_payload(), now=NOW)
        with patch.object(app, "fetch_weather", return_value=snapshot) as fetch:
            with patch.object(app, "read_panel_health", return_value=None) as health:
                with patch.object(app, "push_payload") as push:
                    with redirect_stdout(StringIO()):
                        app.run(once=True)
        fetch.assert_called_once()
        health.assert_called_once()
        push.assert_called_once()

    def test_api_failure_never_pushes_or_deletes_stale_content(self) -> None:
        with patch.object(
            app, "fetch_weather", side_effect=requests.RequestException("offline")
        ):
            with patch.object(app, "push_payload") as push:
                with patch.object(app.requests, "delete") as delete:
                    with self.assertRaises(requests.RequestException):
                        with redirect_stdout(StringIO()):
                            app.run(once=True)
        push.assert_not_called()
        delete.assert_not_called()

    def test_panel_health_probe_is_read_only_and_avoids_the_framebuffer(self) -> None:
        session = Mock()
        session.get.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"uptimeSeconds": 1234, "resetReason": "POWERON"}),
        )
        health = app.read_panel_health(session, app.AWTRIX)
        self.assertEqual(health, app.PanelHealth(1234, "POWERON"))
        session.get.assert_called_once_with(
            f"{app.AWTRIX}{app.DEVICE_PATH}", timeout=app.REQUEST_TIMEOUT_SECONDS
        )
        self.assertNotIn("screen", session.get.call_args.args[0])
        session.put.assert_not_called()
        session.delete.assert_not_called()

    def test_uptime_decrease_is_the_only_reboot_signal(self) -> None:
        old = app.PanelHealth(500, "POWERON")
        self.assertTrue(
            app.panel_rebooted_since(old, app.PanelHealth(12, "SOFTWARE"))
        )
        self.assertFalse(
            app.panel_rebooted_since(old, app.PanelHealth(500, "UNKNOWN"))
        )
        self.assertFalse(
            app.panel_rebooted_since(old, app.PanelHealth(900, "UNKNOWN"))
        )
        self.assertFalse(app.panel_rebooted_since(None, old))
        self.assertFalse(app.panel_rebooted_since(old, None))

    def test_uptime_rollback_latches_the_second_push_to_one_static_frame(self) -> None:
        class StopLoop(Exception):
            pass

        snapshot = app.parse_weather(open_meteo_payload(), now=NOW)
        health = (
            app.PanelHealth(500, "POWERON"),
            app.PanelHealth(12, "PANIC"),
        )
        pushed: list[dict[str, object]] = []

        def record_push(session: object, base_url: str, payload: object) -> None:
            self.assertEqual(base_url, app.AWTRIX)
            assert isinstance(payload, dict)
            pushed.append(payload)

        with patch.object(app, "fetch_weather", return_value=snapshot):
            with patch.object(app, "read_panel_health", side_effect=health):
                with patch.object(app, "push_payload", side_effect=record_push):
                    with patch.object(app.time, "sleep", side_effect=[None, StopLoop()]):
                        with redirect_stdout(StringIO()):
                            with self.assertRaises(StopLoop):
                                app.run()

        self.assertEqual(len(pushed), 2)
        first_frames, _, _ = gif_frames(
            base64.b64decode(pushed[0]["icon"], validate=True)
        )
        second_frames, _, _ = gif_frames(
            base64.b64decode(pushed[1]["icon"], validate=True)
        )
        self.assertGreater(len(first_frames), 1)
        self.assertEqual(len(second_frames), 1)

    def test_only_an_unrequested_reboot_counts(self) -> None:
        old = app.PanelHealth(900, "POWERON")
        self.assertTrue(app.unplanned_reboot(old, app.PanelHealth(12, "panic")))
        self.assertTrue(app.unplanned_reboot(old, app.PanelHealth(12, "taskWatchdog")))
        self.assertFalse(app.unplanned_reboot(old, app.PanelHealth(12, "software")))
        self.assertFalse(app.unplanned_reboot(old, app.PanelHealth(12, " SOFTWARE ")))
        self.assertFalse(app.unplanned_reboot(old, app.PanelHealth(950, "panic")))
        self.assertFalse(app.unplanned_reboot(None, app.PanelHealth(12, "panic")))

    def test_repeated_panel_failures_latch_the_next_attempt_to_static(self) -> None:
        class StopLoop(Exception):
            pass

        snapshot = app.parse_weather(open_meteo_payload(), now=NOW)
        pushed: list[dict[str, object]] = []

        def fail_then_succeed(session: object, base_url: str, payload: object) -> None:
            assert isinstance(payload, dict)
            pushed.append(payload)
            if len(pushed) <= app.SAFE_MODE_FAILURES:
                raise RuntimeError("simulated panel rejection")

        sleeps = [None] * app.SAFE_MODE_FAILURES + [StopLoop()]
        with patch.object(app, "fetch_weather", return_value=snapshot):
            with patch.object(app, "read_panel_health", return_value=None):
                with patch.object(app, "push_payload", side_effect=fail_then_succeed):
                    with patch.object(app.time, "sleep", side_effect=sleeps):
                        with redirect_stdout(StringIO()):
                            with self.assertRaises(StopLoop):
                                app.run()

        self.assertEqual(len(pushed), app.SAFE_MODE_FAILURES + 1)
        for payload in pushed[: app.SAFE_MODE_FAILURES]:
            frames, _, _ = gif_frames(
                base64.b64decode(payload["icon"], validate=True)
            )
            self.assertGreater(len(frames), 1)
        final_frames, _, _ = gif_frames(
            base64.b64decode(pushed[-1]["icon"], validate=True)
        )
        self.assertEqual(len(final_frames), 1)



class RichIconTests(unittest.TestCase):
    """The 2026-10-03 icons: shaded, animated, and still inside the budget."""

    KINDS = ("clear", "part", "cloudy", "overcast", "fog", "rain", "drizzle", "snow", "sleet", "storm")

    def test_every_condition_day_and_night_fits_the_2kb_cap(self) -> None:
        import dataclasses
        base = app.choose_scene(app.parse_weather(open_meteo_payload(), now=NOW), now=NOW)
        for kind in self.KINDS:
            for day in (True, False):
                with self.subTest(kind=kind, day=day):
                    scene = dataclasses.replace(base, kind="current", icon_kind=kind, is_day=day)
                    body = json.dumps(app.build_payload(scene), separators=(",", ":"))
                    self.assertLess(len(body), 2_048)

    def test_icons_move_and_keep_their_silhouette_at_brightness_one(self) -> None:
        import weather_icons
        for kind in self.KINDS:
            for day in (True, False):
                with self.subTest(kind=kind, day=day):
                    frames, delay = weather_icons.frames(kind, day)
                    self.assertEqual(delay % 120, 0)
                    self.assertGreater(len({repr(f) for f in frames}), 1, "the icon never moves")
                    for frame in frames:
                        lit = [c for row in frame for c in row if c != (0, 0, 0)]
                        bright = [c for c in lit if max(c) >= 174]
                        self.assertGreaterEqual(len(bright), 0.6 * len(lit))

    def test_sun_rays_never_switch_off(self) -> None:
        import weather_icons
        frames, _ = weather_icons.frames("clear", True)
        for frame in frames:
            for x, y in weather_icons.SUN_RAYS:
                self.assertNotEqual(frame[y][x], (0, 0, 0))

    def test_exported_loops_keep_even_beats_without_frozen_frames(self) -> None:
        import dataclasses
        import weather_icons
        base = app.choose_scene(app.parse_weather(open_meteo_payload(), now=NOW), now=NOW)
        for kind in self.KINDS:
            for day in (True, False):
                with self.subTest(kind=kind, day=day):
                    source, beat = weather_icons.frames(kind, day)
                    scene = dataclasses.replace(base, icon_kind=kind, is_day=day)
                    with Image.open(BytesIO(app.build_icon_gif(scene))) as image:
                        self.assertEqual(image.n_frames, len(source))
                        decoded = []
                        for index in range(image.n_frames):
                            image.seek(index)
                            self.assertEqual(image.info["duration"], beat)
                            decoded.append(image.convert("RGB").tobytes())
                    for left, right in zip(decoded, decoded[1:] + decoded[:1]):
                        self.assertNotEqual(left, right, "animation freezes at a frame boundary")

    def test_fog_never_splits_its_bands_across_the_edges(self) -> None:
        import weather_icons
        frames, _ = weather_icons.frames("fog")
        for frame in frames:
            for y in (1, 3, 5, 7):
                xs = [x for x in range(8) if frame[y][x] != (0, 0, 0)]
                self.assertEqual(xs, list(range(min(xs), max(xs) + 1)))

    def test_storm_remains_recognizable_between_charge_peaks(self) -> None:
        import weather_icons
        frames, _ = weather_icons.frames("storm")
        for frame in frames:
            for (x, y), _tone in weather_icons.BOLT_PATH:
                self.assertGreaterEqual(max(frame[y][x]), 174)


if __name__ == "__main__":
    unittest.main(verbosity=2)

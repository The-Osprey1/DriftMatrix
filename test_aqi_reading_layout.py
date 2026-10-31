"""Output contracts for explicit AQI identity at every accepted reading."""
from datetime import datetime, timezone
import base64
import json
from pathlib import Path

import pytest

import firmware_compat
import led_redesign_preview as gallery
import visual_system as visual
from weather import aqi_brief_push as aqi

BASELINE = json.loads((Path(__file__).parent / "docs/aqi-reading/before.json").read_text())


def payload(value, animated):
    moment = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    sample = aqi.AQISnapshot(moment, "UTC", value, 12.0, (), ())
    endpoint = "offline://aqi-reading-test"
    firmware_compat.observe_device(endpoint, {"version": "1.2.2"})
    return sample, firmware_compat.prepare_payload(endpoint, aqi.build_payload(sample, animated=animated))


def lane(body):
    data = base64.b64decode(body["draw"][0][5], validate=True)
    assert len(data) == 23 * 8 * 3
    return [[tuple(data[(y * 23 + x) * 3:(y * 23 + x) * 3 + 3]) for x in range(23)] for y in range(8)]


def glyph_rows(text):
    # Read the established font data directly, independently of its placement helpers.
    return ["0".join(visual.FONT_3X5[c][y] for c in text) for y in range(5)]


@pytest.mark.parametrize("animated", [False, True])
def test_every_accepted_aqi_keeps_label_and_complete_value(animated):
    for value in range(501):
        sample, body = payload(value, animated)
        pixels = lane(body)
        digits = str(value)
        # Fixed-width numeric and AQI glyphs establish independent expected positions.
        label_x, value_x = {1: (3, 17), 2: (1, 15), 3: (0, 12)}[len(digits)]
        for text, x0, color in (("AQI", label_x, aqi.AQI_COLORS[sample.level]),
                                (digits, value_x, visual.PEARL)):
            for y, bits in enumerate(glyph_rows(text), 1):
                assert pixels[y][x0:x0 + len(bits)] == [color if bit == "1" else visual.BLACK for bit in bits], value
        assert all(pixels[y][value_x - 1] == visual.BLACK for y in range(1, 6)), value
        assert all(pixels[y][label_x + 11] == visual.BLACK for y in range(1, 6)), value
        assert all(p == visual.BLACK for p in pixels[0])
        assert all(p == visual.BLACK for p in pixels[6]), value
        assert len(json.dumps(body, separators=(",", ":")).encode()) < 2048


@pytest.mark.parametrize("animated", [False, True])
def test_icons_rail_metadata_and_short_readings_match_frozen_wire_baseline(animated):
    for case in BASELINE["cases"]:
        if case["animated"] != animated:
            continue
        value = case["value"]
        _, current = payload(value, animated)
        original = case["payload"]
        assert {k: v for k, v in current.items() if k != "draw"} == {k: v for k, v in original.items() if k != "draw"}
        before, after = lane(original), lane(current)
        assert after[7] == before[7]  # Exact severity rail, including saturation/leader.
        assert all(after[y] == before[y] for y in (0, 6))
        assert (current == original) == (value < 100)
        decoded = gallery.decode(current)  # Includes opaque frame composition and black-gutter checks.
        assert decoded["bytes"] < 2048 and decoded["iconBytes"] <= 1024
        assert all(delay % 120 == 0 for delay in decoded["durations"])


@pytest.mark.parametrize("animated", [False, True])
def test_other_page_payloads_remain_byte_exact(animated):
    endpoint = "offline://aqi-reading-other-test"
    firmware_compat.observe_device(endpoint, {"version": "1.2.2"})
    current = {key: firmware_compat.prepare_payload(endpoint, body)
               for key, _, group, body in gallery.examples(animated) if group != "Air quality"}
    for case in BASELINE["other_pages"]:
        if case["animated"] == animated:
            assert current[case["id"]] == case["payload"]


@pytest.mark.parametrize("value", [0, 50, 51, 100, 101, 150, 151, 200, 201, 300, 301, 500])
def test_label_value_and_gap_survive_existing_low_light_model(value):
    sample, body = payload(value, False)
    pixels = lane(body)
    color = aqi.AQI_COLORS[sample.level]
    assert visual.is_safe_at_low_brightness(color)
    assert visual.is_safe_at_low_brightness(visual.PEARL)
    modeled = [[pixel if max(pixel) >= visual.LOW_BRIGHTNESS_DOMINANT_MIN else visual.BLACK for pixel in row] for row in pixels]
    assert modeled[1:6] == pixels[1:6]


@pytest.mark.parametrize("gap", [0, -1, True, 1.5, None, "1"])
def test_spacing_rejects_invalid_geometry(gap):
    with pytest.raises(ValueError, match="label_gap"):
        visual.render_rich_lane("100", fraction=.3, stops=aqi.AQI_RAIL_STOPS, label="AQI", label_gap=gap)


@pytest.mark.parametrize("gap", [1, 3, 9])
def test_oversized_optional_labels_still_yield_to_complete_value(gap):
    frame = visual.render_rich_lane("500", fraction=1, stops=aqi.AQI_RAIL_STOPS, label="VERY LONG LABEL", label_gap=gap)
    plain = visual.render_rich_lane("500", fraction=1, stops=aqi.AQI_RAIL_STOPS)
    assert frame == plain

"""Readability, wire accuracy, cadence and safety of the app visual review."""
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
import pytest

import desk_companion
import icon_library
import led_redesign_preview as review
import pc_stats_push
from weather import weather_icons


@pytest.fixture(scope="module")
def scenes():
    return review.capture()


@pytest.mark.parametrize("index", range(26))
def test_all_scenes_preserve_values_and_real_static_fallback(index, scenes):
    entry = scenes[index]
    baseline = json.loads(Path(__file__).with_name("app_visual_lane_contract.json").read_text())
    def lane(frame):
        return [v for y in range(8) for v in frame[y * 32 + 9:y * 32 + 32]]
    # Whole-lane contracts protect numbers, labels and rails from icon/motion
    # edits. Six explicit AQI layout/palette restorations update their contract; old
    # hashes remain in the fixture and exhaustive glyph tests cover the layout.
    digest = hashlib.sha256(''.join(lane(entry['frames'][0])).encode()).hexdigest()
    assert digest == baseline[entry['id']]['lane_sha256']
    for variant in (entry, entry['static']):
        assert variant['bytes'] < 2048
        assert len(variant['frames']) == len(variant['durations'])
        for frame in variant['frames']:
            assert len(frame) == 256
            assert lane(frame) == lane(entry['frames'][0])
            assert all(frame[y * 32 + 8] == '000000' for y in range(8))
        assert all(delay > 0 and delay % 120 == 0 for delay in variant['durations'])
    assert len(entry['static']['frames']) == 1


def test_preview_understands_modern_image_prefix_and_counts_wire_bytes():
    payload = desk_companion.build_companion_payload(desk_companion.CompanionMood.IDLE)
    legacy = review.decode(payload)
    modern = review.decode({**payload, 'icon': 'data:image/gif;base64,' + payload['icon']})
    assert modern['frames'] == legacy['frames']
    assert modern['durations'] == legacy['durations']
    assert modern['bytes'] == legacy['bytes'] + 22


def test_companion_blink_is_brief_without_changing_total_cycle():
    assert sum(icon_library.COMPANION_FRAME_MS) == 1920
    assert icon_library.COMPANION_FRAME_MS[-1] == 120
    for mood in desk_companion.CompanionMood:
        data = desk_companion.build_companion_icon_gif(mood)
        with Image.open(BytesIO(data)) as gif:
            delays = []
            for i in range(gif.n_frames):
                gif.seek(i); delays.append(gif.info['duration'])
        assert tuple(delays) == icon_library.COMPANION_FRAME_MS


def test_gpu_has_six_distinct_rotor_poses_and_a_consistent_loop_seam():
    frames = [pc_stats_push._icon_frame('GPU', 96, phase).tobytes() for phase in range(6)]
    assert len(set(frames)) == 6
    changes = [sum(a != b for a, b in zip(frames[i], frames[(i + 1) % 6])) for i in range(6)]
    assert min(changes) > 0
    assert changes[-1] <= max(changes[:-1])  # No larger reset jump at loop end.


def test_rain_falls_on_each_beat_without_full_drop_flashes():
    frames, delay = weather_icons.frames('rain')
    assert delay == 120
    for i in range(len(frames)):
        frame, following = frames[i], frames[(i + 1) % len(frames)]
        assert [row[1] for row in frame[5:]] != [row[1] for row in following[5:]]
        assert all(frame[y][x] != weather_icons.RICH['snow'] for y in range(5, 8) for x in range(8))


def test_overcast_has_open_space_around_the_cloud_silhouette():
    frames, _ = weather_icons.frames('overcast')
    for frame in frames:
        assert all(any(pixel == (0, 0, 0) for pixel in row) for row in frame)

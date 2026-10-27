"""Wire-format regressions for the colourful companion animation."""

import base64
from io import BytesIO
import json

from PIL import Image
import pytest

import desk_companion as dc
import icon_library as icons
import visual_system as vs


@pytest.mark.parametrize("mood", list(dc.CompanionMood))
def test_each_sprite_pose_survives_the_shared_palette_encoder(mood):
    """No merged pauses, palette drift, or lost shading in the actual GIF."""
    source_frames = icons.companion_icon(mood)
    data = dc.build_companion_icon_gif(mood)
    with Image.open(BytesIO(data)) as gif:
        assert gif.size == (8, 8)
        assert gif.n_frames == icons.COMPANION_FRAMES
        assert "transparency" not in gif.info
        for phase, source in enumerate(source_frames):
            gif.seek(phase)
            assert gif.info["duration"] == icons.COMPANION_FRAME_MS[phase]
            expected = [px for row in source[:8] for px in row[:8]]
            assert list(gif.convert("RGB").getdata()) == expected
            # Include the loop seam, where an unnoticed duplicate adds a hold.
            assert source != source_frames[(phase + 1) % len(source_frames)]


@pytest.mark.parametrize("mood", list(dc.CompanionMood))
def test_animation_has_wire_headroom_and_static_fallback_matches(mood):
    """Keep 250+ bytes spare instead of filling the panel's 2 KB envelope."""
    for label in (None, "WATER", "STRETCH", "REST EYES"):
        moving = dc.build_companion_payload(mood, custom_text=label)
        frozen = dc.build_companion_payload(mood, custom_text=label, animated=False)
        assert len(json.dumps(moving, separators=(",", ":")).encode()) <= 1750
        assert len(base64.b64decode(moving["icon"])) <= 600
        assert {k: v for k, v in moving.items() if k != "icon"} == {
            k: v for k, v in frozen.items() if k != "icon"
        }
        with Image.open(BytesIO(base64.b64decode(moving["icon"]))) as animation:
            with Image.open(BytesIO(base64.b64decode(frozen["icon"]))) as still:
                assert still.n_frames == 1
                assert animation.convert("RGB").tobytes() == still.convert("RGB").tobytes()


def test_mood_ribbons_keep_state_identity_without_implying_progress():
    ribbons = []
    for mood in dc.CompanionMood:
        frame = dc.render_companion_lane(mood)
        rail = tuple(frame[7][vs.LANE_X0:])
        assert vs.read_meter(frame) == (0, None)
        assert vs.meter_fill(frame) == 0
        assert vs.PEARL not in rail
        assert len(set(rail)) > 1
        ribbons.append(rail)
    assert len(set(ribbons)) == len(dc.CompanionMood)

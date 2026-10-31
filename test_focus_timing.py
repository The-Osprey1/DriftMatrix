"""Frozen RGB/art and independent encoded-time checks for the Focus correction."""
import base64
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
import pytest

import firmware_compat
import focus_sprint_preview as focus
from information_preview import decode

BASELINE = json.loads((Path(__file__).parent / 'docs/focus-timing/before.json').read_text(encoding='utf-8'))
CASES = BASELINE['cases']
CHANGED = {'short_break_running': 360, 'long_break_running': 360, 'focus_done_cue': 240}


@pytest.mark.parametrize('case', CASES, ids=lambda c: f"{c['state']}-{'motion' if c['animated'] else 'still'}-{c['total']}")
def test_real_wire_preserves_all_rgb_frames_lane_metadata_and_loop_policy(case):
    inputs = {k: case[k] for k in ('state', 'animated', 'remaining', 'total', 'cycle')}
    endpoint = 'offline://focus-regression'
    firmware_compat.observe_device(endpoint, {'version': '1.2.2'})
    after = firmware_compat.prepare_payload(endpoint, focus.build_payload(**inputs))
    before = case['payload']
    assert {k: v for k, v in after.items() if k != 'icon'} == {k: v for k, v in before.items() if k != 'icon'}
    old, new = decode(before), decode(after)
    assert new['frames'] == old['frames']  # Decoded RGB, every pose, in order.
    assert new['loop'] == old['loop']
    assert new['bytes'] < 2048 and new['iconBytes'] <= 850
    assert all(frame[8::32] == ['000000'] * 8 for frame in new['frames'])
    raw = base64.b64decode(after['icon'].split(',')[-1], validate=True)
    focus.validate_icon_gif(raw)
    if case['animated'] and case['state'] in CHANGED:
        assert new['durations'] == [CHANGED[case['state']]] * len(new['frames'])
        assert after['icon'] != before['icon']
    else:
        assert after == before  # Includes all static/paused/ready and the work orbit.
    if len(new['frames']) > 1:
        assert all(delay > 0 and delay % 120 == 0 for delay in new['durations'])
    if case['state'] == 'focus_done_cue' and case['animated']:
        assert not new['loop'] and sum(new['durations']) == 1200
        assert new['frames'][0] == new['frames'][-1]  # Sparkle ends on the quiet check.


@pytest.mark.parametrize('duration', [0, 10, 180, 250, 300])
def test_validator_rejects_non_grid_encoded_delays_even_when_expected_matches(duration):
    images = [Image.new('RGB', (8, 8), color) for color in ('red', 'green')]
    stream = BytesIO()
    images[0].save(stream, format='GIF', save_all=True, append_images=images[1:],
                   duration=[duration, 240], loop=0, disposal=1)
    with pytest.raises(ValueError, match='positive 120 ms multiples'):
        focus.validate_icon_gif(stream.getvalue(), durations_ms=[duration, 240])


@pytest.mark.parametrize('duration', [120, 240, 360, 480])
def test_validator_accepts_grid_delays_independently_of_state_constants(duration):
    images = [Image.new('RGB', (8, 8), color) for color in ('red', 'green')]
    stream = BytesIO()
    images[0].save(stream, format='GIF', save_all=True, append_images=images[1:],
                   duration=[duration, duration], loop=0, disposal=1)
    focus.validate_icon_gif(stream.getvalue())


@pytest.mark.parametrize('state', sorted(CHANGED))
def test_frozen_old_bad_cadence_is_detected_without_supplied_expectations(state):
    case = next(c for c in CASES if c['state'] == state and c['animated'])
    with pytest.raises(ValueError, match='positive 120 ms multiples'):
        focus.validate_icon_gif(base64.b64decode(case['payload']['icon'].split(',')[-1]))


def test_fixture_covers_every_visible_state_and_both_motion_modes():
    visible = {s.value for s in focus.FocusState if s is not focus.FocusState.IDLE}
    assert len(CASES) == 16
    assert {(c['state'], c['animated']) for c in CASES} == {(s, a) for s in visible for a in (False, True)}
    assert len([c for c in CASES if c['state'] == 'break_paused' and c['total'] == 900]) == 2

@pytest.mark.parametrize('state,old_delay,new_delay', [
    ('short_break_running',300,360), ('long_break_running',300,360),
    ('focus_done_cue',180,240)])
def test_review_timeline_uses_encoded_delays_and_real_loop_or_hold_policy(state,old_delay,new_delay):
    from focus_timing_review import capture,frame_at
    case=next(c for c in capture() if c['inputs']['state']==state and c['inputs']['animated'])
    old,new=case['before'],case['after']
    assert frame_at(old,old_delay-1)==old['frames'][0]
    assert frame_at(old,old_delay)==old['frames'][1]
    assert frame_at(new,old_delay)==new['frames'][0]
    assert frame_at(new,new_delay)==new['frames'][1]
    for entry in (old,new):
        end=sum(entry['durations'])
        expected=entry['frames'][0] if entry['loop'] else entry['frames'][-1]
        assert frame_at(entry,end)==expected

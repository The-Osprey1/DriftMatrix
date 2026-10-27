"""Real wire-frame, timing and long-duration readability acceptance."""
import base64
from io import BytesIO
import json
from hashlib import sha256

from PIL import Image
import pytest

import desk_reminder
import firmware_compat
import focus_sprint_preview as focus
import information_preview as review
import next_up_preview as calendar
import visual_system as visual


@pytest.mark.parametrize('seconds,expected', [
    (0, '00:00'), (.01, '00:01'), (5999, '99:59'), (5999.1, '1H40'),
    (6000, '1H40'), (7200, '2H00'), (7500, '2H05'), (7500.1, '2H06'),
    (86400, '24H00'), (99 * 3600 + 59 * 60, '99H59'),
    (100 * 3600 - .1, '99H+'), (1e300, '99H+'),
])
def test_explicit_long_duration_labels_fit_both_font_contracts(seconds, expected):
    assert visual.format_remaining_time(seconds) == focus.format_countdown(seconds) == expected
    assert visual.measure_text(expected) <= 23
    assert focus.text_width(expected) <= 23


@pytest.mark.parametrize('seconds,expected', [(0, '0M'), (.01, '1M'), (5940, '99M'),
    (5940.1, '1H40'), (6000, '1H40'), (7500, '2H05'), (86400, '24H00')])
def test_reminder_units_round_up_without_throwing_away_minutes(seconds, expected):
    assert visual.format_remaining_time(seconds, show_seconds=False) == expected


@pytest.mark.parametrize('seconds', [float('nan'), float('inf'), -float('inf'), True])
def test_invalid_duration_is_rejected(seconds):
    with pytest.raises(ValueError):
        visual.format_remaining_time(seconds)


def test_duration_labels_bound_real_remaining_time_across_supported_day():
    for seconds in range(1, 86401, 37):
        label = visual.format_remaining_time(seconds)
        if ':' in label:
            minutes, secs = map(int, label.split(':'))
            represented, precision = minutes * 60 + secs, 1
        else:
            hours, minutes = map(int, label.split('H'))
            represented, precision = hours * 3600 + minutes * 60, 60
        assert seconds <= represented < seconds + precision


def test_long_reminder_keeps_exact_timer_and_alternating_name_in_exported_bitmap():
    saved = desk_reminder.Reminder('sample', 'TEA', 1000, 8500)
    first = review.decode(desk_reminder.build_payload(saved, 1000))
    later = review.decode(desk_reminder.build_payload(saved, 1010))
    expected = visual.render_glance_lane('2H05', meter=1)
    assert first['frames'][0] != later['frames'][0]
    assert first['frames'][0][9:32] == ['%02x%02x%02x' % p for p in expected[0][9:]]
    # Decode the whole text band, not just metadata or an invisible width claim.
    assert [first['frames'][0][y * 32 + x] for y in range(1, 6) for x in range(9, 32)] == [
        '%02x%02x%02x' % expected[y][x] for y in range(1, 6) for x in range(9, 32)]
    expected_name = visual.render_glance_lane('TEA', meter=7490 / 7500)
    assert [later['frames'][0][y * 32 + x] for y in range(1, 6) for x in range(9, 32)] == [
        '%02x%02x%02x' % expected_name[y][x] for y in range(1, 6) for x in range(9, 32)]


def test_calendar_wire_holds_eta_then_identity_and_roundtrips_exact_colors():
    payload = calendar.build_payload(title='Dentist appointment', minutes_until=45)
    with Image.open(BytesIO(base64.b64decode(payload['icon']))) as image:
        assert 'transparency' not in image.info
    frames = review.decode(payload)
    assert frames['durations'] == [480] * 10
    assert frames['loop']
    source = calendar.calendar_cards(calendar.coerce_event(title='Dentist appointment', minutes_until=45))
    for frame, expected in zip(frames['frames'], source):
        assert frame == ['%02x%02x%02x' % p for row in expected for p in row]
    lane = lambda frame: [frame[y * 32 + x] for y in range(8) for x in range(9, 32)]
    assert all(lane(frame) == lane(frames['frames'][0]) for frame in frames['frames'][:5])
    assert all(lane(frame) == lane(frames['frames'][5]) for frame in frames['frames'][5:])
    expected_title = visual.render_glance_lane('DNTST', meter=.75)
    assert lane(frames['frames'][5]) == ['%02x%02x%02x' % p for row in expected_title for p in row[9:]]
    assert lane(frames['frames'][0]) != lane(frames['frames'][5])
    assert all(frame[8::32] == ['000000'] * 8 for frame in frames['frames'])
    assert all(frame[7 * 32:] == frames['frames'][0][7 * 32:] for frame in frames['frames'])


@pytest.mark.parametrize('title', ['W' * 400, 'WW WW WWW', 'Dentist appointment',
    'Quarterly project design review', 'I I I I I I', '工作', '', 'A', 'LAB'])
def test_calendar_actual_font_bounds_and_wire_budget_for_difficult_titles(title):
    for urgency, minutes in [('routine', 99 * 1440), ('soon', 14), ('imminent', 1), ('now', 0)]:
        visual_event = calendar.coerce_event(title=title, minutes_until=minutes, urgency=urgency)
        assert visual.measure_text(calendar.card_title(visual_event.title)) <= 23
        payload = calendar.build_payload(visual_event)
        firmware_compat.observe_device('offline://test-cards', {'version': '1.2.2'})
        wire = firmware_compat.prepare_payload('offline://test-cards', payload)
        assert len(json.dumps(wire).encode()) < 2048
        assert len(base64.b64decode(payload['icon'])) <= 1024
        assert all(t % 120 == 0 for t in review.decode(wire)['durations'])


def test_calendar_safety_does_not_animate_or_require_full_canvas_gif():
    payload = calendar.build_payload(title='Design review', minutes_until=0, animated=False)
    with Image.open(BytesIO(base64.b64decode(payload['icon']))) as image:
        assert image.size == (8, 8) and image.n_frames == 1
    assert payload['draw'][0][:5] == ['bitmap', 9, 0, 23, 8]
    assert payload['lifetimeExpiry'] == 'remove'


def test_calendar_page_duration_and_lifetime_cover_readable_card_cycle():
    assert calendar.build_payload('LAB', minutes_until=1, duration_ms=1000)['durationMs'] == 4800
    with pytest.raises(ValueError, match='cycle'):
        calendar.build_payload('LAB', minutes_until=1, lifetime_ms=1000)


def test_preview_decodes_all_enabled_information_families_without_network(monkeypatch):
    import requests
    monkeypatch.setattr(requests.Session, 'request', lambda *a, **k: pytest.fail('offline preview requested network'))
    entries = review.capture()
    assert {e['group'] for e in entries} == {'Focus', 'Reminders', 'Calendar', 'PC alerts',
        'Aircraft', 'Earthquakes', 'Gaming', 'Miku'}
    assert len(entries) == 30
    assert all(entry['bytes'] < 2048 for entry in entries)
    assert all(len(frame) == 256 for entry in entries for frame in entry['frames'])
    assert all(entry['static'] is None for entry in entries if entry['group'] in {'Aircraft', 'Earthquakes', 'Gaming'})


def test_review_sizes_and_hashes_match_prepared_http_bodies_without_sending():
    import requests
    firmware_compat.observe_device('offline://wire', {'version': '1.2.2'})
    for _, _, _, animated, safety in review.samples():
        for source in (animated, safety):
            if source is None:
                continue
            wire = firmware_compat.prepare_payload('offline://wire', source)
            body = requests.Request('PUT', 'http://example.invalid', json=wire).prepare().body
            decoded = review.decode(wire)
            assert decoded['bytes'] == len(body)
            assert decoded['payload_sha256'] == sha256(body).hexdigest()

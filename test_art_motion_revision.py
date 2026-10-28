"""Actual-payload geometry and source-executed art/motion regressions; no devices."""
from pathlib import Path

import pytest

import icon_library as icons
import pc_stats_push as pc
from moon_review import MoonPreview
from now_playing_review import MediaPreview
from ocean_preview import OceanPreview
from weather import weather_icons as weather
from weather.minify_ax import minify
from weather.moon_panel_sim import R

ROOT = Path(__file__).resolve().parent


def source(path):
    return (ROOT / path).read_text(encoding='utf-8')


@pytest.mark.parametrize('kind', ['cloudy', 'overcast'])
def test_cloud_drift_preserves_the_whole_silhouette_without_wrapping(kind):
    frames, _delay = weather.frames(kind)
    shapes = [{(x, y) for y, row in enumerate(frame) for x, color in enumerate(row) if color != (0, 0, 0)}
              for frame in frames]
    left = [min(x for x, _y in shape) for shape in shapes]
    assert max(left) - min(left) == 1
    normalized = [{(x - offset, y) for x, y in shape} for offset, shape in zip(left, shapes)]
    assert all(shape == normalized[0] for shape in normalized)
    assert all(abs(a - b) <= 1 for a, b in zip(left, left[1:] + left[:1]))
    # The object, including every shadow cell, remains on the icon canvas.
    assert len({len(shape) for shape in shapes}) == 1


def test_rain_leaves_clear_columns_between_three_staggered_streams():
    frames, delay = weather.frames('rain')
    assert delay == 120
    for frame in frames:
        assert {x for y in range(5, 8) for x in range(8) if frame[y][x] != (0, 0, 0)} == {1, 4, 6}
        assert all(frame[y][x] == (0, 0, 0) for y in range(5, 8) for x in (0, 2, 3, 5, 7))


def test_companion_wave_returns_to_rest_and_does_not_replace_eye_expression():
    frames = icons.companion_icon('idle')
    hand = icons.COMPANION_PALETTES['idle']['L']
    assert frames[0][4][7] != hand and frames[-1][4][7] != hand
    assert frames[2][4][7] == icons.COMPANION_PALETTES['idle']['L']
    assert frames[3][4][7] == icons.COMPANION_PALETTES['idle']['L']
    assert all(frame[2][2] == icons.COMPANION_EYE for frame in frames[:5])
    assert frames[-1][4][2] == icons.COMPANION_PUPIL  # The breathing pose is one row lower.


def test_gpu_contacts_and_shell_do_not_spin_with_the_blades():
    frames = [pc._icon_frame('GPU', 96, phase) for phase in range(6)]
    for frame in frames:
        assert all(frame.getpixel((x, 7)) == pc.PIN_GOLD for x in (2, 4, 5))
        assert all(frame.getpixel((x, 0)) == pc.GPU_SHROUD for x in range(2, 6))


def media(text=None, *, calm=False):
    runner = MediaPreview(text or source('now_playing-v2.3.ax'))
    runner.receive(reduced_motion=calm)
    runner.page.on_show()
    return runner


@pytest.mark.parametrize('calm', [False, True])
def test_new_vinyl_keeps_exact_title_progress_cards_and_exit_behavior(calm):
    old, new = media(source('now_playing-v2.2.ax'), calm=calm), media(calm=calm)
    for stamp in range(1000, 11801, 25):
        a, b = old.draw_at(stamp), new.draw_at(stamp)
        assert a.crop((8, 0, 32, 8)).tobytes() == b.crop((8, 0, 32, 8)).tobytes()
        assert old.page.cards == new.page.cards
        assert old.rotation.advances == new.rotation.advances


@pytest.mark.parametrize('interval', [25, 120, 500])
def test_record_moves_one_adjacent_groove_position_even_after_a_late_draw(interval):
    runner = media()
    previous = 0
    for stamp in range(1000, 6500, interval):
        runner.draw_at(stamp)
        step = (runner.page.record_phase - previous) % 16
        assert step in (0, 1)
        if step:
            old_pos = runner.page.groove[previous]
            pos = runner.page.groove[runner.page.record_phase]
            assert max(abs(pos // 8 - old_pos // 8), abs(pos % 8 - old_pos % 8)) <= 1
        previous = runner.page.record_phase
    runner.page.on_hide()
    runner.draw_at(20000)
    assert runner.page.record_phase == previous


def test_new_record_has_sixteen_unique_poses_and_an_intact_low_light_rim():
    runner = media()
    pictures = []
    for phase in range(16):
        runner.page.record_phase = phase
        frame = runner.draw_at(1000)
        pictures.append(frame.crop((0, 0, 8, 8)).tobytes())
        rim = [(2, 0), (5, 0), (1, 1), (6, 1), (0, 2), (7, 2),
               (0, 5), (7, 5), (1, 6), (6, 6), (2, 7), (5, 7)]
        assert all(max(frame.getpixel(point)) >= 174 for point in rim)
    assert len(set(pictures)) == 16


@pytest.mark.parametrize('illumination', [0, 12, 50, 76, 100])
@pytest.mark.parametrize('waxing', [False, True])
def test_moon_texture_never_paints_the_unlit_side_or_changes_phase_geometry(illumination, waxing):
    old = MoonPreview(source('weather/moon-panel-v2.2.ax'))
    new = MoonPreview(source('weather/moon-panel-v2.3.ax'))
    for runner in (old, new):
        runner.phase(illumination, waxing=waxing)
        runner.screen.clear()
        runner.page.draw_moon_body(0)
    before = old.screen.px[:]
    assert old.page.dr == new.page.dr and before == new.screen.px
    new.page.draw_surface(0)
    changed = [index for index, (a, b) in enumerate(zip(before, new.screen.px)) if a != b]
    assert all(before[index] == old.page.litc for index in changed)
    if illumination == 100:
        assert len(changed) == 6
    if illumination == 0:
        assert not changed


def test_moon_nebula_does_not_reset_its_palette_at_the_old_sawtooth_boundary():
    runner = MoonPreview(source('weather/moon-panel-v2.3.ax'))
    palettes = []
    for stamp in (39999, 40000, 79999, 80000):
        runner.clock.now_ms = stamp
        runner.page.build_pal()
        palettes.append(runner.page.pal[:])
    for left, right in (palettes[:2], palettes[2:]):
        assert max(abs((a >> shift & 255) - (b >> shift & 255))
                   for a, b in zip(left, right) for shift in (0, 8, 16)) <= 2


def test_meteor_fades_in_and_out_over_a_longer_visible_path():
    runner = MoonPreview(source('weather/moon-panel-v2.3.ax'))
    amounts = []
    for stamp in (0, 60, 120, 500, 800, 940, 960):
        runner.screen.clear()
        runner.page.draw_shooting_star(stamp, R(1.0))
        amounts.append(sum(sum((value >> shift) & 255 for shift in (0, 8, 16)) for value in runner.screen.px))
    assert amounts[0] == amounts[-1] == 0
    assert 0 < amounts[-2] < amounts[-3] < amounts[3]
    assert amounts[1] < amounts[2]


def test_manta_has_a_diamond_body_and_one_cell_wing_steps_across_the_loop_seam():
    runner = OceanPreview(source('ocean-v3.1.ax'))
    runner.page.mx, runner.page.my, runner.page.mdir = R(16), R(3), R(1)
    wings = []
    for stamp in range(0, 2400, 400):
        runner.screen.clear()
        runner.page.draw_manta(stamp)
        frame = runner.screen.px
        assert frame[2 * 32 + 16] and frame[4 * 32 + 16]  # Body has depth, not a stripe.
        rows = [y for y in range(8) if frame[y * 32 + 13]]
        assert len(rows) == 1
        wings.append(rows[0])
    assert max(wings) - min(wings) == 2
    assert all(abs(a - b) <= 1 for a, b in zip(wings, wings[1:] + wings[:1]))


def test_new_manta_and_reef_do_not_age_during_hidden_time():
    runner = OceanPreview(source('ocean-v3.1.ax'))
    runner.show()
    runner.advance(20000)
    state = (runner.page.scene_ms, runner.page.mx, runner.page.my)
    runner.hide()
    runner.advance(600000)
    assert (runner.page.scene_ms, runner.page.mx, runner.page.my) == state
    runner.show()
    runner.advance(40)
    assert runner.page.scene_ms - state[0] == 40


def test_revised_vinyl_remains_completely_still_in_reduced_motion():
    runner = media(calm=True)
    icons_seen = [runner.draw_at(stamp).crop((0, 0, 8, 8)).tobytes() for stamp in range(1000, 7000, 120)]
    assert len(set(icons_seen)) == 1


def test_all_resident_candidates_fit_the_existing_source_cap():
    for path in ('ocean-v3.1.ax', 'weather/moon-panel-v2.3.ax', 'now_playing-v2.3.ax'):
        assert len(minify(source(path)).encode()) <= 12288


@pytest.mark.parametrize('path,kind', [('ocean-v3.1.ax', 'ocean'),
    ('weather/moon-panel-v2.3.ax', 'moon'), ('now_playing-v2.3.ax', 'media')])
def test_revised_art_executes_identically_after_actual_upload_minification(path, kind):
    text = source(path)
    if kind == 'ocean':
        runners = [OceanPreview(item) for item in (text, minify(text))]
        for runner in runners:
            runner.show()
            runner.advance(21000)
    elif kind == 'moon':
        runners = [MoonPreview(item) for item in (text, minify(text))]
        for runner in runners:
            runner.phase(100)
            for _ in range(100):
                runner.step(20)
    else:
        runners = [media(item) for item in (text, minify(text))]
        for runner in runners:
            runner.draw_at(2500)
    assert runners[0].screen.px == runners[1].screen.px

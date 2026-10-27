"""Actual-source tests for readable Moon info and frame-bounded movement."""
import pytest

from moon_review import CANDIDATE, REFERENCE, MoonPreview, preview_font
from weather.minify_ax import minify
from weather.moon_panel_sim import Font


@pytest.mark.parametrize('illumination', [0,1,12,49,50,51,76,99,100])
@pytest.mark.parametrize('waxing', [False,True])
def test_phase_and_percentage_fields_fit_without_collision(illumination,waxing):
    runner=MoonPreview();runner.phase(illumination,waxing=waxing)
    for _ in range(330):runner.step(20)
    assert runner.page.cam==32
    assert runner.page.pcts==f'{illumination}%'
    fields=runner.screen.texts
    assert len(fields)==2
    intervals=[(x,x+runner.screen.font.ink_width(text)) for x,_,text,_ in fields]
    assert all(0<=left<right<=32 for left,right in intervals)
    assert intervals[0][1]+2<=intervals[1][0]


def test_large_native_font_alternates_readable_fields_instead_of_overlapping():
    glyphs={c:['######']*5 for c in preview_font().glyphs}
    runner=MoonPreview(font=Font(glyphs));runner.phase(100)
    seen=[]
    for index in range(400):
        runner.step(20)
        if index in (299,374):
            fields=runner.screen.texts
            assert len(fields)==1
            x,_,value,_=fields[0]
            assert 0<=x and x+runner.screen.font.ink_width(value)<=32
            seen.append(value)
    assert seen==['FULL','100%']


@pytest.mark.parametrize('frame_ms',[20,24,40,80,120])
def test_camera_never_skips_a_physical_column(frame_ms):
    runner=MoonPreview();runner.phase(50)
    positions=[runner.page.cam]
    for _ in range(11000//frame_ms):
        runner.step(frame_ms);positions.append(runner.page.cam)
    assert all(abs(b-a)<=1 for a,b in zip(positions,positions[1:]))
    assert 0<=min(positions)<=max(positions)<=32
    assert max(positions)==32


def test_on_show_restarts_camera_after_an_interrupted_visit():
    runner=MoonPreview();runner.phase(76)
    for _ in range(310):runner.step(20)
    assert runner.page.cam==32
    runner.clock.now_ms+=600000
    runner.page.on_show()
    assert runner.page.cam==0
    runner.step(20)
    assert runner.page.cam==0


def test_unsynced_manual_preview_never_claims_half_illumination():
    runner=MoonPreview(year=1970)
    assert runner.page.should_show() is False  # Normal rotation already hides it.
    assert runner.page.valid is False
    assert runner.page.pnm=='SYNC' and runner.page.pcts=='--%'
    runner.step(20)
    assert runner.page.litc not in runner.screen.px
    assert 0x9BB0CF in runner.screen.px
    runner.clock.year=2026;runner.clock.hour=22
    runner.step(1000)
    assert runner.page.valid and runner.page.pnm!='SYNC'


def test_clock_loss_replaces_cached_phase_for_manual_activation():
    runner=MoonPreview();runner.phase(100)
    runner.clock.year=1970
    runner.page.compute_phase()
    assert not runner.page.valid
    assert (runner.page.pnm,runner.page.pcts)==('SYNC','--%')


@pytest.mark.parametrize('epoch',[1767225600000,1772323200000,1793066400000,1893456000000])
def test_astronomical_results_and_disc_geometry_remain_unchanged(epoch):
    old=MoonPreview(REFERENCE.read_text(encoding='utf-8'),epoch_ms=epoch)
    new=MoonPreview(epoch_ms=epoch)
    for attr in ('illum','wax','phf','ecl','dr','litc','midc','dk'):
        assert getattr(old.page,attr)==getattr(new.page,attr),attr
    assert old.page.should_show()==new.page.should_show()
    assert old.page.duration()==new.page.duration()==11000


def test_info_background_is_quiet_without_bright_stars_behind_letters():
    runner=MoonPreview();runner.phase(100)
    for _ in range(300):runner.step(20)
    assert runner.page.cam==32
    runner.page.draw_labels=lambda cam:None
    runner.step(20)
    assert max(max((c>>16)&255,(c>>8)&255,c&255) for c in runner.screen.px)<174


def test_minified_candidate_executes_without_new_native_requirements():
    source=minify(CANDIDATE.read_text(encoding='utf-8'))
    assert '# @version 2.2' in source
    runner=MoonPreview(source);runner.phase(100)
    for _ in range(330):runner.step(20)
    assert any(text=='100%' for _,_,text,_ in runner.screen.texts)

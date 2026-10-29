"""Restore the original reef environment without changing compact Ocean motion."""
from pathlib import Path
import pytest
from ocean_preview import OceanPreview
from weather.minify_ax import minify

ROOT=Path(__file__).resolve().parent
BEFORE=(ROOT/'ocean-v3.2.ax').read_text(encoding='utf-8')
AFTER=(ROOT/'ocean-v3.3.ax').read_text(encoding='utf-8')
ORIGINAL=(ROOT/'ocean.ax').read_text(encoding='utf-8')
RESTORED={6,7,8,*range(15,22)}


def rgb(buffer):
    return [int.from_bytes(buffer[i:i+3],'big') for i in range(0,len(buffer),3)]


def pair(candidate=AFTER):
    return OceanPreview(BEFORE),OceanPreview(candidate)


def same_motion(a,b):
    assert a.page.__berry_members__==b.page.__berry_members__
    for field in a.page.__berry_members__:
        if field not in ('w','c','B','sc'):
            assert getattr(a.page,field)==getattr(b.page,field),field


def test_environment_restores_original_water_kelp_bubbles_and_olive_reef():
    old,base,new=OceanPreview(ORIGINAL),OceanPreview(BEFORE),OceanPreview(AFTER)
    assert rgb(new.page.w)==old.page.w
    a,b=rgb(base.page.c),rgb(new.page.c)
    assert {i for i in range(32) if a[i]!=b[i]}==RESTORED
    assert all(b[i]==old.page.c[i] for i in RESTORED)
    assert all((b[i]>>8&255)>max(b[i]>>16,b[i]&255) for i in range(15,22))


@pytest.mark.parametrize('role', [9,10,11,13,14,22,23,24,25,26,27,28,29,30,31])
def test_fish_manta_and_other_visitor_colours_are_preserved(role):
    a,b=pair()
    assert rgb(a.page.c)[role]==rgb(b.page.c)[role]


def test_compact_buffer_shapes_and_constructor_retirement_are_preserved():
    a,b=pair()
    for field,size in [('w',24),('c',96),('B',90),('T',512),('sc',384),('sn4',32)]:
        assert len(getattr(a.page,field))==len(getattr(b.page,field))==size
    assert b.page.init is None
    assert len(minify(AFTER).encode())<=len(minify(BEFORE).encode())


def test_all_visitors_and_seed_trajectory_match_over_seven_visible_minutes():
    a,b=pair()
    a.show();b.show();seen=set()
    for _ in range(3500):
        a.advance(120,frame_ms=120);b.advance(120,frame_ms=120)
        same_motion(a,b)
        seen|={i for i,visitor in enumerate(b.page.visitors) if visitor[3]}
    assert seen==set(range(6))


@pytest.mark.parametrize('hidden', [1000,900000])
def test_hidden_time_and_restart_do_not_change_animation_progress(hidden):
    a,b=pair();a.show();b.show()
    a.advance(15000);b.advance(15000)
    a.hide();b.hide();a.advance(hidden);b.advance(hidden)
    same_motion(a,b)
    a.show();b.show();a.advance(120);b.advance(120)
    same_motion(a,b)


def test_green_reef_remains_visible_during_an_ordinary_visit_without_visitors():
    a,b=pair();a.show();b.show();before=after=0
    for _ in range(250):
        a.advance(120);b.advance(120)
        for runner,new in [(a,False),(b,True)]:
            count=sum((p>>8&255)>max(p>>16,p&255) for p in runner.screen.px[7*32:])
            if new:after+=count
            else:before+=count
    assert after>250*32*.8
    assert after-before>250*32*.6


def test_upload_minification_preserves_the_new_colours_and_geometry():
    a,b=OceanPreview(AFTER),OceanPreview(minify(AFTER))
    a.show();b.show()
    for _ in range(250):
        a.advance(120);b.advance(120)
        assert a.screen.px==b.screen.px
        same_motion(a,b)

"""Compare the compact candidate to the approved 3.1 source, never a device."""
from pathlib import Path
import struct

import pytest

from ocean_preview import OceanPreview
from weather.minify_ax import minify
from weather.moon_panel_sim import R
from weather.berry_lite import Program, BerryError

ROOT = Path(__file__).resolve().parent
REFERENCE = (ROOT / 'ocean-v3.1.ax').read_text(encoding='utf-8')
CANDIDATE = (ROOT / 'ocean-v3.2.ax').read_text(encoding='utf-8')
GROUPS = [('sx','sy','sdir','sact','snext'), ('mx','my','mdir','mact','mnext'),
          ('tr_x','tr_y','tr_dir','tr_act','tr_next'), ('ag_x','ag_y','ag_dir','ag_act','ag_next'),
          ('ed_x','ed_y','ed_dir','ed_act','ed_next'), ('hc_x',None,'hc_dir','hc_act','hc_next')]
REMOVED = {name for group in GROUPS for name in group if name}


def pair(*, compact=CANDIDATE, wall=0, fill=0xA5):
    return (OceanPreview(REFERENCE, wall_ms=wall, fill=fill),
            OceanPreview(compact, wall_ms=wall, fill=fill))


def assert_state(old, new):
    for fields, values in zip(GROUPS, new.page.visitors):
        for field, value in zip(fields, values):
            if field:
                assert getattr(old.page, field) == value, field
    for field in old.page.__berry_members__:
        if field not in REMOVED:
            actual = getattr(new.page, field)
            if field == 'T':
                actual = list(struct.unpack('<128f', actual))
            elif field == 'B':
                actual = list(struct.unpack('<45h', actual))
            elif field in ('c', 'w'):
                actual = [int.from_bytes(actual[i:i+3], 'big') for i in range(0, len(actual), 3)]
            elif field in ('msk', 'msk2'):
                actual = list(struct.unpack('<4H', actual))
            elif field in ('fl', 'kx'):
                actual = list(actual)
            assert getattr(old.page, field) == actual, field


def advance(old, new, ms, *, frame=40):
    assert old.advance(ms, frame_ms=frame) == new.advance(ms, frame_ms=frame)
    assert_state(old, new)


@pytest.mark.parametrize('fill', [0, 0xA5, 0xFF])
def test_initialization_and_visible_visit_match_exactly(fill):
    old, new = pair(fill=fill)
    assert_state(old, new)
    assert len(new.page.visitors) == 6 and len(new.page.visitor_rules) == 66
    for runner in (old, new):
        runner.show()
    for _ in range(250):
        advance(old, new, 120, frame=120)


def test_complete_natural_encounter_timeline_preserves_seed_pixels_and_state():
    old, new = pair()
    for runner in (old, new):
        runner.show()
    seen = set()
    for _ in range(3500):  # Seven visible minutes, including first shark and repeated visitors.
        advance(old, new, 120, frame=120)
        seen |= {i for i, values in enumerate(new.page.visitors) if values[3]}
    assert seen == set(range(6))


@pytest.mark.parametrize('index', range(6))
@pytest.mark.parametrize('direction', [-1, 1])
def test_each_visitor_preserves_edges_and_art_in_both_directions(index, direction):
    old, new = pair()
    for runner in (old, new):
        runner.show()
    fields = GROUPS[index]
    for slot, value in [(0, R(1.0 if direction < 0 else 30.0)), (2, R(direction)), (3, True)]:
        setattr(old.page, fields[slot], value)
        new.page.visitors[index][slot] = value
    # Includes clipping, fractional movement, rescheduling and the crab's timeout.
    for _ in range(120):
        advance(old, new, 120, frame=120)


@pytest.mark.parametrize('seed', [0, 1, 0x3FFFFFFF, 123456789])
def test_simultaneous_deadlines_consume_random_seed_in_original_order(seed):
    old, new = pair()
    old.page.seed = new.page.seed = seed
    for index, fields in enumerate(GROUPS):
        setattr(old.page, fields[4], 0)
        new.page.visitors[index][4] = 0
    for runner in (old, new):
        runner.show()
    assert_state(old, new)
    for _ in range(20):
        advance(old, new, 40)


@pytest.mark.parametrize('wall', [0, 1_000_000_000])
def test_hidden_intervals_and_stutters_keep_visible_time_contract(wall):
    old, new = pair(wall=wall)
    for runner in (old, new):
        runner.show()
    advance(old, new, 16000)
    before = [list(values) for values in new.page.visitors]
    for runner in (old, new):
        runner.hide()
    advance(old, new, 900000, frame=1000)
    assert new.page.visitors == before
    for runner in (old, new):
        runner.show()
    for elapsed in (1, 24, 40, 200, 3000, 25, 120):
        advance(old, new, elapsed, frame=elapsed)


@pytest.mark.parametrize('hour', [-1, 6, 7, 16, 17, 23])
def test_schedule_and_duration_are_unchanged(hour):
    old, new = pair()
    for runner in (old, new):
        runner.hour = hour
    assert old.page.should_show() == new.page.should_show()
    assert old.page.duration() == new.page.duration() == 30000
    for phase in ('day', 'night'):
        for runner in (old, new):
            runner.shared.phase = phase
            runner.shared.age_ms = 1
        assert old.page.should_show() == new.page.should_show()


def test_minified_source_executes_identically_and_remains_under_the_existing_cap():
    compact = minify(CANDIDATE)
    assert len(compact.encode('utf-8')) <= 12288
    old, new = pair(compact=compact)
    for runner in (old, new):
        runner.show()
    for _ in range(100):
        advance(old, new, 120)


@pytest.mark.parametrize('body', ['var i=0 var i=1', 'var V=[] var V=3', 'var x=0,x=1'])
def test_subset_model_rejects_the_native_compilers_same_scope_redeclaration(body):
    source = 'class A def go() ' + body + ' end end return A()'
    with pytest.raises(BerryError, match='redefinition'):
        Program(source, real_type=R)


def test_subset_model_allows_nested_shadowing_but_not_parameter_redeclaration():
    page = Program('class A def go(x) if true var x=2 end return x end end return A()', real_type=R).run({})
    assert page.go(1) == 1
    with pytest.raises(BerryError, match='redefinition'):
        Program('class A def go(x) var x=2 end end return A()', real_type=R)


def test_constructor_is_released_only_after_the_complete_page_is_created():
    old, new = pair()
    assert new.page.init is None
    assert_state(old, new)
    # Each independent script load defines and initializes its own fresh class.
    other = OceanPreview(CANDIDATE)
    assert other.page is not new.page and other.page.visitors == new.page.visitors
    for runner in (old, new):
        runner.show()
    advance(old, new, 16000)


def test_unknown_class_member_assignment_is_not_silently_added_by_the_model():
    with pytest.raises(BerryError, match='class has no member'):
        Program('class A var x end A.missing=nil return A()', real_type=R).run({})

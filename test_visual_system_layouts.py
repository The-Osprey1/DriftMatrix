import pytest
from visual_system import (
    dithered_fill, render_sparkline, render_split_lane, new_frame,
    WIDTH, HEIGHT, GUTTER_X, LANE_X0, LANE_WIDTH, TRACK, BLACK
)

def test_dithered_fill():
    frame = new_frame()
    dithered_fill(frame, 0, 0, 5, 5, (255, 0, 0), (0, 0, 255))
    # It should not be uniform
    colors = set()
    for y in range(6):
        for x in range(6):
            colors.add(frame[y][x])
    assert len(colors) > 1

def test_render_sparkline():
    frame = render_sparkline([0.1, 0.5, 0.9], label="CPU")
    assert len(frame) == HEIGHT
    assert len(frame[0]) == WIDTH
    for y in range(HEIGHT):
        assert frame[y][GUTTER_X] == BLACK


def test_render_sparkline_preserves_label_row_4():
    """Verify sparkline bars and background track never corrupt label row 4."""
    # Test with label 'CPU' and full-lane maximum values (1.0 across 23 columns)
    frame_max = render_sparkline([1.0] * LANE_WIDTH, label="CPU")
    assert len(frame_max) == HEIGHT
    assert len(frame_max[0]) == WIDTH
    for y in range(HEIGHT):
        assert frame_max[y][GUTTER_X] == BLACK

    # 'CPU' at lx = 15:
    # 'C' at cols 15..17 (row 4: 111) -> (15, 4), (16, 4), (17, 4)
    # 'P' at cols 19..21 (row 4: 100) -> (19, 4)
    # 'U' at cols 23..25 (row 4: 111) -> (23, 4), (24, 4), (25, 4)
    expected_label_pixels = {(15, 4), (16, 4), (17, 4), (19, 4), (23, 4), (24, 4), (25, 4)}
    for x, y in expected_label_pixels:
        assert frame_max[y][x] == (178, 190, 206), f"Label pixel at ({x}, {y}) corrupted!"

    # Unlit pixels on row 4 must remain BLACK (zero background track bleeding)
    for x in range(LANE_X0, LANE_X0 + LANE_WIDTH):
        if (x, 4) not in expected_label_pixels:
            assert frame_max[4][x] == BLACK, f"Row 4 at x={x} should be BLACK, got {frame_max[4][x]}"

    # Rows 5..7 hold full sparkline bars (ICE)
    for x in range(LANE_X0, LANE_X0 + LANE_WIDTH):
        assert frame_max[5][x] == (196, 239, 255)
        assert frame_max[6][x] == (196, 239, 255)
        assert frame_max[7][x] == (196, 239, 255)

    # Test with minimum values (0.0): label row 4 is preserved and rows 5..6 remain TRACK
    frame_min = render_sparkline([0.0] * LANE_WIDTH, label="CPU")
    for x, y in expected_label_pixels:
        assert frame_min[y][x] == (178, 190, 206)
    for x in range(LANE_X0, LANE_X0 + LANE_WIDTH):
        if (x, 4) not in expected_label_pixels:
            assert frame_min[4][x] == BLACK
        assert frame_min[5][x] == TRACK
        assert frame_min[6][x] == TRACK
        assert frame_min[7][x] == (196, 239, 255)  # min 1px baseline pip

    # Test unlabelled sparkline retains 4-row height (rows 4..7)
    frame_unlabelled = render_sparkline([1.0] * LANE_WIDTH, label=None)
    for x in range(LANE_X0, LANE_X0 + LANE_WIDTH):
        for y in range(4):
            assert frame_unlabelled[y][x] == BLACK
        for y in range(4, 8):
            assert frame_unlabelled[y][x] == (196, 239, 255)


def test_render_split_lane(monkeypatch):
    import visual_system as vs

    # 1. Verify zero off-panel draws
    off_panel = []
    orig_put = vs.put_pixel

    def recording_put(frame, x, y, color):
        if not (0 <= x < vs.WIDTH and 0 <= y < vs.HEIGHT):
            off_panel.append((x, y))
        orig_put(frame, x, y, color)

    monkeypatch.setattr(vs, "put_pixel", recording_put)

    frame = vs.render_split_lane("12", "34", meter=0.5, level="good")
    assert off_panel == [], f"Expected zero off-panel draws, got {off_panel}"

    # 2. Dimensions and gutter isolation
    assert len(frame) == vs.HEIGHT
    assert len(frame[0]) == vs.WIDTH
    for y in range(vs.HEIGHT):
        assert frame[y][vs.GUTTER_X] == vs.BLACK

    # 3. Top value strictly in rows 0..2
    assert any(frame[y][x] != vs.BLACK for y in (0, 1, 2) for x in range(vs.LANE_X0, vs.LANE_X0 + vs.LANE_WIDTH))

    # 4. Row 3 is a clean 1px blank separator
    for x in range(vs.LANE_X0, vs.LANE_X0 + vs.LANE_WIDTH):
        assert frame[3][x] == vs.BLACK, f"Row 3 must be empty separator, got {frame[3][x]} at x={x}"

    # 5. Bottom value strictly in rows 4..6
    assert any(frame[y][x] != vs.BLACK for y in (4, 5, 6) for x in range(vs.LANE_X0, vs.LANE_X0 + vs.LANE_WIDTH))

    # 6. Row 7 is dedicated meter, no text collision
    good_color = vs.LEVEL_COLORS["good"]
    fill = max(1, min(vs.LANE_WIDTH, round(0.5 * vs.LANE_WIDTH)))
    for i in range(vs.LANE_WIDTH):
        expected_color = good_color if i < fill else vs.TRACK
        assert frame[7][vs.LANE_X0 + i] == expected_color

    # 7. Unmetered rail baseline test
    frame_unmetered = vs.render_split_lane("AB", "CD", meter=None)
    for x in range(vs.LANE_X0, vs.LANE_X0 + vs.LANE_WIDTH):
        assert frame_unmetered[7][x] == vs.TRACK

    # 8. Lane overflow validation
    with pytest.raises(ValueError, match="exceeds lane width"):
        vs.render_split_lane("TOOLONGTEXTVALUE", "34")

def test_render_dual_lane():
    from visual_system import render_dual_lane
    frame = render_dual_lane("C82", "R94", left_meter=0.82, right_meter=0.94, left_level="fair", right_level="poor")
    assert len(frame) == HEIGHT
    assert len(frame[0]) == WIDTH
    for y in range(HEIGHT):
        assert frame[y][GUTTER_X] == BLACK


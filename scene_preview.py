"""Offline 32x8 renderer for the four pushed scenes, for design review.

Companion, Commute, GitHub Sentinel and Meeting Sentinel shipped without one
of these, which is why two layout faults reached the repository unseen.  Every
other app here has a preview; this is theirs.

It never connects to the panel.  Each sheet composites the real 8x8 icon, the
black column-8 gutter and the real 23x8 lane into a true 32x8 frame, then
enlarges it with nearest-neighbour sampling so pixel geometry survives review.

A second sheet per app renders the same frames at the panel's one-bit
brightness, where any channel under 174 goes black.  Decorative rails are
expected to disappear there; anything carrying meaning must not.

Controlled Folder Access blocks writes under Documents, so --out is required
and should point outside the repository.

    python scene_preview.py --out %TEMP%\\scenes
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

import commute_radar as cr
import desk_companion as dc
import github_sentinel as gs
import meeting_sentinel as ms
import visual_system as vs

WIDTH = 32
HEIGHT = 8
BLACK = (0, 0, 0)
ONE_BIT_THRESHOLD = 174

Frame = list[list[tuple[int, int, int]]]


def composite(icon: Frame, lane: Frame) -> Frame:
    """Icon owns x0..7, column 8 is the black gutter, lane owns x9..31."""
    out = [[BLACK] * WIDTH for _ in range(HEIGHT)]
    for y in range(HEIGHT):
        for x in range(8):
            out[y][x] = icon[y][x]
        for x in range(9, WIDTH):
            out[y][x] = lane[y][x]
    return out


def one_bit(frame: Frame) -> Frame:
    """What the panel shows at brightness 1: one bit per channel."""
    return [
        [tuple(255 if ch >= ONE_BIT_THRESHOLD else 0 for ch in px) for px in row]
        for row in frame
    ]


def sheet(frames: list[Frame], scale: int, gap: int) -> Image.Image:
    img = Image.new(
        "RGB",
        (len(frames) * WIDTH * scale + (len(frames) - 1) * gap, HEIGHT * scale),
        (18, 18, 22),
    )
    for idx, frame in enumerate(frames):
        tile = Image.new("RGB", (WIDTH, HEIGHT))
        tile.putdata([frame[y][x] for y in range(HEIGHT) for x in range(WIDTH)])
        img.paste(
            tile.resize((WIDTH * scale, HEIGHT * scale), Image.NEAREST),
            (idx * (WIDTH * scale + gap), 0),
        )
    return img


def stack(rows: list[Image.Image], gap: int = 10) -> Image.Image:
    img = Image.new(
        "RGB",
        (max(r.width for r in rows), sum(r.height for r in rows) + gap * (len(rows) - 1)),
        (18, 18, 22),
    )
    y = 0
    for row in rows:
        img.paste(row, (0, y))
        y += row.height + gap
    return img


def companion_rows() -> list[list[Frame]]:
    rows = []
    for mood in dc.CompanionMood:
        lane = dc.render_companion_lane(mood)
        frames = []
        for phase in range(4):
            icon = vs.new_frame()
            dc.draw_companion_avatar(icon, mood, phase)
            frames.append(composite(icon, lane))
        rows.append(frames)
    return rows


def commute_rows() -> list[list[Frame]]:
    rows = []
    for traffic, dest, minutes in (
        (cr.TrafficCondition.CLEAR, "WORK", 24),
        (cr.TrafficCondition.MODERATE, "WORK", 38),
        (cr.TrafficCondition.HEAVY, "HOME", 52),
    ):
        lane = cr.render_commute_lane(cr.CommuteEstimate(dest, minutes, 25, traffic))
        frames = []
        for phase in range(2):
            icon = vs.new_frame()
            cr.draw_car_icon(icon, phase, traffic)
            frames.append(composite(icon, lane))
        rows.append(frames)
    return rows


def github_rows() -> list[list[Frame]]:
    rows = []
    for run, icon_fn in (
        (gs.WorkflowRun("owner/miku-jarvis", 412, "completed", "success"), gs.draw_ci_pass_icon),
        (gs.WorkflowRun("owner/miku-jarvis", 413, "completed", "failure"), gs.draw_ci_fail_icon),
        (gs.WorkflowRun("owner/miku-jarvis", 414, "in_progress", None), gs.draw_ci_gear_icon),
    ):
        lane = gs.render_ci_lane_frame(run)
        frames = []
        for phase in range(4):
            icon = gs.new_frame()
            icon_fn(icon, phase)
            frames.append(composite(icon, lane))
        rows.append(frames)

    for report in (
        gs.PRRadarReport("owner/awtrix", 3, 1, 2 * 3600),
        gs.PRRadarReport("owner/awtrix", 7, 2, 25 * 3600),
        gs.PRRadarReport("owner/awtrix", 12, 4, 50 * 3600),
    ):
        lane = gs.render_pr_lane_frame(report)
        frames = []
        for phase in range(4):
            icon = gs.new_frame()
            gs.draw_octocat_pr_icon(icon, phase, report.oldest_waiting_seconds)
            frames.append(composite(icon, lane))
        rows.append(frames)
    return rows


def meeting_rows() -> list[list[Frame]]:
    rows = []
    for session in (
        ms.ActiveMicSession("Discord", "d.exe", 100.0, 125.0),
        ms.ActiveMicSession("Zoom", "z.exe", 100.0, 3665.0),
    ):
        lane = ms.render_lane_frame(session)
        frames = []
        for phase in range(4):
            icon = ms.new_frame()
            ms.draw_onair_mic_icon(icon, phase)
            frames.append(composite(icon, lane))
        rows.append(frames)
    return rows


APPS = {
    "desk_companion": companion_rows,
    "commute_radar": commute_rows,
    "github_sentinel": github_rows,
    "meeting_sentinel": meeting_rows,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scale", type=int, default=14)
    parser.add_argument("--gap", type=int, default=8)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for app, builder in APPS.items():
        rows = builder()
        colour = stack([sheet(frames, args.scale, args.gap) for frames in rows])
        dark = stack([sheet([one_bit(frames[0])], args.scale, args.gap) for frames in rows])

        colour_path = args.out / f"{app}.png"
        dark_path = args.out / f"{app}_1bit.png"
        colour.save(colour_path)
        dark.save(dark_path)
        written.extend((colour_path, dark_path))

    for path in written:
        print(path)


if __name__ == "__main__":
    main()

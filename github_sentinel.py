"""GitHub CI/CD & PR Radar Sentinel for AWTRIX.

Monitors GitHub Actions workflow runs and open Pull Requests awaiting review.
Displays 8x8 status emblems (spinning gear for in-progress builds, green branch
for pass, flashing red cross on failure, and Octocat with amber/red notification
pips for pending PR reviews) along with a 23x8 data lane.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
import json
import logging
import os
import time
from typing import Any, Callable, Sequence

from PIL import Image

from visual_system import (
    glance_text_width,
    render_glance_lane,
    AMBER,
    BLACK,
    CORAL,
    EMERALD,
    FONT_3X5,
    cycle_length,
    GOLD,
    ICE,
    PEARL,
    RED_HOT,
    TRACK,
    draw_lane_rail,
)

try:
    import requests
except ImportError:
    requests = None


RGB = tuple[int, int, int]
Frame = list[list[RGB]]

WIDTH = 32
HEIGHT = 8
ICON_WIDTH = 8
ICON_HEIGHT = 8
GUTTER_X = 8
LANE_X0 = 9
LANE_WIDTH = 23
LANE_HEIGHT = 8

APP_NAME = "ghsentinel"
FRAME_BEAT_MS = 120
PAGE_DURATION_MS = 7_000
LIFETIME_MS = 60_000
ICON_MAX_BYTES = 1_024
PAYLOAD_MAX_BYTES = 2_048

# Colours come from visual_system.  A locally redeclared TRACK here had
# already drifted one shade from the shared one; a duplicated palette is a
# divergence waiting to happen, so there is no longer a second copy to drift.

# The 3x5 font is shared, not copied.  Four divergent copies of this dict
# existed and had already drifted -- the same "1"/"I" correction had to be
# applied three separate times.  visual_system is the single source; its
# glyph set is a strict superset of what this module used.
#
# One consequence, accepted deliberately: visual_system's "W" is 5px, so
# "W PASS" measures 23px against the 21px budget and _fit_label degrades it
# to bare "PASS".  A W-initial repo therefore shows no prefix rather than a
# 3px W that is one pixel from "H" and reads as the wrong letter.


@dataclass(frozen=True)
class WorkflowRun:
    repo: str
    run_number: int
    status: str  # "completed", "in_progress", "queued"
    conclusion: str | None  # "success", "failure", "cancelled", etc.
    branch: str = "main"


@dataclass(frozen=True)
class PRRadarReport:
    repo: str
    open_prs: int
    review_requested: int
    oldest_waiting_seconds: float = 0.0


def new_frame() -> Frame:
    return [[BLACK for _ in range(WIDTH)] for _ in range(HEIGHT)]


def put_pixel(frame: Frame, x: int, y: int, color: RGB) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        frame[y][x] = color


def draw_text(frame: Frame, text: str, x0: int, y0: int, color: RGB) -> int:
    x = x0
    for idx, char in enumerate(text):
        glyph = FONT_3X5.get(char.upper(), FONT_3X5[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    put_pixel(frame, x + gx, y0 + gy, color)
        x += len(glyph[0])
        if idx < len(text) - 1:
            x += 1
    return x - x0


def text_width(text: str) -> int:
    glyphs = [FONT_3X5.get(c.upper(), FONT_3X5[" "]) for c in text]
    return sum(len(g[0]) for g in glyphs) + max(0, len(glyphs) - 1)


# ---------------------------------------------------------------- Icons

def draw_ci_pass_icon(frame: Frame, phase: int) -> None:
    """8x8 checkmark: one connected emblem.

    It used to be two: a green "branch line" -- a bare diagonal stroke that
    descended to the LEFT, the wrong direction for the leading arm of a tick
    -- beside a separate white checkmark.  The eye parsed them as two glyphs
    and the icon read as the numeral 1 next to a tick.  It was also the
    sparsest icon in the rotation at eleven lit pixels, and the tick's tail
    ended in column 7 hard against the mandatory black gutter, so it looked
    clipped.

    One two-pixel-wide polyline in one colour fixes all three: the stroke is
    continuous from (1,3) to (6,1), nothing is left floating, and it stops at
    column 6 so the gutter is not its edge.
    """
    stroke = (
        # short arm, down-right
        (1, 3), (2, 4), (3, 5),
        # long arm, up-right
        (4, 4), (5, 3), (6, 2),
    )
    thickness = (
        (1, 4), (2, 5), (3, 6),
        (4, 5), (5, 4), (6, 3),
    )
    for x, y in stroke + thickness:
        put_pixel(frame, x, y, EMERALD)


def draw_ci_fail_icon(frame: Frame, phase: int) -> None:
    """8x8 red cross that pulses by SIZE on the 120ms beat.

    It used to recolour all twelve lit pixels between RED_HOT and CORAL every
    beat with no shape change, which is flicker, not a pulse -- and at
    brightness 1 it was not even that: both colours threshold to (255,0,0),
    so the two frames were pixel-identical and the animation disappeared
    entirely at the brightness where an alarm matters most.

    Extinguishing the four outer tips instead makes the X contract and
    expand.  Displacement survives the 1-bit collapse; hue does not.
    """
    core = (
        (2, 2), (5, 5), (5, 2), (2, 5),
        (3, 3), (4, 3), (3, 4), (4, 4),
    )
    tips = ((1, 1), (6, 6), (6, 1), (1, 6))
    for x, y in core:
        put_pixel(frame, x, y, RED_HOT)
    if phase % 2 == 0:
        for x, y in tips:
            put_pixel(frame, x, y, RED_HOT)


def draw_ci_gear_icon(frame: Frame, phase: int) -> None:
    """8x8 Rotating gear for in-progress builds."""
    # Two tooth sets, four teeth each, every tooth orthogonally adjacent to
    # the ring -- one radius, one count.
    #
    # The four old phases were not rotations of each other and not even the
    # same size: phase 0 put its teeth a dark pixel clear of the ring, phase 2
    # had them touching it, phase 3 mixed both radii asymmetrically, and the
    # count wandered.  Two of four frames were symmetric and two were
    # scattered, so at 120ms the gear appeared to break apart and reassemble
    # twice per cycle rather than turn.
    #
    # Each set below maps onto itself under a 90-degree turn, and set B is set
    # A advanced one cell around the ring, so A->B is a genuine rotation step.
    # A four-toothed gear looks identical every 90 degrees, so a full turn is
    # not expressible on an eight-pixel grid; one honest step, alternating,
    # reads as turning, which is what the icon has to say.
    offsets = [
        ((3, 1), (6, 3), (4, 6), (1, 4)),
        ((4, 1), (6, 4), (3, 6), (1, 3)),
    ]
    # Center ring
    ring = ((2, 2), (3, 2), (4, 2), (5, 2),
            (2, 5), (3, 5), (4, 5), (5, 5),
            (2, 3), (2, 4), (5, 3), (5, 4))
    for x, y in ring:
        put_pixel(frame, x, y, GOLD)
    for x, y in offsets[phase % 2]:
        put_pixel(frame, x, y, AMBER)
    # Center hole
    put_pixel(frame, 3, 3, BLACK)
    put_pixel(frame, 4, 3, BLACK)
    put_pixel(frame, 3, 4, BLACK)
    put_pixel(frame, 4, 4, BLACK)


def draw_octocat_pr_icon(frame: Frame, phase: int, oldest_waiting_seconds: float = 0.0) -> None:
    """8x8 Octocat silhouette with dynamic notification badge."""
    # Cat ears
    put_pixel(frame, 1, 1, PEARL)
    put_pixel(frame, 5, 1, PEARL)
    # Head: FILLED, then dark features punched into it.  Only the outline used
    # to be drawn, leaving the interior black, so the three "face" pixels below
    # were BLACK writes onto black -- no-ops.  The icon shipped as a hollow
    # 19-pixel ring and read as a box, not a cat.  desk_companion already does
    # it this way: fill first, then cut the features out.
    for y in range(2, 6):
        for x in range(1, 6):
            put_pixel(frame, x, y, PEARL)
    put_pixel(frame, 0, 3, PEARL)
    put_pixel(frame, 6, 3, PEARL)
    put_pixel(frame, 0, 4, PEARL)
    put_pixel(frame, 6, 4, PEARL)

    # Cat face features, now cut out of a lit head
    put_pixel(frame, 2, 3, BLACK)
    put_pixel(frame, 4, 3, BLACK)
    put_pixel(frame, 3, 4, BLACK)

    # Tentacles / base
    put_pixel(frame, 1, 6, PEARL)
    put_pixel(frame, 3, 6, PEARL)
    put_pixel(frame, 5, 6, PEARL)

    # Notification badge at top right (6..7, 0..1)
    # Turns amber if PR > 24h, pulsing coral if > 48h
    # The tiers differ in SIZE as well as colour: 1 pixel, then 3, then a full
    # 2x2 block.  The >24h and >48h badges used to light exactly the same three
    # pixels and differ only in hue, and AMBER, CORAL and RED_HOT all threshold
    # to (255, 0, 0) -- so at brightness 1 the escalation the badge exists to
    # carry disappeared completely.  Count survives; hue does not.
    if oldest_waiting_seconds >= 48 * 3600:
        badge_color = RED_HOT if phase % 2 == 0 else CORAL
        put_pixel(frame, 6, 0, badge_color)
        put_pixel(frame, 7, 0, badge_color)
        put_pixel(frame, 6, 1, badge_color)
        put_pixel(frame, 7, 1, badge_color)
    elif oldest_waiting_seconds >= 24 * 3600:
        put_pixel(frame, 6, 0, AMBER)
        put_pixel(frame, 7, 0, AMBER)
        put_pixel(frame, 7, 1, AMBER)
    elif oldest_waiting_seconds > 0:
        put_pixel(frame, 7, 0, ICE)


def build_icon_gif(
    draw_fn: Callable[[Frame, int], None],
    frame_count: int = 4,
    animated: bool = True,
) -> bytes:
    """Create a validated 8x8 GIF on 120ms beat."""
    count = frame_count if animated else 1
    frames = []
    for phase in range(count):
        f = new_frame()
        draw_fn(f, phase)
        frames.append(f)

    count = cycle_length(frames)
    images: list[Image.Image] = []
    for f in frames[:count]:
        img = Image.new("RGB", (ICON_WIDTH, ICON_HEIGHT), BLACK)
        img.putdata([f[y][x] for y in range(ICON_HEIGHT) for x in range(ICON_WIDTH)])
        images.append(img)

    stream = BytesIO()
    if animated and count > 1:
        images[0].save(
            stream,
            format="GIF",
            save_all=True,
            append_images=images[1:],
            duration=[FRAME_BEAT_MS] * count,
            loop=0,
            disposal=1,
            optimize=False,
        )
    else:
        images[0].save(stream, format="GIF", optimize=True)

    data = stream.getvalue()
    validate_icon_gif(data)
    return data


def validate_icon_gif(data: bytes) -> None:
    if len(data) > ICON_MAX_BYTES:
        raise ValueError(f"Icon is {len(data)} bytes; limit is {ICON_MAX_BYTES}")
    if not data.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("Not a valid GIF")
    with Image.open(BytesIO(data)) as img:
        if img.size != (ICON_WIDTH, ICON_HEIGHT):
            raise ValueError(f"Icon size {img.size} is not (8, 8)")
        if "transparency" in img.info:
            raise ValueError("Icon must be fully opaque")
        for idx in range(img.n_frames):
            img.seek(idx)
            dur = int(img.info.get("duration", 0))
            if dur and dur % FRAME_BEAT_MS != 0:
                raise ValueError(f"Frame {idx} duration {dur}ms is off {FRAME_BEAT_MS}ms beat")


# ---------------------------------------------------------------- Data Lanes

def _fit_label(prefix: str, value: str) -> str:
    """Keep the repo prefix only while it leaves the value uncrowded.

    The lane is 23px and the value is the information, so the prefix degrades
    to its initial and then disappears rather than pushing the value out.
    """
    for candidate in (f"{prefix} {value}", f"{prefix[:1]} {value}", value):
        if text_width(candidate) <= LANE_WIDTH - 2:
            return candidate

    # The bare value can still overrun, and returning it unchecked was a real
    # defect: run_number comes straight from the GitHub API unfiltered, and
    # "#100000" measures 27px.  render_ci_lane_frame centres with
    # (LANE_WIDTH - width) // 2, which goes negative past 21px, so the label
    # started at x=7 -- seven glyph pixels clamped off the panel AND the
    # mandatory black gutter at column 8 lit on rows 2 and 4.
    #
    # Trimming is lossy for a run number, so this is a last resort the three
    # candidates above are meant to avoid; it exists so the geometry invariant
    # can never be broken by an input nobody anticipated.
    trimmed = value
    while trimmed and text_width(trimmed) > LANE_WIDTH - 2:
        trimmed = trimmed[:-1]
    return trimmed


def _ci_status_text(run: WorkflowRun) -> str:
    if run.status == "completed":
        return "PASS" if run.conclusion == "success" else "FAIL"
    # run_number is unbounded (it comes straight from the API), and a
    # truncated number is worse than no number; the gear icon already says
    # "in progress", so fall back to a marker that cannot be misread.
    text = f"#{run.run_number}"
    return text if text_width(text) <= LANE_WIDTH - 6 else "RUN"


def _repo_initial(repo: str) -> str:
    return repo.split("/")[-1][:1].upper() or "?"


def render_ci_lane_frame(run: WorkflowRun) -> Frame:
    """Calm glance: the repo's initial as a grey label, the result in white.

    Several repos share one slot, so the initial stays; the pass/fail colour
    now lives only in the icon (tick, cross or gear).  No scale, so the meter
    row is a plain line.
    """
    return render_glance_lane(_ci_status_text(run), label=_repo_initial(run.repo))


PR_AGE_SCALE_SECONDS = 72 * 3600


def render_pr_lane_frame(report: PRRadarReport) -> Frame:
    """Calm glance: repo initial, then open PR count; the meter is how long
    the oldest one has waited, out of three days."""
    age = report.oldest_waiting_seconds
    level = "good" if age < 24 * 3600 else "fair" if age < 48 * 3600 else "bad"
    value = f"{report.open_prs} PR"
    if glance_text_width(value) > LANE_WIDTH:
        value = str(report.open_prs)  # a huge count keeps its digits, loses "PR"
    return render_glance_lane(value, label=_repo_initial(report.repo),
                              meter=age / PR_AGE_SCALE_SECONDS, level=level)


def lane_bitmap_b64(frame: Frame) -> str:
    raw = bytes(
        channel
        for y in range(HEIGHT)
        for x in range(LANE_X0, LANE_X0 + LANE_WIDTH)
        for channel in frame[y][x]
    )
    return base64.b64encode(raw).decode("ascii")


def build_ci_payload(run: WorkflowRun, animated: bool = True) -> dict:
    if run.status == "completed":
        if run.conclusion == "success":
            icon = build_icon_gif(draw_ci_pass_icon, frame_count=1, animated=False)
        else:
            icon = build_icon_gif(draw_ci_fail_icon, frame_count=2, animated=animated)
    else:
        # Two frames, not four: the gear alternates between two tooth
        # sets, so frames 3 and 4 were byte-for-byte repeats of 1 and 2.
        icon = build_icon_gif(draw_ci_gear_icon, frame_count=2, animated=animated)

    frame = render_ci_lane_frame(run)
    lane_b64 = lane_bitmap_b64(frame)

    payload = {
        "icon": base64.b64encode(icon).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "draw": [["bitmap", LANE_X0, 0, LANE_WIDTH, LANE_HEIGHT, lane_b64]],
        "backgroundColor": "#000000",
        "durationMs": PAGE_DURATION_MS,
        "lifetimeMs": LIFETIME_MS,
        "lifetimeExpiry": "remove",
    }
    raw_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw_json) >= PAYLOAD_MAX_BYTES:
        raise ValueError("CI payload exceeded 2KB safety budget")
    return payload


def build_pr_payload(report: PRRadarReport, animated: bool = True) -> dict:
    def draw_fn(f: Frame, phase: int) -> None:
        draw_octocat_pr_icon(f, phase, report.oldest_waiting_seconds)

    frame_count = 2 if report.oldest_waiting_seconds >= 48 * 3600 else 1
    icon = build_icon_gif(draw_fn, frame_count=frame_count, animated=animated)
    frame = render_pr_lane_frame(report)
    lane_b64 = lane_bitmap_b64(frame)

    payload = {
        "icon": base64.b64encode(icon).decode("ascii"),
        "iconMode": "fixed",
        "iconOffsetX": 0,
        "draw": [["bitmap", LANE_X0, 0, LANE_WIDTH, LANE_HEIGHT, lane_b64]],
        "backgroundColor": "#000000",
        "durationMs": PAGE_DURATION_MS,
        "lifetimeMs": LIFETIME_MS,
        "lifetimeExpiry": "remove",
    }
    raw_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw_json) >= PAYLOAD_MAX_BYTES:
        raise ValueError("PR payload exceeded 2KB safety budget")
    return payload


# ---------------------------------------------------------------- API Client

class GitHubClient:
    """Lightweight GitHub API client fetching workflow runs and PR counts."""

    def __init__(self, token: str | None = None, timeout: float = 5.0) -> None:
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.timeout = float(timeout)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "AWTRIX-GitHub-Sentinel",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def fetch_latest_run(self, repo: str) -> WorkflowRun | None:
        if requests is None:
            return None
        url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=1"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
            if resp.status_code != 200:
                return None
            data = resp.json()
            runs = data.get("workflow_runs", [])
            if not runs:
                return None
            run = runs[0]
            return WorkflowRun(
                repo=repo,
                run_number=int(run.get("run_number", 0)),
                status=str(run.get("status", "completed")),
                conclusion=run.get("conclusion"),
                branch=str(run.get("head_branch", "main")),
            )
        except Exception:
            return None

    def fetch_pr_radar(self, repo: str) -> PRRadarReport | None:
        if requests is None:
            return None
        url = f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=10"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
            if resp.status_code != 200:
                return None
            prs = resp.json()
            if not isinstance(prs, list):
                return None
            now = time.time()
            oldest_age = 0.0
            for pr in prs:
                created_at_str = pr.get("created_at")
                if created_at_str:
                    try:
                        # Simple ISO timestamp conversion
                        import datetime
                        dt = datetime.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                        age = max(0.0, now - dt.timestamp())
                        if age > oldest_age:
                            oldest_age = age
                    except Exception:
                        pass
            return PRRadarReport(
                repo=repo,
                open_prs=len(prs),
                review_requested=0,
                oldest_waiting_seconds=oldest_age,
            )
        except Exception:
            return None


# ---------------------------------------------------------------- Controller

@dataclass(frozen=True)
class GitHubConfig:
    enabled: bool = True
    repos: tuple[str, ...] = ()
    token: str | None = None
    poll_interval_seconds: float = 60.0
    pr_radar_enabled: bool = True


@dataclass(frozen=True)
class GitHubTick:
    pushed: bool = False
    active_repo: str | None = None
    has_failure: bool = False


class GitHubSentinelController:
    """Rotates CI/CD and PR Radar status onto the AWTRIX panel."""

    def __init__(
        self,
        panel: Any,
        config: GitHubConfig,
        *,
        client: GitHubClient | None = None,
        clock: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.panel = panel
        self.config = config
        self.client = client or GitHubClient(token=config.token)
        self.clock = clock or time
        self.logger = logger or logging.getLogger("awtrix.utility_hub.github")
        self.last_poll_at: float | None = None
        self.latest_runs: dict[str, WorkflowRun] = {}
        self.latest_prs: dict[str, PRRadarReport] = {}
        self.repo_index = 0

    def poll(self) -> None:
        if not self.config.repos:
            return
        for repo in self.config.repos:
            run = self.client.fetch_latest_run(repo)
            if run:
                self.latest_runs[repo] = run
            if self.config.pr_radar_enabled:
                radar = self.client.fetch_pr_radar(repo)
                if radar:
                    self.latest_prs[repo] = radar

    def tick(self) -> GitHubTick:
        if not self.config.enabled or not self.config.repos:
            return GitHubTick(pushed=False)

        now = getattr(self.clock, "monotonic", time.monotonic)()

        if self.last_poll_at is None or (now - self.last_poll_at) >= self.config.poll_interval_seconds:
            self.poll()
            self.last_poll_at = now

        if not self.latest_runs and not self.latest_prs:
            return GitHubTick(pushed=False)

        # Check for any failing builds -> immediate priority
        has_failure = any(
            run.status == "completed" and run.conclusion == "failure"
            for run in self.latest_runs.values()
        )

        repo = self.config.repos[self.repo_index % len(self.config.repos)]
        self.repo_index += 1

        pushed = False
        if repo in self.latest_runs:
            run = self.latest_runs[repo]
            payload = build_ci_payload(run, animated=True)
            try:
                self.panel.push_app(APP_NAME, payload)
                if run.conclusion == "failure":
                    try:
                        self.panel.activate_app(APP_NAME)
                    except Exception:
                        pass
                pushed = True
            except Exception as err:
                self.logger.warning("Failed to push GitHub CI status: %s", err)

        return GitHubTick(pushed=pushed, active_repo=repo, has_failure=has_failure)

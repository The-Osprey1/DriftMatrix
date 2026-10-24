r"""Read-only attribution of the panel's heap decay and low FPS.

The Hub leaves one ``panel_health.jsonl`` line per ~30 s probe (free heap,
largest block, fps, uptime) and, once the mutation journal lands, one
``panel_events.jsonl`` line per mutating request.  This joins the two so a
falling largest block or a slow page can be pinned on what was sent in the
interval it happened.  With 30 s samples that is correlation, never proof.

From 2026-10-09 the health lines also carry Wi-Fi (``rssi``, ``wconn``,
``watt``, ``wstate``, ``werr``).  Each unreachable run is then shown with the
probe before and after it, so a Wi-Fi drop (same boot, ``connects`` up, often
weak signal before) can be told from a crash (uptime restarted).  Older lines
simply lack those fields and the Wi-Fi parts say so.

Both logs rotate once at 1 MB; the rotated ``.1.jsonl`` is read first.
Nothing is written anywhere and nothing talks to the panel::

    C:/Users/devyn/anaconda3/python.exe panel_heap_report.py --since-hours 72
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Callable, Iterable, Sequence, TextIO, TypeVar

from focus_control import default_data_dir


HEALTH_LOG_NAME = "panel_health.jsonl"
EVENTS_LOG_NAME = "panel_events.jsonl"
BLOCK_OFFSETS: tuple[tuple[str, int], ...] = (
    ("+0", 0), ("+10m", 600), ("+30m", 1800),
    ("+1h", 3600), ("+6h", 6 * 3600), ("+24h", 24 * 3600),
)
# Probes are ~30 s apart but the Hub can be late or the PC asleep; a sample
# further than this from the target uptime describes a different moment.
OFFSET_TOLERANCE_SECONDS = 120
OFFSET_TOLERANCE_FRACTION = 0.1
# t - uptime is the boot instant and holds within ~2 s over two days here, so
# a bigger jump means the panel rebooted inside a gap even though uptime grew.
BOOT_EPOCH_SLACK_SECONDS = 300
EARLY_UPTIME_SECONDS = 3600
FPS_TARGET = 40
SILENCE_SECONDS = 300
TOP_DROPS = 15
TEXT_ROWS = 12
# Wi-Fi keys the Hub writes from 2026-10-09 (hub_status.append_health). RSSI
# is whole dBm, so the bands are inclusive integer ranges: "-61..-67" is the
# -60..-67 band without the -60 that ">= -60" already holds. A 10-minute
# mean is rounded to the nearest dBm before it is placed in a band.
WIFI_KEYS = ("rssi", "wconn", "watt", "wstate", "werr")
RSSI_MIN, RSSI_MAX = -120, -1
RSSI_BUCKETS: tuple[tuple[str, int | None, int | None], ...] = (
    (">= -60", -60, None), ("-61..-67", -67, -61), ("-68..-72", -72, -68),
    ("-73..-78", -78, -73), ("<= -79", None, -79),
)
RSSI_WINDOW_SECONDS = 600
WIFI_STATE_CHARS = 16
WIFI_ERROR_CHARS = 48
MISSING = "\u2014"
# This PC's ANSI code page is 932, which has no em dash; piped output would
# otherwise print "?" where a value is missing.
ASCII_FALLBACKS = {MISSING: "-", "\u00d7": "x"}

T = TypeVar("T")


@dataclass(frozen=True)
class Sample:
    """One health probe; ``has_app`` separates "nothing reported" from older lines."""
    t: float
    reachable: bool
    uptime: int | None = None
    reason: str | None = None
    free: int | None = None
    min_free: int | None = None
    block: int | None = None
    fps: float | None = None
    app: str | None = None
    power: bool | None = None
    has_app: bool = False
    error: str | None = None
    # Wi-Fi; ``has_wifi`` separates "the panel reported nothing" from a line
    # written before the Hub logged these keys, the same way ``has_app`` does.
    rssi: int | None = None
    wconn: int | None = None
    watt: int | None = None
    wstate: str | None = None
    werr: str | None = None
    has_wifi: bool = False


@dataclass(frozen=True)
class Event:
    """One mutating request from the journal; ``t`` is when it started."""
    t: float
    src: str
    op: str
    app: str | None = None
    bytes: int | None = None
    status: int | None = None
    ms: int | None = None
    wait_ms: int | None = None
    err: str | None = None
    gate: str | None = None


@dataclass(frozen=True)
class LogFile:
    log: str
    name: str
    lines: int
    malformed: int
    error: str | None = None


@dataclass(frozen=True)
class Logs:
    """``samples`` and ``events`` are the window; ``history`` is every sample.

    Outages are found in ``history`` so that one which began before the
    window keeps its real start and its probe before (section 6).
    """
    samples: tuple[Sample, ...]
    events: tuple[Event, ...]
    files: tuple[LogFile, ...]
    events_available: bool
    history: tuple[Sample, ...] = ()


@dataclass(frozen=True)
class BootSummary:
    index: int
    started: float
    reason: str | None
    first_t: float
    last_t: float
    first_uptime: int
    covered_seconds: float
    block_at: dict[str, int | None]
    min_block: int | None
    min_min_free: int | None
    samples: int
    inferred: bool
    # Wi-Fi over the boot. ``wifi_connects_delta`` is how far "connects" rose
    # inside it: anything above 0 is the panel reconnecting without a reboot.
    rssi_samples: int = 0
    rssi_min: int | None = None
    rssi_median: float | None = None
    rssi_max: int | None = None
    wifi_connects_first: int | None = None
    wifi_connects_last: int | None = None
    wifi_connects_delta: int | None = None


@dataclass(frozen=True)
class Interval:
    """Two consecutive block readings in one boot and the mutations between them."""
    boot: int
    before: Sample
    after: Sample
    events: tuple[Event, ...] = ()

    @property
    def delta(self) -> int:
        return self.after.block - self.before.block

    @property
    def early(self) -> bool:
        return self.before.uptime < EARLY_UPTIME_SECONDS


@dataclass(frozen=True)
class MutationGroup:
    src: str
    op: str
    app: str | None
    count: int
    bytes: int


@dataclass(frozen=True)
class Drop:
    boot: int
    t_before: float
    t: float
    uptime: int
    block_before: int
    block_after: int
    delta: int
    app_before: str | None
    app_after: str | None
    mutations: tuple[MutationGroup, ...]


@dataclass(frozen=True)
class DeltaStats:
    intervals: int
    mean_delta: float | None
    early_intervals: int
    early_mean_delta: float | None
    late_intervals: int
    late_mean_delta: float | None


@dataclass(frozen=True)
class AttributionRow:
    src: str
    op: str
    app: str | None
    count: int
    bytes: int
    stats: DeltaStats
    vs_baseline: float | None


@dataclass(frozen=True)
class Attribution:
    """Mean block change of intervals holding a mutation, against quiet intervals."""
    baseline: DeltaStats
    rows: tuple[AttributionRow, ...]
    outside_intervals: int


@dataclass(frozen=True)
class FpsRow:
    app: str
    power: str
    samples: int
    mean: float
    min: float
    below_target_pct: float


@dataclass(frozen=True)
class FpsReport:
    rows: tuple[FpsRow, ...]
    without_app: int
    overall: FpsRow | None


@dataclass(frozen=True)
class RateRow:
    src: str
    events: int
    per_hour: float
    bytes: int
    bytes_per_hour: float
    peak_hour_events: int
    errors: int


@dataclass(frozen=True)
class Rates:
    start: float
    end: float
    hours: float
    rows: tuple[RateRow, ...]
    total: RateRow
    busiest_hour_start: float
    busiest_hour_events: int
    busiest_hour_bytes: int


@dataclass(frozen=True)
class EdgeProbe:
    """The reachable probe just before, or just after, an unreachable run."""
    t: float
    uptime: int
    reason: str | None
    has_wifi: bool
    rssi: int | None
    state: str | None
    connects: int | None
    attempts: int | None
    last_error: str | None


@dataclass(frozen=True)
class Outage:
    """A run of failed probes, with what the panel said on either side of it.

    ``evidence`` reads the two edges: ``wifi_reconnect`` (same boot, connects
    rose: the panel dropped off Wi-Fi and came back without rebooting),
    ``no_reconnect`` (same boot, connects unchanged), ``rebooted`` (uptime
    restarted, so the counters did too), ``stale_before`` (a reboot or a
    connects rise, but the probe before is more than SILENCE_SECONDS older
    than the run, so it happened somewhere in that span, not necessarily in
    the run), ``ongoing`` (no probe after it yet) or ``unknown`` (an edge, or
    its connects, is missing).
    """
    start: float
    end: float
    seconds: float
    failed_probes: int
    errors: tuple[str, ...]
    recovered: bool
    before: EdgeProbe | None = None
    after: EdgeProbe | None = None
    rebooted: bool | None = None
    connects_delta: int | None = None
    rssi_10m_mean: float | None = None
    rssi_10m_probes: int = 0
    evidence: str = "unknown"


@dataclass(frozen=True)
class Silence:
    start: float
    end: float
    seconds: float
    rebooted: bool | None


@dataclass(frozen=True)
class Gaps:
    outages: tuple[Outage, ...]
    silences: tuple[Silence, ...]


@dataclass(frozen=True)
class RssiBucket:
    """Probes in one RSSI band, and the unreachable runs whose 10 minutes before fell in it."""
    label: str
    low: int | None
    high: int | None
    probes: int
    share_pct: float
    runs: int
    runs_per_1k_probes: float | None


@dataclass(frozen=True)
class WifiReport:
    reachable: int
    probes_with_wifi: int
    probes_with_rssi: int
    rssi_mean: float | None
    rssi_median: float | None
    buckets: tuple[RssiBucket, ...]
    runs: int
    runs_with_rssi: int
    runs_rssi_mean: float | None
    runs_below_median: int
    evidence: dict[str, int]


@dataclass(frozen=True)
class Report:
    data_dir: str
    generated_at: float
    since: float | None
    first_t: float | None
    last_t: float | None
    files: tuple[LogFile, ...]
    samples: int
    unreachable: int
    events_available: bool
    events: int
    boots: tuple[BootSummary, ...]
    drops: tuple[Drop, ...]
    attribution: Attribution | None
    fps: FpsReport
    rates: Rates | None
    gaps: Gaps
    wifi: WifiReport


# -- parsing -----------------------------------------------------------------

def _number(value: Any) -> float | int | None:
    if type(value) in (int, float) and math.isfinite(value):
        return value
    return None


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _rssi(value: Any) -> int | None:
    return value if type(value) is int and RSSI_MIN <= value <= RSSI_MAX else None


def _bounded(value: Any, limit: int) -> str | None:
    """The writer already bounds these; a hand-edited log must not widen the report."""
    text = _str(value)
    return None if text is None else "".join(c for c in text[:limit] if c.isprintable()) or None


def parse_sample(raw: Any) -> Sample | None:
    """A health line, or None when it cannot be placed on the timeline.

    Lines from before 2026-10-04 lack ``reachable``; lines before the Hub
    learned ``app``/``power`` lack those, and lines before 2026-10-09 lack the
    Wi-Fi keys.  Junk in a value field only blanks that field, because the
    rest of the line is still evidence.
    """
    if not isinstance(raw, dict):
        return None
    t = _number(raw.get("t"))
    reachable = raw.get("reachable", True)
    if t is None or type(reachable) is not bool:
        return None
    if not reachable:
        return Sample(t=float(t), reachable=False, error=_str(raw.get("error")))
    uptime = _int(raw.get("uptime"))
    if uptime is None or uptime < 0:
        return None
    fps = _number(raw.get("fps"))
    power = raw.get("power")
    return Sample(
        t=float(t), reachable=True, uptime=uptime, reason=_str(raw.get("reason")),
        free=_int(raw.get("free")), min_free=_int(raw.get("min_free")),
        block=_int(raw.get("block")), fps=None if fps is None else float(fps),
        app=_str(raw.get("app")), power=power if type(power) is bool else None,
        has_app="app" in raw,
        rssi=_rssi(raw.get("rssi")), wconn=_count(raw.get("wconn")), watt=_count(raw.get("watt")),
        wstate=_bounded(raw.get("wstate"), WIFI_STATE_CHARS),
        werr=_bounded(raw.get("werr"), WIFI_ERROR_CHARS),
        has_wifi=any(key in raw for key in WIFI_KEYS),
    )


def parse_event(raw: Any) -> Event | None:
    """A journal line, or None without the time, source and operation."""
    if not isinstance(raw, dict):
        return None
    t, src, op = _number(raw.get("t")), _str(raw.get("src")), _str(raw.get("op"))
    if t is None or src is None or op is None:
        return None
    size = _int(raw.get("bytes"))
    return Event(
        t=float(t), src=src, op=op, app=_str(raw.get("app")),
        bytes=size if size is not None and size >= 0 else None,
        status=_int(raw.get("status")), ms=_int(raw.get("ms")),
        wait_ms=_int(raw.get("wait_ms")), err=_str(raw.get("err")),
        gate=_str(raw.get("gate")),
    )


def log_paths(data_dir: Path, name: str) -> tuple[Path, Path]:
    """Rotated then live, the same naming ``hub_status`` rotates to."""
    current = data_dir / name
    return current.with_suffix(".1.jsonl"), current


def read_records(data_dir: Path, name: str, log: str,
                 parse: Callable[[Any], T | None]) -> tuple[list[T], list[LogFile]]:
    """Oldest first; a half-written or garbled line is counted, never fatal."""
    records: list[T] = []
    files: list[LogFile] = []
    for path in log_paths(data_dir, name):
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                lines = [line for line in handle if line.strip()]
        except FileNotFoundError:
            continue
        except OSError as error:
            files.append(LogFile(log, path.name, 0, 0, type(error).__name__))
            continue
        malformed = 0
        for line in lines:
            try:
                item = parse(json.loads(line))
            except (ValueError, RecursionError):
                item = None
            if item is None:
                malformed += 1
            else:
                records.append(item)
        files.append(LogFile(log, path.name, len(lines), malformed))
    return records, files


def load_logs(data_dir: Path, since: float | None = None) -> Logs:
    """Both logs, time-ordered: several senders append to the journal concurrently."""
    samples, health_files = read_records(data_dir, HEALTH_LOG_NAME, "health", parse_sample)
    events, event_files = read_records(data_dir, EVENTS_LOG_NAME, "events", parse_event)
    history = tuple(sorted(samples, key=lambda s: s.t))
    if since is not None:
        samples = [s for s in samples if s.t >= since]
        events = [e for e in events if e.t >= since]
    return Logs(
        samples=tuple(sorted(samples, key=lambda s: s.t)),
        events=tuple(sorted(events, key=lambda e: e.t)),
        files=tuple(health_files + event_files),
        events_available=any(f.error is None for f in event_files),
        history=history,
    )


# -- 1. boots ----------------------------------------------------------------

def boot_instant(sample: Sample) -> float:
    return sample.t - sample.uptime


def is_new_boot(previous: Sample, current: Sample) -> bool:
    """Uptime going backwards, or the boot instant moving across a gap."""
    if current.uptime < previous.uptime:
        return True
    return boot_instant(current) - boot_instant(previous) > BOOT_EPOCH_SLACK_SECONDS


def split_boots(samples: Iterable[Sample]) -> list[list[Sample]]:
    """A failed probe says nothing about uptime, so it never starts a boot."""
    boots: list[list[Sample]] = []
    for sample in samples:
        if not sample.reachable:
            continue
        if not boots or is_new_boot(boots[-1][-1], sample):
            boots.append([])
        boots[-1].append(sample)
    return boots


def offset_tolerance(offset: int) -> float:
    return max(OFFSET_TOLERANCE_SECONDS, offset * OFFSET_TOLERANCE_FRACTION)


def block_near(samples: Sequence[Sample], offset: int) -> int | None:
    """Largest block of the sample nearest that uptime, if any is close enough."""
    sized = [s for s in samples if s.block is not None]
    if not sized:
        return None
    nearest = min(sized, key=lambda s: abs(s.uptime - offset))
    return nearest.block if abs(nearest.uptime - offset) <= offset_tolerance(offset) else None


def _min(values: Iterable[int | None]) -> int | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def connects_rise(values: Sequence[int]) -> int | None:
    """Sum of the rises between readings; a fall (never seen within a boot) adds nothing."""
    if not values:
        return None
    return sum(max(0, later - earlier) for earlier, later in zip(values, values[1:]))


def boot_wifi(boot: Sequence[Sample]) -> dict[str, Any]:
    rssi = [s.rssi for s in boot if s.rssi is not None]
    connects = [s.wconn for s in boot if s.wconn is not None]
    return {
        "rssi_samples": len(rssi),
        "rssi_min": min(rssi) if rssi else None,
        "rssi_median": float(statistics.median(rssi)) if rssi else None,
        "rssi_max": max(rssi) if rssi else None,
        "wifi_connects_first": connects[0] if connects else None,
        "wifi_connects_last": connects[-1] if connects else None,
        "wifi_connects_delta": connects_rise(connects),
    }


def summarize_boots(boots: Sequence[Sequence[Sample]]) -> list[BootSummary]:
    summaries = []
    for index, boot in enumerate(boots, 1):
        first, last = boot[0], boot[-1]
        inferred = index > 1 and first.uptime >= boots[index - 2][-1].uptime
        summaries.append(BootSummary(
            index=index, started=boot_instant(first),
            reason=next((s.reason for s in boot if s.reason), None),
            first_t=first.t, last_t=last.t, first_uptime=first.uptime,
            covered_seconds=last.t - first.t,
            block_at={label: block_near(boot, offset) for label, offset in BLOCK_OFFSETS},
            min_block=_min(s.block for s in boot),
            min_min_free=_min(s.min_free for s in boot),
            samples=len(boot), inferred=inferred, **boot_wifi(boot),
        ))
    return summaries


# -- 2. drops ----------------------------------------------------------------

def block_intervals(boots: Sequence[Sequence[Sample]], events: Sequence[Event]) -> list[Interval]:
    """Pair block readings within a boot; each owns the events in (before.t, after.t]."""
    events = sorted(events, key=lambda e: e.t)
    times = [e.t for e in events]
    intervals = []
    for index, boot in enumerate(boots, 1):
        sized = [s for s in boot if s.block is not None]
        for before, after in zip(sized, sized[1:]):
            low, high = bisect_right(times, before.t), bisect_right(times, after.t)
            intervals.append(Interval(index, before, after, tuple(events[low:high])))
    return intervals


def group_events(events: Iterable[Event]) -> tuple[MutationGroup, ...]:
    counts: Counter[tuple[str, str, str | None]] = Counter()
    sizes: Counter[tuple[str, str, str | None]] = Counter()
    for event in events:
        key = (event.src, event.op, event.app)
        counts[key] += 1
        sizes[key] += event.bytes or 0
    groups = [MutationGroup(*key, counts[key], sizes[key]) for key in counts]
    return tuple(sorted(groups, key=lambda g: (-g.count, -g.bytes, g.src, g.op, g.app or "")))


def biggest_drops(intervals: Sequence[Interval], limit: int = TOP_DROPS) -> list[Drop]:
    falling = sorted((i for i in intervals if i.delta < 0), key=lambda i: (i.delta, i.after.t))
    return [Drop(
        boot=i.boot, t_before=i.before.t, t=i.after.t, uptime=i.after.uptime,
        block_before=i.before.block, block_after=i.after.block, delta=i.delta,
        app_before=i.before.app, app_after=i.after.app, mutations=group_events(i.events),
    ) for i in falling[:limit]]


# -- 3. attribution ----------------------------------------------------------

def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def delta_stats(intervals: Sequence[Interval]) -> DeltaStats:
    """Split at an hour of uptime: boot-time fragmentation is not a sender's doing."""
    early = [i.delta for i in intervals if i.early]
    late = [i.delta for i in intervals if not i.early]
    return DeltaStats(len(intervals), _mean(early + late),
                      len(early), _mean(early), len(late), _mean(late))


def attribute_mutations(intervals: Sequence[Interval], events: Sequence[Event]) -> Attribution:
    """Count and bytes cover every event; deltas only the ones inside an interval.

    Rows sort by excess decay (difference from baseline times intervals), so a
    frequent small leak ranks next to a rare large one.
    """
    baseline = delta_stats([i for i in intervals if not i.events])
    holding: defaultdict[tuple[str, str, str | None], list[Interval]] = defaultdict(list)
    for interval in intervals:
        for key in dict.fromkeys((e.src, e.op, e.app) for e in interval.events):
            holding[key].append(interval)
    rows = []
    for group in group_events(events):
        stats = delta_stats(holding.get((group.src, group.op, group.app), []))
        versus = (None if stats.mean_delta is None or baseline.mean_delta is None
                  else stats.mean_delta - baseline.mean_delta)
        rows.append(AttributionRow(group.src, group.op, group.app, group.count,
                                   group.bytes, stats, versus))
    rows.sort(key=lambda r: (r.vs_baseline is None,
                             (r.vs_baseline or 0) * r.stats.intervals, -r.count))
    inside = sum(len(i.events) for i in intervals)
    return Attribution(baseline, tuple(rows), len(events) - inside)


# -- 4. fps ------------------------------------------------------------------

def _fps_row(app: str, power: str, values: Sequence[float]) -> FpsRow:
    below = sum(1 for v in values if v < FPS_TARGET)
    return FpsRow(app, power, len(values), sum(values) / len(values), min(values),
                  100.0 * below / len(values))


def fps_by_app(samples: Iterable[Sample]) -> FpsReport:
    """Dark-panel samples are split out: nightkeeper's blackout is not a slow page."""
    groups: defaultdict[tuple[str, str], list[float]] = defaultdict(list)
    everything: list[float] = []
    without_app = 0
    for sample in samples:
        if not sample.reachable or sample.fps is None:
            continue
        everything.append(sample.fps)
        if not sample.has_app:
            without_app += 1
            continue
        power = "on" if sample.power is True else "off/unknown"
        groups[(sample.app or "(none)", power)].append(sample.fps)
    rows = [_fps_row(app, power, values) for (app, power), values in groups.items()]
    rows.sort(key=lambda r: (-r.below_target_pct, -r.samples, r.app, r.power))
    overall = _fps_row("(all)", "any", everything) if everything else None
    return FpsReport(tuple(rows), without_app, overall)


# -- 5. rates ----------------------------------------------------------------

def _failed(event: Event) -> bool:
    return event.err is not None or (event.status is not None and event.status >= 400)


def mutation_rates(events: Sequence[Event], end: float) -> Rates | None:
    """Rates over at least an hour, so a fresh journal does not extrapolate wildly."""
    if not events:
        return None
    start = min(e.t for e in events)
    end = max(end, max(e.t for e in events))
    hours = max(end - start, 3600.0) / 3600.0

    def row(src: str, chosen: Sequence[Event]) -> RateRow:
        size = sum(e.bytes or 0 for e in chosen)
        per_hour = Counter(int(e.t // 3600) for e in chosen)
        return RateRow(src, len(chosen), len(chosen) / hours, size, size / hours,
                       max(per_hour.values()), sum(1 for e in chosen if _failed(e)))

    by_src: defaultdict[str, list[Event]] = defaultdict(list)
    for event in events:
        by_src[event.src].append(event)
    rows = sorted((row(src, chosen) for src, chosen in by_src.items()),
                  key=lambda r: (-r.events, r.src))
    buckets: defaultdict[int, list[Event]] = defaultdict(list)
    for event in events:
        buckets[int(event.t // 3600)].append(event)
    busiest = min(buckets, key=lambda h: (-len(buckets[h]), h))
    return Rates(start, end, hours, tuple(rows), row("all", events), busiest * 3600.0,
                 len(buckets[busiest]), sum(e.bytes or 0 for e in buckets[busiest]))


# -- 6. gaps -----------------------------------------------------------------

def edge_probe(sample: Sample | None) -> EdgeProbe | None:
    if sample is None:
        return None
    return EdgeProbe(sample.t, sample.uptime, sample.reason, sample.has_wifi, sample.rssi,
                     sample.wstate, sample.wconn, sample.watt, sample.werr)


def before_is_stale(start: float, before: Sample | EdgeProbe | None) -> bool:
    """The probe before a run is from before a silence (PC asleep, Hub stopped)."""
    return before is not None and start - before.t > SILENCE_SECONDS


def outage_evidence(before: Sample | None, after: Sample | None, start: float | None = None
                    ) -> tuple[bool | None, int | None, str]:
    """(rebooted, connects delta, evidence) from the probes either side of a run.

    With ``start`` (the run's first failed probe), a reboot or a connects rise
    across a probe before that is older than SILENCE_SECONDS is
    ``stale_before``: it happened somewhere since that probe, perhaps hours
    before the run (the 10-09 08:57 run had its probe before at 10-08 23:04).
    "Same boot, no reconnect" holds over the whole span, so it stays.
    """
    if after is None:
        return None, None, "ongoing"
    if before is None:
        return None, None, "unknown"
    stale = start is not None and before_is_stale(start, before)
    if is_new_boot(before, after):
        return True, None, "stale_before" if stale else "rebooted"
    if before.wconn is None or after.wconn is None:
        return False, None, "unknown"
    delta = after.wconn - before.wconn
    if delta > 0:
        return False, delta, "stale_before" if stale else "wifi_reconnect"
    return False, delta, "no_reconnect" if delta == 0 else "unknown"


def rssi_window(points: Sequence[tuple[float, int]], times: Sequence[float],
                start: float) -> list[int]:
    """RSSI readings in the RSSI_WINDOW_SECONDS before ``start`` (start excluded)."""
    low = bisect_left(times, start - RSSI_WINDOW_SECONDS)
    return [rssi for _, rssi in points[low:bisect_left(times, start)]]


def find_gaps(samples: Sequence[Sample], since: float | None = None) -> Gaps:
    """Failed-probe runs, and silences where the Hub (or the PC) was not looking.

    Each run keeps the reachable probe before it and the one after it, and
    the mean RSSI of the ten minutes before it: weak or falling signal before
    and ``connects`` up by one after, in the same boot, is a Wi-Fi drop.

    Give it every sample and ``since``: the runs and silences are found over
    the whole log, so one that began before the window keeps its real start
    and edges, and only those reaching into the window are returned.
    """
    points = sorted((s.t, s.rssi) for s in samples if s.reachable and s.rssi is not None)
    times = [t for t, _ in points]
    outages: list[Outage] = []
    run: list[Sample] = []
    last_reachable: Sample | None = None
    run_before: Sample | None = None
    for sample in [*samples, None]:
        if sample is not None and not sample.reachable:
            if not run:
                run_before = last_reachable
            run.append(sample)
            continue
        if run:
            end = sample.t if sample is not None else run[-1].t
            errors = tuple(dict.fromkeys(s.error or "unknown" for s in run))
            rebooted, delta, evidence = outage_evidence(run_before, sample, run[0].t)
            window = rssi_window(points, times, run[0].t)
            outages.append(Outage(
                run[0].t, end, end - run[0].t, len(run), errors, sample is not None,
                before=edge_probe(run_before), after=edge_probe(sample),
                rebooted=rebooted, connects_delta=delta,
                rssi_10m_mean=_mean(window), rssi_10m_probes=len(window), evidence=evidence,
            ))
            run = []
        if sample is not None:
            last_reachable = sample
    before: list[Sample | None] = []
    last = None
    for sample in samples:
        last = sample if sample.reachable else last
        before.append(last)
    after: list[Sample | None] = [None] * len(samples)
    upcoming = None
    for index in range(len(samples) - 1, -1, -1):
        upcoming = samples[index] if samples[index].reachable else upcoming
        after[index] = upcoming
    silences = []
    for index in range(1, len(samples)):
        start, end = samples[index - 1].t, samples[index].t
        if end - start > SILENCE_SECONDS:
            a, b = before[index - 1], after[index]
            rebooted = None if a is None or b is None else is_new_boot(a, b)
            silences.append(Silence(start, end, end - start, rebooted))
    if since is not None:
        outages = [o for o in outages if o.end >= since]
        silences = [s for s in silences if s.end >= since]
    return Gaps(tuple(outages), tuple(silences))


# -- 7. wi-fi ----------------------------------------------------------------

def rssi_bucket(value: float) -> int:
    """Index into RSSI_BUCKETS; a mean is rounded to the nearest whole dBm first."""
    whole = math.floor(value + 0.5)
    for index, (_, low, high) in enumerate(RSSI_BUCKETS):
        if (low is None or whole >= low) and (high is None or whole <= high):
            return index
    raise AssertionError("RSSI_BUCKETS must cover every integer")


def wifi_summary(samples: Sequence[Sample], outages: Sequence[Outage]) -> WifiReport:
    """Where the signal sat, and whether unreachable runs follow weaker signal.

    ``runs_per_1k_probes`` is the conditional rate: runs whose preceding
    10-minute mean fell in a band, per thousand probes spent in that band.
    With a handful of runs it is a hint, not a statistic.
    """
    reachable = [s for s in samples if s.reachable]
    rssi = [s.rssi for s in reachable if s.rssi is not None]
    probes = Counter(rssi_bucket(value) for value in rssi)
    measured = [o for o in outages if o.rssi_10m_mean is not None]
    runs = Counter(rssi_bucket(o.rssi_10m_mean) for o in measured)
    buckets = tuple(RssiBucket(
        label, low, high, probes[index],
        100.0 * probes[index] / len(rssi) if rssi else 0.0, runs[index],
        1000.0 * runs[index] / probes[index] if probes[index] else None,
    ) for index, (label, low, high) in enumerate(RSSI_BUCKETS))
    median = float(statistics.median(rssi)) if rssi else None
    below = sum(1 for o in measured if median is not None and o.rssi_10m_mean < median)
    return WifiReport(
        reachable=len(reachable),
        probes_with_wifi=sum(1 for s in reachable if s.has_wifi),
        probes_with_rssi=len(rssi), rssi_mean=_mean(rssi), rssi_median=median,
        buckets=buckets, runs=len(outages), runs_with_rssi=len(measured),
        runs_rssi_mean=_mean([o.rssi_10m_mean for o in measured]),
        runs_below_median=below,
        evidence=dict(sorted(Counter(o.evidence for o in outages).items())),
    )


# -- report ------------------------------------------------------------------

def build_report(logs: Logs, *, data_dir: Path, generated_at: float,
                 since: float | None = None) -> Report:
    boots = split_boots(logs.samples)
    intervals = block_intervals(boots, logs.events)
    stamps = [s.t for s in logs.samples] + [e.t for e in logs.events]
    last_t = max(stamps) if stamps else None
    gaps = find_gaps(logs.history or logs.samples, since=since)
    return Report(
        data_dir=str(data_dir), generated_at=generated_at, since=since,
        first_t=min(stamps) if stamps else None, last_t=last_t,
        files=logs.files, samples=len(logs.samples),
        unreachable=sum(1 for s in logs.samples if not s.reachable),
        events_available=logs.events_available, events=len(logs.events),
        boots=tuple(summarize_boots(boots)),
        drops=tuple(biggest_drops(intervals)),
        attribution=attribute_mutations(intervals, logs.events) if logs.events_available else None,
        fps=fps_by_app(logs.samples),
        rates=mutation_rates(logs.events, last_t) if logs.events else None,
        gaps=gaps, wifi=wifi_summary(logs.samples, gaps.outages),
    )


def to_json(report: Report) -> dict[str, Any]:
    return asdict(report)


# -- text --------------------------------------------------------------------

def fmt_int(value: int | float | None) -> str:
    return MISSING if value is None else f"{round(value):,}"


def fmt_delta(value: int | float | None) -> str:
    return MISSING if value is None else f"{round(value):+,}"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return MISSING
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(total // 3600, 24)
    return f"{days}d{hours:02d}h"


def fmt_local(t: float | None) -> str:
    return MISSING if t is None else datetime.fromtimestamp(t).strftime("%m-%d %H:%M")


def _key_text(src: str, op: str, app: str | None) -> str:
    return f"{src}/{op}/{app or '-'}"


def _apps_text(before: str | None, after: str | None) -> str:
    if before == after:
        return before or "-"
    return f"{before or '-'}->{after or '-'}"


def _mutations_text(groups: Sequence[MutationGroup], shown: int = 2) -> str:
    if not groups:
        return "none"
    parts = [f"{_key_text(g.src, g.op, g.app)}\u00d7{g.count} {fmt_int(g.bytes)} B"
             for g in groups[:shown]]
    if len(groups) > shown:
        parts.append(f"+{len(groups) - shown} more")
    return "; ".join(parts)


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], left: Iterable[int] = ()) -> list[str]:
    left = set(left)
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(header)]

    def line(cells: Sequence[str]) -> str:
        return ("  " + "  ".join(c.ljust(w) if i in left else c.rjust(w)
                                 for i, (c, w) in enumerate(zip(cells, widths)))).rstrip()
    return [line(header)] + [line(r) for r in rows]


def _more(total: int, shown: int, what: str) -> list[str]:
    return [f"  ... {total - shown} more {what} (all of them are in --json)"] if total > shown else []


def _header(report: Report) -> list[str]:
    lines = [f"Panel heap report (read-only), generated {fmt_local(report.generated_at)}",
             f"Data     {report.data_dir}"]
    health = [f for f in report.files if f.log == "health"]
    if health:
        files = ", ".join(f"{f.name} {fmt_int(f.lines)} lines" + (f" ({f.error})" if f.error else "")
                          for f in health)
        bad = sum(f.malformed for f in health)
        lines.append(f"Health   {files}; {fmt_int(bad)} malformed skipped")
    else:
        lines.append(f"Health   no {HEALTH_LOG_NAME} (or rotated file) in this directory")
    window = f"since {fmt_local(report.since)}; " if report.since is not None else ""
    span = (f"{fmt_local(report.first_t)} -> {fmt_local(report.last_t)} "
            f"({fmt_duration(report.last_t - report.first_t)})" if report.first_t is not None
            else "no records")
    lines.append(f"Window   {window}{span}; {fmt_int(report.samples)} probes, "
                 f"{fmt_int(report.unreachable)} unreachable")
    events = [f for f in report.files if f.log == "events"]
    if not report.events_available:
        lines.append(f"Journal  NO MUTATION JOURNAL: {EVENTS_LOG_NAME} not found, so drops are "
                     "unattributed and sections 3 and 5 are skipped")
    else:
        files = ", ".join(f"{f.name} {fmt_int(f.lines)} lines" for f in events if f.error is None)
        bad = sum(f.malformed for f in events)
        lines.append(f"Journal  {files}; {fmt_int(bad)} malformed skipped; "
                     f"{fmt_int(report.events)} mutations in window")
    return lines


def _boots_text(report: Report) -> list[str]:
    lines = ["1. Boots: largest free block by uptime (nearest probe, else \u2014)"]
    if not report.boots:
        return lines + ["  no reachable probes"]
    shown = report.boots[-TEXT_ROWS:]
    rows = [[str(b.index), fmt_local(b.started), (b.reason or "?") + ("*" if b.inferred else ""),
             fmt_duration(b.covered_seconds), *(fmt_int(b.block_at[label]) for label, _ in BLOCK_OFFSETS),
             fmt_int(b.min_block), fmt_int(b.min_min_free), fmt_int(b.samples)] for b in shown]
    header = ["#", "started", "reason", "covered", *(label for label, _ in BLOCK_OFFSETS),
              "min block", "min_free", "probes"]
    lines += _table(header, rows, left=(1, 2))
    if any(b.inferred for b in shown):
        lines.append("  * reboot inferred: uptime kept growing across a gap but the boot instant moved")
    if len(report.boots) > len(shown):
        lines.append(f"  ... {len(report.boots) - len(shown)} earlier boots (all of them are in --json)")
    return lines


def _drops_text(report: Report) -> list[str]:
    lines = [f"2. Biggest block drops (top {TOP_DROPS}, consecutive probes in one boot)"]
    if not report.drops:
        return lines + ["  no decreases"]
    header = ["boot", "at", "uptime", "span", "block", "delta", "app"]
    if report.events_available:
        header.append("mutations in (prev, this]")
    rows = []
    for d in report.drops:
        row = [str(d.boot), fmt_local(d.t), fmt_duration(d.uptime), fmt_duration(d.t - d.t_before),
               f"{fmt_int(d.block_before)}->{fmt_int(d.block_after)}", fmt_delta(d.delta),
               _apps_text(d.app_before, d.app_after)]
        if report.events_available:
            row.append(_mutations_text(d.mutations))
        rows.append(row)
    return lines + _table(header, rows, left=(1, 6, 7))


def _stats_cell(count: int, mean: float | None) -> str:
    return f"{count} @ {fmt_delta(mean)}" if count else MISSING


def _attribution_text(report: Report) -> list[str]:
    lines = ["3. Mutation attribution: correlation, not proof"]
    attribution = report.attribution
    if attribution is None:
        return lines + ["  skipped: no mutation journal"]
    base = attribution.baseline
    lines.append(f"  baseline: {fmt_int(base.intervals)} intervals with no mutation, mean block "
                 f"delta {fmt_delta(base.mean_delta)} (<=1h {_stats_cell(base.early_intervals, base.early_mean_delta)}, "
                 f">1h {_stats_cell(base.late_intervals, base.late_mean_delta)})")
    if not attribution.rows:
        return lines + ["  no mutations in window"]
    shown = attribution.rows[:TEXT_ROWS]
    rows = [[_key_text(r.src, r.op, r.app), fmt_int(r.count), fmt_int(r.bytes),
             fmt_int(r.stats.intervals), fmt_delta(r.stats.mean_delta), fmt_delta(r.vs_baseline),
             _stats_cell(r.stats.early_intervals, r.stats.early_mean_delta),
             _stats_cell(r.stats.late_intervals, r.stats.late_mean_delta)] for r in shown]
    lines += _table(["src/op/app", "n", "bytes", "intervals", "mean delta", "vs base",
                     "<=1h n @ delta", ">1h n @ delta"], rows, left=(0,))
    lines += _more(len(attribution.rows), len(shown), "keys")
    if attribution.outside_intervals:
        lines.append(f"  {fmt_int(attribution.outside_intervals)} mutations fell outside any probe "
                     "interval (gaps, boot edges); they count in n and bytes only")
    return lines


def _where(report: Report) -> str:
    return "in the window" if report.since is not None else "in the log"


def _fps_text(report: Report) -> list[str]:
    lines = [f"4. FPS by app (probes that carry \"app\"; target {FPS_TARGET})"]
    fps = report.fps
    if fps.rows:
        shown = fps.rows[:TEXT_ROWS]
        rows = [[r.app, r.power, fmt_int(r.samples), f"{r.mean:.1f}", f"{r.min:g}",
                 f"{r.below_target_pct:.0f}%"] for r in shown]
        lines += _table(["app", "power", "probes", "mean", "min", f"<{FPS_TARGET}"], rows, left=(0, 1))
        lines += _more(len(fps.rows), len(shown), "rows")
    elif report.samples == report.unreachable:
        lines.append(f"  no reachable probe {_where(report)}")
    else:
        lines.append("  no probe carries \"app\" yet (lines from the older Hub writer)")
    if fps.overall is not None:
        o = fps.overall
        older = f", {fmt_int(fps.without_app)} of them without app" if fps.without_app else ""
        lines.append(f"  all probes: {fmt_int(o.samples)}{older}; mean {o.mean:.1f}, min {o.min:g}, "
                     f"{o.below_target_pct:.0f}% below {FPS_TARGET}")
    return lines


def _rates_text(report: Report) -> list[str]:
    lines = ["5. Mutation rate"]
    if not report.events_available:
        return lines + ["  skipped: no mutation journal"]
    rates = report.rates
    if rates is None:
        return lines + ["  no mutations in window"]
    lines[0] += f" over {fmt_duration(rates.end - rates.start)} of journal (at least 1 h)"
    rows = [[r.src, fmt_int(r.events), f"{r.per_hour:,.1f}", fmt_int(r.bytes),
             fmt_int(r.bytes_per_hour), fmt_int(r.peak_hour_events), fmt_int(r.errors)]
            for r in (*rates.rows, rates.total)]
    lines += _table(["src", "events", "per h", "bytes", "bytes/h", "peak h", "errors"], rows, left=(0,))
    lines.append(f"  busiest hour {fmt_local(rates.busiest_hour_start)}: "
                 f"{fmt_int(rates.busiest_hour_events)} mutations, {fmt_int(rates.busiest_hour_bytes)} B")
    return lines


EVIDENCE_TEXT = {
    "wifi_reconnect": "Wi-Fi drop: same boot, connects +{delta}",
    "no_reconnect": "same boot, no reconnect counted",
    "rebooted": "rebooted: its counters restarted",
    "ongoing": "no probe after it yet",
    "unknown": "too little Wi-Fi data to tell",
}
EVIDENCE_LABEL = {
    "wifi_reconnect": "Wi-Fi reconnect", "no_reconnect": "same boot, no reconnect",
    "rebooted": "rebooted", "stale_before": "probe before too old to place it",
    "ongoing": "still down", "unknown": "unknown",
}


def evidence_text(outage: Outage) -> str:
    if outage.evidence == "stale_before":
        what = "rebooted" if outage.rebooted else f"connects +{outage.connects_delta}"
        return f"{what} at some point since the probe before, not necessarily in this run"
    return EVIDENCE_TEXT[outage.evidence].format(delta=outage.connects_delta)


def fmt_dbm(value: int | float | None) -> str:
    return MISSING if value is None else f"{value:g} dBm"


def fmt_g(value: int | float | None) -> str:
    return MISSING if value is None else f"{value:g}"


def _edge_has_wifi(outage: Outage) -> bool:
    return any(p is not None and p.has_wifi for p in (outage.before, outage.after))


def _edge_text(probe: EdgeProbe | None) -> str:
    if probe is None:
        return ": none"
    if not probe.has_wifi:
        return ": no Wi-Fi fields"
    return f" {fmt_dbm(probe.rssi)} {probe.state or MISSING} connects {fmt_int(probe.connects)}"


def _outage_wifi_lines(outage: Outage) -> list[str]:
    """One line of before | after, only when either side carries Wi-Fi fields.

    The probe after is the run's end, and the probe before is normally one
    interval ahead of its start, so their times are not repeated. When the
    Hub was not looking before the run (PC asleep), the probe before can be
    hours old, and then its age is shown: a connects rise is then somewhere
    in that whole span, not necessarily in the run.
    """
    if not _edge_has_wifi(outage):
        return []
    label = "before"
    if before_is_stale(outage.start, outage.before):
        label += f" ({fmt_duration(outage.start - outage.before.t)} earlier)"
    window = (f" (10-min mean {outage.rssi_10m_mean:.1f}, {outage.rssi_10m_probes} probes)"
              if outage.rssi_10m_mean is not None else " (no RSSI in the 10 min before)")
    after = (f"after{_edge_text(outage.after)} => " + evidence_text(outage)
             if outage.after is not None else "after: none yet")
    lines = [f"    {label}{_edge_text(outage.before)}{window} | {after}"]
    before_error = outage.before.last_error if outage.before is not None else None
    if outage.after is not None and outage.after.last_error and outage.after.last_error != before_error:
        lines.append(f'    panel\'s last Wi-Fi error after it: "{outage.after.last_error}"')
    return lines


def _gaps_text(report: Report) -> list[str]:
    lines = ["6. Gaps and outages"]
    gaps = report.gaps
    outages = gaps.outages[-TEXT_ROWS:]
    if not gaps.outages:
        lines.append("  unreachable: none")
    for o in outages:
        if not o.recovered:
            state = "still failing at end of log"
        else:
            reason = f" ({o.after.reason})" if o.rebooted and o.after and o.after.reason else ""
            # A probe before from ahead of a silence cannot place a reboot in the run.
            where = (f"at some point since {fmt_local(o.before.t)}"
                     if o.rebooted and before_is_stale(o.start, o.before) else "inside")
            state = "recovered" + {True: f", panel rebooted {where}{reason}", False: ", same boot",
                                   None: ""}[o.rebooted]
        probes = "probe" if o.failed_probes == 1 else "probes"
        lines.append(f"  unreachable {fmt_local(o.start)} for {fmt_duration(o.seconds)}: "
                     f"{o.failed_probes} failed {probes} ({', '.join(o.errors)}), {state}")
        lines += _outage_wifi_lines(o)
    lines += _more(len(gaps.outages), len(outages), "outages")
    blind = [o for o in outages if not _edge_has_wifi(o)]
    if blind:
        which = "none of these runs has" if len(blind) == len(outages) else f"{len(blind)} of these runs have"
        edgeless = sum(1 for o in blind if o.before is None and o.after is None)
        older = len(blind) - edgeless
        why = ("lines from the older Hub writer" if not edgeless
               else "no reachable probe on either side in the log" if not older
               else f"{older} with lines from the older Hub writer, "
                    f"{edgeless} with no reachable probe on either side in the log")
        lines.append(f"  Wi-Fi before/after: {which} Wi-Fi fields in the probes either side ({why})")
    silences = gaps.silences[-TEXT_ROWS:]
    if not gaps.silences:
        lines.append(f"  no probes for >{fmt_duration(SILENCE_SECONDS)}: none")
    for s in silences:
        boot = {True: ", panel rebooted inside", False: ", same boot", None: ""}[s.rebooted]
        lines.append(f"  no probes {fmt_local(s.start)} -> {fmt_local(s.end)} "
                     f"({fmt_duration(s.seconds)}{boot})")
    lines += _more(len(gaps.silences), len(silences), "silences")
    return lines


def _wifi_text(report: Report) -> list[str]:
    lines = ["7. Wi-Fi: signal per boot and band, and whether unreachable runs follow weak signal"]
    wifi = report.wifi
    if not wifi.reachable:
        return lines + [f"  no reachable probe {_where(report)}, so no Wi-Fi readings"]
    if not wifi.probes_with_wifi:
        return lines + ["  no probe carries Wi-Fi fields yet (lines from the older Hub writer; "
                        "the Hub logs rssi/wconn/watt/wstate from its next restart)"]
    lines.append(f"  {fmt_int(wifi.probes_with_rssi)} of {fmt_int(wifi.reachable)} reachable probes "
                 f"carry RSSI; mean {fmt_dbm(None if wifi.rssi_mean is None else round(wifi.rssi_mean, 1))}, "
                 f"median {fmt_dbm(wifi.rssi_median)}")
    boots = [b for b in report.boots if b.rssi_samples or b.wifi_connects_first is not None]
    shown = boots[-TEXT_ROWS:]
    if shown:
        rows = [[str(b.index), fmt_local(b.started), fmt_int(b.rssi_samples), fmt_int(b.rssi_min),
                 fmt_g(b.rssi_median), fmt_int(b.rssi_max),
                 f"{fmt_int(b.wifi_connects_first)}->{fmt_int(b.wifi_connects_last)}",
                 fmt_delta(b.wifi_connects_delta)] for b in shown]
        lines += _table(["boot", "started", "rssi probes", "min", "median", "max", "connects", "rise"],
                        rows, left=(1,))
        lines += _more(len(boots), len(shown), "boots")
        if any((b.wifi_connects_delta or 0) > 0 for b in shown):
            lines.append("  rise > 0: the panel reconnected to Wi-Fi without rebooting")
    if wifi.probes_with_rssi:
        rows = [[b.label, fmt_int(b.probes), f"{b.share_pct:.0f}%", fmt_int(b.runs),
                 MISSING if b.runs_per_1k_probes is None else f"{b.runs_per_1k_probes:.1f}"]
                for b in wifi.buckets]
        lines += _table(["RSSI dBm", "probes", "share", "runs", "runs/1k probes"], rows, left=(0,))
        lines.append(f"  runs: unreachable runs whose mean RSSI over the {fmt_duration(RSSI_WINDOW_SECONDS)} "
                     "before them falls in that band")
    if wifi.runs:
        if wifi.runs_with_rssi and wifi.rssi_mean is not None:
            lines.append(f"  {wifi.runs_with_rssi} of {wifi.runs} unreachable runs have RSSI in the "
                         f"{fmt_duration(RSSI_WINDOW_SECONDS)} before: mean {wifi.runs_rssi_mean:.1f} dBm "
                         f"vs {wifi.rssi_mean:.1f} over all probes ({wifi.runs_rssi_mean - wifi.rssi_mean:+.1f} dB), "
                         f"{wifi.runs_below_median} below the median ({fmt_dbm(wifi.rssi_median)})")
        else:
            lines.append(f"  none of the {wifi.runs} unreachable runs has RSSI in the "
                         f"{fmt_duration(RSSI_WINDOW_SECONDS)} before it")
        lines.append("  evidence across runs: " + ", ".join(
            f"{EVIDENCE_LABEL.get(key, key)} {count}" for key, count in wifi.evidence.items()))
    return lines


def format_report(report: Report) -> str:
    sections = [_header(report), _boots_text(report), _drops_text(report),
                _attribution_text(report), _fps_text(report), _rates_text(report),
                _gaps_text(report), _wifi_text(report)]
    return "\n\n".join("\n".join(section) for section in sections)


def for_stream(text: str, out: TextIO) -> str:
    """Swap in ASCII only for the marks the stream's encoding cannot carry."""
    encoding = getattr(out, "encoding", None) or "utf-8"
    for char, plain in ASCII_FALLBACKS.items():
        try:
            char.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            text = text.replace(char, plain)
    return text


# -- cli ---------------------------------------------------------------------

def _positive_hours(value: str) -> float:
    hours = float(value)
    if not math.isfinite(hours) or hours <= 0:
        raise argparse.ArgumentTypeError("hours must be a positive number")
    return hours


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="AWTRIX data directory (default: %%LOCALAPPDATA%%\\AWTRIX)")
    parser.add_argument("--since-hours", type=_positive_hours, default=None,
                        help="only records from the last H hours")
    parser.add_argument("--json", action="store_true", help="one JSON document instead of text")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None,
         clock: Callable[[], float] = time.time) -> int:
    """Exit 1 only when there is no health log at all, so a cron check can tell."""
    args = parse_args(argv)
    out = out if out is not None else sys.stdout
    now = clock()
    data_dir = args.data_dir if args.data_dir is not None else default_data_dir()
    since = None if args.since_hours is None else now - args.since_hours * 3600
    logs = load_logs(data_dir, since)
    report = build_report(logs, data_dir=data_dir, generated_at=now, since=since)
    if args.json:
        out.write(json.dumps(to_json(report), indent=2, allow_nan=False) + "\n")
    else:
        out.write(for_stream(format_report(report), out) + "\n")
    return 0 if any(f.log == "health" and f.error is None for f in logs.files) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    raise SystemExit(main())

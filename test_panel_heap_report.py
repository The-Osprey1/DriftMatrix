import io
import json
from pathlib import Path
import re

import pytest

import hub_status
import panel_heap_report as phr
from panel_client import DeviceHealth


T0 = 1_791_000_000.0
NOW = T0 + 10 * 86_400


def probe(t, uptime, block, **extra):
    line = {"t": t, "reachable": True, "uptime": uptime, "reason": "software",
            "free": 50_000, "min_free": 40_000, "block": block, "fps": 42}
    line.update(extra)
    return line


def failed(t, error="ConnectTimeout"):
    return {"t": t, "reachable": False, "error": error}


def wprobe(t, uptime, rssi, wconn, wstate="connected", **extra):
    """A probe as the Hub writes it from 2026-10-09: Wi-Fi keys always there, werr when set."""
    return probe(t, uptime, 30_000, rssi=rssi, wconn=wconn, watt=0, wstate=wstate, **extra)


def wifi_timeline():
    """A Wi-Fi drop, a stall, a crash and an outage still running at the end of the log."""
    return [
        wprobe(T0, 1000, -60, 1),            # older than 10 min before the first run
        wprobe(T0 + 300, 1300, -74, 1),
        wprobe(T0 + 600, 1600, -78, 1),
        failed(T0 + 700), failed(T0 + 730),  # A: same boot, connects 1 -> 2
        wprobe(T0 + 760, 1760, -70, 2, werr="beacon timeout"),
        failed(T0 + 790),                    # B: same boot, connects unchanged
        wprobe(T0 + 820, 1820, -66, 2, werr="beacon timeout"),
        failed(T0 + 850),                    # C: the panel came back from a panic
        wprobe(T0 + 880, 15, -65, 1, reason="panic"),
        failed(T0 + 910),                    # D: still down when the log ends
    ]


def event(t, src="hub", op="push", app="focus", size=100, **extra):
    line = {"t": t, "src": src, "op": op, "app": app, "bytes": size, "status": 200,
            "ms": 40, "wait_ms": 0, "err": None, "gate": "ok"}
    line.update(extra)
    return line


def write_log(path: Path, rows) -> None:
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows),
                    encoding="utf-8")


def parsed(*rows):
    return [phr.parse_sample(r) for r in rows]


def parsed_events(*rows):
    return [phr.parse_event(r) for r in rows]


def run(argv, clock=lambda: NOW):
    out = io.StringIO()
    code = phr.main(argv, out=out, clock=clock)
    return code, out.getvalue()


# -- parsing and reading -----------------------------------------------------

def test_old_lines_without_app_or_power_still_count_and_junk_fields_go_blank():
    old = phr.parse_sample({"t": 1791154227, "uptime": 1196, "reason": "software",
                            "free": 24908, "min_free": 15012, "block": 10228, "fps": 42})
    assert old.reachable and old.block == 10228 and not old.has_app
    assert old.app is None and old.power is None
    new = phr.parse_sample(probe(T0, 10, 2, app="ocean", power=True))
    assert new.has_app and new.app == "ocean" and new.power is True
    nothing_on_screen = phr.parse_sample(probe(T0, 10, 2, app=None, power=None))
    assert nothing_on_screen.has_app and nothing_on_screen.app is None
    junk = phr.parse_sample(probe(T0, 10, "big", fps=float("nan"), power="yes", free=True))
    assert (junk.block, junk.fps, junk.power, junk.free) == (None, None, None, None)
    down = phr.parse_sample(failed(T0))
    assert not down.reachable and down.error == "ConnectTimeout" and down.uptime is None


@pytest.mark.parametrize("raw", [
    [], "x", 7, {"t": "1", "uptime": 1}, {"t": True, "uptime": 1}, {"t": T0},
    {"t": T0, "uptime": -1}, {"t": T0, "uptime": 1.5}, {"t": T0, "uptime": True},
    {"t": T0, "reachable": "yes", "uptime": 1}, {"t": float("inf"), "uptime": 1},
])
def test_unplaceable_health_lines_are_rejected(raw):
    assert phr.parse_sample(raw) is None


def test_journal_lines_need_time_source_and_operation():
    full = phr.parse_event(event(T0 + 0.5, size=812, err="ReadTimeout", gate="timeout"))
    assert (full.t, full.src, full.op, full.app, full.bytes, full.err, full.gate) == (
        T0 + 0.5, "hub", "push", "focus", 812, "ReadTimeout", "timeout")
    bare = phr.parse_event({"t": T0, "src": "aqi", "op": "reboot", "app": None, "bytes": -4})
    assert bare.app is None and bare.bytes is None and bare.status is None
    for raw in [[], {"src": "hub", "op": "push"}, {"t": T0, "op": "push"},
                {"t": T0, "src": "hub"}, {"t": T0, "src": "", "op": "push"}, {"t": "x", "src": "a", "op": "b"}]:
        assert phr.parse_event(raw) is None


def test_rotated_file_is_read_first_and_malformed_lines_are_counted(tmp_path):
    rotated, current = phr.log_paths(tmp_path, phr.HEALTH_LOG_NAME)
    assert rotated.name == "panel_health.1.jsonl"
    write_log(rotated, [probe(T0, 100, 30_000), "not json", '{"t": 1', probe(T0 + 30, 130, 29_000), "[1, 2]", "   "])
    write_log(current, [probe(T0 + 60, 160, 28_000), '{"t": "soon"}', probe(T0 + 90, 190, 27_000)])
    records, files = phr.read_records(tmp_path, phr.HEALTH_LOG_NAME, "health", phr.parse_sample)
    assert [s.block for s in records] == [30_000, 29_000, 28_000, 27_000]
    assert [(f.name, f.lines, f.malformed) for f in files] == [
        ("panel_health.1.jsonl", 5, 3), ("panel_health.jsonl", 3, 1)]
    logs = phr.load_logs(tmp_path)
    assert len(phr.split_boots(logs.samples)) == 1
    assert not logs.events_available and logs.events == ()


def test_reads_what_the_hub_writer_leaves_including_its_rotation(tmp_path):
    assert phr.HEALTH_LOG_NAME == hub_status.HEALTH_LOG_NAME
    log = tmp_path / phr.HEALTH_LOG_NAME
    # 600 B rotates exactly once over six lines of 120..300 B, so none is lost.
    for i in range(6):
        health = DeviceHealth(uptime_seconds=100 + 30 * i, reset_reason="software", free_heap=50_000,
                              min_free_heap=40_000, largest_block=30_000 - i, fps=42,
                              current_app="ocean", matrix_power=True, wifi_rssi=-70 - i,
                              wifi_connects=1 + i // 3, wifi_attempts=0, wifi_state="connected",
                              wifi_last_error="beacon timeout" if i >= 3 else None)
        hub_status.append_health(log, health, T0 + 30 * i, max_bytes=600)
    assert (tmp_path / "panel_health.1.jsonl").exists()
    samples = phr.load_logs(tmp_path).samples
    assert [s.block for s in samples] == [30_000 - i for i in range(6)]
    assert all(s.app == "ocean" and s.power is True for s in samples)
    assert [s.rssi for s in samples] == [-70, -71, -72, -73, -74, -75]
    assert [s.wconn for s in samples] == [1, 1, 1, 2, 2, 2]
    assert [s.werr for s in samples] == [None] * 3 + ["beacon timeout"] * 3
    assert all(s.has_wifi and s.wstate == "connected" and s.watt == 0 for s in samples)
    [boot] = phr.summarize_boots(phr.split_boots(samples))
    assert (boot.rssi_min, boot.rssi_median, boot.rssi_max, boot.wifi_connects_delta) == (
        -75, -72.5, -70, 1)


def test_journal_lines_written_out_of_order_are_sorted_by_start_time(tmp_path):
    write_log(tmp_path / phr.HEALTH_LOG_NAME, [probe(T0, 100, 1)])
    write_log(tmp_path / phr.EVENTS_LOG_NAME, [event(T0 + 5.2), event(T0 + 1.1), "garbage"])
    logs = phr.load_logs(tmp_path)
    assert [e.t for e in logs.events] == [T0 + 1.1, T0 + 5.2]
    assert logs.events_available
    assert [(f.log, f.malformed) for f in logs.files] == [("health", 0), ("events", 1)]


# -- 1. boots ----------------------------------------------------------------

def test_boots_split_where_uptime_goes_back_and_not_on_failed_probes():
    samples = parsed(probe(T0, 100, 30_000), failed(T0 + 30), probe(T0 + 60, 160, 29_000),
                     probe(T0 + 90, 20, 32_000, reason="panic"), failed(T0 + 120), probe(T0 + 150, 80, 31_000))
    boots = phr.split_boots(samples)
    assert [[s.uptime for s in boot] for boot in boots] == [[100, 160], [20, 80]]
    summaries = phr.summarize_boots(boots)
    assert [s.reason for s in summaries] == ["software", "panic"]
    assert [s.inferred for s in summaries] == [False, False]
    assert summaries[1].started == T0 + 70


def test_a_reboot_hidden_in_a_gap_is_inferred_from_the_boot_instant():
    drift = parsed(probe(T0, 1000, 20_000), probe(T0 + 86_400, 1000 + 86_400 + 3, 20_000))
    assert len(phr.split_boots(drift)) == 1
    hidden = parsed(probe(T0, 1000, 20_000), probe(T0 + 30, 1030, 20_000), probe(T0 + 10_000, 5000, 30_000))
    summaries = phr.summarize_boots(phr.split_boots(hidden))
    assert [(s.samples, s.inferred) for s in summaries] == [(2, False), (1, True)]
    assert summaries[1].started == T0 + 5000


def test_boot_summary_reads_the_block_at_each_offset_within_tolerance():
    rows = [(20, 32_756, 42_072), (590, 30_000, 30_000), (1700, 20_000, 18_000),
            (4000, 18_000, 17_000), (23_600, 12_000, 15_000)]
    samples = parsed(*(probe(T0 + up, up, block, min_free=low) for up, block, low in rows))
    [summary] = phr.summarize_boots(phr.split_boots(samples))
    assert summary.block_at == {"+0": 32_756, "+10m": 30_000, "+30m": 20_000,
                                "+1h": None, "+6h": 12_000, "+24h": None}
    assert (summary.min_block, summary.min_min_free, summary.samples) == (12_000, 15_000, 5)
    assert summary.started == T0 and summary.first_uptime == 20
    assert summary.covered_seconds == 23_580
    assert phr.block_near(parsed(probe(T0, 121, 1)), 0) is None
    assert phr.block_near(parsed(probe(T0, 120, 1)), 0) == 1
    assert phr.block_near(parsed(probe(T0, 120, None)), 0) is None


# -- 2. drops ----------------------------------------------------------------

def test_events_on_interval_boundaries_belong_to_the_later_probe():
    samples = parsed(probe(T0, 100, 30_000), probe(T0 + 30, 130, 28_000), probe(T0 + 60, 160, 28_000))
    events = parsed_events(event(T0), event(T0 + 30), event(T0 + 30.1), event(T0 + 60), event(T0 + 60.1))
    intervals = phr.block_intervals(phr.split_boots(samples), events)
    assert [[e.t for e in i.events] for i in intervals] == [[T0 + 30], [T0 + 30.1, T0 + 60]]
    assert phr.attribute_mutations(intervals, events).outside_intervals == 2


def test_no_interval_spans_two_boots_and_probes_without_block_are_stepped_over():
    samples = parsed(probe(T0, 100, 30_000), probe(T0 + 30, 130, None), probe(T0 + 60, 160, 29_000),
                     probe(T0 + 90, 10, 32_000), probe(T0 + 120, 40, 31_000))
    events = parsed_events(event(T0 + 45), event(T0 + 75, op="reboot", app=None))
    intervals = phr.block_intervals(phr.split_boots(samples), events)
    assert [(i.boot, i.before.uptime, i.after.uptime, i.delta) for i in intervals] == [
        (1, 100, 160, -1000), (2, 10, 40, -1000)]
    assert [len(i.events) for i in intervals] == [1, 0]


def test_biggest_drops_rank_by_size_name_the_apps_and_summarise_mutations():
    rows, block, last = [], 40_000, 40
    for i in range(last):
        block -= 100 * (i % 7) if i % 2 else -50
        rows.append(probe(T0 + 30 * i, 100 + 30 * i, block, app="ocean" if i % 3 else "moon"))
    rows.append(probe(T0 + 30 * last, 100 + 30 * last, block - 9_000, app="nowplaying"))
    samples = parsed(*rows)
    events = parsed_events(event(T0 + 30 * last - 1, size=900), event(T0 + 30 * last - 2, size=900),
                           event(T0 + 30 * last, src="nowplaying", app="nowplaying", size=3000))
    intervals = phr.block_intervals(phr.split_boots(samples), events)
    assert sum(1 for i in intervals if i.delta < 0) > phr.TOP_DROPS
    drops = phr.biggest_drops(intervals)
    assert len(drops) == phr.TOP_DROPS
    assert [d.delta for d in drops] == sorted(d.delta for d in drops)
    top = drops[0]
    assert (top.delta, top.block_before - top.block_after, top.app_after) == (-9_000, 9_000, "nowplaying")
    assert top.app_before == "moon" and top.uptime == 100 + 30 * last
    assert [(g.src, g.op, g.app, g.count, g.bytes) for g in top.mutations] == [
        ("hub", "push", "focus", 2, 1800), ("nowplaying", "push", "nowplaying", 1, 3000)]
    assert all(d.mutations == () for d in drops[1:])
    assert phr._mutations_text(top.mutations) == "hub/push/focus×2 1,800 B; nowplaying/push/nowplaying×1 3,000 B"


# -- 3. attribution ----------------------------------------------------------

def attribution_fixture():
    plan = [(1800, 30_000), (1830, 29_000), (1860, 29_000), (1890, 29_000),
            (4000, 29_000), (4030, 27_000), (4060, 27_000), (4090, 26_900)]
    samples = parsed(*(probe(T0 + up, up, block) for up, block in plan))
    events = parsed_events(
        event(T0 + 1000, src="nowplaying", app="nowplaying", size=50),
        event(T0 + 1825, src="nowplaying", app="nowplaying", size=500),
        event(T0 + 4025, src="nowplaying", app="nowplaying", size=500),
        event(T0 + 4020, size=200),
        event(T0 + 4050, size=200), event(T0 + 4055, size=200),
    )
    return samples, events


def test_attribution_measures_against_quiet_intervals_and_splits_the_first_hour():
    samples, events = attribution_fixture()
    intervals = phr.block_intervals(phr.split_boots(samples), events)
    result = phr.attribute_mutations(intervals, events)
    assert result.baseline == phr.DeltaStats(4, -25.0, 3, 0.0, 1, -100.0)
    playing, hub = result.rows
    assert (playing.src, playing.count, playing.bytes) == ("nowplaying", 3, 1050)
    assert playing.stats == phr.DeltaStats(2, -1500.0, 1, -1000.0, 1, -2000.0)
    assert playing.vs_baseline == -1475.0
    assert (hub.src, hub.app, hub.count, hub.bytes) == ("hub", "focus", 3, 600)
    assert hub.stats == phr.DeltaStats(2, -1000.0, 0, None, 2, -1000.0)
    assert hub.vs_baseline == -975.0
    assert result.outside_intervals == 1


def test_attribution_without_any_quiet_interval_has_no_baseline_to_compare():
    samples = parsed(probe(T0, 100, 30_000), probe(T0 + 30, 130, 29_000))
    events = parsed_events(event(T0 + 10))
    result = phr.attribute_mutations(phr.block_intervals(phr.split_boots(samples), events), events)
    assert result.baseline.intervals == 0 and result.baseline.mean_delta is None
    assert result.rows[0].stats.mean_delta == -1000.0 and result.rows[0].vs_baseline is None


# -- 4. fps ------------------------------------------------------------------

def test_fps_by_app_uses_only_probes_that_carry_app_and_splits_dark_panels():
    samples = parsed(
        probe(T0, 1, 1, fps=20),
        probe(T0, 1, 1, fps=30, app="ocean", power=True), probe(T0, 1, 1, fps=34, app="ocean", power=True),
        probe(T0, 1, 1, fps=45, app="ocean", power=True),
        probe(T0, 1, 1, fps=10, app="ocean", power=False), probe(T0, 1, 1, fps=10, app="ocean", power=False),
        probe(T0, 1, 1, fps=42, app="moon", power=None), probe(T0, 1, 1, fps=41, app=None, power=True),
        probe(T0, 1, 1, fps=None, app="moon", power=True), failed(T0),
    )
    report = phr.fps_by_app(samples)
    assert [(r.app, r.power, r.samples) for r in report.rows] == [
        ("ocean", "off/unknown", 2), ("ocean", "on", 3), ("(none)", "on", 1), ("moon", "off/unknown", 1)]
    ocean = report.rows[1]
    assert ocean.mean == pytest.approx(109 / 3) and ocean.min == 30
    assert ocean.below_target_pct == pytest.approx(200 / 3)
    assert report.without_app == 1
    assert (report.overall.samples, report.overall.min) == (8, 10)
    assert phr.fps_by_app([]) == phr.FpsReport((), 0, None)


# -- 5. rates ----------------------------------------------------------------

def test_mutation_rate_per_source_per_hour_with_bytes_errors_and_busiest_hour():
    hour = (T0 // 3600 + 1) * 3600
    events = parsed_events(
        event(hour + 10), event(hour + 20), event(hour + 30), event(hour + 3610),
        event(hour + 3700, src="nowplaying", size=1000, err="ReadTimeout", status=None),
        event(hour + 3800, src="nowplaying", size=1000, status=500),
    )
    rates = phr.mutation_rates(events, end=hour + 7210)
    assert rates.hours == 2.0
    assert rates.rows == (phr.RateRow("hub", 4, 2.0, 400, 200.0, 3, 0),
                          phr.RateRow("nowplaying", 2, 1.0, 2000, 1000.0, 2, 2))
    assert rates.total == phr.RateRow("all", 6, 3.0, 2400, 1200.0, 3, 2)
    assert (rates.busiest_hour_start, rates.busiest_hour_events, rates.busiest_hour_bytes) == (hour, 3, 300)


def test_a_fresh_journal_is_rated_over_at_least_an_hour():
    rates = phr.mutation_rates(parsed_events(event(T0, size=None)), end=T0)
    assert rates.hours == 1.0 and rates.total.per_hour == 1.0 and rates.total.bytes == 0
    assert phr.mutation_rates([], end=T0) is None


# -- 6. gaps -----------------------------------------------------------------

def test_gaps_find_failed_probe_runs_and_silences_with_their_boot():
    samples = parsed(
        failed(T0 - 1000), probe(T0, 100, 1), failed(T0 + 30), failed(T0 + 60, "PanelTransportError"),
        probe(T0 + 90, 190, 1), probe(T0 + 390, 490, 1), probe(T0 + 1000, 30, 1),
        probe(T0 + 1400, 430, 1), failed(T0 + 1430),
    )
    gaps = phr.find_gaps(samples)
    assert [(o.start, o.end, o.seconds, o.failed_probes, o.errors, o.recovered)
            for o in gaps.outages] == [
        (T0 - 1000, T0, 1000, 1, ("ConnectTimeout",), True),
        (T0 + 30, T0 + 90, 60, 2, ("ConnectTimeout", "PanelTransportError"), True),
        (T0 + 1430, T0 + 1430, 0, 1, ("ConnectTimeout",), False),
    ]
    # Old lines: the edges and the reboot question still work; Wi-Fi cannot.
    first, middle, last = gaps.outages
    assert first.before is None and first.after.t == T0 and first.evidence == "unknown"
    assert (middle.before.uptime, middle.after.uptime, middle.rebooted) == (100, 190, False)
    assert middle.evidence == "unknown" and not middle.before.has_wifi
    assert last.after is None and last.before.t == T0 + 1400 and last.evidence == "ongoing"
    assert gaps.silences == (
        phr.Silence(T0 - 1000, T0, 1000, None),
        phr.Silence(T0 + 390, T0 + 1000, 610, True),
        phr.Silence(T0 + 1000, T0 + 1400, 400, False),
    )
    assert phr.find_gaps([]) == phr.Gaps((), ())


# -- 7. wi-fi ----------------------------------------------------------------

def test_wifi_keys_parse_and_junk_in_them_goes_blank():
    full = phr.parse_sample(wprobe(T0, 10, -69, 3, werr="beacon timeout"))
    assert (full.rssi, full.wconn, full.watt, full.wstate, full.werr, full.has_wifi) == (
        -69, 3, 0, "connected", "beacon timeout", True)
    reported_nothing = phr.parse_sample(probe(T0, 10, 2, rssi=None, wconn=None, watt=None, wstate=None))
    assert reported_nothing.has_wifi and reported_nothing.rssi is None and reported_nothing.werr is None
    old = phr.parse_sample(probe(T0, 10, 2, app="ocean", power=True))
    assert not old.has_wifi and (old.rssi, old.wconn, old.wstate) == (None, None, None)
    for rssi in [True, "-69", -69.5, 0, 5, -121, float("nan"), [], {}]:
        assert phr.parse_sample(probe(T0, 10, 2, rssi=rssi)).rssi is None, rssi
    assert phr.parse_sample(probe(T0, 10, 2, rssi=-120)).rssi == -120
    for count in [-1, True, 1.0, "2", None]:
        junk = phr.parse_sample(probe(T0, 10, 2, wconn=count, watt=count))
        assert (junk.wconn, junk.watt) == (None, None), count
    junk = phr.parse_sample(probe(T0, 10, 2, wstate=7, werr="e" * 200 + "\n"))
    assert junk.wstate is None and junk.werr == "e" * phr.WIFI_ERROR_CHARS
    assert phr.parse_sample(probe(T0, 10, 2, wstate="", werr="a\nb\x1b")).werr == "ab"
    assert phr.parse_sample(probe(T0, 10, 2, wstate="x" * 40)).wstate == "x" * phr.WIFI_STATE_CHARS
    # Junk in Wi-Fi never costs the line its heap evidence.
    assert phr.parse_sample(probe(T0, 10, 1234, rssi="weak", wconn=-3)).block == 1234


def test_each_boot_reports_rssi_spread_and_how_far_connects_rose():
    samples = parsed(wprobe(T0, 100, -70, 1), wprobe(T0 + 30, 130, -66, 1),
                     wprobe(T0 + 60, 160, -75, 2), wprobe(T0 + 90, 190, -72, 2),
                     probe(T0 + 120, 10, 30_000), probe(T0 + 150, 40, 30_000))
    wifi_boot, old_boot = phr.summarize_boots(phr.split_boots(samples))
    assert (wifi_boot.rssi_samples, wifi_boot.rssi_min, wifi_boot.rssi_median, wifi_boot.rssi_max) == (
        4, -75, -71.0, -66)
    assert (wifi_boot.wifi_connects_first, wifi_boot.wifi_connects_last,
            wifi_boot.wifi_connects_delta) == (1, 2, 1)
    assert (old_boot.rssi_samples, old_boot.rssi_min, old_boot.rssi_median, old_boot.rssi_max,
            old_boot.wifi_connects_first, old_boot.wifi_connects_delta) == (0, None, None, None, None, None)
    assert phr.connects_rise([1, 2, 1, 3]) == 3 and phr.connects_rise([4]) == 0
    assert phr.connects_rise([]) is None


def test_outage_edges_tell_a_wifi_drop_from_a_stall_and_a_crash():
    a, b, c, d = phr.find_gaps(parsed(*wifi_timeline())).outages
    assert (a.evidence, a.rebooted, a.connects_delta, a.failed_probes) == ("wifi_reconnect", False, 1, 2)
    assert (a.before.t, a.before.rssi, a.before.state, a.before.connects) == (T0 + 600, -78, "connected", 1)
    assert (a.after.t, a.after.rssi, a.after.connects, a.after.last_error) == (
        T0 + 760, -70, 2, "beacon timeout")
    assert (a.rssi_10m_mean, a.rssi_10m_probes) == (-76.0, 2)  # T0 is more than 10 min before
    assert (b.evidence, b.rebooted, b.connects_delta) == ("no_reconnect", False, 0)
    assert (b.rssi_10m_mean, b.rssi_10m_probes) == (-74.0, 3)
    assert (c.evidence, c.rebooted, c.connects_delta, c.after.reason) == ("rebooted", True, None, "panic")
    assert (c.rssi_10m_mean, c.rssi_10m_probes) == (-72.0, 4)
    assert (d.evidence, d.recovered, d.after, d.before.t) == ("ongoing", False, None, T0 + 880)
    assert (d.rssi_10m_mean, d.rssi_10m_probes) == (-69.75, 4)


def test_outage_evidence_stays_unknown_without_counters_on_both_sides():
    mixed = parsed(failed(T0 - 30), probe(T0, 100, 1), failed(T0 + 30), wprobe(T0 + 60, 160, -70, 1),
                   failed(T0 + 90), wprobe(T0 + 120, 220, -70, None),
                   wprobe(T0 + 150, 250, -70, 3), failed(T0 + 180), wprobe(T0 + 210, 310, -71, 2))
    start, old_to_new, lost_counter, backwards = phr.find_gaps(mixed).outages
    assert start.before is None and (start.evidence, start.rebooted) == ("unknown", None)
    assert (old_to_new.evidence, old_to_new.rebooted, old_to_new.connects_delta) == ("unknown", False, None)
    assert not old_to_new.before.has_wifi and old_to_new.after.has_wifi
    assert old_to_new.rssi_10m_probes == 0 and old_to_new.rssi_10m_mean is None
    assert (lost_counter.evidence, lost_counter.connects_delta) == ("unknown", None)
    assert (backwards.evidence, backwards.connects_delta) == ("unknown", -1)
    assert phr._outage_wifi_lines(start) == []  # neither side has Wi-Fi: no line at all
    assert phr._outage_wifi_lines(old_to_new) == [
        "    before: no Wi-Fi fields (no RSSI in the 10 min before) | "
        "after -70 dBm connected connects 1 => too little Wi-Fi data to tell"]
    log_starts_down = phr.find_gaps(parsed(failed(T0), wprobe(T0 + 30, 100, -70, 1))).outages[0]
    assert phr._outage_wifi_lines(log_starts_down) == [
        "    before: none (no RSSI in the 10 min before) | "
        "after -70 dBm connected connects 1 => too little Wi-Fi data to tell"]
    # The PC slept before the run: the probe before is old, the line says how
    # old, and the reconnect is not pinned on this run.
    [after_sleep] = phr.find_gaps(parsed(wprobe(T0, 100, -70, 1), failed(T0 + 3600),
                                         wprobe(T0 + 3630, 3730, -71, 2))).outages
    assert (after_sleep.evidence, after_sleep.connects_delta) == ("stale_before", 1)
    assert phr._outage_wifi_lines(after_sleep) == [
        "    before (1h00m earlier) -70 dBm connected connects 1 (no RSSI in the 10 min before) | "
        "after -71 dBm connected connects 2 => connects +1 at some point since the probe before, "
        "not necessarily in this run"]


def test_a_probe_before_from_ahead_of_a_silence_cannot_place_a_reboot_or_a_reconnect():
    # The 10-09 08:57 shape: the last probe before the night, the first probe
    # after waking fails (this PC's network coming up), the next one answers.
    night = 35_584
    rows = [wprobe(T0, 1000, -70, 1), failed(T0 + night),
            wprobe(T0 + night + 36, 1000 + night + 36, -69, 2),       # same boot, connects +1
            failed(T0 + 2 * night), wprobe(T0 + 2 * night + 36, 40, -69, 1, reason="panic"),
            failed(T0 + 3 * night), wprobe(T0 + 3 * night + 36, 40 + night + 36, -69, 1),
            wprobe(T0 + 3 * night + 66, 40 + night + 66, -69, 1), failed(T0 + 3 * night + 96),
            wprobe(T0 + 3 * night + 126, 40 + night + 126, -69, 2)]  # a fresh edge: counted
    samples = parsed(*rows)
    reconnect, reboot, steady, fresh = phr.find_gaps(samples).outages
    assert (reconnect.evidence, reconnect.rebooted, reconnect.connects_delta) == ("stale_before", False, 1)
    assert (reboot.evidence, reboot.rebooted) == ("stale_before", True)
    assert (steady.evidence, steady.connects_delta) == ("no_reconnect", 0)  # holds for the whole span
    assert fresh.evidence == "wifi_reconnect"
    wifi = phr.wifi_summary(samples, phr.find_gaps(samples).outages)
    assert wifi.evidence == {"no_reconnect": 1, "stale_before": 2, "wifi_reconnect": 1}
    report = phr.build_report(phr.Logs(tuple(samples), (), (), False), data_dir=Path("."),
                              generated_at=NOW)
    gaps_text = "\n".join(phr._gaps_text(report))
    assert (f"1 failed probe (ConnectTimeout), recovered, panel rebooted at some point since "
            f"{phr.fmt_local(T0 + night + 36)} (panic)") in gaps_text
    assert not any("rebooted inside" in line for line in gaps_text.splitlines()
                   if line.startswith("  unreachable"))
    assert ("evidence across runs: same boot, no reconnect 1, probe before too old to place it 2, "
            "Wi-Fi reconnect 1") in "\n".join(phr._wifi_text(report))


def since_window_dir(tmp_path):
    """40 probes of the new writer, then 200 failures (the panel still down at the end)."""
    rows = [wprobe(T0 + 30 * i, 600 + 30 * i, -70, 1) for i in range(40)]
    rows += [failed(T0 + 1200 + 30 * i) for i in range(200)]
    write_log(tmp_path / phr.HEALTH_LOG_NAME, rows)
    return tmp_path


def test_a_window_inside_an_outage_keeps_its_real_start_and_probe_before(tmp_path):
    data = since_window_dir(tmp_path)
    end = T0 + 1200 + 30 * 199
    clock = lambda: end + 60  # noqa: E731
    _, out = run(["--data-dir", str(data), "--since-hours", "1"], clock=clock)
    gaps = out[out.index("6. Gaps"):out.index("7. Wi-Fi")]
    assert (f"unreachable {phr.fmt_local(T0 + 1200)} for {phr.fmt_duration(end - (T0 + 1200))}: "
            "200 failed probes (ConnectTimeout), still failing at end of log") in gaps
    assert "    before -70 dBm connected connects 1 (10-min mean -70.0, 20 probes) | after: none yet" in gaps
    assert "older Hub writer" not in out
    assert '4. FPS by app (probes that carry "app"; target 40)\n  no reachable probe in the window' in out
    wifi = out[out.index("7. Wi-Fi"):]
    assert "  no reachable probe in the window, so no Wi-Fi readings" in wifi
    doc = json.loads(run(["--data-dir", str(data), "--json", "--since-hours", "1"], clock=clock)[1])
    [outage] = doc["gaps"]["outages"]
    assert outage["start"] == T0 + 1200 and outage["before"]["t"] == T0 + 30 * 39
    assert doc["samples"] == doc["unreachable"] == 119  # the window itself: only its failures


def test_runs_and_silences_before_the_window_stay_out_of_it(tmp_path):
    write_log(tmp_path / phr.HEALTH_LOG_NAME, [
        probe(NOW - 5 * 3600, 100, 30_000), failed(NOW - 5 * 3600 + 30),
        probe(NOW - 5 * 3600 + 60, 160, 30_000), probe(NOW - 3 * 3600, 7300, 30_000),
        probe(NOW - 600, 17_500, 30_000), failed(NOW - 570), probe(NOW - 540, 17_560, 30_000)])
    doc = json.loads(run(["--data-dir", str(tmp_path), "--json", "--since-hours", "1"])[1])
    assert [o["start"] for o in doc["gaps"]["outages"]] == [NOW - 570]
    # The silence from NOW - 3 h to NOW - 600 s reaches into the window, so it shows.
    assert [(s["start"], s["end"]) for s in doc["gaps"]["silences"]] == [(NOW - 3 * 3600, NOW - 600)]


def test_runs_with_no_reachable_probe_anywhere_say_so_not_older_writer(tmp_path):
    write_log(tmp_path / phr.HEALTH_LOG_NAME, [failed(T0), failed(T0 + 30)])
    _, out = run(["--data-dir", str(tmp_path)])
    assert ("Wi-Fi before/after: none of these runs has Wi-Fi fields in the probes either side "
            "(no reachable probe on either side in the log)") in out
    assert "  no reachable probe in the log, so no Wi-Fi readings" in out
    write_log(tmp_path / phr.HEALTH_LOG_NAME, [failed(T0), probe(T0 + 30, 10, 1), failed(T0 + 60)])
    _, out = run(["--data-dir", str(tmp_path)])
    assert "(lines from the older Hub writer)" in out


def test_rssi_bands_are_inclusive_whole_dbm_and_cover_every_reading():
    assert [label for label, _, _ in phr.RSSI_BUCKETS] == [
        ">= -60", "-61..-67", "-68..-72", "-73..-78", "<= -79"]
    edges = {-1: 0, -60: 0, -61: 1, -67: 1, -68: 2, -72: 2, -73: 3, -78: 3, -79: 4, -120: 4}
    assert {value: phr.rssi_bucket(value) for value in edges} == edges
    assert all(0 <= phr.rssi_bucket(v) < len(phr.RSSI_BUCKETS) for v in range(phr.RSSI_MIN, 0))
    # A 10-minute mean is rounded to the nearest dBm first.
    assert [phr.rssi_bucket(v) for v in (-60.4, -60.6, -67.5, -67.6, -78.4, -78.5)] == [0, 1, 1, 2, 3, 3]


def test_wifi_summary_sets_runs_against_the_ten_minutes_before_them():
    samples = parsed(*wifi_timeline())
    wifi = phr.wifi_summary(samples, phr.find_gaps(samples).outages)
    assert (wifi.reachable, wifi.probes_with_wifi, wifi.probes_with_rssi) == (6, 6, 6)
    assert wifi.rssi_mean == pytest.approx(-413 / 6) and wifi.rssi_median == -68.0
    assert [(b.label, b.probes, b.runs) for b in wifi.buckets] == [
        (">= -60", 1, 0), ("-61..-67", 2, 0), ("-68..-72", 1, 2), ("-73..-78", 2, 2), ("<= -79", 0, 0)]
    assert [b.runs_per_1k_probes for b in wifi.buckets] == [0.0, 0.0, 2000.0, 1000.0, None]
    assert wifi.buckets[1].share_pct == pytest.approx(100 / 3)
    assert (wifi.runs, wifi.runs_with_rssi, wifi.runs_below_median) == (4, 4, 4)
    assert wifi.runs_rssi_mean == pytest.approx((-76 - 74 - 72 - 69.75) / 4)
    assert wifi.evidence == {"no_reconnect": 1, "ongoing": 1, "rebooted": 1, "wifi_reconnect": 1}


def test_wifi_summary_of_old_lines_is_empty_not_an_error():
    samples = parsed(probe(T0, 100, 1), failed(T0 + 30), probe(T0 + 60, 160, 1))
    wifi = phr.wifi_summary(samples, phr.find_gaps(samples).outages)
    assert (wifi.probes_with_wifi, wifi.probes_with_rssi, wifi.rssi_mean, wifi.rssi_median) == (0, 0, None, None)
    assert all(b.probes == 0 and b.runs == 0 and b.runs_per_1k_probes is None and b.share_pct == 0.0
               for b in wifi.buckets)
    assert (wifi.runs, wifi.runs_with_rssi, wifi.runs_rssi_mean) == (1, 0, None)
    assert wifi.evidence == {"unknown": 1}
    empty = phr.wifi_summary([], ())
    assert (empty.reachable, empty.runs, empty.evidence) == (0, 0, {})


def test_text_report_puts_wifi_either_side_of_each_run(tmp_path):
    write_log(tmp_path / phr.HEALTH_LOG_NAME, wifi_timeline())
    code, out = run(["--data-dir", str(tmp_path)])
    assert code == 0
    at = phr.fmt_local
    gaps = out[out.index("6. Gaps"):out.index("7. Wi-Fi")].splitlines()
    assert gaps[1:8] == [
        f"  unreachable {at(T0 + 700)} for 1m: 2 failed probes (ConnectTimeout), recovered, same boot",
        "    before -78 dBm connected connects 1 (10-min mean -76.0, 2 probes) | "
        "after -70 dBm connected connects 2 => Wi-Fi drop: same boot, connects +1",
        # The panel's error shows once, where it first appears, not again after the stall.
        '    panel\'s last Wi-Fi error after it: "beacon timeout"',
        f"  unreachable {at(T0 + 790)} for 30s: 1 failed probe (ConnectTimeout), recovered, same boot",
        "    before -70 dBm connected connects 2 (10-min mean -74.0, 3 probes) | "
        "after -66 dBm connected connects 2 => same boot, no reconnect counted",
        f"  unreachable {at(T0 + 850)} for 30s: 1 failed probe (ConnectTimeout), "
        "recovered, panel rebooted inside (panic)",
        "    before -66 dBm connected connects 2 (10-min mean -72.0, 4 probes) | "
        "after -65 dBm connected connects 1 => rebooted: its counters restarted",
    ]
    assert gaps[8:10] == [
        f"  unreachable {at(T0 + 910)} for 0s: 1 failed probe (ConnectTimeout), still failing at end of log",
        "    before -65 dBm connected connects 1 (10-min mean -69.8, 4 probes) | after: none yet",
    ]
    assert "Wi-Fi before/after" not in out
    wifi = out[out.index("7. Wi-Fi"):]
    assert "6 of 6 reachable probes carry RSSI; mean -68.8 dBm, median -68 dBm" in wifi
    assert "rise > 0: the panel reconnected to Wi-Fi without rebooting" in wifi
    band = next(line for line in wifi.splitlines() if line.strip().startswith("-73..-78"))
    assert band.split() == ["-73..-78", "2", "33%", "2", "1000.0"]
    assert ("4 of 4 unreachable runs have RSSI in the 10m before: mean -72.9 dBm vs -68.8 over all "
            "probes (-4.1 dB), 4 below the median (-68 dBm)") in wifi
    assert ("evidence across runs: same boot, no reconnect 1, still down 1, rebooted 1, "
            "Wi-Fi reconnect 1") in wifi
    boot_rows = [line.split() for line in wifi.splitlines() if re.match(r"\s+[12]\s+\d\d-\d\d ", line)]
    assert [(row[0], row[3], row[4], row[5], row[6], row[7], row[8]) for row in boot_rows] == [
        ("1", "5", "-78", "-70", "-60", "1->2", "+1"), ("2", "1", "-65", "-65", "-65", "1->1", "+0")]


def test_old_lines_only_report_says_where_wifi_will_come_from(tmp_path):
    code, out = run(["--data-dir", str(two_boot_dir(tmp_path))])
    assert code == 0
    assert "recovered, panel rebooted inside (software)" in out
    assert ("Wi-Fi before/after: none of these runs has Wi-Fi fields in the probes either side "
            "(lines from the older Hub writer)") in out
    assert "    before " not in out and "    after " not in out
    assert out.rstrip().endswith("the Hub logs rssi/wconn/watt/wstate from its next restart)")
    doc = json.loads(run(["--data-dir", str(tmp_path), "--json"])[1])
    assert doc["wifi"]["probes_with_wifi"] == 0 and doc["wifi"]["runs"] == 1
    assert [b["probes"] for b in doc["wifi"]["buckets"]] == [0] * 5
    [outage] = doc["gaps"]["outages"]
    assert (outage["evidence"], outage["rebooted"], outage["before"]["has_wifi"]) == ("rebooted", True, False)
    assert all(b["rssi_samples"] == 0 and b["rssi_median"] is None for b in doc["boots"])


def test_json_carries_the_wifi_sections(tmp_path):
    write_log(tmp_path / phr.HEALTH_LOG_NAME, wifi_timeline())
    doc = json.loads(run(["--data-dir", str(tmp_path), "--json"])[1])
    assert [b["label"] for b in doc["wifi"]["buckets"]] == [b[0] for b in phr.RSSI_BUCKETS]
    assert doc["wifi"]["evidence"]["wifi_reconnect"] == 1
    first = doc["gaps"]["outages"][0]
    assert (first["before"]["rssi"], first["after"]["connects"], first["connects_delta"]) == (-78, 2, 1)
    assert (first["rssi_10m_mean"], first["rssi_10m_probes"], first["evidence"]) == (-76.0, 2, "wifi_reconnect")
    assert doc["gaps"]["outages"][-1]["after"] is None
    assert [(b["rssi_median"], b["wifi_connects_delta"]) for b in doc["boots"]] == [(-70.0, 1), (-65.0, 0)]


# -- report and cli ----------------------------------------------------------

def two_boot_dir(tmp_path, with_events=True):
    rotated, current = phr.log_paths(tmp_path, phr.HEALTH_LOG_NAME)
    old = [probe(T0 + 30 * i, 600 + 30 * i, 30_000 - 512 * (i // 4)) for i in range(40)]
    write_log(rotated, old + ["{broken"])
    boot = T0 + 30 * 40 + 300
    new = [probe(boot + 25 + 30 * i, 25 + 30 * i, 32_756 - 1024 * (i // 10), app="ocean", power=True)
           for i in range(30)]
    write_log(current, [failed(T0 + 30 * 40 + 60)] + new)
    if with_events:
        write_log(tmp_path / phr.EVENTS_LOG_NAME,
                  [event(T0 + 30 * 4 - 3, src="pcstats", app="pcstats", size=1500),
                   event(boot + 25 + 30 * 10 - 1, src="nowplaying", app="nowplaying", size=700),
                   event(boot + 1, src="hub", op="reboot", app=None, size=None)])
    return tmp_path


def test_json_output_is_one_document_that_round_trips(tmp_path):
    data = two_boot_dir(tmp_path)
    code, out = run(["--data-dir", str(data), "--json"])
    assert code == 0
    doc = json.loads(out)
    expected = phr.to_json(phr.build_report(phr.load_logs(data), data_dir=data, generated_at=NOW))
    assert doc == json.loads(json.dumps(expected))
    assert json.loads(json.dumps(doc)) == doc
    assert [b["block_at"]["+0"] for b in doc["boots"]] == [None, 32_756]
    assert doc["boots"][0]["block_at"]["+10m"] == 30_000
    assert doc["events_available"] is True and doc["events"] == 3
    assert doc["drops"][0]["delta"] == -1024 and doc["drops"][0]["mutations"][0]["src"] == "nowplaying"
    assert doc["attribution"]["outside_intervals"] == 1
    assert {f["name"]: f["malformed"] for f in doc["files"]} == {
        "panel_health.1.jsonl": 1, "panel_health.jsonl": 0, "panel_events.jsonl": 0}


def test_text_report_says_plainly_when_there_is_no_mutation_journal(tmp_path):
    code, out = run(["--data-dir", str(two_boot_dir(tmp_path, with_events=False))])
    assert code == 0
    assert "NO MUTATION JOURNAL: panel_events.jsonl not found" in out
    assert out.count("skipped: no mutation journal") == 2
    assert "mutations in (prev, this]" not in out
    for title in ["1. Boots", "2. Biggest block drops", "3. Mutation attribution",
                  "4. FPS by app", "5. Mutation rate", "6. Gaps and outages"]:
        assert title in out
    assert "32,756" in out and "ocean" in out
    doc = json.loads(run(["--data-dir", str(tmp_path), "--json"])[1])
    assert doc["attribution"] is None and doc["rates"] is None and doc["events_available"] is False


def test_text_report_with_a_journal_attributes_drops(tmp_path):
    _, out = run(["--data-dir", str(two_boot_dir(tmp_path))])
    assert "correlation, not proof" in out and "baseline:" in out
    assert "nowplaying/push/nowplaying×1 700 B" in out
    assert "hub/reboot/-" in out
    assert "1 mutations fell outside any probe interval" in out


def test_empty_inputs_report_nothing_without_failing(tmp_path):
    code, out = run(["--data-dir", str(tmp_path / "missing")])
    assert code == 1
    assert "no panel_health.jsonl" in out and "no reachable probes" in out
    (tmp_path / phr.HEALTH_LOG_NAME).write_text("", encoding="utf-8")
    (tmp_path / phr.EVENTS_LOG_NAME).write_text("\n", encoding="utf-8")
    code, out = run(["--data-dir", str(tmp_path)])
    assert code == 0 and out.count("no mutations in window") == 2
    doc = json.loads(run(["--data-dir", str(tmp_path), "--json"])[1])
    assert doc["boots"] == [] and doc["drops"] == [] and doc["first_t"] is None
    assert doc["attribution"]["rows"] == [] and doc["attribution"]["baseline"]["mean_delta"] is None


def test_since_hours_keeps_only_recent_records(tmp_path):
    write_log(tmp_path / phr.HEALTH_LOG_NAME, [
        probe(NOW - 10 * 3600, 100, 30_000), probe(NOW - 3 * 3600, 25_300, 29_000),
        probe(NOW - 5400, 30_700, 20_000), probe(NOW - 5370, 30_730, 19_000)])
    write_log(tmp_path / phr.EVENTS_LOG_NAME, [event(NOW - 5 * 3600), event(NOW - 5380)])
    doc = json.loads(run(["--data-dir", str(tmp_path), "--json", "--since-hours", "2"])[1])
    assert doc["since"] == NOW - 7200
    assert doc["samples"] == 2 and doc["events"] == 1
    assert doc["drops"][0]["mutations"][0]["count"] == 1
    assert json.loads(run(["--data-dir", str(tmp_path), "--json"])[1])["samples"] == 4
    for bad in ["0", "-1", "nan"]:
        with pytest.raises(SystemExit):
            phr.parse_args(["--since-hours", bad])


def test_three_days_of_busy_data_fit_on_about_two_screens(tmp_path):
    apps = ["ocean", "moon", "nowplaying", "nightkeeper", "weatherbrief", "aqibrief", "pcstats"]
    sources = ["hub", "nowplaying", "pcstats", "weather", "aqi", "meridian"]
    rows, events, t = [], [], T0
    for boot in range(3):
        for i in range(2880):
            wifi = {"rssi": -55 - i % 30, "wconn": 1 + i // 700, "watt": 0, "wstate": "connected"}
            if i >= 2000:
                wifi["werr"] = "e" * phr.WIFI_ERROR_CHARS
            rows.append(probe(t, 20 + 30 * i, 32_756 - 37 * i % 20_000, app=apps[i % 7],
                              power=i % 5 != 0, fps=24 + i % 20, **wifi))
            if i % 3 == 0:
                for lag in range(2 + (i % 9 == 0)):
                    events.append(event(t - 4.5 - 5 * lag, src=sources[(i + lag) % 6],
                                        app=apps[(i // 3 + lag) % 7], size=600 + i % 900))
            t += 30
        rows.append(failed(t))
        t += 600
    write_log(tmp_path / phr.HEALTH_LOG_NAME, rows)
    write_log(tmp_path / phr.EVENTS_LOG_NAME, events)
    code, out = run(["--data-dir", str(tmp_path)], clock=lambda: t)
    lines = out.splitlines()
    assert code == 0 and len(lines) <= 110
    assert max(len(line) for line in lines) <= 160


def test_the_report_writes_nothing(tmp_path):
    data = two_boot_dir(tmp_path)

    def snapshot():
        return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in data.iterdir()}
    before = snapshot()
    run(["--data-dir", str(data)])
    run(["--data-dir", str(data), "--json", "--since-hours", "1"])
    assert snapshot() == before


def test_default_directory_is_the_awtrix_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "AWTRIX").mkdir()
    write_log(tmp_path / "AWTRIX" / phr.HEALTH_LOG_NAME, [probe(T0, 20, 32_756)])
    code, out = run(["--json"])
    assert code == 0 and json.loads(out)["data_dir"] == str(tmp_path / "AWTRIX")


def test_number_formats_match_the_docs():
    assert phr.fmt_int(32_756) == "32,756" and phr.fmt_int(None) == "—"
    assert phr.fmt_delta(-1536) == "-1,536" and phr.fmt_delta(12.4) == "+12"
    assert [phr.fmt_duration(s) for s in (0, 59, 600, 3 * 3600 + 300, 2 * 86_400 + 5 * 3600)] == [
        "0s", "59s", "10m", "3h05m", "2d05h"]


def test_streams_that_cannot_carry_the_marks_get_ascii():
    text = "— hub/push/focus×2"
    assert phr.for_stream(text, io.StringIO()) == text
    assert phr.for_stream(text, io.TextIOWrapper(io.BytesIO(), encoding="cp932")) == "- hub/push/focus×2"
    assert phr.for_stream(text, io.TextIOWrapper(io.BytesIO(), encoding="ascii")) == "- hub/push/focusx2"

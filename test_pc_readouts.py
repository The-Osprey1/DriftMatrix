"""Offline rendering and telemetry-failure checks; no device requests."""
import subprocess
import json
from unittest.mock import patch

import pytest
import pc_readouts as app


@pytest.mark.parametrize("metric", ["CPU", "GPU"])
@pytest.mark.parametrize("value", [0, 1, 26, 49.9, 50, 75, 90, 99.9, 100, None, float("nan"), float("inf"), -1, 101])
def test_all_states_fit_and_keep_black_background(metric, value):
    frame = app.readout_frame(app.Reading(metric, value, 100, "test"), 100)
    assert len(frame) == 8 and all(len(row) == 32 for row in frame)
    assert all(pixel == app.BLACK for y in (0, 6) for pixel in frame[y])


def test_full_scale_remains_100_percent_and_rail_is_full():
    reading = app.Reading("CPU", 100, 100, "test")
    frame = app.readout_frame(reading, 100)
    assert all(pixel == app.load_color(100) for pixel in frame[7])
    # The explicit % glyph occupies the rightmost three columns.
    assert [tuple(int(pixel != app.BLACK) for pixel in row[29:32]) for row in frame[1:6]] == [tuple(map(int, row)) for row in app.FONT["%"]]


@pytest.mark.parametrize("stamp", [0, 101, float("nan")])
def test_stale_future_and_invalid_timestamps_are_unavailable(stamp):
    reading = app.Reading("GPU", 0, stamp, "test")
    assert app.display_value(reading, 100) is None
    assert all(pixel == app.TRACK for pixel in app.readout_frame(reading, 100)[7])


def test_gpu_selection_and_unsupported_values():
    output = "0, GPU one, 26\n1, GPU two, N/A\n"
    assert app.parse_gpu_csv(output, 0, 100).value == 26
    assert app.parse_gpu_csv(output, 1, 100).value is None
    assert app.parse_gpu_csv(output, 2, 100).value is None


def test_gpu_timeout_is_unavailable_not_zero():
    with patch.object(app.shutil, "which", return_value="nvidia-smi"):
        with patch.object(app.subprocess, "run", side_effect=subprocess.TimeoutExpired("nvidia-smi", 5)):
            reading = app.sample_gpu()
    assert reading.value is None and reading.detail == "TimeoutExpired"


def test_gpu_missing_is_unavailable():
    with patch.object(app.shutil, "which", return_value=None):
        assert app.sample_gpu().value is None


def test_zero_has_no_filled_bar_and_load_boundaries():
    frame = app.readout_frame(app.Reading("CPU", 0, 100, "test"), 100)
    assert frame[7] == [app.TRACK] * 32
    assert len({app.load_color(v) for v in (0, 50, 75, 90)}) == 4


@pytest.mark.parametrize("raw, displayed", [(49.5, 50), (74.5, 75), (89.5, 90)])
def test_rounded_digits_color_and_rail_agree(raw, displayed):
    frame = app.readout_frame(app.Reading("CPU", raw, 100, "test"), 100)
    assert frame == app.readout_frame(app.Reading("CPU", displayed, 100, "test"), 100)


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), -1, 101])
def test_invalid_cpu_snapshot_exports_json_null(tmp_path, raw):
    with patch.object(app.psutil, "cpu_percent", return_value=raw):
        with patch.object(app, "sample_gpu", return_value=app.Reading("GPU", None, 100, "test")):
            app.export(tmp_path, live=True)
    data = json.loads((tmp_path / "snapshot.json").read_text())
    assert data["readings"][0]["value"] is None


def test_cpu_sampling_failure_exports_unavailable(tmp_path):
    with patch.object(app.psutil, "cpu_percent", side_effect=OSError("unavailable")):
        with patch.object(app, "sample_gpu", return_value=app.Reading("GPU", None, 100, "test")):
            app.export(tmp_path, live=True)
    data = json.loads((tmp_path / "snapshot.json").read_text())
    assert data["readings"][0]["value"] is None
    assert data["readings"][0]["detail"] == "OSError"

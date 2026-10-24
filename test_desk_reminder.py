"""Reminder timing, payload and lifecycle checks; never contact a real panel."""
import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import desk_reminder as reminder
import utility_hub
import visual_system as visual


def test_schedule_survives_reload_and_expires_after_sleep(tmp_path):
    path = tmp_path / "reminder.json"
    saved = reminder.schedule(path, "tea", 5, now=1000)
    assert reminder.read_reminder(path) == saved
    assert saved.phase(999) == "clock_changed"
    assert saved.phase(1299) == "countdown"
    assert saved.phase(1300) == "due"
    assert saved.phase(1899) == "due"
    assert saved.phase(1900) == "expired"


@pytest.mark.parametrize("label,minutes", [("LONG LABEL", 1), ("☕", 1), ("", 1), ("TEA", 0), ("TEA", True), ("TEA", 1441), ("TEA", 1.5)])
def test_invalid_schedule_preserves_existing_file(tmp_path, label, minutes):
    path = tmp_path / "reminder.json"
    reminder.schedule(path, "TEA", 5, now=1000)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        reminder.schedule(path, label, minutes, now=1000)
    assert path.read_bytes() == original


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"schema_version":true}', b"x" * 4097, b"\xff"])
def test_corrupt_state_is_reported_without_rewriting_it(tmp_path, body):
    path = tmp_path / "reminder.json"
    path.write_bytes(body)
    with pytest.raises(ValueError):
        reminder.read_reminder(path)
    assert path.read_bytes() == body


def test_lifecycle_refresh_due_activation_cancel_and_replacement(tmp_path):
    path = tmp_path / "reminder.json"
    now = [1000.0]
    panel = Mock()
    panel.activate_app.return_value = SimpleNamespace(sent=False, reason="quiet-hours")
    controller = reminder.ReminderController(panel, path=path, clock=lambda: now[0])
    assert controller.tick() == "absent"
    panel.push_app.assert_not_called()
    reminder.schedule(path, "TEA", 1, now=now[0])
    assert controller.tick() == "countdown"
    controller.tick()
    assert panel.push_app.call_count == 1
    panel.activate_app.assert_not_called()
    now[0] = 1060
    assert controller.tick() == "due"
    assert panel.activate_app.call_count == 1
    now[0] += 5
    controller.tick()
    assert panel.activate_app.call_count == 1  # Suppressed activation is not retried.
    reminder.schedule(path, "BREAK", 5, now=now[0])
    assert controller.tick() == "countdown"
    path.unlink()
    assert controller.tick() == "absent"
    panel.delete_pushed_app.assert_called_once_with(reminder.APP_NAME)


def test_expiry_removes_page_and_failure_can_retry(tmp_path):
    path = tmp_path / "reminder.json"
    now = [1000]
    panel = Mock()
    controller = reminder.ReminderController(panel, path=path, clock=lambda: now[0], monotonic=lambda: now[0])
    reminder.schedule(path, "TEA", 1, now=1000)
    panel.push_app.side_effect = [RuntimeError("offline"), None]
    assert controller.tick() == "panel_unavailable"
    assert not controller.present
    assert controller.tick() == "retry_wait"
    assert panel.push_app.call_count == 1
    now[0] += 5
    controller.tick()
    assert controller.present
    now[0] = 1660
    assert controller.tick() == "expired"
    panel.delete_pushed_app.assert_called_once()


def test_corrupt_active_state_clears_old_page(tmp_path):
    path = tmp_path / "reminder.json"
    panel = Mock()
    reminder.schedule(path, "TEA", 1, now=1000)
    controller = reminder.ReminderController(panel, path=path, clock=lambda: 1000)
    controller.tick()
    path.write_text("broken")
    assert controller.tick() == "absent"
    panel.delete_pushed_app.assert_called_once()


@pytest.mark.parametrize("minutes", [1, 99, 100, 1440])
def test_exact_bitmap_and_label_fit_budget_and_keep_gutter_black(tmp_path, minutes):
    saved = reminder.schedule(tmp_path / "reminder.json", "WATER", minutes, now=1000)
    for now in (1000, 1010, saved.due_at - .1, saved.due_at):
        payload = reminder.build_payload(saved, now)
        assert len(json.dumps(payload).encode()) < 2048
        assert "icon" not in payload
        assert payload["lifetimeExpiry"] == "remove"
        bitmap = base64.b64decode(payload["draw"][0][5])
        assert len(bitmap) == 32 * 8 * 3
        for y in range(8):
            assert bitmap[(y * 32 + 8) * 3:(y * 32 + 9) * 3] == b"\0\0\0"
    frame = reminder.render(saved, saved.due_at - .1)
    assert visual.read_meter(frame) [1] == visual.ICE  # Positive time never has an empty meter.
    assert visual.meter_fill(frame) > 0
    due = reminder.render(saved, saved.due_at)
    assert frame != due
    # Main icon and letters survive brightness-1 channel threshold.
    # (Row 7's plain track line is decoration and may go dark.)
    assert all(max(pixel) >= 174 for row in due[:7] for pixel in row if pixel != visual.BLACK)


def test_reminder_is_opt_in_and_strictly_validated():
    assert not utility_hub.config_from_mapping({}).reminders_enabled
    assert utility_hub.config_from_mapping({"desk_reminder": {"enabled": True}}).reminders_enabled
    with pytest.raises(ValueError):
        utility_hub.config_from_mapping({"desk_reminder": {"enabled": "false"}})

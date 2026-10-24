"""Desktop actions against temporary files and real focus controller logic."""
from dataclasses import replace
from unittest.mock import Mock
import time

import pytest

from desk_controls import ControlsModel, DeskControls, status_is_fresh
from focus_control import FocusControlError, FocusStatus, StatusKind
from focus_controller import FocusController
from focus_sprint import SprintState, save_state
import desk_reminder


@pytest.mark.parametrize("checkpoint,fresh", [(1000, True), (910, True), (909, False), (1001, False), (None, False)])
def test_status_freshness(checkpoint, fresh):
    assert status_is_fresh(FocusStatus(StatusKind.WORK, checkpoint=checkpoint), 1000) is fresh


def test_controls_never_queue_to_a_stale_or_missing_controller(tmp_path):
    model = ControlsModel(tmp_path)
    with pytest.raises(ValueError, match="not reporting"):
        model.send_focus("start", now=1000)
    assert not model.queue.directory.exists()
    save_state(tmp_path / "focus_state.json", SprintState.idle(wall_checkpoint=100, monotonic_checkpoint=100))
    with pytest.raises(ValueError):
        model.send_focus("start", now=1000)
    assert not model.queue.directory.exists()


def test_command_acknowledgement_round_trip_with_real_controller(tmp_path):
    now = time.time()
    model = ControlsModel(tmp_path)
    state_path = tmp_path / "focus_state.json"
    save_state(state_path, SprintState.idle(wall_checkpoint=now, monotonic_checkpoint=time.monotonic()))
    identifier = model.send_focus("start", preset="sprint", now=now)
    with pytest.raises(ValueError, match="previous command"):
        model.send_focus("start", now=now)
    assert not model.acknowledge(model.focus_status())
    controller = FocusController(Mock(), queue=model.queue, state_path=state_path)
    result = controller.tick()
    assert result.commands_processed == 1
    status = model.focus_status()
    assert status.kind is StatusKind.WORK
    assert 898 <= status.remaining_seconds <= 900
    assert identifier in status.consumed_command_ids
    assert model.acknowledge(status)
    assert model.pending_id is None


def test_native_window_controls_and_layout_in_demo(tmp_path):
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        model = ControlsModel(tmp_path)
        app = DeskControls(root, model, demo=True)
        root.update_idletasks()
        assert app.timer.get().startswith("25:") or app.timer.get().startswith("24:")
        app.focus_action("start", preset="classic")
        assert not model.queue.directory.exists()
        app.select_preset("BREAK", 60)
        assert app.label.get() == "BREAK" and app.minutes.get() == "60"
        assert not model.reminder_path.exists()  # Presets never start a timer.
        app.label.set("WATER")
        app.minutes.set("2")
        app.set_reminder()
        first = desk_reminder.read_reminder(model.reminder_path)
        assert first.label == "WATER"
        assert app.set_button.cget("text") == "Replace reminder"
        assert not app.snooze_button.instate(["disabled"])
        import desk_preferences
        assert desk_preferences.load(model.preferences_path).label == "WATER"
        app.pin.set(True)
        app.set_topmost()
        assert desk_preferences.load(model.preferences_path).keep_on_top
        app.snooze()
        snoozed = desk_reminder.read_reminder(model.reminder_path)
        assert snoozed.due_at > first.due_at
        app.label.set("TOO LONG")
        app.set_reminder()
        assert desk_reminder.read_reminder(model.reminder_path) == snoozed
        app.label.set("TEA")
        app.preview_label()
        assert len(app.canvas.find_all()) == 256
        app.dismiss()
        assert not model.reminder_path.exists()
        assert app.snooze_button.instate(["disabled"])
        assert app.dismiss_button.instate(["disabled"])
        assert app.set_button.cget("text") == "Set reminder"
        # The native toolkit computes text extents, including system DPI.
        assert root.winfo_reqwidth() <= 1000
        assert root.winfo_reqheight() <= 1050
        root.update_idletasks()
        assert float(app.viewport.cget("scrollregion").split()[3]) >= app.body.winfo_reqheight()
        for button in app.focus_buttons:
            assert button.winfo_reqwidth() < 200
    finally:
        root.destroy()


def test_reminder_caption_crosses_deadline_without_false_remaining_time():
    from desk_controls import reminder_caption
    reminder = desk_reminder.Reminder('test', 'TEA', 1000, 1120)
    assert '2:00 left' in reminder_caption(reminder, 1000)
    assert '0:01 left' in reminder_caption(reminder, 1119.9)
    assert 'due now' in reminder_caption(reminder, 1120)
    assert 'expired' in reminder_caption(reminder, 1720)
    assert 'clock moved backwards' in reminder_caption(reminder, 999)

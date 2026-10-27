"""Unified control paths, local-only feedback and durable Focus receipts."""
from dataclasses import replace
import json
import time
from unittest.mock import Mock

import pytest

import control_status
import desk_controls as controls
import desk_preferences as preferences
import driftmatrix
import focus_controller
from config_loader import ConfigError, example_document, load_config
from focus_control import FocusCommandQueue, FocusStatus, StatusKind
from focus_sprint import SprintState, load_state, save_state


def configuration(tmp_path):
    document = example_document()
    document['runtime']['data_dir'] = str(tmp_path / 'state')
    document['panel']['url'] = 'http://panel.test'
    document['services']['hub']['enabled'] = True
    document['services']['hub']['options']['focus_sprint'] = {'enabled': True}
    path = tmp_path / 'driftmatrix.json'
    path.write_text(json.dumps(document), encoding='utf-8')
    return path, document


def test_selected_config_controls_all_local_paths_without_creating_state(tmp_path):
    path, _ = configuration(tmp_path)
    model = controls.controls_model(config_path=path)
    assert model.data_dir == tmp_path / 'state'
    assert model.queue.directory == model.data_dir / 'focus_commands'
    assert model.reminder_path.parent == model.preferences_path.parent == model.data_dir
    assert not model.data_dir.exists()


def test_default_config_is_used_and_explicit_legacy_directory_wins(tmp_path, monkeypatch):
    path, _ = configuration(tmp_path)
    monkeypatch.setattr(controls, 'default_config_path', lambda: path)
    assert controls.controls_model().config.source_path == path
    model = controls.controls_model(data_dir=tmp_path / 'legacy')
    assert model.config is None and model.data_dir == tmp_path / 'legacy'


def test_missing_default_falls_back_but_explicit_invalid_config_does_not(tmp_path, monkeypatch):
    path = tmp_path / 'missing.json'
    monkeypatch.setattr(controls, 'default_config_path', lambda: path)
    monkeypatch.setattr(controls, 'default_data_dir', lambda: tmp_path / 'legacy')
    assert controls.controls_model().data_dir == tmp_path / 'legacy'
    with pytest.raises(ConfigError):
        controls.controls_model(config_path=path)


def test_retargeted_config_never_silently_moves_control_writes(tmp_path):
    path, document = configuration(tmp_path)
    model = controls.controls_model(config_path=path)
    document['runtime']['data_dir'] = str(tmp_path / 'different')
    path.write_text(json.dumps(document), encoding='utf-8')
    with pytest.raises(ConfigError, match='target changed'):
        model.send_focus('start')
    assert 'unavailable' in model.host_summary().headline
    assert not (tmp_path / 'state').exists() and not (tmp_path / 'different').exists()


def test_disabled_or_stopped_host_never_gets_a_new_focus_command(tmp_path, monkeypatch):
    path, document = configuration(tmp_path)
    model = controls.controls_model(config_path=path)
    now = time.time()
    save_state(model.data_dir / 'focus_state.json', SprintState.idle(
        wall_checkpoint=now, monotonic_checkpoint=time.monotonic()))
    monkeypatch.setattr(model, 'service_snapshot', lambda config: {'live': False})
    with pytest.raises(ValueError, match='must be running'):
        model.send_focus('start', now=now)
    document['services']['hub']['options']['focus_sprint']['enabled'] = False
    path.write_text(json.dumps(document), encoding='utf-8')
    assert 'disabled' in model.focus_configuration_reason()
    with pytest.raises(ValueError, match='disabled'):
        model.send_focus('start', now=now)
    assert not model.queue.directory.exists()


def test_pending_feedback_never_resends_and_recovers_missing_receipt(tmp_path):
    model = controls.ControlsModel(tmp_path)
    save_state(tmp_path / 'focus_state.json', SprintState.idle(
        wall_checkpoint=1000, monotonic_checkpoint=1000))
    identifier = model.send_focus('start', minutes=40, now=1000)
    status = FocusStatus(StatusKind.WORK, checkpoint=1031)
    assert model.pending_state(status, 1031) == 'slow'
    assert len(model.queue.read()) == 1
    # A missing file is not called success; a newer fresh state allows recovery
    # with an explicit unconfirmed outcome, without a duplicate action.
    model.queue.ack(model.queue.read()[0])
    assert model.pending_state(status, 1031) == 'unconfirmed'
    assert model.pending_id is None and not model.queue.read()
    assert identifier not in status.consumed_command_ids


def test_config_change_requires_host_adoption_before_new_focus_actions(tmp_path, monkeypatch):
    path, _ = configuration(tmp_path)
    model = controls.controls_model(config_path=path)
    monkeypatch.setattr(model, 'service_snapshot', lambda config: {
        'live': True, 'config_matches': False, 'services': {'hub': {'live': True}}})
    with pytest.raises(ValueError, match='different configuration'):
        model.send_focus('start')
    assert not model.queue.directory.exists()


@pytest.mark.parametrize('value', ['0', '1441', '1.5', 'bad', True, 2.5, None])
def test_custom_durations_reject_invalid_values(value):
    with pytest.raises(ValueError, match='whole number'):
        controls.minute_value(value)


def test_old_preferences_get_new_defaults_without_rewriting(tmp_path):
    path = tmp_path / 'prefs.json'
    old = b'{"schema_version":1,"label":"TEA","minutes":5,"keep_on_top":false}'
    path.write_bytes(old)
    result = preferences.load(path)
    assert (result.focus_minutes, result.snooze_minutes) == (25, 5)
    assert path.read_bytes() == old
    chosen = replace(result, focus_minutes=40, snooze_minutes=12)
    preferences.save(path, chosen)
    assert preferences.load(path) == chosen


@pytest.mark.parametrize('field', ['focus_minutes', 'snooze_minutes'])
def test_invalid_new_preferences_preserve_saved_file(tmp_path, field):
    path = tmp_path / 'prefs.json'
    preferences.save(path, preferences.Preferences())
    before = path.read_bytes()
    with pytest.raises(ValueError):
        preferences.save(path, replace(preferences.Preferences(), **{field: True}))
    assert path.read_bytes() == before


def test_service_summary_does_not_call_dead_workers_running():
    snapshot = {'live': False, 'healthy': False, 'config_matches': True,
                'services': {'hub': {'state': 'RUNNING', 'live': False}}}
    summary = control_status.summarize(snapshot)
    assert summary.headline == 'Host stopped'
    assert all(state != 'Running' for _, state, _ in summary.rows)
    assert 'different configuration' not in control_status.summarize(
        {'live': False, 'services': {}}).detail


def test_service_summary_explains_restarts_circuit_and_config_changes():
    snapshot = {'live': True, 'healthy': False, 'degraded': True, 'config_matches': False,
                'services': {'media': {'state': 'FAILED', 'circuit_open': True, 'restart_count': 5}}}
    summary = control_status.summarize(snapshot)
    assert summary.headline == 'A service needs attention'
    assert 'different configuration' in summary.detail
    media = next(row for row in summary.rows if row[0] == 'Now Playing')
    assert media[1] == 'Needs attention' and '5 automatic restarts' in media[2]


def test_unified_cli_forwards_controls_without_loading_worker_runtime(monkeypatch):
    launch = Mock(return_value=0)
    monkeypatch.setattr(controls, 'main', launch)
    assert driftmatrix.main(['controls', '--demo']) == 0
    launch.assert_called_once_with(['--demo'])


def test_native_custom_focus_snooze_and_service_demo_stay_isolated(tmp_path, monkeypatch, desktop_tk):
    import tkinter as tk
    import desk_reminder
    root = tk.Toplevel(desktop_tk); root.withdraw()
    model = controls.ControlsModel(tmp_path)
    monkeypatch.setattr(model, 'host_summary', Mock(side_effect=AssertionError('demo read live status')))
    try:
        app = controls.DeskControls(root, model, demo=True)
        app.focus_minutes.set('40'); app.start_custom_focus()
        assert preferences.load(model.preferences_path).focus_minutes == 40
        assert not model.queue.directory.exists()
        app.label.set('TEA'); app.minutes.set('2'); app.set_reminder()
        app.snooze_minutes.set('12'); app.snooze()
        reminder = desk_reminder.read_reminder(model.reminder_path)
        assert 719 <= reminder.due_at - time.time() <= 720
        assert preferences.load(model.preferences_path).snooze_minutes == 12
        saved = model.reminder_path.read_bytes()
        app.snooze_minutes.set('bad'); app.snooze()
        assert model.reminder_path.read_bytes() == saved
        assert 'whole number' in app.message.get()
        monkeypatch.setattr(preferences, 'save', Mock(side_effect=OSError('read only')))
        app.snooze_minutes.set('12'); app.snooze()
        assert 'preferences could not be saved' in app.message.get()
        app.tabs.select(1); app.refresh_services()
        assert app.host_headline.get() == 'DEMO · Host running'
        assert app.service_rows['Drift-Link'][0].get() == 'Disabled'
        assert app.service_rows['Weather'][0].get() == 'Running'
        app.refresh(); previous = app.refresh_after_id
        app.refresh()
        assert previous != app.refresh_after_id
        assert previous not in root.tk.call('after', 'info')
        root.update_idletasks()
        assert root.winfo_reqwidth() <= 1000
    finally:
        root.destroy()


def test_native_focus_actions_explain_idle_and_paused_states(tmp_path, monkeypatch, desktop_tk):
    import tkinter as tk
    root = tk.Toplevel(desktop_tk); root.withdraw()
    model = controls.ControlsModel(tmp_path)
    monkeypatch.setattr(model, 'focus_status', lambda: FocusStatus(StatusKind.IDLE, checkpoint=time.time()))
    try:
        app = controls.DeskControls(root, model)
        for button, action in app.focus_actions:
            assert button.instate(['disabled']) is (action != 'start')
        monkeypatch.setattr(model, 'focus_status', lambda: FocusStatus(
            StatusKind.PAUSED, remaining_seconds=900, checkpoint=time.time()))
        app.refresh()
        assert app.toggle_button.cget('text') == 'Resume'
        assert all(not button.instate(['disabled']) for button in app.focus_buttons)
    finally:
        root.destroy()


def controller(tmp_path):
    queue = FocusCommandQueue(tmp_path / 'commands')
    clock = Mock()
    clock.monotonic.return_value = 1000.0
    clock.wall_time.return_value = 1750000000.0
    panel = Mock()
    panel.activate_app.return_value.sent = True
    return focus_controller.FocusController(panel, queue=queue, state_path=tmp_path / 'state.json', clock=clock), queue


@pytest.mark.parametrize('restart', [False, True])
def test_failed_state_save_keeps_command_and_add_is_not_applied_twice(tmp_path, monkeypatch, restart):
    app, queue = controller(tmp_path)
    queue.enqueue('start', minutes=15); app.tick()
    queue.enqueue('add', minutes=5)
    original = focus_controller.save_state
    monkeypatch.setattr(focus_controller, 'save_state', Mock(side_effect=OSError('disk unavailable')))
    tick = app.tick()
    assert not tick.state_saved and len(queue.read()) == 1
    assert load_state(tmp_path / 'state.json').remaining_seconds == 900
    monkeypatch.setattr(focus_controller, 'save_state', original)
    if restart:
        app, queue = controller(tmp_path)
    app.tick()
    assert not queue.read()
    assert load_state(tmp_path / 'state.json').remaining_seconds == 1200


def test_failed_ack_stops_batch_before_replay_history_can_be_evicted(tmp_path, monkeypatch):
    app, queue = controller(tmp_path)
    first = queue.enqueue('start', minutes=15)
    for _ in range(260):
        queue.enqueue('stop')
    original = queue.ack
    monkeypatch.setattr(queue, 'ack', Mock(side_effect=OSError('queue busy')))
    result = app.tick()
    assert result.commands_processed == 1 and len(queue.read()) == 261
    assert first.id in load_state(tmp_path / 'state.json').consumed_command_ids
    monkeypatch.setattr(queue, 'ack', original)
    app, queue = controller(tmp_path)
    app.tick()
    assert not queue.read()

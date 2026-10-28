"""Declared policy selection, fresh guards, scoped state and offset timestamps."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

import config_loader
import maintenance_policy as policy
import panel_gate
import panel_maintenance as pm
from runtime_paths import current_data_dir
import script_swap as swap
import supervisor
import utility_hub as hub
from test_script_swap import BASE, FakeClock, FakePanel, NEW, OLD, source_files


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'localappdata'))


def canonical(tmp_path, *, default=False, enabled=True, maintenance=False, zone='UTC'):
    path = config_loader.default_config_path() if default else tmp_path / 'driftmatrix.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    document = config_loader.example_document()
    document['panel']['url'] = BASE
    document['panel']['timezone'] = zone
    document['runtime']['data_dir'] = str(tmp_path / 'runtime')
    document['services']['hub']['enabled'] = enabled
    document['services']['hub']['options']['maintenance'] = {
        'enabled': maintenance, 'window_start': '21:00', 'window_end': '23:00',
        'min_uptime_hours': 12, 'low_memory_reboot': True, 'low_memory_min_free_bytes': 10000}
    path.write_text(json.dumps(document), encoding='utf-8-sig')
    return path, document


def make_tool(tmp_path, config, *extra):
    new, old = source_files(tmp_path)
    argv = ['moon', str(new), '--expect-version', '2.1', '--rollback', str(old),
            '--rollback-version', '2.0', '--config', str(config), '--no-fidelity', *extra]
    options, _log = swap.options_from_args(swap.build_parser().parse_args(argv), argv)
    clock = FakeClock(wall=datetime(2026, 10, 27, 18, 0, tzinfo=timezone.utc))
    panel = FakePanel(clock)
    tool = swap.ScriptSwap(options, session=panel, clock=clock, logger=Mock())
    return tool, panel, clock


def test_default_canonical_wins_and_explicit_legacy_remains_explicit(tmp_path):
    config, _doc = canonical(tmp_path, default=True)
    legacy = hub.default_config_path()
    legacy.write_text(json.dumps({'panel_url': BASE, 'maintenance': {'enabled': True}}))
    selected = policy.resolve_maintenance_context()
    assert selected.kind == 'canonical' and selected.path == config
    assert selected.policy.enabled is False and selected.policy.timezone_name == 'UTC'
    explicit = policy.resolve_maintenance_context(hub_config=legacy)
    assert explicit.kind == 'legacy' and explicit.policy.enabled is True
    assert explicit.policy.timezone_name is None and explicit.explicit
    assert explicit.data_dir == legacy.parent


def test_legacy_fallback_and_no_file_defaults_are_distinct_and_watch_discovery(tmp_path):
    empty = policy.resolve_maintenance_context()
    assert empty.kind == 'defaults' and empty.policy.enabled
    legacy = hub.default_config_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({'maintenance': {'enabled': False}}))
    with pytest.raises(policy.MaintenancePolicyError, match='appeared'):
        empty.assert_unchanged()
    selected = policy.resolve_maintenance_context()
    assert selected.kind == 'legacy' and not selected.policy.enabled
    config_loader.default_config_path().write_text('{invalid')
    with pytest.raises(policy.MaintenancePolicyError, match='appeared'):
        selected.assert_unchanged()
    with pytest.raises(policy.MaintenancePolicyError, match='canonical'):
        policy.resolve_maintenance_context()  # Never silently fall back to valid legacy.


@pytest.mark.parametrize('raw', [b'{', b'[]', b'null', b'\xff',
    b'{"schema_version":1,"schema_version":1}', b'{"secret-private":NaN}'])
def test_invalid_selected_configuration_is_private_and_never_falls_back(tmp_path, raw):
    path, _doc = canonical(tmp_path, default=True)
    path.write_bytes(raw)
    for kwargs in ({}, {'config': path}):
        with pytest.raises(policy.MaintenancePolicyError) as error:
            policy.resolve_maintenance_context(**kwargs)
        assert 'secret-private' not in str(error.value) and 'NaN' not in str(error.value)


@pytest.mark.parametrize('kind', ['config', 'hub_config'])
def test_explicit_missing_files_are_errors_even_when_other_defaults_exist(tmp_path, kind):
    canonical(tmp_path, default=True)
    with pytest.raises(policy.MaintenancePolicyError):
        policy.resolve_maintenance_context(**{kind: tmp_path / 'missing.json'})


def test_canonical_file_cannot_silently_be_interpreted_as_legacy_defaults(tmp_path):
    path, _doc = canonical(tmp_path)
    with pytest.raises(policy.MaintenancePolicyError, match='requires --config'):
        policy.resolve_maintenance_context(hub_config=path)
    with pytest.raises(policy.MaintenancePolicyError, match='Choose'):
        policy.resolve_maintenance_context(config=path, hub_config=path)


@pytest.mark.parametrize('url', ['http://other.test', 'http://panel.test:1234'])
def test_explicit_panel_must_agree_with_selected_policy(tmp_path, url):
    path, _doc = canonical(tmp_path)
    with pytest.raises(policy.MaintenancePolicyError, match='disagrees'):
        policy.resolve_maintenance_context(config=path, panel_url=url)
    assert policy.resolve_maintenance_context(config=path, panel_url=BASE + '/').panel_url == BASE


def test_policy_comes_from_exact_worker_mapping_and_its_runtime_defaults(tmp_path, monkeypatch):
    path, _doc = canonical(tmp_path, maintenance=True, zone='America/New_York')
    context = policy.resolve_maintenance_context(config=path)
    config = config_loader.load_config(path)
    run = Mock()
    monkeypatch.setattr(hub, 'run_hub', run)
    assert hub.run_worker(config, threading.Event()) == 0
    worker = run.call_args.args[0]
    actual = hub.build_maintenance_config(worker.maintenance)
    assert worker.panel_url == BASE and worker.next_up.timezone_name == 'America/New_York'
    assert context.policy.enabled == actual.enabled
    assert (context.policy.window_start_minute, context.policy.window_end_minute,
            context.policy.low_memory_min_free_bytes, context.policy.low_memory_max_per_day) == (
        actual.window_start_minute, actual.window_end_minute, actual.low_memory_min_free_bytes,
        actual.low_memory_max_per_day)
    assert context.fingerprint == sha256(path.read_bytes()).hexdigest()
    assert context.summary()['runtime_config_hash'] == config.config_hash


@pytest.mark.parametrize('hub_enabled,maintenance_enabled', [(False, True), (True, False), (False, False)])
def test_disabled_policy_has_no_due_reboot_but_history_in_progress_is_still_respected(tmp_path, hub_enabled, maintenance_enabled):
    path, _doc = canonical(tmp_path, enabled=hub_enabled, maintenance=maintenance_enabled)
    context = policy.resolve_maintenance_context(config=path)
    assert not context.policy.enabled
    now = datetime(2026, 10, 27, 22)
    assert not swap.maintenance_view([], now, context.policy, 100000)['nightly_pending']
    assert swap.low_memory_view([], now, context.policy, {'uptimeSeconds': 100000, 'minFreeHeapBytes': 1}) is None
    tool, panel, clock = make_tool(tmp_path, path)
    history = tool.opts.maintenance_report
    history.parent.mkdir(parents=True)
    history.write_text(json.dumps({'last': {'at': clock.wall_now().isoformat(), 'outcome': pm.IN_PROGRESS}}))
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []


def test_custom_runtime_controls_history_lock_and_gate_even_with_separate_evidence(tmp_path, monkeypatch):
    path, _doc = canonical(tmp_path)
    evidence = tmp_path / 'evidence'
    tool, panel, _clock = make_tool(tmp_path, path, '--data-dir', str(evidence))
    monkeypatch.delenv(panel_gate.DIR_ENV)
    assert tool.opts.report_path.parent == evidence
    assert tool.opts.maintenance_report.parent == tmp_path / 'runtime'
    assert tool.run() == swap.EXIT_OK
    runtime = tmp_path / 'runtime'
    assert (runtime / swap.LOCK_NAME).exists() and not (evidence / swap.LOCK_NAME).exists()
    rows = [json.loads(line) for line in (runtime / panel_gate.EVENTS_NAME).read_text().splitlines()]
    assert len(rows) == 4 and all(row['gate'] == 'ok' for row in rows)
    assert not (evidence / panel_gate.EVENTS_NAME).exists()
    assert current_data_dir() is None
    assert tool.entry['maintenance_selection']['history'] == str(runtime / 'maintenance.json')
    assert tool.entry['maintenance_selection']['supervisor']['state'] == 'NOT_RUNNING'


def test_read_only_default_does_not_create_runtime_evidence_or_lock(tmp_path):
    path, _doc = canonical(tmp_path)
    tool, panel, _clock = make_tool(tmp_path, path, '--dry-run')
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert tool.run() == swap.EXIT_OK and panel.writes == []
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert before == after and not (tmp_path / 'runtime').exists()


def test_explicit_history_override_is_retained_in_recovery_args(tmp_path):
    path, _doc = canonical(tmp_path)
    history = tmp_path / 'chosen-history.json'
    tool, _panel, _clock = make_tool(tmp_path, path, '--maintenance-report', str(history), '--dry-run')
    assert tool.run() == swap.EXIT_OK
    tool._rollback_command()
    argv = tool.entry['recovery_argv']
    assert argv[argv.index('--config') + 1] == str(path)
    assert argv[argv.index('--maintenance-report') + 1] == str(history)


def test_derived_history_is_reselected_from_canonical_path_in_recovery(tmp_path):
    path, _doc = canonical(tmp_path)
    tool, _panel, _clock = make_tool(tmp_path, path, '--dry-run')
    assert tool.run() == swap.EXIT_OK
    tool._rollback_command()
    argv = tool.entry['recovery_argv']
    assert '--maintenance-report' not in argv
    replay, _log = swap.options_from_args(swap.build_parser().parse_args(argv), argv)
    assert replay.maintenance_report == tool.opts.maintenance_report and replay.base_url == BASE
    assert replay.maintenance_context.fingerprint == tool.opts.maintenance_context.fingerprint


def test_configuration_change_during_pacing_blocks_the_first_dispatch(tmp_path):
    path, _doc = canonical(tmp_path)
    tool, panel, _clock = make_tool(tmp_path, path)
    wait = tool.client._wait_for_mutation_slot
    def change():
        result = wait()
        path.write_bytes(path.read_bytes() + b'\n')
        return result
    tool.client._wait_for_mutation_slot = change
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []
    assert tool.entry['writes'] == 0 and 'configuration changed' in tool.entry['error']


def test_configuration_change_after_delete_pauses_without_automatic_rollback_and_can_resume(tmp_path):
    path, _doc = canonical(tmp_path)
    tool, panel, clock = make_tool(tmp_path, path)
    delete = panel.delete
    def change(*args, **kwargs):
        result = delete(*args, **kwargs)
        path.write_bytes(path.read_bytes() + b'\n')
        return result
    panel.delete = change
    assert tool.run() == swap.EXIT_INTERRUPTED and tool.entry['outcome'] == swap.PAUSED
    assert panel.sequence() == [('DELETE', None)]
    panel.delete = delete
    options, _log = swap.options_from_args(swap.build_parser().parse_args(tool.opts.argv), tool.opts.argv)
    resumed = swap.ScriptSwap(options, session=panel, clock=clock, logger=Mock())
    assert resumed.run() == swap.EXIT_OK
    assert panel.sequence().count(('DELETE', None)) == 1


def test_fresh_history_added_during_pacing_blocks_delete_even_if_hub_disabled(tmp_path):
    path, _doc = canonical(tmp_path, enabled=False)
    tool, panel, clock = make_tool(tmp_path, path)
    wait = tool.client._wait_for_mutation_slot
    def pending():
        result = wait()
        tool.opts.maintenance_report.write_text(json.dumps({'last': {
            'at': clock.wall_now().isoformat(), 'outcome': pm.IN_PROGRESS}}))
        return result
    tool.client._wait_for_mutation_slot = pending
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []


@pytest.mark.parametrize('raw', [b'{', b'[]', b'{"history":"bad"}', b'{"history":[null]}',
    b'{"last":null,"last":{}}', b'{"last":{"outcome":"in-progress","at":"unknown"}}'])
def test_unreadable_or_uncertain_selected_history_cannot_authorize_writes(tmp_path, raw):
    path, _doc = canonical(tmp_path)
    tool, panel, _clock = make_tool(tmp_path, path)
    tool.opts.maintenance_report.parent.mkdir()
    tool.opts.maintenance_report.write_bytes(raw)
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []


@pytest.mark.parametrize('observed', [{'live': True, 'config_matches': False}, {'live': True},
    {'live': True, 'config_matches': True, 'state': 'STARTING'},
    {'live': True, 'config_matches': True, 'state': 'STOPPING'}])
def test_active_supervisor_with_another_or_unconfirmed_config_stops_before_panel_reads(tmp_path, monkeypatch, observed):
    path, _doc = canonical(tmp_path)
    tool, panel, _clock = make_tool(tmp_path, path)
    panel.get = Mock(wraps=panel.get)
    monkeypatch.setattr(supervisor, 'status', lambda _config: observed)
    assert tool.run() == swap.EXIT_ABORTED
    panel.get.assert_not_called()
    assert panel.writes == []


def test_active_supervisor_configuration_is_rechecked_after_a_partial_swap(tmp_path, monkeypatch):
    path, _doc = canonical(tmp_path)
    tool, panel, _clock = make_tool(tmp_path, path)
    live = {'live': True, 'config_matches': True, 'state': 'RUNNING'}
    monkeypatch.setattr(supervisor, 'status', lambda _config: live.copy())
    delete = panel.delete
    def change(*args, **kwargs):
        result = delete(*args, **kwargs)
        live['config_matches'] = False
        return result
    panel.delete = change
    assert tool.run() == swap.EXIT_INTERRUPTED
    assert tool.entry['outcome'] == swap.PAUSED and panel.sequence() == [('DELETE', None)]


def test_offset_history_uses_configured_zone_and_elapsed_age_survives_dst():
    selected = policy.HubMaintenance(window_start_minute=21*60, window_end_minute=23*60,
                                     min_uptime_hours=0, timezone_name='America/New_York')
    wall = datetime(2026, 10, 27, 2, tzinfo=timezone.utc)
    now = policy.maintenance_local_time(wall, selected.timezone_name)
    entries = [{'at': '2026-10-27T01:15:00Z', 'kind': 'nightly', 'outcome': 'ok'}]
    view = swap.maintenance_view(entries, now, selected, 100000, wall_now=wall)
    assert view['inside_window'] and not view['nightly_pending'] and view['nightly_done']
    spring = datetime.fromisoformat('2026-03-08T03:05:00-04:00')
    at = '2026-03-08T01:55:00-05:00'
    assert policy.maintenance_elapsed_seconds(spring, at) == 600
    active = swap.maintenance_view([{'at': at, 'outcome': pm.IN_PROGRESS}],
        policy.maintenance_local_time(spring, selected.timezone_name), selected, 1000, wall_now=spring)
    assert active['in_progress']['age_seconds'] == 600
    assert active['in_progress']['stale'] is False
    fall = datetime.fromisoformat('2026-11-01T01:50:00-05:00')
    assert policy.maintenance_elapsed_seconds(fall, '2026-11-01T01:30:00-04:00') == 4800


def test_same_runtime_lock_is_shared_across_separate_evidence_directories(tmp_path):
    path, _doc = canonical(tmp_path)
    one, _p1, _c1 = make_tool(tmp_path, path, '--data-dir', str(tmp_path / 'one'))
    two, _p2, _c2 = make_tool(tmp_path, path, '--data-dir', str(tmp_path / 'two'))
    fd = one._acquire_lock()
    try:
        with pytest.raises(swap.SwapAbort, match='another script_swap'):
            two._acquire_lock()
    finally:
        one._release_lock(fd)


def test_invalid_cli_configuration_returns_a_private_diagnostic_without_panel_or_file_writes(tmp_path, capsys):
    path, _doc = canonical(tmp_path)
    path.write_text('{"private-secret":NaN}')
    new, old = source_files(tmp_path)
    panel = Mock()
    assert swap.main(['moon', str(new), '--expect-version', '2.1', '--rollback', str(old),
                      '--rollback-version', '2.0', '--config', str(path)], session=panel) == swap.EXIT_ABORTED
    assert not panel.mock_calls and not (tmp_path / 'runtime').exists()
    assert 'private-secret' not in capsys.readouterr().err


def test_one_byte_snapshot_drives_policy_and_fingerprint_even_if_file_changes_during_mapping(tmp_path, monkeypatch):
    path, _doc = canonical(tmp_path)
    original = path.read_bytes()
    mapping = hub.hub_config_from_canonical
    def changed(config):
        path.write_bytes(original + b'\n')
        return mapping(config)
    monkeypatch.setattr(hub, 'hub_config_from_canonical', changed)
    selected = policy.resolve_maintenance_context(config=path)
    assert selected.fingerprint == sha256(original).hexdigest()
    with pytest.raises(policy.MaintenancePolicyError, match='changed'):
        selected.assert_unchanged()


def test_runtime_status_errors_refuse_before_any_panel_io(tmp_path, monkeypatch):
    path, _doc = canonical(tmp_path)
    tool, panel, _clock = make_tool(tmp_path, path)
    panel.get = Mock(wraps=panel.get)
    monkeypatch.setattr(supervisor, 'status', Mock(side_effect=config_loader.ConfigError('invalid local status')))
    assert tool.run() == swap.EXIT_ABORTED
    panel.get.assert_not_called()


def test_removed_configuration_after_delete_pauses_instead_of_using_defaults(tmp_path):
    path, _doc = canonical(tmp_path)
    tool, panel, _clock = make_tool(tmp_path, path)
    delete = panel.delete
    def removed(*args, **kwargs):
        result = delete(*args, **kwargs)
        path.unlink()
        return result
    panel.delete = removed
    assert tool.run() == swap.EXIT_INTERRUPTED
    assert panel.sequence() == [('DELETE', None)] and tool.entry['outcome'] == swap.PAUSED


def test_bom_history_and_local_timezone_history_are_accepted_without_rewriting_files(tmp_path):
    path, _doc = canonical(tmp_path)
    tool, panel, clock = make_tool(tmp_path, path)
    history = tool.opts.maintenance_report
    history.parent.mkdir()
    history.write_text(json.dumps({'last': {'at': clock.wall_now().isoformat(), 'outcome': 'ok'}}), encoding='utf-8-sig')
    original = history.read_bytes()
    assert tool.run() == swap.EXIT_OK and panel.writes
    assert history.read_bytes() == original


def test_unscoped_or_different_endpoint_reports_cannot_resume_canonical_operations(tmp_path):
    path, _doc = canonical(tmp_path)
    tool, _panel, _clock = make_tool(tmp_path, path, '--dry-run')
    assert tool.run() == swap.EXIT_OK
    assert tool._same_sources(tool.entry)
    assert not tool._same_sources(tool.entry | {'base_url': 'http://other.test'})
    assert not tool._same_sources({key: value for key, value in tool.entry.items() if key != 'base_url'})
    assert not tool._same_sources(tool.entry | {'maintenance_selection': tool.entry['maintenance_selection'] | {
        'data_dir': str(tmp_path / 'another-runtime')}})


def test_relative_evidence_and_history_paths_are_bound_once_for_recovery(tmp_path, monkeypatch):
    path, _doc = canonical(tmp_path)
    monkeypatch.chdir(tmp_path)
    tool, _panel, _clock = make_tool(tmp_path, path, '--data-dir', 'evidence', '--maintenance-report', 'chosen.json')
    assert tool.opts.report_path.parent == tmp_path / 'evidence'
    assert tool.opts.maintenance_report == tmp_path / 'chosen.json'
    assert tool.opts.new_path.is_absolute() and tool.opts.rollback_path.is_absolute()


def test_same_bytes_resolving_to_a_different_source_do_not_keep_a_stale_relative_runtime(tmp_path, monkeypatch):
    path, _doc = canonical(tmp_path)
    resolved = Path.resolve
    targets = [tmp_path / 'first' / 'config.json']
    def resolution(self, *args, **kwargs):
        return targets[0] if self == path else resolved(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolution)
    context = policy.resolve_maintenance_context(config=path)
    assert context.resolved_source == targets[0]
    targets[0] = tmp_path / 'second' / 'config.json'
    with pytest.raises(policy.MaintenancePolicyError, match='different source'):
        context.assert_unchanged()


def test_resolution_failure_after_selection_is_a_policy_error(tmp_path, monkeypatch):
    path, _doc = canonical(tmp_path)
    context = policy.resolve_maintenance_context(config=path)
    resolved = Path.resolve
    def looped(self, *args, **kwargs):
        if self == path:
            raise RuntimeError('symlink loop')
        return resolved(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', looped)
    with pytest.raises(policy.MaintenancePolicyError, match='no longer readable'):
        context.assert_unchanged()


def test_invalid_programmatic_endpoint_is_refused_before_panel_io(tmp_path):
    from dataclasses import replace
    path, _doc = canonical(tmp_path)
    tool, panel, clock = make_tool(tmp_path, path)
    bad = swap.ScriptSwap(replace(tool.opts, base_url='invalid-endpoint'), session=panel, clock=clock, logger=Mock())
    panel.get = Mock(wraps=panel.get)
    assert bad.run() == swap.EXIT_ABORTED
    panel.get.assert_not_called()

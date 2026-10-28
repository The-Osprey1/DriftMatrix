"""Exact versionless identity with a fake panel, real gate and real snapshots."""
from hashlib import sha256
import json
from pathlib import Path

import pytest
import requests

import panel_gate
import script_swap as swap
from test_panel_client import make_client
from test_script_swap import BASE, FakeClock, FakePanel, FakeResponse, NEW, OLD, make_swap, last_entry
from weather.minify_ax import minify

LEGACY = '\n'.join(line for line in OLD.splitlines() if not line.startswith('# @version')) + '\n'
ORIGINAL_PIN = sha256(LEGACY.encode()).hexdigest()
UPLOAD_PIN = sha256(minify(LEGACY).encode()).hexdigest()


@pytest.fixture(autouse=True)
def _isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'localappdata'))


def legacy_swap(tmp_path, panel=None, clock=None, **options):
    clock = clock or FakeClock()
    panel = panel or FakePanel(clock, moon=LEGACY)
    panel.stored['moon'] = LEGACY
    panel._boot_log()
    panel._restore()
    tool = make_swap(tmp_path, panel, clock, old=LEGACY, rollback_version=swap.UNVERSIONED,
                     rollback_installed_sha256=ORIGINAL_PIN, rollback_sha256=UPLOAD_PIN, **options)
    return tool, panel, clock


def test_versionless_forward_path_takes_exact_snapshot_and_verifies_new_source(tmp_path, panel_gate_dir):
    tool, panel, _clock = legacy_swap(tmp_path)
    assert tool.run() == swap.EXIT_OK
    assert panel.sequence() == [('DELETE', None), ('POST', None), ('PUT', '2.1'), ('POST', None)]
    entry = last_entry(tmp_path)
    snapshot = Path(entry['snapshot']['path'])
    assert (snapshot / 'moon.ax').read_bytes() == LEGACY.encode()
    assert entry['rollback']['version'] is None
    assert entry['rollback']['identity']['installed_sha256'] == ORIGINAL_PIN
    assert entry['rollback']['identity']['upload_sha256'] == UPLOAD_PIN
    assert entry['installed_identity']['sha256'] == sha256(minify(NEW).encode()).hexdigest()
    assert entry['installed_version'] == '2.1'
    assert any(step['step'] == 'source-identity' and step['representation'] == 'installed-original' for step in entry['steps'])
    journal = [json.loads(line) for line in (panel_gate_dir / panel_gate.EVENTS_NAME).read_text().splitlines()]
    assert len(journal) == 4 and all(row['gate'] == 'ok' and row['status'] == 200 for row in journal)


def test_compile_failure_restores_minified_original_and_proves_its_distinct_hash(tmp_path):
    tool, panel, _clock = legacy_swap(tmp_path)
    panel.boot_errors[('moon', '2.1')] = 'attribute_error: nil'
    assert tool.run() == swap.EXIT_ROLLED_BACK
    assert panel.stored['moon'] == minify(LEGACY)
    assert ORIGINAL_PIN != UPLOAD_PIN
    entry = last_entry(tmp_path)
    assert entry['installed_version'] is None
    assert entry['installed_identity']['sha256'] == UPLOAD_PIN
    assert entry['verifications'][-1]['problems'] == []
    assert any(step['step'] == 'source-identity' and step['representation'] == 'minified-upload' for step in entry['steps'])


@pytest.mark.parametrize('digest', [None, '', 'a'*12, 'g'*64, '0'*63, '0'*65])
@pytest.mark.parametrize('field', ['installed_sha256', 'upload_pin'])
def test_legacy_requires_two_complete_hashes_before_any_panel_read(tmp_path, digest, field):
    path = tmp_path / 'old.ax'
    path.write_bytes(LEGACY.encode())
    kwargs = dict(installed_sha256=ORIGINAL_PIN, upload_pin=UPLOAD_PIN)
    kwargs[field] = digest
    with pytest.raises(swap.SwapAbort, match='64-hex'):
        swap.prepare_source(path, swap.UNVERSIONED, 'rollback', **kwargs)


@pytest.mark.parametrize('options', [dict(installed_sha256='0'*64), dict(upload_pin='0'*64)])
def test_valid_length_wrong_pins_do_not_admit_a_different_local_source(tmp_path, options):
    path = tmp_path / 'old.ax'
    path.write_bytes(LEGACY.encode())
    kwargs = dict(installed_sha256=ORIGINAL_PIN, upload_pin=UPLOAD_PIN) | options
    with pytest.raises(swap.SwapAbort, match='does not match'):
        swap.prepare_source(path, swap.UNVERSIONED, 'rollback', **kwargs)


def test_original_crlf_and_utf8_lf_are_explicit_local_representations_not_http_normalization(tmp_path):
    path = tmp_path / 'old.ax'
    crlf = LEGACY.replace('\n', '\r\n').encode()
    path.write_bytes(crlf)
    for pin, representation in [(sha256(crlf).hexdigest(), 'source-bytes'), (ORIGINAL_PIN, 'source-utf8-lf')]:
        source = swap.prepare_source(path, swap.UNVERSIONED, 'rollback', installed_sha256=pin, upload_pin=UPLOAD_PIN)
        assert source.installed_representation == representation and source.sha256 == UPLOAD_PIN
    tool, panel, _clock = legacy_swap(tmp_path)
    panel.stored['moon'] = LEGACY.replace('\n', '\r\n')
    assert tool.run() == swap.EXIT_ABORTED  # HTTP bytes are hashed exactly, never newline-normalized.
    assert panel.writes == []


def test_version_tag_new_unversioned_and_versioned_original_pin_are_rejected(tmp_path):
    path = tmp_path / 'old.ax'
    path.write_bytes(OLD.encode())
    with pytest.raises(swap.SwapAbort, match='no @version'):
        swap.prepare_source(path, swap.UNVERSIONED, 'rollback', installed_sha256=sha256(OLD.encode()).hexdigest(),
                            upload_pin=sha256(minify(OLD).encode()).hexdigest())
    path.write_bytes(LEGACY.encode())
    with pytest.raises(swap.SwapAbort, match='rollback-only'):
        swap.prepare_source(path, swap.UNVERSIONED, 'new', installed_sha256=ORIGINAL_PIN, upload_pin=UPLOAD_PIN)
    with pytest.raises(swap.SwapAbort, match='requires --rollback-version'):
        swap.prepare_source(path, '2.0', 'rollback', installed_sha256=ORIGINAL_PIN)


@pytest.mark.parametrize('prefix', ['\ufeff', '# @version\n'])
def test_hidden_or_empty_version_tags_cannot_be_admitted_as_unversioned(tmp_path, prefix):
    path = tmp_path / 'bad-header.ax'
    text = prefix + LEGACY
    path.write_bytes(text.encode())
    with pytest.raises(swap.SwapAbort):
        swap.prepare_source(path, swap.UNVERSIONED, 'rollback', installed_sha256=sha256(text.encode()).hexdigest(),
                            upload_pin=sha256(minify(text).encode()).hexdigest())


@pytest.mark.parametrize('failure', ['different', 'empty', 'http', 'network', 'wrong-version'])
def test_missing_or_foreign_live_identity_stops_before_all_writes(tmp_path, failure):
    tool, panel, _clock = legacy_swap(tmp_path)
    get = panel.get
    if failure == 'different':
        panel.stored['moon'] += '\n# another source\n'
    if failure == 'wrong-version':
        panel.stored['moon'] = OLD
    def read(url, **kwargs):
        if url.endswith('/apps/script/moon'):
            if failure == 'empty':
                return FakeResponse(content=b'')
            if failure == 'http':
                return FakeResponse(500, content=b'error')
            if failure == 'network':
                raise requests.ConnectionError('offline')
        return get(url, **kwargs)
    panel.get = read
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []
    assert last_entry(tmp_path)['writes'] == 0


@pytest.mark.parametrize('change', ['provenance', 'incomplete', 'wrong-panel', 'file-tamper', 'digest', 'malformed-root'])
def test_snapshot_must_preserve_the_fresh_panel_bytes_not_a_repository_guess(tmp_path, change):
    tool, panel, _clock = legacy_swap(tmp_path)
    capture = tool.snapshot_fn
    def snapshot(**kwargs):
        directory = capture(**kwargs)
        path = directory / 'manifest.json'
        manifest = json.loads(path.read_text())
        if change == 'provenance':
            manifest['exactScriptBackups']['moon']['provenance'] = 'repository'
        elif change == 'incomplete':
            manifest['complete'] = False
        elif change == 'wrong-panel':
            manifest['panel'] = 'http://other.test'
        elif change == 'digest':
            manifest['exactScriptBackups']['moon']['sha256'] = '0'*64
        elif change == 'malformed-root':
            manifest = []
        else:
            (directory / 'moon.ax').write_bytes(b'tampered')
        path.write_text(json.dumps(manifest))
        return directory
    tool.snapshot_fn = snapshot
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []


def test_source_changes_during_client_pacing_are_checked_inside_gate_before_delete(tmp_path, panel_gate_dir):
    tool, panel, _clock = legacy_swap(tmp_path)
    wait = tool.client._wait_for_mutation_slot
    def pacing():
        result = wait()
        panel.stored['moon'] += '\n# changed after snapshot\n'
        return result
    tool.client._wait_for_mutation_slot = pacing
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []
    assert last_entry(tmp_path)['writes'] == 0
    event = json.loads((panel_gate_dir / panel_gate.EVENTS_NAME).read_text().splitlines()[-1])
    assert event['gate'] == 'ok' and event['status'] is None and event['err'] == 'SwapError'


def test_unheld_gate_never_dispatches_even_though_normal_gate_policy_is_best_effort(tmp_path, monkeypatch):
    tool, panel, _clock = legacy_swap(tmp_path)
    monkeypatch.setattr(panel_gate, '_acquire', lambda *args: (None, 'timeout'))
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []
    assert last_entry(tmp_path)['writes'] == 0


def test_legacy_cannot_opt_out_of_the_mutation_gate(tmp_path):
    tool, panel, _clock = legacy_swap(tmp_path, allow_ungated=True)
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []


@pytest.mark.parametrize('point', ['put', 'reboot'])
def test_raced_in_foreign_source_is_neither_overwritten_deleted_nor_rebooted(tmp_path, point):
    tool, panel, _clock = legacy_swap(tmp_path)
    wait = tool.client._wait_for_mutation_slot
    changed = []
    def pacing():
        result = wait()
        if not changed and ((point == 'put' and panel.boots >= 1 and 'moon' not in panel.stored)
                            or (point == 'reboot' and panel.moon_version() == '2.1')):
            panel.stored['moon'] = minify(NEW) + '\n# foreign body with same version\n'
            panel.errors['moon'] = None
            changed.append(True)
        return result
    tool.client._wait_for_mutation_slot = pacing
    assert tool.run() == swap.EXIT_FAILED and changed
    assert panel.stored['moon'].endswith('# foreign body with same version\n')
    assert panel.sequence() == ([('DELETE', None), ('POST', None)] if point == 'put'
                                else [('DELETE', None), ('POST', None), ('PUT', '2.1')])


def test_interruption_after_delete_resumes_only_the_same_pinned_sources(tmp_path):
    tool, panel, clock = legacy_swap(tmp_path)
    post = panel.post
    def interrupt(*args, **kwargs):
        post(*args, **kwargs)
        raise KeyboardInterrupt
    panel.post = interrupt
    assert tool.run() == swap.EXIT_INTERRUPTED
    panel.post = post
    clock.advance(600)
    resumed = make_swap(tmp_path, panel, clock, old=LEGACY, rollback_version=swap.UNVERSIONED,
                       rollback_installed_sha256=ORIGINAL_PIN, rollback_sha256=UPLOAD_PIN)
    assert resumed.run() == swap.EXIT_OK
    assert panel.sequence().count(('DELETE', None)) == 1
    assert last_entry(tmp_path)['resumed_from']


def test_rollback_now_from_candidate_restores_only_the_pinned_legacy_upload(tmp_path):
    tool, panel, _clock = legacy_swap(tmp_path, rollback_now=True)
    panel.stored['moon'] = minify(NEW)
    assert tool.run() == swap.EXIT_ROLLED_BACK
    assert ('PUT', '2.1') not in panel.sequence()
    assert panel.stored['moon'] == minify(LEGACY)


def test_restored_minified_source_is_recognized_without_reinstalling_it(tmp_path):
    tool, panel, _clock = legacy_swap(tmp_path, rollback_now=True)
    panel.stored['moon'] = minify(LEGACY)
    assert tool.run() == swap.EXIT_ABORTED
    assert panel.writes == []
    assert 'nothing to roll back' in last_entry(tmp_path)['error']


def test_interrupted_rollback_recognizes_the_restored_upload_and_only_verifies_it(tmp_path):
    tool, panel, clock = legacy_swap(tmp_path)
    panel.boot_errors[('moon', '2.1')] = 'attribute_error: nil'
    put = panel.put
    interrupted = []
    def stop_after_store(*args, **kwargs):
        result = put(*args, **kwargs)
        if panel.stored.get('moon') == minify(LEGACY) and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return result
    panel.put = stop_after_store
    assert tool.run() == swap.EXIT_INTERRUPTED and interrupted
    panel.put = put
    before = len(panel.writes)
    clock.advance(600)
    resumed = make_swap(tmp_path, panel, clock, old=LEGACY, rollback_version=swap.UNVERSIONED,
                       rollback_installed_sha256=ORIGINAL_PIN, rollback_sha256=UPLOAD_PIN)
    assert resumed.run() == swap.EXIT_ROLLED_BACK
    assert panel.sequence()[before:] == [('POST', None)]
    assert last_entry(tmp_path)['installed_identity']['sha256'] == UPLOAD_PIN


def test_checkout_newline_conversion_does_not_change_the_resume_identity_contract(tmp_path):
    tool, panel, clock = legacy_swap(tmp_path)
    Path(tool.opts.rollback_path).write_bytes(LEGACY.encode())
    post = panel.post
    def stop(*args, **kwargs):
        post(*args, **kwargs)
        raise KeyboardInterrupt
    panel.post = stop
    assert tool.run() == swap.EXIT_INTERRUPTED
    panel.post = post
    clock.advance(600)
    resumed = make_swap(tmp_path, panel, clock, old=LEGACY, rollback_version=swap.UNVERSIONED,
                       rollback_installed_sha256=ORIGINAL_PIN, rollback_sha256=UPLOAD_PIN)
    Path(resumed.opts.rollback_path).write_bytes(LEGACY.replace('\n', '\r\n').encode())
    assert resumed.run() == swap.EXIT_OK
    assert last_entry(tmp_path)['resumed_from']


def test_changed_original_pin_cannot_resume_an_absent_target_from_another_contract(tmp_path):
    tool, panel, clock = legacy_swap(tmp_path)
    crlf = LEGACY.replace('\n', '\r\n').encode()
    Path(tool.opts.rollback_path).write_bytes(crlf)
    post = panel.post
    def stop(*args, **kwargs):
        post(*args, **kwargs)
        raise KeyboardInterrupt
    panel.post = stop
    assert tool.run() == swap.EXIT_INTERRUPTED
    panel.post = post
    clock.advance(600)
    before = len(panel.writes)
    resumed = make_swap(tmp_path, panel, clock, old=LEGACY, rollback_version=swap.UNVERSIONED,
                       rollback_installed_sha256=sha256(crlf).hexdigest(), rollback_sha256=UPLOAD_PIN)
    Path(resumed.opts.rollback_path).write_bytes(crlf)
    assert resumed.run() == swap.EXIT_ABORTED
    assert len(panel.writes) == before
    assert 'no unfinished run' in last_entry(tmp_path)['error']


def test_recovery_command_binds_full_pins_endpoint_and_report_even_with_partial_programmatic_argv(tmp_path):
    tool, panel, _clock = legacy_swap(tmp_path)
    tool.new = swap.prepare_source(tool.opts.new_path, '2.1', 'new')
    tool.old = swap.prepare_source(tool.opts.rollback_path, swap.UNVERSIONED, 'rollback',
                                   installed_sha256=ORIGINAL_PIN, upload_pin=UPLOAD_PIN)
    command = tool._rollback_command()
    args = tool.entry['recovery_argv']
    assert args[args.index('--rollback-installed-sha256') + 1] == ORIGINAL_PIN
    assert args[args.index('--rollback-sha256') + 1] == UPLOAD_PIN
    assert args[args.index('--panel') + 1] == BASE
    assert args[args.index('--report') + 1] == str(tool.opts.report_path)
    assert '--rollback-now' in args and '--rollback-installed-sha256' in command
    options, _log = swap.options_from_args(swap.build_parser().parse_args(args), args)
    assert options.rollback_installed_sha256 == ORIGINAL_PIN and options.rollback_now


def test_boot_loop_retry_never_reuses_a_cached_origin_as_source_proof(tmp_path):
    tool, panel, clock = legacy_swap(tmp_path)
    tool.new = swap.prepare_source(tool.opts.new_path, '2.1', 'new')
    tool.old = swap.prepare_source(tool.opts.rollback_path, swap.UNVERSIONED, 'rollback',
                                   installed_sha256=ORIGINAL_PIN, upload_pin=UPLOAD_PIN)
    deleted = []
    def refuse(*args, **kwargs):
        deleted.append(True)
        panel.stored['moon'] += '\n# raced after refused delete\n'
        return FakeResponse(503, body={})
    panel.delete = refuse
    with pytest.raises(swap.SwapError, match='unexpected SHA'):
        tool._delete_during_boot_loop()
    assert len(deleted) == 1 and tool.writes == 1


@pytest.mark.parametrize('fidelity', [False, True])
def test_new_version_label_without_matching_bytes_is_not_already_installed_or_checkable(tmp_path, fidelity):
    tool, panel, _clock = legacy_swap(tmp_path, check_only=True, fidelity=fidelity)
    panel.stored['moon'] = minify(NEW) + '\n# wrong bytes, same metadata\n'
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []


def test_low_memory_readback_failure_cannot_be_waived_by_pins_or_no_fidelity(tmp_path):
    tool, panel, _clock = legacy_swap(tmp_path, fidelity=False)
    panel.block_with = panel.block = 20000
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []
    assert 'Cannot prove source identity' in last_entry(tmp_path)['error']


def test_source_identity_is_refreshed_inside_the_gate_before_dispatch(tmp_path):
    tool, panel, _clock = legacy_swap(tmp_path)
    get = panel.get
    depths = []
    def read(url, **kwargs):
        if url.endswith('/apps/script/moon'):
            depths.append(getattr(panel_gate._local, 'depth', 0))
        return get(url, **kwargs)
    panel.get = read
    assert tool.run() == swap.EXIT_OK
    assert 0 in depths and 1 in depths  # preflight, then fresh proof inside dispatch lock


def test_real_ocean_reference_and_candidate_complete_the_fake_panel_pipeline(tmp_path):
    root = Path(__file__).parent
    original = (root / 'ocean.ax').read_text(encoding='utf-8')
    assert minify(original).rstrip('\n') == original.rstrip('\n')
    candidate = root / 'ocean-v3.ax'
    clock = FakeClock()
    panel = FakePanel(clock, block_with=50000, block_without=75000)
    panel.stored['ocean'] = original
    panel._boot_log()
    panel._restore()
    tool = make_swap(tmp_path, panel, clock, target='ocean', new_path=candidate, expect_version='3.0',
                     rollback_path=root / 'ocean.ax', rollback_version=swap.UNVERSIONED,
                     rollback_installed_sha256=sha256(original.encode()).hexdigest(),
                     rollback_sha256=sha256(minify(original).encode()).hexdigest(), fidelity=False)
    assert tool.run() == swap.EXIT_OK
    assert panel.stored['ocean'] == minify(candidate.read_text(encoding='utf-8'))
    assert sha256((root / 'ocean.ax').read_text(encoding='utf-8').encode()).hexdigest() == '5000462ec6ea5f02a831331bd71e77c81e244c50a271afc33bca6de9adc50b7f'
    assert all(name in panel.stored for name in ('moon', 'nightkeeper', 'nowplaying'))


def test_darkness_is_rechecked_after_pacing_and_source_proof(tmp_path):
    tool, panel, _clock = legacy_swap(tmp_path, require_dark=True)
    wait = tool.client._wait_for_mutation_slot
    def lights_on():
        result = wait()
        panel.power = True
        return result
    tool.client._wait_for_mutation_slot = lights_on
    assert tool.run() == swap.EXIT_ABORTED and panel.writes == []


def test_put_block_is_rechecked_inside_the_gate_before_dispatch(tmp_path):
    tool, panel, _clock = legacy_swap(tmp_path)
    wait = tool.client._wait_for_mutation_slot
    failed = []
    def decay():
        result = wait()
        if panel.boots >= 1 and 'moon' not in panel.stored and not failed:
            panel.block = 100
            failed.append(True)
        return result
    tool.client._wait_for_mutation_slot = decay
    code = tool.run()
    assert failed and code == swap.EXIT_ROLLED_BACK
    assert ('PUT', '2.1') not in panel.sequence()
    assert panel.stored['moon'] == minify(LEGACY)


def test_known_original_reappearing_before_heap_reboot_is_not_treated_as_absent(tmp_path):
    tool, panel, _clock = legacy_swap(tmp_path)
    wait = tool.client._wait_for_mutation_slot
    changed = []
    def restore():
        result = wait()
        if 'moon' not in panel.stored and not changed:
            panel.stored['moon'] = LEGACY
            panel.errors['moon'] = None
            changed.append(True)
        return result
    tool.client._wait_for_mutation_slot = restore
    assert tool.run() == swap.EXIT_ROLLED_BACK
    assert panel.sequence() == [('DELETE', None), ('POST', None)]  # only the later rollback verification reboot
    assert panel.stored['moon'] == LEGACY


def test_failed_restore_recipe_round_trips_quoted_paths_and_restores_only_the_pinned_old_body(tmp_path):
    import logging
    import os
    import shlex
    directory = tmp_path / "with spaces ' quoted"
    directory.mkdir()
    tool, panel, clock = legacy_swap(directory)
    old = swap.prepare_source(tool.opts.rollback_path, swap.UNVERSIONED, 'rollback',
                              installed_sha256=ORIGINAL_PIN, upload_pin=UPLOAD_PIN)
    panel.block_without = old.rollback_block - 1
    assert tool.run() == swap.EXIT_FAILED
    entry = last_entry(directory)
    argv = entry['recovery_argv']
    assert entry['next'].find('--rollback-installed-sha256') >= 0
    command = tool._rollback_command()
    if os.name == 'nt':
        assert "spaces '' quoted" in command
    else:
        assert shlex.split(command)[1:] == argv
    args = swap.build_parser().parse_args(argv)
    options, _log = swap.options_from_args(args, argv)
    panel.block_without = 52000
    clock.advance(600)
    before = len(panel.writes)
    resumed = swap.ScriptSwap(options, session=panel, clock=clock, hub_maintenance=swap.HubMaintenance(),
                              logger=logging.getLogger('test.versionless.recovery'))
    assert resumed.run() == swap.EXIT_ROLLED_BACK
    assert ('PUT', '2.1') not in panel.sequence()[before:]
    assert panel.stored['moon'] == minify(LEGACY)


def test_mutation_precondition_runs_under_gate_and_is_not_forwarded_to_http(panel_gate_dir):
    client, session, _clock = make_client()
    observed = []
    def guard(record):
        observed.append((record.gate, getattr(panel_gate._local, 'depth', 0), session.put.call_count))
    client._mutate('put', '/api/v1/apps/pushed/demo', operation='guarded test', json={'text': 'ok'}, before_send=guard)
    assert observed == [('ok', 1, 0)]
    assert 'before_send' not in session.put.call_args.kwargs


@pytest.mark.parametrize('error_class', [ValueError, requests.ConnectionError])
def test_rejected_precondition_is_journaled_without_http_or_static_failure(panel_gate_dir, error_class):
    client, session, _clock = make_client()
    def reject(_record):
        raise error_class('precondition failed')
    with pytest.raises(error_class):
        client.reboot(before_send=reject)
    session.post.assert_not_called()
    assert client.consecutive_mutation_failures == 0
    event = json.loads((panel_gate_dir / panel_gate.EVENTS_NAME).read_text().splitlines()[-1])
    assert event['err'] == error_class.__name__ and event['status'] is None

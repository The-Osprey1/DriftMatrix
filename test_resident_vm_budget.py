"""Shared VM acceptance and recovery against the real swap state machine."""
from hashlib import sha256
import re

import pytest

import script_swap as ss
from test_script_swap import (FakeClock, FakePanel, FakeResponse, NEW, OLD,
                              header_version, last_entry, make_swap)
from test_versionless_script_swap import LEGACY, legacy_swap
from weather.minify_ax import minify


class MemoryPanel(FakePanel):
    """Expose distinct measured totals for old, target-absent and new boots."""
    def __init__(self, clock, *, old_total=81_652, without_total=55_291,
                 new_total=73_728, budget=98_304, moon=OLD):
        self.old_total, self.without_total, self.new_total = old_total, without_total, new_total
        self.budget = budget
        self.new_budget = budget
        self.new_log_change = None
        super().__init__(clock, moon=moon)

    def device(self):
        result = super().device()
        result['scriptHeapBudgetBytes'] = self.new_budget if self.moon_version() == '2.1' else self.budget
        return result

    def _restore(self):
        super()._restore()
        target = self.stored.get('moon')
        total = self.without_total if target is None else self.new_total if header_version(target) == '2.1' else self.old_total
        positions = [i for i, line in enumerate(self.log) if ss._SHARED.search(line)]
        if positions:
            i = positions[-1]
            self.log[i] = re.sub(r'\(shared \d+\)', f'(shared {total})', self.log[i])
        if target is not None and header_version(target) == '2.1' and self.new_log_change:
            self.log = self.new_log_change(self.log)


def view(panel, logs=None):
    return ss.vm_budget_view(panel.device(), panel.apps(), {'lines': panel.log} if logs is None else logs)


@pytest.mark.parametrize('total,ready', [(73_727, True), (73_728, True), (73_729, False), (81_652, False), (98_305, False)])
def test_measured_shared_total_includes_overhead_and_enforces_integer_boundary(total, ready):
    panel = MemoryPanel(FakeClock(), old_total=total)
    result = view(panel)
    assert result['adoption_ready'] is ready
    assert result['shared_bytes'] == total and result['ceiling_bytes'] == 73_728
    assert result['complete_boot']
    assert result['status'] == ('within-limit' if ready else 'over-limit')


@pytest.mark.parametrize('budget', [None, True, False, 0, -1, '98304', 98304.0, {}, []])
def test_invalid_budget_is_not_coerced_and_refuses_before_any_write(tmp_path, budget):
    panel = MemoryPanel(FakeClock(), budget=budget)
    result = view(panel)
    assert not result['adoption_ready'] and result['budget_bytes'] is None
    tool = make_swap(tmp_path, panel, panel.clock)
    assert tool.run() == ss.EXIT_ABORTED and not panel.writes
    assert 'scriptHeapBudgetBytes' in last_entry(tmp_path)['error']


@pytest.mark.parametrize('damage', [
    lambda lines: [line for line in lines if 'boot:' not in line],
    lambda lines: [line for line in lines if 'scripts:' not in line],
    lambda lines: [line for line in lines if '[script:ocean]' not in line],
    lambda lines: [line.replace('scripts: 4 restored', 'scripts: 3 restored') for line in lines],
    lambda lines: [re.sub(r'\(shared \d+\)', '(shared unknown)', line) for line in lines],
    lambda lines: [re.sub(r'\(shared \d+\)', '(shared -1)', line) for line in lines],
    lambda lines: lines + [lines[-2]],  # A new compile after the restore is not a boot measurement.
])
def test_incomplete_or_install_time_measurements_cannot_prove_adoption(damage):
    panel = MemoryPanel(FakeClock(), old_total=60_000)
    result = view(panel, {'lines': damage(panel.log)})
    assert result['status'] == 'unverified' and not result['adoption_ready']


def test_old_boot_and_unrelated_shared_message_do_not_mask_current_total():
    panel = MemoryPanel(FakeClock(), old_total=81_652)
    logs = ['old boot: AWTRIX NG 1.1.2 on ESP32',
            'old [script:moon] vm heap +1 bytes (shared 1), free heap 1 -> 1, max block 1',
            'old scripts: 1 restored', *panel.log, '[script:moon] progress message (shared 100)']
    result = view(panel, {'lines': logs})
    assert result['shared_bytes'] == 81_652 and result['status'] == 'over-limit'


def test_reported_budget_controls_limit_without_rounding_up():
    panel = MemoryPanel(FakeClock(), old_total=75_001, budget=100_001)
    result = view(panel)
    assert result['ceiling_bytes'] == 75_000 and not result['adoption_ready']


def test_trial_can_reclaim_memory_from_an_over_ceiling_baseline(tmp_path):
    panel = MemoryPanel(FakeClock())
    tool = make_swap(tmp_path, panel, panel.clock, fidelity=False)
    assert tool.run() == ss.EXIT_OK
    entry = last_entry(tmp_path)
    assert entry['before']['vm_budget']['status'] == 'over-limit'
    assert entry['verifications'][-1]['vm_budget']['adoption_ready']
    assert entry['verifications'][-1]['vm_budget']['shared_bytes'] == 73_728
    assert panel.sequence() == [('DELETE', None), ('POST', None), ('PUT', '2.1'), ('POST', None)]


@pytest.mark.parametrize('fidelity', [True, False])
def test_over_ceiling_new_boot_rolls_back_even_when_fidelity_is_disabled(tmp_path, fidelity):
    panel = MemoryPanel(FakeClock(), new_total=73_729)
    assert make_swap(tmp_path, panel, panel.clock, fidelity=fidelity).run() == ss.EXIT_ROLLED_BACK
    entry = last_entry(tmp_path)
    new, old = entry['verifications']
    assert new['memory_policy'] == 'adoption' and new['vm_budget']['status'] == 'over-limit'
    assert '75% ceiling' in entry['rollback_reason']
    assert old['memory_policy'] == 'recovery' and old['vm_budget']['status'] == 'over-limit'
    assert not old['vm_budget']['adoption_ready'] and not old['problems']
    assert panel.stored['moon'] == minify(OLD)
    assert panel.sequence()[-4:] == [('DELETE', None), ('POST', None), ('PUT', '2.0'), ('POST', None)]


def test_remaining_residents_over_ceiling_prevent_new_put_but_allow_recovery(tmp_path):
    panel = MemoryPanel(FakeClock(), without_total=73_729)
    assert make_swap(tmp_path, panel, panel.clock).run() == ss.EXIT_ROLLED_BACK
    assert [v for method, _path, v, _time in panel.writes if method == 'PUT'] == ['2.0']
    assert 'before PUT' in last_entry(tmp_path)['rollback_reason']


@pytest.mark.parametrize('damage', [
    lambda lines: [line for line in lines if '[script:ocean]' not in line],
    lambda lines: [line for line in lines if 'boot:' not in line],
    lambda lines: [re.sub(r'\(shared \d+\)', '(shared invalid)', line) for line in lines],
])
def test_missing_new_boot_proof_restores_previous_source(tmp_path, damage):
    panel = MemoryPanel(FakeClock())
    panel.new_log_change = damage
    assert make_swap(tmp_path, panel, panel.clock, fidelity=False).run() == ss.EXIT_ROLLED_BACK
    entry = last_entry(tmp_path)
    assert entry['verifications'][0]['vm_budget']['status'] == 'unverified'
    assert panel.moon_version() == '2.0'


@pytest.mark.parametrize('budget', [None, 196_608, 65_536])
def test_new_boot_cannot_change_reported_budget_to_evade_the_limit(tmp_path, budget):
    panel = MemoryPanel(FakeClock(), new_total=80_000)
    panel.new_budget = budget
    assert make_swap(tmp_path, panel, panel.clock, fidelity=False).run() == ss.EXIT_ROLLED_BACK
    assert 'budget' in last_entry(tmp_path)['rollback_reason']
    assert panel.moon_version() == '2.0'


@pytest.mark.parametrize('check_only', [True, False])
@pytest.mark.parametrize('total', [73_728, 73_729])
def test_read_only_and_already_installed_paths_check_memory(tmp_path, check_only, total):
    panel = MemoryPanel(FakeClock(), moon=NEW, new_total=total)
    result = make_swap(tmp_path, panel, panel.clock, check_only=check_only, fidelity=False).run()
    assert result == (ss.EXIT_OK if total == 73_728 else ss.EXIT_CHECK_FAILED)
    assert not panel.writes
    entry = last_entry(tmp_path)
    memory = entry['check']['vm_budget'] if check_only else entry['current_vm_budget']
    assert memory['adoption_ready'] is (total == 73_728)


def test_current_memory_read_is_rejected_if_clock_reboots_mid_evidence(tmp_path):
    panel = MemoryPanel(FakeClock(), moon=NEW)
    original = panel.get
    device_reads = 0

    def get(url, **kwargs):
        nonlocal device_reads
        response = original(url, **kwargs)
        if url.endswith('/api/v1/device'):
            device_reads += 1
            if device_reads >= 3:  # Preflight, bracket start, bracket end.
                body = response.json()
                body['uptimeSeconds'] = 1
                return FakeResponse(body=body)
        return response

    panel.get = get
    assert make_swap(tmp_path, panel, panel.clock, fidelity=False).run() == ss.EXIT_CHECK_FAILED
    assert 'rebooted' in last_entry(tmp_path)['error'] and not panel.writes


def test_interrupted_verification_resumes_with_memory_guard(tmp_path):
    panel = MemoryPanel(FakeClock(), new_total=80_000)
    first = make_swap(tmp_path, panel, panel.clock, fidelity=False)
    original = first._reboot_and_settle

    def interrupt(purpose, *args, **kwargs):
        if purpose.startswith('verify'):
            raise KeyboardInterrupt
        return original(purpose, *args, **kwargs)

    first._reboot_and_settle = interrupt
    assert first.run() == ss.EXIT_INTERRUPTED and panel.moon_version() == '2.1'
    assert make_swap(tmp_path, panel, panel.clock, fidelity=False).run() == ss.EXIT_ROLLED_BACK
    assert panel.moon_version() == '2.0'
    assert '75% ceiling' in last_entry(tmp_path)['rollback_reason']


def test_versionless_exact_identity_and_budget_recovery_are_both_preserved(tmp_path, panel_gate_dir):
    panel = MemoryPanel(FakeClock(), moon=LEGACY, new_total=80_000)
    tool, panel, _clock = legacy_swap(tmp_path, panel=panel, clock=panel.clock, fidelity=False)
    assert tool.run() == ss.EXIT_ROLLED_BACK
    entry = last_entry(tmp_path)
    assert entry['installed_identity']['sha256'] == sha256(minify(LEGACY).encode()).hexdigest()
    assert not entry['verifications'][-1]['vm_budget']['adoption_ready']
    assert panel.stored['moon'] == minify(LEGACY)

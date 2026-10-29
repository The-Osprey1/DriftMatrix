"""A sharing violation is retryable; permanent denial must still time out."""
from types import SimpleNamespace

import pytest

import test_real_host_fleet as fleet


def clock(monkeypatch):
    state = SimpleNamespace(now=0.0)
    def sleep(seconds):
        state.now += seconds
    monkeypatch.setattr(fleet, 'time', SimpleNamespace(monotonic=lambda: state.now, sleep=sleep))
    return state


def test_polling_observes_receipt_after_a_transient_windows_read_denial(monkeypatch):
    state = clock(monkeypatch)
    attempts = []
    receipt = {'consumed_command_ids': ['receipt']}
    def read():
        attempts.append(state.now)
        if len(attempts) == 1:
            raise PermissionError('sharing violation')
        return receipt
    result = fleet.wait_for(read, SimpleNamespace(poll=lambda: None), lambda: 'state', timeout=1)
    assert result is receipt and attempts == [0.0, 0.05]


def test_polling_does_not_hide_permanent_read_denial_or_extend_its_deadline(monkeypatch):
    state = clock(monkeypatch)
    attempts = []
    def denied():
        attempts.append(state.now)
        raise PermissionError('permanent denial')
    with pytest.raises(pytest.fail.Exception, match='timed out: denied'):
        fleet.wait_for(denied, SimpleNamespace(poll=lambda: None), lambda: 'denied', timeout=0.11)
    assert attempts == [0.0, 0.05, 0.1]
    assert 0.11 <= state.now <= 0.16

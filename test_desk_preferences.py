import json
from dataclasses import replace
import pytest
import desk_preferences as prefs


def test_preferences_round_trip_and_invalid_save_preserves_good_file(tmp_path):
    path = tmp_path / 'prefs.json'
    chosen = prefs.Preferences('WATER', 30, True)
    prefs.save(path, chosen)
    assert prefs.load(path) == chosen
    for invalid in [replace(chosen, minutes=True), replace(chosen, minutes=0), replace(chosen, label='TOO LONG'), replace(chosen, keep_on_top='yes')]:
        with pytest.raises(ValueError):
            prefs.save(path, invalid)
        assert prefs.load(path) == chosen


def test_unreadable_preferences_use_defaults_without_rewriting_file(tmp_path):
    path = tmp_path / 'prefs.json'
    assert prefs.load(path) == prefs.Preferences()
    assert not path.exists()
    for body in [b'[]', b'null', b'\xff', b'x'*4097, b'{"schema_version": true}', b'{}']:
        path.write_bytes(body)
        assert prefs.load(path) == prefs.Preferences()
        assert path.read_bytes() == body

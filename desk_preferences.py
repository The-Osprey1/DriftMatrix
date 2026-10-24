"""Bounded, local-only desktop preferences; never changes the running Hub."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from desk_reminder import atomic_json, validate_label

PREFERENCES_NAME = "desk_preferences.json"


@dataclass(frozen=True)
class Preferences:
    label: str = "TEA"
    minutes: int = 5
    keep_on_top: bool = False


def validate(value: dict) -> Preferences:
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('Unsupported preferences')
    label = validate_label(value.get('label'))
    minutes = value.get('minutes')
    keep_on_top = value.get('keep_on_top')
    if type(minutes) is not int or not 1 <= minutes <= 1440 or type(keep_on_top) is not bool:
        raise ValueError('Invalid preferences')
    return Preferences(label, minutes, keep_on_top)


def load(path: Path) -> Preferences:
    try:
        with path.open('rb') as handle:
            body = handle.read(4097)
        if len(body) > 4096:
            return Preferences()
        return validate(json.loads(body))
    except (OSError, ValueError, UnicodeError):
        return Preferences()


def save(path: Path, preferences: Preferences) -> None:
    value = {'schema_version': 1, **asdict(preferences)}
    validate(value)
    atomic_json(path, value)

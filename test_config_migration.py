"""Migration provenance, conflicts, privacy, and filesystem publication races."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path

import pytest

import config_loader as loader
import config_migration as migration
import driftmatrix

ROOT = Path(__file__).parent


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8-sig")
    return path


@pytest.fixture
def inputs(tmp_path):
    hub = write(tmp_path / "hub.json", json.loads((ROOT / "utility_hub.example.json").read_text()))
    ecosystem = write(tmp_path / "ecosystem.json", json.loads((ROOT / "drift_ecosystem.json").read_text()))
    return hub, ecosystem


def preview(inputs, tmp_path, **kwargs):
    hub, ecosystem = inputs
    return migration.migrate(hub_path=hub, ecosystem_path=ecosystem,
                             output_path=tmp_path / "candidate.json", **kwargs)


def test_example_migration_preserves_hub_semantics_without_enabling_workers(inputs, tmp_path):
    import utility_hub
    before = {path: path.read_bytes() for path in inputs}
    result = preview(inputs, tmp_path)
    assert result.report["status"] == "ready"
    assert not result.report["candidate_written"]
    config = loader.config_from_mapping(result.candidate, source_path=tmp_path / "candidate.json")
    assert not any(service.enabled for service in config.services.values())
    old = utility_hub.load_config(inputs[0])
    reconstructed = deepcopy(result.candidate["services"]["hub"]["options"])
    reconstructed.update(panel_url=config.panel.url, quiet_hours=dict(config.panel.quiet_hours))
    reconstructed.setdefault("next_up", {})["timezone"] = config.panel.timezone
    assert utility_hub.config_from_mapping(reconstructed) == old
    assert all(path.read_bytes() == body for path, body in before.items())
    assert not (tmp_path / "candidate.json").exists()


def test_every_candidate_leaf_has_provenance_and_shared_endpoint_has_both_sources(inputs, tmp_path):
    result = preview(inputs, tmp_path)
    fields = {item["field"] for item in result.report["provenance"]}
    assert fields == {loader.safe_path(path) for path in migration._leaves(result.candidate)}
    endpoint = next(item for item in result.report["provenance"] if item["field"] == "panel.url")
    assert {source["source"] for source in endpoint["sources"]} == {"hub", "ecosystem"}


def test_same_endpoint_spelling_is_normalized(inputs, tmp_path):
    hub = loader.read_json(inputs[0])
    hub["panel_url"] = "HTTP://192.168.1.90:80/"
    write(inputs[0], hub)
    assert preview(inputs, tmp_path).report["status"] == "ready"


@pytest.mark.parametrize("field,value", [("panel_url", "http://private.test/secret"),
    ("timezone", "UTC"), ("quiet_hours", {"start": "22:00", "end": "07:00"})])
def test_conflicts_produce_no_candidate_and_no_private_values(inputs, tmp_path, field, value):
    inventory = write(tmp_path / "senders.json", {"pc_stats": {field: value}})
    result = preview(inputs, tmp_path, senders_path=inventory)
    assert result.candidate is None
    assert result.report["status"] == "conflicts"
    assert "conflicting_shared_setting" in {item["code"] for item in result.report["conflicts"]}
    assert "private.test" not in json.dumps(result.report)
    assert "secret" not in json.dumps(result.report)


def test_private_tokens_and_aliases_stay_in_candidate_only(inputs, tmp_path):
    hub = loader.read_json(inputs[0])
    hub["github"] = {"token": "private-token"}
    hub["next_up"]["sources"] = ["https://calendar.private/secret-token"]
    hub["next_up"]["aliases"] = {"private-appointment": "CALL"}
    write(inputs[0], hub)
    result = preview(inputs, tmp_path)
    assert result.candidate["services"]["hub"]["options"]["github"]["token"] == "private-token"
    report = json.dumps(result.report)
    for secret in ("private-token", "private-appointment", "calendar.private", "secret-token"):
        assert secret not in report
        assert secret not in repr(result)


@pytest.mark.parametrize("change", ["root_unknown", "option_unknown", "device_unknown", "bad_enable", "empty_rotation", "version"])
def test_unsupported_or_invalid_settings_block_output(inputs, tmp_path, change):
    hub = loader.read_json(inputs[0])
    eco = loader.read_json(inputs[1])
    if change == "root_unknown":
        hub["private-secret-key"] = True
    elif change == "option_unknown":
        hub["focus_sprint"]["unknown"] = True
    elif change == "device_unknown":
        eco["ecosystem"]["devices"]["watch"]["unknown"] = True
    elif change == "bad_enable":
        eco["ecosystem"]["devices"]["matrix"]["enabled"] = "false"
    elif change == "empty_rotation":
        eco["ecosystem"]["devices"]["matrix"]["rotation_apps"] = []
    else:
        eco["version"] = "2.0"
    write(inputs[0], hub)
    write(inputs[1], eco)
    result = preview(inputs, tmp_path)
    assert result.candidate is None
    assert "private-secret-key" not in json.dumps(result.report)


def test_reserved_metadata_is_reported_not_silently_activated(inputs, tmp_path):
    eco = loader.read_json(inputs[1])
    eco["ecosystem"]["devices"]["watch"].update(haptic_alerts=True, udp_discovery_port=4210)
    write(inputs[1], eco)
    result = preview(inputs, tmp_path)
    assert result.report["status"] == "ready"
    assert sum(item["code"] == "reserved_metadata_omitted" for item in result.report["warnings"]) == 2
    assert "haptic_alerts" not in result.candidate["services"]["drift_link"]["options"]["devices"]["watch"]


def test_relative_legacy_calendar_requires_working_directory(inputs, tmp_path):
    hub = loader.read_json(inputs[0])
    hub["next_up"]["sources"] = ["calendar.ics"]
    write(inputs[0], hub)
    result = preview(inputs, tmp_path)
    assert result.candidate is None
    assert result.report["conflicts"][0]["code"] == "ambiguous_relative_path"
    resolved = preview(inputs, tmp_path, legacy_working_dir=tmp_path / "old-working-directory")
    assert resolved.candidate["services"]["hub"]["options"]["next_up"]["sources"] == [
        str(tmp_path / "old-working-directory" / "calendar.ics")]


def test_explicit_sender_inventory_and_relative_media_path(inputs, tmp_path):
    inventory = write(tmp_path / "senders.json", {
        "weather": {"enabled": True, "options": {"latitude": 1, "longitude": 2}},
        "media": {"enabled": False, "options": {"script_path": "media.ax", "bridge_port": 8766}},
        "pc_stats": {"options": {"interval_seconds": 10, "refresh_delta_points": 4}},
    })
    result = preview(inputs, tmp_path, senders_path=inventory)
    assert result.report["status"] == "ready"
    assert result.candidate["services"]["weather"]["enabled"] is True
    assert result.candidate["services"]["media"]["options"]["script_path"] == str(tmp_path / "media.ax")
    assert result.candidate["services"]["pc_stats"]["options"]["interval_seconds"] == 10


def test_missing_panel_url_never_guesses_a_live_device(tmp_path):
    source = write(tmp_path / "empty.json", {})
    result = migration.migrate(hub_path=source, output_path=tmp_path / "out.json")
    assert result.candidate is None
    assert result.report["conflicts"][0]["code"] == "missing_panel_url"


def test_no_selected_input_is_rejected(tmp_path):
    with pytest.raises(loader.ConfigError):
        migration.migrate(output_path=tmp_path / "out.json")


def test_cli_preview_and_validation_are_read_only(inputs, tmp_path, capsys):
    before = {item.name for item in tmp_path.iterdir()}
    assert driftmatrix.main(["migrate", "--from-hub", str(inputs[0]), "--from-ecosystem", str(inputs[1])]) == 0
    assert json.loads(capsys.readouterr().out)["candidate_written"] is False
    assert {item.name for item in tmp_path.iterdir()} == before
    assert driftmatrix.main(["validate", "--config", str(ROOT / "driftmatrix_config" / "driftmatrix.example.json")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "valid"


def test_cli_publishes_candidate_and_redacted_report_without_touching_inputs(inputs, tmp_path, capsys):
    output, report = tmp_path / "new.json", tmp_path / "report.json"
    originals = [path.read_bytes() for path in inputs]
    arguments = ["migrate", "--from-hub", str(inputs[0]), "--from-ecosystem", str(inputs[1]),
                 "--output", str(output), "--report", str(report)]
    assert driftmatrix.main(arguments) == 0
    assert loader.load_config(output).schema_version == 1
    assert loader.read_json(report)["candidate_written"] is True
    assert json.loads(capsys.readouterr().out)["candidate_written"] is True
    saved = output.read_bytes()
    assert driftmatrix.main(arguments) == 2
    assert output.read_bytes() == saved
    assert [path.read_bytes() for path in inputs] == originals
    assert not list(tmp_path.glob(".driftmatrix-*.tmp"))


def test_conflicting_cli_does_not_write_candidate(inputs, tmp_path, capsys):
    hub = loader.read_json(inputs[0])
    hub["panel_url"] = "http://different.private"
    write(inputs[0], hub)
    out = tmp_path / "candidate.json"
    assert driftmatrix.main(["migrate", "--from-hub", str(inputs[0]), "--from-ecosystem", str(inputs[1]),
                             "--output", str(out)]) == 2
    assert not out.exists()
    assert "different.private" not in capsys.readouterr().out


def test_input_alias_and_same_report_candidate_are_rejected(inputs, tmp_path, capsys):
    original = inputs[0].read_bytes()
    assert driftmatrix.main(["migrate", "--from-hub", str(inputs[0]), "--output", str(inputs[0])]) == 2
    assert inputs[0].read_bytes() == original
    out = tmp_path / "same.json"
    assert driftmatrix.main(["migrate", "--from-hub", str(inputs[0]), "--output", str(out), "--report", str(out)]) == 2
    assert not out.exists()


def test_missing_input_is_redacted_cli_error(tmp_path, capsys):
    assert driftmatrix.main(["migrate", "--from-hub", str(tmp_path / "secret-token.json")]) == 2
    error = capsys.readouterr().out
    assert "secret-token" not in error
    assert json.loads(error)["status"] == "error"


def test_publication_race_does_not_overwrite_another_writer(tmp_path, monkeypatch):
    target = tmp_path / "candidate.json"
    def competitor(_source, destination):
        Path(destination).write_text("competitor", encoding="utf-8")
        raise FileExistsError()
    monkeypatch.setattr(migration.os, "link", competitor)
    with pytest.raises(loader.ConfigError):
        migration.write_new_json(target, {"candidate": True})
    assert target.read_text() == "competitor"
    assert not list(tmp_path.glob(".driftmatrix-*.tmp"))


def test_two_writers_publish_exactly_one_complete_document(tmp_path):
    target = tmp_path / "new.json"
    def publish(number):
        try:
            migration.write_new_json(target, {"winner": number, "payload": "x" * 10000})
            return True
        except loader.ConfigError:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(publish, [1, 2])) == 1
    assert loader.read_json(target)["winner"] in {1, 2}
    assert len(loader.read_json(target)["payload"]) == 10000
    assert not list(tmp_path.glob(".driftmatrix-*.tmp"))


def test_failed_publication_cleans_temporary_file(tmp_path, monkeypatch):
    monkeypatch.setattr(migration.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError()))
    target = tmp_path / "new.json"
    with pytest.raises(loader.ConfigError):
        migration.write_new_json(target, {"token": "secret"})
    assert not target.exists()
    assert not list(tmp_path.glob(".driftmatrix-*.tmp"))


def test_partial_quiet_hours_default_has_honest_provenance(tmp_path):
    source = write(tmp_path / "hub.json", {"panel_url": "http://192.0.2.10", "quiet_hours": {"start": "22:00"}})
    result = migration.migrate(hub_path=source, output_path=tmp_path / "new.json")
    provenance = {item["field"]: item["sources"] for item in result.report["provenance"]}
    assert provenance["panel.quiet_hours.start"] == [{"source": "hub", "field": "quiet_hours.start"}]
    assert provenance["panel.quiet_hours.end"] == [{"source": "default", "field": "panel.quiet_hours.end"}]


def test_report_failure_after_candidate_creation_is_explicit(inputs, tmp_path, monkeypatch, capsys):
    out, report = tmp_path / "new.json", tmp_path / "report.json"
    original = driftmatrix.write_new_json
    def publish(path, value, **kwargs):
        if path == report:
            raise loader.ConfigError("Simulated report publication failure")
        return original(path, value, **kwargs)
    monkeypatch.setattr(driftmatrix, "write_new_json", publish)
    assert driftmatrix.main(["migrate", "--from-hub", str(inputs[0]),
                             "--output", str(out), "--report", str(report)]) == 2
    assert loader.load_config(out).schema_version == 1
    assert json.loads(capsys.readouterr().out)["candidate_written"] is True


def test_missing_output_parent_is_not_created(tmp_path):
    target = tmp_path / "absent" / "new.json"
    with pytest.raises(loader.ConfigError):
        migration.write_new_json(target, {"x": 1})
    assert not target.parent.exists()


@pytest.mark.parametrize("value", [[], None, True])
def test_invalid_legacy_input_structure_is_a_conflict(tmp_path, value):
    source = write(tmp_path / "hub.json", value)
    result = migration.migrate(hub_path=source, output_path=tmp_path / "new.json")
    assert result.candidate is None
    assert result.report["conflicts"][0]["code"] == "invalid_structure"


def test_existing_report_prevents_candidate_publication(inputs, tmp_path, capsys):
    report = write(tmp_path / "report.json", {"existing": True})
    out = tmp_path / "new.json"
    assert driftmatrix.main(["migrate", "--from-hub", str(inputs[0]), "--output", str(out), "--report", str(report)]) == 2
    assert not out.exists()
    assert loader.read_json(report) == {"existing": True}

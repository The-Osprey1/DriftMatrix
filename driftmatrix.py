"""DriftMatrix configuration and host-service lifecycle commands."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from config_loader import ConfigError, default_config_path, load_config
from config_migration import check_new_destination, migrate, write_new_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate configuration without starting services")
    validate.add_argument("--config", type=Path, default=None)
    migration = commands.add_parser("migrate", help="preview migration; write only explicitly requested new files")
    migration.add_argument("--from-hub", type=Path)
    migration.add_argument("--from-ecosystem", type=Path)
    migration.add_argument("--from-senders", type=Path, help="optional offline sender inventory JSON")
    migration.add_argument("--legacy-working-dir", type=Path, help="anchor for relative legacy calendar paths")
    migration.add_argument("--output", type=Path, help="new candidate file; existing files are never overwritten")
    migration.add_argument("--report", type=Path, help="optional new file for the redacted provenance report")
    controls = commands.add_parser('controls', help='open local focus, reminder and service controls')
    target = controls.add_mutually_exclusive_group()
    target.add_argument('--config', type=Path)
    target.add_argument('--data-dir', type=Path, help='explicit legacy local state folder')
    target.add_argument('--demo', action='store_true', help='use isolated temporary state')
    for name, help_text in (("run", "run the foreground process supervisor"),
                            ("status", "inspect local supervisor and process status"),
                            ("stop", "request cooperative supervisor shutdown")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    candidate_written = False
    try:
        if args.command == 'controls':
            from desk_controls import main as controls_main
            selected = ['--demo'] if args.demo else (['--config', str(args.config)] if args.config
                        else ['--data-dir', str(args.data_dir)] if args.data_dir else [])
            return controls_main(selected)
        if args.command == "validate":
            config = load_config(args.config)
            result = {"status": "valid", "schema_version": config.schema_version,
                      "config_hash": config.config_hash,
                      "services": {name: service.enabled for name, service in config.services.items()}}
            print(json.dumps(result, indent=2))
            return 0
        if args.command in {"run", "status", "stop"}:
            # Offline commands must not import process or worker runtime modules.
            from supervisor import Supervisor, request_stop, status
            config = load_config(args.config)
            if args.command == "run":
                return Supervisor(config).run()
            print(json.dumps(status(config) if args.command == "status" else request_stop(config), indent=2))
            return 0
        inputs = (args.from_hub, args.from_ecosystem, args.from_senders)
        destinations = [path for path in (args.output, args.report) if path is not None]
        if len({path.expanduser().resolve() for path in destinations}) != len(destinations):
            raise ConfigError("Candidate and report destinations must be different")
        for path in destinations:
            check_new_destination(path, inputs)
        result = migrate(hub_path=args.from_hub, ecosystem_path=args.from_ecosystem,
                         senders_path=args.from_senders, output_path=args.output or default_config_path(),
                         legacy_working_dir=args.legacy_working_dir)
        report = result.report
        if result.candidate is not None and args.output is not None:
            write_new_json(args.output, result.candidate, inputs=inputs)
            candidate_written = True
            report["candidate_written"] = True
        if args.report is not None:
            write_new_json(args.report, report, inputs=inputs)
        print(json.dumps(report, indent=2))
        return 2 if result.candidate is None else 0
    except ConfigError as error:
        print(json.dumps({"status": "error", "message": str(error), "candidate_written": candidate_written}))
        return 2
    except OSError as error:
        print(json.dumps({"status": "error", "message": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

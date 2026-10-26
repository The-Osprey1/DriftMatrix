"""Spawn-safe child entry point. Configuration and stop events travel over IPC."""
from __future__ import annotations

import importlib
import logging
from logging.handlers import RotatingFileHandler
import os
import threading

from config_loader import config_from_mapping
from runtime_paths import data_directory
from service_runtime import scope_directory, worker_notifications


def worker_main(document, source_path, service, module_name, stop_event, connection):
    finished = threading.Event()
    handlers = []
    code = 1

    def notify(message):
        try:
            connection.send(message)
        except (OSError, EOFError):
            stop_event.set()

    def watch_parent():
        while not finished.wait(0.1):
            try:
                if connection.poll(0.1):
                    connection.recv()
            except (OSError, EOFError):
                stop_event.set()
                # A dead parent cannot enforce its normal shutdown deadline.
                if not finished.wait(document["runtime"]["shutdown_timeout_seconds"]):
                    os._exit(1)
                return

    monitor = threading.Thread(target=watch_parent, name="supervisor-parent-watch", daemon=True)
    try:
        config = config_from_mapping(document, source_path=source_path)
        with data_directory(config.runtime.data_dir):
            folder = scope_directory(config.panel.url, config.runtime.data_dir) / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            settings = config.runtime.logging
            handler = RotatingFileHandler(folder / f"service_{service}.log", encoding="utf-8",
                                          maxBytes=settings["max_bytes"], backupCount=settings["backup_count"])
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            root = logging.getLogger()
            root.setLevel(settings["level"])
            root.addHandler(handler)
            handlers.append(handler)
            monitor.start()
            module = importlib.import_module(module_name)
            with worker_notifications(lambda: notify({"state": "RUNNING"})):
                code = module.run_worker(config, stop_event)
    except BaseException as error:
        # Do not serialize exception messages or configuration values into status.
        notify({"error": type(error).__name__})
        code = 1
    finally:
        finished.set()
        if monitor.ident is not None:
            monitor.join(1)
        connection.close()
        for handler in handlers:
            logging.getLogger().removeHandler(handler)
            handler.close()
    raise SystemExit(code)

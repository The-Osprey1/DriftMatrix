"""Simulated workers using real ownership and IPC, with no panel or feed access."""
import multiprocessing
import os
from pathlib import Path
import time
import psutil

from service_runtime import worker_adapter


def run_worker(config, stop_event):
    service = multiprocessing.current_process().name.removeprefix("driftmatrix-")
    @worker_adapter(service)
    def execute(config, stop_event):
        mode = config.services[service].options.get("refresh_confirm_samples", 1)
        marker = config.runtime.data_dir / f"test_starts_{service}.txt"
        previous = marker.read_text() if marker.exists() else ""
        with marker.open("a") as stream:
            import json
            stream.write(json.dumps({"pid": os.getpid(), "created": psutil.Process().create_time()}) + "\n")
        if mode == 2:
            return 7
        if mode == 3:
            raise ValueError("private-value-must-not-appear-in-status")
        if mode == 4:
            time.sleep(60)
        if mode == 5 and not previous:
            return 7
        if mode == 7:
            os._exit(9)
        stop_event.wait(60)
        return 0
    return execute(config, stop_event)

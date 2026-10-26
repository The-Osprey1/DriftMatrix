"""Process supervisor with scoped ownership, bounded retries, and local control."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import multiprocessing
import os
from pathlib import Path
import secrets
import socket
import threading
import time
import uuid

import psutil

from config_loader import ConfigError, SERVICE_NAMES
from service_runtime import ServiceAlreadyRunning, scope_directory, stop_signals, supervisor_ownership
from supervisor_worker import worker_main

WORKER_MODULES = {"hub": "utility_hub", "media": "now_playing_push", "pc_stats": "pc_stats_push",
                  "weather": "weather.weather_brief_push", "aqi": "weather.aqi_brief_push",
                  "drift_link": "drift_ecosystem"}
STATUS_FILE = "supervisor_status.json"
CONTROL_FILE = "supervisor_control.json"
TICK_SECONDS = 0.1
HEARTBEAT_SECONDS = 1.0


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    """Publish complete records; a failed update leaves the last record intact."""
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def read_record(path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            text = stream.read(262145)
        if len(text) > 262144:
            raise ValueError()
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError()
        json.dumps(value, allow_nan=False)
        return value
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ConfigError("Local supervisor record is missing or invalid") from None


def process_matches(pid, created):
    if type(pid) is not int or pid <= 0 or type(created) not in (int, float):
        return False
    try:
        process = psutil.Process(pid)
        return abs(process.create_time() - created) < 0.001 and process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, OSError):
        return False


def receive_json(connection):
    data = bytearray()
    while len(data) <= 4096:
        block = connection.recv(min(1024, 4097 - len(data)))
        if not block:
            break
        data.extend(block)
        if b"\n" in block:
            try:
                value = json.loads(bytes(data).split(b"\n", 1)[0])
            except RecursionError:
                raise ValueError("Invalid control request") from None
            if isinstance(value, dict):
                return value
            break
    raise ValueError("Invalid control request")


class ControlServer:
    """Loopback-only, authenticated stop requests; status never opens a socket."""
    def __init__(self, stop_event, instance):
        self.stop_event, self.instance = stop_event, instance
        self.token = secrets.token_hex(32)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(4)
        self.socket.settimeout(0.2)
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self.serve, name="supervisor-control", daemon=True)

    def start(self):
        self.thread.start()

    def serve(self):
        while not self.finished.is_set():
            try:
                client, _address = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                client.settimeout(0.5)
                try:
                    request = receive_json(client)
                    if (request.get("command") != "stop" or request.get("instance") != self.instance
                            or not isinstance(request.get("token"), str)
                            or not request["token"].isascii()
                            or not hmac.compare_digest(request["token"], self.token)):
                        continue
                    self.stop_event.set()
                    client.sendall(json.dumps({"accepted": True, "instance": self.instance}).encode() + b"\n")
                except (OSError, ValueError, UnicodeError):
                    continue

    def close(self):
        self.finished.set()
        self.socket.close()
        if self.thread.ident is not None:
            self.thread.join(1)


@dataclass
class Child:
    process: object
    stop_event: object
    connection: object
    created: float | None


@dataclass
class ServiceState:
    name: str
    state: str
    child: Child | None = None
    attempts: deque = field(default_factory=deque)
    restart_count: int = 0
    restart_at: float | None = None
    exit_code: int | None = None
    last_error: str | None = None
    circuit_open: bool = False
    escalated: bool = False
    pid: int | None = None
    created: float | None = None


def spawn_child(config, service):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    stop_event = context.Event()
    process = context.Process(target=worker_main, name=f"driftmatrix-{service}",
        args=(config.to_mapping(), str(config.source_path), service, WORKER_MODULES[service], stop_event, child))
    try:
        process.start()
        try:
            created = psutil.Process(process.pid).create_time()
        except psutil.NoSuchProcess:
            created = None
        return Child(process, stop_event, parent, created)
    except BaseException:
        parent.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
            process.close()
        raise
    finally:
        child.close()


class Supervisor:
    def __init__(self, config, stop_event=None, *, spawn=spawn_child, monotonic=time.monotonic):
        self.config, self.stop_event = config, stop_event or threading.Event()
        self.spawn, self.monotonic = spawn, monotonic
        self.services = {name: ServiceState(name, "STARTING" if config.services[name].enabled else "DISABLED")
                         for name in SERVICE_NAMES}
        self.instance = uuid.uuid4().hex
        self.pid = os.getpid()
        self.created = psutil.Process(self.pid).create_time()
        self.started_at = utc_now()
        self.started = monotonic()
        self.state = "STARTING"
        self.directory = scope_directory(config.panel.url, config.runtime.data_dir)
        self.logger = logging.getLogger(f"driftmatrix.supervisor.{self.instance}")
        self.logger.propagate = False

    def schedule_restart(self, service, now):
        policy = self.config.runtime.restart
        while service.attempts and service.attempts[0] <= now - policy.window_seconds:
            service.attempts.popleft()
        service.state = "FAILED"
        if service.last_error is None:
            service.last_error = {2: "UnsupportedWorker", 3: "OwnershipUnavailable"}.get(service.exit_code, "UnexpectedExit")
        if service.exit_code in (2, 3) or len(service.attempts) >= policy.max_attempts:
            service.circuit_open = True
            service.restart_at = None
        else:
            service.restart_at = now + min(60.0, 2.0 ** min(len(service.attempts), 6))

    def start_service(self, service, now, *, restart=False):
        if restart:
            service.attempts.append(now)
            service.restart_count += 1
        service.restart_at = None
        service.state = "STARTING"
        service.last_error = None
        service.exit_code = None
        try:
            child = self.spawn(self.config, service.name)
            service.child = child
            service.pid = child.process.pid
            service.created = child.created
            self.logger.info("service=%s state=STARTING pid=%s", service.name, service.pid)
        except Exception as error:
            service.exit_code = None
            service.last_error = type(error).__name__
            self.schedule_restart(service, now)

    def collect(self, service):
        child = service.child
        try:
            while child.connection.poll():
                message = child.connection.recv()
                if not isinstance(message, dict):
                    continue
                if message.get("state") == "RUNNING" and service.state == "STARTING":
                    service.state = "RUNNING"
                if isinstance(message.get("error"), str) and message["error"].isidentifier():
                    service.last_error = message["error"]
        except (OSError, EOFError):
            pass
        if child.process.is_alive():
            return False
        child.process.join(0)
        service.exit_code = child.process.exitcode
        child.connection.close()
        child.process.close()
        service.child = None
        self.logger.info("service=%s exit=%s", service.name, service.exit_code)
        return True

    def step(self):
        now = self.monotonic()
        for service in self.services.values():
            if self.stop_event.is_set():
                break
            if service.state == "DISABLED":
                continue
            if service.child is not None:
                if self.collect(service):
                    self.schedule_restart(service, now)
            elif not service.circuit_open and (service.restart_at is None or now >= service.restart_at):
                self.start_service(service, now, restart=service.restart_at is not None)

    def snapshot(self):
        now = self.monotonic()
        return {"version": 1, "instance": self.instance, "pid": self.pid, "process_created": self.created,
            "state": self.state, "started_at": self.started_at, "updated_at": utc_now(),
            "uptime_seconds": max(0, now - self.started), "config_hash": self.config.config_hash,
            "services": {name: {"state": item.state, "pid": item.pid, "process_created": item.created,
                "exit_code": item.exit_code, "last_error": item.last_error, "restart_count": item.restart_count,
                "circuit_open": item.circuit_open, "escalated": item.escalated,
                "retry_in_seconds": None if item.restart_at is None else max(0, item.restart_at - now)}
                for name, item in self.services.items()}}

    def publish(self):
        atomic_json(self.directory / STATUS_FILE, self.snapshot())

    def publish_shutdown(self):
        try:
            self.publish()
        except OSError as error:
            self.logger.error("status publication failed: %s", type(error).__name__)

    def shutdown(self):
        self.state = "STOPPING"
        for service in self.services.values():
            service.restart_at = None
            if service.child is not None:
                service.state = "STOPPING"
                service.child.stop_event.set()
            elif service.state not in {"DISABLED", "FAILED"}:
                service.state = "STOPPED"
        deadline = self.monotonic() + self.config.runtime.shutdown_timeout_seconds
        # All children share one deadline, rather than one full timeout per child.
        while any(item.child for item in self.services.values()) and self.monotonic() < deadline:
            for service in self.services.values():
                if service.child is not None and self.collect(service):
                    service.state = "STOPPED" if service.exit_code == 0 else "FAILED"
            self.publish_shutdown()
            time.sleep(TICK_SECONDS)
        remaining = [item for item in self.services.values() if item.child is not None]
        for service in remaining:
            if service.child.process.is_alive():
                service.escalated = True
                service.child.process.terminate()
        terminate_deadline = time.monotonic() + 1.0
        for service in remaining:
            service.child.process.join(max(0, terminate_deadline - time.monotonic()))
        for service in remaining:
            if service.child.process.is_alive():
                service.child.process.kill()
        kill_deadline = time.monotonic() + 2.0
        for service in remaining:
            service.child.process.join(max(0, kill_deadline - time.monotonic()))
            if not self.collect(service):
                service.last_error = "TerminationFailed"
                service.state = "FAILED"
                continue
            service.state = "FAILED" if service.escalated or service.exit_code != 0 else "STOPPED"
        self.state = "STOPPED"
        self.publish_shutdown()

    def run(self):
        if self.stop_event.is_set():
            return 0
        control = None
        handler = None
        try:
            with supervisor_ownership(self.config.panel.url, self.config.runtime.data_dir):
                reserved = {self.directory / name for name in (STATUS_FILE, CONTROL_FILE, "supervisor.lock", "supervisor.log")}
                reserved.update(self.directory / "logs" / f"service_{name}.log" for name in SERVICE_NAMES)
                if self.config.source_path in {path.resolve() for path in reserved}:
                    raise ConfigError("Configuration cannot use a reserved supervisor path")
                settings = self.config.runtime.logging
                handler = RotatingFileHandler(self.directory / "supervisor.log", encoding="utf-8",
                    maxBytes=settings["max_bytes"], backupCount=settings["backup_count"])
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                self.logger.addHandler(handler)
                self.logger.setLevel(settings["level"])
                control = ControlServer(self.stop_event, self.instance)
                control.start()
                with stop_signals(self.stop_event):
                    try:
                        self.publish()
                        atomic_json(self.directory / CONTROL_FILE, {"instance": self.instance, "pid": self.pid,
                            "process_created": self.created, "port": control.socket.getsockname()[1], "token": control.token})
                        self.state = "RUNNING"
                        last_publish = -float("inf")
                        while not self.stop_event.is_set():
                            self.step()
                            if self.monotonic() - last_publish >= HEARTBEAT_SECONDS:
                                self.publish()
                                last_publish = self.monotonic()
                            self.stop_event.wait(TICK_SECONDS)
                    finally:
                        self.stop_event.set()
                        try:
                            self.shutdown()
                        finally:
                            control.close()
                            (self.directory / CONTROL_FILE).unlink(missing_ok=True)
                return 1 if any(item.state == "FAILED" for item in self.services.values()) else 0
        except ServiceAlreadyRunning:
            return 3
        finally:
            if control is not None:
                control.close()
            if handler is not None:
                self.logger.removeHandler(handler)
                handler.close()


def status(config):
    path = scope_directory(config.panel.url, config.runtime.data_dir) / STATUS_FILE
    if not path.exists():
        return {"state": "NOT_RUNNING", "live": False, "services": {}}
    record = read_record(path)
    try:
        live = process_matches(record["pid"], record["process_created"]) and record["state"] in {"STARTING", "RUNNING", "STOPPING"}
        result = {key: record[key] for key in ("state", "pid", "started_at", "updated_at", "uptime_seconds", "config_hash")}
        result["live"] = live
        age = max(0, time.time() - datetime.fromisoformat(record["updated_at"]).timestamp())
        result["heartbeat_age_seconds"] = age
        result["healthy"] = live and age <= 5
        result["config_matches"] = record["config_hash"] == config.config_hash
        if not live and record["state"] in {"STARTING", "RUNNING", "STOPPING"}:
            result["state"] = "STALE"
        result["services"] = {}
        for name in SERVICE_NAMES:
            item = record["services"][name]
            result["services"][name] = {key: item[key] for key in
                ("state", "pid", "exit_code", "last_error", "restart_count", "circuit_open", "escalated", "retry_in_seconds")}
            result["services"][name]["live"] = process_matches(item["pid"], item["process_created"])
        result["degraded"] = any(item["state"] == "FAILED" for item in result["services"].values())
        result["healthy"] = result["healthy"] and not result["degraded"]
        return result
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
        raise ConfigError("Local supervisor status is invalid") from None


def request_stop(config):
    directory = scope_directory(config.panel.url, config.runtime.data_dir)
    record = read_record(directory / STATUS_FILE)
    control = read_record(directory / CONTROL_FILE)
    try:
        if (record["instance"] != control["instance"] or record["pid"] != control["pid"]
                or record["process_created"] != control["process_created"]
                or not process_matches(control["pid"], control["process_created"])
                or type(control["port"]) is not int or not 1 <= control["port"] <= 65535):
            raise ValueError()
        with socket.create_connection(("127.0.0.1", control["port"]), timeout=2) as connection:
            connection.sendall(json.dumps({"command": "stop", "instance": control["instance"],
                                          "token": control["token"]}).encode() + b"\n")
            reply = receive_json(connection)
        if reply.get("accepted") is not True or reply.get("instance") != control["instance"]:
            raise ValueError()
        return {"state": "STOP_REQUESTED", "pid": control["pid"]}
    except (KeyError, ValueError, TypeError, OSError, UnicodeError):
        raise ConfigError("Supervisor identity is stale or stop request was not accepted") from None

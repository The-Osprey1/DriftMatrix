"""Drift-Link keyboard shortcuts, guarded matrix actions, and USB discovery.

USB enumeration identifies candidate Pad/Watch adapters. It does not verify
their firmware or establish a synchronized connection to either device.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
from dataclasses import dataclass
from contextvars import copy_context
from typing import Any, Dict, Optional

import requests
from jsonschema import Draft7Validator

import panel_gate
from service_runtime import DriftMatrixConfig, run_owned, worker_adapter
from focus_control import default_data_dir

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

from drift_link_core import DriftEvent, DriftEventBus, DriftEventType

# Default constants
CONFIG_PATH = Path(__file__).parent / "drift_ecosystem.json"
SCHEMA_PATH = Path(__file__).parent / "drift_ecosystem.schema.json"
DEFAULT_MATRIX_URL = os.environ.get("DRIFT_MATRIX_URL", "http://192.168.1.90")
RPI_PICO_VID = 0x2E8A       # Raspberry Pi Pico (DriftPad)
WAVESHARE_VID = 0x1A86      # CH343 USB-Serial (Drift-Watch ESP32-S3)

logger = logging.getLogger("drift_ecosystem")


@dataclass
class DeviceStatus:
    name: str
    connected: bool
    connection_type: str
    details: Dict[str, Any]


@dataclass(frozen=True)
class HotkeyBinding:
    macro_index: int
    name: str
    action: str
    target: str
    hotkey: str
    modifiers: int
    virtual_key: int


def parse_hotkey(value: str) -> tuple[int, int]:
    """Translate the documented key syntax into Windows RegisterHotKey values."""
    parts = value.upper().split("+")
    modifier_names = {"ALT": 0x0001, "CTRL": 0x0002, "CONTROL": 0x0002,
                      "SHIFT": 0x0004, "WIN": 0x0008}
    modifiers = 0x4000  # MOD_NOREPEAT
    for part in parts[:-1]:
        flag = modifier_names.get(part)
        if flag is None or modifiers & flag:
            raise ValueError("Unsupported or repeated hotkey modifier")
        modifiers |= flag
    key = parts[-1]
    named_keys = {"SPACE": 0x20, "ENTER": 0x0D, "ESC": 0x1B, "ESCAPE": 0x1B,
                  "TAB": 0x09, "BACKSPACE": 0x08, "DELETE": 0x2E, "INSERT": 0x2D,
                  "HOME": 0x24, "END": 0x23, "LEFT": 0x25, "UP": 0x26,
                  "RIGHT": 0x27, "DOWN": 0x28, "PAGEUP": 0x21, "PAGEDOWN": 0x22}
    if len(key) == 1 and key in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        virtual_key = ord(key)
    elif re.fullmatch(r"F([1-9]|1[0-9]|2[0-4])", key):
        virtual_key = 0x70 + int(key[1:]) - 1
    elif key in named_keys:
        virtual_key = named_keys[key]
    else:
        raise ValueError("Unsupported hotkey key")
    return modifiers, virtual_key


def build_hotkey_bindings(pad_config: dict[str, Any]) -> list[HotkeyBinding]:
    """Validate and compile configured macros without registering any OS hooks."""
    if not pad_config.get("enabled", True):
        return []
    bindings = []
    seen = set()
    for index, macro in pad_config.get("macro_mapping", {}).items():
        if not macro.get("enabled", True):
            continue
        for field in ("hotkey_primary", "hotkey_secondary"):
            if field not in macro:
                continue
            key = macro[field]
            modifiers, virtual_key = parse_hotkey(key)
            identity = modifiers, virtual_key
            if identity in seen:
                raise ValueError("Duplicate configured hotkey")
            seen.add(identity)
            bindings.append(HotkeyBinding(
                int(index), macro.get("name", f"Macro {index}"), macro["action"],
                macro.get("target", ""), key, modifiers, virtual_key,
            ))
    return bindings


class DriftEcosystem:
    def __init__(self, config_file: Optional[Path] = None, *, config=None):
        self.config = self._load_config(config_file or CONFIG_PATH) if config is None else config
        self.devices = self.config["ecosystem"]["devices"]
        matrix_cfg = self.devices.get("matrix", {})
        
        self.matrix_url = matrix_cfg.get("url", DEFAULT_MATRIX_URL).rstrip("/")
        self.session = requests.Session()
        self.app_cycle = matrix_cfg.get("rotation_apps", [
            "moon", "nowplaying", "ocean", "pcstats0", "weatherbrief", "timepiece"
        ])
        self._cycle_index = -1
        self.hotkeys = build_hotkey_bindings(self.devices.get("pad", {}))
        self.bus = DriftEventBus()
        self._setup_event_listeners()

    def _load_config(self, path: Path) -> Dict[str, Any]:
        try:
            config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise ValueError(f"Cannot read ecosystem configuration {path}") from error
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        error = next(Draft7Validator(schema).iter_errors(config), None)
        if error is not None:
            location = ".".join(str(part) for part in error.absolute_path) or "configuration"
            # Do not include the rejected value: configuration may contain private URLs.
            raise ValueError(f"Invalid ecosystem configuration at {location} ({error.validator})")
        return config

    def device_enabled(self, device: str) -> bool:
        return self.devices.get(device, {}).get("enabled", True)

    @staticmethod
    def _disabled(name: str, connection: str) -> DeviceStatus:
        return DeviceStatus(name, False, connection, {"state": "Disabled by configuration"})

    def _setup_event_listeners(self) -> None:
        """Link event bus signals to physical hardware actions."""
        self.bus.subscribe(DriftEventType.MACRO_TRIGGERED, self._on_macro_triggered)
        self.bus.subscribe(DriftEventType.ALERT_BROADCAST, self._on_alert_broadcast)

    def _on_macro_triggered(self, event: DriftEvent) -> None:
        """Handle macro execution from DriftPad."""
        action = event.data.get("action")
        target = event.data.get("target")
        logger.info(f"Macro Event: {action} -> {target}")

        if action == "switch_app" and target:
            self.switch_matrix_app(target)
        elif action == "cycle_app":
            self.cycle_matrix_app()
        elif action == "toggle_power":
            self.toggle_matrix_power()

    def _on_alert_broadcast(self, event: DriftEvent) -> None:
        """Deliver an alert to DriftMatrix; wearable delivery is not implemented."""
        msg = event.data.get("message", "ALERT")
        icon = event.data.get("icon")
        duration = event.data.get("duration_ms", 4000)
        self.push_matrix_notification(msg, icon=icon, duration_ms=duration)

    def probe_matrix(self) -> DeviceStatus:
        """Probe DriftMatrix over local Wi-Fi HTTP REST."""
        if not self.device_enabled("matrix"):
            return self._disabled("DriftMatrix", "Wi-Fi (HTTP REST)")
        try:
            res = self.session.get(f"{self.matrix_url}/api/v1/device", timeout=1.5)
            if res.status_code == 200:
                data = res.json()
                return DeviceStatus(
                    name="DriftMatrix",
                    connected=True,
                    connection_type="Wi-Fi (HTTP REST)",
                    details={
                        "endpoint": self.matrix_url,
                        "version": data.get("version"),
                        "active_app": data.get("currentApp"),
                        "fps": data.get("fps"),
                        "wifi_rssi": f"{data.get('wifiRssi')} dBm",
                        "free_heap": f"{data.get('freeHeapBytes', 0):,} B",
                        "matrix_power": data.get("matrixPower", True),
                        "brightness": data.get("brightness"),
                    },
                )
        except Exception as e:
            logger.debug(f"DriftMatrix offline: {e}")

        return DeviceStatus(
            name="DriftMatrix",
            connected=False,
            connection_type="Wi-Fi (HTTP REST)",
            details={"endpoint": self.matrix_url, "error": "Not responding"},
        )

    def probe_driftpad(self) -> DeviceStatus:
        """Find a candidate USB port; this is not a firmware handshake."""
        if not self.device_enabled("pad"):
            return self._disabled("DriftPad", "USB Serial")
        if not serial:
            return DeviceStatus("DriftPad", False, "USB Serial", {"error": "pyserial not installed"})

        try:
            ports = list(serial.tools.list_ports.comports())
            pico_port = None
            for p in ports:
                if p.vid == int(self.devices.get("pad", {}).get("vid", hex(RPI_PICO_VID)), 16):
                    pico_port = p.device
                    break

            if pico_port:
                return DeviceStatus(
                    name="DriftPad",
                    connected=True,
                    connection_type="USB Serial (candidate adapter)",
                    details={
                        "port": pico_port,
                        "state": "USB port detected; adapter present, firmware unverified",
                    },
                )
        except Exception as e:
            logger.debug(f"DriftPad probe error: {e}")

        return DeviceStatus("DriftPad", False, "USB Serial", {"error": "Not detected"})

    def probe_driftwatch(self) -> DeviceStatus:
        """Find a candidate USB port; no watch protocol is implemented."""
        if not self.device_enabled("watch"):
            return self._disabled("Drift-Watch", "USB Serial")
        if not serial:
            return DeviceStatus("Drift-Watch", False, "USB Serial", {"error": "pyserial not installed"})

        try:
            ports = list(serial.tools.list_ports.comports())
            watch_port = None
            for p in ports:
                watch_cfg = self.devices.get("watch", {})
                vid = int(watch_cfg.get("vid", hex(WAVESHARE_VID)), 16)
                if p.vid == vid or ("vid" not in watch_cfg and p.description and "CH343" in p.description):
                    watch_port = p.device
                    break

            if watch_port:
                return DeviceStatus(
                    name="Drift-Watch",
                    connected=True,
                    connection_type="USB Serial (CH343)",
                    details={
                        "port": watch_port,
                        "state": "USB port detected; adapter present, firmware unverified",
                    },
                )
        except Exception as e:
            logger.debug(f"Drift-Watch probe error: {e}")

        return DeviceStatus("Drift-Watch", False, "USB Serial", {"error": "Not detected"})

    def get_ecosystem_status(self) -> Dict[str, DeviceStatus]:
        return {
            "matrix": self.probe_matrix(),
            "pad": self.probe_driftpad(),
            "watch": self.probe_driftwatch(),
        }

    def switch_matrix_app(self, app_name: str) -> bool:
        """Switch the active scene on DriftMatrix."""
        if not self.device_enabled("matrix"):
            return False
        payload = {"name": app_name, "fast": True}
        try:
            with panel_gate.mutation("ecosystem", "activate", app_name, body=payload) as gate:
                res = self.session.put(
                    f"{self.matrix_url}/api/v1/apps/active", json=payload, timeout=2.0,
                )
                gate.observe(res)
            success = res.status_code == 200
            if success:
                logger.info(f"DriftMatrix switched to {app_name}")
            return success
        except Exception as e:
            logger.error(f"Failed to switch matrix app to {app_name}: {e}")
            return False

    def cycle_matrix_app(self) -> str:
        """Cycle to the next visual scene in sequence."""
        index = (self._cycle_index + 1) % len(self.app_cycle)
        app = self.app_cycle[index]
        if self.switch_matrix_app(app):
            self._cycle_index = index
            return app
        return ""

    def toggle_matrix_power(self) -> bool:
        """Toggle display power; return success, including when turning it off."""
        if not self.device_enabled("matrix"):
            return False
        try:
            res = self.session.get(f"{self.matrix_url}/api/v1/device", timeout=1.5)
            if res.status_code != 200:
                return False
            curr = res.json().get("matrixPower")
            if not isinstance(curr, bool):
                return False
            target = not curr
            payload = {"power": target}
            with panel_gate.mutation("ecosystem", "display", None, body=payload) as gate:
                result = self.session.patch(
                    f"{self.matrix_url}/api/v1/display", json=payload, timeout=1.5,
                )
                gate.observe(result)
            if result.status_code != 200:
                return False
            logger.info(f"DriftMatrix power set to {target}")
            return True
        except Exception as e:
            logger.error(f"Failed to toggle matrix power: {e}")
            return False

    def push_matrix_notification(self, text: str, icon: Optional[str] = None, duration_ms: int = 4000) -> bool:
        """Send a high-priority notification banner across DriftMatrix."""
        if not self.device_enabled("matrix"):
            return False
        try:
            payload = {
                "text": text,
                "lifetimeMs": duration_ms,
                "lifetimeExpiry": "remove",
            }
            if icon:
                payload["icon"] = icon

            with panel_gate.mutation("ecosystem", "push", "drift_alert", body=payload) as gate:
                res = self.session.put(
                    f"{self.matrix_url}/api/v1/apps/pushed/drift_alert",
                    json=payload, timeout=2.0,
                )
                gate.observe(res)
            return res.status_code == 200
        except Exception as e:
            logger.error(f"Failed to push notification to matrix: {e}")
            return False


def print_dashboard(eco: DriftEcosystem) -> None:
    print("\n" + "=" * 65)
    print("           🌌 DRIFT HARDWARE ECOSYSTEM DASHBOARD            ")
    print("=" * 65)

    status = eco.get_ecosystem_status()
    for key, dev in status.items():
        symbol = ("DISABLED" if not eco.device_enabled(key) else
                  "API ONLINE" if key == "matrix" and dev.connected else
                  "ADAPTER PRESENT" if dev.connected else "NOT DETECTED")
        print(f"\n[{symbol}] {dev.name.upper()} ({dev.connection_type})")
        for k, v in dev.details.items():
            print(f"   • {k:14}: {v}")

    print("\n" + "-" * 65)
    print("USB adapter presence does not verify firmware health or synchronization.")
    print("=" * 65 + "\n")


def run_hotkey_listener(eco: DriftEcosystem, daemon_mode: bool = False, *, user32=None,
                       stop_event=None, thread_id=None) -> None:
    """Register configured shortcuts; USB adapter presence is not required."""
    if ((stop_event is not None and stop_event.is_set()) or not eco.device_enabled("pad")
            or not eco.device_enabled("matrix") or not eco.hotkeys):
        logger.info("Drift-Link listener disabled or no macros configured")
        return
    if user32 is None:
        if sys.platform != "win32":
            raise RuntimeError("Drift-Link global hotkeys require Windows")
        user32 = ctypes.windll.user32

    registered = {}
    finished = threading.Event()
    monitor = None
    msg = wintypes.MSG()
    try:
        if stop_event is not None:
            # PeekMessage creates the owning thread's queue before any quit is posted.
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            owner = thread_id if thread_id is not None else ctypes.windll.kernel32.GetCurrentThreadId()
            def request_quit():
                while not finished.is_set():
                    if stop_event.wait(0.05):
                        if user32.PostThreadMessageW(owner, 0x0012, 0, 0):  # WM_QUIT
                            return
                        finished.wait(0.05)
            monitor = threading.Thread(target=request_quit, name="drift-link-stop", daemon=True)
            monitor.start()
        for hotkey_id, binding in enumerate(eco.hotkeys, start=1):
            if not user32.RegisterHotKey(None, hotkey_id, binding.modifiers, binding.virtual_key):
                raise RuntimeError(f"Could not register hotkey {binding.hotkey}; it may be in use")
            registered[hotkey_id] = binding
        eco.bus.start()
        if not daemon_mode:
            print("Drift-Link keyboard shortcuts registered (from ecosystem configuration):")
            for binding in registered.values():
                print(f"  {binding.hotkey}: {binding.name} -> {binding.action} {binding.target}")
            print("Listening... Press Ctrl+C to exit.")

        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result == 0:
                break
            if result == -1:
                raise RuntimeError("Windows hotkey message loop failed")
            if (msg.message == 0x0312 and msg.wParam in registered
                    and not (stop_event is not None and stop_event.is_set())):  # WM_HOTKEY
                binding = registered[msg.wParam]
                eco.bus.publish(DriftEvent(
                    event_type=DriftEventType.MACRO_TRIGGERED,
                    source="DriftPad",
                    data={"macro_index": binding.macro_index,
                          "action": binding.action, "target": binding.target},
                ))
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        logger.info("Stopping Drift-Link listener")
    finally:
        finished.set()
        if monitor is not None:
            monitor.join()
        for hotkey_id in registered:
            user32.UnregisterHotKey(None, hotkey_id)
        eco.bus.stop(timeout=None)


def start_hotkey_thread(eco, stop_event, *, daemon_mode=True):
    """Keep the main thread available for Python's console signal handlers."""
    failures = []
    def listen():
        try:
            run_hotkey_listener(eco, daemon_mode, stop_event=stop_event)
        except BaseException as error:
            failures.append(error)
        finally:
            stop_event.set()
    listener_thread = threading.Thread(target=copy_context().run, args=(listen,),
                                       name="HotkeyListenerThread", daemon=True)
    listener_thread.start()
    return listener_thread, failures


def run_daemon(eco: DriftEcosystem, stop_event=None) -> None:
    """Run full background ecosystem orchestrator with heartbeat and listener."""
    logger.info("Starting Drift Hardware Ecosystem Background Daemon...")
    stop_event = stop_event or threading.Event()
    listener_thread, failures = None, []
    if eco.device_enabled("pad") and eco.device_enabled("matrix") and eco.hotkeys:
        listener_thread, failures = start_hotkey_thread(eco, stop_event)

    try:
        while not stop_event.is_set():
            # Read-only heartbeat at the configured polling interval.
            status = eco.get_ecosystem_status()
            detected = sum(1 for dev in status.values() if dev.connected)
            logger.info("Ecosystem heartbeat: %s endpoints/adapters detected; firmware sync unverified", detected)
            stop_event.wait(eco.devices["matrix"].get("poll_interval_s", 30))
    except KeyboardInterrupt:
        logger.info("Daemon terminating gracefully.")
    finally:
        stop_event.set()
        if listener_thread is not None:
            listener_thread.join()
    if failures:
        raise RuntimeError("Drift-Link listener failed") from failures[0]


@worker_adapter("drift_link")
def run_worker(config: DriftMatrixConfig, stop_event: threading.Event) -> int:
    options = config.to_mapping()["services"]["drift_link"]["options"]
    devices = options.get("devices", {})
    matrix = {"enabled": options.get("matrix_enabled", True), "url": config.panel.url}
    for key in ("rotation_apps", "poll_interval_s"):
        if key in options:
            matrix[key] = options[key]
    pad = {"enabled": False, **devices.get("pad", {}), "macro_mapping": options.get("macro_mapping", {})}
    watch = {"enabled": False, **devices.get("watch", {})}
    eco = DriftEcosystem(config={"version": "1.0.0", "ecosystem": {"devices": {
        "matrix": matrix, "pad": pad, "watch": watch}}})
    try:
        if eco.hotkeys and matrix["enabled"] and sys.platform != "win32":
            logger.error("Global hotkeys require Windows")
            return 2
        run_daemon(eco, stop_event)
        return 0
    finally:
        eco.session.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Drift Hardware Ecosystem Coordinator")
    parser.add_argument("command", choices=["status", "switch", "alert", "monitor", "listen", "daemon"], default="status", nargs="?")
    parser.add_argument("--app", help="App to activate on DriftMatrix (e.g., moon, nowplaying, ocean, pcstats0)")
    parser.add_argument("--message", help="Message for ecosystem alert")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Ecosystem configuration JSON")
    args = parser.parse_args()

    try:
        eco = DriftEcosystem(args.config)
    except ValueError as error:
        parser.error(str(error))

    stop_event = threading.Event()
    def execute():
        if args.command == "status":
            print_dashboard(eco)
        elif args.command == "switch":
            if not args.app:
                print("Error: Specify --app <name>")
                sys.exit(1)
            ok = eco.switch_matrix_app(args.app)
            print(f"Switching DriftMatrix to '{args.app}': {'SUCCESS' if ok else 'FAILED'}")
            if not ok:
                raise SystemExit(1)
        elif args.command == "alert":
            msg = args.message or "DRIFT LINK"
            if not eco.push_matrix_notification(msg):
                parser.exit(1, "Matrix alert failed or matrix disabled.\n")
            print("Alert accepted by DriftMatrix; watch delivery is not implemented.")
        elif args.command == "monitor":
            print("Starting Drift Ecosystem real-time monitor... Press Ctrl+C to stop.")
            while not stop_event.is_set():
                print_dashboard(eco)
                stop_event.wait(10)
        elif args.command == "listen":
            listener, failures = start_hotkey_thread(eco, stop_event, daemon_mode=False)
            try:
                while listener.is_alive():
                    listener.join(0.05)
            finally:
                stop_event.set()
                listener.join()
            if failures:
                raise RuntimeError("Drift-Link listener failed") from failures[0]
        elif args.command == "daemon":
            run_daemon(eco, stop_event)

        return 0
    try:
        if args.command == "status":
            return execute()
        return run_owned("drift_link", eco.matrix_url, default_data_dir(), stop_event, execute)
    finally:
        eco.session.close()


if __name__ == "__main__":
    raise SystemExit(main())

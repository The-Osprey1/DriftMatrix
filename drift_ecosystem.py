"""Drift Ecosystem Bridge & Central Coordinator.

Unifies DriftPad (RP2040 Macropad), Drift-Watch (ESP32-S3 Watch), and DriftMatrix
(ESP32 32x8 LED Matrix Display) into a single, reactive hardware ecosystem.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

from drift_link_core import DriftEvent, DriftEventBus, DriftEventType

# Default constants
CONFIG_PATH = Path(__file__).parent / "drift_ecosystem.json"
DEFAULT_MATRIX_URL = os.environ.get("DRIFT_MATRIX_URL", "http://192.168.1.90")
RPI_PICO_VID = 0x2E8A       # Raspberry Pi Pico (DriftPad)
WAVESHARE_VID = 0x1A86      # CH343 USB-Serial (Drift-Watch ESP32-S3)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("drift_ecosystem")


@dataclass
class DeviceStatus:
    name: str
    connected: bool
    connection_type: str
    details: Dict[str, Any]


class DriftEcosystem:
    def __init__(self, config_file: Optional[Path] = None):
        self.config = self._load_config(config_file or CONFIG_PATH)
        devices = self.config.get("ecosystem", {}).get("devices", {})
        matrix_cfg = devices.get("matrix", {})
        
        self.matrix_url = matrix_cfg.get("url", DEFAULT_MATRIX_URL).rstrip("/")
        self.session = requests.Session()
        self.app_cycle = matrix_cfg.get("rotation_apps", [
            "moon", "nowplaying", "ocean", "pcstats0", "weatherbrief", "timepiece"
        ])
        self._cycle_index = 0
        self.bus = DriftEventBus()
        self._setup_event_listeners()

    def _load_config(self, path: Path) -> Dict[str, Any]:
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not parse {path}, using defaults: {e}")
        return {}

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
        """Broadcast alert to DriftMatrix and connected wearable nodes."""
        msg = event.data.get("message", "ALERT")
        icon = event.data.get("icon")
        duration = event.data.get("duration_ms", 4000)
        self.push_matrix_notification(msg, icon=icon, duration_ms=duration)

    def probe_matrix(self) -> DeviceStatus:
        """Probe DriftMatrix over local Wi-Fi HTTP REST."""
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
        """Probe DriftPad over USB Serial / HID."""
        if not serial:
            return DeviceStatus("DriftPad", False, "USB Serial", {"error": "pyserial not installed"})

        try:
            ports = list(serial.tools.list_ports.comports())
            pico_port = None
            for p in ports:
                if p.vid == RPI_PICO_VID:
                    pico_port = p.device
                    break

            if pico_port:
                return DeviceStatus(
                    name="DriftPad",
                    connected=True,
                    connection_type="USB (CDC + HID Keyboard)",
                    details={
                        "port": pico_port,
                        "vid": hex(RPI_PICO_VID),
                        "keys": 16,
                        "controller": "RP2040 (Raspberry Pi Pico)",
                        "display": "SSD1306 OLED (128x64)",
                        "state": "Active (Listening for Macro / Keystrokes)",
                    },
                )
        except Exception as e:
            logger.debug(f"DriftPad probe error: {e}")

        return DeviceStatus("DriftPad", False, "USB Serial", {"error": "Not detected"})

    def probe_driftwatch(self) -> DeviceStatus:
        """Probe Drift-Watch over USB Serial / Wi-Fi."""
        if not serial:
            return DeviceStatus("Drift-Watch", False, "USB Serial", {"error": "pyserial not installed"})

        try:
            ports = list(serial.tools.list_ports.comports())
            watch_port = None
            for p in ports:
                if p.vid == WAVESHARE_VID or (p.description and "CH343" in p.description):
                    watch_port = p.device
                    break

            if watch_port:
                return DeviceStatus(
                    name="Drift-Watch",
                    connected=True,
                    connection_type="USB Serial (CH343)",
                    details={
                        "port": watch_port,
                        "controller": "ESP32-S3R2 (Dual-Core 240MHz)",
                        "screen": "1.28\" Round IPS LCD (240x240)",
                        "touch": "CST816S Capacitive Touch",
                        "state": "Active (Rendering / Touch Polling)",
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
        try:
            res = self.session.put(
                f"{self.matrix_url}/api/v1/apps/active",
                json={"name": app_name, "fast": True},
                timeout=2.0,
            )
            success = res.status_code == 200
            if success:
                logger.info(f"DriftMatrix switched to {app_name}")
            return success
        except Exception as e:
            logger.error(f"Failed to switch matrix app to {app_name}: {e}")
            return False

    def cycle_matrix_app(self) -> str:
        """Cycle to the next visual scene in sequence."""
        self._cycle_index = (self._cycle_index + 1) % len(self.app_cycle)
        app = self.app_cycle[self._cycle_index]
        self.switch_matrix_app(app)
        return app

    def toggle_matrix_power(self) -> bool:
        """Toggle display power on DriftMatrix."""
        try:
            res = self.session.get(f"{self.matrix_url}/api/v1/device", timeout=1.5)
            curr = res.json().get("matrixPower", True) if res.status_code == 200 else True
            target = not curr
            self.session.put(f"{self.matrix_url}/api/v1/system", json={"matrixPower": target}, timeout=1.5)
            logger.info(f"DriftMatrix power set to {target}")
            return target
        except Exception as e:
            logger.error(f"Failed to toggle matrix power: {e}")
            return False

    def push_matrix_notification(self, text: str, icon: Optional[str] = None, duration_ms: int = 4000) -> bool:
        """Send a high-priority notification banner across DriftMatrix."""
        try:
            payload = {
                "text": text,
                "lifetimeMs": duration_ms,
                "lifetimeExpiry": "remove",
            }
            if icon:
                payload["icon"] = icon

            res = self.session.put(
                f"{self.matrix_url}/api/v1/apps/pushed/drift_alert",
                json=payload,
                timeout=2.0,
            )
            return res.status_code == 200
        except Exception as e:
            logger.error(f"Failed to push notification to matrix: {e}")
            return False


def print_dashboard(eco: DriftEcosystem) -> None:
    print("\n" + "=" * 65)
    print("           🌌 DRIFT HARDWARE ECOSYSTEM DASHBOARD            ")
    print("=" * 65)

    status = eco.get_ecosystem_status()
    all_connected = all(dev.connected for dev in status.values())

    for key, dev in status.items():
        symbol = "🟢 ONLINE " if dev.connected else "🔴 OFFLINE"
        print(f"\n[{symbol}] {dev.name.upper()} ({dev.connection_type})")
        for k, v in dev.details.items():
            print(f"   • {k:14}: {v}")

    print("\n" + "-" * 65)
    if all_connected:
        print("  ⚡ FULL ECOSYSTEM SYNCHRONIZED — ALL 3 HARDWARE NODES ONLINE")
    else:
        print("  ⚠️  PARTIAL ECOSYSTEM RUNNING")
    print("=" * 65 + "\n")


def run_hotkey_listener(eco: DriftEcosystem, daemon_mode: bool = False) -> None:
    """Register global Windows hotkeys to listen for DriftPad Macro keys & shortcuts."""
    user32 = ctypes.windll.user32

    # Modifier constants
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_NOREPEAT = 0x4000

    def trigger_macro(macro_idx: int, action: str, target: str):
        eco.bus.publish(
            DriftEvent(
                event_type=DriftEventType.MACRO_TRIGGERED,
                source="DriftPad",
                data={"macro_index": macro_idx, "action": action, "target": target},
            )
        )

    # Hotkey map: hotkey_id -> (description, trigger_func)
    HOTKEYS = {
        # Macro 1: Now Playing
        101: ("Macro 1 (Ctrl+Alt+1 / F13) -> Now Playing", lambda: trigger_macro(1, "switch_app", "nowplaying")),
        # Macro 2: Moon Phase Tracker
        102: ("Macro 2 (Ctrl+Alt+2 / F14) -> Moon Phase", lambda: trigger_macro(2, "switch_app", "moon")),
        # Macro 3: PC Hardware Telemetry
        103: ("Macro 3 (Ctrl+Alt+3 / F15) -> PC Stats", lambda: trigger_macro(3, "switch_app", "pcstats0")),
        # Macro 4: Ocean Wave Simulation
        104: ("Macro 4 (Ctrl+Alt+4 / F16) -> Ocean Waves", lambda: trigger_macro(4, "switch_app", "ocean")),
        # Cycle App
        105: ("Cycle App (Ctrl+Alt+Space)", lambda: trigger_macro(0, "cycle_app", "")),
        # Toggle Power
        106: ("Toggle Matrix Power (Ctrl+Alt+0)", lambda: trigger_macro(0, "toggle_power", "")),
    }

    # Register Ctrl+Alt combinations
    user32.RegisterHotKey(None, 101, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("1"))
    user32.RegisterHotKey(None, 102, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("2"))
    user32.RegisterHotKey(None, 103, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("3"))
    user32.RegisterHotKey(None, 104, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("4"))
    user32.RegisterHotKey(None, 105, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x20)  # Space
    user32.RegisterHotKey(None, 106, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("0"))

    # Also register F13-F16 directly
    user32.RegisterHotKey(None, 201, MOD_NOREPEAT, 0x7C)  # F13
    user32.RegisterHotKey(None, 202, MOD_NOREPEAT, 0x7D)  # F14
    user32.RegisterHotKey(None, 203, MOD_NOREPEAT, 0x7E)  # F15
    user32.RegisterHotKey(None, 204, MOD_NOREPEAT, 0x7F)  # F16
    HOTKEYS[201] = HOTKEYS[101]
    HOTKEYS[202] = HOTKEYS[102]
    HOTKEYS[203] = HOTKEYS[103]
    HOTKEYS[204] = HOTKEYS[104]

    eco.bus.start()

    if not daemon_mode:
        print("\n" + "=" * 65)
        print("      🎮 DRIFT-LINK ACTIVE: MACROPAD & CROSS-DEVICE LISTENER     ")
        print("=" * 65)
        print("Global hotkeys registered for DriftPad Macro keys & shortcuts:")
        print("  • Macro 1 [F13 / Ctrl+Alt+1] : Switch DriftMatrix -> Now Playing (Vinyl)")
        print("  • Macro 2 [F14 / Ctrl+Alt+2] : Switch DriftMatrix -> Moon Tracker")
        print("  • Macro 3 [F15 / Ctrl+Alt+3] : Switch DriftMatrix -> PC Telemetry (GPU/CPU)")
        print("  • Macro 4 [F16 / Ctrl+Alt+4] : Switch DriftMatrix -> Ocean Waves")
        print("  • Hotkey  [Ctrl+Alt+Space]   : Cycle Next App")
        print("  • Hotkey  [Ctrl+Alt+0]       : Toggle Matrix Screen Power")
        print("-" * 65)
        print("Listening for keystrokes in background... Press Ctrl+C in terminal to exit.\n")

    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                hotkey_id = msg.wParam
                if hotkey_id in HOTKEYS:
                    desc, trigger = HOTKEYS[hotkey_id]
                    if not daemon_mode:
                        print(f"[{time.strftime('%H:%M:%S')}] 🔥 DriftPad Trigger: {desc}")
                    trigger()
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        if not daemon_mode:
            print("\nStopping Drift-Link listener...")
    finally:
        eco.bus.stop()
        for hid in (101, 102, 103, 104, 105, 106, 201, 202, 203, 204):
            user32.UnregisterHotKey(None, hid)


def run_daemon(eco: DriftEcosystem) -> None:
    """Run full background ecosystem orchestrator with heartbeat and listener."""
    logger.info("Starting Drift Hardware Ecosystem Background Daemon...")
    listener_thread = threading.Thread(
        target=run_hotkey_listener, args=(eco, True), name="HotkeyListenerThread", daemon=True
    )
    listener_thread.start()

    try:
        while True:
            # Heartbeat check every 30s
            status = eco.get_ecosystem_status()
            online_count = sum(1 for dev in status.values() if dev.connected)
            logger.info(f"Drift Ecosystem Heartbeat: {online_count}/3 nodes online")
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Daemon terminating gracefully.")


def main():
    parser = argparse.ArgumentParser(description="Drift Hardware Ecosystem Coordinator")
    parser.add_argument("command", choices=["status", "switch", "alert", "monitor", "listen", "daemon"], default="status", nargs="?")
    parser.add_argument("--app", help="App to activate on DriftMatrix (e.g., moon, nowplaying, ocean, pcstats0)")
    parser.add_argument("--message", help="Message for ecosystem alert")
    args = parser.parse_args()

    eco = DriftEcosystem()

    if args.command == "status":
        print_dashboard(eco)
    elif args.command == "switch":
        if not args.app:
            print("Error: Specify --app <name>")
            sys.exit(1)
        ok = eco.switch_matrix_app(args.app)
        print(f"Switching DriftMatrix to '{args.app}': {'SUCCESS' if ok else 'FAILED'}")
    elif args.command == "alert":
        msg = args.message or "DRIFT LINK"
        eco.bus.publish_sync(
            DriftEvent(
                event_type=DriftEventType.ALERT_BROADCAST,
                source="CLI",
                data={"message": msg},
            )
        )
        print(f"Broadcast alert '{msg}' sent across Drift Ecosystem.")
    elif args.command == "monitor":
        print("Starting Drift Ecosystem real-time monitor... Press Ctrl+C to stop.")
        while True:
            print_dashboard(eco)
            time.sleep(10)
    elif args.command == "listen":
        run_hotkey_listener(eco)
    elif args.command == "daemon":
        run_daemon(eco)


if __name__ == "__main__":
    main()

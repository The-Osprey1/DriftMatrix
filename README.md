<div align="center">

# ⚡ DriftMatrix

### Intelligent 32×8 RGB LED Matrix Desk Companion & Telemetry Display

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Platform: ESP32](https://img.shields.io/badge/Platform-ESP32-E7352C.svg?logo=espressif&logoColor=white)](https://espressif.com/)
[![Display: 32x8 WS2812B](https://img.shields.io/badge/Display-32%C3%978%20WS2812B-FF6F00.svg)]()
[![Target: 42 FPS](https://img.shields.io/badge/Performance-42%20FPS%20Target-00C853.svg)]()
[![Drift Ecosystem](https://img.shields.io/badge/Ecosystem-Drift%20Hardware-9C27B0.svg)](#-the-drift-hardware-ecosystem)

*A responsive desktop companion driving an individually addressable 32×8 RGB matrix with float32 celestial tracking, smooth typography, Windows media integration, and PC telemetry.*

[Overview](#-overview) •
[Visual Showcase](#-visual-showcase) •
[Hardware & Wiring](#-hardware--wiring) •
[On-Device Apps](#-on-device-apps-berry-vm) •
[PC Companion Suite](#-pc-companion-suite-python) •
[Quickstart](#-quickstart--setup) •
[Ecosystem](#-the-drift-hardware-ecosystem)

</div>

---

## 🧭 Overview

**DriftMatrix** is the desk-side visual anchor of the **Drift** hardware ecosystem. Built atop an ESP32 microcontroller and the AWTRIX NG engine, DriftMatrix combines on-device Berry virtual machine micro-apps with a Python companion service running locally on your workstation.

```
       WORKSTATION (PC)                          DRIFTMATRIX (ESP32)
  +-------------------------+               +----------------------------+
  |  Windows Media Session  |               |  Berry VM (Embedded Heap)  |
  |  NVIDIA GPU Telemetry   |  HTTP / REST  |   - Moon Tracker (v2.1)    |
  |  Google/Outlook ICS     +-------------->|   - Vinyl Media Scroller   |
  |  Commute & Travel Radar |   Push API    |   - Ocean Wave Simulator   |
  |  Desk Controls GUI Tray |               |   - Nightkeeper Scheduler  |
  +-------------------------+               +--------------+-------------+
                                                           | GPIO 32 (WS2812B)
                                                           v
                                            +----------------------------+
                                            |   32x8 RGB LED MATRIX      |
                                            |   [0..7]   [8]   [9...31]  |
                                            |    Icon    Gap   Text Lane |
                                            +----------------------------+
```

### 📐 The Layout Contract
Every pixel on the 32×8 canvas follows a strict spatial hierarchy:

```
  x:  0 1 2 3 4 5 6 7   |  8  |   9 10 11 12 13 ... 28 29 30 31
 y0:  . . . . . . . .   |  .  |   .  .  .  .  . ...  .  .  .  .
 y1:  . . # # # . . .   |  .  |   .  .  .  .  . ...  .  .  .  .
 y2:  . # # # # # . .   |  .  |   .  N  O  W     P  L  A  Y  .
 y3:  # # # # # # # .   |  .  |   .  .  .  .  . ...  .  .  .  .
 y4:  # # # O # # # .   |  .  |   .  B  o  h  e ...  i  a  n  .  <- Centered Mass
 y5:  # # # # # # # .   |  .  |   .  .  .  .  . ...  .  .  .  .
 y6:  . # # # # # . .   |  .  |   =  =  =  =  = ...  =  =  -  -  <- Text Baseline
 y7:  . . . . . . . .   |  .  |   .  .  .  .  . ...  .  .  .  .  <- Clear Rail
     +---------------+     +     +-----------------------------+
        Icon Well        Gutter          Dynamic Text Lane
       (Columns 0-7)   (Column 8)        (Columns 9 to 31)
```

* **Icon Well (`x0–x7`):** Visual mass centered on row `y4`.
* **Gutter (`x8`):** Unlit column separating iconography from alphanumeric data.
* **Text Lane (`x9–x31`):** High-legibility proportional typography anchored on row `y6`.
* **Clear Rail (`y7`):** Reserved for ambient gauges, progression timelines, or kept dark.

---

## 🎨 Visual Showcase

| App | Icon Well | Text Lane Preview | Description |
| :--- | :---: | :--- | :--- |
| **Moon Tracker** | `🌔` | `WAXING  82%` | Float32 astronomical ephemeris with earthshine and real crater placement |
| **Now Playing** | `💽` | `Queen - Bohemian Rhapsody` | Smooth-scrolling artist & track with animated rotating vinyl disc |
| **PC Telemetry** | `⚡` | `GPU 58°C  CPU 32%` | Real-time PC hardware vitals via `nvidia-smi` and system counters |
| **Ocean Ambient**| `🌊` | `... drifting ...` | Relaxing procedural multi-harmonic sinusoidal wave animation |
| **Calendar Next**| `📅` | `11:30 AM  Team Standup` | Upcoming meeting agenda alerts parsed directly from calendar feeds |
| **Weather Suite**| `☀️` | `78°F  CLEAR SKY` | Micro-weather station with solar dawn/dusk timelines and air quality |

---

## 🔌 Hardware & Wiring

### Bill of Materials (BOM)
* **Microcontroller:** ESP32 development board (ESP32-D0WD, ESP32-WROOM, or ESP32-S3 with 4MB+ flash)
* **Display:** 32×8 Flexible WS2812B RGB LED Matrix panel
* **Power Supply:** 5V DC power adapter (≥ 2.0A recommended)
* **Passive Components:** 
  * 1× `1000 µF` (6.3V or higher) electrolytic capacitor (power rail smoothing)
  * 1× `330Ω` – `470Ω` resistor (data line reflection protection)
* **Optional Inputs:** 3× tactile push buttons, 1× Light Dependent Resistor (LDR), 1× piezo buzzer

### Pinout Mapping

| Component / Function | Microcontroller Pin | Signal Type | Electrical Notes |
| :--- | :---: | :---: | :--- |
| **LED Matrix Data (DIN)** | **`GPIO 32`** | Digital Output | Series `330Ω–470Ω` resistor recommended |
| **Power Supply (5V / VIN)**| **`5V / VBUS`** | Power Rail | Connect directly to 5V power source |
| **Common Ground (GND)** | **`GND`** | Ground | Must tie ESP32 and Matrix ground together |
| **Left Button** | `GPIO 26` | Digital Input | Active LOW (internal pull-up enabled) |
| **Select / Mid Button** | `GPIO 27` | Digital Input | Active LOW (internal pull-up enabled) |
| **Right Button** | `GPIO 14` | Digital Input | Active LOW (internal pull-up enabled) |
| **Light Sensor (LDR)** | `GPIO 35` | Analog (ADC1) | Ambient light divider (with 10kΩ pull-down) |
| **Battery Voltage** | `GPIO 34` | Analog (ADC1) | Voltage divider (e.g., 200kΩ / 100kΩ) |
| **Buzzer** | `GPIO 15` | PWM Output | Piezo sounder for alerts and notifications |
| **I2C Bus (SDA / SCL)** | `GPIO 21` / `GPIO 22` | I2C Open-Drain | Expansion bus for environmental sensors |

### ⚡ Circuit Schematic & Signal Flow

```
                      +---------------------------------------+
                      |       5V Power Supply (>= 2A)         |
                      +-------------------+-------------------+
                                          |
                         +----------------+----------------+
                         |                                 |
                        5V                                5V
                         |                                 |
                 +-------v-------+                 +-------v-------+
                 |  ESP32 Board  |                 |  32x8 WS2812B |
                 |               |                 |  LED Matrix   |
                 |       GPIO 32 +---[ 330Ω-470Ω ]-+-> DIN         |
                 |               |                 |               |
                 |           GND +--------+--------+-> GND         |
                 |               |        |        +---------------+
                 |       GPIO 26 +--[Btn]-+ (Left)         |
                 |       GPIO 27 +--[Btn]-+ (Select)  [ 1000 µF ]
                 |       GPIO 14 +--[Btn]-+ (Right)   (across 5V & GND)
                 |               |        |
                 |       GPIO 35 +--[LDR]-+--> 3.3V (with 10k pull-down)
                 |       GPIO 15 +--[Buzzer]-+
                 +---------------+
```

```mermaid
flowchart TD
    subgraph PSU ["⚡ 5V Power Supply (≥ 2.0A)"]
        VCC["5V (+) Rail"]
        GND_MAIN["GND (-) Rail"]
    end

    subgraph ESP ["🧠 ESP32 Controller"]
        ESP_5V["VIN / 5V"]
        ESP_GND["GND"]
        ESP_G32["GPIO 32 (Matrix DIN)"]
        ESP_G26["GPIO 26 (Left Btn)"]
        ESP_G27["GPIO 27 (Select Btn)"]
        ESP_G14["GPIO 14 (Right Btn)"]
        ESP_G35["GPIO 35 (LDR ADC)"]
        ESP_G15["GPIO 15 (Buzzer)"]
        ESP_3V3["3.3V Out"]
    end

    subgraph MATRIX ["💡 32×8 WS2812B LED Matrix"]
        MAT_5V["5V Rail"]
        MAT_DIN["DIN (Data In)"]
        MAT_GND["GND Rail"]
    end

    subgraph PERIPHERALS ["🎛️ Peripherals & Controls"]
        BTN_L["Left Button (NO)"]
        BTN_M["Select Button (NO)"]
        BTN_R["Right Button (NO)"]
        BUZZ["Piezo Buzzer (+)"]
        LDR["LDR Divider"]
    end

    %% Power distribution
    VCC ==>|"5V Rail"| ESP_5V
    VCC ==>|"5V Rail"| MAT_5V
    GND_MAIN ==>|"Common Ground"| ESP_GND
    GND_MAIN ==>|"Common Ground"| MAT_GND

    %% High speed data
    ESP_G32 -->|"330Ω - 470Ω Resistor"| MAT_DIN

    %% Buttons & Sensors
    ESP_G26 --> BTN_L --> ESP_GND
    ESP_G27 --> BTN_M --> ESP_GND
    ESP_G14 --> BTN_R --> ESP_GND
    ESP_G15 --> BUZZ --> ESP_GND
    ESP_3V3 --> LDR --> ESP_G35
```

### Panel Geometry Config (`/api/v1/system`)
```json
{
  "panelWidth": 32,
  "panels": 1,
  "panelStart": "bottomRight",
  "panelWiring": "rows",
  "panelSerpentine": false
}
```

---

## 🚀 On-Device Apps (Berry VM)

All visual apps run locally on the ESP32 inside the Berry VM. Because RAM is constrained (~98 KB total VM budget), every app is strictly optimized to eliminate per-frame floating-point arithmetic and hold **40–42 FPS**:

* **🌙 Moon Tracker (`weather/moon-panel.ax` v2.1):**
  Calculates synodic lunar phases using reference epochs (`Julian Day 2451550.26`). Renders earthshine on the dark limb, 3 fixed craters, and an animated terminator line while holding 42 FPS.
* **🎵 Now Playing (`now_playing.ax`):**
  Renders an animated spinning vinyl record in the icon well with center label illumination, smooth horizontal text scrolling, and accurate progress tracking.
* **🌊 Ocean Ambient (`ocean.ax`):**
  Generative, multi-harmonic sinusoidal ocean waves designed for ambient desk lighting and focus.
* **⏰ Timepiece (`timepiece.ax`):**
  Proportional 3×5 numeric glyphs with second-tick animations and custom palettes.
* **🌙 Nightkeeper (`nightkeeper.ax`):**
  Circadian display manager. Monitors ambient room light and schedules nighttime blanking to prevent sleep disruption.
* **⛅ Weather Suite (`weather/wx*.ax`):**
  Modular weather stack containing `wxnow` (temperature), `wxrange` (high/low band), `wxsky` (sky condition), `wxsun` (solar dawn/dusk angle), `aqi` (air quality), and `soon` (countdown timer).

---

## 🖥️ PC Companion Suite (Python)

The PC companion services bridge local workstation state and cloud data directly to DriftMatrix over local HTTP REST:

```bash
# Push real-time GPU/CPU stats to DriftMatrix
python pc_stats_push.py

# Launch Windows Media Manager bridge (Spotify/YouTube/Browser audio)
python now_playing_push.py

# Start background utility orchestrator (Calendar, Commute, Weather feeds)
python utility_hub.py

# Launch Tkinter desk companion controls
python desk_controls.py
```

### Key Services
* **`pc_stats_push.py`:** Reads GPU temperature and memory using `nvidia-smi`, CPU usage, and RAM, updating the display on significant deltas with exponential backoff.
* **`now_playing_push.py`:** Captures Windows media metadata and runs the Now Playing sender. `now_playing_bridge.py` provides its HTTP bridge and script-installation helpers.
* **`next_up.py` / `next_up_controller.py`:** Parses `.ics` calendar subscriptions, filtering out expired events and alerting before meetings.
* **`commute_radar.py`:** Evaluates travel time and route conditions before scheduled departure times.
* **`github_sentinel.py`:** Alerts on PR reviews, CI workflow completions, and notifications.
* **`focus_sprint.py`:** Desk-side Pomodoro timer displaying active sprint countdowns and break phases.

---

## ⚡ Quickstart & Setup

### 1. Prerequisites
* **Python 3.10 or newer**
* Windows for media capture, native notifications, and global Drift-Link keyboard shortcuts. Core logic and previews also run on Linux.
* **ESP32** flashed with AWTRIX NG `1.1.2` or later

CI tests Python 3.10, 3.12, and 3.13 on Ubuntu and Windows. Windows-only dependencies are selected by platform markers; `tzdata` supplies Windows timezone data. USB port discovery uses `pyserial`, and ecosystem configuration validation uses `jsonschema`.

Commands below use the Python interpreter selected for your environment. On the original Windows installation, use `& C:/Users/devyn/anaconda3/python.exe` in PowerShell in place of `python` if the bare command opens the Microsoft Store.

### 2. Installation
```bash
# Clone repository
git clone https://github.com/The-Osprey1/DriftMatrix.git
cd DriftMatrix

# Install Python companion dependencies
python -m pip install -r requirements.txt
```

### 3. Configuration
The Hub reads `%LOCALAPPDATA%/AWTRIX/utility_hub.json` by default on Windows. For a new installation, copy the template there without overwriting an existing configuration:

```powershell
$configDir = Join-Path $env:LOCALAPPDATA 'AWTRIX'
New-Item -ItemType Directory -Force $configDir | Out-Null
$configFile = Join-Path $configDir 'utility_hub.json'
if (-not (Test-Path -LiteralPath $configFile)) {
    Copy-Item utility_hub.example.json $configFile
}
python utility_hub.py
```

Alternatively, use a local configuration and pass its path explicitly:

```bash
cp utility_hub.example.json utility_hub.json
python utility_hub.py --config ./utility_hub.json
```

Edit the selected file with your panel IP and local preferences. The local `utility_hub.json` is ignored by Git. Drift-Link has a separate configuration, described below.

### 4. Deploying Apps to the Matrix
Before a live change, capture and validate a rollback snapshot with `panel_snapshot.py`. A complete snapshot has a `manifest.json`; an incomplete capture is not a rollback set. Check the installed app version before choosing the rollback source.

The following example applies **only when Moon v2.0 is installed** and you are preparing v2.1. The dry run reads panel state but performs no panel writes or reboots:

```bash
# Optional offline minified output; the swapper also minifies automatically
python weather/minify_ax.py weather/moon-panel.ax --out build/moon.min.ax

# Validate the planned replacement without installing it
python script_swap.py moon weather/moon-panel.ax --expect-version 2.1 --rollback weather/moon-panel-v2.0.ax --rollback-version 2.0 --dry-run
```

After the snapshot and preflight are validated, removing `--dry-run` performs the guarded replacement and may reboot the panel. Use the source and version actually installed for `--rollback` and `--rollback-version`; do not assume the example matches your device.

### 5. Running Tests
Run the comprehensive test suite verifying layout rules, API resilience, and companion bridges:
```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q -p no:cacheprovider
```

On headless Linux, run tests under `xvfb-run -a` so the Tkinter window tests have a display. The development dependencies include NumPy and Astropy for the independent moon-accuracy checks. Tests use temporary state and mocked devices rather than the private live Hub configuration.

---

## 🌌 The Drift Hardware Ecosystem & Integration

The **Drift-Link Central Coordinator (`drift_ecosystem.py`)** connects configured Windows keyboard shortcuts to DriftMatrix HTTP actions and reports candidate USB adapters for the other Drift devices. Firmware handshakes, watch synchronization, UDP discovery, and wearable alert delivery are not implemented.

| Device | Controller | Display / Actuator | Role in Ecosystem | Repository |
| :--- | :--- | :--- | :--- | :--- |
| **DriftPad** | Raspberry Pi Pico (RP2040) | 16 Hall-Effect Keys + SSD1306 OLED | Primary Macro & Scene Controller | [The-Osprey1/DriftPad](https://github.com/The-Osprey1/DriftPad) |
| **Drift-Watch**| ESP32-S3 | 1.28" Round 240×240 IPS Touch LCD | Wrist Companion & Sync Node | [The-Osprey1/Drift-Watch](https://github.com/The-Osprey1/Drift-Watch) |
| **DriftMatrix**| ESP32 | 32×8 WS2812B RGB LED Matrix | Desktop Visual Hub & Telemetry Display | [The-Osprey1/DriftMatrix](https://github.com/The-Osprey1/DriftMatrix) |

### ⚡ Cross-Device Workflows: Drift-Link Bridge

The shipped configuration supplies these keyboard shortcuts. They work with any keyboard or a DriftPad configured to emit those keys:

1. **DriftPad Macro Triggers:**
   * **Macro 1 (`F13` / `Ctrl+Alt+1`):** Instantly commands DriftMatrix to switch to **Now Playing** (Vinyl record & playback scroller).
   * **Macro 2 (`F14` / `Ctrl+Alt+2`):** Switches DriftMatrix to **Moon Tracker** (real celestial ephemeris & craters).
   * **Macro 3 (`F15` / `Ctrl+Alt+3`):** Switches DriftMatrix to **PC Telemetry** (GPU temp via `nvidia-smi` & CPU load).
   * **Macro 4 (`F16` / `Ctrl+Alt+4`):** Switches DriftMatrix to **Ocean Waves** ambient focus visualizer.
   * **`Ctrl+Alt+Space`:** Steps through the configured rotation (`moon` → `nowplaying` → `ocean` → `pcstats0` → `weatherbrief` → `timepiece`). A page must be installed or present to activate it.
   * **`Ctrl+Alt+0`:** Toggles DriftMatrix display power. Nightkeeper retains its independent schedule.

2. **Unified Hardware Status Dashboard:**
   Run `python drift_ecosystem.py status` to read matrix API health and enumerate candidate USB adapters. USB detection does not open a serial session or prove that firmware, rendering, or touch input is working. Example labels:
   ```
   =================================================================
              🌌 DRIFT HARDWARE ECOSYSTEM DASHBOARD            
   =================================================================
   [API ONLINE] DRIFTMATRIX
   [ADAPTER PRESENT] DRIFTPAD — firmware unverified
   [ADAPTER PRESENT] DRIFT-WATCH — firmware unverified
   -----------------------------------------------------------------
   USB adapter presence does not verify firmware health or synchronization.
   =================================================================
   ```

3. **Running the Bridge Listener:**
   ```bash
   # Launch the real-time background hotkey & macro listener
   python drift_ecosystem.py listen
   ```

`drift_ecosystem.json` is an instance configuration referencing the separate Draft-07 `drift_ecosystem.schema.json`. To keep personal changes outside the tracked example, copy it to `drift_ecosystem.local.json` and select it with `--config`:

```bash
python drift_ecosystem.py listen --config ./drift_ecosystem.local.json
```

All three devices have an `enabled` flag. A disabled matrix receives no reads or writes, and disabling either the matrix or pad prevents hotkey registration. Disabling the watch prevents its USB enumeration. Each entry in `ecosystem.devices.pad.macro_mapping` specifies an action (`switch_app`, `cycle_app`, or `toggle_power`), a target for `switch_app`, and a primary and/or secondary hotkey. Entries may also set `enabled: false`.

Hotkeys accept letters, digits, F1–F24, named keys such as `Space` and `Enter`, and `Ctrl`, `Alt`, `Shift`, and `Win` modifiers. Invalid or duplicate bindings are rejected before registration. If Windows refuses a binding, previously registered keys are released and the listener reports a failure. Matrix actions use the shared `panel_gate` lock, pacing, and journal under source `ecosystem`; they do not yet share the Utility Hub's in-process `PanelClient` policy.

Invalid or missing selected configuration fails explicitly rather than silently enabling defaults. Legacy `fallback_port`, `udp_discovery_port`, and `haptic_alerts` fields are accepted as metadata for older configurations, but they do not establish serial connections, discover watches, or deliver haptics.

---

## 📜 License & Acknowledgments

* Licensed under the **[MIT License](LICENSE)**.
* Created & maintained by [The-Osprey1](https://github.com/The-Osprey1).
* Firmware powered by the open-source [AWTRIX NG](https://github.com/Blueforcer/awtrix-light) project.

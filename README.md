# DriftMatrix

An intelligent 32×8 RGB LED matrix desk display and companion ecosystem, powered by ESP32 and AWTRIX NG. 

Part of the **Drift** hardware ecosystem alongside [DriftPad](https://github.com/The-Osprey1/DriftPad) (16-key Hall-effect macropad) and [Drift-Watch](https://github.com/The-Osprey1/Drift-Watch) (ESP32-S3 round smartwatch).

---

## 📐 Hardware Architecture

DriftMatrix drives a **32×8 WS2812B individually addressable RGB LED matrix** with sub-pixel visual smoothing and custom pixel-art renderers.

### Pinout Mapping

| Component / Function | Microcontroller Pin | Description |
| :--- | :--- | :--- |
| **LED Matrix Data (DIN)** | **`GPIO 32`** | High-speed WS2812B digital data stream |
| **Power (VIN / 5V)** | **`5V / VBUS`** | Dedicated 5V rail (ensure adequate 2A+ supply) |
| **Ground (GND)** | **`GND`** | Common ground shared with logic & power |
| **Left Button** | `GPIO 26` | Physical navigation / app toggle |
| **Select / Mid Button** | `GPIO 27` | Physical confirm / settings toggle |
| **Right Button** | `GPIO 14` | Physical navigation / app toggle |
| **Light Sensor (LDR)** | `GPIO 35` | Ambient light ADC for automatic day/night dimming |
| **Battery Voltage** | `GPIO 34` | Battery ADC via voltage divider |
| **Buzzer** | `GPIO 15` | Audible alert & countdown notifications |
| **I2C Bus (SDA / SCL)** | `GPIO 21` / `GPIO 22` | Sensor expansion (temperature, pressure, etc.) |

### Matrix Geometry Configuration

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

## 🎨 On-Device Matrix Apps (Berry VM)

All visual apps run directly on the ESP32 using the Berry scripting language, optimized for high framerate (40–42 FPS) within strict heap and memory constraints:

* **🌙 Moon Tracker (`weather/moon-panel.ax` v2.1):** 
  Pixel-perfect astronomical lunar phase simulation with earthshine, fixed craters, terminator line progression, and orbital timing computed with float32 accuracy without frame drops.
* **🎵 Now Playing (`now_playing.ax`):** 
  Live media scroller with an animated spinning vinyl record, smooth text scrolling, and real-time progress bar synced to your PC playback.
* **🌊 Ocean Ambient (`ocean.ax`):** 
  Generative, multi-harmonic sinusoidal ocean wave animation for calm desk focus.
* **⏰ Timepiece (`timepiece.ax`):** 
  Minimalist, high-legibility clock face with proportional typography.
* **🌙 Nightkeeper (`nightkeeper.ax`):** 
  Automated circadian display manager that gently dims or blanks the matrix according to schedules and ambient light sensors.
* **⛅ Weather Suite (`weather/wx*.ax`):** 
  Comprehensive weather station with current conditions (`wxnow`), daily temperature spectra (`wxrange`), atmospheric conditions (`wxsky`), solar sunrise/sunset trajectories (`wxsun`), Air Quality Index (`aqi`), and urgency countdown timers (`soon`).

---

## 🖥️ PC Companion & Automation Services (Python)

The PC-side service layer monitors system metrics and online feeds, pushing dynamic screens and alerts directly to DriftMatrix over local HTTP:

* **Now Playing Bridge (`now_playing_bridge.py`):** Windows Media Manager integration for Spotify, YouTube, and browser audio.
* **PC Telemetry (`pc_stats_push.py`):** Real-time monitoring of CPU load, GPU temperature (via `nvidia-smi`), RAM usage, and network traffic.
* **Calendar Agenda (`next_up.py` / `next_up_controller.py`):** Upcoming schedule alerts and meeting notifications from `.ics` calendar feeds.
* **Commute Radar (`commute_radar.py`):** Real-time travel time and traffic condition alerts.
* **ISS Transit Watch (`iss_watch.py`):** Alerts when the International Space Station is passing visibly overhead.
* **GitHub Sentinel (`github_sentinel.py`):** CI/CD build alerts, reviews, and PR mentions.
* **Focus & Sprint (`focus_sprint.py` / `focus_control.py`):** Desk-side Pomodoro / deep work timer with visual progress tracking.
* **Desk Companion GUI (`desk_companion.py` / `desk_controls.py`):** Windows system tray app with quick toggles for brightness, manual overrides, and app loops.

---

## 🚀 Quickstart & Setup

### 1. Requirements

* **Python 3.10+** (Anaconda or standard virtual environment)
* Install PC companion dependencies:
  ```bash
  pip install -r requirements.txt
  ```

### 2. Configure Environment

Copy the example configuration file:
```bash
cp utility_hub.example.json utility_hub.json
```
Edit `utility_hub.json` with your panel IP address (`http://<PANEL_IP>`), local timezone, and desired feed settings.

### 3. Deploying Berry Scripts to the Matrix

Use the built-in minifier and script swap tools to safely deploy apps to the ESP32:
```bash
python weather/minify_ax.py weather/moon-panel.ax
python script_swap.py moon weather/moon-panel.ax --expect-version 2.1
```

### 4. Running the Test Suite

Run the full automated test suite verifying layout rules, memory budgets, and companion services:
```bash
pytest -q -p no:cacheprovider
```

---

## 📜 License

MIT License. Designed and developed by [The-Osprey1](https://github.com/The-Osprey1).

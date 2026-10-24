# Contributing to DriftMatrix

Thank you for your interest in contributing to **DriftMatrix**! We welcome bug reports, feature suggestions, new matrix visualizers, and companion bridges.

---

## 🛠️ Development Guidelines

### 1. On-Device Berry Apps (`.ax` / `.be`)

Because the ESP32 operates under tight memory constraints (interpreting Berry scripts dynamically with a ~98 KB heap pool):

* **Layout Contract:**
  * Coordinates: `x = 0..31`, `y = 0..7` (origin top-left).
  * Icon well: `x0–7` (visual mass centered on row 4).
  * Gutter: `x8`.
  * Text lane: `x9–31` (baseline at row 6).
  * Row 7 must carry no ambient text or baseline decoration.
* **Performance Budget:**
  * Must maintain **40–42 FPS** during active rendering.
  * Avoid per-pixel floating point calculations (`sqrt`, `sin`) in `draw()`. Precompute bitmaps and lookup tables in `init()` or `loop()`.
  * `line()` calls are strongly preferred over loops of individual `pixel()` calls.
* **Memory & Minification:**
  * All on-device scripts must be minified using `python weather/minify_ax.py <script.ax>` before deployment to fit within the contiguous RAM block budget.

### 2. PC Companion Services (Python)

* Companion scripts must run on **Python 3.10+**.
* Ensure all telemetry pushers handle panel disconnections gracefully with exponential backoff rather than spamming HTTP requests.
* Never log or leak personal credentials, Wi-Fi keys, or private calendar URLs.

---

## 🧪 Testing

Before submitting a Pull Request, run the automated test suite:

```bash
pytest -q -p no:cacheprovider
```

Ensure all tests pass and that any new companion service or matrix app includes corresponding unit tests or visual preview tests.

---

## 📬 Submitting Changes

1. **Fork** the repository and create your feature branch:
   ```bash
   git checkout -b feature/awesome-new-app
   ```
2. **Commit** your changes with clear, descriptive commit messages:
   ```bash
   git commit -m "feat(visualizer): add retro spectrum analyzer app"
   ```
3. **Push** to your fork:
   ```bash
   git push origin feature/awesome-new-app
   ```
4. **Open a Pull Request** against `The-Osprey1/DriftMatrix:main`.

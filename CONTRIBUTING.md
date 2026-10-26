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
* CI covers Python 3.10, 3.12, and 3.13 on Ubuntu and Windows. Keep platform-specific dependencies behind environment markers in both `requirements.txt` and `pyproject.toml`; Windows requires `tzdata` for portable timezone support.
* Put every panel mutation inside `panel_gate.mutation(...)` and record the response with `gate.observe(response)`. Hub services should use `PanelClient`, which applies that gate along with its transport policies.
* Ensure all telemetry pushers handle panel disconnections gracefully with exponential backoff rather than spamming HTTP requests.
* Never log or leak personal credentials, Wi-Fi keys, or private calendar URLs.
* Canonical configuration is validated by `config_loader.py` against the packaged Draft-07 schema. Keep migration offline and explicit; preserve provenance, reject unresolved conflicts, and never overwrite legacy inputs or an existing candidate. See [configuration and migration](docs/configuration.md).

---

## 🧪 Testing

Before submitting a Pull Request, run the automated test suite:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q -p no:cacheprovider
```

Ensure all tests pass and that any new companion service or matrix app includes corresponding unit tests or visual preview tests.

Use `xvfb-run -a python -m pytest -q -p no:cacheprovider` on headless Linux, with Tk, Xvfb, and xauth installed. Do not exclude whole test modules to work around platform or configuration differences. Tests must use temporary configuration and mocked hardware, never require a personal live installation. OS-specific behavior may use narrowly scoped platform skips.

Before any live panel mutation or reboot, capture a complete snapshot and validate the planned change and rollback source. Offline test success does not establish physical display behavior or device memory headroom.

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

# Local CPU and GPU readout preview

`pc_readouts.py` is an opt-in local design and telemetry tool. It does not send
anything to AWTRIX, install an app, start a watcher, or change the existing
CPU/RAM alert sender. No prior GPU readout was found in this repository.

Each static 32x8 page shows the full CPU or GPU label, a right-aligned percentage
(including the percent sign at 100%), and a proportional load rail. Backgrounds
stay black. Load colors are green below 50%, yellow at 50-74%, orange at 75-89%,
and red at 90% or above. These describe utilization, not hardware health or
temperature. Labels remain neutral white so meaning does not rely on color.
Invalid, unavailable, or readings older than 15 seconds render neutral `N/A`
without a filled load bar. Values are rounded to the nearest whole percent;
digits, load color, and rail all use that same rounded value. Snapshot metadata
retains the original valid sensor value.

From this folder in PowerShell, generate the synthetic comparison:

```powershell
& C:/Users/devyn/anaconda3/python.exe pc_readouts.py
```

Read this computer once and save a separate local snapshot:

```powershell
& C:/Users/devyn/anaconda3/python.exe pc_readouts.py --snapshot
```

Output defaults to `%LOCALAPPDATA%/AWTRIX/previews/cpu-gpu`. `comparison.png`
includes the existing CPU 87% alert and proposed CPU 87% page, plus CPU/GPU
examples at zero, moderate load, high load, 100%, and N/A. Individual source
images are exactly 32x8. `snapshot.png` and `snapshot.json` contain the optional
real local sample and source/timestamp metadata. Saved images are historical
snapshots; they do not update or become N/A automatically as time passes.

Previews use source RGB colors enlarged into LED cells. They do not simulate
the panel's brightness, gamma, or ambient lighting and have not been checked on
the physical display.

CPU utilization is the whole-system psutil average over 0.5 seconds. GPU
utilization is NVIDIA `nvidia-smi utilization.gpu`, which uses its own sampling
window; the samples are not synchronized. GPU index 0 is selected by default;
use `--gpu-index 1` for another device. Unsupported NVIDIA metrics, a missing
utility, a missing index, or a query failure produce N/A. AMD/Intel GPU sources
are not implemented. No CPU/GPU temperature, VRAM, clock, or thermal-health
claim is made. This design does not alter the existing RAM alert policy.

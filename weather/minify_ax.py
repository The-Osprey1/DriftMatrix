"""Strip a Berry script down for upload to a heap-constrained panel.

Compiling on the ESP32 needs a *contiguous* free block, and the panel reports
its own limit when it refuses:

    heap too fragmented to compile (largest contiguous block 10740 bytes,
    source is 11125); remove a script or reboot

So the readable source stays in the repo and a stripped copy goes to the panel.
`moon` on the device is already minified for the same reason.

Conservative by design: it only drops whole comment lines, leading indentation
and blank lines.  It never touches inline `#`, so a `#` inside a string literal
cannot be mangled.  `# @name` style metadata is preserved because the firmware
parses it.

    python minify_ax.py sunset.ax -o build/sunset.min.ax
"""

from __future__ import annotations

import argparse
from pathlib import Path


def minify(source: str) -> str:
    kept: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Metadata headers are read by the firmware; every other whole-line
        # comment is for us, not the panel.
        if stripped.startswith("#") and not stripped.startswith("# @"):
            continue
        kept.append(stripped)
    return "\n".join(kept) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    args = ap.parse_args()

    original = args.source.read_text(encoding="utf-8")
    result = minify(original)
    before = len(original.encode("utf-8"))
    after = len(result.encode("utf-8"))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(result, encoding="utf-8", newline="\n")
        print(f"{args.out}  {before} -> {after} bytes  (-{before - after})")
    else:
        print(result, end="")


if __name__ == "__main__":
    main()

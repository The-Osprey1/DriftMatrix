"""Create the exact-pixel source/implementation comparison for PC Alert."""

from __future__ import annotations

import argparse
from pathlib import Path

from pc_alert_native_concepts import composite_target
from pc_stats_preview import combined_frame, led_image, raw_image, storyboard


CPU_HISTORY = (72, 78, 82, 85, 88, 91, 94, 87)
RAM_HISTORY = (79, 82, 84, 86, 88, 90, 92, 93)


def render(output: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    source = composite_target(5)
    implementation = combined_frame(
        87,
        93,
        phase=5,
        cpu_history=CPU_HISTORY,
        ram_history=RAM_HISTORY,
    )
    if source != implementation:
        raise AssertionError("production renderer drifted from selected composite target")

    comparison = output / "pc_alert_source_vs_implementation.png"
    storyboard([source, implementation], scale=28, gap=2).save(comparison)

    native = output / "pc_alert_implementation_32x8.png"
    raw_image(implementation).save(native)

    enlarged = output / "pc_alert_implementation_led.png"
    led_image(implementation, scale=28, gap=2).save(enlarged)
    return [comparison, native, enlarged]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for path in render(args.output.resolve()):
        print(path)


if __name__ == "__main__":
    main()

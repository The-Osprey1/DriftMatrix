"""Deterministic reference model for the AWTRIX Nightkeeper schedule.

This module is deliberately independent of the panel and the network.  The
deployed Berry script can be small, while tests still have an executable source
of truth for boundaries, rounding, restarts, and retry/idempotence behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


SECONDS_PER_DAY = 24 * 60 * 60

Phase = Literal["day", "fade_down", "sleep", "fade_up"]


@dataclass(frozen=True)
class NightSchedule:
    """A recurring local-wall-clock sleep schedule."""

    sleep_hour: int = 21
    sleep_minute: int = 30
    wake_hour: int = 7
    wake_minute: int = 0
    fade_minutes: int = 30
    fade_steps: int = 15
    day_brightness: int = 45
    night_brightness: int = 1

    def __post_init__(self) -> None:
        for name, value, upper in (
            ("sleep_hour", self.sleep_hour, 23),
            ("wake_hour", self.wake_hour, 23),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= upper:
                raise ValueError(f"{name} must be an integer from 0 through {upper}")
        for name, value in (
            ("sleep_minute", self.sleep_minute),
            ("wake_minute", self.wake_minute),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 59:
                raise ValueError(f"{name} must be an integer from 0 through 59")
        if not isinstance(self.fade_minutes, int) or isinstance(self.fade_minutes, bool):
            raise ValueError("fade_minutes must be an integer")
        if self.fade_minutes <= 0:
            raise ValueError("fade_minutes must be positive")
        if not isinstance(self.fade_steps, int) or isinstance(self.fade_steps, bool):
            raise ValueError("fade_steps must be an integer")
        if self.fade_steps <= 0 or self.fade_steps > self.fade_seconds:
            raise ValueError("fade_steps must be positive and no finer than one second")
        for name, value in (
            ("day_brightness", self.day_brightness),
            ("night_brightness", self.night_brightness),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255:
                raise ValueError(f"{name} must be an integer from 0 through 255")
        if self.night_brightness >= self.day_brightness:
            raise ValueError("night_brightness must be below day_brightness")

        sleep_second = self.sleep_second
        wake_second = self.wake_second
        if sleep_second == wake_second:
            raise ValueError("sleep and wake times must differ")

        # Both fades happen during the awake interval.  Reject ambiguous
        # schedules rather than silently choosing which fade wins.
        awake_seconds = (sleep_second - wake_second) % SECONDS_PER_DAY
        if 2 * self.fade_seconds > awake_seconds:
            raise ValueError("fade windows overlap in the awake interval")

    @property
    def sleep_second(self) -> int:
        return (self.sleep_hour * 60 + self.sleep_minute) * 60

    @property
    def wake_second(self) -> int:
        return (self.wake_hour * 60 + self.wake_minute) * 60

    @property
    def fade_seconds(self) -> int:
        return self.fade_minutes * 60

    @property
    def ramp(self) -> list[int]:
        """The perceptual fade curve this schedule implies."""

        return perceptual_ramp(
            self.day_brightness,
            self.night_brightness,
            self.fade_minutes,
        )


@dataclass(frozen=True)
class DisplayTarget:
    """The physical state Nightkeeper wants the panel to have now."""

    power: bool
    brightness: int
    phase: Phase

    @property
    def actuator_state(self) -> tuple[bool, int]:
        """State relevant to HTTP writes (phase labels are diagnostic only)."""

        return self.power, self.brightness


DEFAULT_SCHEDULE = NightSchedule()


def _cyclic_elapsed(second: int, start: int) -> int:
    return (second - start) % SECONDS_PER_DAY


def _in_cyclic_window(second: int, start: int, duration: int) -> bool:
    return _cyclic_elapsed(second, start) < duration


def _ceil_ratio(numerator: int, denominator: int) -> int:
    """Ceiling division for a non-negative ratio."""

    return (numerator + denominator - 1) // denominator


# Perceived brightness follows roughly V ** (1/2.2), so a linear ramp of the
# raw value does not look like a linear fade.  The panel carries a precomputed
# table instead; this rebuilds it, and the tests assert the two agree.
PERCEPTUAL_GAMMA = 2.2


def perceptual_ramp(
    day: int,
    night: int,
    steps: int,
    gamma: float = PERCEPTUAL_GAMMA,
) -> list[int]:
    """Levels whose *perceived* brightness falls in equal steps.

    ``ramp[0]`` is ``day`` and ``ramp[steps - 1]`` is ``night``.  Values repeat
    near the bottom because brightness is an integer and the perceptual step
    there is smaller than one level; that is quantisation, not a defect.
    """

    if steps < 2:
        raise ValueError("steps must be at least 2")
    if night >= day:
        raise ValueError("night must be below day")

    hi = day ** (1.0 / gamma)
    lo = night ** (1.0 / gamma)
    out: list[int] = []
    for i in range(steps):
        level = hi + (lo - hi) * (i / (steps - 1))
        out.append(max(night, min(day, round(level ** gamma))))
    return out


def desired_state(
    local_dt: datetime,
    schedule: NightSchedule = DEFAULT_SCHEDULE,
) -> DisplayTarget:
    """Return the desired state for a valid local wall-clock datetime.

    Time-zone offsets and ``fold`` intentionally do not enter the calculation:
    this is a daily civil-time schedule.  That keeps both instances of a
    repeated fall-back hour in the same state and resumes from the correct wall
    clock phase after a spring-forward gap, reboot, or long stalled loop.
    """

    if not isinstance(local_dt, datetime):
        raise TypeError("local_dt must be a datetime")

    second = (
        local_dt.hour * 60 * 60
        + local_dt.minute * 60
        + local_dt.second
    )
    sleep_start = schedule.sleep_second
    wake_start = schedule.wake_second
    fade_duration = schedule.fade_seconds
    fade_down_start = (sleep_start - fade_duration) % SECONDS_PER_DAY

    if _in_cyclic_window(second, sleep_start, (wake_start - sleep_start) % SECONDS_PER_DAY):
        return DisplayTarget(False, schedule.night_brightness, "sleep")

    ramp = schedule.ramp

    # The panel resolves the fade per minute, not per second, because that is
    # the finest resolution its clock exposes to a script.
    if _in_cyclic_window(second, fade_down_start, fade_duration):
        minute = _cyclic_elapsed(second, fade_down_start) // 60
        return DisplayTarget(True, ramp[minute], "fade_down")

    if _in_cyclic_window(second, wake_start, fade_duration):
        minute = _cyclic_elapsed(second, wake_start) // 60
        # The same curve walked backwards, so the two fades cannot drift apart.
        return DisplayTarget(True, ramp[schedule.fade_minutes - 1 - minute], "fade_up")

    return DisplayTarget(True, schedule.day_brightness, "day")


def needs_apply(applied: DisplayTarget | None, desired: DisplayTarget) -> bool:
    """Whether a controller must send writes for ``desired``.

    A phase-only transition is not a hardware change, and a process start uses
    ``None`` so boot always reconciles the panel instead of trusting stale RAM.
    """

    return applied is None or applied.actuator_state != desired.actuator_state


def record_result(
    applied: DisplayTarget | None,
    desired: DisplayTarget,
    succeeded: bool,
) -> DisplayTarget | None:
    """Commit a target only after all required panel writes succeed."""

    return desired if succeeded else applied


def observe_power(
    holding: bool,
    last_written_power: bool | None,
    observed_power: bool,
) -> bool:
    """Whether to hold after seeing the panel's actual power state.

    Re-asserting power on a timer meant a manual change was undone within the
    re-assert interval: turn the panel off by hand at midday and it came back
    on, which is the display arguing with the person in front of it.

    If what the panel is doing disagrees with what this controller last wrote,
    something else moved it, and something else is a person.  Hold.
    """

    if last_written_power is None:
        return holding
    if observed_power != last_written_power:
        return True
    return holding


def observe_brightness(
    holding: bool,
    last_written_brightness: int | None,
    observed_brightness: int,
) -> bool:
    """Whether to hold after seeing the panel's actual brightness level.

    The same argument as :func:`observe_power`, for the other half of the
    setpoint. v2.0 held power but re-forced brightness every tick, so turning
    the panel on by hand during the blackout left it stuck at level 1, and
    dimming it by hand was undone within a second. Measured on the panel on
    2026-08-22: a manual brightness of 45 was back to 1 six seconds later.

    A hold has to cover the whole setpoint or it is not a hold.
    """

    if last_written_brightness is None:
        return holding
    if observed_brightness != last_written_brightness:
        return True
    return holding


def adopt_brightness(last_known: int | None, observed: int) -> int:
    """Seed the override baseline on the first observation, not the first write.

    v2.1 recorded a level only when it *wrote* one, so a panel that already
    agreed with the schedule never established a baseline -- and the first
    manual change then read as "nothing known yet, so not an override" and was
    overwritten anyway.  Measured on the panel 2026-08-22: after a reboot into
    the blackout the level already matched, and a manual 45 was still stomped
    back to 1 within fifteen seconds.

    Same failure shape as the moon script's ``lh = -1``: a sentinel for
    "unknown" that nothing ever resolved.
    """

    return observed if last_known is None else last_known


def release_hold(holding: bool, previous_phase: Phase | None, phase: Phase) -> bool:
    """A hold lasts until the next scheduled transition, not forever.

    Same contract as a thermostat's temporary hold: the manual setting stands
    until the schedule next has something to say, then the schedule resumes.
    """

    if previous_phase is not None and phase != previous_phase:
        return False
    return holding

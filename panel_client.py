"""Shared, conservative HTTP transport for PC-pushed AWTRIX apps.

The utility apps share one client so display mutations are serialized, app
activation is globally de-duplicated, and a struggling ESP32 is not hammered by
several independent senders.  This module deliberately owns no display power
policy: Nightkeeper remains the sole authority for the panel's sleep cycle.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time as wall_time
from enum import IntEnum
import heapq
import itertools
import math
import re
import threading
import time
from typing import Any, Generic, Protocol, TypeVar
import unicodedata
from urllib.parse import quote

import requests

import firmware_compat
import panel_gate


APPS_PATH = "/api/v1/apps"
DEVICE_PATH = "/api/v1/device"
PUSHED_APP_PATH = "/api/v1/apps/pushed/{name}"
DELETE_APP_PATH = "/api/v1/apps/{name}"
ACTIVE_APP_PATH = "/api/v1/apps/active"
REBOOT_PATH = "/api/v1/device/reboot"

# Sanity bounds for the Wi-Fi fields of GET /api/v1/device. RSSI is negative
# dBm; 0 is excluded on purpose, because the ESP32 Wi-Fi stack reports 0 when it
# has no reading (not associated), and logging it would rank "no signal" as the
# best signal in the report's histogram. Counters are uint32 on the device.
WIFI_RSSI_MIN = -120
WIFI_RSSI_MAX = -1
WIFI_COUNTER_MAX = 2**32 - 1
WIFI_STATE_CHARS = 16
WIFI_ERROR_CHARS = 48
# What a Wi-Fi text becomes when a fragment of the network's name survives in
# it (see _wifi_text). Fits WIFI_STATE_CHARS.
WIFI_REDACTED = "(redacted)"
SSID_FRAGMENT_CHARS = 4


class Clock(Protocol):
    """The two clocks needed by the transport, injectable for deterministic tests."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    def wall_now(self) -> datetime: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def wall_now(self) -> datetime:
        return datetime.now().astimezone()


class PanelClientError(RuntimeError):
    """Base class for errors produced by :class:`PanelClient`."""


class PanelTransportError(PanelClientError):
    """The panel could not be reached."""


class PanelProtocolError(PanelClientError):
    """The panel returned a response whose shape cannot be trusted."""


class UnsafeAppDeletion(PanelClientError):
    """A generic delete was refused because pushed-app ownership was not proven."""


class PanelHTTPError(PanelClientError):
    def __init__(self, operation: str, status_code: int, detail: str = "") -> None:
        self.operation = operation
        self.status_code = int(status_code)
        self.detail = str(detail)[:200]
        suffix = f": {self.detail}" if self.detail else ""
        super().__init__(f"{operation} HTTP {self.status_code}{suffix}")

    @property
    def retryable(self) -> bool:
        return self.status_code in (408, 425, 429) or self.status_code >= 500


@dataclass(frozen=True)
class RetryBackoff:
    """Bounded deterministic exponential backoff for coordinator retry loops."""

    initial_seconds: float = 1.0
    multiplier: float = 2.0
    maximum_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.initial_seconds < 0:
            raise ValueError("initial_seconds cannot be negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")
        if self.maximum_seconds < self.initial_seconds:
            raise ValueError("maximum_seconds cannot be below initial_seconds")

    def delay(self, failure_number: int) -> float:
        failure_number = max(1, int(failure_number))
        delay = self.initial_seconds * self.multiplier ** (failure_number - 1)
        return min(self.maximum_seconds, float(delay))


@dataclass(frozen=True)
class PanelPolicy:
    request_timeout_seconds: float = 5.0
    minimum_mutation_interval_seconds: float = 2.0
    activation_cooldown_seconds: float = 10.0
    quiet_start: wall_time | None = wall_time(21, 30)
    quiet_end: wall_time | None = wall_time(7, 0)
    static_after_failures: int = 5
    # ...and how many clean mutations in a row earn animation back.
    #
    # Without this the latch was one-way: five consecutive failures -- which is
    # about twenty-five seconds of a Wi-Fi dropout on a -75 dBm link -- silently
    # swapped every animated payload for its static variant for the rest of the
    # process's life, with nothing logged and no way back. The same mistake was
    # already found and fixed once in pc_stats_push's safe_mode; this is the
    # second copy of it.
    #
    # Higher than static_after_failures on purpose. Degrading should be quick
    # and recovering should be slow, because the failure this guards against
    # is a panel that cannot cope with animation, and asking it again too
    # eagerly is how a struggling panel gets pushed over.
    animate_after_successes: int = 10
    retry_backoff: RetryBackoff = field(default_factory=RetryBackoff)

    def __post_init__(self) -> None:
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.minimum_mutation_interval_seconds < 0:
            raise ValueError("minimum mutation interval cannot be negative")
        if self.activation_cooldown_seconds < 0:
            raise ValueError("activation cooldown cannot be negative")
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("quiet_start and quiet_end must both be set or both be None")
        if self.static_after_failures < 1:
            raise ValueError("static_after_failures must be at least 1")
        if self.animate_after_successes < 1:
            raise ValueError("animate_after_successes must be at least 1")


@dataclass(frozen=True)
class DeviceHealth:
    uptime_seconds: int
    reset_reason: str | None = None
    free_heap: int | None = None
    min_free_heap: int | None = None
    largest_block: int | None = None
    fps: float | None = None
    current_app: str | None = None
    matrix_power: bool | None = None
    # From "wifiRssi" and the "wifi" object. The object's "host" is the SSID
    # and "endpoint" names the network too: neither is ever kept, and both are
    # cut out of the state and error text before those are stored.
    wifi_rssi: int | None = None
    wifi_connects: int | None = None
    wifi_attempts: int | None = None
    wifi_state: str | None = None
    wifi_last_error: str | None = None


class MutationPriority(IntEnum):
    """Lower values run first; this encodes the Utility Hub relevance policy."""

    OUTAGE = 0
    FOCUS = 1
    NEXT_UP = 2
    ROUTINE = 3


@dataclass(frozen=True)
class ActivationOutcome:
    sent: bool
    reason: str
    priority: MutationPriority
    retry_after_seconds: float = 0.0
    response: Any = field(default=None, compare=False, repr=False)


T = TypeVar("T")


@dataclass(order=True)
class QueuedMutation(Generic[T]):
    priority: MutationPriority
    sequence: int
    operation: Callable[[], T] = field(compare=False, repr=False)
    label: str = field(default="", compare=False)


class PriorityMutationQueue:
    """Small coordinator hook: priority first, FIFO within equal priority."""

    def __init__(self) -> None:
        self._items: list[QueuedMutation[Any]] = []
        self._sequence = itertools.count()
        self._lock = threading.Lock()

    def submit(
        self,
        operation: Callable[[], T],
        *,
        priority: MutationPriority = MutationPriority.ROUTINE,
        label: str = "",
    ) -> QueuedMutation[T]:
        item = QueuedMutation(MutationPriority(priority), next(self._sequence), operation, label)
        with self._lock:
            heapq.heappush(self._items, item)
        return item

    def pop(self) -> QueuedMutation[Any]:
        with self._lock:
            if not self._items:
                raise IndexError("mutation queue is empty")
            return heapq.heappop(self._items)

    def run_next(self) -> Any:
        return self.pop().operation()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def _printable(value: str) -> str:
    """Drop control (C0, DEL, C1), format (bidi, zero-width) and surrogate code points."""
    return "".join(
        character for character in value
        if unicodedata.category(character) not in ("Cc", "Cf", "Cs")
    )


def short_text(value: Any, limit: int = 32) -> str | None:
    """A device-reported name, bounded and free of control characters."""
    if not isinstance(value, str):
        return None
    text = _printable(value).strip()[:limit].strip()
    return text or None


def _wifi_rssi(value: Any) -> int | None:
    """Whole negative dBm within reason, else None (never raises)."""
    if type(value) is float and math.isfinite(value) and value.is_integer():
        value = int(value)
    if type(value) is not int:
        return None
    return value if WIFI_RSSI_MIN <= value <= WIFI_RSSI_MAX else None


def _wifi_counter(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= WIFI_COUNTER_MAX else None


def _wifi_secrets(wifi: Mapping[str, Any]) -> list[str]:
    """The names to keep out of the log (SSID, endpoint), NFKC, longest first."""
    secrets = []
    for key in ("host", "endpoint"):
        value = wifi.get(key)
        if isinstance(value, str):
            name = unicodedata.normalize("NFKC", _printable(value)).strip()
            if name:
                secrets.append(name)
    # Longest first, so an endpoint that contains the SSID goes out whole.
    return sorted(set(secrets), key=len, reverse=True)


def _wifi_text(value: Any, wifi: Mapping[str, Any], limit: int) -> str | None:
    """Short Wi-Fi text with the network's name cut out *before* truncating.

    Both sides are NFKC-normalised first, so a decomposed "Café" still
    matches. Every whole occurrence of the name becomes "*", ignoring case
    (a firmware message need not quote it verbatim). Then, compared
    case-folded so that "STRASSE" finds "Straße", the rest must not hold any
    run of SSID_FRAGMENT_CHARS characters of the name: firmware that cuts its
    message to a fixed buffer can leave a prefix of the name at the end, and
    such a text is replaced whole by WIFI_REDACTED. Cutting here before
    redacting could leave a prefix too, so the cut comes last. A name shorter
    than the fragment length is only caught whole.
    """
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", _printable(value))
    secrets = _wifi_secrets(wifi)
    for secret in secrets:
        text = re.sub(re.escape(secret), "*", text, flags=re.IGNORECASE)
    folded = text.casefold()
    width = SSID_FRAGMENT_CHARS
    for secret in secrets:
        name = secret.casefold()
        if any(name[i:i + width] in folded for i in range(len(name) - width + 1)):
            return short_text(WIFI_REDACTED, limit)
    return short_text(text, limit)


def is_quiet_time(
    moment: datetime,
    start: wall_time | None = wall_time(21, 30),
    end: wall_time | None = wall_time(7, 0),
) -> bool:
    """Return whether *moment* lies in a possibly midnight-wrapping window."""

    if start is None or end is None or start == end:
        return False
    current = moment.time().replace(tzinfo=None)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def retry_call(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    backoff: RetryBackoff | None = None,
    clock: Clock | None = None,
) -> T:
    """Run a panel operation with bounded retry of transient failures only."""

    attempts = int(attempts)
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    backoff = backoff or RetryBackoff()
    clock = clock or SystemClock()
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except PanelHTTPError as error:
            if not error.retryable or attempt == attempts:
                raise
        except PanelTransportError:
            if attempt == attempts:
                raise
        clock.sleep(backoff.delay(attempt))
    raise AssertionError("unreachable retry loop")


class PanelClient:
    """One shared transport for all PC-hosted utility apps."""

    def __init__(
        self,
        base_url: str,
        *,
        session: Any | None = None,
        clock: Clock | None = None,
        policy: PanelPolicy | None = None,
        source: str = "hub",
    ) -> None:
        root = str(base_url).strip().rstrip("/")
        if not root:
            raise ValueError("base_url cannot be empty")
        self.base_url = root
        self.session = session if session is not None else requests.Session()
        self.clock = clock or SystemClock()
        self.policy = policy or PanelPolicy()
        # Who the cross-process mutation journal names for this client's writes.
        self.source = source

        self._state_lock = threading.RLock()
        self._last_mutation_at: float | None = None
        self._last_activation_at: float | None = None
        self._last_uptime: int | None = None
        self._reboot_generation = 0
        self._mutation_failures = 0
        self._static_latched = False
        self._static_reason: str | None = None
        self._static_successes = 0

    @property
    def static_latched(self) -> bool:
        with self._state_lock:
            return self._static_latched

    @property
    def static_reason(self) -> str | None:
        with self._state_lock:
            return self._static_reason

    @property
    def consecutive_mutation_failures(self) -> int:
        with self._state_lock:
            return self._mutation_failures

    @property
    def last_uptime(self) -> int | None:
        with self._state_lock:
            return self._last_uptime

    @property
    def reboot_generation(self) -> int:
        """Number of observed uptime rollbacks, including planned restarts."""
        with self._state_lock:
            return self._reboot_generation

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _app_segment(name: str) -> str:
        value = str(name).strip()
        if not value:
            raise ValueError("app name cannot be empty")
        if any(ord(character) < 32 for character in value):
            raise ValueError("app name cannot contain control characters")
        return quote(value, safe="")

    def _latch_static(self, reason: str) -> None:
        self._static_latched = True
        self._static_successes = 0
        if self._static_reason is None:
            self._static_reason = reason

    def _record_mutation_failure(self) -> None:
        self._mutation_failures += 1
        self._static_successes = 0
        if self._mutation_failures >= self.policy.static_after_failures:
            self._latch_static(f"{self._mutation_failures} consecutive mutation failures")

    def _record_mutation_success(self) -> None:
        self._mutation_failures = 0
        if not self._static_latched:
            return
        # Earn animation back rather than latching for the life of the process.
        # The successes have to be consecutive: _record_mutation_failure zeroes
        # this counter, so an intermittent link never accumulates its way out.
        self._static_successes += 1
        if self._static_successes >= self.policy.animate_after_successes:
            self._static_latched = False
            self._static_reason = None
            self._static_successes = 0

    def seconds_until_mutation(self) -> float:
        with self._state_lock:
            if self._last_mutation_at is None:
                return 0.0
            elapsed = self.clock.monotonic() - self._last_mutation_at
            return max(0.0, self.policy.minimum_mutation_interval_seconds - elapsed)

    def seconds_until_activation(self) -> float:
        with self._state_lock:
            if self._last_activation_at is None:
                return 0.0
            elapsed = self.clock.monotonic() - self._last_activation_at
            return max(0.0, self.policy.activation_cooldown_seconds - elapsed)

    def _wait_for_mutation_slot(self) -> float:
        wait = self.seconds_until_mutation()
        if wait > 0:
            self.clock.sleep(wait)
        started_at = self.clock.monotonic()
        self._last_mutation_at = started_at
        return started_at

    def _mutate(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        accepted_statuses: tuple[int, ...] = (),
        before_send: Callable[[Any], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run an optional precondition after pacing and gate entry, before HTTP.

        The callback sees the actual gate record and may refuse a best-effort
        gate outcome. Rejection sends no HTTP request or transport-failure count.
        """
        op, app = panel_gate.classify(method, path, kwargs.get("json"))
        body = kwargs.get("json", kwargs.get("data"))
        with self._state_lock:
            self._wait_for_mutation_slot()
            request = getattr(self.session, method.lower())
            dispatched = False
            try:
                with panel_gate.mutation(self.source, op, app, body=body) as gate:
                    if before_send is not None:
                        before_send(gate)
                    dispatched = True
                    response = request(
                        self._url(path),
                        timeout=self.policy.request_timeout_seconds,
                        **kwargs,
                    )
                    gate.observe(response)
            except requests.RequestException as error:
                if not dispatched:
                    raise  # A failed precondition did not attempt this mutation.
                self._record_mutation_failure()
                raise PanelTransportError(
                    f"{operation} unreachable: {type(error).__name__}"
                ) from error

            status = int(response.status_code)
            if not (200 <= status < 300 or status in accepted_statuses):
                self._record_mutation_failure()
                raise PanelHTTPError(operation, status, getattr(response, "text", ""))
            self._record_mutation_success()
            return response

    def _read_json(self, path: str, *, operation: str) -> Any:
        try:
            response = self.session.get(
                self._url(path),
                timeout=self.policy.request_timeout_seconds,
            )
        except requests.RequestException as error:
            raise PanelTransportError(
                f"{operation} unreachable: {type(error).__name__}"
            ) from error
        status = int(response.status_code)
        if not 200 <= status < 300:
            raise PanelHTTPError(operation, status, getattr(response, "text", ""))
        try:
            return response.json()
        except (TypeError, ValueError) as error:
            raise PanelProtocolError(f"{operation} returned invalid JSON") from error

    def push_app(
        self,
        name: str,
        payload: Mapping[str, Any],
        *,
        static_payload: Mapping[str, Any] | None = None,
    ) -> Any:
        """Push an app, automatically choosing its safe static variant once latched."""

        segment = self._app_segment(name)
        selected = static_payload if self.static_latched and static_payload is not None else payload
        if not isinstance(selected, Mapping):
            raise TypeError("payload must be a mapping")
        return self._mutate(
            "put",
            PUSHED_APP_PATH.format(name=segment),
            operation=f"push {name}",
            json=firmware_compat.prepare_payload(self.base_url, selected),
        )

    # Compact alias for callers that prefer transport verbs.
    push = push_app

    @staticmethod
    def _inventory_list(inventory: Any) -> list[Any]:
        if isinstance(inventory, dict):
            inventory = inventory.get("apps", inventory.get("value"))
        if not isinstance(inventory, list):
            raise PanelProtocolError("app inventory has an unexpected shape")
        return inventory

    def delete_pushed_app(self, name: str) -> bool:
        """Delete only after the current inventory proves pushed-app ownership."""

        segment = self._app_segment(name)
        inventory = self._inventory_list(self._read_json(APPS_PATH, operation="inspect apps"))
        matches = [
            item
            for item in inventory
            if isinstance(item, dict) and item.get("name") == str(name).strip()
        ]
        if len(matches) > 1:
            raise PanelProtocolError(f"app inventory contains duplicate {name!r} entries")
        if not matches:
            return False

        entry = matches[0]
        present = entry.get("present")
        origin = entry.get("origin")
        if present is False:
            return False
        if present is not True:
            raise UnsafeAppDeletion(f"refusing to delete {name!r}: presence is not confirmed")
        if origin != "pushed":
            raise UnsafeAppDeletion(
                f"refusing to delete {name!r}: origin is {origin!r}, not 'pushed'"
            )

        self._mutate(
            "delete",
            DELETE_APP_PATH.format(name=segment),
            operation=f"delete {name}",
            accepted_statuses=(404,),
        )
        return True

    delete = delete_pushed_app

    def activate_app(
        self,
        name: str,
        *,
        priority: MutationPriority = MutationPriority.ROUTINE,
        fast: bool = True,
        at: datetime | None = None,
    ) -> ActivationOutcome:
        """Request one immediate show unless quiet hours or global cooldown suppress it."""

        self._app_segment(name)  # Validate before changing any timing state.
        priority = MutationPriority(priority)
        with self._state_lock:
            moment = at if at is not None else self.clock.wall_now()
            if is_quiet_time(moment, self.policy.quiet_start, self.policy.quiet_end):
                return ActivationOutcome(False, "quiet-hours", priority)

            retry_after = self.seconds_until_activation()
            if retry_after > 0:
                return ActivationOutcome(False, "cooldown", priority, retry_after)

            try:
                response = self._mutate(
                    "put",
                    ACTIVE_APP_PATH,
                    operation=f"activate {name}",
                    json={"name": str(name).strip(), "fast": bool(fast)},
                )
            finally:
                # A failed activation still consumes the cooldown so an outage
                # cannot generate a retry storm from several producers.
                self._last_activation_at = self._last_mutation_at
            return ActivationOutcome(True, "sent", priority, response=response)

    activate = activate_app

    def queue_activation(
        self,
        queue: PriorityMutationQueue,
        name: str,
        *,
        priority: MutationPriority = MutationPriority.ROUTINE,
        fast: bool = True,
        at: datetime | None = None,
    ) -> QueuedMutation[ActivationOutcome]:
        """Enqueue, rather than immediately send, an activation request."""

        return queue.submit(
            lambda: self.activate_app(name, priority=priority, fast=fast, at=at),
            priority=priority,
            label=f"activate:{name}",
        )

    def probe_device(self) -> DeviceHealth:
        """Read health and select static art after an unplanned uptime rollback."""

        body = self._read_json(DEVICE_PATH, operation="probe device")
        if not isinstance(body, dict):
            raise PanelProtocolError("device response has an unexpected shape")
        try:
            uptime = int(body["uptimeSeconds"])
        except (KeyError, TypeError, ValueError) as error:
            raise PanelProtocolError("device response has no valid uptimeSeconds") from error
        if uptime < 0:
            raise PanelProtocolError("device uptimeSeconds cannot be negative")
        firmware_compat.observe_device(self.base_url, body)
        reason_value = body.get("resetReason")
        reason = None if reason_value is None else str(reason_value)

        with self._state_lock:
            previous = self._last_uptime
            if previous is not None and uptime < previous:
                self._reboot_generation += 1
                if (reason or "").strip().lower() != "software":
                    self._latch_static(f"uptime rollback {previous} -> {uptime}")
            self._last_uptime = uptime
        def number(key: str) -> Any:
            value = body.get(key)
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        # Wi-Fi is evidence, not a precondition: a missing or malformed object
        # blanks these fields and never fails the probe.
        wifi = body.get("wifi")
        if not isinstance(wifi, dict):
            wifi = {}

        return DeviceHealth(
            uptime_seconds=uptime,
            reset_reason=reason,
            free_heap=number("freeHeapBytes"),
            min_free_heap=number("minFreeHeapBytes"),
            largest_block=number("largestFreeBlockBytes"),
            fps=number("fps"),
            current_app=short_text(body.get("currentApp")),
            matrix_power=body.get("matrixPower") if isinstance(body.get("matrixPower"), bool) else None,
            wifi_rssi=_wifi_rssi(body.get("wifiRssi")),
            wifi_connects=_wifi_counter(wifi.get("connects")),
            wifi_attempts=_wifi_counter(wifi.get("attempts")),
            wifi_state=_wifi_text(wifi.get("state"), wifi, WIFI_STATE_CHARS),
            wifi_last_error=_wifi_text(wifi.get("lastError"), wifi, WIFI_ERROR_CHARS),
        )

    observe_health = probe_device

    def read_json(self, path: str, *, operation: str | None = None) -> Any:
        """Read-only GET of any API path; never counts against the mutation budget."""

        return self._read_json(path, operation=operation or f"read {path}")

    def expect_reboot(self) -> None:
        """Keep the health baseline when a caller is about to request a reboot.

        A reboot is planned only when the panel reports ``software`` after an
        observed uptime rollback. Clearing the baseline hid a panic if it was
        the next read, and a read just before the reset rearmed a false crash
        latch. This compatibility method deliberately creates no suppression
        window: unknown reset reasons still take the conservative path.
        """

    def reboot(self, *, before_send: Callable[[Any], None] | None = None) -> Any:
        """Ask the panel to restart (about nine seconds to come back)."""

        self.expect_reboot()
        if before_send is None:
            return self._mutate("post", REBOOT_PATH, operation="reboot")
        return self._mutate("post", REBOOT_PATH, operation="reboot", before_send=before_send)

    def call_with_retry(self, operation: Callable[[], T], *, attempts: int = 3) -> T:
        return retry_call(
            operation,
            attempts=attempts,
            backoff=self.policy.retry_backoff,
            clock=self.clock,
        )


__all__ = [
    "ACTIVE_APP_PATH",
    "APPS_PATH",
    "ActivationOutcome",
    "DELETE_APP_PATH",
    "DEVICE_PATH",
    "DeviceHealth",
    "MutationPriority",
    "PanelClient",
    "PanelClientError",
    "PanelHTTPError",
    "PanelPolicy",
    "PanelProtocolError",
    "PanelTransportError",
    "PriorityMutationQueue",
    "PUSHED_APP_PATH",
    "QueuedMutation",
    "REBOOT_PATH",
    "RetryBackoff",
    "SystemClock",
    "UnsafeAppDeletion",
    "is_quiet_time",
    "retry_call",
    "short_text",
]

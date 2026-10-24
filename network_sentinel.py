"""Network-health classification for the AWTRIX Network Sentinel.

The module intentionally stops at observation and state.  It never contacts the
AWTRIX panel and never changes network or operating-system configuration.  A
future sender can feed :class:`ProbeSample` objects into :class:`NetworkSentinel`
and decide how to render the returned :class:`SentinelDecision`.

The default policy favours useful silence:

* router, WAN, DNS, and captive-login faults need three consecutive samples;
* those hard faults need six fully healthy samples to clear;
* intermittent loss needs at least three failures and 25% loss in 12 attempts;
* slowness needs four of the last six attempts at 150 ms or above;
* recovery is emitted once as ``BACK``, then the state returns to ``HEALTHY``.

Small, bounded probe helpers are included for the eventual Windows PC service.
They are read-only and injectable; deterministic tests should construct samples
directly instead of touching the real network.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import ipaddress
import math
import os
import queue
import re
import socket
import subprocess
import threading
import time
from typing import Callable, Protocol
import urllib.error
import urllib.request


class NetworkState(str, Enum):
    """Stable public states consumed by the future renderer and sender."""

    HEALTHY = "healthy"
    ROUTER = "router"
    WAN = "wan"
    DNS = "dns"
    LOGIN = "login"
    LOSS = "loss"
    SLOW = "slow"
    BACK = "back"


HARD_STATES = frozenset(
    {
        NetworkState.ROUTER,
        NetworkState.WAN,
        NetworkState.DNS,
        NetworkState.LOGIN,
    }
)
FAULT_STATES = frozenset((*HARD_STATES, NetworkState.LOSS, NetworkState.SLOW))


@dataclass(frozen=True)
class ProbeSample:
    """One read-only observation of the packet path.

    ``None`` means a downstream check was not attempted or was inconclusive.
    For example, a collector should normally leave ``wan_ok`` and ``dns_ok`` as
    ``None`` when the router itself cannot be reached.  ``latency_ms`` describes
    the WAN attempt, not the time taken to render or send an AWTRIX payload.
    """

    router_ok: bool
    wan_ok: bool | None
    dns_ok: bool | None
    login_required: bool | None
    latency_ms: float | None = None
    timestamp: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        for name in ("router_ok",):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")
        for name in ("wan_ok", "dns_ok", "login_required"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be a bool or None")
        if self.latency_ms is not None:
            if isinstance(self.latency_ms, bool) or not isinstance(
                self.latency_ms, (int, float)
            ):
                raise TypeError("latency_ms must be a number or None")
            if not math.isfinite(float(self.latency_ms)) or self.latency_ms < 0:
                raise ValueError("latency_ms must be finite and non-negative")
        if isinstance(self.timestamp, bool) or not isinstance(self.timestamp, (int, float)):
            raise TypeError("timestamp must be a number")
        if not math.isfinite(float(self.timestamp)):
            raise ValueError("timestamp must be finite")


@dataclass(frozen=True)
class SentinelPolicy:
    """Hysteresis thresholds for the shipped Network Sentinel behaviour."""

    hard_trigger_count: int = 3
    hard_clear_count: int = 6
    loss_window: int = 12
    loss_min_failures: int = 3
    loss_trigger_percent: float = 25.0
    loss_clear_percent: float = 10.0
    slow_window: int = 6
    slow_min_high: int = 4
    slow_trigger_ms: float = 150.0
    slow_clear_ms: float = 100.0
    slow_clear_count: int = 6

    def __post_init__(self) -> None:
        integer_fields = (
            "hard_trigger_count",
            "hard_clear_count",
            "loss_window",
            "loss_min_failures",
            "slow_window",
            "slow_min_high",
            "slow_clear_count",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.loss_min_failures > self.loss_window:
            raise ValueError("loss_min_failures cannot exceed loss_window")
        if self.slow_min_high > self.slow_window:
            raise ValueError("slow_min_high cannot exceed slow_window")
        if not 0 <= self.loss_clear_percent < self.loss_trigger_percent <= 100:
            raise ValueError("loss percentages must satisfy 0 <= clear < trigger <= 100")
        if not 0 <= self.slow_clear_ms < self.slow_trigger_ms:
            raise ValueError("slow thresholds must satisfy 0 <= clear < trigger")


DEFAULT_POLICY = SentinelPolicy()


@dataclass(frozen=True)
class SentinelDecision:
    """The state-machine result after consuming one sample.

    ``state`` is the durable output state.  ``observed`` is the highest-priority
    condition visible in this one update before debounce/hysteresis.  ``entered``
    is populated only when the output changes.  A successful alert clearance is
    represented by one ``BACK`` result with ``recovered_from`` naming the fault.
    """

    state: NetworkState
    observed: NetworkState
    changed: bool
    entered: NetworkState | None
    recovered_from: NetworkState | None
    loss_percent: float
    loss_failures: int
    loss_samples: int
    high_latency_samples: int
    latency_samples: int
    trigger_streak: int
    clear_streak: int

    @property
    def is_alert(self) -> bool:
        return self.state in FAULT_STATES

    @property
    def is_recovery(self) -> bool:
        return self.state is NetworkState.BACK and self.recovered_from is not None


class ProbeSource(Protocol):
    """Minimal injectable boundary for a live or deterministic sample source."""

    def sample(self) -> ProbeSample:
        """Return the next observation without changing external state."""


class NetworkSentinel:
    """Priority classifier with confirmation and recovery hysteresis."""

    def __init__(self, policy: SentinelPolicy = DEFAULT_POLICY) -> None:
        if not isinstance(policy, SentinelPolicy):
            raise TypeError("policy must be a SentinelPolicy")
        self.policy = policy
        self._active: NetworkState | None = None
        self._last_output = NetworkState.HEALTHY
        self._pending_hard: NetworkState | None = None
        self._pending_count = 0
        self._hard_clear_count = 0
        self._slow_clear_count = 0
        self._losses: deque[bool] = deque(maxlen=policy.loss_window)
        self._latencies: deque[float | None] = deque(maxlen=policy.slow_window)

    @property
    def current_state(self) -> NetworkState:
        """Most recently emitted public state, including a one-shot ``BACK``."""

        return self._last_output

    @property
    def active_fault(self) -> NetworkState | None:
        """Durable fault under hysteresis, excluding ``HEALTHY`` and ``BACK``."""

        return self._active

    def reset(self) -> None:
        """Forget history, as a newly started PC service would."""

        self._active = None
        self._last_output = NetworkState.HEALTHY
        self._pending_hard = None
        self._pending_count = 0
        self._hard_clear_count = 0
        self._slow_clear_count = 0
        self._losses.clear()
        self._latencies.clear()

    def update(self, sample: ProbeSample) -> SentinelDecision:
        """Consume one sample and return the current classified state."""

        if not isinstance(sample, ProbeSample):
            raise TypeError("sample must be a ProbeSample")

        hard = self._hard_observation(sample)

        # Do not turn a confirmed hard outage into a synthetic loss percentage.
        # Fully healthy recovery samples are still retained, so that a lower
        # priority degradation can be selected truthfully after hard recovery.
        if not (self._active in HARD_STATES and hard is not None):
            self._record_path_attempt(sample)

        loss_triggered = self._loss_triggered()
        slow_triggered = self._slow_triggered()
        if hard is not None:
            observed = hard
        elif loss_triggered:
            observed = NetworkState.LOSS
        elif slow_triggered:
            observed = NetworkState.SLOW
        else:
            observed = NetworkState.HEALTHY

        recovered_from: NetworkState | None = None
        output: NetworkState

        if self._active is None:
            output = self._update_while_clear(hard, loss_triggered, slow_triggered)
        elif self._active in HARD_STATES:
            output, recovered_from = self._update_hard_fault(
                hard, loss_triggered, slow_triggered
            )
        elif self._active is NetworkState.LOSS:
            output, recovered_from = self._update_loss_fault(
                sample, hard, slow_triggered
            )
        else:
            output, recovered_from = self._update_slow_fault(sample, hard, loss_triggered)

        return self._decision(output, observed, recovered_from)

    @staticmethod
    def _hard_observation(sample: ProbeSample) -> NetworkState | None:
        # This ordering is a contract: report the earliest broken hop, rather
        # than a cascade of downstream symptoms caused by it.
        if not sample.router_ok:
            return NetworkState.ROUTER
        if sample.wan_ok is False:
            return NetworkState.WAN
        if sample.dns_ok is False:
            return NetworkState.DNS
        if sample.login_required is True:
            return NetworkState.LOGIN
        return None

    def _record_path_attempt(self, sample: ProbeSample) -> None:
        if not sample.router_ok or sample.wan_ok is None:
            return
        failed = not sample.wan_ok
        self._losses.append(failed)
        latency: float | None = None
        if sample.wan_ok and sample.latency_ms is not None:
            latency = float(sample.latency_ms)
        self._latencies.append(latency)

    def _loss_metrics(self) -> tuple[int, int, float]:
        samples = len(self._losses)
        failures = sum(self._losses)
        percent = 100.0 * failures / samples if samples else 0.0
        return failures, samples, percent

    def _latency_metrics(self) -> tuple[int, int]:
        valid = [value for value in self._latencies if value is not None]
        high = sum(value >= self.policy.slow_trigger_ms for value in valid)
        return high, len(valid)

    def _loss_triggered(self) -> bool:
        failures, _samples, percent = self._loss_metrics()
        return (
            failures >= self.policy.loss_min_failures
            and percent >= self.policy.loss_trigger_percent
        )

    def _loss_cleared(self) -> bool:
        _failures, _samples, percent = self._loss_metrics()
        return percent < self.policy.loss_clear_percent

    def _slow_triggered(self) -> bool:
        if len(self._latencies) < self.policy.slow_window:
            return False
        high, _valid = self._latency_metrics()
        return high >= self.policy.slow_min_high

    def _advance_hard_candidate(self, state: NetworkState) -> bool:
        if state not in HARD_STATES:
            raise ValueError("hard candidate must be ROUTER, WAN, DNS, or LOGIN")
        if state is self._pending_hard:
            self._pending_count += 1
        else:
            self._pending_hard = state
            self._pending_count = 1
        return self._pending_count >= self.policy.hard_trigger_count

    def _clear_hard_candidate(self) -> None:
        self._pending_hard = None
        self._pending_count = 0

    def _enter(self, state: NetworkState) -> NetworkState:
        if state not in FAULT_STATES:
            raise ValueError("only fault states can become active")
        self._active = state
        self._clear_hard_candidate()
        self._hard_clear_count = 0
        self._slow_clear_count = 0
        if state in HARD_STATES:
            # Consecutive outage packets should not linger as a LOSS alert after
            # the hard outage has recovered.
            self._losses.clear()
            self._latencies.clear()
        return state

    def _back(self, prior: NetworkState) -> tuple[NetworkState, NetworkState]:
        self._active = None
        self._clear_hard_candidate()
        self._hard_clear_count = 0
        self._slow_clear_count = 0
        return NetworkState.BACK, prior

    def _update_while_clear(
        self,
        hard: NetworkState | None,
        loss_triggered: bool,
        slow_triggered: bool,
    ) -> NetworkState:
        if hard is not None:
            if self._advance_hard_candidate(hard):
                return self._enter(hard)
            return NetworkState.HEALTHY

        self._clear_hard_candidate()
        if loss_triggered:
            return self._enter(NetworkState.LOSS)
        if slow_triggered:
            return self._enter(NetworkState.SLOW)
        return NetworkState.HEALTHY

    def _update_hard_fault(
        self,
        hard: NetworkState | None,
        loss_triggered: bool,
        slow_triggered: bool,
    ) -> tuple[NetworkState, NetworkState | None]:
        assert self._active in HARD_STATES
        active = self._active

        if hard is active:
            self._clear_hard_candidate()
            self._hard_clear_count = 0
            return active, None

        if hard is not None:
            self._hard_clear_count = 0
            if self._advance_hard_candidate(hard):
                return self._enter(hard), None
            return active, None

        self._clear_hard_candidate()
        self._hard_clear_count += 1
        if self._hard_clear_count < self.policy.hard_clear_count:
            return active, None

        # A confirmed hard fault is gone.  If a separately confirmed lower
        # priority condition remains, transition directly rather than flashing
        # a misleading recovery screen between two real problems.
        if loss_triggered:
            return self._enter(NetworkState.LOSS), None
        if slow_triggered:
            return self._enter(NetworkState.SLOW), None
        return self._back(active)

    def _update_loss_fault(
        self,
        sample: ProbeSample,
        hard: NetworkState | None,
        slow_triggered: bool,
    ) -> tuple[NetworkState, NetworkState | None]:
        assert self._active is NetworkState.LOSS
        if hard is not None:
            if self._advance_hard_candidate(hard):
                return self._enter(hard), None
            return NetworkState.LOSS, None

        self._clear_hard_candidate()
        if not self._loss_cleared():
            return NetworkState.LOSS, None
        if slow_triggered:
            return self._enter(NetworkState.SLOW), None
        return self._back(NetworkState.LOSS)

    def _update_slow_fault(
        self,
        sample: ProbeSample,
        hard: NetworkState | None,
        loss_triggered: bool,
    ) -> tuple[NetworkState, NetworkState | None]:
        assert self._active is NetworkState.SLOW
        if hard is not None:
            self._slow_clear_count = 0
            if self._advance_hard_candidate(hard):
                return self._enter(hard), None
            return NetworkState.SLOW, None

        self._clear_hard_candidate()
        if loss_triggered:
            return self._enter(NetworkState.LOSS), None

        if (
            sample.wan_ok is True
            and sample.latency_ms is not None
            and sample.latency_ms < self.policy.slow_clear_ms
        ):
            self._slow_clear_count += 1
        else:
            self._slow_clear_count = 0

        if self._slow_clear_count >= self.policy.slow_clear_count:
            return self._back(NetworkState.SLOW)
        return NetworkState.SLOW, None

    def _decision(
        self,
        output: NetworkState,
        observed: NetworkState,
        recovered_from: NetworkState | None,
    ) -> SentinelDecision:
        changed = output is not self._last_output
        entered = output if changed else None
        failures, samples, loss_percent = self._loss_metrics()
        high, valid_latencies = self._latency_metrics()
        decision = SentinelDecision(
            state=output,
            observed=observed,
            changed=changed,
            entered=entered,
            recovered_from=recovered_from,
            loss_percent=round(loss_percent, 1),
            loss_failures=failures,
            loss_samples=samples,
            high_latency_samples=high,
            latency_samples=valid_latencies,
            trigger_streak=self._pending_count,
            clear_streak=(
                self._hard_clear_count
                if self._active in HARD_STATES
                else self._slow_clear_count
                if self._active is NetworkState.SLOW
                else 0
            ),
        )
        self._last_output = output
        return decision


# ---------------------------------------------------------------------------
# Bounded, read-only Windows-friendly probe helpers


@dataclass(frozen=True)
class ProbeAttempt:
    ok: bool
    latency_ms: float | None = None


@dataclass(frozen=True)
class PortalAttempt:
    reachable: bool
    login_required: bool | None


def _positive_timeout(timeout_seconds: float) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise TypeError("timeout_seconds must be a number")
    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    return timeout


def ping_once(host: str, timeout_seconds: float = 1.0) -> ProbeAttempt:
    """Run one bounded ICMP attempt without a shell.

    Windows uses ``-n 1 -w <milliseconds>``.  The fallback flags keep the
    helper usable by developers on Unix-like systems as well.  If a localized
    ping executable does not expose a parseable ``time=`` value, reachability
    remains valid and latency is left unknown rather than guessed from process
    startup time.
    """

    if not isinstance(host, str) or not host.strip():
        raise ValueError("host must be a non-empty string")
    timeout = _positive_timeout(timeout_seconds)
    if os.name == "nt":
        command = ["ping", "-n", "1", "-w", str(max(1, round(timeout * 1000))), host]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        command = ["ping", "-c", "1", "-W", str(max(1, math.ceil(timeout))), host]
        creationflags = 0
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout + 0.5,
            check=False,
            shell=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ProbeAttempt(False, None)
    if completed.returncode != 0:
        return ProbeAttempt(False, None)
    match = re.search(r"time\s*[=<]\s*(\d+(?:\.\d+)?)\s*ms", completed.stdout, re.I)
    latency = float(match.group(1)) if match else None
    return ProbeAttempt(True, latency)


def discover_default_gateway(
    timeout_seconds: float = 2.0,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Return Windows' lowest-metric active IPv4 default gateway.

    ``Get-NetRoute`` is the operating system's authoritative routing-table
    view. Discovery is read-only, bounded, shell-free, and returns ``None`` on
    any missing or malformed result so callers can use an explicit fallback.
    """

    timeout = _positive_timeout(timeout_seconds)
    if os.name != "nt":
        return None
    script = (
        "$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
        "-AddressFamily IPv4 -ErrorAction Stop | "
        "Where-Object { $_.State -eq 'Alive' -and $_.NextHop -ne '0.0.0.0' } | "
        "Sort-Object @{Expression={[int]$_.RouteMetric + [int]$_.InterfaceMetric} } | "
        "Select-Object -First 1 -ExpandProperty NextHop; "
        "if ($route) { [Console]::Out.Write($route) }"
    )
    try:
        result = runner(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    candidate = result.stdout.strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if address.version != 4 or address.is_unspecified:
        return None
    return str(address)


def tcp_connect_once(
    host: str,
    port: int = 443,
    timeout_seconds: float = 1.0,
) -> ProbeAttempt:
    """Measure one bounded TCP connection attempt and immediately close it."""

    if not isinstance(host, str) or not host.strip():
        raise ValueError("host must be a non-empty string")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 through 65535")
    timeout = _positive_timeout(timeout_seconds)
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return ProbeAttempt(False, None)
    return ProbeAttempt(True, (time.perf_counter() - started) * 1000.0)


def dns_resolves(host: str, timeout_seconds: float = 1.0) -> bool:
    """Resolve a hostname behind a strict caller-side deadline.

    Python's platform resolver has no per-call timeout.  A daemon worker keeps
    a wedged Windows resolver from blocking either the sampling loop or process
    shutdown.  The helper reports timeout as an inconclusive/failed DNS check;
    it never changes resolver configuration or caches.
    """

    if not isinstance(host, str) or not host.strip():
        raise ValueError("host must be a non-empty string")
    timeout = _positive_timeout(timeout_seconds)
    result: queue.Queue[bool] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            socket.getaddrinfo(host, None)
        except OSError:
            ok = False
        else:
            ok = True
        try:
            result.put_nowait(ok)
        except queue.Full:
            pass

    threading.Thread(target=resolve, name="network-sentinel-dns", daemon=True).start()
    try:
        return result.get(timeout=timeout)
    except queue.Empty:
        return False


def probe_captive_portal(
    url: str,
    *,
    expected_status: int,
    expected_body: bytes | None = None,
    timeout_seconds: float = 1.5,
) -> PortalAttempt:
    """Check a known response without following a captive-portal redirect."""

    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("url must use http or https")
    if not isinstance(expected_status, int) or not 100 <= expected_status <= 599:
        raise ValueError("expected_status must be a valid HTTP status")
    timeout = _positive_timeout(timeout_seconds)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
            return None

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AWTRIX-Network-Sentinel/1.0"},
        method="GET",
    )
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.getcode()
            body = response.read(256)
    except urllib.error.HTTPError as exc:
        # Redirects and unexpected HTTP responses prove that a web server was
        # reached, and are the usual shape of a captive login interception.
        return PortalAttempt(True, True if 300 <= exc.code <= 599 else None)
    except (OSError, urllib.error.URLError, TimeoutError):
        return PortalAttempt(False, None)

    matches = status == expected_status
    if expected_body is not None:
        matches = matches and expected_body in body
    return PortalAttempt(True, not matches)


@dataclass(frozen=True)
class SystemProbeConfig:
    """Endpoints for a conservative Windows PC probe chain."""

    router_host: str
    wan_host: str = "1.1.1.1"
    wan_port: int = 443
    dns_name: str = "www.msftconnecttest.com"
    portal_url: str = "http://www.msftconnecttest.com/connecttest.txt"
    portal_status: int = 200
    portal_body: bytes = b"Microsoft Connect Test"
    timeout_seconds: float = 1.0
    wan_targets: tuple[tuple[str, int], ...] | None = None
    dns_interval_seconds: float = 15.0
    portal_interval_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.router_host, str) or not self.router_host.strip():
            raise ValueError("router_host must be a non-empty string")
        if not isinstance(self.wan_host, str) or not self.wan_host.strip():
            raise ValueError("wan_host must be a non-empty string")
        if not isinstance(self.dns_name, str) or not self.dns_name.strip():
            raise ValueError("dns_name must be a non-empty string")
        if not isinstance(self.wan_port, int) or not 1 <= self.wan_port <= 65535:
            raise ValueError("wan_port must be from 1 through 65535")
        if self.wan_targets is not None:
            if len(self.wan_targets) < 2:
                raise ValueError("wan_targets must contain at least two independent endpoints")
            for index, target in enumerate(self.wan_targets):
                if not isinstance(target, tuple) or len(target) != 2:
                    raise ValueError(f"wan_targets[{index}] must be a (host, port) pair")
                host, port = target
                if not isinstance(host, str) or not host.strip():
                    raise ValueError(f"wan_targets[{index}] host must be non-empty")
                if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                    raise ValueError(f"wan_targets[{index}] port must be 1..65535")
        _positive_timeout(self.timeout_seconds)
        _positive_timeout(self.dns_interval_seconds)
        _positive_timeout(self.portal_interval_seconds)

    @property
    def effective_wan_targets(self) -> tuple[tuple[str, int], ...]:
        return self.wan_targets or ((self.wan_host, self.wan_port),)


class SystemProbe:
    """Read-only probe chain whose operations are injectable for tests."""

    def __init__(
        self,
        config: SystemProbeConfig,
        *,
        ping: Callable[[str, float], ProbeAttempt] = ping_once,
        tcp: Callable[[str, int, float], ProbeAttempt] = tcp_connect_once,
        dns: Callable[[str, float], bool] = dns_resolves,
        portal: Callable[..., PortalAttempt] = probe_captive_portal,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(config, SystemProbeConfig):
            raise TypeError("config must be a SystemProbeConfig")
        self.config = config
        self._ping = ping
        self._tcp = tcp
        self._dns = dns
        self._portal = portal
        self._clock = clock
        self._last_dns_at: float | None = None
        self._last_dns_ok: bool | None = None
        self._last_portal_at: float | None = None
        self._last_portal: PortalAttempt | None = None

    def sample(self) -> ProbeSample:
        timeout = self.config.timeout_seconds
        timestamp = self._clock()
        router = self._ping(self.config.router_host, timeout)
        if not router.ok:
            return ProbeSample(False, None, None, None, None, timestamp)

        attempts = [
            self._tcp(host, port, timeout)
            for host, port in self.config.effective_wan_targets
        ]
        successes = [attempt for attempt in attempts if attempt.ok]
        if not successes:
            return ProbeSample(True, False, None, None, None, timestamp)

        known_latencies = [
            float(attempt.latency_ms)
            for attempt in successes
            if attempt.latency_ms is not None
        ]
        latency = min(known_latencies) if known_latencies else None

        if (
            self._last_dns_at is None
            or timestamp - self._last_dns_at >= self.config.dns_interval_seconds
        ):
            dns_ok = self._dns(self.config.dns_name, timeout)
            self._last_dns_at = timestamp
            self._last_dns_ok = dns_ok
        else:
            dns_ok = self._last_dns_ok
        assert dns_ok is not None
        if not dns_ok:
            return ProbeSample(True, True, False, None, latency, timestamp)

        if (
            self._last_portal_at is None
            or timestamp - self._last_portal_at >= self.config.portal_interval_seconds
        ):
            portal = self._portal(
                self.config.portal_url,
                expected_status=self.config.portal_status,
                expected_body=self.config.portal_body,
                timeout_seconds=timeout,
            )
            self._last_portal_at = timestamp
            self._last_portal = portal
        else:
            portal = self._last_portal
        assert portal is not None
        return ProbeSample(
            True,
            True,
            True,
            portal.login_required if portal.reachable else None,
            latency,
            timestamp,
        )


__all__ = [
    "DEFAULT_POLICY",
    "FAULT_STATES",
    "HARD_STATES",
    "NetworkSentinel",
    "NetworkState",
    "PortalAttempt",
    "ProbeAttempt",
    "ProbeSample",
    "ProbeSource",
    "SentinelDecision",
    "SentinelPolicy",
    "SystemProbe",
    "SystemProbeConfig",
    "discover_default_gateway",
    "dns_resolves",
    "ping_once",
    "probe_captive_portal",
    "tcp_connect_once",
]

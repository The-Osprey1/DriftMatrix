"""Core Event Bus and Unified Data Contracts for the Drift Hardware Ecosystem.

Provides a thread-safe, decoupled event publisher/subscriber bus allowing
DriftPad, Drift-Watch, DriftMatrix, and PC telemetry services to react
seamlessly to cross-device hardware triggers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import queue
import threading
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("drift_link_core")


class DriftEventType(str, Enum):
    """Standardized event types for cross-device telemetry and actions."""
    MACRO_TRIGGERED = "macro_triggered"
    SCENE_CHANGED = "scene_changed"
    FOCUS_STATE_CHANGED = "focus_state_changed"
    MEDIA_TRACK_CHANGED = "media_track_changed"
    ALERT_BROADCAST = "alert_broadcast"
    DEVICE_CONNECTED = "device_connected"
    DEVICE_DISCONNECTED = "device_disconnected"


@dataclass(frozen=True)
class DriftEvent:
    """Immutable event envelope passed through the Drift Event Bus."""
    event_type: DriftEventType
    source: str
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class DriftEventBus:
    """Thread-safe asynchronous Event Bus for the Drift ecosystem."""

    def __init__(self):
        self._subscribers: Dict[DriftEventType, List[Callable[[DriftEvent], None]]] = {}
        self._global_subscribers: List[Callable[[DriftEvent], None]] = []
        self._lock = threading.RLock()
        self._queue: queue.Queue[Optional[DriftEvent]] = queue.Queue(maxsize=1000)
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False

    def subscribe(
        self,
        event_type: Optional[DriftEventType],
        callback: Callable[[DriftEvent], None],
    ) -> None:
        """Register a callback for a specific event type, or all events if None."""
        with self._lock:
            if event_type is None:
                if callback not in self._global_subscribers:
                    self._global_subscribers.append(callback)
            else:
                if event_type not in self._subscribers:
                    self._subscribers[event_type] = []
                if callback not in self._subscribers[event_type]:
                    self._subscribers[event_type].append(callback)

    def unsubscribe(
        self,
        event_type: Optional[DriftEventType],
        callback: Callable[[DriftEvent], None],
    ) -> None:
        """Unregister a callback."""
        with self._lock:
            if event_type is None:
                if callback in self._global_subscribers:
                    self._global_subscribers.remove(callback)
            elif event_type in self._subscribers:
                if callback in self._subscribers[event_type]:
                    self._subscribers[event_type].remove(callback)

    def publish(self, event: DriftEvent) -> None:
        """Publish an event to the queue for asynchronous dispatch."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning(f"DriftEventBus queue full; dropping event: {event.event_type}")

    def publish_sync(self, event: DriftEvent) -> None:
        """Publish an event and dispatch to all listeners immediately (blocking)."""
        self._dispatch(event)

    def _dispatch(self, event: DriftEvent) -> None:
        with self._lock:
            callbacks = list(self._subscribers.get(event.event_type, []))
            globals_ = list(self._global_subscribers)

        for cb in callbacks + globals_:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Error in DriftEventBus handler {cb}: {e}", exc_info=True)

    def start(self) -> None:
        """Start the background worker thread for event dispatch."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._worker_thread = threading.Thread(
                target=self._run_worker, name="DriftEventBusWorker", daemon=True
            )
            self._worker_thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the background worker thread."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._queue.put(None)  # Sentinel to unblock get()

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)

    def _run_worker(self) -> None:
        while self._running:
            try:
                event = self._queue.get(timeout=0.2)
                if event is None:
                    break
                self._dispatch(event)
                self._queue.task_done()
            except queue.Empty:
                continue

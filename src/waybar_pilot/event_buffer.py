"""Bounded buffering for controller events."""

from collections import deque
from queue import Empty
import threading

from .hyprland import HyprlandEvent


class EventBuffer:
    """Bounded event buffer that coalesces redundant Hyprland events."""

    def __init__(self, maxsize: int = 256):
        self._events: deque[object] = deque()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def put(self, event: object) -> None:
        """Append an event without blocking producers."""
        with self._lock:
            if isinstance(event, HyprlandEvent):
                for index, queued in enumerate(self._events):
                    if (
                        isinstance(queued, HyprlandEvent)
                        and queued.event_type == event.event_type
                    ):
                        del self._events[index]
                        break

            if len(self._events) >= self._maxsize:
                self._discard_oldest_hyprland_event()
            if len(self._events) >= self._maxsize:
                self._events.popleft()
            self._events.append(event)

    def _discard_oldest_hyprland_event(self) -> None:
        """Prefer retaining ordered cursor transitions when making room."""
        for index, queued in enumerate(self._events):
            if isinstance(queued, HyprlandEvent):
                del self._events[index]
                return

    def get_nowait(self) -> object:
        """Return the oldest event or raise Empty."""
        with self._lock:
            if not self._events:
                raise Empty
            return self._events.popleft()

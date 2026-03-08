"""Cursor event dataclasses for event-driven cursor detection."""

from dataclasses import dataclass
from enum import Enum, auto


class CursorEventType(Enum):
    """Types of cursor events."""

    ENTER = auto()
    LEAVE = auto()


@dataclass(frozen=True)
class CursorEvent:
    """Base cursor event."""

    event_type: CursorEventType
    monitor_id: int
    monitor_name: str


@dataclass(frozen=True)
class CursorEnter(CursorEvent):
    """Cursor entered the sensor zone."""

    def __init__(self, monitor_id: int, monitor_name: str):
        super().__init__(CursorEventType.ENTER, monitor_id, monitor_name)


@dataclass(frozen=True)
class CursorLeave(CursorEvent):
    """Cursor left the sensor zone."""

    exit_y: int  # Y coordinate when leaving sensor (for hysteresis)

    def __init__(self, monitor_id: int, monitor_name: str, exit_y: int):
        super().__init__(CursorEventType.LEAVE, monitor_id, monitor_name)
        object.__setattr__(self, "exit_y", exit_y)

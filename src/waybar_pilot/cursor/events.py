"""Cursor event dataclasses for event-driven cursor detection."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CursorEnter:
    """Cursor entered the sensor zone."""

    monitor_id: int
    monitor_name: str


@dataclass(frozen=True)
class CursorLeave:
    """Cursor left the sensor zone."""

    monitor_id: int
    monitor_name: str

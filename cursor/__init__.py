"""Cursor detection module using GTK Layer Shell for event-driven cursor tracking."""

from cursor.events import CursorEnter, CursorLeave, CursorEventType
from cursor.manager import CursorManager
from cursor.sensor import CursorSensor

__all__ = [
    "CursorEnter",
    "CursorLeave",
    "CursorEventType",
    "CursorManager",
    "CursorSensor",
]

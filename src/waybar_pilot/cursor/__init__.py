"""Cursor detection module using GTK Layer Shell for event-driven cursor tracking."""

from .events import CursorEnter, CursorLeave, CursorEventType
from .manager import CursorManager
from .sensor import CursorSensor

__all__ = [
    "CursorEnter",
    "CursorLeave",
    "CursorEventType",
    "CursorManager",
    "CursorSensor",
]

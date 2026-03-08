"""Hyprland integration package for waybar-pilot."""

from .client import HyprlandClient, HyprlandConnectionError, HyprlandError
from .fullscreen_handler import FullscreenHandler
from .models import Client, CursorPosition, Monitor, Workspace
from .socket2 import EventType, HyprlandEvent, Socket2Listener

__all__ = [
    # Client
    "HyprlandClient",
    "HyprlandConnectionError",
    "HyprlandError",
    # Fullscreen handler
    "FullscreenHandler",
    # Models
    "Client",
    "CursorPosition",
    "Monitor",
    "Workspace",
    # Socket2
    "EventType",
    "HyprlandEvent",
    "Socket2Listener",
]

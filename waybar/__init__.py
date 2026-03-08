"""Waybar process management package for waybar-pilot."""

from .instance import WaybarInstance
from .manager import WaybarManager

__all__ = [
    "WaybarInstance",
    "WaybarManager",
]

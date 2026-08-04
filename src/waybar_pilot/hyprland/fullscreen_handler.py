"""Fullscreen handler for disabling cursor sensors during fullscreen."""

from dataclasses import dataclass, field
import logging
from typing import Dict, List, Optional, Set
import time

from .models import Client, Monitor

log = logging.getLogger("waybar-pilot")


@dataclass
class FullscreenState:
    """Tracks fullscreen state for a monitor."""

    monitor_id: int
    monitor_name: str
    is_fullscreen: bool = False
    fullscreen_workspaces: Set[int] = field(default_factory=set)
    last_change_time: float = field(default_factory=time.time)


class FullscreenHandler:
    """Tracks fullscreen state per monitor and manages sensor visibility.

    When a window goes fullscreen on a monitor:
    1. Sensor is disabled (hidden)
    2. Waybar should be hidden
    3. No cursor detection until fullscreen exits
    """

    def __init__(self):
        """Initialize fullscreen handler."""
        self._states: Dict[int, FullscreenState] = {}  # monitor_id -> state

    def get_or_create_state(
        self, monitor_id: int, monitor_name: str
    ) -> FullscreenState:
        """Get or create state for a monitor."""
        if monitor_id not in self._states:
            self._states[monitor_id] = FullscreenState(
                monitor_id=monitor_id,
                monitor_name=monitor_name,
            )
        return self._states[monitor_id]

    def remove_monitor(self, monitor_id: int) -> None:
        """Remove state for a monitor (e.g., when monitor is disconnected)."""
        removed = self._states.pop(monitor_id, None)
        if removed:
            log.debug(
                "Removed fullscreen state for monitor %s (was fullscreen=%s)",
                monitor_id,
                removed.is_fullscreen,
            )

    def update_from_clients(
        self,
        clients: List[Client],
        monitors: List[Monitor],
    ) -> None:
        """Update fullscreen state from current client list.

        Records every workspace that has a fullscreen window, keyed by
        monitor. Hidden windows are excluded because that is an explicit
        user action rather than a transient workspace transition.

        The active-workspace mapping is NOT used here either. It comes
        from a separate ``hyprctl -j monitors`` call that can be out of
        sync with ``hyprctl -j clients``. By recording all fullscreen
        workspaces and deferring the active-workspace check to
        ``is_fullscreen``, we decouple the two independently-sourced
        facts and eliminate the race.

        Args:
            clients: Current list of window clients
            monitors: Current list of monitors
        """
        # Build monitor_id -> set of fullscreen workspace IDs
        fullscreen_workspaces_by_monitor: Dict[int, Set[int]] = {}

        for client in clients:
            if not client.fullscreen or client.hidden:
                continue

            fullscreen_workspaces_by_monitor.setdefault(client.monitor_id, set()).add(
                client.workspace_id
            )

        # Update state for all known monitors
        for monitor in monitors:
            state = self.get_or_create_state(monitor.id, monitor.name)

            new_workspaces = fullscreen_workspaces_by_monitor.get(monitor.id, set())
            was_fullscreen = state.is_fullscreen
            is_fullscreen = bool(new_workspaces)

            if new_workspaces != state.fullscreen_workspaces:
                state.fullscreen_workspaces = new_workspaces
                state.is_fullscreen = is_fullscreen
                state.last_change_time = time.time()
                log.info(
                    "Monitor %s (%s): fullscreen %s -> %s (workspaces=%s)",
                    monitor.id,
                    monitor.name,
                    was_fullscreen,
                    is_fullscreen,
                    sorted(new_workspaces),
                )

    def is_fullscreen(
        self, monitor_id: int, active_workspace_id: Optional[int] = None
    ) -> bool:
        """Check if a monitor is currently in fullscreen mode.

        If active_workspace_id is provided, only returns True if a
        fullscreen window exists on that workspace. This prevents
        disabling waybar when switching to a different workspace on the
        same monitor that has no fullscreen window.

        Args:
            monitor_id: Monitor ID to check
            active_workspace_id: Optional ID of the currently active workspace on this monitor

        Returns:
            True if fullscreen window is active on this monitor (and optionally, on the active workspace)
        """
        state = self._states.get(monitor_id)
        if not state or not state.is_fullscreen:
            return False

        # If no active workspace specified, return the raw fullscreen state
        if active_workspace_id is None:
            return state.is_fullscreen

        # Only consider it fullscreen if the active workspace has one
        return active_workspace_id in state.fullscreen_workspaces

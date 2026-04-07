"""Fullscreen handler for disabling cursor sensors during fullscreen."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
import time

from .models import Client, Monitor


@dataclass
class FullscreenState:
    """Tracks fullscreen state for a monitor."""

    monitor_id: int
    monitor_name: str
    is_fullscreen: bool = False
    fullscreen_client: Optional[str] = None  # client address
    fullscreen_workspace_id: Optional[int] = None  # workspace with fullscreen
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

    def get_or_create_state(self, monitor_id: int, monitor_name: str) -> FullscreenState:
        """Get or create state for a monitor."""
        if monitor_id not in self._states:
            self._states[monitor_id] = FullscreenState(
                monitor_id=monitor_id,
                monitor_name=monitor_name,
            )
        return self._states[monitor_id]

    def remove_monitor(self, monitor_id: int) -> None:
        """Remove state for a monitor (e.g., when monitor is disconnected)."""
        self._states.pop(monitor_id, None)

    def update_from_clients(self, clients: List[Client], monitors: List[Monitor]) -> None:
        """Update fullscreen state from current client list.

        This should be called when processing window-related events to
        ensure fullscreen state is accurate.

        Args:
            clients: Current list of window clients
            monitors: Current list of monitors
        """
        # Build set of monitors with fullscreen clients and their workspaces
        fullscreen_by_monitor: Dict[int, tuple] = {}  # monitor_id -> (client_address, workspace_id)

        for client in clients:
            if client.fullscreen and client.mapped and not client.hidden:
                # Client is fullscreen on this monitor
                fullscreen_by_monitor[client.monitor_id] = (client.address, client.workspace_id)

        # Update state for all known monitors
        for monitor in monitors:
            state = self.get_or_create_state(monitor.id, monitor.name)

            was_fullscreen = state.is_fullscreen
            is_fullscreen = monitor.id in fullscreen_by_monitor

            if is_fullscreen != was_fullscreen:
                state.is_fullscreen = is_fullscreen
                if is_fullscreen:
                    client_addr, workspace_id = fullscreen_by_monitor[monitor.id]
                    state.fullscreen_client = client_addr
                    state.fullscreen_workspace_id = workspace_id
                else:
                    state.fullscreen_client = None
                    state.fullscreen_workspace_id = None
                state.last_change_time = time.time()

    def is_fullscreen(self, monitor_id: int, active_workspace_id: Optional[int] = None) -> bool:
        """Check if a monitor is currently in fullscreen mode.

        If active_workspace_id is provided, only returns True if the fullscreen
        window is on the active workspace. This prevents disabling waybar when
        switching to a different workspace on the same monitor.

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
        
        # Only consider it fullscreen if it's on the active workspace
        return state.fullscreen_workspace_id == active_workspace_id

    def get_fullscreen_monitors(self) -> Set[int]:
        """Get set of monitor IDs currently in fullscreen mode."""
        return {
            monitor_id
            for monitor_id, state in self._states.items()
            if state.is_fullscreen
        }

    def get_state_changes(self) -> Dict[int, bool]:
        """Get monitors that changed fullscreen state since last check.

        Returns:
            Dict mapping monitor_id to new fullscreen state (True=entered, False=exited)
        """
        # This could be expanded to track actual changes if needed
        # For now, just return current states
        return {
            monitor_id: state.is_fullscreen
            for monitor_id, state in self._states.items()
        }

    def reset(self) -> None:
        """Reset all fullscreen states (e.g., on reconnection)."""
        self._states.clear()

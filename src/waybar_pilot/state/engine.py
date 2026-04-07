"""State management for waybar visibility decisions."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
import time

from ..config import Config, WaybarState
from ..hyprland.models import Client, CursorPosition, Monitor


@dataclass
class MonitorState:
    """Tracks state for a single monitor.

    Maintains current visibility state and transition history.
    """

    monitor_id: int
    current_state: WaybarState = field(default=WaybarState.VISIBLE)
    last_transition_time: float = field(default_factory=time.time)
    transition_count: int = field(default=0)

    def transition_to(self, new_state: WaybarState) -> bool:
        """Record a state transition.

        Args:
            new_state: State to transition to

        Returns:
            True if transition occurred, False if already in that state
        """
        if new_state == self.current_state:
            return False

        self.current_state = new_state
        self.last_transition_time = time.time()
        self.transition_count += 1
        return True

    @property
    def time_in_current_state(self) -> float:
        """Seconds spent in current state."""
        return time.time() - self.last_transition_time


class StateEngine:
    """Pure logic engine for determining waybar visibility.

    This class has no side effects - it only makes decisions based on
    the current state of the system (cursor position, windows, etc.).
    """

    def __init__(self, config: Config):
        """Initialize the state engine.

        Args:
            config: Application configuration
        """
        self._config = config
        self._monitor_states: Dict[int, MonitorState] = {}

    def get_or_create_monitor_state(self, monitor_id: int) -> MonitorState:
        """Get existing state or create new one.

        Args:
            monitor_id: Monitor ID

        Returns:
            MonitorState for this monitor
        """
        if monitor_id not in self._monitor_states:
            self._monitor_states[monitor_id] = MonitorState(
                monitor_id=monitor_id,
                current_state=self._config.initial_state,
            )
        return self._monitor_states[monitor_id]

    def remove_monitor_state(self, monitor_id: int) -> None:
        """Remove state tracking for a monitor.

        Args:
            monitor_id: Monitor ID
        """
        self._monitor_states.pop(monitor_id, None)

    def should_show(
        self,
        monitor_id: int,
        cursor_monitor: Optional[int],
        cursor_position: CursorPosition,
        overlapping_clients: List[Client],
        cursor_in_sensor_zone: bool = False,
        is_fullscreen: bool = False,
        is_autohide_monitor: bool = True,
        is_show_monitor: bool = False,
    ) -> bool:
        """Determine if waybar should be visible on a monitor.

        This is the main decision function implementing the visibility logic:
        1. Fullscreen monitors: never visible
        2. Always-show monitors: always visible
        3. Autohide monitors: visible if cursor in sensor zone or no windows overlapping

        Args:
            monitor_id: Monitor to check
            cursor_monitor: Which monitor the cursor is on (if any)
            cursor_position: Current cursor position
            overlapping_clients: Clients overlapping the bar area
            cursor_in_sensor_zone: Whether cursor is in sensor zone (event-driven mode)
            is_fullscreen: Whether monitor is in fullscreen mode

        Returns:
            True if waybar should be visible
        """
        # Never show during fullscreen
        if is_fullscreen:
            return False

        # Always show on show monitors
        if is_show_monitor:
            return True

        # If not an autohide monitor, default to visible
        if not is_autohide_monitor:
            return True

        # Check if cursor is in sensor zone (event-driven mode)
        if cursor_in_sensor_zone and cursor_monitor == monitor_id:
            return True

        # Check if windows overlap the bar
        if overlapping_clients:
            return False

        # Default: not visible
        return False

    def find_overlapping_clients(
        self,
        clients: List[Client],
        monitor_id: int,
        active_workspace_ids: List[int],
    ) -> List[Client]:
        """Find clients that overlap the bar area.

        Args:
            clients: All clients to check
            monitor_id: Monitor ID to check
            active_workspace_ids: Currently active workspace IDs

        Returns:
            List of clients overlapping the bar
        """
        overlapping = []
        bar_top = 0
        bar_bottom = self._config.total_detection_height

        for client in clients:
            # Skip invalid clients
            if not client.mapped or client.hidden or client.fullscreen:
                continue

            # Wrong monitor
            if client.monitor_id != monitor_id:
                continue

            # Wrong workspace
            if client.workspace_id not in active_workspace_ids:
                continue

            # Check Y overlap
            if client.overlaps_y_range(bar_top, bar_bottom):
                overlapping.append(client)

        return overlapping

    def get_cursor_monitor(
        self,
        cursor: CursorPosition,
        monitors: List[Monitor],
    ) -> Optional[int]:
        """Determine which monitor the cursor is on.

        Args:
            cursor: Cursor position
            monitors: Available monitors

        Returns:
            Monitor ID if on a monitor, None otherwise
        """
        for monitor in monitors:
            if monitor.contains_point(cursor.x, cursor.y):
                return monitor.id
        return None

    def decide_transitions(
        self,
        managed_monitor_ids: List[int],
        cursor: CursorPosition,
        monitors: List[Monitor],
        clients: List[Client],
        active_workspace_ids: List[int],
        active_workspaces_by_monitor: Optional[Dict[int, int]] = None,
        cursor_in_sensor_zone: Optional[Dict[int, bool]] = None,
        autohide_monitor_ids: Optional[Set[int]] = None,
        show_monitor_ids: Optional[Set[int]] = None,
        monitor_lists_configured: bool = False,
        fullscreen_handler=None,
    ) -> List[Tuple[int, WaybarState, WaybarState]]:
        """Decide all state transitions for managed monitors.

        This is the main orchestration method that:
        1. Finds which monitor the cursor is on
        2. Finds overlapping clients per monitor
        3. Decides visibility for each managed monitor
        4. Returns list of (monitor_id, old_state, new_state) tuples

        Args:
            managed_monitor_ids: IDs of monitors being managed
            cursor: Current cursor position
            monitors: All available monitors
            clients: All window clients
            active_workspace_ids: Active workspace IDs
            active_workspaces_by_monitor: Dict of monitor_id -> active workspace ID
            cursor_in_sensor_zone: Dict of monitor_id -> bool for sensor zone state
            fullscreen_handler: FullscreenHandler instance to check fullscreen state

        Returns:
            List of (monitor_id, old_state, new_state) for transitions
        """
        transitions = []

        # Find cursor monitor
        cursor_monitor = self.get_cursor_monitor(cursor, monitors)

        # Default empty dicts
        if cursor_in_sensor_zone is None:
            cursor_in_sensor_zone = {}
        if active_workspaces_by_monitor is None:
            active_workspaces_by_monitor = {}
        if autohide_monitor_ids is None:
            autohide_monitor_ids = set()
        if show_monitor_ids is None:
            show_monitor_ids = set()

        # Group clients by monitor for efficiency
        clients_by_monitor: Dict[int, List[Client]] = {}
        for monitor_id in managed_monitor_ids:
            overlapping = self.find_overlapping_clients(
                clients,
                monitor_id,
                active_workspace_ids,
            )
            clients_by_monitor[monitor_id] = overlapping

        # Decide for each managed monitor
        for monitor_id in managed_monitor_ids:
            monitor_state = self.get_or_create_monitor_state(monitor_id)
            old_state = monitor_state.current_state

            # Check fullscreen state for the active workspace on this monitor
            is_fullscreen = False
            if fullscreen_handler:
                active_workspace = active_workspaces_by_monitor.get(monitor_id)
                is_fullscreen = fullscreen_handler.is_fullscreen(
                    monitor_id, active_workspace
                )

            # Check if cursor is in sensor zone for this monitor
            in_sensor = cursor_in_sensor_zone.get(monitor_id, False)

            is_show_monitor = monitor_id in show_monitor_ids or (
                monitor_lists_configured and monitor_id not in autohide_monitor_ids
            )
            is_autohide_monitor = (
                not monitor_lists_configured or monitor_id in autohide_monitor_ids
            )

            # Make decision
            should_show = self.should_show(
                monitor_id,
                cursor_monitor,
                cursor,
                clients_by_monitor.get(monitor_id, []),
                cursor_in_sensor_zone=in_sensor,
                is_fullscreen=is_fullscreen,
                is_autohide_monitor=is_autohide_monitor,
                is_show_monitor=is_show_monitor,
            )

            new_state = WaybarState.VISIBLE if should_show else WaybarState.HIDDEN

            # Record transition if changed
            if monitor_state.transition_to(new_state):
                transitions.append((monitor_id, old_state, new_state))

        return transitions

    def get_all_states(self) -> Dict[int, WaybarState]:
        """Get current state for all tracked monitors.

        Returns:
            Dict mapping monitor_id to current state
        """
        return {
            monitor_id: state.current_state
            for monitor_id, state in self._monitor_states.items()
        }

    def reset(self) -> None:
        """Reset all monitor states to initial state."""
        for state in self._monitor_states.values():
            state.current_state = self._config.initial_state
            state.last_transition_time = time.time()

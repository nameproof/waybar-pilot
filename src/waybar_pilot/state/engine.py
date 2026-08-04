"""State management for waybar visibility decisions."""

from typing import Dict, List, Optional, Set

from ..config import WaybarState
from ..hyprland.models import CursorPosition, Monitor


class StateEngine:
    """Pure logic engine for determining waybar visibility.

    This class has no side effects; managed Waybar instances own actual
    visibility while the engine computes desired visibility.
    """

    def should_show(
        self,
        cursor_in_sensor_zone: bool = False,
        is_fullscreen: bool = False,
        is_autohide_monitor: bool = True,
        is_show_monitor: bool = False,
    ) -> bool:
        """Determine if waybar should be visible on a monitor.

        This is the main decision function implementing the visibility logic:
        1. Fullscreen monitors: never visible
        2. Always-show monitors: always visible
        3. Autohide monitors: visible only while the cursor is in the sensor zone

        Args:
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
        if cursor_in_sensor_zone:
            return True

        # Default: not visible
        return False

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

    def decide_states(
        self,
        managed_monitor_ids: List[int],
        active_workspaces_by_monitor: Optional[Dict[int, int]] = None,
        cursor_in_sensor_zone: Optional[Dict[int, bool]] = None,
        autohide_monitor_ids: Optional[Set[int]] = None,
        show_monitor_ids: Optional[Set[int]] = None,
        monitor_lists_configured: bool = False,
        fullscreen_handler=None,
    ) -> Dict[int, WaybarState]:
        """Decide desired visibility for all managed monitors.

        This is the main orchestration method that:
        1. Decides visibility for each managed monitor
        2. Returns desired state by monitor ID

        Args:
            managed_monitor_ids: IDs of monitors being managed
            active_workspaces_by_monitor: Dict of monitor_id -> active workspace ID
            cursor_in_sensor_zone: Dict of monitor_id -> bool for sensor zone state
            fullscreen_handler: FullscreenHandler instance to check fullscreen state

        Returns:
            Desired state keyed by monitor ID
        """
        desired_states = {}

        # Default empty dicts
        if cursor_in_sensor_zone is None:
            cursor_in_sensor_zone = {}
        if active_workspaces_by_monitor is None:
            active_workspaces_by_monitor = {}
        if autohide_monitor_ids is None:
            autohide_monitor_ids = set()
        if show_monitor_ids is None:
            show_monitor_ids = set()

        # Decide for each managed monitor
        for monitor_id in managed_monitor_ids:
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
                cursor_in_sensor_zone=in_sensor,
                is_fullscreen=is_fullscreen,
                is_autohide_monitor=is_autohide_monitor,
                is_show_monitor=is_show_monitor,
            )

            desired_states[monitor_id] = (
                WaybarState.VISIBLE if should_show else WaybarState.HIDDEN
            )

        return desired_states

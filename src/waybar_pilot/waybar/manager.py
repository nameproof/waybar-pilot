"""Waybar manager for handling multiple monitor instances."""

import logging
from typing import Dict, Iterator, List, Optional

from ..config import Config, WaybarState
from ..hyprland import Monitor
from .instance import WaybarInstance

log = logging.getLogger("waybar-pilot")


class WaybarManager:
    """Manages waybar instances across multiple monitors.

    Handles:
    - Starting/killing waybar per monitor
    - Tracking which monitors have waybar
    - Bulk operations (kill all, restart all)
    - Process health monitoring
    """

    def __init__(self, config: Config):
        """Initialize the manager.

        Args:
            config: Application configuration
        """
        self._config = config
        self._instances: Dict[int, WaybarInstance] = {}

    def has_external_waybars(self) -> bool:
        """Quick check: any waybar process not managed by us."""
        import subprocess

        try:
            result = subprocess.run(
                ["pgrep", "-x", "waybar"], capture_output=True, text=True
            )
            if result.returncode != 0:
                return False
            total_pids = len(
                [ln for ln in result.stdout.strip().split("\n") if ln.strip()]
            )
            return total_pids > len(self._instances)
        except Exception:
            return False

    def kill_all_external_waybars(self) -> int:
        """Kill all externally-started waybar processes.

        Compares all waybar PIDs against our managed instances. Any PID
        not in our set is considered external and killed.
        """
        import subprocess
        import os
        import signal

        # Collect managed PIDs
        managed_pids: set[int] = set()
        for instance in self._instances.values():
            try:
                managed_pids.add(instance.pid)
            except RuntimeError:
                pass

        killed = 0
        try:
            result = subprocess.run(
                ["pgrep", "-x", "waybar"], capture_output=True, text=True
            )
            if result.returncode != 0:
                return 0

            for line in result.stdout.strip().split("\n"):
                pid_str = line.strip()
                if not pid_str:
                    continue
                try:
                    pid = int(pid_str)
                except ValueError:
                    continue

                if pid in managed_pids:
                    continue

                try:
                    os.kill(pid, signal.SIGTERM)
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    killed += 1
                except (ProcessLookupError, PermissionError):
                    pass
        except Exception as e:
            log.warning(f"Error in kill_all_external_waybars: {e}")

        if killed > 0:
            log.info(f"Killed {killed} external waybar(s)")
        return killed

    def start_for_monitor(self, monitor: Monitor) -> WaybarInstance:
        """Start waybar for a specific monitor.

        Args:
            monitor: Monitor to start waybar on

        Returns:
            The created WaybarInstance

        Raises:
            RuntimeError: If waybar is already running for this monitor
        """
        if monitor.id in self._instances:
            raise RuntimeError(f"Waybar already running for monitor {monitor.id}")

        log.info(f"Starting waybar for monitor {monitor.name} (ID {monitor.id})")

        # Kill any external waybars before starting ours
        killed = self.kill_all_external_waybars()
        if killed > 0:
            log.info(f"Killed {killed} external waybar(s)")

        instance = WaybarInstance(
            monitor_id=monitor.id,
            monitor_name=monitor.name,
            config=self._config,
            # initial_state will be set by controller based on monitor type
        )
        self._instances[monitor.id] = instance
        log.info(
            f"Started managed waybar for monitor {monitor.name} (PID {instance.pid})"
        )
        return instance

    def get_instance(self, monitor_id: int) -> Optional[WaybarInstance]:
        """Get waybar instance for a monitor.

        Args:
            monitor_id: Monitor ID

        Returns:
            WaybarInstance if running, None otherwise
        """
        return self._instances.get(monitor_id)

    def has_instance(self, monitor_id: int) -> bool:
        """Check if waybar is running on a monitor.

        Args:
            monitor_id: Monitor ID

        Returns:
            True if waybar is running
        """
        return monitor_id in self._instances

    def kill_monitor(self, monitor_id: int) -> bool:
        """Kill waybar for a specific monitor.

        Args:
            monitor_id: Monitor ID

        Returns:
            True if killed, False if not running
        """
        if monitor_id not in self._instances:
            return False

        self._instances[monitor_id].kill()
        del self._instances[monitor_id]
        return True

    def kill_all(self) -> None:
        """Kill all waybar instances."""
        for instance in list(self._instances.values()):
            instance.kill()
        self._instances.clear()

    def check_health(self) -> List[int]:
        """Check which instances have died.

        Returns:
            List of monitor IDs that need restart
        """
        dead_monitors = []
        for monitor_id, instance in list(self._instances.items()):
            if not instance.is_alive():
                dead_monitors.append(monitor_id)
                # Cleanup the dead instance
                instance._cleanup()
                del self._instances[monitor_id]

        return dead_monitors

    def restart_dead_instances(
        self, available_monitors: List[Monitor]
    ) -> List[WaybarInstance]:
        """Restart any dead waybar instances.

        Args:
            available_monitors: List of currently available monitors

        Returns:
            List of restarted instances
        """
        restarted = []
        dead_ids = self.check_health()

        # If any instances died, kill all external waybars first
        # This handles the case where external tools (like omarchy) restart waybar
        # which creates a global waybar instance that shows on all monitors
        if dead_ids:
            log.info(
                f"Detected {len(dead_ids)} dead waybar instances, killing all external waybars"
            )
            self.kill_all_external_waybars()

        for monitor_id in dead_ids:
            # Find the monitor in available list
            monitor = next((m for m in available_monitors if m.id == monitor_id), None)
            if monitor:
                try:
                    instance = self.start_for_monitor(monitor)
                    restarted.append(instance)
                except RuntimeError:
                    pass  # Already started by someone else

        return restarted

    def get_all_instances(self) -> Iterator[WaybarInstance]:
        """Iterate over all running instances.

        Yields:
            WaybarInstance objects
        """
        yield from self._instances.values()

    def get_all_ids(self) -> List[int]:
        """Get all monitor IDs with running waybar.

        Returns:
            List of monitor IDs
        """
        return list(self._instances.keys())

    def get_state(self, monitor_id: int) -> Optional[WaybarState]:
        """Get current state for a monitor.

        Args:
            monitor_id: Monitor ID

        Returns:
            Current state if running, None otherwise
        """
        instance = self._instances.get(monitor_id)
        return instance.state if instance else None

    def set_state(self, monitor_id: int, state: WaybarState) -> bool:
        """Set state for a monitor (updates tracking, doesn't toggle).

        Args:
            monitor_id: Monitor ID
            state: New state

        Returns:
            True if updated, False if monitor not found
        """
        instance = self._instances.get(monitor_id)
        if instance:
            instance.state = state
            return True
        return False

    def toggle_monitor(self, monitor_id: int) -> bool:
        """Toggle waybar visibility for a monitor.

        Args:
            monitor_id: Monitor ID

        Returns:
            True if toggled, False if not running
        """
        instance = self._instances.get(monitor_id)
        if instance:
            instance.toggle()
            return True
        return False

    def __len__(self) -> int:
        """Number of running waybar instances."""
        return len(self._instances)

    def __contains__(self, monitor_id: int) -> bool:
        """Check if monitor has waybar running."""
        return monitor_id in self._instances

    def __iter__(self) -> Iterator[WaybarInstance]:
        """Iterate over all instances."""
        return iter(self._instances.values())

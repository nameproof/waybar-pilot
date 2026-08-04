"""Waybar manager for handling multiple monitor instances."""

import logging
from typing import Dict, List, Optional

from ..config import BarPosition
from ..hyprland import Monitor
from ..processes import find_named_pids, terminate_pids
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

    def __init__(self):
        """Initialize the manager."""
        self._instances: Dict[int, WaybarInstance] = {}

    def validate_bar_position(self, requested: BarPosition) -> None:
        """Verify that every configured bar uses the requested screen edge."""
        config_path = WaybarInstance.resolve_base_config()
        config_content: dict | list = {}
        if config_path is not None:
            config_content = WaybarInstance.load_base_config(config_path)
        elif requested == BarPosition.BOTTOM:
            expected = WaybarInstance.config_directory() / "config.jsonc"
            raise RuntimeError(
                "Configured --bar-position 'bottom', but no Waybar config was "
                f'found. Create {expected} with "position": "bottom", or use '
                "--bar-position top."
            )

        bars = config_content if isinstance(config_content, list) else [config_content]
        source = str(config_path) if config_path is not None else "the default config"

        for index, bar in enumerate(bars, start=1):
            label = f"bar definition #{index}" if len(bars) > 1 else "the bar"
            configured = bar.get("position")
            has_includes = bool(bar.get("include"))

            if configured is None and has_includes:
                if requested == BarPosition.BOTTOM:
                    raise RuntimeError(
                        f"Cannot verify --bar-position bottom: {label} in {source} "
                        "inherits settings from included files but does not define "
                        '\'position\' locally. Add "position": "bottom" to the '
                        "main bar definition."
                    )
                log.warning(
                    "%s in %s uses included files without a local 'position'; "
                    "assuming Waybar's default position 'top'. Add "
                    '"position": "top" locally to silence this warning.',
                    label.capitalize(),
                    source,
                )
                continue

            effective = configured if configured is not None else BarPosition.TOP.value
            try:
                configured_position = BarPosition(effective)
            except (TypeError, ValueError):
                raise RuntimeError(
                    f"Unsupported Waybar position {effective!r} in {label} of "
                    f"{source}; waybar-pilot currently supports only 'top' and "
                    "'bottom'."
                ) from None
            if configured_position != requested:
                raise RuntimeError(
                    f"Configured --bar-position {requested.value!r}, but {label} in "
                    f"{source} uses position {configured_position.value!r}. Set its "
                    "'position' to "
                    f"{requested.value!r} or use --bar-position {effective}."
                )

    def has_external_waybars(self) -> bool:
        """Quick check: any waybar process not managed by us."""
        managed_pids: set[int] = set()
        for instance in self._instances.values():
            try:
                managed_pids.add(instance.pid)
            except RuntimeError:
                continue
        return bool(find_named_pids("waybar") - managed_pids)

    def kill_all_external_waybars(self) -> int:
        """Kill all externally-started waybar processes.

        Compares all waybar PIDs against our managed instances. Any PID
        not in our set is considered external and killed.
        """
        # Collect managed PIDs
        managed_pids: set[int] = set()
        for instance in self._instances.values():
            try:
                managed_pids.add(instance.pid)
            except RuntimeError:
                pass

        external_pids = find_named_pids("waybar") - managed_pids
        terminate_pids(external_pids)
        killed = len(external_pids)

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
        self.kill_all_external_waybars()

        instance = WaybarInstance(
            monitor_id=monitor.id,
            monitor_name=monitor.name,
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

    def get_all_ids(self) -> List[int]:
        """Get all monitor IDs with running waybar.

        Returns:
            List of monitor IDs
        """
        return list(self._instances.keys())

    def __len__(self) -> int:
        """Number of running waybar instances."""
        return len(self._instances)

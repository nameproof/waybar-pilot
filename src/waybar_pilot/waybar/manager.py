"""Waybar manager for handling multiple monitor instances."""

import logging
import time
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

    def _kill_external_waybar_for_monitor(self, monitor: Monitor) -> bool:
        """Kill any externally-started waybar on this monitor.
        
        This prevents duplicate waybars when external tools (like omarchy-theme-install)
        kill and restart waybar. We detect external waybar processes by checking if
        WAYBAR_MONITOR_ID env var is not set (our instances set this).
        
        Args:
            monitor: Monitor to check for external waybar
            
        Returns:
            True if external waybar was killed, False otherwise
        """
        import subprocess
        import os
        import signal
        
        killed = False
        
        try:
            # Find all waybar processes
            result = subprocess.run(
                ["pgrep", "-a", "-x", "waybar"],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                log.debug(f"No waybar processes found for monitor {monitor.name}")
                return False
            
            log.debug(f"Checking {len(result.stdout.strip().split(chr(10)))} waybar processes for monitor {monitor.name}")
            
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                
                parts = line.split(maxsplit=1)
                if len(parts) < 1:
                    continue
                
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                
                # Check if this waybar is using our monitor
                try:
                    # Read process environment to check WAYBAR_MONITOR_ID
                    env_result = subprocess.run(
                        ["cat", f"/proc/{pid}/environ"],
                        capture_output=True,
                        text=True
                    )
                    
                    if env_result.returncode == 0:
                        env_vars = env_result.stdout.split("\0")
                        has_monitor_id = any(
                            var.startswith("WAYBAR_MONITOR_ID=") 
                            for var in env_vars
                        )
                        
                        if has_monitor_id:
                            log.debug(f"Skipping managed waybar (PID {pid}) with WAYBAR_MONITOR_ID")
                            continue
                        
                        # This is an external waybar - check if it's using our monitor
                        cmdline_result = subprocess.run(
                            ["cat", f"/proc/{pid}/cmdline"],
                            capture_output=True,
                            text=True
                        )
                        
                        if cmdline_result.returncode == 0:
                            cmdline = cmdline_result.stdout.replace("\0", " ")
                            log.debug(f"External waybar (PID {pid}) cmdline: {cmdline[:100]}...")
                            
                            # Check if this waybar is outputting to our monitor
                            # Waybar uses --output or -o to specify monitor
                            if f"--output {monitor.name}" in cmdline or f"-o {monitor.name}" in cmdline:
                                log.info(f"Killing external waybar (PID {pid}) on monitor {monitor.name}")
                                os.kill(pid, signal.SIGTERM)
                                time.sleep(0.1)
                                try:
                                    os.kill(pid, signal.SIGKILL)
                                except ProcessLookupError:
                                    pass
                                killed = True
                            else:
                                log.debug(f"External waybar (PID {pid}) not targeting {monitor.name}")
                                
                except (ProcessLookupError, PermissionError, OSError) as e:
                    log.debug(f"Error checking PID {pid}: {e}")
                    continue
                    
        except Exception as e:
            log.warning(f"Error in _kill_external_waybar_for_monitor: {e}")
        
        return killed

    def _kill_all_external_waybars(self) -> int:
        """Kill all externally-started waybar processes.
        
        This is called when external tools (like omarchy) restart waybar,
        which typically creates a single waybar instance that shows on all monitors.
        We kill all external waybars so we can start our managed per-monitor instances.
        
        Returns:
            Number of external waybars killed
        """
        import subprocess
        import os
        import signal
        
        killed_count = 0
        try:
            # Find all waybar processes
            result = subprocess.run(
                ["pgrep", "-a", "-x", "waybar"],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                log.debug("No waybar processes found to kill")
                return 0
            
            log.info(f"Found {len(result.stdout.strip().split(chr(10)))} waybar processes, checking for external ones")
            
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                
                parts = line.split(maxsplit=1)
                if len(parts) < 1:
                    continue
                
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                
                try:
                    # Read process environment to check WAYBAR_MONITOR_ID
                    env_result = subprocess.run(
                        ["cat", f"/proc/{pid}/environ"],
                        capture_output=True,
                        text=True
                    )
                    
                    if env_result.returncode == 0:
                        env_vars = env_result.stdout.split("\0")
                        has_monitor_id = any(
                            var.startswith("WAYBAR_MONITOR_ID=") 
                            for var in env_vars
                        )
                        
                        if has_monitor_id:
                            log.debug(f"Skipping managed waybar (PID {pid})")
                            continue
                        
                        # This is an external waybar - kill it
                        log.info(f"Killing external waybar (PID {pid})")
                        try:
                            os.kill(pid, signal.SIGTERM)
                            time.sleep(0.1)
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            killed_count += 1
                        except (ProcessLookupError, PermissionError):
                            pass
                                
                except (ProcessLookupError, PermissionError, OSError) as e:
                    log.debug(f"Error checking/killing PID {pid}: {e}")
                    continue
                    
        except Exception as e:
            log.warning(f"Error in _kill_all_external_waybars: {e}")
        
        if killed_count > 0:
            log.info(f"Killed {killed_count} external waybar(s)")
        
        return killed_count

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
            raise RuntimeError(
                f"Waybar already running for monitor {monitor.id}"
            )
        
        log.info(f"Starting waybar for monitor {monitor.name} (ID {monitor.id})")
        
        # Kill any external waybar on this monitor before starting ours
        external_killed = self._kill_external_waybar_for_monitor(monitor)
        if external_killed:
            log.info(f"Killed external waybar on monitor {monitor.name}")
        
        instance = WaybarInstance(
            monitor_id=monitor.id,
            monitor_name=monitor.name,
            config=self._config,
            # initial_state will be set by controller based on monitor type
        )
        self._instances[monitor.id] = instance
        log.info(f"Started managed waybar for monitor {monitor.name} (PID {instance.pid})")
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

    def restart_dead_instances(self, available_monitors: List[Monitor]) -> List[WaybarInstance]:
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
            log.info(f"Detected {len(dead_ids)} dead waybar instances, killing all external waybars")
            self._kill_all_external_waybars()
        
        for monitor_id in dead_ids:
            # Find the monitor in available list
            monitor = next(
                (m for m in available_monitors if m.id == monitor_id),
                None
            )
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

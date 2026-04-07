"""Cursor sensor manager for handling multiple monitor sensors."""

from queue import Queue
from typing import Callable, Dict, List, Optional
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gtk, Gdk  # type: ignore

from ..hyprland.models import Monitor
from .sensor import CursorSensor


class CursorManager:
    """Manages cursor sensors for multiple monitors.

    Responsibilities:
    - Create/destroy sensors for autohide monitors
    - Map Hyprland monitors to GDK monitors via geometry
    - Handle monitor add/remove events
    - Relay cursor events to the controller via queue
    - Disable sensors during fullscreen mode
    """

    def __init__(
        self,
        event_queue: Queue,
        hyprland_client,
    ):
        """Initialize cursor manager.

        Args:
            event_queue: Queue to push cursor events to
            hyprland_client: HyprlandClient for querying monitor info
        """
        self._event_queue = event_queue
        self._hyprland = hyprland_client
        self._sensors: Dict[str, CursorSensor] = {}  # monitor_name -> sensor
        self._gtk_display: Optional[Gdk.Display] = None
        self._gdk_monitor_map: Dict[str, Gdk.Monitor] = {}  # monitor_name -> gdk_monitor
        self._monitor_id_map: Dict[str, int] = {}  # monitor_name -> monitor_id

        # Initialize GTK if not already done
        self._init_gtk()

    def _init_gtk(self) -> None:
        """Initialize GTK and get display."""
        if not Gtk.main_level():
            # Only init if not already initialized
            Gtk.init([])
        self._gtk_display = Gdk.Display.get_default()

    def _build_monitor_mapping(self, hyprland_monitors: List[Monitor]) -> bool:
        """Map Hyprland monitors to GDK monitors using geometry.

        Args:
            hyprland_monitors: List of Hyprland Monitor objects

        Returns:
            True if all monitors were mapped, False otherwise
        """
        if not self._gtk_display:
            return False

        self._gdk_monitor_map.clear()
        n_gdk_monitors = self._gtk_display.get_n_monitors()
        
        import logging
        log = logging.getLogger("waybar-pilot")
        log.info(f"Building monitor mapping: {len(hyprland_monitors)} Hyprland monitors, {n_gdk_monitors} GDK monitors")

        # Build list of GDK monitors with their geometries
        gdk_monitors_with_geo = []
        for i in range(n_gdk_monitors):
            gdk_monitor = self._gtk_display.get_monitor(i)
            if gdk_monitor:
                geometry = gdk_monitor.get_geometry()
                gdk_monitors_with_geo.append({
                    "index": i,
                    "monitor": gdk_monitor,
                    "x": geometry.x,
                    "y": geometry.y,
                    "width": geometry.width,
                    "height": geometry.height,
                })
                log.info(f"  GDK monitor {i}: {geometry.width}x{geometry.height} at ({geometry.x},{geometry.y})")

        # Match by geometry (x, y position)
        matched = 0
        for hl_monitor in hyprland_monitors:
            log.info(f"  Hyprland monitor {hl_monitor.name}: {hl_monitor.width}x{hl_monitor.height} at ({hl_monitor.x},{hl_monitor.y})")
            for gdk_info in gdk_monitors_with_geo:
                if hl_monitor.x == gdk_info["x"] and hl_monitor.y == gdk_info["y"]:
                    self._gdk_monitor_map[hl_monitor.name] = gdk_info["monitor"]
                    matched += 1
                    log.info(f"    -> Matched to GDK monitor {gdk_info['index']}")
                    break
            else:
                log.warning(f"    -> No matching GDK monitor found!")

        log.info(f"Monitor mapping complete: {matched}/{len(hyprland_monitors)} matched")
        return len(self._gdk_monitor_map) == len(hyprland_monitors)

    def _on_sensor_event(
        self,
        event_type: str,
        monitor_name: str,
        y: Optional[int] = None,
    ) -> None:
        """Handle events from sensors and queue them for controller.

        Args:
            event_type: "enter" or "leave"
            monitor_name: Monitor that triggered the event
            y: Y coordinate (for leave events)
        """
        from .events import CursorEnter, CursorLeave

        monitor_id = self._monitor_id_map.get(monitor_name)
        if monitor_id is None:
            return

        # Queue appropriate event
        if event_type == "enter":
            self._event_queue.put(CursorEnter(monitor_id, monitor_name))
        elif event_type == "leave":
            self._event_queue.put(CursorLeave(monitor_id, monitor_name, y or 0))

    def create_sensor_for_monitor(self, monitor: Monitor) -> bool:
        """Create a sensor for the given monitor.

        Args:
            monitor: Hyprland Monitor object

        Returns:
            True if sensor created successfully, False otherwise
        """
        import logging
        log = logging.getLogger("waybar-pilot")
        
        if monitor.name in self._sensors:
            return True  # Already exists

        if monitor.name not in self._gdk_monitor_map:
            # Try to rebuild mapping
            if not self._build_monitor_mapping([monitor]):
                return False

        gdk_monitor = self._gdk_monitor_map.get(monitor.name)
        if not gdk_monitor:
            return False
        
        # Validate GDK monitor geometry - should not be 0x0
        geometry = gdk_monitor.get_geometry()
        if geometry.width == 0 or geometry.height == 0:
            log.warning(f"GDK monitor {monitor.name} has invalid geometry: {geometry.width}x{geometry.height}")
            return False
        
        # Validate Hyprland monitor geometry
        if monitor.width == 0 or monitor.height == 0:
            log.warning(f"Hyprland monitor {monitor.name} has invalid geometry: {monitor.width}x{monitor.height}")
            return False

        try:
            log.info(f"Creating sensor for {monitor.name}: Hyprland={monitor.width}x{monitor.height}, GDK={geometry.width}x{geometry.height}")
            sensor = CursorSensor(
                monitor_name=monitor.name,
                monitor_width=monitor.width,
                monitor_x=monitor.x,
                monitor_y=monitor.y,
                gdk_monitor=gdk_monitor,
                event_callback=self._on_sensor_event,
            )
            self._sensors[monitor.name] = sensor
            sensor.show_sensor()
            return True
        except Exception as e:
            log.error(f"Failed to create sensor for {monitor.name}: {e}")
            return False

    def remove_sensor(self, monitor_name: str) -> bool:
        """Remove a sensor for the given monitor.

        Args:
            monitor_name: Monitor name

        Returns:
            True if sensor was removed, False if not found
        """
        if monitor_name not in self._sensors:
            return False

        sensor = self._sensors.pop(monitor_name)
        sensor.destroy_sensor()
        return True

    def hide_sensor(self, monitor_name: str) -> bool:
        """Hide sensor (for fullscreen mode).

        Args:
            monitor_name: Monitor name

        Returns:
            True if sensor was hidden, False if not found
        """
        if monitor_name not in self._sensors:
            return False

        self._sensors[monitor_name].hide_sensor()
        return True

    def show_sensor(self, monitor_name: str) -> bool:
        """Show sensor (after fullscreen exit).

        Args:
            monitor_name: Monitor name

        Returns:
            True if sensor was shown, False if not found
        """
        if monitor_name not in self._sensors:
            return False

        self._sensors[monitor_name].show_sensor()
        return True

    def update_monitors(self, monitors: List[Monitor], autohide_monitor_ids: List[int]) -> None:
        """Update sensors based on current monitor configuration.

        Creates sensors for new autohide monitors, removes sensors for
        monitors that are no longer autohide or no longer exist.
        Retries sensor creation for monitors with 0x0 geometry.

        Args:
            monitors: Current list of monitors from Hyprland
            autohide_monitor_ids: List of monitor IDs that should have sensors
        """
        import logging
        log = logging.getLogger("waybar-pilot")
        
        # Rebuild GDK mapping
        self._build_monitor_mapping(monitors)
        self._monitor_id_map = {m.name: m.id for m in monitors}

        # Get set of monitor names that should have sensors
        autohide_names = {
            m.name for m in monitors if m.id in autohide_monitor_ids
        }

        # Remove sensors for monitors no longer in autohide list
        current_names = set(self._sensors.keys())
        to_remove = current_names - autohide_names
        for name in to_remove:
            self.remove_sensor(name)

        # Create sensors for new autohide monitors (including retry for failed ones)
        failed_monitors = []
        for monitor in monitors:
            if monitor.name in autohide_names:
                if monitor.name not in self._sensors:
                    success = self.create_sensor_for_monitor(monitor)
                    if not success:
                        failed_monitors.append(monitor)
                        log.warning(f"Will retry sensor creation for {monitor.name} on next update")
        
        # Log status
        log.info(f"Sensor status: {len(self._sensors)}/{len(autohide_names)} sensors active")
        if failed_monitors:
            log.info(f"Pending sensor creation: {[m.name for m in failed_monitors]}")

    def is_gtk_available(self) -> bool:
        """Check if GTK and display are available."""
        return self._gtk_display is not None

    def get_sensor_count(self) -> int:
        """Get number of active sensors."""
        return len(self._sensors)

    def shutdown(self) -> None:
        """Clean up all sensors."""
        for sensor in list(self._sensors.values()):
            sensor.destroy_sensor()
        self._sensors.clear()

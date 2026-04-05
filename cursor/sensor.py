"""GTK Layer Shell cursor sensor for detecting cursor at top edge."""

import logging
import threading
from typing import Callable, Optional
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")

from gi.repository import Gtk, Gdk, GtkLayerShell, GLib  # type: ignore


log = logging.getLogger("waybar-pilot")


class CursorSensor(Gtk.Window):
    """Invisible sensor strip at top of monitor using GTK Layer Shell.

    Creates a transparent, input-only 10px strip at the top edge that:
    - Detects cursor enter/leave events (no polling)
    - Passes clicks through to waybar below
    - Emits events to a callback for processing
    - Debounces rapid enter/leave events to prevent flickering

    Uses Layer.TOP (same as Waybar) with exclusive_zone=0 so it doesn't
    reserve space or block interactions with the bar.
    """

    # Sensor dimensions
    SENSOR_HEIGHT = 10  # pixels - physical tracking zone
    TRIGGER_HEIGHT = 1  # pixels - logical reveal threshold at top edge

    # Debounce time to prevent flickering during cursor movement (milliseconds)
    DEBOUNCE_MS = 50

    def __init__(
        self,
        monitor_name: str,
        monitor_width: int,
        monitor_x: int,
        monitor_y: int,
        gdk_monitor: Gdk.Monitor,
        event_callback: Callable,
    ):
        """Initialize the cursor sensor.

        Args:
            monitor_name: Hyprland monitor name (e.g., "DP-1")
            monitor_width: Width of the monitor in pixels
            monitor_x: X position of monitor
            monitor_y: Y position of monitor
            gdk_monitor: GDK monitor object for this display
            event_callback: Function to call with events (enter, leave, motion)
        """
        super().__init__(type=Gtk.WindowType.TOPLEVEL)

        self._monitor_name = monitor_name
        self._monitor_width = monitor_width
        self._monitor_x = monitor_x
        self._monitor_y = monitor_y
        self._gdk_monitor = gdk_monitor
        self._event_callback = event_callback

        # Track state
        self._cursor_inside = False
        self._trigger_active = False
        self._is_active = False

        # Debounce timer
        self._debounce_timer: Optional[threading.Timer] = None
        self._debounce_lock = threading.Lock()

        # Setup the layer shell window
        self._setup_layer_shell()
        self._setup_appearance()
        self._setup_events()

    def _setup_layer_shell(self) -> None:
        """Configure as layer shell surface."""
        # Initialize layer shell
        GtkLayerShell.init_for_window(self)

        # Set namespace so we can identify/configure in Hyprland
        GtkLayerShell.set_namespace(self, "waybar-pilot-sensor")

        # Set layer to TOP (same as waybar, under notifications/overlays)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.TOP)

        # Anchor to top of screen, full width
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)

        # Don't reserve exclusive zone (waybar handles that)
        GtkLayerShell.set_exclusive_zone(self, 0)

        # No margins needed - layer shell handles per-monitor positioning
        # via set_monitor below

        # Set monitor
        GtkLayerShell.set_monitor(self, self._gdk_monitor)

    def _setup_appearance(self) -> None:
        """Make window transparent and input-only."""
        import logging
        log = logging.getLogger("waybar-pilot")
        
        # Set size - full width, sensor height
        log.info(f"Setting sensor size: {self._monitor_width}x{self.SENSOR_HEIGHT}")
        self.set_default_size(self._monitor_width, self.SENSOR_HEIGHT)
        self.set_size_request(self._monitor_width, self.SENSOR_HEIGHT)
        
        # Log the actual window size after setting
        actual_width = self.get_allocated_width()
        actual_height = self.get_allocated_height()
        log.info(f"Sensor window allocated size after set: {actual_width}x{actual_height}")

        # Use RGBA visual for transparency
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)

        # Apply transparent CSS - use rgba(0,0,0,0) for fully transparent
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
            window {
                background: rgba(0, 0, 0, 0);
                border: none;
                box-shadow: none;
                padding: 0;
                margin: 0;
            }
        """)
        self.get_style_context().add_provider(
            css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # No decorations
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)

    def _setup_events(self) -> None:
        """Setup event handlers for enter/leave."""
        # Motion events are only tracked inside the small sensor strip so we can
        # separate the physical tracking zone from the reveal trigger threshold.
        self.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )

        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)
        self.connect("motion-notify-event", self._on_motion)

    def _cancel_debounce(self) -> None:
        """Cancel any pending debounce timer."""
        with self._debounce_lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None

    def _debounced_leave(self, y: int) -> None:
        """Handle leave event after debounce period."""
        with self._debounce_lock:
            self._debounce_timer = None

        if self._trigger_active:
            self._trigger_active = False
            self._event_callback("leave", self._monitor_name, y)

    def _schedule_leave(self, y: int, source: str) -> None:
        """Schedule a debounced logical leave from the reveal threshold."""
        if not self._trigger_active:
            return

        with self._debounce_lock:
            if self._debounce_timer:
                return

            self._debounce_timer = threading.Timer(
                self.DEBOUNCE_MS / 1000.0,
                self._debounced_leave,
                args=(y,),
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

        log.info(
            "Sensor %s: scheduled debounced leave from %s in %sms (y=%s)",
            self._monitor_name,
            source,
            self.DEBOUNCE_MS,
            y,
        )

    def _should_trigger(self, y: float) -> bool:
        """Return True when the cursor is at the reveal threshold."""
        return y < self.TRIGGER_HEIGHT

    def _activate_trigger(self) -> None:
        """Emit reveal event once when the cursor reaches the top edge."""
        if not self._trigger_active:
            self._trigger_active = True
            log.info(
                "Sensor %s: reveal triggered at top edge",
                self._monitor_name,
            )
            self._event_callback("enter", self._monitor_name)

    def _on_enter(self, widget: Gtk.Widget, event: Gdk.EventCrossing) -> bool:
        """Handle cursor entering sensor zone."""
        # Cancel any pending leave
        self._cancel_debounce()

        self._cursor_inside = True
        log.info(
            "Sensor %s: cursor entered tracking zone at y=%.1f",
            self._monitor_name,
            float(getattr(event, "y", -1.0)),
        )
        if self._should_trigger(float(getattr(event, "y", self.SENSOR_HEIGHT))):
            self._activate_trigger()
        return False  # Don't stop propagation

    def _on_motion(self, widget: Gtk.Widget, event: Gdk.EventMotion) -> bool:
        """Trigger reveal when motion reaches the top edge threshold."""
        if not self._cursor_inside:
            return False

        y = int(getattr(event, "y", self.SENSOR_HEIGHT))
        if self._should_trigger(float(y)):
            self._cancel_debounce()
            self._activate_trigger()
        elif self._trigger_active:
            self._schedule_leave(y, "motion")
        return False  # Don't stop propagation

    def _on_leave(self, widget: Gtk.Widget, event: Gdk.EventCrossing) -> bool:
        """Handle cursor leaving sensor zone with debouncing."""
        # Get Y coordinate for hysteresis calculation
        y = int(event.y) if hasattr(event, "y") else 0

        # Cancel any pending debounce
        self._cancel_debounce()

        self._cursor_inside = False
        log.info(
            "Sensor %s: cursor left tracking zone at y=%s (trigger_active=%s)",
            self._monitor_name,
            y,
            self._trigger_active,
        )

        # Physical leave means the cursor is definitely no longer at the top
        # edge reveal threshold. Normalize the reported y away from 0 so the
        # controller does not mistake this backup path for a genuine re-entry.
        if self._trigger_active:
            normalized_y = max(y, self.TRIGGER_HEIGHT + 1)
            self._schedule_leave(normalized_y, "leave")

        return False  # Don't stop propagation

    def show_sensor(self) -> None:
        """Show and activate the sensor."""
        import logging
        log = logging.getLogger("waybar-pilot")
        
        if not self._is_active:
            log.info(f"Showing sensor for {self._monitor_name}")
            self.show_all()
            self._is_active = True
            # Log actual window size after showing
            GLib.timeout_add(100, self._log_window_size)
    
    def _log_window_size(self) -> bool:
        """Log actual window size (called via GLib.timeout_add)."""
        import logging
        log = logging.getLogger("waybar-pilot")
        allocation = self.get_allocation()
        log.info(f"Sensor window {self._monitor_name} actual size: {allocation.width}x{allocation.height}")
        return False  # Don't repeat
    
    def hide_sensor(self) -> None:
        """Hide the sensor (for fullscreen mode)."""
        if self._is_active:
            self.hide()
            self._is_active = False
            self._cursor_inside = False
            self._trigger_active = False
            self._cancel_debounce()

    def destroy_sensor(self) -> None:
        """Clean up and destroy the sensor."""
        self._cancel_debounce()
        self._is_active = False
        self._cursor_inside = False
        self._trigger_active = False
        self.destroy()

    @property
    def is_active(self) -> bool:
        """Check if sensor is currently active."""
        return self._is_active

    @property
    def monitor_name(self) -> str:
        """Get monitor name."""
        return self._monitor_name

"""Main controller orchestrating waybar autohide functionality."""

from dataclasses import dataclass
import logging
import signal
import subprocess
import sys
import threading
import time
from queue import Empty, Queue
from typing import Dict, List, Optional, Set

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore

from config import Config, ResolvedMonitorSelection, WaybarState
from cursor import CursorEnter, CursorLeave, CursorManager, CursorSensor
from hyprland import (
    Client,
    CursorPosition,
    EventType,
    FullscreenHandler,
    HyprlandClient,
    HyprlandConnectionError,
    HyprlandEvent,
    Monitor,
    Socket2Listener,
)
from state import StateEngine
from waybar import WaybarInstance, WaybarManager

# Setup logging - unbuffered so output appears in log files immediately
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("waybar-pilot")


@dataclass
class PendingExitCheck:
    """Scheduled hide recheck handled by the main loop."""

    next_check_at: float
    leave_enter_count: int
    top_edge_leave: bool


class AutohideController:
    """Main controller for waybar autohide.

    Orchestrates:
    - Hyprland client for querying state
    - Socket2Listener for event notifications
    - CursorManager for event-driven cursor detection (GTK layer shell)
    - FullscreenHandler for disabling sensors during fullscreen
    - StateEngine for visibility decisions
    - WaybarManager for process control

    Usage:
        controller = AutohideController(config)
        if controller.initialize():
            controller.run()
    """

    # --- Timing constants (seconds) ---
    MAIN_LOOP_INTERVAL = 0.05       # 50 ms between main loop iterations
    STARTUP_GRACE_PERIOD = 0.5      # Wait before first hide after waybar starts
    EXIT_GRACE_PERIOD = 0.1         # Initial delay after cursor leaves sensor
    EXIT_EXTENDED_PERIOD = 2.0      # Extended delay while cursor is in bar area
    PROCESS_KILL_SETTLE = 0.5       # Wait after pkill for processes to die

    # --- Sensor geometry constants (pixels) ---
    SENSOR_REENTER_ZONE = 10        # Y threshold to consider cursor back in sensor

    # --- Top-zone re-entry verification ---
    TOP_ZONE_RECHECK_INTERVAL = 0.2 # Poll while a reveal-triggered top-edge leave is unresolved

    # --- GTK event processing ---
    GTK_MAX_EVENTS_PER_TICK = 50    # Max GTK events processed per main loop tick

    # --- Sensor retry ---
    SENSOR_RETRY_INTERVAL = 10      # Main loop ticks between sensor creation retries

    def __init__(self, config: Config):
        """Initialize the controller.

        Args:
            config: Application configuration
        """
        self._config = config
        self._running = False
        self._shutdown_requested = False

        # Components (initialized in initialize())
        self._hyprland: Optional[HyprlandClient] = None
        self._waybar_manager: Optional[WaybarManager] = None
        self._state_engine: Optional[StateEngine] = None
        self._event_queue: Optional[Queue] = None
        self._socket2_listener: Optional[Socket2Listener] = None
        self._cursor_manager: Optional[CursorManager] = None
        self._fullscreen_handler: Optional[FullscreenHandler] = None

        # State cache
        self._monitors: List[Monitor] = []
        self._clients: List[Client] = []
        self._active_workspaces: List[int] = []
        self._active_workspaces_by_monitor: Dict[int, int] = {}
        self._cursor: Optional[CursorPosition] = None

        # Cursor tracking
        self._cursor_in_sensor_zone: Dict[int, bool] = {}  # monitor_id -> bool
        self._exit_checks: Dict[int, PendingExitCheck] = {}
        self._sensor_enter_counts: Dict[int, int] = {}  # monitor_id -> monotonic enter count
        self._last_cursor_monitor: Optional[int] = None  # Track cursor monitor to detect teleport
        self._loop_tick = 0
        self._cursor_query_reasons_this_tick: List[str] = []

        # Startup grace period tracking
        self._waybar_start_times: Dict[int, float] = {}  # monitor_id -> start time

        # Flag for deferred sensor creation
        self._sensors_need_update = False
        
        # Sensor retry tracking
        self._sensor_retry_counter = 0

        # Resolved monitor behavior (selectors -> monitor IDs)
        self._resolved_selection = ResolvedMonitorSelection(
            autohide_ids=set(),
            show_ids=set(),
            monitor_lists_configured=False,
            unresolved_autohide=[],
            unresolved_show=[],
        )

        # Setup signal handlers
        self._setup_signal_handlers()

    def _setup_signal_handlers(self) -> None:
        """Setup handlers for graceful shutdown."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals."""
        log.info(f"Received signal {signum}, shutting down...")
        self._shutdown_requested = True
        self._running = False

    def initialize(self) -> bool:
        """Initialize all components."""
        try:
            # Check Hyprland is running
            self._hyprland = HyprlandClient()
            if not self._hyprland.is_running():
                log.error("Hyprland is not running")
                return False

            # Initialize state engine
            self._state_engine = StateEngine(self._config)

            # Initialize fullscreen handler
            self._fullscreen_handler = FullscreenHandler()

            # Initialize waybar manager
            self._waybar_manager = WaybarManager(self._config)

            # Kill any existing waybar processes
            self._kill_existing_waybar()

            # Get initial state
            self._refresh_state()
            self._resolve_monitor_selection(strict=True)
            
            # Log detected monitors
            for m in self._monitors:
                log.info(f"Detected monitor: {m.name} (ID {m.id}, {m.width}x{m.height})")

            # Determine which monitors to manage
            managed_ids = self._get_managed_monitor_ids()
            if not managed_ids:
                log.error("No monitors to manage")
                return False

            # Start waybar for each managed monitor
            self._start_waybar_for_monitors(managed_ids)

            if len(self._waybar_manager) == 0:
                log.error("No waybar instances started")
                return False

            # Setup event queue and listener
            self._event_queue = Queue()
            self._socket2_listener = Socket2Listener(
                event_queue=self._event_queue,
                hyprland_client=self._hyprland,
            )
            self._socket2_listener.start()

            # Initialize cursor manager
            try:
                self._cursor_manager = CursorManager(
                    event_queue=self._event_queue,
                    hyprland_client=self._hyprland,
                )
                # Sensors will be created on first main loop iteration
                # to avoid blocking during initialization
                self._sensors_need_update = True
            except Exception as e:
                log.error(f"Failed to initialize cursor detection: {e}")
                return False

            log.info(f"Managing {len(self._waybar_manager)} monitors: "
                  f"{self._waybar_manager.get_all_ids()}")
            log.info(f"Autohide selectors: {self._config.autohide_monitors}")
            log.info(f"Show selectors: {self._config.show_monitors}")
            log.info(f"Resolved autohide IDs: {sorted(self._resolved_selection.autohide_ids)}")
            log.info(f"Resolved show IDs: {sorted(self._resolved_selection.show_ids)}")
            log.info("Waybar autohide is running...")

            return True

        except Exception as e:
            log.error(f"Error during initialization: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _kill_existing_waybar(self) -> None:
        """Kill any existing waybar processes."""
        try:
            subprocess.run(
                ["pkill", "-9", "-x", self._config.waybar_proc],
                check=False,
                capture_output=True,
            )
            time.sleep(self.PROCESS_KILL_SETTLE)
        except (FileNotFoundError, OSError):
            pass

    def _get_managed_monitor_ids(self) -> List[int]:
        """Get list of monitor IDs to manage.

        If no monitors specified in config, use all available monitors with autohide.
        If monitors are specified, use all monitors but treat unlisted ones as "show".
        """
        all_monitor_ids = [m.id for m in self._monitors]
        
        # If no monitors specified in config, use all with autohide behavior
        if not self._config.autohide_monitors and not self._config.show_monitors:
            return all_monitor_ids
        
        # Monitors are specified - return ALL monitors
        # (unlisted ones will be treated as "show" by is_show_monitor())
        return all_monitor_ids

    def _resolve_monitor_selection(self, strict: bool = False) -> None:
        """Resolve configured monitor selectors to current monitor IDs."""
        try:
            self._resolved_selection = self._config.resolve_monitor_selection(self._monitors)
        except ValueError:
            if strict:
                raise
            log.exception("Failed to resolve monitor selectors; keeping previous mapping")
            return

        if self._resolved_selection.unresolved_autohide:
            log.warning(
                "Unresolved --hide-monitors selectors: %s",
                self._resolved_selection.unresolved_autohide,
            )
        if self._resolved_selection.unresolved_show:
            log.warning(
                "Unresolved --show-monitors selectors: %s",
                self._resolved_selection.unresolved_show,
            )

    def _is_show_monitor(self, monitor_id: int) -> bool:
        return self._resolved_selection.is_show_monitor(monitor_id)

    def _start_waybar_for_monitors(self, monitor_ids: List[int]) -> None:
        """Start waybar instances for specified monitors."""
        for monitor in self._monitors:
            if monitor.id in monitor_ids:
                try:
                    instance = self._waybar_manager.start_for_monitor(monitor)

                    # Sync state based on current conditions
                    self._sync_initial_state(instance, monitor.id)

                except RuntimeError as e:
                    log.warning(f"Could not start waybar for monitor {monitor.id}: {e}")

    def _sync_initial_state(self, instance: WaybarInstance, monitor_id: int) -> None:
        """Sync waybar state based on current conditions."""
        # Record startup time for grace period
        self._waybar_start_times[monitor_id] = time.time()

        # Show monitors should ALWAYS be visible
        if self._is_show_monitor(monitor_id):
            # Ensure instance state is VISIBLE
            instance.state = WaybarState.VISIBLE
            # CRITICAL: Also set the StateEngine's monitor state to VISIBLE
            # so decide_transitions doesn't try to transition it
            monitor_state = self._state_engine.get_or_create_monitor_state(monitor_id)
            monitor_state.current_state = WaybarState.VISIBLE
            return

        # For autohide monitors, check current conditions
        overlapping = self._state_engine.find_overlapping_clients(
            self._clients,
            monitor_id,
            self._active_workspaces,
        )

        cursor_monitor = None
        if self._cursor:
            cursor_monitor = self._state_engine.get_cursor_monitor(
                self._cursor,
                self._monitors,
            )

        should_show = self._state_engine.should_show(
            monitor_id,
            cursor_monitor,
            self._cursor or CursorPosition(0, 0),
            overlapping,
        )

        # Update instance state to match actual visibility
        # CRITICAL: Also update StateEngine's monitor state to prevent double-toggle
        monitor_state = self._state_engine.get_or_create_monitor_state(monitor_id)
        if should_show:
            instance.state = WaybarState.VISIBLE
            monitor_state.current_state = WaybarState.VISIBLE
        else:
            # Need to hide it - but wait for grace period
            # Schedule a delayed hide to give waybar time to fully initialize
            instance.state = WaybarState.HIDDEN  # Mark as should-be-hidden
            monitor_state.current_state = WaybarState.HIDDEN
            
            # Schedule the actual toggle after grace period
            def delayed_hide():
                time.sleep(self.STARTUP_GRACE_PERIOD)
                try:
                    if self._waybar_manager.get_instance(monitor_id):
                        instance.toggle()
                        log.info(f"Monitor {monitor_id}: hidden after startup grace period")
                except RuntimeError:
                    pass  # Waybar may have died, _check_process_health will handle it
            
            # Run in background thread
            timer = threading.Thread(target=delayed_hide, daemon=True)
            timer.start()

    def _refresh_state(self) -> None:
        """Refresh state from Hyprland.

        Uses a single ``hyprctl -j monitors`` call to obtain monitors,
        active workspace IDs, and the per-monitor workspace mapping.
        """
        try:
            (
                self._monitors,
                self._active_workspaces,
                self._active_workspaces_by_monitor,
            ) = self._hyprland.get_monitors_and_workspaces()
            self._clients = self._hyprland.get_clients()

            # Update fullscreen state
            self._fullscreen_handler.update_from_clients(self._clients, self._monitors)

        except HyprlandConnectionError:
            log.error("Lost connection to Hyprland")
            self._running = False
        except Exception as e:
            log.warning(f"Error refreshing state: {e}")

    def _process_events(self) -> None:
        """Process pending events from the event queue."""
        if not self._event_queue:
            return

        # Process all pending events
        events_to_process = []
        try:
            while True:
                event = self._event_queue.get_nowait()
                events_to_process.append(event)
        except Empty:
            pass

        # Handle events
        needs_refresh = False
        needs_visibility_update = False
        has_active_window_event = False
        last_active_window_event = None

        for event in events_to_process:
            if isinstance(event, (CursorEnter, CursorLeave)):
                # Handle cursor events
                self._handle_cursor_event(event)
                # Only trigger visibility update for enter events
                # Leave events let the timer handle it to prevent flickering
                if isinstance(event, CursorEnter):
                    needs_visibility_update = True

            elif isinstance(event, HyprlandEvent):
                # Handle Hyprland socket2 events
                if event.event_type in (
                    EventType.MONITOR_ADDED,
                    EventType.MONITOR_ADDED_V2,
                    EventType.MONITOR_REMOVED,
                ):
                    self._handle_monitor_change(event)
                    needs_refresh = True
                elif event.event_type == EventType.WORKSPACE_CREATED:
                    needs_refresh = True
                elif event.event_type == EventType.WORKSPACE_DESTROYED:
                    needs_refresh = True
                elif event.event_type == EventType.ACTIVE_WORKSPACE:
                    needs_refresh = True
                    needs_visibility_update = True  # Workspace switch affects fullscreen check
                elif event.event_type in (
                    EventType.ACTIVE_WINDOW,
                    EventType.WINDOW_CLOSE,
                    EventType.WINDOW_MOVE,
                    EventType.FULLSCREEN,
                ):
                    needs_refresh = True
                    needs_visibility_update = True  # Fullscreen affects visibility
                    
                    # Special handling for active window changes
                    if event.event_type == EventType.ACTIVE_WINDOW:
                        has_active_window_event = True
                        last_active_window_event = event

        if needs_refresh:
            self._refresh_state()

        if last_active_window_event is not None:
            focus_state_cleared = self._handle_active_window_focus_change(last_active_window_event)
            if focus_state_cleared:
                needs_visibility_update = True

        # Only check for cursor monitor teleports when events suggest it
        # (avoids spawning hyprctl cursorpos every 50ms)
        if needs_refresh and any(
            isinstance(e, HyprlandEvent) and e.event_type in (
                EventType.ACTIVE_WORKSPACE,
                EventType.FULLSCREEN,
            )
            for e in events_to_process
        ) and not has_active_window_event:
            cursor_state_cleared = self._check_cursor_monitor_changed()
            if cursor_state_cleared:
                needs_visibility_update = True

        if needs_visibility_update:
            self._update_visibility()

    def _handle_cursor_event(self, event) -> None:
        """Handle cursor enter/leave events from sensors."""
        monitor_id = event.monitor_id

        if isinstance(event, CursorEnter):
            self._exit_checks.pop(monitor_id, None)

            # Mark cursor as in sensor zone
            self._cursor_in_sensor_zone[monitor_id] = True
            self._sensor_enter_counts[monitor_id] = self._sensor_enter_counts.get(monitor_id, 0) + 1
            log.debug("Monitor %s: cursor entered reveal zone", monitor_id)

        elif isinstance(event, CursorLeave):
            # Cursor left sensor zone - start grace period timer
            # Set state to False immediately for accurate tracking
            # Timer will handle the actual hide action after grace period
            self._cursor_in_sensor_zone[monitor_id] = False
            log.debug(
                "Monitor %s: cursor left reveal zone at y=%s, starting hide timer",
                monitor_id,
                event.exit_y,
            )
            self._start_bar_exit_timer(monitor_id, event.exit_y)

    def _start_bar_exit_timer(self, monitor_id: int, exit_y: int) -> None:
        """Schedule the first hide recheck after leaving the reveal zone."""
        leave_enter_count = self._sensor_enter_counts.get(monitor_id, 0)
        top_edge_leave = exit_y <= self.SENSOR_REENTER_ZONE

        log.debug(
            "Monitor %s: scheduling hide grace timer for %.2fs",
            monitor_id,
            self.EXIT_GRACE_PERIOD,
        )
        self._schedule_exit_check(
            monitor_id,
            self.EXIT_GRACE_PERIOD,
            leave_enter_count,
            top_edge_leave,
        )

    def _schedule_exit_check(
        self,
        monitor_id: int,
        delay: float,
        leave_enter_count: int,
        top_edge_leave: bool,
    ) -> None:
        """Schedule a hide recheck to be processed by the main loop."""
        self._exit_checks[monitor_id] = PendingExitCheck(
            next_check_at=time.time() + delay,
            leave_enter_count=leave_enter_count,
            top_edge_leave=top_edge_leave,
        )

    def _get_cursor_position_logged(self, reason: str) -> CursorPosition:
        """Get cursor position and log duplicate per-tick queries.

        This is instrumentation to validate whether a per-tick cache would
        actually buy us anything before we add more state.
        """
        self._cursor_query_reasons_this_tick.append(reason)
        return self._hyprland.get_cursor_position()

    def _finish_loop_tick(self) -> None:
        """Emit a log if this loop tick queried cursor position multiple times."""
        if len(self._cursor_query_reasons_this_tick) > 1:
            log.debug(
                "Tick %s: multiple cursor queries in one loop: %s",
                self._loop_tick,
                ", ".join(self._cursor_query_reasons_this_tick),
            )
        self._cursor_query_reasons_this_tick.clear()

    def _process_exit_checks(self) -> None:
        """Process due hide rechecks with one shared cursor query."""
        if not self._exit_checks:
            return

        now = time.time()
        due_monitor_ids = [
            monitor_id
            for monitor_id, pending in self._exit_checks.items()
            if pending.next_check_at <= now
        ]
        if not due_monitor_ids:
            return

        try:
            cursor_pos = self._get_cursor_position_logged("exit_checks")
            cursor_monitor = self._state_engine.get_cursor_monitor(cursor_pos, self._monitors)
        except Exception as exc:
            log.warning("Exit check cursor query failed (%s), forcing hide", exc)
            for monitor_id in due_monitor_ids:
                self._cursor_in_sensor_zone[monitor_id] = False
                self._exit_checks.pop(monitor_id, None)
            self._update_visibility()
            return

        needs_visibility_update = False
        for monitor_id in due_monitor_ids:
            pending = self._exit_checks.get(monitor_id)
            if not pending or pending.next_check_at > now:
                continue

            monitor = next((m for m in self._monitors if m.id == monitor_id), None)
            if not monitor:
                log.debug("Monitor %s: hide check fired but monitor no longer exists", monitor_id)
                self._cursor_in_sensor_zone[monitor_id] = False
                self._exit_checks.pop(monitor_id, None)
                needs_visibility_update = True
                continue

            relative_y = cursor_pos.y - monitor.y
            if relative_y <= self.SENSOR_REENTER_ZONE and cursor_monitor == monitor_id:
                current_enter_count = self._sensor_enter_counts.get(monitor_id, 0)
                if current_enter_count == pending.leave_enter_count:
                    log.debug(
                        "Monitor %s: top-edge leave is still unresolved, rechecking in %.2fs",
                        monitor_id,
                        self.TOP_ZONE_RECHECK_INTERVAL,
                    )
                    self._schedule_exit_check(
                        monitor_id,
                        self.TOP_ZONE_RECHECK_INTERVAL,
                        pending.leave_enter_count,
                        pending.top_edge_leave,
                    )
                    continue

                log.debug(
                    "Monitor %s: hide check cancelled, cursor returned to top zone (y=%s)",
                    monitor_id,
                    relative_y,
                )
                self._exit_checks.pop(monitor_id, None)
                self._cursor_in_sensor_zone[monitor_id] = True
                continue

            hide_threshold = self._config.bar_height + self._config.height_threshold
            if relative_y > hide_threshold or cursor_monitor != monitor_id:
                log.debug(
                    "Monitor %s: hide check hiding waybar (cursor_monitor=%s, y=%s, threshold=%s)",
                    monitor_id,
                    cursor_monitor,
                    relative_y,
                    hide_threshold,
                )
                self._cursor_in_sensor_zone[monitor_id] = False
                self._exit_checks.pop(monitor_id, None)
                needs_visibility_update = True
                continue

            if pending.top_edge_leave:
                log.debug(
                    "Monitor %s: cursor still in bar zone after top-edge leave, rechecking in %.2fs (y=%s, threshold=%s)",
                    monitor_id,
                    self.TOP_ZONE_RECHECK_INTERVAL,
                    relative_y,
                    hide_threshold,
                )
                self._schedule_exit_check(
                    monitor_id,
                    self.TOP_ZONE_RECHECK_INTERVAL,
                    pending.leave_enter_count,
                    pending.top_edge_leave,
                )
                continue

            log.debug(
                "Monitor %s: extending visibility for %.2fs (cursor y=%s within threshold=%s)",
                monitor_id,
                self.EXIT_EXTENDED_PERIOD,
                relative_y,
                hide_threshold,
            )
            self._schedule_exit_check(
                monitor_id,
                self.EXIT_EXTENDED_PERIOD,
                pending.leave_enter_count,
                pending.top_edge_leave,
            )

        if needs_visibility_update:
            self._update_visibility()

    def _handle_monitor_change(self, event: HyprlandEvent) -> None:
        """Handle monitor add/remove events."""
        log.info(f"Monitor change: {event.event_type.value} - {event.raw_data.strip()}")

        # Snapshot monitor id->name mapping BEFORE refresh so we can
        # clean up sensors for monitors that disappear from the list.
        old_monitor_names = {m.id: m.name for m in self._monitors}

        self._refresh_state()
        self._resolve_monitor_selection()

        # Get current managed monitors
        current_ids = set(self._waybar_manager.get_all_ids())
        available_ids = {m.id for m in self._monitors}
        managed_ids = set(self._get_managed_monitor_ids())

        log.info(f"Waybar on: {current_ids}, available: {available_ids}, managed: {managed_ids}")

        # Find monitors to add/remove
        to_remove = current_ids - available_ids
        to_add = (managed_ids & available_ids) - current_ids

        # Remove dead monitors
        for monitor_id in to_remove:
            monitor_name = old_monitor_names.get(monitor_id)
            log.info(f"Removing waybar for monitor {monitor_id} ({monitor_name})")
            self._waybar_manager.kill_monitor(monitor_id)
            self._state_engine.remove_monitor_state(monitor_id)
            if monitor_name and self._cursor_manager:
                self._cursor_manager.remove_sensor(monitor_name)

        # Add new monitors
        for monitor_id in to_add:
            monitor = next((m for m in self._monitors if m.id == monitor_id), None)
            if monitor:
                log.info(f"Adding waybar for monitor {monitor_id} ({monitor.name})")
                try:
                    instance = self._waybar_manager.start_for_monitor(monitor)
                    self._sync_initial_state(instance, monitor_id)
                except RuntimeError as e:
                    log.error(f"Failed to start waybar for monitor {monitor_id}: {e}")

        # Update cursor sensors for autohide monitors
        if self._cursor_manager:
            autohide_ids = [
                mid for mid in managed_ids
                if not self._is_show_monitor(mid)
            ]
            self._cursor_manager.update_monitors(self._monitors, autohide_ids)

    def _check_process_health(self) -> None:
        """Check and restart dead waybar processes."""
        restarted = self._waybar_manager.restart_dead_instances(self._monitors)

        for instance in restarted:
            # Sync state for restarted instance
            self._sync_initial_state(instance, instance.monitor_id)

    def _check_cursor_monitor_changed(self) -> bool:
        """Check if cursor moved to a different monitor and clear stale sensor states.
        
        This handles the case where the compositor teleports the cursor to a different
        monitor (e.g., when switching to a workspace on another monitor) without
        generating leave events for the old monitor's sensor.
        
        Returns:
            True if stale state was cleared and visibility needs update, False otherwise
        """
        state_cleared = False
        try:
            # Get actual cursor position from Hyprland
            cursor_pos = self._get_cursor_position_logged("cursor_monitor_changed")
            current_monitor = self._state_engine.get_cursor_monitor(cursor_pos, self._monitors)
            
            log.debug(f"Cursor check: last={self._last_cursor_monitor}, current={current_monitor}, pos=({cursor_pos.x},{cursor_pos.y})")
            
            if current_monitor is not None and self._last_cursor_monitor is not None:
                if current_monitor != self._last_cursor_monitor:
                    # Cursor moved to a different monitor
                    old_monitor = self._last_cursor_monitor
                    log.debug(f"Cursor moved from monitor {old_monitor} to {current_monitor}")
                    
                    # Clear stale sensor state for the old monitor
                    if old_monitor in self._cursor_in_sensor_zone:
                        if self._cursor_in_sensor_zone[old_monitor]:
                            log.debug(f"Clearing stale sensor state for monitor {old_monitor}")
                            self._cursor_in_sensor_zone[old_monitor] = False
                            state_cleared = True
                            
                            self._exit_checks.pop(old_monitor, None)
            elif self._last_cursor_monitor is None and current_monitor is not None:
                log.debug(f"Initializing cursor monitor tracking: {current_monitor}")
            
            # Update last known cursor monitor
            self._last_cursor_monitor = current_monitor
            
        except Exception as e:
            # Log errors for debugging
            log.warning(f"Error checking cursor monitor: {e}")
        
        return state_cleared

    def _handle_active_window_focus_change(self, event) -> bool:
        """Handle active window change to detect cursor focus movement.
        
        When a new window takes focus (e.g., browser opening from URL click),
        Hyprland may move the cursor to the new window. This can happen:
        1. On the same monitor (cursor below sensor zone)
        2. On a different monitor (cursor warped to new monitor)
        
        In both cases, the sensor won't detect a leave event because the cursor
        was moved programmatically, not by user movement.
        
        This method queries the actual cursor position and clears stale sensor
        state if the cursor has moved away from the sensor zone or to another monitor.
        
        Args:
            event: The ACTIVE_WINDOW HyprlandEvent
            
        Returns:
            True if stale sensor state was cleared and visibility needs update
        """
        state_cleared = False
        
        try:
            # Get current cursor position to verify actual location
            cursor_pos = self._get_cursor_position_logged("active_window_focus")
            
            # Find which monitor the cursor is actually on
            cursor_monitor = self._state_engine.get_cursor_monitor(cursor_pos, self._monitors)
            
            if cursor_monitor is None:
                return False
            
            # Check if cursor moved to a DIFFERENT monitor
            if (self._last_cursor_monitor is not None and 
                cursor_monitor != self._last_cursor_monitor):
                # Cursor moved to different monitor - clear old monitor's state
                old_monitor = self._last_cursor_monitor
                if old_monitor in self._cursor_in_sensor_zone:
                    if self._cursor_in_sensor_zone[old_monitor]:
                        log.debug(f"Active window change: cursor moved to monitor "
                                f"{cursor_monitor} from {old_monitor}, clearing stale state")
                        self._cursor_in_sensor_zone[old_monitor] = False
                        
                        self._exit_checks.pop(old_monitor, None)
                        
                        state_cleared = True
            
            # Also check if cursor is below sensor zone on the SAME monitor
            if cursor_monitor in self._cursor_in_sensor_zone:
                if self._cursor_in_sensor_zone[cursor_monitor]:
                    # Cursor is marked as "in sensor zone" but let's verify
                    monitor = next((m for m in self._monitors if m.id == cursor_monitor), None)
                    if monitor:
                        relative_y = cursor_pos.y - monitor.y
                        
                        # Calculate sensor zone height from config
                        sensor_zone_height = self._config.bar_height + self._config.height_threshold
                        
                        # If cursor is below the sensor zone, it's likely been moved
                        if relative_y > sensor_zone_height:
                            log.debug(f"Active window change: cursor moved below sensor zone "
                                    f"on monitor {cursor_monitor} (y={relative_y}, "
                                    f"zone={sensor_zone_height}px), clearing stale state")
                            self._cursor_in_sensor_zone[cursor_monitor] = False
                            
                            self._exit_checks.pop(cursor_monitor, None)
                            
                            state_cleared = True
            
            # Update last cursor monitor tracking
            self._last_cursor_monitor = cursor_monitor
            
        except Exception as e:
            log.debug(f"Error handling active window focus change: {e}")
        
        return state_cleared

    def _update_visibility(self) -> None:
        """Update waybar visibility based on current state."""
        # Build cursor position from sensor state
        self._cursor = self._get_cursor_position_for_decision()
        
        # Use cached workspace mapping (populated by _refresh_state)
        active_workspaces_by_monitor = self._active_workspaces_by_monitor

        # Check fullscreen state and hide sensors as needed
        if self._cursor_manager:
            for monitor in self._monitors:
                active_workspace = active_workspaces_by_monitor.get(monitor.id)
                
                # Only hide sensor if fullscreen is on the active workspace
                if self._fullscreen_handler.is_fullscreen(monitor.id, active_workspace):
                    self._cursor_manager.hide_sensor(monitor.name)
                else:
                    self._cursor_manager.show_sensor(monitor.name)

        # Decide transitions
        transitions = self._state_engine.decide_transitions(
            managed_monitor_ids=self._waybar_manager.get_all_ids(),
            cursor=self._cursor or CursorPosition(0, 0),
            monitors=self._monitors,
            clients=self._clients,
            active_workspace_ids=self._active_workspaces,
            active_workspaces_by_monitor=active_workspaces_by_monitor,
            cursor_in_sensor_zone=self._cursor_in_sensor_zone,
            autohide_monitor_ids=self._resolved_selection.autohide_ids,
            show_monitor_ids=self._resolved_selection.show_ids,
            monitor_lists_configured=self._resolved_selection.monitor_lists_configured,
            fullscreen_handler=self._fullscreen_handler,
        )

        # Apply transitions with safety checks
        for monitor_id, old_state, new_state in transitions:
            instance = self._waybar_manager.get_instance(monitor_id)
            if instance:
                # Skip if waybar is still in startup grace period
                if monitor_id in self._waybar_start_times:
                    elapsed = time.time() - self._waybar_start_times[monitor_id]
                    if elapsed < self.STARTUP_GRACE_PERIOD:
                        log.debug(f"Monitor {monitor_id}: skipping toggle during startup grace period ({elapsed:.1f}s)")
                        continue
                    else:
                        # Grace period passed, clean up the entry
                        del self._waybar_start_times[monitor_id]
                
                # Skip if process is not alive
                if not instance.is_alive():
                    log.warning(f"Monitor {monitor_id}: waybar process not alive, skipping toggle")
                    continue
                
                try:
                    instance.toggle()
                    self._waybar_manager.set_state(monitor_id, new_state)
                    log.info(f"Monitor {monitor_id}: {old_state} -> {new_state}")
                except RuntimeError as e:
                    log.warning(f"Monitor {monitor_id}: toggle failed - {e}")
                    pass

    def _get_cursor_position_for_decision(self) -> CursorPosition:
        """Get cursor position for visibility decisions.
        
        Simplified version - no longer queries actual cursor position.
        Just uses sensor state to determine position.
        """
        # Default to 0,0 if no cursor in any sensor
        default_pos = CursorPosition(0, 0)

        # Find which monitor has cursor in sensor zone
        for monitor_id, in_sensor in self._cursor_in_sensor_zone.items():
            if in_sensor:
                # Cursor is in sensor zone - position is near top
                # Find the monitor to get its X position
                for monitor in self._monitors:
                    if monitor.id == monitor_id:
                        # Return position at top center of this monitor
                        return CursorPosition(
                            monitor.x + monitor.width // 2,
                            monitor.y + CursorSensor.SENSOR_HEIGHT // 2
                        )

        # No cursor in any sensor - return default
        # We don't query actual position anymore to avoid subprocess overhead
        return default_pos

    def _process_gtk_events(self) -> None:
        """Process pending GTK events (required for cursor sensor events).
        
        Limits events per iteration to prevent infinite loop if GTK
        keeps generating events (e.g. during compositor state changes).
        """
        try:
            max_events = self.GTK_MAX_EVENTS_PER_TICK
            processed = 0
            while Gtk.events_pending() and processed < max_events:
                Gtk.main_iteration_do(blocking=False)
                processed += 1
            
            # Log if we're hitting the limit (indicates potential issue)
            if processed >= max_events:
                log.warning(f"GTK event loop processing limit hit ({max_events} events)")
        except Exception:
            pass  # Ignore GTK errors

    def run(self) -> None:
        """Main control loop."""
        self._running = True

        try:
            while self._running:
                self._loop_tick += 1
                self._cursor_query_reasons_this_tick.clear()
                # Create sensors on first iteration if needed, or retry periodically
                need_sensor_update = self._sensors_need_update
                if not need_sensor_update and self._cursor_manager:
                    # Retry periodically if we don't have all expected sensors
                    self._sensor_retry_counter += 1
                    if self._sensor_retry_counter >= self.SENSOR_RETRY_INTERVAL:
                        self._sensor_retry_counter = 0
                        expected_sensors = len(self._resolved_selection.autohide_ids)
                        if not self._resolved_selection.monitor_lists_configured:
                            # All monitors have autohide by default
                            expected_sensors = len(self._monitors)
                        actual_sensors = self._cursor_manager.get_sensor_count()
                        if actual_sensors < expected_sensors:
                            log.debug(f"Retrying sensor creation: {actual_sensors}/{expected_sensors} sensors")
                            need_sensor_update = True
                
                if need_sensor_update and self._cursor_manager:
                    autohide_ids = list(self._resolved_selection.autohide_ids)
                    if not self._resolved_selection.monitor_lists_configured:
                        autohide_ids = [
                            mid for mid in self._waybar_manager.get_all_ids()
                            if not self._is_show_monitor(mid)
                        ]
                    self._cursor_manager.update_monitors(self._monitors, autohide_ids)
                    self._sensors_need_update = False

                # Process GTK events (must be done for cursor sensors to work)
                self._process_gtk_events()

                # Process events (both cursor and Hyprland events)
                self._process_events()

                # Process scheduled hide rechecks with one shared cursor query.
                self._process_exit_checks()

                # Check process health
                self._check_process_health()

                self._finish_loop_tick()

                # Short sleep to prevent busy-waiting but still be responsive
                time.sleep(self.MAIN_LOOP_INTERVAL)

        except KeyboardInterrupt:
            pass
        finally:
            self._finish_loop_tick()
            self.shutdown()

    def shutdown(self) -> None:
        """Graceful shutdown."""
        log.info("Shutting down...")

        self._exit_checks.clear()

        # Stop socket2 listener
        if self._socket2_listener:
            self._socket2_listener.stop()

        # Shutdown cursor manager
        if self._cursor_manager:
            self._cursor_manager.shutdown()

        # Kill all waybar instances
        if self._waybar_manager:
            self._waybar_manager.kill_all()

        self._running = False
        log.info("Shutdown complete")

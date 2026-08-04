"""Main controller orchestrating waybar autohide functionality."""

from dataclasses import dataclass
import logging
import signal
import time
from queue import Empty
from typing import Dict, List, Optional, Set

from .config import Config, ResolvedMonitorSelection, WAYBAR_PROC, WaybarState
from .cursor import CursorEnter, CursorLeave, CursorManager
from .event_buffer import EventBuffer
from .hyprland import (
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
from .state import StateEngine
from .processes import find_named_pids, terminate_pids
from .waybar import WaybarInstance, WaybarManager

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

log = logging.getLogger("waybar-pilot")


@dataclass
class PendingExitCheck:
    """Scheduled hide recheck handled by the main loop."""

    next_check_at: float


@dataclass
class PendingStartupHide:
    """Initial hide scheduled after a Waybar process starts."""

    instance: WaybarInstance
    hide_at: float


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
    MAIN_LOOP_INTERVAL = 0.05  # 50 ms between main loop iterations
    STARTUP_GRACE_PERIOD = 0.5  # Wait before first hide after waybar starts
    EXIT_GRACE_PERIOD = 0.1  # Initial delay after cursor leaves sensor
    CURSOR_POLL_SOCKET_TIMEOUT = 0.2
    CURSOR_POLL_BACKOFF_INITIAL = 0.25
    CURSOR_POLL_BACKOFF_MAX = 2.0

    # --- GTK event processing ---
    GTK_MAX_EVENTS_PER_TICK = 50  # Max GTK events processed per main loop tick
    MAX_EVENTS_PER_TICK = 64

    # --- Sensor retry ---
    SENSOR_RETRY_INTERVAL = 10  # Main loop ticks between sensor creation retries

    # --- Startup coordination ---
    STARTUP_EXTERNAL_WAYBAR_WAIT = 3.0  # Seconds to wait for Omarchy's waybar to start

    def __init__(self, config: Config):
        """Initialize the controller.

        Args:
            config: Application configuration
        """
        self._config = config
        self._running = False

        # Components (initialized in initialize())
        self._hyprland: Optional[HyprlandClient] = None
        self._waybar_manager: Optional[WaybarManager] = None
        self._state_engine: Optional[StateEngine] = None
        self._event_queue: Optional[EventBuffer] = None
        self._socket2_listener: Optional[Socket2Listener] = None
        self._cursor_manager: Optional[CursorManager] = None
        self._fullscreen_handler: Optional[FullscreenHandler] = None

        # State cache
        self._monitors: List[Monitor] = []
        self._clients: List[Client] = []
        self._active_workspaces_by_monitor: Dict[int, int] = {}

        # Cursor tracking
        self._cursor_in_sensor_zone: Dict[int, bool] = {}  # monitor_id -> bool
        self._exit_checks: Dict[int, PendingExitCheck] = {}
        self._last_cursor_monitor: Optional[int] = (
            None  # Track cursor monitor to detect teleport
        )
        self._loop_tick = 0
        self._cursor_query_reasons_this_tick: List[str] = []
        self._cursor_this_tick: Optional[CursorPosition] = None
        self._visible_threshold_polling_ids: Set[int] = set()
        self._cursor_poll_degraded = False
        self._cursor_poll_next_attempt_at = 0.0
        self._cursor_poll_backoff = self.CURSOR_POLL_BACKOFF_INITIAL

        # Startup grace period tracking
        self._startup_hides: Dict[int, PendingStartupHide] = {}

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
        log.info("Received signal %s, shutting down...", signum)
        self._running = False

    def initialize(self) -> bool:
        """Initialize all components."""
        initialized = False
        try:
            # Check Hyprland is running
            self._hyprland = HyprlandClient()
            if not self._hyprland.is_running():
                log.error("Hyprland is not running")
                return False

            # Initialize state engine
            self._state_engine = StateEngine()

            # Initialize fullscreen handler
            self._fullscreen_handler = FullscreenHandler()

            # Initialize waybar manager
            self._waybar_manager = WaybarManager()

            # Get initial state
            self._refresh_state()
            self._resolve_monitor_selection(strict=True)

            # Log detected monitors
            for m in self._monitors:
                log.info(
                    f"Detected monitor: {m.name} (ID {m.id}, {m.width}x{m.height})"
                )

            # Wait for Omarchy's waybar to start + network to come up
            self._wait_for_startup_conditions()

            # Kill any existing waybar processes
            self._kill_existing_waybar()

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
            self._event_queue = EventBuffer()
            self._socket2_listener = Socket2Listener(
                event_queue=self._event_queue,
                socket_path=self._hyprland.get_socket2_path(),
            )
            self._socket2_listener.start()

            # Initialize cursor manager
            try:
                self._cursor_manager = CursorManager(
                    event_queue=self._event_queue,
                )
                # Sensors will be created on first main loop iteration
                # to avoid blocking during initialization
                self._sensors_need_update = True
            except Exception as e:
                log.error(f"Failed to initialize cursor detection: {e}")
                return False

            log.info(
                f"Managing {len(self._waybar_manager)} monitors: "
                f"{self._waybar_manager.get_all_ids()}"
            )
            log.info(f"Autohide selectors: {self._config.autohide_monitors}")
            log.info(f"Show selectors: {self._config.show_monitors}")
            log.info(
                f"Resolved autohide IDs: {sorted(self._resolved_selection.autohide_ids)}"
            )
            log.info(f"Resolved show IDs: {sorted(self._resolved_selection.show_ids)}")
            log.info("Waybar autohide is running...")

            initialized = True
            return True

        except Exception as e:
            log.exception("Error during initialization: %s", e)
            return False
        finally:
            if not initialized:
                self.shutdown()

    def _network_available(self) -> bool:
        """Check if network + DNS is reachable.

        Uses HTTPS probe (not raw IP) so readiness for weather/update modules
        is accurately detected (verifies DNS + TLS + HTTP).
        """
        import urllib.request
        import urllib.error

        try:
            req = urllib.request.Request(
                "https://one.one.one.one",
                headers={"User-Agent": "waybar-pilot/0.2 network-probe"},
            )
            with urllib.request.urlopen(req, timeout=1.5):
                return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
            return False
        except Exception:
            return False

    def _wait_for_startup_conditions(self) -> None:
        """Wait for network and external waybar startup before launching ours.

        Omarchy's waybar starts shortly after Hyprland. We need to give it
        time to appear so our subsequent pkill catches it. We also wait for
        network so modules like weather work on first run.
        """
        network_wait = self._config.wait_for_network
        external_wait = self.STARTUP_EXTERNAL_WAYBAR_WAIT
        deadline = time.time() + max(network_wait, external_wait)

        network_ready = network_wait <= 0
        external_wait_done = False
        start = time.time()

        log.debug(
            "Waiting for startup conditions (network<=%ds external<=%.1fs)",
            network_wait,
            external_wait,
        )

        while time.time() < deadline:
            if not network_ready:
                network_ready = self._network_available()

            if not external_wait_done:
                if self._waybar_manager and self._waybar_manager.has_external_waybars():
                    external_wait_done = True
                elif time.time() - start >= external_wait:
                    external_wait_done = True

            if network_ready and external_wait_done:
                if time.time() - start > 0.5:
                    log.info(
                        "Startup conditions met after %.1fs",
                        time.time() - start,
                    )
                return

            time.sleep(0.2)

        log.warning(
            "Startup timeout after %.1fs: network=%s external=%s",
            time.time() - start,
            network_ready,
            external_wait_done,
        )

    def _kill_existing_waybar(self) -> None:
        """Kill every owned waybar before starting managed instances."""
        terminate_pids(find_named_pids(WAYBAR_PROC))

    def _get_managed_monitor_ids(self) -> List[int]:
        """Return all monitor IDs; selectors only change visibility behavior."""
        return [monitor.id for monitor in self._monitors]

    def _resolve_monitor_selection(self, strict: bool = False) -> None:
        """Resolve configured monitor selectors to current monitor IDs."""
        try:
            self._resolved_selection = self._config.resolve_monitor_selection(
                self._monitors
            )
        except ValueError:
            if strict:
                raise
            log.exception(
                "Failed to resolve monitor selectors; keeping previous mapping"
            )
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
        # Show monitors should ALWAYS be visible
        if self._is_show_monitor(monitor_id):
            instance.state = WaybarState.VISIBLE
            return

        should_show = self._state_engine.should_show(
            cursor_in_sensor_zone=self._cursor_in_sensor_zone.get(monitor_id, False),
        )

        if should_show:
            instance.state = WaybarState.VISIBLE
        else:
            # Waybar starts visible. Keep state matching reality until delayed
            # startup hide actually toggles it, otherwise early cursor events can
            # invert actual and tracked visibility.
            instance.state = WaybarState.VISIBLE

            self._startup_hides[monitor_id] = PendingStartupHide(
                instance=instance,
                hide_at=time.monotonic() + self.STARTUP_GRACE_PERIOD,
            )

    def _process_startup_hides(self) -> None:
        """Apply due initial hides from the main loop."""
        now = time.monotonic()
        due_monitor_ids = [
            monitor_id
            for monitor_id, pending in self._startup_hides.items()
            if pending.hide_at <= now
        ]

        for monitor_id in due_monitor_ids:
            pending = self._startup_hides.pop(monitor_id, None)
            if pending is None:
                continue

            instance = pending.instance
            current = self._waybar_manager.get_instance(monitor_id)
            if current is not instance or not instance.is_alive():
                continue
            if self._cursor_in_bar_zone(monitor_id):
                log.info(
                    "Monitor %s: startup hide skipped, cursor in bar zone",
                    monitor_id,
                )
                continue
            if instance.state != WaybarState.VISIBLE:
                continue

            try:
                instance.hide()
            except RuntimeError:
                continue

            log.info("Monitor %s: hidden after startup grace period", monitor_id)

    def _cursor_in_bar_zone(self, monitor_id: int) -> bool:
        """Check whether the real cursor is inside the bar detection zone."""
        try:
            cursor_pos = self._hyprland.get_cursor_position()
            cursor_monitor = self._state_engine.get_cursor_monitor(
                cursor_pos,
                self._monitors,
            )
            if cursor_monitor != monitor_id:
                return False
            monitor = next((m for m in self._monitors if m.id == monitor_id), None)
            if not monitor:
                return False
            relative_y = cursor_pos.y - monitor.y
            return 0 <= relative_y <= self._config.visible_cursor_height
        except Exception as e:
            log.debug(f"Error checking startup cursor position: {e}")
            return False

    def _refresh_state(self) -> None:
        """Refresh state from Hyprland.

        Uses a single ``hyprctl -j monitors`` call to obtain monitors and
        their active workspace mapping.
        """
        try:
            (
                self._monitors,
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

        # Process a bounded batch so GTK and health checks cannot be starved.
        events_to_process = []
        try:
            while len(events_to_process) < self.MAX_EVENTS_PER_TICK:
                event = self._event_queue.get_nowait()
                events_to_process.append(event)
        except Empty:
            pass

        # Handle events
        needs_refresh = False
        needs_visibility_update = False
        has_active_window_event = False
        last_monitor_event = None
        monitors_before_refresh = self._monitors

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
                    last_monitor_event = event
                    needs_refresh = True
                    needs_visibility_update = True
                elif event.event_type == EventType.ACTIVE_WORKSPACE:
                    needs_refresh = True
                    needs_visibility_update = (
                        True  # Workspace switch affects fullscreen check
                    )
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

        if needs_refresh:
            self._refresh_state()

        if last_monitor_event is not None:
            self._handle_monitor_change(last_monitor_event, monitors_before_refresh)

        if has_active_window_event:
            focus_state_cleared = self._handle_active_window_focus_change()
            if focus_state_cleared:
                needs_visibility_update = True

        # Only check for cursor monitor teleports when events suggest it
        # (avoids spawning hyprctl cursorpos every 50ms)
        if (
            needs_refresh
            and any(
                isinstance(e, HyprlandEvent)
                and e.event_type
                in (
                    EventType.ACTIVE_WORKSPACE,
                    EventType.FULLSCREEN,
                )
                for e in events_to_process
            )
            and not has_active_window_event
        ):
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
        elif isinstance(event, CursorLeave):
            # Leave events are informational in the hybrid design. The actual
            # visible hide threshold is enforced from real cursor position.
            return

    def _start_bar_exit_timer(self, monitor_id: int) -> None:
        """Schedule the first hide recheck after leaving the reveal zone."""
        log.debug(
            "Monitor %s: scheduling hide grace timer for %.2fs",
            monitor_id,
            self.EXIT_GRACE_PERIOD,
        )
        self._schedule_exit_check(monitor_id, self.EXIT_GRACE_PERIOD)

    def _schedule_exit_check(self, monitor_id: int, delay: float) -> None:
        """Schedule a hide recheck to be processed by the main loop."""
        self._exit_checks[monitor_id] = PendingExitCheck(
            next_check_at=time.monotonic() + delay,
        )

    def _clear_sensor_zone_state(
        self,
        monitor_id: int,
        *,
        schedule_exit_grace: bool,
        reason: str,
    ) -> bool:
        """Clear stale sensor state and optionally preserve hide grace."""
        if not self._cursor_in_sensor_zone.get(monitor_id, False):
            return False

        self._cursor_in_sensor_zone[monitor_id] = False

        if schedule_exit_grace:
            instance = self._waybar_manager.get_instance(monitor_id)
            if (
                instance
                and instance.state == WaybarState.VISIBLE
                and monitor_id not in self._exit_checks
            ):
                log.debug(
                    "Monitor %s: stale sensor clear (%s), starting hide grace",
                    monitor_id,
                    reason,
                )
                self._start_bar_exit_timer(monitor_id)
        else:
            self._exit_checks.pop(monitor_id, None)

        return True

    def _get_cursor_position_logged(self, reason: str) -> CursorPosition:
        """Get cursor position, cached per main-loop tick.

        The first caller each tick pays for the ``hyprctl cursorpos``
        query; subsequent callers in the same tick read the cached
        value. The cache is cleared at the top of each loop iteration.
        """
        if self._cursor_this_tick is not None:
            self._cursor_query_reasons_this_tick.append(f"{reason} (cached)")
            return self._cursor_this_tick
        self._cursor_query_reasons_this_tick.append(reason)
        self._cursor_this_tick = self._hyprland.get_cursor_position()
        return self._cursor_this_tick

    def _finish_loop_tick(self) -> None:
        """Emit a log if this loop tick queried cursor position multiple times."""
        if len(self._cursor_query_reasons_this_tick) > 2:
            log.debug(
                "Tick %s: multiple cursor queries in one loop: %s",
                self._loop_tick,
                ", ".join(self._cursor_query_reasons_this_tick),
            )
        self._cursor_query_reasons_this_tick.clear()

    def _get_threshold_cursor_position(self) -> Optional[CursorPosition]:
        """Query cursor over the socket with degraded-mode backoff."""
        now = time.monotonic()
        if self._cursor_poll_degraded and now < self._cursor_poll_next_attempt_at:
            return None

        self._cursor_query_reasons_this_tick.append("visible_threshold (socket)")
        try:
            cursor_pos = self._hyprland.get_cursor_position_socket(
                timeout=self.CURSOR_POLL_SOCKET_TIMEOUT
            )
        except (HyprlandConnectionError, UnicodeError, ValueError) as exc:
            if not self._cursor_poll_degraded:
                self._cursor_poll_degraded = True
                self._cursor_poll_backoff = self.CURSOR_POLL_BACKOFF_INITIAL
                log.warning(
                    "Cursor threshold polling degraded; retaining current bar state: %s",
                    exc,
                )
            else:
                self._cursor_poll_backoff = min(
                    self._cursor_poll_backoff * 2,
                    self.CURSOR_POLL_BACKOFF_MAX,
                )

            self._cursor_poll_next_attempt_at = (
                time.monotonic() + self._cursor_poll_backoff
            )
            return None

        if self._cursor_poll_degraded:
            log.info("Cursor threshold polling recovered")
        self._cursor_poll_degraded = False
        self._cursor_poll_next_attempt_at = 0.0
        self._cursor_poll_backoff = self.CURSOR_POLL_BACKOFF_INITIAL
        self._cursor_this_tick = cursor_pos
        return cursor_pos

    def _process_exit_checks(self) -> None:
        """Process due hide rechecks without extra cursor polling."""
        if not self._exit_checks:
            return

        now = time.monotonic()
        due_monitor_ids = [
            monitor_id
            for monitor_id, pending in self._exit_checks.items()
            if pending.next_check_at <= now
        ]
        if not due_monitor_ids:
            return

        needs_visibility_update = False
        for monitor_id in due_monitor_ids:
            pending = self._exit_checks.get(monitor_id)
            if not pending or pending.next_check_at > now:
                continue

            self._exit_checks.pop(monitor_id, None)
            if self._cursor_in_sensor_zone.get(monitor_id, False):
                continue

            log.debug("Monitor %s: hide grace elapsed, hiding waybar", monitor_id)
            needs_visibility_update = True

        if needs_visibility_update:
            self._update_visibility()

    def _process_visible_cursor_thresholds(self) -> None:
        """Use actual cursor position to enforce the visible hide threshold.

        GTK crossing events are still useful for reveal, but their local event
        coordinates are not stable enough to be the sole source of truth for
        the visible bar threshold. While an autohide bar is visible, use one
        shared cursor query to track whether the pointer is still within the
        configured visible zone.

        Important: we intentionally do not use GTK enter/leave events alone to
        decide hiding. In practice, the top-edge layer-shell sensor can report
        inconsistent crossing coordinates depending on compositor/window state,
        which made hide timing and `--hide-margin` behavior unreliable across the
        experiments in this branch. Reveal stays event-driven; hide threshold
        correctness comes from actual cursor position.
        """
        visible_autohide_ids = []
        for monitor in self._monitors:
            if self._is_show_monitor(monitor.id):
                continue

            instance = self._waybar_manager.get_instance(monitor.id)
            if instance and instance.state == WaybarState.VISIBLE:
                visible_autohide_ids.append(monitor.id)

        current_polling_ids = set(visible_autohide_ids)
        for monitor_id in sorted(
            current_polling_ids - self._visible_threshold_polling_ids
        ):
            log.debug("Monitor %s: visible-threshold polling active", monitor_id)
        for monitor_id in sorted(
            self._visible_threshold_polling_ids - current_polling_ids
        ):
            log.debug("Monitor %s: visible-threshold polling inactive", monitor_id)
        self._visible_threshold_polling_ids = current_polling_ids

        if not visible_autohide_ids:
            return

        cursor_pos = self._get_threshold_cursor_position()
        if cursor_pos is None:
            return
        cursor_monitor = self._state_engine.get_cursor_monitor(
            cursor_pos, self._monitors
        )

        hide_threshold = self._config.visible_cursor_height
        for monitor_id in visible_autohide_ids:
            monitor = next((m for m in self._monitors if m.id == monitor_id), None)
            if not monitor:
                continue

            relative_y = cursor_pos.y - monitor.y
            inside_threshold = (
                cursor_monitor == monitor_id and relative_y <= hide_threshold
            )
            was_inside = self._cursor_in_sensor_zone.get(monitor_id, False)

            if inside_threshold:
                if not was_inside:
                    log.debug(
                        "Monitor %s: actual cursor re-entered visible threshold (y=%s, threshold=%s)",
                        monitor_id,
                        relative_y,
                        hide_threshold,
                    )
                self._cursor_in_sensor_zone[monitor_id] = True
                self._exit_checks.pop(monitor_id, None)
                continue

            if was_inside:
                log.debug(
                    "Monitor %s: actual cursor crossed visible threshold (cursor_monitor=%s, y=%s, threshold=%s)",
                    monitor_id,
                    cursor_monitor,
                    relative_y,
                    hide_threshold,
                )
                self._cursor_in_sensor_zone[monitor_id] = False
                self._start_bar_exit_timer(monitor_id)

    def _handle_monitor_change(
        self,
        event: HyprlandEvent,
        previous_monitors: List[Monitor],
    ) -> None:
        """Handle monitor add/remove events."""
        log.info(f"Monitor change: {event.event_type.value} - {event.raw_data.strip()}")

        # Preserve old names so removed-monitor sensors can be cleaned up.
        old_monitor_names = {m.id: m.name for m in previous_monitors}
        self._resolve_monitor_selection()

        # Get current managed monitors
        current_ids = set(self._waybar_manager.get_all_ids())
        available_ids = {m.id for m in self._monitors}
        managed_ids = set(self._get_managed_monitor_ids())

        log.info(
            f"Waybar on: {current_ids}, available: {available_ids}, managed: {managed_ids}"
        )

        # Find monitors to add/remove
        to_remove = current_ids - available_ids
        to_add = (managed_ids & available_ids) - current_ids

        # Remove dead monitors
        for monitor_id in to_remove:
            monitor_name = old_monitor_names.get(monitor_id)
            log.info(f"Removing waybar for monitor {monitor_id} ({monitor_name})")
            self._waybar_manager.kill_monitor(monitor_id)
            self._fullscreen_handler.remove_monitor(monitor_id)
            self._exit_checks.pop(monitor_id, None)
            self._cursor_in_sensor_zone.pop(monitor_id, None)
            self._startup_hides.pop(monitor_id, None)
            if self._last_cursor_monitor == monitor_id:
                self._last_cursor_monitor = None
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
                mid for mid in managed_ids if not self._is_show_monitor(mid)
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
            current_monitor = self._state_engine.get_cursor_monitor(
                cursor_pos, self._monitors
            )

            log.debug(
                f"Cursor check: last={self._last_cursor_monitor}, current={current_monitor}, pos=({cursor_pos.x},{cursor_pos.y})"
            )

            if current_monitor is not None and self._last_cursor_monitor is not None:
                if current_monitor != self._last_cursor_monitor:
                    # Cursor moved to a different monitor
                    old_monitor = self._last_cursor_monitor
                    log.debug(
                        f"Cursor moved from monitor {old_monitor} to {current_monitor}"
                    )

                    # Clear stale sensor state for the old monitor
                    if self._clear_sensor_zone_state(
                        old_monitor,
                        schedule_exit_grace=True,
                        reason="cursor monitor changed",
                    ):
                        log.debug(
                            f"Clearing stale sensor state for monitor {old_monitor}"
                        )
                        state_cleared = True
            elif self._last_cursor_monitor is None and current_monitor is not None:
                log.debug(f"Initializing cursor monitor tracking: {current_monitor}")

            # Update last known cursor monitor
            self._last_cursor_monitor = current_monitor

        except Exception as e:
            # Log errors for debugging
            log.warning(f"Error checking cursor monitor: {e}")

        return state_cleared

    def _handle_active_window_focus_change(self) -> bool:
        """Handle active window change to detect cursor focus movement.

        When a new window takes focus (e.g., browser opening from URL click),
        Hyprland may move the cursor to the new window. This can happen:
        1. On the same monitor (cursor below sensor zone)
        2. On a different monitor (cursor warped to new monitor)

        In both cases, the sensor won't detect a leave event because the cursor
        was moved programmatically, not by user movement.

        This method queries the actual cursor position and clears stale sensor
        state if the cursor has moved away from the sensor zone or to another monitor.

        Returns:
            True if stale sensor state was cleared and visibility needs update
        """
        state_cleared = False

        try:
            # Get current cursor position to verify actual location
            cursor_pos = self._get_cursor_position_logged("active_window_focus")

            # Find which monitor the cursor is actually on
            cursor_monitor = self._state_engine.get_cursor_monitor(
                cursor_pos, self._monitors
            )

            if cursor_monitor is None:
                return False

            # Check if cursor moved to a DIFFERENT monitor
            if (
                self._last_cursor_monitor is not None
                and cursor_monitor != self._last_cursor_monitor
            ):
                # Cursor moved to different monitor - clear old monitor's state
                old_monitor = self._last_cursor_monitor
                if self._clear_sensor_zone_state(
                    old_monitor,
                    schedule_exit_grace=True,
                    reason="active window monitor change",
                ):
                    log.debug(
                        f"Active window change: cursor moved to monitor "
                        f"{cursor_monitor} from {old_monitor}, clearing stale state"
                    )
                    state_cleared = True

            # Also check if cursor is below sensor zone on the SAME monitor
            if cursor_monitor in self._cursor_in_sensor_zone:
                if self._cursor_in_sensor_zone[cursor_monitor]:
                    # Cursor is marked as "in sensor zone" but let's verify
                    monitor = next(
                        (m for m in self._monitors if m.id == cursor_monitor), None
                    )
                    if monitor:
                        relative_y = cursor_pos.y - monitor.y

                        # Calculate sensor zone height from config
                        sensor_zone_height = self._config.visible_cursor_height

                        # If cursor is below the sensor zone, it's likely been moved
                        if (
                            relative_y > sensor_zone_height
                            and self._clear_sensor_zone_state(
                                cursor_monitor,
                                schedule_exit_grace=True,
                                reason="active window moved below sensor zone",
                            )
                        ):
                            log.debug(
                                f"Active window change: cursor moved below sensor zone "
                                f"on monitor {cursor_monitor} (y={relative_y}, "
                                f"zone={sensor_zone_height}px), clearing stale state"
                            )

                            state_cleared = True

            # Update last cursor monitor tracking
            self._last_cursor_monitor = cursor_monitor

        except Exception as e:
            log.debug(f"Error handling active window focus change: {e}")

        return state_cleared

    def _update_visibility(self) -> None:
        """Update waybar visibility based on current state."""
        # Use cached workspace mapping (populated by _refresh_state)
        active_workspaces_by_monitor = self._active_workspaces_by_monitor

        # Check fullscreen state and hide sensors as needed
        if self._cursor_manager:
            for monitor in self._monitors:
                active_workspace = active_workspaces_by_monitor.get(monitor.id)

                # Only hide sensor if fullscreen is on the active workspace
                if self._fullscreen_handler.is_fullscreen(monitor.id, active_workspace):
                    self._cursor_manager.hide_sensor(monitor.name)
                    # Drop stale cursor state so that when fullscreen exits,
                    # we don't briefly treat the bar as revealed and flash it.
                    self._cursor_in_sensor_zone.pop(monitor.id, None)
                    self._exit_checks.pop(monitor.id, None)
                else:
                    self._cursor_manager.show_sensor(monitor.name)

        # Decide desired states; WaybarInstance remains the visibility authority.
        desired_states = self._state_engine.decide_states(
            managed_monitor_ids=self._waybar_manager.get_all_ids(),
            active_workspaces_by_monitor=active_workspaces_by_monitor,
            cursor_in_sensor_zone=self._cursor_in_sensor_zone,
            autohide_monitor_ids=self._resolved_selection.autohide_ids,
            show_monitor_ids=self._resolved_selection.show_ids,
            monitor_lists_configured=self._resolved_selection.monitor_lists_configured,
            fullscreen_handler=self._fullscreen_handler,
        )

        # Apply transitions with safety checks
        for monitor_id, new_state in desired_states.items():
            instance = self._waybar_manager.get_instance(monitor_id)
            if instance:
                old_state = instance.state
                if old_state == new_state:
                    continue

                active_workspace = active_workspaces_by_monitor.get(monitor_id)
                is_fullscreen = self._fullscreen_handler.is_fullscreen(
                    monitor_id, active_workspace
                )

                if (
                    self._is_show_monitor(monitor_id)
                    and not is_fullscreen
                    and new_state != WaybarState.VISIBLE
                ):
                    log.warning(
                        "Monitor %s: refusing hidden transition for always-show monitor",
                        monitor_id,
                    )
                    continue

                pending_exit = self._exit_checks.get(monitor_id)
                if (
                    pending_exit
                    and old_state == WaybarState.VISIBLE
                    and new_state == WaybarState.HIDDEN
                    and not is_fullscreen
                ):
                    log.debug(
                        "Monitor %s: hide deferred, exit grace still pending",
                        monitor_id,
                    )
                    continue

                # Skip if waybar is still in startup grace period
                pending_startup_hide = self._startup_hides.get(monitor_id)
                if (
                    pending_startup_hide
                    and time.monotonic() < pending_startup_hide.hide_at
                ):
                    log.debug(
                        "Monitor %s: skipping toggle during startup grace period",
                        monitor_id,
                    )
                    continue

                # Skip if process is not alive
                if not instance.is_alive():
                    log.warning(
                        f"Monitor {monitor_id}: waybar process not alive, skipping toggle"
                    )
                    continue

                try:
                    instance.toggle()
                    log.info(f"Monitor {monitor_id}: {old_state} -> {new_state}")
                except RuntimeError as e:
                    log.warning(f"Monitor {monitor_id}: toggle failed - {e}")
                    pass

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
                log.warning(
                    f"GTK event loop processing limit hit ({max_events} events)"
                )
        except Exception:
            log.exception("GTK event processing failed")

    def run(self) -> None:
        """Main control loop."""
        self._running = True

        try:
            while self._running:
                self._loop_tick += 1
                self._cursor_query_reasons_this_tick.clear()
                self._cursor_this_tick = None
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
                            log.debug(
                                f"Retrying sensor creation: {actual_sensors}/{expected_sensors} sensors"
                            )
                            need_sensor_update = True

                if need_sensor_update and self._cursor_manager:
                    autohide_ids = list(self._resolved_selection.autohide_ids)
                    if not self._resolved_selection.monitor_lists_configured:
                        autohide_ids = [
                            mid
                            for mid in self._waybar_manager.get_all_ids()
                            if not self._is_show_monitor(mid)
                        ]
                    self._cursor_manager.update_monitors(self._monitors, autohide_ids)
                    self._sensors_need_update = False

                # Process GTK events (must be done for cursor sensors to work)
                self._process_gtk_events()

                # Process events (both cursor and Hyprland events)
                self._process_events()

                # While an autohide bar is visible, enforce the configured hide
                # threshold from actual cursor position rather than GTK crossing
                # coordinates alone.
                self._process_visible_cursor_thresholds()

                # Process scheduled hide rechecks with one shared cursor query.
                self._process_exit_checks()

                # Apply initial hides without mutating state from a worker thread.
                self._process_startup_hides()

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
        self._startup_hides.clear()

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

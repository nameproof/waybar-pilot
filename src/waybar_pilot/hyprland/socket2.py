"""Socket2 event listener for Hyprland events."""

import socket
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from queue import Queue
from typing import Dict, Optional, Set

from .client import HyprlandClient


class EventType(Enum):
    """Types of Hyprland events we care about."""

    ACTIVE_WINDOW = "activewindow"
    FULLSCREEN = "fullscreen"
    MONITOR_ADDED = "monitoradded"
    MONITOR_ADDED_V2 = "monitoraddedv2"
    MONITOR_REMOVED = "monitorremoved"
    WORKSPACE_CREATED = "createworkspace"
    WORKSPACE_DESTROYED = "destroyworkspace"
    ACTIVE_WORKSPACE = "workspace"  # Triggered when switching workspaces

    # Window-related (may need immediate check)
    WINDOW_CLOSE = "closewindow"
    WINDOW_MOVE = "movewindow"


@dataclass(frozen=True)
class HyprlandEvent:
    """Represents a parsed Hyprland socket2 event."""

    event_type: EventType
    raw_data: str
    timestamp: float


class Socket2Listener:
    """Listen to Hyprland socket2 for events.

    Runs in a background thread and pushes events to a queue.
    Provides event caching for monitor name-to-ID mapping.
    """

    def __init__(
        self,
        event_queue: Queue,
        hyprland_client: HyprlandClient,
        socket_path: Optional[Path] = None,
    ):
        """Initialize the listener.

        Args:
            event_queue: Queue to push events to
            hyprland_client: Client for querying Hyprland state
            socket_path: Optional override for socket2 path
        """
        self._event_queue = event_queue
        self._hyprland = hyprland_client
        self._socket_path = socket_path or hyprland_client.get_socket2_path()

        # Monitor name to ID mapping cache
        self._monitor_name_to_id: Dict[str, int] = {}
        self._lock = threading.Lock()

        # Event types we care about
        self._tracked_events: Set[str] = {
            "activewindow",
            "fullscreen",
            "monitoradded",
            "monitoraddedv2",
            "monitorremoved",
            "createworkspace",
            "destroyworkspace",
            "closewindow",
            "movewindow",
            "workspace",  # Active workspace changed
        }

        self._thread: Optional[threading.Thread] = None
        self._running = False

    def _initialize_monitor_cache(self) -> None:
        """Load current monitor info into cache."""
        try:
            monitors = self._hyprland.get_monitors()
            with self._lock:
                for monitor in monitors:
                    self._monitor_name_to_id[monitor.name] = monitor.id
        except Exception:
            pass  # Will retry later

    def _parse_event(self, line: str) -> Optional[HyprlandEvent]:
        """Parse an event line from socket2.

        Args:
            line: Raw event line from socket2

        Returns:
            Parsed event or None if not tracked
        """
        if ">>" not in line:
            return None

        event_type_str = line.split(">>")[0]

        if event_type_str not in self._tracked_events:
            return None

        # Map to EventType enum
        event_type_map = {
            "activewindow": EventType.ACTIVE_WINDOW,
            "fullscreen": EventType.FULLSCREEN,
            "monitoradded": EventType.MONITOR_ADDED,
            "monitoraddedv2": EventType.MONITOR_ADDED_V2,
            "monitorremoved": EventType.MONITOR_REMOVED,
            "createworkspace": EventType.WORKSPACE_CREATED,
            "destroyworkspace": EventType.WORKSPACE_DESTROYED,
            "closewindow": EventType.WINDOW_CLOSE,
            "movewindow": EventType.WINDOW_MOVE,
            "workspace": EventType.ACTIVE_WORKSPACE,
        }

        event_type = event_type_map.get(event_type_str)
        if not event_type:
            return None

        return HyprlandEvent(
            event_type=event_type,
            raw_data=line,
            timestamp=__import__("time").time(),
        )

    def _handle_monitor_added(self, line: str) -> None:
        """Update cache when monitor is added."""
        try:
            monitors = self._hyprland.get_monitors()
            with self._lock:
                for monitor in monitors:
                    self._monitor_name_to_id[monitor.name] = monitor.id
        except Exception:
            pass

    def _handle_monitor_removed(self, line: str) -> Optional[int]:
        """Get monitor ID from cache when removed.

        Args:
            line: Event line like "monitorremoved>>DP-1"

        Returns:
            Monitor ID if found in cache, None otherwise
        """
        try:
            monitor_name = line.split(">>")[1].strip()
            with self._lock:
                if monitor_name in self._monitor_name_to_id:
                    monitor_id = self._monitor_name_to_id[monitor_name]
                    del self._monitor_name_to_id[monitor_name]
                    return monitor_id
        except (IndexError, KeyError):
            pass
        return None

    def get_monitor_id_from_name(self, name: str) -> Optional[int]:
        """Look up monitor ID from name using cache.

        Args:
            name: Monitor name (e.g., "DP-1")

        Returns:
            Monitor ID if in cache, None otherwise
        """
        with self._lock:
            return self._monitor_name_to_id.get(name)

    def _listen_loop(self) -> None:
        """Main listening loop - runs in background thread."""
        import time

        while self._running:
            sock = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(self._socket_path))
                sock.settimeout(1.0)

                buffer = ""
                while self._running:
                    try:
                        data = sock.recv(4096).decode("utf-8")
                        if not data:
                            break

                        buffer += data
                        lines = buffer.split("\n")
                        buffer = lines.pop()  # Keep incomplete line

                        for line in lines:
                            if not line.strip():
                                continue

                            # Handle special events that need cache updates
                            if "monitoradded" in line:
                                self._handle_monitor_added(line)
                            elif "monitorremoved" in line:
                                self._handle_monitor_removed(line)

                            # Parse and queue event
                            event = self._parse_event(line)
                            if event:
                                self._event_queue.put(event)

                    except socket.timeout:
                        continue
                    except Exception:
                        break

            except (FileNotFoundError, ConnectionRefusedError):
                # Socket not available, retry after delay
                time.sleep(1)
                continue
            except Exception:
                time.sleep(0.5)
                continue
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass

            # Reconnect delay
            time.sleep(0.5)

    def start(self) -> threading.Thread:
        """Start the listener in a background thread.

        Returns:
            The background thread
        """
        if self._running:
            raise RuntimeError("Listener already running")

        self._running = True
        self._initialize_monitor_cache()

        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

        return self._thread

    def stop(self) -> None:
        """Stop the listener."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

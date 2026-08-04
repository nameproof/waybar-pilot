"""Socket2 event listener for Hyprland events."""

import codecs
import socket
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from queue import Queue
from typing import Optional


class EventType(Enum):
    """Types of Hyprland events we care about."""

    ACTIVE_WINDOW = "activewindow"
    FULLSCREEN = "fullscreen"
    MONITOR_ADDED = "monitoradded"
    MONITOR_ADDED_V2 = "monitoraddedv2"
    MONITOR_REMOVED = "monitorremoved"
    ACTIVE_WORKSPACE = "workspace"  # Triggered when switching workspaces

    # Window-related (may need immediate check)
    WINDOW_CLOSE = "closewindow"
    WINDOW_MOVE = "movewindow"


@dataclass(frozen=True)
class HyprlandEvent:
    """Represents a parsed Hyprland socket2 event."""

    event_type: EventType
    raw_data: str


class Socket2Listener:
    """Listen to Hyprland socket2 for events.

    Runs in a background thread and pushes events to a queue.
    """

    def __init__(
        self,
        event_queue: Queue,
        socket_path: Path,
    ):
        """Initialize the listener.

        Args:
            event_queue: Queue to push events to
            socket_path: Hyprland socket2 path
        """
        self._event_queue = event_queue
        self._socket_path = socket_path

        self._thread: Optional[threading.Thread] = None
        self._running = False

    def _parse_event(self, line: str) -> Optional[HyprlandEvent]:
        """Parse an event line from socket2.

        Args:
            line: Raw event line from socket2

        Returns:
            Parsed event or None if not tracked
        """
        if ">>" not in line:
            return None

        event_type_str = line.split(">>", 1)[0]
        try:
            event_type = EventType(event_type_str)
        except ValueError:
            return None

        return HyprlandEvent(
            event_type=event_type,
            raw_data=line,
        )

    def _listen_loop(self) -> None:
        """Main listening loop - runs in background thread."""
        while self._running:
            sock = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(self._socket_path))
                sock.settimeout(1.0)

                buffer = ""
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                while self._running:
                    try:
                        data = sock.recv(4096)
                        if not data:
                            break

                        buffer += decoder.decode(data)
                        lines = buffer.split("\n")
                        buffer = lines.pop()  # Keep incomplete line

                        for line in lines:
                            if not line.strip():
                                continue

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
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

        return self._thread

    def stop(self) -> None:
        """Stop the listener."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

"""Hyprland client for querying and interacting with the compositor."""

import json
import os
import socket
import subprocess
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from .models import Monitor, Client, CursorPosition


class HyprlandConnectionError(Exception):
    """Raised when unable to connect to Hyprland."""

    pass


class HyprlandError(Exception):
    """Raised when Hyprland returns an error."""

    pass


class HyprlandClient:
    """Client for interacting with Hyprland compositor.

    Talks to Hyprland's request socket (.socket.sock) directly instead
    of spawning the ``hyprctl`` binary for every query. Falls back to
    the binary if the socket is unavailable (e.g. wrong environment).
    The socket gives ~10-30x lower latency per query (~0.1ms vs
    ~1-3ms fork+exec) while preserving the exact same fresh-snapshot
    semantics — every call still hits the compositor, no caching.
    """

    HYPRCTL_TIMEOUT = 3.0

    def __init__(self):
        self._hyprctl_path = "hyprctl"
        self._request_socket_path: Optional[Path] = None

    def _get_request_socket_path(self) -> Path:
        """Get the path to Hyprland's request socket (.socket.sock).

        Sibling of the event socket (.socket2.sock) — same directory,
        just without the ``2``.
        """
        if self._request_socket_path is not None:
            return self._request_socket_path

        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        his = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")

        if his:
            path = Path(f"{runtime_dir}/hypr/{his}/.socket.sock")
        else:
            path = Path(f"{runtime_dir}/hypr/.socket.sock")
        self._request_socket_path = path
        return path

    def _run_socket_command(self, command: str, timeout: float = 2.0) -> str:
        """Send a command to Hyprland's request socket and return the reply.

        Args:
            command: The command string (e.g. ``j/monitors``, ``cursorpos``)
            timeout: Socket read timeout in seconds

        Returns:
            Response string from the compositor

        Raises:
            HyprlandConnectionError: If the socket cannot be reached
        """
        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(str(self._get_request_socket_path()))
            sock.sendall(command.encode("utf-8"))

            chunks: List[bytes] = []
            while True:
                data = sock.recv(8192)
                if not data:
                    break
                chunks.append(data)
            return b"".join(chunks).decode("utf-8")
        except (OSError, ConnectionError) as e:
            raise HyprlandConnectionError(
                f"Failed to talk to Hyprland request socket "
                f"({self._get_request_socket_path()}): {e}"
            )
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _run_hyprctl(self, args: List[str], check: bool = True) -> str:
        """Run a hyprctl command via the request socket and return stdout.

        Converts the ``hyprctl``-style ``args`` to a socket command
        (``-j monitors`` → ``j/monitors``, ``cursorpos`` → ``cursorpos``)
        and sends it over the Unix socket. Falls back to the
        ``hyprctl`` binary if the socket is unavailable.

        Args:
            args: Command arguments (after 'hyprctl')
            check: Whether to raise on non-zero exit code (subprocess
                fallback only; the socket has no exit codes)

        Returns:
            Command stdout as string

        Raises:
            HyprlandConnectionError: If neither socket nor binary works
            HyprlandError: If the subprocess fallback fails
        """
        if len(args) >= 2 and args[0] == "-j":
            socket_command = f"j/{args[1]}"
        else:
            socket_command = args[0]

        try:
            return self._run_socket_command(socket_command)
        except HyprlandConnectionError:
            pass

        try:
            result = subprocess.run(
                [self._hyprctl_path] + args,
                capture_output=True,
                text=True,
                check=check,
                timeout=self.HYPRCTL_TIMEOUT,
            )
            return result.stdout
        except FileNotFoundError:
            raise HyprlandConnectionError(f"hyprctl not found at {self._hyprctl_path}")
        except subprocess.CalledProcessError as e:
            raise HyprlandError(f"hyprctl {' '.join(args)} failed: {e.stderr}")
        except subprocess.TimeoutExpired as e:
            raise HyprlandConnectionError(
                f"hyprctl {' '.join(args)} timed out after {self.HYPRCTL_TIMEOUT}s"
            ) from e

    def is_running(self) -> bool:
        """Check if Hyprland is running.

        Returns:
            True if Hyprland is running and responsive
        """
        try:
            self._run_hyprctl(["version"], check=True)
            return True
        except (HyprlandConnectionError, HyprlandError):
            return False

    def get_monitors(self) -> List[Monitor]:
        """Get all connected monitors.

        Returns:
            List of Monitor objects

        Raises:
            HyprlandError: If unable to query monitors
        """
        stdout = self._run_hyprctl(["-j", "monitors"])
        data = json.loads(stdout)
        return [Monitor.from_dict(m) for m in data]

    def get_monitors_and_workspaces(
        self,
    ) -> Tuple[List[Monitor], Dict[int, int]]:
        """Get monitors and their active workspace mapping.

        Both values come from one ``hyprctl -j monitors`` snapshot.

        Returns:
            Tuple of (monitors, workspaces_by_monitor)
        """
        stdout = self._run_hyprctl(["-j", "monitors"])
        data = json.loads(stdout)

        monitors = [Monitor.from_dict(m) for m in data]
        by_monitor = {int(m["id"]): int(m["activeWorkspace"]["id"]) for m in data}
        return monitors, by_monitor

    def get_clients(self) -> List[Client]:
        """Get all window clients.

        Returns:
            List of Client objects

        Raises:
            HyprlandError: If unable to query clients
        """
        stdout = self._run_hyprctl(["-j", "clients"])
        data = json.loads(stdout)
        return [Client.from_dict(c) for c in data]

    def get_cursor_position(self) -> CursorPosition:
        """Get current cursor position.

        Returns:
            CursorPosition with x, y coordinates

        Raises:
            HyprlandError: If unable to query cursor position
        """
        stdout = self._run_hyprctl(["cursorpos"])
        return CursorPosition.from_string(stdout)

    def get_cursor_position_socket(self, timeout: float) -> CursorPosition:
        """Get cursor position directly from the request socket.

        This deliberately has no ``hyprctl`` fallback so callers can use a
        short timeout and control retry behavior without spawning processes.
        """
        stdout = self._run_socket_command("cursorpos", timeout=timeout)
        return CursorPosition.from_string(stdout)

    def get_socket2_path(self) -> Path:
        """Get the path to Hyprland's socket2 for events.

        Returns:
            Path to the socket2 Unix socket
        """
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
        his = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")

        if his:
            return Path(f"{runtime_dir}/hypr/{his}/.socket2.sock")
        return Path(f"{runtime_dir}/hypr/.socket2.sock")

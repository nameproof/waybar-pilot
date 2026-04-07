"""Waybar instance management per monitor."""

import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..config import Config, WaybarState


@dataclass
class WaybarInstance:
    """Manages a single waybar instance for a specific monitor.

    Handles process lifecycle, config file management, and state tracking.
    """

    monitor_id: int
    monitor_name: str
    config: Config
    _process: Optional[subprocess.Popen] = field(default=None, repr=False)
    _state: WaybarState = field(default=WaybarState.VISIBLE)
    _config_path: Optional[Path] = field(default=None, repr=False)

    def __post_init__(self):
        if self._process is None:
            self._start_process()
            # Note: _state defaults to VISIBLE since waybar process starts visible
            # Controller's _sync_initial_state will correct this if needed

    @staticmethod
    def _strip_jsonc_comments(text: str) -> str:
        """Strip C-style comments from JSONC text.

        Handles // line comments and /* block comments */ while
        preserving strings that contain comment-like sequences.
        """
        result = []
        i = 0
        in_string = False
        length = len(text)

        while i < length:
            ch = text[i]

            # Track string boundaries (respecting escapes)
            if ch == '"' and (i == 0 or text[i - 1] != "\\"):
                in_string = not in_string
                result.append(ch)
                i += 1
                continue

            if in_string:
                result.append(ch)
                i += 1
                continue

            # Line comment
            if ch == "/" and i + 1 < length and text[i + 1] == "/":
                # Skip until end of line
                i += 2
                while i < length and text[i] != "\n":
                    i += 1
                continue

            # Block comment
            if ch == "/" and i + 1 < length and text[i + 1] == "*":
                i += 2
                while i + 1 < length and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2  # skip closing */
                continue

            result.append(ch)
            i += 1

        return "".join(result)

    def _create_config(self) -> Path:
        """Create a temporary config file for this monitor.

        Returns:
            Path to the temporary config file
        """
        # Read base config
        config_dir = Path.home() / ".config" / "waybar"
        base_config = config_dir / "config.jsonc"

        if not base_config.exists():
            base_config = config_dir / "config"

        config_content = {}
        if base_config.exists():
            try:
                with open(base_config) as f:
                    raw = f.read()
                stripped = self._strip_jsonc_comments(raw)
                config_content = json.loads(stripped)
            except (json.JSONDecodeError, IOError):
                config_content = {}

        # Set output to this specific monitor
        config_content["output"] = self.monitor_name

        # Create temp file with proper cleanup handling
        fd, path = tempfile.mkstemp(
            suffix=".jsonc",
            prefix=f"waybar-config-monitor-{self.monitor_id}-",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(config_content, f, indent=2)
        except OSError:
            os.close(fd)
            raise

        return Path(path)

    def _start_process(self) -> None:
        """Start the waybar process."""
        import subprocess

        self._config_path = self._create_config()

        env = os.environ.copy()
        env["WAYBAR_MONITOR_ID"] = str(self.monitor_id)

        self._process = subprocess.Popen(
            [self.config.waybar_proc, "-c", str(self._config_path)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        time.sleep(0.3)  # Give waybar time to start
        self._state = WaybarState.VISIBLE

    @property
    def pid(self) -> int:
        """Get the process ID.

        Returns:
            Process ID

        Raises:
            RuntimeError: If process is not running
        """
        if self._process is None:
            raise RuntimeError("Waybar process not started")
        return self._process.pid

    @property
    def state(self) -> WaybarState:
        """Get current visibility state."""
        return self._state

    @state.setter
    def state(self, value: WaybarState) -> None:
        """Set visibility state (doesn't toggle, just updates tracking)."""
        self._state = value

    def is_alive(self) -> bool:
        """Check if the waybar process is still running.

        Returns:
            True if process is alive, False otherwise
        """
        if self._process is None:
            return False
        return self._process.poll() is None

    def toggle(self) -> None:
        """Toggle waybar visibility by sending SIGUSR1.

        Updates internal state tracking.
        """
        if not self.is_alive():
            raise RuntimeError("Cannot toggle: waybar process is not running")

        try:
            os.kill(self.pid, signal.SIGUSR1)
            # Toggle internal state
            self._state = (
                WaybarState.HIDDEN
                if self._state == WaybarState.VISIBLE
                else WaybarState.VISIBLE
            )
        except ProcessLookupError:
            raise RuntimeError("Waybar process died during toggle")

    def show(self) -> None:
        """Show waybar if currently hidden."""
        if self._state == WaybarState.HIDDEN:
            self.toggle()

    def hide(self) -> None:
        """Hide waybar if currently visible."""
        if self._state == WaybarState.VISIBLE:
            self.toggle()

    def kill(self) -> None:
        """Kill the waybar process and cleanup."""
        if self._process is not None and self.is_alive():
            try:
                self._process.terminate()
                try:
                    self._process.wait(timeout=1.0)
                except Exception:
                    self._process.kill()
                    self._process.wait()
            except Exception:
                pass

        self._cleanup()

    def _cleanup(self) -> None:
        """Cleanup temporary files."""
        if self._config_path and self._config_path.exists():
            try:
                self._config_path.unlink()
            except Exception:
                pass
        self._config_path = None
        self._process = None

    def restart(self) -> None:
        """Restart the waybar process."""
        self.kill()
        self._start_process()

    def __del__(self):
        """Cleanup on garbage collection."""
        self._cleanup()

"""Data models for Hyprland objects."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Monitor:
    """Represents a Hyprland monitor with geometry information."""

    id: int
    name: str
    x: int
    y: int
    width: int
    height: int
    serial: Optional[str] = None
    description: Optional[str] = None

    @property
    def left(self) -> int:
        """Left edge X coordinate."""
        return self.x

    @property
    def right(self) -> int:
        """Right edge X coordinate."""
        return self.x + self.width

    @property
    def top(self) -> int:
        """Top edge Y coordinate."""
        return self.y

    @property
    def bottom(self) -> int:
        """Bottom edge Y coordinate."""
        return self.y + self.height

    def contains_point(self, x: int, y: int) -> bool:
        """Check if a point is within this monitor's bounds.

        Uses half-open ranges so a point on the shared edge between two
        adjacent monitors matches exactly one of them.
        """
        return self.left <= x < self.right and self.top <= y < self.bottom

    @classmethod
    def from_dict(cls, data: dict) -> "Monitor":
        """Create a Monitor from a Hyprland JSON response."""
        return cls(
            id=int(data["id"]),
            name=data["name"],
            x=int(data["x"]),
            y=int(data["y"]),
            width=int(data["width"]),
            height=int(data["height"]),
            serial=data.get("serial"),
            description=data.get("description"),
        )


@dataclass(frozen=True)
class Client:
    """Represents a Hyprland window/client."""

    hidden: bool
    workspace_id: int
    monitor_id: int
    fullscreen: bool

    @classmethod
    def from_dict(cls, data: dict) -> "Client":
        """Create a Client from a Hyprland JSON response."""
        return cls(
            hidden=bool(data.get("hidden", False)),
            workspace_id=int(data["workspace"]["id"]),
            monitor_id=int(data["monitor"]),
            fullscreen=bool(data.get("fullscreen", 0)),
        )


@dataclass(frozen=True)
class CursorPosition:
    """Represents cursor position."""

    x: int
    y: int

    @classmethod
    def from_string(cls, s: str) -> "CursorPosition":
        """Parse from Hyprland cursorpos output (e.g., '123, 456').

        Raises:
            ValueError: If the string cannot be parsed as 'x, y'.
        """
        parts = s.strip().split(",")
        if len(parts) != 2:
            raise ValueError(f"Expected 'x, y' cursor position, got: {s.strip()!r}")
        try:
            return cls(x=int(parts[0].strip()), y=int(parts[1].strip()))
        except ValueError:
            raise ValueError(f"Non-integer cursor coordinates in: {s.strip()!r}")

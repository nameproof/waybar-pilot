#!/usr/bin/env python
"""Waybar autohide - entry point script.

This script adds the application directory to Python path before importing modules.
"""

import sys
from pathlib import Path

# Add the application directory to Python path
APP_DIR = Path.home() / ".local" / "share" / "waybar-pilot"
if APP_DIR.exists():
    sys.path.insert(0, str(APP_DIR))

# Now import and run the main module
try:
    from main import main
except ModuleNotFoundError as exc:
    if exc.name != "main":
        raise

    print("Error: Could not load waybar-pilot modules.", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Expected application directory: {APP_DIR}", file=sys.stderr)
    print(f"  Directory exists: {'yes' if APP_DIR.exists() else 'no'}", file=sys.stderr)
    print(file=sys.stderr)
    print("This usually means the install is incomplete or the launcher points", file=sys.stderr)
    print("to the wrong directory.", file=sys.stderr)
    print(file=sys.stderr)
    print("Try reinstalling:", file=sys.stderr)
    print("  make install", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    sys.exit(main())

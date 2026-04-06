#!/usr/bin/env python3
"""
macOS Idle Detection Module

Detects user inactivity by querying the HIDIdleTime from IOKit.
This represents the time since the last keyboard/mouse input.
"""

import subprocess
import re


def get_idle_seconds() -> float:
    """
    Get the system idle time in seconds on macOS.

    Uses `ioreg` to query the HIDIdleTime from IOHIDSystem,
    which reports time in nanoseconds since last user input.

    Returns:
        Idle time in seconds, or 0.0 on error.
    """
    try:
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return 0.0

        for line in result.stdout.split("\n"):
            if "HIDIdleTime" in line:
                # Parse: "HIDIdleTime" = 123456789 (nanoseconds)
                match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', line)
                if match:
                    idle_ns = int(match.group(1))
                    return idle_ns / 1_000_000_000  # Convert to seconds
    except (subprocess.TimeoutExpired, Exception):
        pass

    return 0.0


def get_idle_minutes() -> float:
    """
    Get the system idle time in minutes.

    Returns:
        Idle time in minutes, or 0.0 on error.
    """
    return get_idle_seconds() / 60


def is_user_idle(timeout_minutes: float = 15) -> bool:
    """
    Check if the user has been idle longer than the specified timeout.

    Args:
        timeout_minutes: Idle threshold in minutes (default: 15)

    Returns:
        True if idle time exceeds the threshold, False otherwise.
    """
    return get_idle_minutes() > timeout_minutes


if __name__ == "__main__":
    # Test the module
    secs = get_idle_seconds()
    mins = get_idle_minutes()
    print(f"Idle time: {secs:.1f} seconds ({mins:.2f} minutes)")
    print(f"Is idle (>15 min): {is_user_idle(15)}")
    print(f"Is idle (>1 min): {is_user_idle(1)}")

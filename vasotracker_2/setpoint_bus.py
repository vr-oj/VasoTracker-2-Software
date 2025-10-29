"""
Lightweight pub/sub helper for pressure setpoint commands.

Modules that want to command the pressure hardware should call
`notify_setpoint(mmHg, source)` instead of talking to the device
directly. The active `SessionController` registers a callback so it can
be the single authority that forwards commands to the hardware and
updates internal state.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

_lock = threading.Lock()
_callback: Optional[Callable[[float, str], None]] = None


def register_setpoint_handler(handler: Callable[[float, str], None]) -> None:
    """Register a callable that receives setpoint commands."""
    global _callback
    with _lock:
        _callback = handler


def clear_setpoint_handler(handler: Callable[[float, str], None]) -> None:
    """Remove the handler if it matches the currently registered one."""
    global _callback
    with _lock:
        if _callback is handler:
            _callback = None


def notify_setpoint(value: float, source: str = "external") -> bool:
    """
    Publish a new setpoint request.

    Returns True when a handler consumed the command, False otherwise so
    callers can optionally fall back to legacy behaviour.
    """
    with _lock:
        handler = _callback
    if handler is None:
        return False
    try:
        handler(float(value), source)
        return True
    except Exception:
        return False

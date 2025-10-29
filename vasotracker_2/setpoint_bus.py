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
from typing import Callable, Optional, List

_lock = threading.Lock()
_callback: Optional[Callable[[float, str], None]] = None
_query_callback: Optional[Callable[[], None]] = None
_listeners: List[Callable[[float, str], None]] = []


def _same_callable(lhs, rhs) -> bool:
    if lhs is rhs:
        return True
    if lhs is None or rhs is None:
        return False
    return (
        getattr(lhs, "__func__", None) is getattr(rhs, "__func__", None)
        and getattr(lhs, "__self__", None) is getattr(rhs, "__self__", None)
    )


def register_setpoint_handler(handler: Callable[[float, str], None]) -> None:
    """Register a callable that receives setpoint commands."""
    global _callback
    with _lock:
        _callback = handler


def clear_setpoint_handler(handler: Callable[[float, str], None]) -> None:
    """Remove the handler if it matches the currently registered one."""
    global _callback
    with _lock:
        if _same_callable(_callback, handler):
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


def add_setpoint_listener(listener: Callable[[float, str], None]) -> None:
    """Subscribe to setpoint events emitted by the active controller."""
    with _lock:
        for existing in _listeners:
            if _same_callable(existing, listener):
                return
        _listeners.append(listener)


def remove_setpoint_listener(listener: Callable[[float, str], None]) -> None:
    """Remove a previously registered setpoint listener."""
    with _lock:
        for existing in list(_listeners):
            if _same_callable(existing, listener):
                _listeners.remove(existing)
                break


def broadcast_setpoint(value: float, source: str) -> None:
    """Notify listeners that the authoritative setpoint changed."""
    with _lock:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener(float(value), source)
        except Exception:
            continue


def register_setpoint_query(handler: Callable[[], None]) -> None:
    """Register a callable that can issue a device setpoint query."""
    global _query_callback
    with _lock:
        _query_callback = handler


def clear_setpoint_query(handler: Callable[[], None]) -> None:
    """Remove the registered query handler if it matches the provided callable."""
    global _query_callback
    with _lock:
        if _same_callable(_query_callback, handler):
            _query_callback = None


def request_setpoint_refresh() -> bool:
    """Ask the active controller to re-query the hardware setpoint."""
    with _lock:
        handler = _query_callback
    if handler is None:
        return False
    try:
        handler()
        return True
    except Exception:
        return False

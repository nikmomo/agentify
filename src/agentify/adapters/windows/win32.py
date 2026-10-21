"""Minimal Win32 helpers via ctypes (no pywin32 dependency at this layer)."""

from __future__ import annotations

import ctypes
import os
import sys


def window_process_id(hwnd: int) -> int | None:
    """PID owning `hwnd`, or None if the window does not exist or we are not on Windows."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    if not user32.IsWindow(ctypes.c_void_p(hwnd)):
        return None
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return int(pid.value) or None


def process_image_path(pid: int) -> str | None:
    """Full path of the executable of process `pid`, or None."""
    if sys.platform != "win32" or not pid:
        return None
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        return buf.value or None
    finally:
        kernel32.CloseHandle(h)


def is_interactive_session() -> bool:
    """False for RDP-less service sessions or remote sessions where input injection is unreliable."""
    if sys.platform != "win32":
        return False
    session = os.environ.get("SESSIONNAME", "")
    return not session.upper().startswith("RDP") and session != "Services"


def activate_window(hwnd: int) -> bool:
    """Bring `hwnd` to the foreground so real input (click, typing) reaches it.

    Windows' foreground lock stops a background process from calling SetForegroundWindow, so the
    caller's thread input is briefly attached to the current foreground thread (the documented way
    to take part in the foreground handoff). Returns True if `hwnd` is foreground afterwards.
    """
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    target = ctypes.c_void_p(hwnd)
    if not user32.IsWindow(target):
        return False
    if (user32.GetForegroundWindow() or 0) == hwnd:
        return True
    if user32.IsIconic(target):
        user32.ShowWindow(target, 9)  # SW_RESTORE
    current = kernel32.GetCurrentThreadId()
    foreground = user32.GetForegroundWindow()
    threads = {
        user32.GetWindowThreadProcessId(ctypes.c_void_p(foreground), None) if foreground else 0,
        user32.GetWindowThreadProcessId(target, None),
    } - {0, current}
    attached = [t for t in threads if user32.AttachThreadInput(current, t, True)]
    try:
        user32.BringWindowToTop(target)
        user32.SetForegroundWindow(target)
    finally:
        for t in attached:
            user32.AttachThreadInput(current, t, False)
    return (user32.GetForegroundWindow() or 0) == hwnd

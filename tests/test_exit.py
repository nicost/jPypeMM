"""Tests for the clean-exit behavior (no JVM required)."""
from __future__ import annotations

import subprocess
import sys


def test_quit_now_calls_os_exit(monkeypatch):
    """quit_now must use os._exit (immediate, skips interpreter teardown), not
    sys.exit (which would raise SystemExit and run the blocking JVM shutdown)."""
    import start_mm

    called = {}

    def fake_os_exit(code):
        called["code"] = code
        raise RuntimeError("os._exit stub")  # stop execution like the real one

    monkeypatch.setattr(start_mm.os, "_exit", fake_os_exit)
    try:
        start_mm.quit_now(3)
    except RuntimeError:
        pass
    assert called == {"code": 3}


def test_install_clean_exit_registers_atexit(monkeypatch):
    import start_mm

    registered = []
    monkeypatch.setattr("atexit.register", lambda fn: registered.append(fn))
    start_mm._install_clean_exit()
    assert start_mm.quit_now in registered


def test_atexit_handler_actually_terminates():
    """End-to-end: a script that registers the handler and then exits normally
    must terminate promptly via os._exit rather than hang.

    Note os._exit skips stdout flushing, so the script flushes explicitly before
    returning; the timeout (not the output) is what guards against a hang here.
    """
    code = (
        "import sys, start_mm; start_mm._install_clean_exit(); "
        "print('REACHED_END'); sys.stdout.flush()"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "REACHED_END" in proc.stdout

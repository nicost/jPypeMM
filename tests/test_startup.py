"""Unit tests for the intro-dialog-skip wiring (no JVM required).

These verify that main() only suppresses MM's modal startup dialog when
skip_intro=True is passed — interactive launches (the default) must behave as
before. The MM-touching functions are stubbed out so no JVM/MM is needed.
"""
from __future__ import annotations

import start_mm


class _FakeCore:
    def getVersionInfo(self):
        return "MMCore version FAKE"

    def getAPIVersionInfo(self):
        return "API FAKE"

    def getLoadedDevices(self):
        return ["Core"]


def _patch_launch(monkeypatch):
    """Stub the heavy bits of main() and record the call order."""
    calls = []
    monkeypatch.setattr(start_mm, "find_mm_root", lambda: "FAKE_ROOT")
    monkeypatch.setattr(start_mm, "start_jvm", lambda root: calls.append("start_jvm"))

    def fake_launch(timeout_s=60.0, quiet=True, skip_intro=False):
        calls.append(f"launch(skip_intro={skip_intro})")
        return ("studio", _FakeCore())

    monkeypatch.setattr(start_mm, "launch_imagej_with_mm", fake_launch)
    return calls


def test_main_default_does_not_skip_intro(monkeypatch):
    calls = _patch_launch(monkeypatch)
    studio, core = start_mm.main()
    assert studio == "studio"
    assert "launch(skip_intro=False)" in calls


def test_main_skip_intro_true_propagates(monkeypatch):
    calls = _patch_launch(monkeypatch)
    start_mm.main(skip_intro=True)
    assert "launch(skip_intro=True)" in calls


def test_launch_calls_suppress_only_when_requested(monkeypatch):
    """launch_imagej_with_mm must call suppress_intro_dialog iff skip_intro=True,
    and before the ImageJ/plugin load. We stub the Java-touching pieces."""
    import sys
    import types

    # Single shared list so we can assert suppress runs before ImageJ/runPlugIn.
    order = []
    monkeypatch.setattr(
        start_mm, "suppress_intro_dialog", lambda: order.append("suppress")
    )
    monkeypatch.setattr(start_mm, "_redirect_java_streams_to_null", lambda: None)
    monkeypatch.setattr(start_mm, "_silence_core_stderr", lambda core: None)

    # Fake the `from ij import IJ, ImageJ` and `from org.micromanager.internal
    # import MMStudio` so no real JVM is needed. ImageJ() is a no-op; runPlugIn
    # records ordering; getInstance() returns a stub with a live core().
    class _Studio:
        def core(self):
            return object()

    fake_ij = types.ModuleType("ij")
    fake_ij.ImageJ = lambda: order.append("ImageJ")
    fake_ij.IJ = types.SimpleNamespace(
        runPlugIn=lambda cls, arg: order.append("runPlugIn")
    )

    fake_internal = types.ModuleType("org.micromanager.internal")
    fake_internal.MMStudio = types.SimpleNamespace(getInstance=lambda: _Studio())

    monkeypatch.setitem(sys.modules, "ij", fake_ij)
    monkeypatch.setitem(sys.modules, "org", types.ModuleType("org"))
    monkeypatch.setitem(sys.modules, "org.micromanager", types.ModuleType("org.micromanager"))
    monkeypatch.setitem(sys.modules, "org.micromanager.internal", fake_internal)

    # skip_intro=False -> no suppression, but launch still works.
    start_mm.launch_imagej_with_mm(quiet=False, skip_intro=False)
    assert "suppress" not in order

    # skip_intro=True -> suppress_intro_dialog runs before the plugin load.
    order.clear()
    start_mm.launch_imagej_with_mm(quiet=False, skip_intro=True)
    assert order[0] == "suppress"  # before ImageJ()/runPlugIn
    assert "runPlugIn" in order

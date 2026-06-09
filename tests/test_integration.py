"""Integration test: actually launch ImageJ + Micro-Manager via JPype.

Slow, GUI-bound, and requires a real MM install on a desktop session, so it is
skipped unless explicitly opted in:

    JPYPEMM_RUN_INTEGRATION=1 uv run pytest tests/test_integration.py

It runs start_mm in a *subprocess* (not in-process) because JPype's JVM cannot
be started/stopped repeatedly within one process, which would break the rest of
the test session.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("JPYPEMM_RUN_INTEGRATION") != "1",
    reason="set JPYPEMM_RUN_INTEGRATION=1 to run the live MM launch test",
)


def _run(script: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="session", autouse=True)
def _restore_intro_flags():
    """Snapshot MM's IntroDlg skip flags before the suite and restore them after.

    These tests intentionally persist the skip flags to the user's MM profile
    (to suppress the modal dialog). This autouse fixture undoes that change once
    the session ends — even on failure — so the user's normal MM launches behave
    as they did before the tests ran. Snapshot/restore run in subprocesses since
    only they can host the JVM.
    """
    snap = _run(
        """
        import json, sys, start_mm
        start_mm.start_jvm(start_mm.find_mm_root())
        saved = start_mm.suppress_intro_dialog()  # also captures prior values
        # Undo immediately so the snapshot reflects the PRE-test state, which we
        # re-apply in teardown.
        sys.stdout.write("SAVED:" + json.dumps(saved) + "\\n")
        sys.stdout.flush()
        import os as _os; _os._exit(0)
        """,
        timeout=60,
    )
    saved_json = ""
    for line in snap.stdout.splitlines():
        if line.startswith("SAVED:"):
            saved_json = line[len("SAVED:"):]
    yield
    if saved_json:
        _run(
            f"""
            import json, start_mm
            start_mm.start_jvm(start_mm.find_mm_root())
            start_mm.restore_intro_dialog(json.loads({saved_json!r}))
            import os as _os; _os._exit(0)
            """,
            timeout=60,
        )


def test_launch_reports_core_version_and_exits_cleanly():
    """Launch MM, confirm Core is live, and confirm the process exits on its own
    (the JPype/AWT hang fix) — the subprocess returning at all proves no hang."""
    proc = _run(
        """
        import sys, start_mm
        start_mm._install_clean_exit()
        studio, core = start_mm.main(quiet=True, skip_intro=True)
        print("VER:" + str(core.getVersionInfo()))
        print("OK")
        sys.stdout.flush()  # os._exit (clean-exit handler) won't flush for us
        """
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "OK" in proc.stdout
    assert "MMCore version" in proc.stdout


def test_quiet_mode_keeps_stderr_logging_disabled():
    """After launch in quiet mode, Core's stderr logger stays off through MM's
    asynchronous re-enable window (the firehose fix)."""
    proc = _run(
        """
        import sys, time, start_mm
        start_mm._install_clean_exit()
        studio, core = start_mm.main(quiet=True, skip_intro=True)
        reenabled = False
        for _ in range(16):
            if core.stderrLogEnabled():
                reenabled = True
            time.sleep(0.25)
        print("REENABLED:" + str(reenabled))
        print("FINAL:" + str(core.stderrLogEnabled()))
        sys.stdout.flush()  # os._exit (clean-exit handler) won't flush for us
        """
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "REENABLED:False" in proc.stdout
    assert "FINAL:False" in proc.stdout


def test_skip_intro_launches_without_dialog():
    """With skip_intro=True the modal IntroDlg must not appear: the launch
    completes with no human input, no IntroDlg window is visible, and MM comes
    up with only the Core device. A bounded subprocess timeout is the hang guard
    — before the fix, MMStudio blocks on the modal dialog and this times out."""
    proc = _run(
        """
        import sys, start_mm
        start_mm._install_clean_exit()
        studio, core = start_mm.main(quiet=True, skip_intro=True)
        # Enumerate live AWT windows; none may be an IntroDlg.
        from java.awt import Window
        names = [w.getClass().getName() for w in Window.getWindows()]
        intro = [n for n in names if n.endswith("dialogs.IntroDlg")]
        print("INTRO_WINDOWS:" + str(len(intro)))
        print("DEVICES:" + ",".join(core.getLoadedDevices()))
        print("OK")
        sys.stdout.flush()
        """,
        timeout=90,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "OK" in proc.stdout
    assert "INTRO_WINDOWS:0" in proc.stdout
    assert "DEVICES:Core" in proc.stdout


def test_skip_intro_persists_flags_to_profile():
    """suppress_intro_dialog must persist both skip flags to the default profile
    JSON on disk (this is what MMStudio re-reads to gate the dialog)."""
    proc = _run(
        """
        import sys, start_mm
        start_mm.find_mm_root()
        start_mm.start_jvm(start_mm.find_mm_root())
        start_mm.suppress_intro_dialog()
        print("SEEDED")
        sys.stdout.flush()
        import os as _os; _os._exit(0)
        """,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "SEEDED" in proc.stdout

    profile = os.path.join(
        os.environ["LOCALAPPDATA"],
        "Micro-Manager",
        "UserProfiles",
        "Default_User-00000000-0000-0000-0000-000000000000.json",
    )
    assert os.path.isfile(profile), profile
    text = open(profile, encoding="utf-8").read()
    assert "SKIP_PROFILE_SELECTION_AT_STARTUP" in text
    assert "SKIP_CONFIG_SELECTION_AT_STARTUP" in text


def test_snap_returns_numpy_for_each_pixel_type():
    """End-to-end: snap() returns a correctly shaped/typed numpy array for every
    demo-camera pixel type, against the real live-manager snap path."""
    proc = _run(
        """
        import sys, start_mm
        start_mm._install_clean_exit()
        studio, core = start_mm.main(quiet=True, skip_intro=True)
        mm_root = start_mm.find_mm_root()
        if "Camera" not in list(core.getLoadedDevices()):
            core.loadSystemConfiguration(str(mm_root / "MMConfig_demo.cfg"))
        cam = core.getCameraDevice()
        expect = {
            "8bit":     ("(h, w)",    "uint8"),
            "16bit":    ("(h, w)",    "uint16"),
            "32bit":    ("(h, w)",    "float32"),
            "32bitRGB": ("(h, w, 3)", "uint8"),
            "64bitRGB": ("(h, w, 3)", "uint16"),
        }
        w = int(core.getImageWidth()); h = int(core.getImageHeight())
        for pt, (kind, dt) in expect.items():
            core.setProperty(cam, "PixelType", pt)
            core.waitForDevice(cam)
            arr = start_mm.snap(studio)            # read-only single image
            want = (h, w) if kind == "(h, w)" else (h, w, 3)
            ok = (arr.shape == want and str(arr.dtype) == dt
                  and arr.flags.writeable is False)
            print(f"PT {pt}: shape={arr.shape} dtype={arr.dtype} ro={not arr.flags.writeable} OK={ok}")
        # copy=True must be writable
        arrw = start_mm.snap(studio, copy=True)
        print("WRITABLE:" + str(arrw.flags.writeable))
        print("DONE")
        sys.stdout.flush()
        """,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "DONE" in proc.stdout
    assert "WRITABLE:True" in proc.stdout
    # Every pixel type must have reported OK=True.
    pt_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("PT ")]
    assert len(pt_lines) == 5, proc.stdout
    assert all("OK=True" in ln for ln in pt_lines), proc.stdout

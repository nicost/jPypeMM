"""Integration test: actually launch ImageJ + Micro-Manager via JPype.

Slow, GUI-bound, and requires a real MM install on a desktop session, so it is
skipped unless explicitly opted in:

    JPYPEMM_RUN_INTEGRATION=1 uv run pytest tests/test_integration.py

It runs start_mm in a *subprocess* (not in-process) because JPype's JVM cannot
be started/stopped repeatedly within one process, which would break the rest of
the test session.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import start_mm

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
def _restore_intro_flags(tmp_path_factory):
    """Snapshot the MM default profile before the suite and restore it after.

    The tests set the IntroDlg skip flags in the user's default profile so the
    modal startup dialog doesn't block automated launches. That is a real on-disk
    mutation, so it MUST be reverted no matter how the suite ends — pass, assertion
    failure, or crash. suppress/restore_intro_dialog are pure-Python JSON edits
    (no JVM, no profile write lock — see start_mm for why), so this fixture runs
    them in-process, with two safeguards:

      * The snapshot (the verbatim prior profile text) is written to a file the
        instant it is captured, so teardown can recover it even if the in-memory
        value is lost.
      * Restore runs in a finally block and is *verified* (we re-read the file and
        assert it matches the snapshot), raising loudly rather than silently
        leaving the user's profile mutated.
    """
    saved = start_mm.suppress_intro_dialog()  # captures prior text, sets skip flags
    snapshot_file = tmp_path_factory.mktemp("mm_profile") / "profile_snapshot.json"
    snapshot_file.write_text(json.dumps(saved), encoding="utf-8")

    try:
        yield
    finally:
        saved = json.loads(snapshot_file.read_text(encoding="utf-8"))
        start_mm.restore_intro_dialog(saved)
        # Restoring the user's profile is not optional — verify and surface failure.
        if saved.get("existed", True):
            current = Path(saved["path"]).read_text(encoding="utf-8")
            assert current == saved["text"], (
                "FAILED to restore the MM default profile — it may be left with the "
                f"IntroDlg skip flags enabled. Profile: {saved['path']}"
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
    completes with no human input and no IntroDlg window is visible. A bounded
    subprocess timeout is the hang guard — before the fix, MMStudio blocked on the
    modal dialog and this timed out.

    We assert only that the Core is up and no IntroDlg is showing; the exact device
    list depends on whatever hardware config the user's profile last remembered, so
    it isn't checked here."""
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
    assert "DEVICES:" in proc.stdout and "Core" in proc.stdout


def test_skip_intro_persists_flags_to_profile():
    """suppress_intro_dialog must set both skip flags to true in the default
    profile JSON on disk (this is what MMStudio re-reads to gate the dialog).

    Pure-Python now, so no JVM/subprocess is needed. The session fixture has
    already set the flags, so this asserts the resulting on-disk state directly.
    """
    profile = start_mm.default_profile_path()
    assert profile.is_file(), profile
    text = profile.read_text(encoding="utf-8")
    for flag in ("SKIP_PROFILE_SELECTION_AT_STARTUP", "SKIP_CONFIG_SELECTION_AT_STARTUP"):
        m = re.search(
            r'"' + flag + r'"\s*:\s*\{.*?"scalar"\s*:\s*(true|false)', text, re.DOTALL
        )
        assert m is not None and m.group(1) == "true", f"{flag} not set true"


def test_snap_returns_numpy_for_each_pixel_type():
    """snap_core() returns a correctly shaped/typed numpy array for every
    demo-camera pixel type, and snap(studio.live()) works for a single snap.

    Pixel-type cycling is exercised via snap_core (the CMMCore path), which is
    reliable when the camera is reconfigured repeatedly. The live-manager path
    (snap(studio.live())) is checked once: in a non-interactive test subprocess
    the live manager's snap() can return null right after a PixelType change
    (its display pipeline isn't driven by a real EDT), so we don't cycle types
    through it — that headless quirk does not affect interactive use.
    """
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
            arr = start_mm.snap_core(core)         # read-only single image (Core path)
            want = (h, w) if kind == "(h, w)" else (h, w, 3)
            ok = (arr.shape == want and str(arr.dtype) == dt
                  and arr.flags.writeable is False)
            print(f"PT {pt}: shape={arr.shape} dtype={arr.dtype} ro={not arr.flags.writeable} OK={ok}")
        # copy=True must be writable
        arrw = start_mm.snap_core(core, copy=True)
        print("WRITABLE:" + str(arrw.flags.writeable))
        # The live-manager path works for a single snap (back on a plain pixel type).
        core.setProperty(cam, "PixelType", "8bit"); core.waitForDevice(cam)
        live_arr = start_mm.snap(studio.live())
        print("LIVE_SNAP_SHAPE:" + str(live_arr.shape))
        print("DONE")
        sys.stdout.flush()
        """,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "DONE" in proc.stdout
    assert "WRITABLE:True" in proc.stdout
    assert "LIVE_SNAP_SHAPE:" in proc.stdout  # snap(studio.live()) returned an array
    # Every pixel type must have reported OK=True.
    pt_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("PT ")]
    assert len(pt_lines) == 5, proc.stdout
    assert all("OK=True" in ln for ln in pt_lines), proc.stdout


def test_numpy_to_image_roundtrips_each_pixel_type():
    """End-to-end through the real DataManager.createImage path:

      * Supported types (8bit, 16bit, 32bit-float gray, 32bitRGB) must round-trip
        exactly: snap -> numpy_to_image -> image_to_numpy recovers the original.
      * Unsupported 64bitRGB (uint16 RGB) must be rejected by numpy_to_image with a
        TypeError — MM has no 16-bit-per-component RGB PixelType.

    (32bit float requires a patched MM whose DefaultDataManager.createImage clones
    float[]; on stock MM that case would raise instead of round-tripping.)
    """
    proc = _run(
        """
        import sys, numpy as np, start_mm
        start_mm._install_clean_exit()
        studio, core = start_mm.main(quiet=True, skip_intro=True)
        mm_root = start_mm.find_mm_root()
        if "Camera" not in list(core.getLoadedDevices()):
            core.loadSystemConfiguration(str(mm_root / "MMConfig_demo.cfg"))
        cam = core.getCameraDevice()
        data = studio.data()
        SUPPORTED = {"8bit", "16bit", "32bit", "32bitRGB"}
        for pt in ("8bit", "16bit", "32bit", "32bitRGB", "64bitRGB"):
            core.setProperty(cam, "PixelType", pt)
            core.waitForDevice(cam)
            arr = start_mm.snap_core(core, copy=True)   # writable original
            if pt in SUPPORTED:
                img = start_mm.numpy_to_image(data, arr)
                back = start_mm.image_to_numpy(img, copy=True)
                ok = (back.shape == arr.shape and back.dtype == arr.dtype
                      and np.array_equal(back, arr))
                print(f"PT {pt}: roundtrip OK={ok}")
            else:
                try:
                    start_mm.numpy_to_image(data, arr)
                    print(f"PT {pt}: rejected OK=False (no error raised)")
                except TypeError:
                    print(f"PT {pt}: rejected OK=True")
        print("DONE")
        sys.stdout.flush()
        """,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "DONE" in proc.stdout
    pt_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("PT ")]
    assert len(pt_lines) == 5, proc.stdout
    assert all("OK=True" in ln for ln in pt_lines), proc.stdout

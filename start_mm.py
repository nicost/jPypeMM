"""Start ImageJ with Micro-Manager as a plugin, in-process via JPype.

Launches the bundled-JRE JVM inside this Python process, brings up ImageJ
(ij.ImageJ), then runs the Micro-Manager ImageJ plugin exactly as the
"Plugins > Micro-Manager Studio" menu item does. Exposes ``studio`` and ``core``.

Run interactively so the GUI stays open and the references stay live::

    uv run python -i start_mm.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import jpype
import jpype.imports  # enables `from org... import ...` after startJVM  # noqa: F401

# Same JVM flags Micro-Manager's own launcher uses (see ImageJ.cfg).
MM_FLAGS = ("-Xmx24000m", "-XX:MaxDirectMemorySize=1000g", "-XX:+UseG1GC")

# os.add_dll_directory() returns a cookie that removes the directory from the
# search set when garbage-collected; keep them alive for the process lifetime.
_DLL_DIR_HANDLES = []


def find_mm_root() -> Path:
    """Locate a Micro-Manager 2.0 install. MM_DIR env var overrides discovery."""
    candidates = []
    if os.environ.get("MM_DIR"):
        candidates.append(Path(os.environ["MM_DIR"]))
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    # Rank installs: prefer modern numbered builds (2.0, 2.0.3 — bundled JRE 11)
    # over old dated "Gamma" builds (e.g. 2.0Gamma-20201208, which ships Java 8),
    # then by most-recently-installed. Name-sorting alone is wrong: "Gamma"
    # sorts high but is the oldest build.
    def rank(p: Path) -> tuple:
        is_gamma = "gamma" in p.name.lower()
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (is_gamma, -mtime)  # non-gamma first, then newest install first

    candidates.extend(sorted(pf.glob("Micro-Manager-2.0*"), key=rank))
    for c in candidates:
        if c and (c / "MMCoreJ_wrap.dll").is_file() and (c / "ij.jar").is_file():
            return c.resolve()
    raise FileNotFoundError(
        f"No MM 2.0 install found. Tried: {[str(c) for c in candidates]}"
    )


def find_bundled_jvm(mm_root: Path) -> Path:
    """Return MM's bundled JRE jvm.dll (avoids any system JDK on the machine)."""
    jvm = mm_root / "jre" / "bin" / "server" / "jvm.dll"
    if not jvm.is_file():
        raise FileNotFoundError(f"Bundled jvm.dll not found at {jvm}")
    return jvm


def build_classpath(mm_root: Path) -> list[str]:
    """Glob MM's jars: ij.jar first, then the plugin dirs. Excludes stack-backup."""
    entries: list[str] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        if "stack-backup" in p.parts:
            return
        key = p.name.lower()
        if key in seen:  # de-dup by filename; first occurrence wins
            return
        seen.add(key)
        entries.append(str(p))

    ij = mm_root / "ij.jar"
    if ij.is_file():
        add(ij)
    for sub in ("plugins/Micro-Manager", "mmplugins", "mmautofocus"):  # non-recursive
        for jar in sorted((mm_root / sub).glob("*.jar")):
            add(jar)

    have = {Path(e).name for e in entries}
    for req in ("ij.jar", "MMJ_.jar", "MMCoreJ.jar"):
        if req not in have:
            raise RuntimeError(f"Required jar {req} missing from classpath")
    return entries


def setup_dll_dirs(mm_root: Path) -> None:
    """Make the device adapter + JVM dependent DLLs resolvable on Windows.

    Python 3.8+ no longer honors PATH for transitive native deps, so the
    install root (MMCoreJ_wrap.dll + ~341 device DLLs) and the JRE dirs must be
    registered via os.add_dll_directory BEFORE the JVM starts.
    """
    dll_dirs = [mm_root, mm_root / "jre" / "bin", mm_root / "jre" / "bin" / "server"]
    for d in dll_dirs:
        if d.is_dir():
            _DLL_DIR_HANDLES.append(os.add_dll_directory(str(d)))
    os.environ["PATH"] = os.pathsep.join(
        [str(d) for d in dll_dirs if d.is_dir()] + [os.environ.get("PATH", "")]
    )


def start_jvm(mm_root: Path) -> None:
    """Start the in-process JVM using MM's bundled JRE and full classpath."""
    if jpype.isJVMStarted():
        return
    setup_dll_dirs(mm_root)  # MUST precede startJVM
    jpype.startJVM(
        str(find_bundled_jvm(mm_root)),
        *MM_FLAGS,
        f"-Djava.library.path={mm_root}",
        classpath=build_classpath(mm_root),
        convertStrings=True,
    )


def _redirect_java_streams_to_null() -> None:
    """Point Java's System.out/System.err at the OS null device.

    Catches the chatter that ImageJ / SciJava print directly to the Java
    streams (plugin-search lines, reflective-access warnings, the SciJava stack
    trace). JPype 1.7 won't let us subclass java.io.OutputStream in Python, so
    route through a real file sink: "nul" on Windows, "/dev/null" elsewhere.
    Python's own stdout/stderr are untouched, so prints and the REPL still work.
    """
    from java.io import FileOutputStream, PrintStream

    null_path = "nul" if os.name == "nt" else "/dev/null"
    devnull = PrintStream(FileOutputStream(null_path))
    jpype.java.lang.System.setOut(devnull)
    jpype.java.lang.System.setErr(devnull)


def suppress_intro_dialog() -> None:
    """Seed the MM user profiles so MMStudio skips its modal startup dialog.

    On launch MMStudio normally shows IntroDlg (the "splash screen" where you
    pick a user profile + hardware config and click OK), which blocks until a
    human clicks OK — fatal for automated/headless launches. MMStudio's init
    gate is:

        StartupSettings.create(admin.getNonSavingProfile(uuid))
                       .shouldSkipUserInteractionWithSplashScreen()

    which is true only when BOTH the profile-selection and config-selection skip
    flags are set. The gate re-reads the profile JSON from disk, so we set both
    flags and force a synchronous flush (DefaultUserProfile.close()) before the
    plugin loads. Seeding both the default and current profile UUIDs avoids any
    ambiguity over which one MMStudio resolves.

    NOTE: this is a PERSISTED, per-machine setting — it also affects normal MM
    launches until re-enabled in MM's dialog. It is opt-in (tests only); call it
    AFTER start_jvm() and BEFORE launch_imagej_with_mm().

    Returns the prior flag values, keyed by profile-UUID string, so the change
    can be undone with restore_intro_dialog(saved).
    """
    from org.micromanager.internal import StartupSettings
    from org.micromanager.profile.internal import UserProfileAdmin

    admin = UserProfileAdmin.create()
    uuids = {admin.getUUIDOfDefaultProfile(), admin.getUUIDOfCurrentProfile()}

    saved: dict[str, tuple[bool, bool]] = {}
    for uuid in uuids:
        prior = StartupSettings.create(admin.getNonSavingProfile(uuid))
        saved[str(uuid)] = (
            bool(prior.shouldSkipProfileSelectionAtStartup()),
            bool(prior.shouldSkipConfigSelectionAtStartup()),
        )
    _write_intro_skip_flags(admin, uuids, profile_skip=True, config_skip=True)

    # Verify the gate exactly as MMStudio will read it (fresh non-saving read).
    for uuid in uuids:
        p = admin.getNonSavingProfile(uuid)
        if not StartupSettings.create(p).shouldSkipUserInteractionWithSplashScreen():
            raise RuntimeError(f"IntroDlg skip was not persisted for profile {uuid}")
    return saved


def restore_intro_dialog(saved: "dict[str, tuple[bool, bool]]") -> None:
    """Undo suppress_intro_dialog(), restoring each profile's prior skip flags.

    Pass the dict returned by suppress_intro_dialog(). This re-enables MM's
    startup dialog for normal launches if it was showing before.
    """
    import java.util.UUID as UUID

    from org.micromanager.internal import StartupSettings
    from org.micromanager.profile.internal import UserProfileAdmin

    admin = UserProfileAdmin.create()
    for uuid_str, (profile_skip, config_skip) in saved.items():
        _write_intro_skip_flags(
            admin, [UUID.fromString(uuid_str)], profile_skip, config_skip
        )
    # Confirm each profile now reads back the requested values.
    for uuid_str, (profile_skip, config_skip) in saved.items():
        ss = StartupSettings.create(admin.getNonSavingProfile(UUID.fromString(uuid_str)))
        if (
            bool(ss.shouldSkipProfileSelectionAtStartup()) != profile_skip
            or bool(ss.shouldSkipConfigSelectionAtStartup()) != config_skip
        ):
            raise RuntimeError(f"IntroDlg flags not restored for profile {uuid_str}")


def _write_intro_skip_flags(admin, uuids, profile_skip: bool, config_skip: bool) -> None:
    """Set both IntroDlg skip flags on each profile and flush synchronously."""
    from org.micromanager.internal import StartupSettings

    listener = jpype.JProxy(
        "java.beans.ExceptionListener", dict={"exceptionThrown": lambda e: None}
    )
    for uuid in uuids:
        profile = admin.getAutosavingProfile(uuid, listener)
        settings = StartupSettings.create(profile)
        settings.setSkipProfileSelectionAtStartup(profile_skip)
        settings.setSkipConfigSelectionAtStartup(config_skip)
        try:
            profile.close()  # synchronous flush to disk — required, not the async saver
        except jpype.JException:
            pass  # close() declares InterruptedException; the write still happened


def launch_imagej_with_mm(
    timeout_s: float = 60.0, quiet: bool = True, skip_intro: bool = False
):
    """Start ImageJ, then run the MM plugin exactly as the ImageJ menu does.

    With quiet=True, MM/ImageJ console output is suppressed (it is still written
    to MM's CoreLogs/ file). Redirecting the Java streams *before* the plugin
    load also hides the verbose startup dump, not just the steady-state logs.

    With skip_intro=True, MM's modal startup dialog is suppressed so the launch
    needs no human input (opt-in; see suppress_intro_dialog for the persisted
    side effect).
    """
    from ij import IJ, ImageJ
    from org.micromanager.internal import MMStudio

    if skip_intro:
        suppress_intro_dialog()  # must precede the plugin load below
    ImageJ()  # opens the ImageJ main window (the host application)
    if quiet:
        _redirect_java_streams_to_null()
    # Same entry as plugins.config: Plugins > "Micro-Manager Studio" -> MMStudioPlugin
    IJ.runPlugIn("MMStudioPlugin", "")

    # MMStudio initializes asynchronously on the Swing EDT after runPlugIn
    # returns, so getInstance() — and its core() — are briefly null. Poll until
    # both are live before handing back references.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        studio = MMStudio.getInstance()
        if studio is not None and studio.core() is not None:
            core = studio.core()
            if quiet:
                _silence_core_stderr(core)
            return studio, core
        time.sleep(0.2)
    raise TimeoutError("MMStudio did not initialize within timeout")


def _silence_core_stderr(core, settle_s: float = 3.0) -> None:
    """Disable MM Core's stderr logger and keep it disabled.

    MM re-enables stderr logging asynchronously during its startup (observed
    flipping back on ~0.75s after getInstance() returns), so a single disable
    loses the race. Poll for ``settle_s`` seconds and re-disable whenever MM
    turns it back on, until it stays off. The log file in CoreLogs/ is
    unaffected — only the console (native fd 2) copy is silenced.
    """
    deadline = time.time() + settle_s
    core.enableStderrLog(False)
    while time.time() < deadline:
        if core.stderrLogEnabled():
            core.enableStderrLog(False)
        time.sleep(0.1)


def quit_now(code: int = 0):
    """Terminate the process immediately, bypassing JPype's blocking shutdown.

    The JVM started by JPype keeps non-daemon AWT/Swing (EDT) threads alive, so
    a normal interpreter exit — closing the MM window, typing exit()/Ctrl-D, or
    falling off the end of the script — calls jpype.shutdownJVM(), which blocks
    forever waiting for those threads to die. os._exit() skips interpreter
    teardown (and that blocking JVM shutdown) and ends the process at once.
    """
    os._exit(code)


def _install_clean_exit() -> None:
    """Make any normal exit terminate immediately instead of hanging.

    Registered as an atexit handler so that in `python -i` typing exit() or
    Ctrl-D actually quits, and a non-interactive run ends after its work.
    """
    import atexit

    atexit.register(quit_now)


def main(quiet: bool = True, skip_intro: bool = False):
    mm_root = find_mm_root()
    print(f"MM root: {mm_root}")
    start_jvm(mm_root)
    studio, core = launch_imagej_with_mm(quiet=quiet, skip_intro=skip_intro)
    print("ImageJ + Micro-Manager started.")
    print("  MM version :", core.getVersionInfo())
    print("  API version:", core.getAPIVersionInfo())
    print("  Devices    :", list(core.getLoadedDevices()))
    return studio, core


# When run with `python -i`, `studio` and `core` are left in the namespace and
# the GUI stays open at the interactive prompt; type exit() / Ctrl-D or call
# quit_now() to terminate (the atexit handler ensures it doesn't hang). When run
# non-interactively, hold the process open until Ctrl+C, then exit cleanly.
if __name__ == "__main__":
    _install_clean_exit()
    studio, core = main()
    if not sys.flags.interactive:
        print("\nGUI is open. Press Ctrl+C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Shutting down.")
        quit_now()

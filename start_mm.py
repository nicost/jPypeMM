"""Start ImageJ with Micro-Manager as a plugin, in-process via JPype.

Launches the bundled-JRE JVM inside this Python process, brings up ImageJ
(ij.ImageJ), then runs the Micro-Manager ImageJ plugin exactly as the
"Plugins > Micro-Manager Studio" menu item does. Exposes ``studio`` and ``core``.

Run interactively so the GUI stays open and the references stay live::

    uv run python -i start_mm.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

import jpype
import jpype.imports  # enables `from org... import ...` after startJVM  # noqa: F401

# ndv (n-dimensional array viewer, Qt backend) for displaying snapped images.
# Imported lazily-but-eagerly here so it's in the namespace at the `-i` prompt;
# guarded so a missing/broken Qt backend never blocks launching Micro-Manager.
try:
    import ndv
except Exception:  # pragma: no cover - only when the Qt/graphics backend is unavailable
    ndv = None

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


# MM's IntroDlg skip flags live in the default profile's JSON on disk, under
#   "map" -> "org.micromanager.internal.StartupSettings" -> "scalar" ->
#       SKIP_CONFIG_SELECTION_AT_STARTUP / SKIP_PROFILE_SELECTION_AT_STARTUP.
# We set them by editing that JSON directly in Python — NOT via the Java
# UserProfileAdmin API. UserProfileAdmin.create() takes MM's UserProfileWriteLock
# and never releases it (only on process death); doing that in our JVM makes
# MMStudio's own startup admin collide with the held lock and pop a blocking
# "Failed to acquire User Profile write lock" modal. MMStudio re-reads the JSON
# from disk at startup, so a plain text write is all that's needed and no lock is
# ever taken by us.
_STARTUP_SETTINGS_KEY = "org.micromanager.internal.StartupSettings"
_DEFAULT_PROFILE_UUID = "00000000-0000-0000-0000-000000000000"


def mm_profiles_dir() -> Path:
    """Directory holding MM's per-user profile JSON files."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "Micro-Manager" / "UserProfiles"


def default_profile_path() -> Path:
    """Path to MM's default user-profile JSON (the all-zeros-UUID profile).

    MM's default profile always carries the all-zeros UUID; its file is named
    ``Default_User-<uuid>.json``. Resolved from Index.json when possible, else the
    conventional filename.
    """
    profiles = mm_profiles_dir()
    index = profiles / "Index.json"
    if index.exists():
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
            for entry in data["map"]["Profiles"]["array"]:
                if entry.get("UUID", {}).get("scalar") == _DEFAULT_PROFILE_UUID:
                    return profiles / entry["File"]["scalar"]
        except (ValueError, KeyError):
            pass  # fall through to the conventional name
    return profiles / f"Default_User-{_DEFAULT_PROFILE_UUID}.json"


def suppress_intro_dialog():
    """Edit the default profile JSON so MMStudio skips its modal startup dialog.

    On launch MMStudio normally shows IntroDlg (pick a profile + hardware config,
    click OK), which blocks until a human clicks — fatal for automated launches.
    The gate is satisfied when BOTH skip flags are true in the default profile's
    JSON, which MMStudio re-reads from disk at startup. We set them with a targeted
    text edit (see module note above on why not via UserProfileAdmin).

    Pure Python, no JVM required. Returns an opaque ``saved`` token (the prior
    on-disk text) to pass to restore_intro_dialog() for an exact revert.

    NOTE: this is a PERSISTED, per-machine setting — it also affects normal MM
    launches until reverted. Opt-in (tests only).
    """
    path = default_profile_path()
    original = path.read_text(encoding="utf-8")
    saved = {"path": str(path), "text": original, "existed": path.exists()}

    updated = _set_skip_flags_in_text(original, skip=True)
    if updated != original:
        _atomic_write(path, updated)
    return saved


def restore_intro_dialog(saved) -> None:
    """Undo suppress_intro_dialog(), restoring the profile's exact prior content.

    Pass the token returned by suppress_intro_dialog(). Writes the captured prior
    text back verbatim, re-enabling MM's startup dialog if it was showing before.
    """
    path = Path(saved["path"])
    if not saved.get("existed", True):
        # The profile did not exist before we touched it — remove what we created.
        if path.exists():
            path.unlink()
        return
    if path.read_text(encoding="utf-8") != saved["text"]:
        _atomic_write(path, saved["text"])


def _set_skip_flags_in_text(text: str, skip: bool) -> str:
    """Return ``text`` with both IntroDlg skip flags set to ``skip``.

    The two SKIP_*_AT_STARTUP keys are unique in the file, so each flag's boolean
    "scalar" is rewritten in place by name — no need to match (and risk mangling)
    the enclosing StartupSettings block. If neither flag is present, a complete
    block is inserted into the top-level "map" object. Everything else in the file
    is left byte-for-byte unchanged, and re-applying the same value is a no-op.
    """
    value = "true" if skip else "false"
    total = 0
    for flag in ("SKIP_CONFIG_SELECTION_AT_STARTUP", "SKIP_PROFILE_SELECTION_AT_STARTUP"):
        # Match: "FLAG": { "type": "BOOLEAN", "scalar": <bool> } — value only.
        text, n = re.subn(
            r'("' + re.escape(flag) + r'"\s*:\s*\{[^{}]*?"scalar"\s*:\s*)(?:true|false)',
            lambda m: m.group(1) + value,
            text,
        )
        total += n
    if total == 0:
        # Neither flag present (fresh profile) — add the whole StartupSettings block.
        return _insert_startup_block(text, skip)
    return text


def _insert_startup_block(text: str, skip: bool) -> str:
    """Insert a fresh StartupSettings block as the first entry of "map": { ... }."""
    m = re.search(r'"map"\s*:\s*\{', text)
    if m is None:
        raise RuntimeError("profile JSON has no top-level 'map' object to edit")
    insert_at = m.end()
    # Indent one level past "map" (its entries sit at a deeper indent).
    block = _startup_block("        ", skip)
    return text[:insert_at] + "\n" + block + "," + text[insert_at:]


def _startup_block(indent: str, skip: bool) -> str:
    """Render a complete StartupSettings property-map block at ``indent``."""
    value = "true" if skip else "false"
    inner = indent + "  "
    return (
        f'{indent}"{_STARTUP_SETTINGS_KEY}": {{\n'
        f'{inner}"type": "PROPERTY_MAP",\n'
        f'{inner}"scalar": {{\n'
        f'{inner}  "SKIP_CONFIG_SELECTION_AT_STARTUP": {{\n'
        f'{inner}    "type": "BOOLEAN",\n'
        f'{inner}    "scalar": {value}\n'
        f'{inner}  }},\n'
        f'{inner}  "SKIP_PROFILE_SELECTION_AT_STARTUP": {{\n'
        f'{inner}    "type": "BOOLEAN",\n'
        f'{inner}    "scalar": {value}\n'
        f'{inner}  }}\n'
        f'{inner}}}\n'
        f'{indent}}}'
    )


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically (temp file + os.replace), flushed to disk."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def launch_imagej_with_mm(
    timeout_s: float = 60.0,
    quiet: bool = True,
    skip_intro: bool = False,
    wait_for_live: bool = True,
):
    """Start ImageJ, then run the MM plugin exactly as the ImageJ menu does.

    With quiet=True, MM/ImageJ console output is suppressed (it is still written
    to MM's CoreLogs/ file). Redirecting the Java streams *before* the plugin
    load also hides the verbose startup dump, not just the steady-state logs.

    With skip_intro=True, MM's modal startup dialog is suppressed so the launch
    needs no human input (opt-in; see suppress_intro_dialog for the persisted
    side effect).

    With wait_for_live=True (default), waits until studio.live() (the live/snap
    manager) is available before returning, so snap(studio) can use the live
    path and the MM display updates on snap. Set False to return as soon as
    core() is ready (faster; only CMMCore access needed).
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
    # returns, so getInstance(), its core(), and its live() manager are briefly
    # null and are published at different construction stages. Poll until all the
    # references we need are live before handing back, so callers can use
    # studio.live().snap(...) directly. (Set wait_for_live=False to return as
    # soon as core() is up, e.g. when only CMMCore access is needed.)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        studio = MMStudio.getInstance()
        if (
            studio is not None
            and studio.core() is not None
            and (not wait_for_live or studio.live() is not None)
        ):
            core = studio.core()
            if quiet:
                _silence_core_stderr(core)
            global _studio_ref
            _studio_ref = studio  # for clean profile-lock release at exit
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


# --- Image -> numpy conversion ------------------------------------------------
#
# Zero-copy is NOT possible here: MM hands pixels back as an on-heap Java
# primitive array (byte[]/short[]/float[]) from Image.getRawPixels() /
# CMMCore.getImage() — never a java.nio direct buffer. The JVM GC may relocate
# heap arrays, so no stable pointer can be shared. JPype's memoryview() of such
# an array is therefore a *read-only copy* out of the JVM. We do exactly one
# copy (JVM heap -> numpy) and add no further Python-side copies.
#
# Demo-camera pixel-type matrix (PixelType -> raw / shape / dtype), keyed off the
# Image's own metadata rather than the PixelType string so it generalizes:
#   8bit     byte[]   1 comp  -> (h, w)     uint8
#   16bit    short[]  1 comp  -> (h, w)     uint16   (Java short is signed; pixels are unsigned)
#   32bit    float[]  1 comp  -> (h, w)     float32
#   32bitRGB byte[]   4 comp  -> (h, w, 3)  uint8    (packed BGRA; drop alpha, reorder to RGB)
#   64bitRGB short[]  4 comp  -> (h, w, 3)  uint16

# memoryview format char (from JPype) -> the matching unsigned/native numpy dtype.
# Java has no unsigned types, so 'b'/'h'/'i' arrive signed but hold unsigned pixel
# data; reinterpret as the unsigned dtype of the same width.
_FORMAT_TO_DTYPE = {
    "b": np.uint8,   # signed byte holding unsigned 8-bit pixels
    "B": np.uint8,
    "h": np.uint16,  # signed short holding unsigned 16-bit pixels
    "H": np.uint16,
    "f": np.float32,
}


def image_to_numpy(image, copy: bool = False) -> "np.ndarray":
    """Convert an org.micromanager.data.Image to a correctly shaped numpy array.

    Grayscale images become (height, width); RGB images become (height, width, 3)
    in R,G,B order (MM stores them packed as BGRA — the alpha channel is dropped).
    dtype follows the pixel type: uint8 (8-bit), uint16 (16-bit), float32 (32-bit
    float).

    Exactly one copy is made (JVM heap -> numpy), which is unavoidable (see the
    module note above). By default the returned array is **read-only** (a view of
    that single copied buffer). Pass copy=True for a writable array, at the cost
    of a second copy.
    """
    return _raw_to_numpy(
        image.getRawPixels(),  # Java byte[]/short[]/float[]
        int(image.getWidth()),
        int(image.getHeight()),
        int(image.getNumComponents()),
        copy,
    )


def _raw_to_numpy(raw, width: int, height: int, n_components: int, copy: bool):
    """Shared core: wrap a Java primitive pixel array as a shaped numpy array.

    See image_to_numpy for the copy semantics; this is the buffer-level worker
    used by both the Image and the CMMCore paths.
    """
    mv = memoryview(raw)  # JPype: single read-only copy out of the JVM
    dtype = _FORMAT_TO_DTYPE.get(mv.format)
    if dtype is None:
        raise TypeError(f"Unsupported pixel buffer format {mv.format!r}")
    flat = np.frombuffer(mv, dtype=dtype)  # no further copy

    # The number of channels actually present in the buffer is len/(w*h), which is
    # authoritative. RGB is stored 4-wide (BGRA) even though MM reports it as 3
    # components — and CMMCore.getNumberOfComponents() (4) and Image.getNumComponents()
    # (3) disagree for the same data, so we trust the buffer, not n_components.
    channels = flat.size // (width * height)
    if channels == 1:
        arr = flat.reshape(height, width)
    else:
        packed = flat.reshape(height, width, channels)
        arr = packed[:, :, 2::-1]  # BGRA -> R,G,B (drop alpha)

    if copy:
        return arr.copy()
    arr.flags.writeable = False
    return arr


def jint(value):
    """Box a Python int as a Java Integer for MM builder setters.

    Many MM builder methods take a boxed ``java.lang.Integer`` (e.g.
    ``Metadata.Builder.bitDepth``, ``SummaryMetadata.Builder.imageWidth`` /
    ``imageHeight``). JPype will not match a bare Python ``int`` to an ``Integer``
    parameter (it raises "No matching overloads ...(int)"), so the value must be
    wrapped. ``None`` passes through unchanged (these fields are nullable).

    Example::

        data.summaryMetadataBuilder().imageWidth(jint(w)).imageHeight(jint(h))
    """
    if value is None:
        return None
    return jpype.JInt(int(value))


# --- numpy -> Image conversion (inverse of the above) -------------------------
#
# DataManager.createImage(pixels, width, height, bytesPerPixel, numComponents,
# coords, metadata) takes a Java byte[]/short[]/float[] of pixel data. createImage
# can make:
#   - 8-bit gray  (uint8   -> byte[],  bpp 1, 1 comp)
#   - 16-bit gray (uint16  -> short[], bpp 2, 1 comp)
#   - 32-bit gray (float32 -> float[], bpp 4, 1 comp; GRAY32)
#   - 8-bit RGB   (uint8   -> byte[],  bpp 4, 3 comp; packed BGRA, RGB32)
# It CANNOT make 16-bit RGB (MM has no 16-bit-per-component RGB PixelType) — that
# is rejected up front. (float32 RGB support required a patch to MM's
# DefaultDataManager.createImage to clone float[]; older MM builds reject float[].)
#
# As in image_to_numpy, we reverse the BGRA packing and the signed/unsigned
# reinterpretation: Java has no unsigned types, so a uint16/uint8 array is
# .view()ed as the same-width *signed* dtype before JArray.of() hands it to the
# JVM (the bit pattern is preserved). float32 needs no reinterpretation (IEEE
# float maps straight to Java float[]). One copy (numpy -> JVM heap) is unavoidable.
_DTYPE_TO_SIGNED = {
    np.dtype(np.uint8): np.int8,       # -> Java byte[]
    np.dtype(np.uint16): np.int16,     # -> Java short[]
    np.dtype(np.float32): np.float32,  # -> Java float[] (no sign reinterpretation)
}


def numpy_to_image(data, array, coords=None, metadata=None):
    """Convert a numpy array to an org.micromanager.data.Image.

    Inverse of image_to_numpy. ``data`` is a DataManager (i.e. ``studio.data()``).
    A 2-D array (height, width) becomes a grayscale Image; a 3-D array
    (height, width, 3) in R,G,B order becomes an RGB Image (repacked to MM's BGRA
    layout with a zero alpha).

    Supported dtypes follow what MM's createImage can build: uint8/uint16/float32
    for grayscale, and uint8 only for RGB. 16-bit RGB is not supported by MM and
    raises TypeError (see the module note above).

    If ``coords`` is omitted a blank one is built (all axes 0). If ``metadata`` is
    omitted, metadata carrying the image's bit depth is built — this is REQUIRED
    for the image to display correctly: MM's display initializes its contrast
    range from Metadata.getBitDepth(), and a null bit depth makes the contrast
    maximum default to Long.MAX_VALUE, rendering the image black (blank viewer).
    Pass your own ``metadata`` to add fields, but include a bitDepth or the view
    will be blank.

    One copy is made (numpy -> JVM heap), which is unavoidable.
    """
    flat_signed, width, height, bytes_per_pixel, num_components = _numpy_to_raw(array)
    pixels = jpype.JArray.of(flat_signed)
    if coords is None:
        coords = data.coordsBuilder().build()
    if metadata is None:
        # bitDepth = bits per component (uint8->8, uint16->16, float32->32). MM's
        # display needs this set or it picks a degenerate contrast range -> black.
        bit_depth = int(np.dtype(array.dtype).itemsize * 8)
        metadata = data.metadataBuilder().bitDepth(jint(bit_depth)).build()
    return data.createImage(
        pixels, width, height, bytes_per_pixel, num_components, coords, metadata
    )


def _numpy_to_raw(array):
    """Shared core: turn a shaped numpy array into createImage's raw arguments.

    Returns (flat_signed, width, height, bytes_per_pixel, num_components), where
    flat_signed is a 1-D numpy array viewed as the signed dtype the JVM expects
    (see the module note above). No JPype/DataManager dependency, so it is
    unit-testable without a JVM. See numpy_to_image for the format rules.
    """
    arr = np.ascontiguousarray(array)
    signed = _DTYPE_TO_SIGNED.get(arr.dtype)
    if signed is None:
        raise TypeError(
            f"Unsupported numpy dtype {arr.dtype!r}; use uint8, uint16, or float32"
        )

    if arr.ndim == 2:
        height, width = arr.shape
        num_components = 1
        bytes_per_pixel = arr.itemsize
        flat = arr.reshape(-1)
    elif arr.ndim == 3 and arr.shape[2] == 3:
        if arr.dtype != np.dtype(np.uint8):
            # MM's only RGB PixelType is RGB32 (8-bit); there is no float or 16-bit RGB.
            raise TypeError(
                f"RGB images must be uint8 (MM has only 8-bit RGB), not {arr.dtype}"
            )
        height, width = arr.shape[:2]
        num_components = 3
        bytes_per_pixel = 4 * arr.itemsize  # MM packs RGB as 4-channel BGRA (RGB32)
        packed = np.zeros((height, width, 4), dtype=arr.dtype)
        packed[:, :, 0] = arr[:, :, 2]  # B
        packed[:, :, 1] = arr[:, :, 1]  # G
        packed[:, :, 2] = arr[:, :, 0]  # R
        # packed[:, :, 3] stays 0 (alpha)
        flat = packed.reshape(-1)
    else:
        raise ValueError(
            f"Expected (h, w) grayscale or (h, w, 3) RGB array, got shape {arr.shape}"
        )

    return flat.view(signed), width, height, bytes_per_pixel, num_components


def snap_core(core, copy: bool = False) -> "np.ndarray":
    """Snap one image straight from CMMCore and return it as a numpy array.

    Uses core.snapImage() + core.getImage(), which is reliable regardless of the
    GUI/live-manager state (studio.live() can be null early in startup). Shape
    and dtype are derived from the Core's image metadata. See image_to_numpy for
    copy semantics; copy=False (default) returns a read-only array.
    """
    core.snapImage()
    raw = core.getImage()
    width = int(core.getImageWidth())
    height = int(core.getImageHeight())
    n_components = int(core.getNumberOfComponents())
    return _raw_to_numpy(raw, width, height, n_components, copy)


def snap(studio, copy: bool = False, display: bool = True):
    """Snap and return the image(s) as numpy array(s).

    display=True (default) snaps via MM's live manager — studio.live().snap(True),
    the same call as the live "Snap" button — so the snapped image is shown in
    MM's display. display=False snaps straight from CMMCore (snap_core) without
    updating any MM window.

    The live manager is not always available (studio.live() can be null until
    MM's GUI finishes initializing); when display=True but it is unavailable,
    this falls back to the CMMCore path (so you still get the array, just without
    the on-screen update).

    Returns a single numpy array for the common single-image case, else a list of
    arrays (one per channel/camera). See image_to_numpy for copy semantics;
    copy=False (default) returns read-only array(s).
    """
    live = studio.live() if display else None
    if live is not None:
        images = live.snap(True)
        if images is not None and images.size() > 0:
            arrays = [
                image_to_numpy(images.get(i), copy=copy) for i in range(images.size())
            ]
            return arrays[0] if len(arrays) == 1 else arrays
    # Fallback: live manager not ready — go straight to the Core.
    return snap_core(studio.core(), copy=copy)


def view(array=None):
    """Open a NON-BLOCKING ndv viewer and return the ArrayViewer.

    Unlike ndv.imshow (which runs the app loop and blocks the prompt), this
    constructs an ndv.ArrayViewer, shows it, and returns immediately so you keep
    the live reference and the prompt stays interactive. Hold onto the returned
    viewer (don't let it be garbage-collected) or it will close.

    Updating the view (the returned `viewer`):
      * Push new pixels:        viewer.data = snap(studio)
      * Show an N-d stack:      viewer.data = np.stack(frames)   # e.g. (T, C, H, W)
      * Move to a time/z point: viewer.display_model.current_index = {0: t, 1: z}
      * Multi-channel display:  set channel_mode / channel_axis / luts on
                                viewer.display_model (see ndv.models.ArrayDisplayModel)
    ndv is an n-dimensional *array* viewer (not napari-style named layers): give
    it one N-d array and drive the axes via current_index, rather than adding
    separate layer objects.

    After mutating data/display_model from a non-GUI thread, call
    refresh(viewer) to repaint.

    Requires ndv with a working Qt/graphics backend (installed via `ndv[qt]`).
    The Qt viewer runs alongside MM's Swing windows in the same process.
    """
    if ndv is None:
        raise RuntimeError(
            "ndv is not available (Qt/graphics backend failed to import); "
            "install it with: uv add 'ndv[qt]'"
        )
    viewer = ndv.ArrayViewer(array) if array is not None else ndv.ArrayViewer()
    viewer.show()
    ndv.process_events()  # pump the Qt loop once so the window actually paints
    return viewer


def refresh(viewer=None):
    """Process pending GUI events so the ndv viewer repaints / stays responsive.

    Call after updating viewer.data or viewer.display_model (especially from a
    non-GUI thread) to flush the change to screen. With no argument it just pumps
    the event loop. No-op if ndv is unavailable.
    """
    if ndv is None:
        return
    ndv.process_events()


# The most recently launched MMStudio, captured so quit_now() can shut it down
# cleanly (which releases MM's profile lock) before hard-exiting.
_studio_ref = None


def release_profile_lock(timeout_s: float = 10.0) -> bool:
    """Cleanly shut down MMStudio so it releases the MM user-profile lock.

    MM holds an OS file lock on UserProfileWriteLock for its whole run and only
    releases it during orderly shutdown (MMStudio.closeSequence -> the profile
    admin's shutdown()). Because we terminate via os._exit (to dodge the JPype/AWT
    hang — see quit_now), that orderly shutdown never runs, so the lock can stay
    held and the *next* MM launch fails with "Failed to acquire user lock". This
    runs closeSequence(true) on a watchdog thread (it touches Swing, which can
    block) and returns once it finishes or the timeout elapses; best-effort, so a
    stuck shutdown never prevents the process from exiting.

    Returns True if the shutdown call completed within the timeout.
    """
    studio = _studio_ref
    if studio is None or not jpype.isJVMStarted():
        return False
    import threading

    done = threading.Event()

    def _close():
        try:
            studio.closeSequence(True)  # true: shutdown sequence; releases the lock
        except Exception:
            pass
        finally:
            done.set()

    t = threading.Thread(target=_close, daemon=True)
    t.start()
    return done.wait(timeout_s)


def quit_now(code: int = 0):
    """Terminate the process immediately, bypassing JPype's blocking shutdown.

    The JVM started by JPype keeps non-daemon AWT/Swing (EDT) threads alive, so
    a normal interpreter exit — closing the MM window, typing exit()/Ctrl-D, or
    falling off the end of the script — calls jpype.shutdownJVM(), which blocks
    forever waiting for those threads to die. os._exit() skips interpreter
    teardown (and that blocking JVM shutdown) and ends the process at once.

    Before exiting we shut MMStudio down cleanly so it releases the profile lock
    (otherwise the next MM launch hits "Failed to acquire user lock"). That step
    is best-effort and time-bounded; os._exit then guarantees termination.
    """
    release_profile_lock()
    os._exit(code)


def _install_clean_exit() -> None:
    """Make any normal exit terminate immediately instead of hanging.

    Registered as an atexit handler so that in `python -i` typing exit() or
    Ctrl-D actually quits, and a non-interactive run ends after its work — while
    still releasing MM's profile lock first (see quit_now / release_profile_lock).
    """
    import atexit

    atexit.register(quit_now)


def main(quiet: bool = True, skip_intro: bool = False, wait_for_live: bool = True):
    mm_root = find_mm_root()
    print(f"MM root: {mm_root}")
    start_jvm(mm_root)
    studio, core = launch_imagej_with_mm(
        quiet=quiet, skip_intro=skip_intro, wait_for_live=wait_for_live
    )
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

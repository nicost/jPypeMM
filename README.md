# jPypeMM

Launch **ImageJ** with **Micro-Manager 2.0** loaded as a plugin, in-process from Python
using [JPype](https://jpype.readthedocs.io). The JVM runs *inside* the Python process via
JNI — no sockets, no IPC — so Python holds direct references to the MM `Studio` and `CMMCore`
objects and can call any Java method (including plugin classes) with near-zero overhead.

This reproduces Micro-Manager's real startup path: it starts `ij.ImageJ` and then runs the
`MMStudioPlugin` exactly as the **Plugins > Micro-Manager Studio** menu item does, rather than
constructing `MMStudio` directly.

## Requirements

- Windows, run in a **desktop session** (ImageJ/MM are Swing GUI apps — no headless mode).
- A 64-bit **Micro-Manager 2.0** install (auto-detected under `C:\Program Files\`).
- [`uv`](https://docs.astral.sh/uv/) for environment management.
- Microsoft Visual C++ 2015–2022 x64 redistributable (MM's device DLLs depend on it).

No separate JDK is needed: the script uses Micro-Manager's **bundled JRE 11** at runtime.

## Setup

```powershell
uv sync
```

## Run

```powershell
# Interactive: GUI stays open, `studio` and `core` available at the prompt
uv run python -i start_mm.py

# Non-interactive: launches and holds the GUI open until Ctrl+C
uv run python start_mm.py
```

To target a specific install, set `MM_DIR`:

```powershell
$env:MM_DIR = "C:\Program Files\Micro-Manager-2.0.3"
uv run python -i start_mm.py
```

## Example session

```python
>>> core.snapImage()
>>> img = core.getImage()                  # raw pixel buffer
>>> studio.live().setLiveModeOn(True)      # opens MM's live view window
>>> core.loadSystemConfiguration(r"C:\Program Files\Micro-Manager-2.0\MMConfig_demo.cfg")
>>> list(core.getLoadedDevices())
```

## How it works

1. **Bundled JRE** — JPype is pointed explicitly at `jre\bin\server\jvm.dll` inside the MM
   install. The native bridge (`MMCoreJ_wrap.dll`) and device adapters are built against this
   JRE 11, so any system JDK on the machine is deliberately *not* used.
2. **Classpath** — all jars from `ij.jar`, `plugins\Micro-Manager\`, `mmplugins\`, and
   `mmautofocus\` are globbed (the stale `stack-backup\` subdir is excluded, duplicates are
   removed by filename).
3. **Native DLLs** — the install root (containing `MMCoreJ_wrap.dll` and ~341 device DLLs) plus
   the JRE dirs are registered via `os.add_dll_directory()` **before** the JVM starts, and the
   install root is also passed as `-Djava.library.path`, so `System.loadLibrary` resolves the
   bridge and dependent DLLs.
4. **Launch** — `ij.ImageJ()` opens, then `IJ.runPlugIn("MMStudioPlugin", "")` loads MM; the
   script polls `MMStudio.getInstance()` to get the live `studio` / `core` handles.

## Quiet console (default)

By default the launcher suppresses MM's and ImageJ's console output so the interactive prompt
stays usable — otherwise log lines stream over whatever you're typing. Everything is still
written to MM's `CoreLogs/` file on disk; only the console copy is silenced. Two sources are
handled: MM Core's stderr logger (`core.enableStderrLog(False)`) and the lines ImageJ/SciJava
print directly to Java's `System.out`/`System.err` (redirected to the OS null device before the
plugin load, so even the startup dump is hidden). Your own Python prints and the REPL are
unaffected.

To see the full MM/ImageJ console output, launch verbosely:

```python
>>> import start_mm
>>> studio, core = start_mm.main(quiet=False)
```

A one-time burst of MM startup log lines still appears at launch even in quiet mode: MM's
Core logger writes to the native stderr file descriptor during initialization, before the
launcher can disable it. MM also re-enables stderr logging asynchronously a fraction of a
second into startup, so the launcher polls and re-disables it for a few seconds until it stays
off — after that the console is quiet through steady-state use.

## Exiting

The JVM that JPype starts keeps non-daemon AWT/Swing threads alive, so a normal interpreter
exit would call `jpype.shutdownJVM()` and **hang** waiting for those threads. The launcher
installs an `atexit` handler (`quit_now()`) that calls `os._exit(0)` to terminate immediately,
so you can quit the `-i` prompt with `exit()` / Ctrl-D (or call `quit_now()`) without hanging.
Closing the process also closes the MM windows, since they live in the same JVM.

## Snapping images as numpy arrays

`snap(studio)` snaps an image and returns it as a numpy array with the correct shape and dtype:

```python
>>> import start_mm
>>> studio, core = start_mm.main(skip_intro=True)
>>> core.loadSystemConfiguration(r"C:\Program Files\Micro-Manager-2.0\MMConfig_demo.cfg")
>>> img = start_mm.snap(studio)        # read-only array
>>> img.shape, img.dtype               # e.g. (512, 512) uint16
>>> img = start_mm.snap(studio, copy=True)   # writable copy
```

Shape/dtype follow the camera's pixel type: grayscale → `(height, width)` as `uint8` / `uint16`
/ `float32`; RGB → `(height, width, 3)` as `uint8` / `uint16` in **R, G, B** order (MM stores
RGB packed as BGRA; the alpha channel is dropped).

Related helpers: `snap_core(core)` snaps straight from `CMMCore` (always works, even before the
GUI is ready); `image_to_numpy(image)` converts an existing `org.micromanager.data.Image`.

**On copying / "zero-copy":** a true zero-copy view is not possible here. MM returns pixels as a
Java on-heap primitive array (`byte[]`/`short[]`/`float[]`) — never an off-heap `java.nio`
direct buffer — and the JVM garbage collector may relocate heap arrays, so no stable pointer can
be shared with numpy. JPype's `memoryview()` of such an array is itself a **read-only copy** out
of the JVM. These functions therefore make **exactly one copy** (JVM heap → numpy) and add no
further Python-side copies; that single copy is unavoidable. The result is returned **read-only**
by default (a view onto that one buffer); pass `copy=True` for a writable array, at the cost of a
second copy.

`snap(studio)` prefers MM's live manager (`studio.live().snap(True)`, the same call as the live
"Snap" button, so the MM display updates too) and falls back to the `CMMCore` path when the live
manager isn't available yet (`studio.live()` can be `null` until MM's GUI finishes initializing).

## Building a Datastore and viewing it in Micro-Manager

`numpy_to_image(data, array, ...)` is the inverse of `image_to_numpy`: it turns a numpy array
into an `org.micromanager.data.Image` via `DataManager.createImage`. Combined with a RAM
Datastore and a DataViewer, you can push processed numpy data back into MM's own display.

This snippet (paste it into an interactive session where `studio`/`core` already exist — e.g.
after `uv run python -i start_mm.py`) snaps an image, computes an Otsu threshold with
scikit-image, and shows the original and the mask as a **2-channel** dataset in a DataViewer:

```python
import numpy as np
from skimage.filters import threshold_otsu
import start_mm

data = studio.data()

# 1. snap
original = start_mm.snap_core(core, copy=True)

# 2. Otsu mask, in the original's dtype so both channels share a pixel type
level = threshold_otsu(original)
on = np.iinfo(original.dtype).max if np.issubdtype(original.dtype, np.integer) else 1.0
mask = np.where(original >= level, on, 0).astype(original.dtype)

# 3. 2-channel RAM datastore (set summary metadata BEFORE putImage)
#    imageWidth/imageHeight take a boxed Java Integer; start_mm.jint() converts it.
h, w = original.shape[:2]
store = data.createRAMDatastore()
store.setSummaryMetadata(
    data.summaryMetadataBuilder()
        .channelNames("Original", "Otsu mask")
        .intendedDimensions(data.coordsBuilder().c(2).build())
        .imageWidth(start_mm.jint(w))
        .imageHeight(start_mm.jint(h))
        .build()
)
for ch, arr in enumerate((original, mask)):
    img = start_mm.numpy_to_image(data, arr, coords=data.coordsBuilder().c(ch).build())
    store.putImage(img)
store.freeze()

# 4. open a Micro-Manager DataViewer on the datastore
display = studio.displays().createDisplay(store)
```

Keep references to `store` and `display` alive (assigning them at the prompt is enough) or the
window may close. If `snap_core` fails because no camera is loaded, run
`core.loadSystemConfiguration(str(start_mm.find_mm_root() / "MMConfig_demo.cfg"))` first.

The full version is in [`examples/snap_threshold_2channel.py`](examples/snap_threshold_2channel.py).

**Why images show up (bit depth metadata):** MM's display initializes its contrast range from
each image's `Metadata.getBitDepth()`. If that is unset, the contrast maximum defaults to
`Long.MAX_VALUE` and the image renders **completely black** (a blank-looking viewer).
`numpy_to_image` sets `bitDepth` automatically from the array dtype (uint8→8, uint16→16,
float32→32), so the default path "just works". If you pass your **own** `metadata=`, include a
`bitDepth` (`data.metadataBuilder().bitDepth(jpype.JInt(16))...`) or the view will be blank.

Two kinds of metadata are involved, and they do different things: per-image `Metadata` (bit
depth, pixel size, exposure, …) describes each image; the Datastore's `SummaryMetadata`
(`channelNames`, `intendedDimensions`, …) describes the dataset as a whole and drives the
channel sliders/names in the viewer.

## Startup dialog (IntroDlg)

By default MM shows a modal startup dialog (pick a user profile + hardware config, click OK)
that blocks until a human responds — fine interactively, fatal for automated launches. The
launcher leaves this **unchanged by default**: `uv run python -i start_mm.py` (and normal MM
use) still show the dialog.

For unattended/automated launches, pass `skip_intro=True`:

```python
>>> import start_mm
>>> studio, core = start_mm.main(skip_intro=True)   # no dialog, no human input
```

This works by setting MM's "skip profile selection" and "skip config selection" flags in the
user profile (`suppress_intro_dialog()`), which is what MM itself persists when you tick those
boxes in the dialog. **It is a persisted, per-machine setting**: once set, normal MM launches
also skip the dialog until you re-enable it from MM's startup dialog. With the dialog skipped MM
starts with only the `Core` device; load a config yourself afterward
(`core.loadSystemConfiguration(...)`). The integration tests use `skip_intro=True` so they run
unattended.

## Tests

```powershell
uv run pytest                       # fast unit tests (no JVM / MM needed)
```

The unit tests cover the path and classpath logic — install discovery (including the
`MM_DIR` override and the Gamma-build ranking), bundled-JVM lookup, and classpath assembly
(`stack-backup` exclusion, filename de-dup, `ij.jar` ordering, required-jar checks) — against a
fake MM tree, plus the clean-exit handler wiring.

Live integration tests that actually launch ImageJ + Micro-Manager are opt-in (they need a real
MM install on a desktop session):

```powershell
$env:JPYPEMM_RUN_INTEGRATION = "1"
uv run pytest tests/test_integration.py
```

## Notes

- Micro-Manager writes its `CoreLogs/` directory and searches for some plugins relative to
  the **current working directory**, not the install dir. Running from the project folder is
  fine (`CoreLogs/` is gitignored); the plugins already on the classpath load regardless.

## Caveats (inherent to the in-process model)

- **The agent owns MM's lifecycle.** MM runs only while this Python process runs; restarting
  Python restarts MM. The biologist cannot open MM independently and attach afterward.
- **One JVM per process, not restartable.** A misbehaving device adapter that crashes the JVM
  takes the Python process down with it.

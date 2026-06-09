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

## Viewing images with ndv

The environment includes [`ndv`](https://github.com/pyapp-kit/ndv) (an n-dimensional array
viewer, installed with the Qt backend via `ndv[qt]`). It's imported in `start_mm`.

`view(array)` opens a **non-blocking** viewer and returns the `ndv.ArrayViewer` so you keep the
prompt and can update it live (unlike `ndv.imshow`, which runs the app loop and blocks):

```python
>>> viewer = view(snap(studio))        # opens a window, returns immediately
>>> viewer.data = snap(studio)         # push new pixels into the same window
>>> refresh(viewer)                    # repaint after an update
```

Hold onto the returned `viewer` (don't let it be garbage-collected, or the window closes).

**Layers / time points / channels:** ndv is an n-dimensional *array* viewer (not napari-style
named layers). To show a stack, give it one N-d array and drive the axes:

```python
>>> import numpy as np
>>> viewer = view(np.stack(frames))           # e.g. shape (T, C, H, W)
>>> viewer.display_model.current_index = {0: t, 1: z}   # jump to a time/z point
>>> refresh(viewer)
```

Multi-channel display (channel_mode / channel_axis / luts) is configured on
`viewer.display_model` — see `ndv.models.ArrayDisplayModel`.

**REPL note:** in a plain `python -i` prompt nothing pumps the Qt event loop while you type, so
the window can look frozen between commands; `view()`/`refresh()` pump it once per call so your
updates appear. For continuous interactivity (dragging sliders), call `refresh()` after
interacting, or use `ndv.run_app()` (blocking) when you're done issuing commands.

`view(array)` raises if ndv's Qt/graphics backend failed to import. The ndv viewer (Qt) runs
alongside MM's Swing windows in the same process.

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

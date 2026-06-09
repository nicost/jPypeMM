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

## Notes

- Micro-Manager writes its `CoreLogs/` directory and searches for some plugins relative to
  the **current working directory**, not the install dir. Running from the project folder is
  fine (`CoreLogs/` is gitignored); the plugins already on the classpath load regardless.

## Caveats (inherent to the in-process model)

- **The agent owns MM's lifecycle.** MM runs only while this Python process runs; restarting
  Python restarts MM. The biologist cannot open MM independently and attach afterward.
- **One JVM per process, not restartable.** A misbehaving device adapter that crashes the JVM
  takes the Python process down with it.

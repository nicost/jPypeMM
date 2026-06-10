"""Interactive Micro-Manager: launch MM and drop into IPython with Qt GUI integration.

This is the recommended way to use jPypeMM interactively when you want the ndv
viewer to stay continuously responsive. It:

  1. starts the JVM and launches ImageJ + Micro-Manager (start_mm.main),
  2. enables IPython's Qt event-loop integration (the equivalent of %gui qt), so
     ndv's Qt windows repaint and respond between prompt commands without manual
     refresh() calls,
  3. drops you into an IPython shell with `studio`, `core`, the start_mm helpers
     (snap, view, refresh, ...) and the module itself (`mm`) already in scope.

Run it with:

    uv run python imm.py                 # shows MM's startup dialog (default)
    uv run python imm.py --skip-intro    # suppress the dialog (persisted; tests/automation)

Type exit() / Ctrl-D to quit (the clean-exit handler releases MM's profile lock
first, then terminates).
"""
from __future__ import annotations

import argparse

import start_mm


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-intro",
        action="store_true",
        help="suppress MM's modal startup dialog (persisted profile setting)",
    )
    parser.add_argument(
        "--no-quiet",
        action="store_true",
        help="show MM/ImageJ console output (off by default)",
    )
    parser.add_argument(
        "--simple-prompt",
        action="store_true",
        help=(
            "fall back to IPython's plain prompt (no prompt_toolkit). Use this if "
            "the Qt GUI integration misbehaves; you then drive the viewer with "
            "explicit refresh() calls instead of a live event loop."
        ),
    )
    args = parser.parse_args()

    # Pin ndv to the Qt GUI backend BEFORE anything creates a viewer, so ndv's
    # canvas (rendercanvas) shares Qt's event loop rather than spinning up its
    # own asyncio/trio loop. Without this, IPython's Qt inputhook collides with
    # rendercanvas's default loop ("Incompatible awaitable result ..."). Import a
    # Qt binding first so rendercanvas binds to Qt.
    if start_mm.ndv is not None:
        try:
            import PyQt6.QtWidgets  # noqa: F401  (selects the Qt toolkit)

            start_mm.ndv.set_gui_backend("qt")
        except Exception:
            pass

    start_mm._install_clean_exit()
    studio, core = start_mm.main(quiet=not args.no_quiet, skip_intro=args.skip_intro)

    # Namespace exposed at the IPython prompt.
    user_ns = {
        "mm": start_mm,
        "studio": studio,
        "core": core,
        "snap": start_mm.snap,
        "snap_core": start_mm.snap_core,
        "view": start_mm.view,
        "refresh": start_mm.refresh,
        "image_to_numpy": start_mm.image_to_numpy,
    }
    if start_mm.ndv is not None:
        user_ns["ndv"] = start_mm.ndv

    banner = (
        "\njPypeMM interactive shell (IPython + Qt GUI integration).\n"
        "  studio, core ready. Helpers: snap(studio), view(array), refresh(viewer).\n"
        "  ndv Qt windows stay responsive — no manual refresh() needed.\n"
        "  exit() / Ctrl-D to quit.\n"
    )

    from IPython import start_ipython
    from traitlets.config import Config

    config = Config()
    config.TerminalInteractiveShell.banner1 = banner
    config.TerminalIPythonApp.display_banner = True
    if args.simple_prompt:
        # No prompt_toolkit input loop and so no Qt inputhook: the viewer won't
        # auto-update — call refresh(viewer) after changes.
        config.TerminalInteractiveShell.simple_prompt = True
    else:
        # %gui qt equivalent: integrate the Qt event loop with the prompt so ndv
        # (and MM's Swing windows) stay live while you type.
        config.InteractiveShellApp.gui = "qt"

    start_ipython(argv=[], user_ns=user_ns, config=config)


if __name__ == "__main__":
    main()

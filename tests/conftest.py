"""Shared fixtures for the start_mm tests."""
from __future__ import annotations

from pathlib import Path

import pytest


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


@pytest.fixture
def fake_mm(tmp_path: Path) -> Path:
    """Build a minimal but realistic Micro-Manager install tree on disk.

    Mirrors the real Windows layout this project targets: the native bridge and
    ij.jar in the root, a bundled JRE jvm.dll, jars spread across three plugin
    dirs, and a stale stack-backup/ subdir that must be excluded.
    """
    root = tmp_path / "Micro-Manager-2.0"

    # Native bridge + ImageJ jar that find_mm_root keys on.
    _touch(root / "MMCoreJ_wrap.dll")
    _touch(root / "ij.jar")

    # Bundled JRE jvm.dll.
    _touch(root / "jre" / "bin" / "server" / "jvm.dll")

    # Required jars live under plugins/Micro-Manager.
    _touch(root / "plugins" / "Micro-Manager" / "MMJ_.jar")
    _touch(root / "plugins" / "Micro-Manager" / "MMCoreJ.jar")
    _touch(root / "plugins" / "Micro-Manager" / "some-plugin.jar")

    # Stale backups that must NOT end up on the classpath.
    _touch(root / "plugins" / "Micro-Manager" / "stack-backup" / "MMJ_.jar")
    _touch(root / "plugins" / "Micro-Manager" / "stack-backup" / "old.jar")

    # Other plugin dirs.
    _touch(root / "mmplugins" / "extra.jar")
    _touch(root / "mmautofocus" / "autofocus.jar")

    return root

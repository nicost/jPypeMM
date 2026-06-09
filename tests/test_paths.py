"""Unit tests for start_mm's path/classpath logic.

These run without a JVM or a real Micro-Manager install — they exercise the
real filesystem code against a fake MM tree (see conftest.fake_mm), so the
stack-backup exclusion, de-dup, ordering, and required-jar checks are tested
faithfully rather than mocked.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import start_mm


# --------------------------------------------------------------------------
# find_mm_root
# --------------------------------------------------------------------------
def test_find_mm_root_honors_mm_dir_env(fake_mm, monkeypatch):
    monkeypatch.setenv("MM_DIR", str(fake_mm))
    # Point ProgramFiles somewhere empty so only MM_DIR can match.
    monkeypatch.setenv("ProgramFiles", str(fake_mm.parent / "nonexistent"))
    assert start_mm.find_mm_root() == fake_mm.resolve()


def test_find_mm_root_discovers_under_program_files(fake_mm, monkeypatch):
    monkeypatch.delenv("MM_DIR", raising=False)
    monkeypatch.setenv("ProgramFiles", str(fake_mm.parent))
    assert start_mm.find_mm_root() == fake_mm.resolve()


def test_find_mm_root_prefers_non_gamma_build(tmp_path, monkeypatch):
    """A Gamma build must rank below a modern numbered build even though its
    name sorts higher alphabetically."""
    pf = tmp_path
    for name in ("Micro-Manager-2.0", "Micro-Manager-2.0Gamma-20201208"):
        root = pf / name
        (root / "jre" / "bin" / "server").mkdir(parents=True)
        (root / "MMCoreJ_wrap.dll").write_bytes(b"")
        (root / "ij.jar").write_bytes(b"")
    monkeypatch.delenv("MM_DIR", raising=False)
    monkeypatch.setenv("ProgramFiles", str(pf))
    assert start_mm.find_mm_root().name == "Micro-Manager-2.0"


def test_find_mm_root_skips_incomplete_install(tmp_path, monkeypatch):
    """A dir missing the native bridge is not a valid install."""
    pf = tmp_path
    bad = pf / "Micro-Manager-2.0"
    bad.mkdir()
    (bad / "ij.jar").write_bytes(b"")  # has ij.jar but no MMCoreJ_wrap.dll
    monkeypatch.delenv("MM_DIR", raising=False)
    monkeypatch.setenv("ProgramFiles", str(pf))
    with pytest.raises(FileNotFoundError):
        start_mm.find_mm_root()


def test_find_mm_root_raises_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delenv("MM_DIR", raising=False)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "empty"))
    with pytest.raises(FileNotFoundError):
        start_mm.find_mm_root()


# --------------------------------------------------------------------------
# find_bundled_jvm
# --------------------------------------------------------------------------
def test_find_bundled_jvm_returns_server_jvm(fake_mm):
    jvm = start_mm.find_bundled_jvm(fake_mm)
    assert jvm == fake_mm / "jre" / "bin" / "server" / "jvm.dll"
    assert jvm.is_file()


def test_find_bundled_jvm_raises_when_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        start_mm.find_bundled_jvm(tmp_path)


# --------------------------------------------------------------------------
# build_classpath
# --------------------------------------------------------------------------
def test_build_classpath_includes_required_jars(fake_mm):
    cp = start_mm.build_classpath(fake_mm)
    names = {Path(e).name for e in cp}
    assert {"ij.jar", "MMJ_.jar", "MMCoreJ.jar"} <= names


def test_build_classpath_excludes_stack_backup(fake_mm):
    cp = start_mm.build_classpath(fake_mm)
    assert not any("stack-backup" in e for e in cp)
    # The stale old.jar lives only in stack-backup and must be absent.
    assert not any(Path(e).name == "old.jar" for e in cp)


def test_build_classpath_ij_jar_is_first(fake_mm):
    cp = start_mm.build_classpath(fake_mm)
    assert Path(cp[0]).name == "ij.jar"


def test_build_classpath_dedups_by_filename(fake_mm):
    """MMJ_.jar exists in both Micro-Manager/ and stack-backup/; only one entry
    (the non-backup one) should survive."""
    cp = start_mm.build_classpath(fake_mm)
    mmj = [e for e in cp if Path(e).name == "MMJ_.jar"]
    assert len(mmj) == 1
    assert "stack-backup" not in mmj[0]


def test_build_classpath_includes_other_plugin_dirs(fake_mm):
    cp = start_mm.build_classpath(fake_mm)
    names = {Path(e).name for e in cp}
    assert "extra.jar" in names  # mmplugins/
    assert "autofocus.jar" in names  # mmautofocus/


def test_build_classpath_raises_when_required_jar_missing(fake_mm):
    (fake_mm / "plugins" / "Micro-Manager" / "MMJ_.jar").unlink()
    with pytest.raises(RuntimeError, match="MMJ_.jar"):
        start_mm.build_classpath(fake_mm)

"""Unit tests for image_to_numpy (no JVM needed).

These use a fake Image whose getRawPixels() returns a real Python buffer
(array.array), so memoryview/np.frombuffer exercise the exact same conversion
path as a JPype Java array — without a live JVM.
"""
from __future__ import annotations

import array

import numpy as np
import pytest

import start_mm


class FakeImage:
    """Mimics the bits of org.micromanager.data.Image that image_to_numpy uses."""

    def __init__(self, width, height, n_components, raw):
        self._w, self._h, self._n, self._raw = width, height, n_components, raw

    def getWidth(self):
        return self._w

    def getHeight(self):
        return self._h

    def getNumComponents(self):
        return self._n

    def getRawPixels(self):
        return self._raw


def test_8bit_gray_shape_and_dtype():
    raw = array.array("b", range(-128, -128 + 6))  # signed bytes, 6 px
    img = FakeImage(3, 2, 1, raw)
    arr = start_mm.image_to_numpy(img)
    assert arr.shape == (2, 3)
    assert arr.dtype == np.uint8
    # signed -128 must read back as unsigned 128
    assert arr[0, 0] == 128


def test_16bit_gray_is_uint16():
    # Values above int16 max must survive as unsigned.
    raw = array.array("h", [0, 1000, -1, 32767, -32768, 12345])  # signed shorts
    img = FakeImage(3, 2, 1, raw)
    arr = start_mm.image_to_numpy(img)
    assert arr.shape == (2, 3)
    assert arr.dtype == np.uint16
    assert arr[0, 1] == 1000
    assert arr[0, 2] == 65535  # -1 signed -> 65535 unsigned


def test_32bit_float():
    raw = array.array("f", [1.5, 2.5, 3.5, 4.5])
    img = FakeImage(2, 2, 1, raw)
    arr = start_mm.image_to_numpy(img)
    assert arr.shape == (2, 2)
    assert arr.dtype == np.float32
    assert arr[0, 0] == 1.5


def test_rgb_drops_alpha_and_reorders_bgra_to_rgb():
    # One pixel, BGRA = (B=10, G=20, R=30, A=0) -> RGB should be (30, 20, 10).
    raw = array.array("b", [10, 20, 30, 0])
    img = FakeImage(1, 1, 4, raw)
    arr = start_mm.image_to_numpy(img, copy=True)  # copy so we can assert contiguous values
    assert arr.shape == (1, 1, 3)
    assert arr.dtype == np.uint8
    assert list(arr[0, 0]) == [30, 20, 10]  # R, G, B


def test_default_is_readonly_copy_true_is_writable():
    raw = array.array("b", [1, 2, 3, 4])
    img = FakeImage(2, 2, 1, raw)

    ro = start_mm.image_to_numpy(img)  # default copy=False
    assert not ro.flags.writeable
    with pytest.raises(ValueError):
        ro[0, 0] = 9

    rw = start_mm.image_to_numpy(img, copy=True)
    assert rw.flags.writeable
    rw[0, 0] = 9  # must not raise
    assert rw[0, 0] == 9


def test_unsupported_format_raises():
    raw = array.array("i", [1, 2, 3, 4])  # 32-bit signed int: not a MM pixel type
    img = FakeImage(2, 2, 1, raw)
    with pytest.raises(TypeError):
        start_mm.image_to_numpy(img)


# --- snap(): display flag routing --------------------------------------------
class _FakeImageList:
    def __init__(self, images):
        self._images = images

    def size(self):
        return len(self._images)

    def get(self, i):
        return self._images[i]


class _FakeLive:
    def __init__(self, img):
        self._img = img
        self.called = False

    def snap(self, should_display):
        self.called = True
        return _FakeImageList([self._img])


class _FakeCore:
    def __init__(self, width, height, n_components, raw):
        self._w, self._h, self._n, self._raw = width, height, n_components, raw
        self.snap_called = False

    def snapImage(self):
        self.snap_called = True

    def getImage(self):
        return self._raw

    def getImageWidth(self):
        return self._w

    def getImageHeight(self):
        return self._h

    def getNumberOfComponents(self):
        return self._n


class _FakeStudio:
    def __init__(self, live, core):
        self._live, self._core = live, core

    def live(self):
        return self._live

    def core(self):
        return self._core


def _make_studio(live_available=True):
    raw = array.array("b", [1, 2, 3, 4])
    img = FakeImage(2, 2, 1, raw)
    live = _FakeLive(img) if live_available else None
    core = _FakeCore(2, 2, 1, raw)
    return _FakeStudio(live, core), live, core


def test_snap_display_true_uses_live_manager():
    studio, live, core = _make_studio(live_available=True)
    arr = start_mm.snap(studio, display=True)
    assert live.called is True            # went through studio.live().snap(...)
    assert core.snap_called is False      # did NOT use the Core path
    assert arr.shape == (2, 2)


def test_snap_display_false_uses_core_no_display():
    studio, live, core = _make_studio(live_available=True)
    arr = start_mm.snap(studio, display=False)
    assert live.called is False           # live manager untouched -> no display update
    assert core.snap_called is True       # snapped via CMMCore
    assert arr.shape == (2, 2)


def test_snap_falls_back_to_core_when_live_unavailable():
    studio, live, core = _make_studio(live_available=False)
    arr = start_mm.snap(studio, display=True)  # asked to display, but live() is None
    assert core.snap_called is True            # fell back to Core path
    assert arr.shape == (2, 2)

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


# --- _numpy_to_raw: numpy -> createImage args (no JVM) -----------------------
# Mirrors the image_to_numpy tests above. _numpy_to_raw is the JVM-free worker
# behind numpy_to_image; it returns (flat_signed, width, height,
# bytes_per_pixel, num_components).
def test_numpy_to_raw_8bit_gray():
    arr = np.arange(6, dtype=np.uint8).reshape(2, 3)
    flat, w, h, bpp, comps = start_mm._numpy_to_raw(arr)
    assert (w, h, bpp, comps) == (3, 2, 1, 1)
    assert flat.dtype == np.int8
    assert list(flat) == list(arr.reshape(-1))


def test_numpy_to_raw_16bit_gray_preserves_unsigned():
    # 40000 > int16 max: the bit pattern must survive as a signed short.
    arr = np.array([[0, 1000, 40000], [32768, 65535, 12345]], dtype=np.uint16)
    flat, w, h, bpp, comps = start_mm._numpy_to_raw(arr)
    assert (w, h, bpp, comps) == (3, 2, 2, 1)
    assert flat.dtype == np.int16
    # Reinterpreting back to uint16 must recover the original values.
    assert list(flat.view(np.uint16)) == list(arr.reshape(-1))


def test_numpy_to_raw_32bit_float_gray():
    # float32 grayscale -> Java float[] (bpp 4, 1 comp); no sign reinterpretation.
    arr = np.array([[1.5, 2.5, 3.5], [-4.5, 0.0, 1e6]], dtype=np.float32)
    flat, w, h, bpp, comps = start_mm._numpy_to_raw(arr)
    assert (w, h, bpp, comps) == (3, 2, 4, 1)
    assert flat.dtype == np.float32
    assert list(flat) == list(arr.reshape(-1))


def test_numpy_to_raw_rgb_packs_to_bgra():
    # One pixel, RGB = (30, 20, 10) -> packed BGRA = (10, 20, 30, 0).
    arr = np.array([[[30, 20, 10]]], dtype=np.uint8)
    flat, w, h, bpp, comps = start_mm._numpy_to_raw(arr)
    assert (w, h, bpp, comps) == (1, 1, 4, 3)
    assert list(flat.view(np.uint8)) == [10, 20, 30, 0]  # B, G, R, A


def test_numpy_to_raw_rejects_bad_dtype():
    with pytest.raises(TypeError):
        start_mm._numpy_to_raw(np.zeros((2, 2), dtype=np.int32))


def test_numpy_to_raw_rejects_uint16_rgb():
    # MM has no 16-bit-per-component RGB PixelType; uint16 RGB must be rejected.
    with pytest.raises(TypeError, match="RGB"):
        start_mm._numpy_to_raw(np.zeros((1, 1, 3), dtype=np.uint16))


def test_numpy_to_raw_rejects_float_rgb():
    with pytest.raises(TypeError):
        start_mm._numpy_to_raw(np.zeros((1, 1, 3), dtype=np.float32))


def test_numpy_to_raw_rejects_bad_shape():
    with pytest.raises(ValueError):
        start_mm._numpy_to_raw(np.zeros((2, 2, 2), dtype=np.uint8))


def test_roundtrip_gray_and_rgb():
    # numpy -> raw -> _raw_to_numpy must recover the original array. _raw_to_numpy
    # derives the channel count from the buffer length (not a component count).
    for arr in (
        np.array([[1, 2, 3], [4, 5, 6]], dtype=np.uint8),
        np.array([[0, 40000], [65535, 123]], dtype=np.uint16),
        np.array([[1.5, -2.5], [3.5, 4.5]], dtype=np.float32),
        np.array([[[30, 20, 10], [1, 2, 3]]], dtype=np.uint8),  # (1,2,3) RGB
    ):
        flat, w, h, bpp, comps = start_mm._numpy_to_raw(arr)
        back = start_mm._raw_to_numpy(flat, w, h, copy=True)
        assert np.array_equal(back, arr)


# --- snap(): display flag routing --------------------------------------------
class _FakeImageList:
    def __init__(self, images):
        self._images = images

    def size(self):
        return len(self._images)

    def get(self, i):
        return self._images[i]


class _FakeLive:
    """Mimics studio.live(): snap(shouldDisplay) -> image list, recording the arg."""

    def __init__(self, *imgs):
        self._imgs = imgs
        self.snap_display_arg = None  # the bool passed to the last snap()

    def snap(self, should_display):
        self.snap_display_arg = should_display
        return _FakeImageList(list(self._imgs))


def test_snap_passes_display_true_to_live():
    raw = array.array("b", [1, 2, 3, 4])
    live = _FakeLive(FakeImage(2, 2, 1, raw))
    arr = start_mm.snap(live, display=True)
    assert live.snap_display_arg is True   # live.snap(True): image shown in MM display
    assert arr.shape == (2, 2)


def test_snap_passes_display_false_to_live():
    raw = array.array("b", [1, 2, 3, 4])
    live = _FakeLive(FakeImage(2, 2, 1, raw))
    arr = start_mm.snap(live, display=False)
    assert live.snap_display_arg is False  # live.snap(False): snapped without display
    assert arr.shape == (2, 2)


def test_snap_returns_list_for_multiple_images():
    raw = array.array("b", [1, 2, 3, 4])
    live = _FakeLive(FakeImage(2, 2, 1, raw), FakeImage(2, 2, 1, raw))
    arrs = start_mm.snap(live)
    assert isinstance(arrs, list) and len(arrs) == 2
    assert all(a.shape == (2, 2) for a in arrs)


def test_snap_raises_when_live_returns_no_images():
    # live.snap() can return null/empty (it swallows internal errors); snap must
    # raise a clear error rather than crash on .size()/.get().
    class _NullLive:
        def snap(self, should_display):
            return None

    class _EmptyLive:
        def snap(self, should_display):
            return _FakeImageList([])

    for live in (_NullLive(), _EmptyLive()):
        with pytest.raises(RuntimeError, match="no images"):
            start_mm.snap(live)


def test_snap_raises_clearly_when_live_is_none():
    # studio.live() is null until the GUI is ready; snap(None) must raise a clear
    # RuntimeError, not a bare AttributeError on None.snap().
    with pytest.raises(RuntimeError, match="live is None"):
        start_mm.snap(None)

"""Unit tests for TaggedImage -> numpy and JSON-tags -> Python (no JVM needed).

These use fakes that mimic the bits of mmcorej.TaggedImage / mmcorej.org.json
that the converters touch, so the same code path runs without a live JVM:
  * FakeTaggedImage has `.pix` (a real Python buffer) and `.tags`.
  * FakeJSONObject / FakeJSONArray mimic the org.json keys()/get()/length() API.
"""
from __future__ import annotations

import array

import numpy as np
import pytest

import start_mm


class FakeJSONObject:
    """Mimics mmcorej.org.json.JSONObject: keys() + get(key) + has(key)."""

    def __init__(self, mapping):
        self._m = dict(mapping)

    def keys(self):
        # Real JSONObject.keys() returns a Java Iterator; a Python iterator of the
        # keys exercises the same consumption path (for k in obj.keys()).
        return iter(list(self._m.keys()))

    def get(self, key):
        return self._m[key]

    def has(self, key):
        return key in self._m

    def length(self):
        return len(self._m)


class FakeJSONArray:
    """Mimics mmcorej.org.json.JSONArray: length() + get(index)."""

    def __init__(self, items):
        self._items = list(items)

    def length(self):
        return len(self._items)

    def get(self, i):
        return self._items[i]


class FakeTaggedImage:
    """Mimics mmcorej.TaggedImage: public fields `pix` and `tags`."""

    def __init__(self, pix, tags):
        self.pix = pix
        self.tags = tags


# --- json_to_python ----------------------------------------------------------
def test_json_to_python_flat_scalars():
    tags = FakeJSONObject(
        {"Width": 512, "Height": 512, "PixelType": "GRAY16", "PixelSizeUm": 0.65,
         "On": True}
    )
    out = start_mm.json_to_python(tags)
    assert out == {
        "Width": 512, "Height": 512, "PixelType": "GRAY16",
        "PixelSizeUm": 0.65, "On": True,
    }


def test_json_to_python_nested_object_and_array():
    tags = FakeJSONObject(
        {
            "scalar": 1,
            "nested": FakeJSONObject({"a": 1, "b": "two"}),
            "list": FakeJSONArray([1, 2, FakeJSONObject({"deep": 3})]),
        }
    )
    out = start_mm.json_to_python(tags)
    assert out == {
        "scalar": 1,
        "nested": {"a": 1, "b": "two"},
        "list": [1, 2, {"deep": 3}],
    }


def test_json_to_python_null_becomes_none():
    # org.json represents JSON null as a singleton instance of a private class
    # named `Null` (JSONObject.NULL). The converter detects it by class name and
    # maps it to Python None. (The real type is mmcorej.org.json.JSONObject$Null;
    # JPype surfaces its simple name as "Null".)
    class Null:  # noqa: N801 — deliberately matches the org.json class simple name
        def __str__(self):
            return "null"

    tags = FakeJSONObject({"missing": Null(), "present": 5})
    out = start_mm.json_to_python(tags)
    assert out == {"missing": None, "present": 5}


def test_json_to_python_empty():
    assert start_mm.json_to_python(FakeJSONObject({})) == {}


# --- tagged_image_to_numpy ---------------------------------------------------
def test_tagged_image_to_numpy_16bit_gray():
    raw = array.array("h", [0, 1000, -1, 32767, -32768, 12345])  # signed shorts
    tags = FakeJSONObject({"Width": 3, "Height": 2, "PixelType": "GRAY16"})
    img = FakeTaggedImage(raw, tags)
    arr = start_mm.tagged_image_to_numpy(img)
    assert arr.shape == (2, 3)
    assert arr.dtype == np.uint16
    assert arr[0, 1] == 1000
    assert arr[0, 2] == 65535  # -1 as unsigned


def test_tagged_image_to_numpy_8bit_gray():
    raw = array.array("b", range(-128, -128 + 6))
    tags = FakeJSONObject({"Width": 3, "Height": 2, "PixelType": "GRAY8"})
    arr = start_mm.tagged_image_to_numpy(FakeTaggedImage(raw, tags))
    assert arr.shape == (2, 3)
    assert arr.dtype == np.uint8
    assert arr[0, 0] == 128  # -128 as unsigned


def test_tagged_image_to_numpy_rgb_drops_alpha():
    # One pixel, packed BGRA = (B=10, G=20, R=30, A=0) -> RGB (30, 20, 10).
    raw = array.array("b", [10, 20, 30, 0])
    tags = FakeJSONObject({"Width": 1, "Height": 1, "PixelType": "RGB32"})
    arr = start_mm.tagged_image_to_numpy(FakeTaggedImage(raw, tags), copy=True)
    assert arr.shape == (1, 1, 3)
    assert list(arr[0, 0]) == [30, 20, 10]


def test_tagged_image_to_numpy_default_readonly_copy_writable():
    raw = array.array("b", [1, 2, 3, 4])
    tags = FakeJSONObject({"Width": 2, "Height": 2, "PixelType": "GRAY8"})
    img = FakeTaggedImage(raw, tags)
    ro = start_mm.tagged_image_to_numpy(img)
    assert not ro.flags.writeable
    rw = start_mm.tagged_image_to_numpy(img, copy=True)
    assert rw.flags.writeable


def test_tagged_image_metadata_helper():
    raw = array.array("b", [1, 2, 3, 4])
    tags = FakeJSONObject({"Width": 2, "Height": 2, "BitDepth": 8})
    md = start_mm.tagged_image_metadata(FakeTaggedImage(raw, tags))
    assert md == {"Width": 2, "Height": 2, "BitDepth": 8}


# --- round trip: TaggedImage -> numpy must match what numpy_to_image expects --
def test_tagged_numpy_roundtrip_shape_dtype_match():
    # tagged_image_to_numpy must produce arrays of exactly the dtype/shape that
    # _numpy_to_raw (the numpy->Image path) accepts, so the full
    # TaggedImage -> numpy -> Image trip is type-consistent.
    for code, pixeltype, w, h, dt in (
        ("b", "GRAY8", 3, 2, np.uint8),
        ("h", "GRAY16", 3, 2, np.uint16),
        ("f", "GRAY32", 2, 2, np.float32),
    ):
        raw = array.array(code, [0] * (w * h))
        tags = FakeJSONObject({"Width": w, "Height": h, "PixelType": pixeltype})
        arr = start_mm.tagged_image_to_numpy(FakeTaggedImage(raw, tags), copy=True)
        assert arr.shape == (h, w) and arr.dtype == dt
        # _numpy_to_raw accepts it (would raise TypeError/ValueError otherwise).
        flat, rw, rh, bpp, comps = start_mm._numpy_to_raw(arr)
        assert (rw, rh) == (w, h)

"""Snap, Otsu-threshold, and show as a 2-channel Micro-Manager dataset.

End-to-end example tying jPypeMM to scikit-image and back into Micro-Manager's
own display:

  1. snap an image from Micro-Manager (CMMCore),
  2. compute an Otsu threshold with scikit-image and build a binary mask,
  3. put the original (channel 0) and the mask (channel 1) into a RAM Datastore
     as a 2-channel image,
  4. open a Micro-Manager DataViewer (DisplayWindow) on that Datastore.

Run it interactively so the JVM/GUI stay alive and the viewer stays open::

    uv run python -i examples/snap_threshold_2channel.py

(or paste the body at the `imm.py` prompt, where ``studio``/``core`` already
exist). Running it non-interactively will snap, build the dataset, and open the
viewer, then hold the process open until you press Ctrl+C.
"""
from __future__ import annotations

import numpy as np
from skimage.filters import threshold_otsu

import start_mm


def snap_threshold_two_channel(studio, core):
    """Snap, Otsu-threshold, and return (datastore, display) as a 2-channel view.

    Channel 0 is the snapped image; channel 1 is the thresholded mask (same
    pixel type as the original so both channels display consistently).
    """
    data = studio.data()

    # 1. Snap one image straight from the Core as a numpy array (writable copy so
    #    we can derive the mask from it). snap_core handles shape/dtype.
    original = start_mm.snap_core(core, copy=True)

    # 2. Otsu threshold (scikit-image). threshold_otsu returns a scalar in the
    #    image's intensity range; the mask is everything at or above it.
    level = threshold_otsu(original)
    # Express the mask in the SAME dtype/scale as the original so the two channels
    # share a pixel type. For integer images use the dtype max as "on"; for float
    # use 1.0. (MM datastores expect a consistent pixel type across the dataset.)
    if np.issubdtype(original.dtype, np.integer):
        on_value = np.iinfo(original.dtype).max
    else:
        on_value = 1.0
    mask = np.where(original >= level, on_value, 0).astype(original.dtype)

    # 3. Build a 2-channel RAM Datastore.
    store = data.createRAMDatastore()

    # Summary metadata must be set BEFORE putImage. Declaring the channel names
    # (and intended dimensions) is what makes the viewer present this as a
    # 2-channel dataset with a channel slider. imageWidth/imageHeight take a boxed
    # Java Integer — start_mm.jint() does that conversion (a bare int won't match).
    height, width = original.shape[:2]
    intended = data.coordsBuilder().c(2).build()  # 2 channels, single z/t/p
    summary = (
        data.summaryMetadataBuilder()
        .channelNames("Original", "Otsu mask")
        .intendedDimensions(intended)
        .imageWidth(start_mm.jint(width))
        .imageHeight(start_mm.jint(height))
        .build()
    )
    store.setSummaryMetadata(summary)

    # Place each numpy array at its channel coordinate. numpy_to_image accepts a
    # Coords, so we just set the channel axis (c) per image.
    for channel, arr in enumerate((original, mask)):
        coords = data.coordsBuilder().c(channel).build()
        image = start_mm.numpy_to_image(data, arr, coords=coords)
        store.putImage(image)

    # Freeze: the dataset is complete and won't change. (Optional, but it tells
    # the viewer no more images are coming.)
    store.freeze()

    # 4. Open a Micro-Manager DataViewer (DisplayWindow) on the Datastore.
    display = studio.displays().createDisplay(store)

    print(
        f"2-channel dataset: shape={original.shape} dtype={original.dtype} "
        f"otsu_level={level:.4g}; channels=['Original', 'Otsu mask']"
    )
    return store, display


def main():
    # Launch MM (no user input needed); returns live studio + core.
    studio, core = start_mm.main(quiet=True, skip_intro=True)
    store, display = snap_threshold_two_channel(studio, core)
    return studio, core, store, display


if __name__ == "__main__":
    import sys
    import time

    start_mm._install_clean_exit()
    studio, core, store, display = main()
    if not sys.flags.interactive:
        print("\nDataViewer is open. Press Ctrl+C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Shutting down.")
        start_mm.quit_now()

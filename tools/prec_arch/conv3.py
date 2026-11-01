"""conv3: three strided convolutions over the raw window, then one dense layer.

Input (window, 3): the normalised x, y, z samples. The Bangle.js 2 has no
EXPAND_DIMS (used by Conv1D), so the window is reshaped to (window, 1, 3)
and Conv2D kernels of shape (k, 1) slide along time only. Ops: RESHAPE,
CONV_2D, FULLY_CONNECTED - all available on the watch. About 200 ms per
prediction on the watch for a 256 sample window.
"""
import keras
from keras import layers as L

DFT = 0           # raw samples, no DFT
DFT_ABS = None    # not used


def create_model(input_shape):
    """Model for windows of shape input_shape = (window, 3); outputs the speed (m/s)."""
    n, ch = input_shape
    return keras.Sequential([
        keras.Input(input_shape),
        L.Reshape((n, 1, ch)),                       # artificially 2-D
        L.Conv2D(8, (8, 1), strides=(2, 1), activation="relu"),
        L.Conv2D(16, (8, 1), strides=(2, 1), activation="relu"),
        L.Conv2D(16, (8, 1), strides=(2, 1), activation="relu"),
        L.Flatten(),
        L.Dense(1),
    ], name="conv3")

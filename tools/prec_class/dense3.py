"""dense3: three dense layers on the magnitudes of the first 20 DFT coefficients; classifies run, walk, still, stretch.

Input (20, 3): |DFT| of x, y, z, normalised per coefficient. Output: one
probability per class (softmax), in the order run, walk, still, stretch.
Uses only RESHAPE, FULLY_CONNECTED and SOFTMAX, which the Bangle.js 2 runs.
"""
import keras
from keras import layers as L

DFT = 20          # number of DFT coefficients per axis
DFT_ABS = True    # use their absolute values


def create_model(input_shape, n_classes):
    """Model for windows of shape input_shape = (20, 3); outputs n_classes probabilities."""
    return keras.Sequential([
        keras.Input(input_shape),
        L.Flatten(),
        L.Dense(64, activation="relu"),
        L.Dense(32, activation="relu"),
        L.Dense(n_classes, activation="softmax"),
    ], name="dense3")

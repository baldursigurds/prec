"""flat3: three dense layers on the magnitudes of the first 20 DFT coefficients.

Input (20, 3): |DFT| of x, y, z, normalised per coefficient. Uses only
RESHAPE and FULLY_CONNECTED (with fused ReLU), which the Bangle.js 2 runs.
"""
import keras
from keras import layers as L

DFT = 20          # number of DFT coefficients per axis
DFT_ABS = True    # use their absolute values


def create_model(input_shape):
    """Model for windows of shape input_shape = (20, 3); outputs the speed (m/s)."""
    return keras.Sequential([
        keras.Input(input_shape),
        L.Flatten(),
        L.Dense(64, activation="relu"),
        L.Dense(32, activation="relu"),
        L.Dense(1),
    ], name="flat3")

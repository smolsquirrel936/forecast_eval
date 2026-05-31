"""Smoke test for forecaster/toto2.py.

Loads the smallest Toto-2.0 checkpoint, builds a small synthetic history,
runs a single forecast through the wrapper, and prints the result.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

# Make the package importable whether run as a module
# (`python -m forecast_eval.test_toto2` from myPaper/) or directly
# (`python test_toto2.py` from inside forecast_eval/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from forecast_eval.forecaster.toto2 import Toto2Forecaster


def make_history(n: int = 512, start_price: float = 17500.0,
                 seed: int = 0) -> pd.DataFrame:
    """Random-walk price series shaped like the framework's tick history."""
    rng = np.random.default_rng(seed)
    steps = rng.choice([-1, 0, 1], size=n, p=[0.33, 0.34, 0.33])
    prices = start_price + steps.cumsum()
    base = pd.Timestamp("2024-01-02 09:00:00")
    timestamps = pd.date_range(base, periods=n, freq="1s")
    return pd.DataFrame({
        "timestamp": timestamps,
        "price": prices.astype(float),
        "volume": rng.integers(1, 10, size=n),
    })


def main() -> int:
    print("=== Toto2Forecaster smoke test ===")
    history = make_history(n=512)
    print(f"history: {len(history)} rows, "
          f"price range [{history['price'].min():.1f}, "
          f"{history['price'].max():.1f}]")

    fc = Toto2Forecaster(
        warmup_bars=256,
        forecast_stride_bars=32,
        forecast_horizon_bars=16,
        context_length=256,
        checkpoint="Datadog/Toto-2.0-313m",   # medium checkpoint
        device="auto",
        bar_freq=None,                       # use raw history directly
        signal_step="last",
    )
    print(f"checkpoint: {fc.checkpoint}")
    print("loading model + running one forecast (lazy import of torch/toto2)...")

    try:
        forecast = fc.forecast(history)
    except ImportError as e:
        print(f"\n[IMPORT ERROR] {e}")
        print("Install torch + the toto2 package (paper/toto/toto2) and retry.")
        return 2
    except Exception as e:
        print(f"\n[RUNTIME ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    p = forecast.payload
    print("\n=== Forecast ===")
    print(f"  timestamp:        {forecast.timestamp}")
    print(f"  horizon_bars:     {forecast.horizon_bars}")
    print(f"  last_price:       {p['last_price']:.4f}")
    print(f"  predicted_price:  {p['predicted_price']:.4f}")
    print(f"  predicted_return: {p['predicted_return']:+.6f}")
    median = p["median_path"]
    print(f"  median_path len:  {len(median)}")
    print(f"  median[:5]:       {[round(v, 2) for v in median[:5]]}")
    print(f"  median[-5:]:      {[round(v, 2) for v in median[-5:]]}")

    assert len(median) == fc.forecast_horizon_bars, "median path length mismatch"
    assert forecast.timestamp == history["timestamp"].iloc[-1], \
        "forecast.timestamp must equal the last history timestamp"

    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

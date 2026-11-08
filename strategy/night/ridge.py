"""NIGHT's price-ridge forecaster (strategy_test_1108_ridge).

Predicts the next ``target_bars`` log return as a linear function of the tape's
own past 60/300/1140/3420-bar log returns and its 1140-bar realised volatility:
the "price ridge" of the 2026-11-08 forecast screen. The weights come from
``fit_ridge`` over a training window that ends before the evaluation, are saved
as JSON, and stay frozen for the whole run, so a forecast at bar t reads only
bars <= t. It stands in for Toto2 under the unchanged emitter and trader. The
payload carries no ``prob_up`` and no ``median_path``, so the emitter's
probability gate and path check are inert, as for ``reversal.py``.

Kept out of ``forecaster.py`` on purpose: that file is what ``forecaster_sha``
hashes, and the sha is part of the Toto2 forecast-cache key.
"""
import json
from typing import Any, Dict, List, Sequence, Union

import numpy as np
import pandas as pd

from ...events import Forecast
from ...forecaster.base import Forecaster

RETURN_BARS = (60, 300, 1140, 3420)
VOL_BARS = 1140
FEATURES = [f"ret{n}" for n in RETURN_BARS] + [f"vol{VOL_BARS}"]
LOOKBACK_BARS = max(max(RETURN_BARS), VOL_BARS)


def features_at(logp: np.ndarray, b: int) -> np.ndarray:
    """The five features at index ``b`` of a log-price array (needs b >= LOOKBACK_BARS)."""
    # Tech: N-bar log returns ending at b, and the root mean square of the last
    #       VOL_BARS one-bar log returns.
    # Why:  one helper serves fitting, forecast() and the batch path, so the
    #       three cannot drift apart (batch == single is test-covered exactly).
    rets = [logp[b] - logp[b - n] for n in RETURN_BARS]
    vol = float(np.sqrt(np.mean(np.diff(logp[b - VOL_BARS:b + 1]) ** 2)))
    return np.array(rets + [vol])


def fit_ridge(timestamps: Sequence[Any], prices: Sequence[float], *, fit_end: Any,
              target_bars: int, step: int = 10, lam: float = 10.0) -> Dict[str, Any]:
    """Fit the ridge on every ``step``-th bar whose target window ends by ``fit_end``.

    Returns the JSON-ready coefficient dict ``RidgeForecaster`` takes.
    """
    # Tech: sample bars b = LOOKBACK_BARS, +step, ... whose target bar
    #       b + target_bars is stamped at or before fit_end; standardise the
    #       features on that sample; solve (A'A + lam I) w = A'y with an
    #       intercept column, as screen_tsfm.py's ridge does.
    # Why:  requiring the TARGET bar (not just b) to sit inside the window keeps
    #       every post-fit_end price out of the weights, so a run evaluated after
    #       fit_end is out of sample.
    ts = pd.to_datetime(pd.Series(timestamps)).to_numpy()
    logp = np.log(np.asarray(prices, dtype=float))
    end = np.datetime64(pd.Timestamp(fit_end))
    rows = [b for b in range(LOOKBACK_BARS, len(logp) - target_bars, step) if ts[b + target_bars] <= end]
    if len(rows) < 100:
        raise ValueError(f"only {len(rows)} fitting rows end by {fit_end}; need at least 100")
    X = np.array([features_at(logp, b) for b in rows])
    y = np.array([logp[b + target_bars] - logp[b] for b in rows])
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    A = np.column_stack([(X - mu) / sd, np.ones(len(rows))])
    w = np.linalg.solve(A.T @ A + lam * np.eye(A.shape[1]), A.T @ y)
    return {"features": FEATURES, "mu": mu.tolist(), "sd": sd.tolist(), "weights": w[:-1].tolist(),
            "intercept": float(w[-1]), "target_bars": int(target_bars), "fit_end": str(pd.Timestamp(fit_end)),
            "fit_rows": len(rows), "first_fit_bar": str(pd.Timestamp(ts[rows[0]])),
            "step": int(step), "lam": float(lam)}


def load_coefficients(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class RidgeForecaster(Forecaster):
    def __init__(self, *, warmup_bars: int, forecast_stride_bars: int, coefficients: Dict[str, Any]):
        # Tech: store the scheduling attributes and the frozen weights; refuse a
        #       coefficient set fitted on other features.
        # Why:  the run loop schedules this exactly like Toto2 (SPEC §3). The
        #       horizon is the ridge's own target, so the report's direction hit
        #       is measured at the horizon the weights were fitted for.
        if coefficients.get("features") != FEATURES:
            raise ValueError(f"coefficients are for features {coefficients.get('features')!r}, expected {FEATURES!r}")
        self.warmup_bars = warmup_bars
        self.forecast_stride_bars = forecast_stride_bars
        self.forecast_horizon_bars = int(coefficients["target_bars"])
        self.coefficients = coefficients
        self._mu = np.asarray(coefficients["mu"], dtype=float)
        self._sd = np.asarray(coefficients["sd"], dtype=float)
        self._w = np.asarray(coefficients["weights"], dtype=float)
        self._b = float(coefficients["intercept"])

    def _make(self, timestamp: Any, logp: np.ndarray, b: int) -> Forecast:
        # Tech: the forecast at index b of a log-price array; zero predicted
        #       return when there are fewer than LOOKBACK_BARS bars before b.
        # Why:  zero is inside every band, so a too-short history holds rather
        #       than trading on partial features.
        last = float(np.exp(logp[b]))
        if b < LOOKBACK_BARS:
            r = 0.0
        else:
            r = float(np.expm1(((features_at(logp, b) - self._mu) / self._sd) @ self._w + self._b))
        return Forecast(timestamp=timestamp, horizon_bars=self.forecast_horizon_bars,
                        payload={"predicted_price": last * (1.0 + r), "predicted_return": r, "last_price": last})

    def forecast(self, history_df: pd.DataFrame) -> Forecast:
        # Tech: read the trailing LOOKBACK_BARS + 1 prices of the history-through-t frame.
        # Why:  only rows <= t are read, the look-ahead contract (SPEC §6).
        prices = history_df["price"].to_numpy(dtype=float)[-(LOOKBACK_BARS + 1):]
        logp = np.log(prices)
        return self._make(history_df["timestamp"].iloc[-1], logp, len(logp) - 1)

    def forecast_series_batch(self, timestamps: Sequence[Any], prices: Sequence[float],
                              boundaries: Sequence[int], *, batch_size: Union[int, str] = "auto",
                              progress: bool = False) -> List[Forecast]:
        # Tech: take logs once and evaluate the features at each boundary b,
        #       reading only indices <= b.
        # Why:  the base fallback rebuilds the whole history frame per boundary,
        #       O(n) each -- hours on a 1.9M-bar tape. Same values as forecast().
        logp = np.log(np.asarray(prices, dtype=float))
        return [self._make(timestamps[b], logp, b) for b in boundaries]

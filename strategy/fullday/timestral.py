"""Timestral-209M forecaster, FULL-DAY package's own copy.

PER-SESSION SEAM (STRATEGY_SPLIT_PLAN S1). Built on branch
``forecaster_eval_1116`` to evaluate replacing the incumbent Toto-2.0-1B
with Mistral's Timestral-209M: the model_eval bake-off found it the
strongest candidate (best pinball in every grid cell, the only
majority-month MASE<1 family, ~2.4x faster than toto2-1B), and the
signal-level study found its gated 5-minute stream is a mean-reversion
fade whose edge lives in the first minute after the signal bar.

This is a from-scratch adapter (not a copy of ``forecaster/toto2.py``)
because the library APIs differ, but it reproduces the incumbent's
*contract* exactly: the same ``Forecast`` payload keys
(``predicted_return``, ``prob_up``, ``median_path``, ``last_price``, the
quantile fan) so the copied ``ThresholdEmitter`` and ``Trader`` are
untouched, and the same ``forecast_series_batch`` shape so
``--precompute`` and ``--forecast-cache`` work unchanged.

Timestral emits a 21-level quantile ladder; the nine deciles sit at
indices [2, 4, ..., 18] (verified against the package's live
``forecast.quantile_levels`` on 2026-11-16 — re-verify if a checkpoint
ever ships a different ladder). ``prob_up`` uses the same decile-CDF
interpolation as the incumbent, so the emitter's conviction read is
comparable across both forecasters.
"""
from typing import Any, List, Literal, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from ...events import Forecast
from ...forecaster.base import Forecaster

# Tech: the canonical nine-decile ladder and the median row, mirroring the
#       incumbent's constants.
# Why:  the emitter's prob_up gate and the research tooling (model_eval) both
#       speak deciles; reducing Timestral's 21 levels to these nine keeps
#       every downstream consumer forecaster-agnostic.
DECILE_LEVELS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
MEDIAN_IDX = 4  # index of Q0.5 in the 9-decile fan
# Tech: where each decile sits inside Timestral's 21-level output axis.
# Why:  see the module docstring — verified against the live ladder.
DECILE_IDX_21 = [2, 4, 6, 8, 10, 12, 14, 16, 18]


class TimestralForecaster(Forecaster):
    def __init__(
        self,
        *,
        warmup_bars: int,
        forecast_stride_bars: int,
        forecast_horizon_bars: int,
        context_length: int = 2048,
        checkpoint: str = "mistralai/Timestral-209M",
        device: str = "auto",
        signal_step: Literal["last", "first", "mean"] = "last",
    ):
        """
        Parameters
        ----------
        warmup_bars, forecast_stride_bars, forecast_horizon_bars
            Standard Forecaster attributes (SPEC §3), in bars/ticks as the
            run loop sees them (``bar_freq`` is not supported — the fullday
            tape is already 1-minute bars).
        context_length
            Trailing bars fed to the model (2048 = the bake-off's best
            cell; Timestral is trained up to 32768).
        checkpoint
            HuggingFace model id, ``mistralai/Timestral-209M``.
        device
            ``"cuda"``, ``"cpu"``, or ``"auto"``.
        signal_step
            Which horizon step summarizes into ``predicted_price`` — same
            semantics as the incumbent (the emitter reads the end-of-horizon
            return, i.e. ``"last"``).
        """
        # Tech: store every config knob; the model/torch handles stay None
        #       until the first forecast.
        # Why:  the lazy load keeps this package importable (isolation test,
        #       --help, wiring) on a machine with no GPU/timestral, matching
        #       the incumbent's convention.
        self.warmup_bars = warmup_bars
        self.forecast_stride_bars = forecast_stride_bars
        self.forecast_horizon_bars = forecast_horizon_bars
        self.context_length = context_length
        self.checkpoint = checkpoint
        self.device = device
        self.signal_step = signal_step
        self._model: Any = None
        self._torch: Any = None

    def _load_model(self) -> Any:
        # Tech: import torch + timestral lazily, disable TF32, resolve
        #       "auto", and load the checkpoint.
        # Why:  torch/timestral are optional for the rest of the framework;
        #       TF32's reduced mantissa makes cuBLAS pick batch-shape
        #       dependent kernels, so disabling it keeps the batched
        #       precompute path bit-identical to a sequential forecast (the
        #       same rationale as the incumbent's wrapper — a cached run
        #       must reproduce a fresh one).
        import torch  # noqa: WPS433 (lazy import)
        from timestral import TimestralForecaster as _Runner  # type: ignore[import-not-found]

        self._torch = torch
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        dev = self.device
        if dev == "auto":
            dev = "cuda" if torch.cuda.is_available() else "cpu"
        m = _Runner.from_pretrained(self.checkpoint, device=dev)
        self._model = m
        return m

    def _context_array(self, history_df: pd.DataFrame) -> np.ndarray:
        # Tech: take the trailing context_length prices as a float32 array.
        # Why:  Timestral takes a plain (T,) numpy context; float32 matches
        #       the model weights so no per-call cast happens on device.
        prices = history_df["price"].astype(float).to_numpy()
        if len(prices) > self.context_length:
            prices = prices[-self.context_length:]
        if len(prices) < 2:
            raise ValueError(
                f"TimestralForecaster: context of {len(prices)} bars is "
                f"too short to forecast"
            )
        return np.ascontiguousarray(prices, dtype=np.float32)

    def _fans_from_quantiles(self, q: np.ndarray) -> np.ndarray:
        # Tech: drop the variate axis then pick the nine decile rows,
        #       returning (n, 9, H).
        # Why:  the canonical fan layout the payload builder expects — the
        #       two-step selection avoids numpy's non-adjacent advanced
        #       indexing permuting the axes.
        dec = q[:, 0][:, :, DECILE_IDX_21]  # (n, H, 9)
        return np.transpose(dec, (0, 2, 1))

    def forecast(self, history_df: pd.DataFrame) -> Forecast:
        # Tech: load on first use, build the context, run one forecast and
        #       hand the fan to the shared payload builder.
        # Why:  this is the per-tick path the run loop uses without
        #       --precompute; identical inputs/outputs to the batched path
        #       (same _make_forecast), so cache hits and fresh runs agree.
        if self._model is None:
            self._model = self._load_model()
        ctx = self._context_array(history_df)
        out = self._model.forecast(ctx, prediction_length=self.forecast_horizon_bars)
        q = out.quantiles
        if hasattr(q, "detach"):
            q = q.detach().cpu().numpy()
        fan = self._fans_from_quantiles(np.asarray(q)[None])[0]  # (9, H)
        return self._make_forecast(
            fan, float(ctx[-1]), history_df["timestamp"].iloc[-1]
        )

    def forecast_series_batch(
        self,
        timestamps: Sequence[Any],
        prices: Sequence[float],
        boundaries: Sequence[int],
        *,
        batch_size: Union[int, str] = "auto",
        progress: bool = False,
    ) -> List[Forecast]:
        """Batch-forecast many boundaries at once (SPEC §6 speedup path).

        Same contract as the incumbent's: for each boundary ``b`` the model
        conditions on the trailing ``context_length`` of ``prices[:b+1]``
        and the Forecast is stamped at ``timestamps[b]``. Requires every
        boundary to have a full context window.
        """
        # Tech: materialise the (n, T) window matrix once, chunk it through
        #       the model at a fixed batch size, then build payloads from
        #       the collected fans.
        # Why:  one forward per chunk saturates the GPU where per-boundary
        #       forecast() calls would pay Python overhead ~500k times on a
        #       stride-5 fullday run; a fixed chunk of 512 at context 2048
        #       measured ~30 s per 100k windows on the RTX 5090 with tens of
        #       GB to spare, so no VRAM probing is needed (the incumbent
        #       probes because its 1B checkpoint at context 3008 is the
        #       binding constraint, not this 209M one).
        if self._model is None:
            self._model = self._load_model()
        boundaries = list(boundaries)
        if not boundaries:
            return []
        T = self.context_length
        if min(boundaries) + 1 < T:
            raise ValueError(
                f"boundary {min(boundaries)} has < {T} bars of history; "
                f"raise warmup to >= context_length or use sequential "
                f"forecast()."
            )
        arr = np.asarray(prices, dtype=np.float32)
        windows = np.stack([arr[b + 1 - T: b + 1] for b in boundaries])
        bs = 512 if batch_size == "auto" else int(batch_size)
        fans: List[np.ndarray] = []
        if progress:
            from tqdm.auto import tqdm
            print(f"precomputing {len(windows)} forecasts in batches "
                  f"(batch_size={bs})")
            bar = tqdm(total=len(windows), desc="precompute", unit="fcst")
        else:
            bar = None
        for i in range(0, len(windows), bs):
            chunk = np.ascontiguousarray(windows[i: i + bs])
            out = self._model.forecast(
                chunk, prediction_length=self.forecast_horizon_bars)
            q = out.quantiles
            if hasattr(q, "detach"):
                q = q.detach().cpu().numpy()
            fans.append(self._fans_from_quantiles(np.asarray(q)))
            if bar is not None:
                bar.update(len(chunk))
        if bar is not None:
            bar.close()
        fan = np.concatenate(fans, axis=0)  # (n, 9, H)
        return [
            self._make_forecast(fan[j], float(windows[j][-1]), timestamps[b])
            for j, b in enumerate(boundaries)
        ]

    @staticmethod
    def _prob_up(q_prices: np.ndarray, last_price: float) -> float:
        # Tech: read the predictive CDF at last_price by inverse-interpolating
        #       the (sorted decile price -> level) ladder, then P(up) = 1 - F(last).
        # Why:  identical maths to the incumbent's _prob_up so the emitter's
        #       conviction gate means the same thing under either forecaster;
        #       np.interp clamps outside [Q0.1, Q0.9], so prob_up lands in
        #       [0.1, 0.9] — a deliberately conservative decile read.
        cdf_at_last = float(np.interp(last_price, q_prices, DECILE_LEVELS))
        return 1.0 - cdf_at_last

    def _make_forecast(self, fan: np.ndarray, last_price: float,
                       timestamp: Any) -> Forecast:
        # Tech: reduce each decile row to the chosen horizon step, take the
        #       median row as the point forecast, and assemble the payload.
        # Why:  the payload keys mirror the incumbent's exactly (see the
        #       module docstring) — predicted_return drives the emitter's
        #       band, prob_up its conviction gate, median_path the path
        #       momentum veto, so the strategy code is forecaster-agnostic.
        def _reduce(path: np.ndarray) -> float:
            if self.signal_step == "first":
                return float(path[0])
            if self.signal_step == "mean":
                return float(path.mean())
            return float(path[-1])

        median = fan[MEDIAN_IDX]
        predicted_price = _reduce(median)
        predicted_return = (predicted_price - last_price) / last_price
        q_prices = np.array([_reduce(fan[i]) for i in range(fan.shape[0])])
        q_returns = (q_prices - last_price) / last_price
        prob_up = self._prob_up(q_prices, last_price)

        return Forecast(
            timestamp=timestamp,
            horizon_bars=self.forecast_horizon_bars,
            payload={
                "predicted_price": predicted_price,
                "predicted_return": predicted_return,
                "last_price": last_price,
                "median_path": [float(x) for x in median],
                "quantile_levels": list(DECILE_LEVELS),
                "quantile_prices": [float(x) for x in q_prices],
                "quantile_returns": [float(x) for x in q_returns],
                "prob_up": float(prob_up),
                "band_return_80": float(q_returns[-1] - q_returns[0]),
            },
        )

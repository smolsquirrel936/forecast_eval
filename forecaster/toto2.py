"""toto2 forecaster wrapper.

Wraps the Datadog Toto-2.0 time-series foundation model
(``paper/toto/toto2``) for use as a Forecaster. The notebooks in
``paper/myPaper/toto2_TXF_*.ipynb`` use it on OHLC minute bars; this
wrapper runs it **univariate** on the close-price series derived from
the framework's tick history, optionally resampled into bars.

Inference call shape (matches the notebooks):

    qs = model.forecast(
        {"target": t,           # (B, C, T) float32
         "target_mask": m,      # bool, same shape
         "series_ids": sid},    # (B, C) long zeros
        horizon=H,
    )
    # qs: (Q=9 quantiles, B, C, H)  — MEDIAN_IDX = 4 (Q0.5)

Notes:
  * ``torch`` and ``toto2`` are imported lazily on first forecast, so
    the rest of the framework can be used without those deps installed.
  * Context length is truncated to a multiple of the model's
    ``patch_size`` (required by ``PatchedCausalStdScaler``).
  * Multivariate OHLC support is a straightforward extension: change
    ``_prepare_context`` to return a ``(C, T)`` array and set the
    ``series_ids`` shape accordingly.
"""
from typing import Any, Literal, Optional

import pandas as pd

from ..events import Forecast
from .base import Forecaster

# Tech: index of the median (Q0.5) row in the model's 9-quantile output.
# Why:  toto2 returns a quantile fan; we trade off the median path, so this picks
#       the central forecast out of qs[Q, B, C, H].
MEDIAN_IDX = 4  # index of Q0.5 in the 9-quantile output


class Toto2Forecaster(Forecaster):
    def __init__(
        self,
        *,
        warmup_bars: int,
        forecast_stride_bars: int,
        forecast_horizon_bars: int,
        context_length: int = 3008,
        checkpoint: str = "Datadog/Toto-2.0-2.5B",
        device: str = "auto",
        bar_freq: Optional[str] = None,
        signal_step: Literal["last", "first", "mean"] = "last",
    ):
        """
        Parameters
        ----------
        warmup_bars, forecast_stride_bars, forecast_horizon_bars
            Standard Forecaster attributes (SPEC §3). Units are TICKS as
            seen by the run loop; if ``bar_freq`` is set, the model
            internally operates on aggregated bars.
        context_length
            Number of bars (post-aggregation) fed to the model. Matches
            CONTEXT_LENGTH in the notebooks. Truncated to a multiple of
            patch_size at inference time.
        checkpoint
            HuggingFace model id. One of:
            ``Datadog/Toto-2.0-{4m, 22m, 313m, 1B, 2.5B}``.
        device
            ``"cuda"``, ``"cpu"``, or ``"auto"``.
        bar_freq
            pandas resample frequency (e.g. ``"1min"``) to aggregate
            tick history into bars before forecasting. ``None`` feeds
            the raw history price series directly.
        signal_step
            Which horizon step to summarize into ``predicted_price`` for
            the SignalEmitter: ``"last"`` (end of horizon), ``"first"``
            (one step ahead), or ``"mean"`` (average across horizon).
        """
        # Tech: store every config knob, and initialize the model/torch/patch_size
        #       handles to None.
        # Why:  the heavy objects (model weights, torch) are loaded lazily on the
        #       first forecast (see _load_model), so constructing a Toto2Forecaster
        #       is cheap and doesn't require GPU/torch to even import.
        self.warmup_bars = warmup_bars
        self.forecast_stride_bars = forecast_stride_bars
        self.forecast_horizon_bars = forecast_horizon_bars
        self.context_length = context_length
        self.checkpoint = checkpoint
        self.device = device
        self.bar_freq = bar_freq
        self.signal_step = signal_step
        self._model: Any = None
        self._torch: Any = None
        self._patch_size: Optional[int] = None

    def _load_model(self) -> Any:
        # Tech: import torch + toto2 here, resolve "auto" to cuda/cpu, load the
        #       checkpoint onto the device in eval mode, and cache patch_size.
        # Why:  lazy import keeps torch/toto2 optional for the rest of the framework;
        #       eval() disables dropout/grad bookkeeping for inference; patch_size is
        #       read once because every forecast must align its context to it.
        import torch  # noqa: WPS433 (lazy import)
        from toto2 import Toto2Model  # type: ignore[import-not-found]

        self._torch = torch
        dev = self.device
        if dev == "auto":
            dev = "cuda" if torch.cuda.is_available() else "cpu"
        m = Toto2Model.from_pretrained(self.checkpoint, map_location=dev)
        m = m.to(dev).eval()
        self._patch_size = int(m.config.patch_size)
        return m

    def _prepare_context(self, history_df: pd.DataFrame) -> pd.Series:
        """Return a 1-D close-price series, length <= context_length."""
        # Tech: take the price column; if bar_freq is set, resample onto a datetime
        #       index taking the last price per bar and dropping empty bars.
        # Why:  the notebooks condition on fixed-frequency bars, not raw ticks; "last
        #       per bar" is the close, and dropping NaN bars avoids feeding gaps the
        #       model would otherwise propagate.
        prices = history_df["price"].astype(float)
        if self.bar_freq is not None:
            ts = pd.to_datetime(history_df["timestamp"])
            series = pd.Series(prices.values, index=ts)
            series = series.resample(self.bar_freq).last().dropna()
        else:
            series = prices
        # Tech: keep only the trailing context_length values and drop the index.
        # Why:  the model has a bounded context window; the most recent bars carry
        #       the signal, so we truncate from the front and hand back a clean 0..N
        #       series the tensor builder can reshape directly.
        if len(series) > self.context_length:
            series = series.iloc[-self.context_length:]
        return series.reset_index(drop=True)

    def forecast(self, history_df: pd.DataFrame) -> Forecast:
        # Tech: load the model on first use and grab cached torch/patch_size.
        # Why:  amortizes the one-time load; the assert documents that patch_size is
        #       always set once _model exists.
        if self._model is None:
            self._model = self._load_model()
        torch = self._torch
        assert self._patch_size is not None

        # Tech: build the context array, then trim it to the largest multiple of
        #       patch_size by dropping the oldest values; error if nothing remains.
        # Why:  toto2's PatchedCausalStdScaler requires the sequence length to divide
        #       patch_size; dropping from the front keeps the most recent context,
        #       and a context shorter than one patch can't be forecast at all.
        ctx = self._prepare_context(history_df).values
        aligned = (len(ctx) // self._patch_size) * self._patch_size
        if aligned == 0:
            raise ValueError(
                f"Toto2Forecaster: context length {len(ctx)} shorter than "
                f"patch_size {self._patch_size}"
            )
        ctx = ctx[-aligned:]

        # Tech: move data to the model's device and build the (B=1, C=1, T) target
        #       tensor, an all-True mask, and a zero series-id.
        # Why:  univariate close-price means one batch, one channel; the mask says
        #       every step is observed (no missing values), and series_ids=0 is the
        #       single-series convention from the notebooks.
        dev = next(self._model.parameters()).device
        t = torch.tensor(ctx, dtype=torch.float32, device=dev).reshape(1, 1, -1)
        m = torch.ones_like(t, dtype=torch.bool)
        sid = torch.zeros(1, 1, dtype=torch.long, device=dev)

        # Tech: run inference under no_grad and extract the median quantile's path,
        #       moving it back to CPU/numpy.
        # Why:  no_grad avoids building an autograd graph (faster, less memory) since
        #       we never backprop; qs[MEDIAN_IDX, 0, 0, :] is the single-series median
        #       forecast across the horizon.
        with torch.no_grad():
            qs = self._model.forecast(
                {"target": t, "target_mask": m, "series_ids": sid},
                horizon=self.forecast_horizon_bars,
            )
        # qs: (Q=9, B=1, C=1, H)
        median = qs[MEDIAN_IDX, 0, 0, :].detach().cpu().numpy()

        # Tech: pick the predicted price from the chosen horizon step (last/first/
        #       mean) and convert it to a return vs. the last context price.
        # Why:  signal_step lets the caller decide whether the trade thesis is the
        #       end-of-horizon level, the next step, or the average; emitters key off
        #       predicted_return, so we derive it here in one place.
        last_price = float(ctx[-1])
        if self.signal_step == "first":
            predicted_price = float(median[0])
        elif self.signal_step == "mean":
            predicted_price = float(median.mean())
        else:
            predicted_price = float(median[-1])
        predicted_return = (predicted_price - last_price) / last_price

        # Tech: return the Forecast stamped at the last history timestamp, carrying
        #       predicted price/return, last price, and the full median path.
        # Why:  stamping at the last observed row keeps it look-ahead-clean; shipping
        #       the median_path lets reports/diagnostics inspect the whole forecast,
        #       not just the scalar the emitter uses.
        return Forecast(
            timestamp=history_df["timestamp"].iloc[-1],
            horizon_bars=self.forecast_horizon_bars,
            payload={
                "predicted_price": predicted_price,
                "predicted_return": predicted_return,
                "last_price": last_price,
                "median_path": median.tolist(),
            },
        )

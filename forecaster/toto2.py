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
from typing import Any, List, Literal, Optional, Sequence, Union

import numpy as np
import pandas as pd

from ..events import Forecast
from .base import Forecaster

# Tech: the nine quantile levels toto2 emits along axis 0 of its forecast output,
#       and the index of the median (Q0.5) row.
# Why:  verified against toto/toto2 (configuration.py `quantiles` default and
#       model.py QuantileKnotsOutputHead knots, also exposed live as
#       model.output_head.knots): evenly-spaced deciles, so index 4 == Q0.5. These
#       levels are the x-axis of the predictive CDF used for prob_up / strength
#       below — if a checkpoint ever ships different knots, read them off the model
#       instead of trusting this constant.
QUANTILE_LEVELS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
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

        # Tech: disable TF32 for cuBLAS matmuls and cuDNN.
        # Why:  TF32's ~10-bit mantissa makes cuBLAS pick batch-shape-dependent GEMM
        #       kernels whose accumulation diverges ~0.1-0.2% between B=1 (sequential
        #       forecast) and a large batch (precompute). Full fp32 is batch-invariant,
        #       so the batched precompute path is bit-identical to the per-tick path
        #       (verified diff 0.0) — the backtest reproduces exactly regardless of
        #       batch size. Costs a little matmul speed; correctness wins here.
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
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

        # Tech: run inference under no_grad and extract the full quantile fan for
        #       the single series, moving it back to CPU/numpy as (Q=9, H).
        # Why:  no_grad avoids building an autograd graph (faster, less memory) since
        #       we never backprop; keeping all nine quantile rows (not just the
        #       median) is what lets _make_forecast derive directional probability and
        #       a predictive band for confidence-scaled trading (#1/#2/#7).
        with torch.no_grad():
            qs = self._model.forecast(
                {"target": t, "target_mask": m, "series_ids": sid},
                horizon=self.forecast_horizon_bars,
            )
        # qs: (Q=9, B=1, C=1, H)
        fan = qs[:, 0, 0, :].detach().cpu().numpy()

        # Tech: hand the quantile fan + last context price + timestamp to the shared
        #       Forecast builder.
        # Why:  the fan->Forecast conversion (signal_step, return, prob_up, payload)
        #       is identical for the single and batched paths, so it lives in one
        #       place (_make_forecast) to guarantee they agree.
        return self._make_forecast(
            fan, float(ctx[-1]), history_df["timestamp"].iloc[-1]
        )

    @staticmethod
    def _prob_up(q_prices: np.ndarray, last_price: float) -> float:
        # Tech: read the predictive CDF at last_price by inverse-interpolating the
        #       (sorted quantile price -> level) ladder, then P(up) = 1 - F(last).
        # Why:  the nine quantile prices are F^-1 at QUANTILE_LEVELS; interpolating
        #       price->level gives F(last_price) = P(next price <= last). np.interp
        #       clamps outside the [Q0.1, Q0.9] band, so prob_up lands in [0.1, 0.9]
        #       — a deliberately conservative read of conviction from deciles alone.
        cdf_at_last = float(np.interp(last_price, q_prices, QUANTILE_LEVELS))
        return 1.0 - cdf_at_last

    def _make_forecast(self, fan: np.ndarray, last_price: float,
                       timestamp: Any) -> Forecast:
        # Tech: reduce each quantile row to the chosen horizon step (last/first/mean)
        #       giving a 9-vector of quantile prices; the median row is the point
        #       forecast and predicted_return is its return vs. the last context price.
        # Why:  signal_step lets the caller decide whether the trade thesis is the
        #       end-of-horizon level, the next step, or the average; reducing every
        #       quantile the same way keeps the fan internally consistent at that step.
        def _reduce(path: np.ndarray) -> float:
            if self.signal_step == "first":
                return float(path[0])
            if self.signal_step == "mean":
                return float(path.mean())
            return float(path[-1])

        median = fan[MEDIAN_IDX]
        predicted_price = _reduce(median)
        predicted_return = (predicted_price - last_price) / last_price

        # Tech: build the 9 quantile prices/returns at the signal step, the upward
        #       probability from the CDF, and the 80% predictive band width (Q0.9-Q0.1)
        #       expressed as a return.
        # Why:  these are the distributional handles the emitter consumes — prob_up
        #       drives directional conviction (strength/gate, #1/#2) and band_return_80
        #       is a ready-made uncertainty proxy for vol-aware extensions. They are
        #       sorted because the model guarantees monotone quantiles along axis 0.
        q_prices = np.array([_reduce(fan[i]) for i in range(fan.shape[0])])
        q_returns = (q_prices - last_price) / last_price
        prob_up = self._prob_up(q_prices, last_price)
        band_return_80 = float(q_returns[-1] - q_returns[0])

        return Forecast(
            timestamp=timestamp,
            horizon_bars=self.forecast_horizon_bars,
            payload={
                "predicted_price": predicted_price,
                "predicted_return": predicted_return,
                "last_price": last_price,
                "median_path": [float(x) for x in median],
                # Distributional fields (new) — see _prob_up / docstring above.
                "quantile_levels": list(QUANTILE_LEVELS),
                "quantile_prices": [float(x) for x in q_prices],
                "quantile_returns": [float(x) for x in q_returns],
                "prob_up": float(prob_up),
                "band_return_80": band_return_80,
            },
        )

    def forecast_series_batch(
        self,
        timestamps: Sequence[Any],
        prices: Sequence[float],
        boundaries: Sequence[int],
        *,
        batch_size: Union[int, str] = "auto",
    ) -> List[Forecast]:
        """Batch-forecast many boundaries at once (SPEC §6 speedup path).

        For each index ``b`` in ``boundaries`` the model is conditioned on the
        trailing context of ``prices[:b+1]`` and the resulting Forecast is
        stamped at ``timestamps[b]`` — identical inputs/outputs to calling
        ``forecast()`` on the history-through-b frame, but every window is run
        on the GPU in batches instead of one at a time.

        Only ``bar_freq=None`` is supported: per-window resampling would make
        the contexts ragged and unbatchable. Requires every boundary to have
        at least one full ``context_length``-aligned window of history (the
        scripts enforce ``warmup >= context_length``).
        """
        # Tech: load the model on first use and reject the resample path.
        # Why:  batching needs uniform-length contexts; bar_freq aggregation yields
        #       a variable number of bars per window, so it must use sequential
        #       forecast() instead. Failing loudly beats silently wrong batches.
        if self._model is None:
            self._model = self._load_model()
        assert self._patch_size is not None
        if self.bar_freq is not None:
            raise NotImplementedError(
                "forecast_series_batch supports bar_freq=None only; per-window "
                "resampling yields ragged contexts — use sequential forecast()."
            )

        # Tech: compute the uniform context length T (context_length floored to a
        #       patch_size multiple) and verify every boundary has >= T history.
        # Why:  PatchedCausalStdScaler needs T divisible by patch_size; a single T
        #       across all windows is what makes them stack into one rectangular
        #       batch. The earliest boundary is the binding constraint.
        prices = np.asarray(prices, dtype=np.float32)
        boundaries = list(boundaries)
        if not boundaries:
            return []
        T = (self.context_length // self._patch_size) * self._patch_size
        if T == 0:
            raise ValueError(
                f"context_length {self.context_length} < patch_size "
                f"{self._patch_size}"
            )
        if min(boundaries) + 1 < T:
            raise ValueError(
                f"boundary {min(boundaries)} has < {T} bars of history; raise "
                f"warmup to >= context_length or use sequential forecast()."
            )

        # Tech: materialize all windows as a single (num, T) array of trailing
        #       context slices — pure numpy views, no per-window DataFrame.
        # Why:  building one DataFrame per window is exactly the O(n) overhead this
        #       path exists to avoid; numpy slicing the fixed price array is ~free.
        windows = np.stack([prices[b + 1 - T: b + 1] for b in boundaries])

        # Tech: resolve the batch size (probe VRAM when "auto") then run each chunk
        #       through the model, collecting the median path per row.
        # Why:  one big forward would blow VRAM on the activation side; chunking by a
        #       VRAM-aware batch size saturates the GPU without OOM, and
        #       _forecast_chunk recovers from an over-estimate by splitting.
        bs = self._auto_batch_size(T) if batch_size == "auto" else int(batch_size)
        fans: List[np.ndarray] = []
        for i in range(0, len(windows), bs):
            fans.append(self._forecast_chunk(windows[i: i + bs], T))
        fan = np.concatenate(fans, axis=0)  # (num, Q=9, H)

        # Tech: turn each window's quantile fan into a Forecast stamped at its boundary.
        # Why:  same _make_forecast as the single path → byte-identical payload
        #       semantics (median + distributional fields); last_price is the window's
        #       final close.
        return [
            self._make_forecast(fan[j], float(windows[j][-1]), timestamps[b])
            for j, b in enumerate(boundaries)
        ]

    def _forecast_chunk(self, windows_chunk: np.ndarray, T: int) -> np.ndarray:
        # Tech: run one (chunk, 1, T) batch through the model and return the full
        #       quantile fan as a (chunk, Q=9, H) numpy array; on CUDA OOM, recursively
        #       split the chunk in half and retry until it fits (or a single row
        #       still OOMs, which re-raises).
        # Why:  the auto batch size is an estimate; halving-on-OOM makes an optimistic
        #       guess self-correcting instead of crashing the whole run. Keeping all
        #       nine quantiles (not just the median) feeds the distributional payload.
        torch = self._torch
        dev = next(self._model.parameters()).device
        try:
            t = torch.tensor(
                windows_chunk, dtype=torch.float32, device=dev
            ).reshape(len(windows_chunk), 1, T)
            m = torch.ones_like(t, dtype=torch.bool)
            sid = torch.zeros(len(windows_chunk), 1, dtype=torch.long, device=dev)
            with torch.no_grad():
                qs = self._model.forecast(
                    {"target": t, "target_mask": m, "series_ids": sid},
                    horizon=self.forecast_horizon_bars,
                )
            # qs: (Q=9, chunk, C=1, H) -> (chunk, Q=9, H), single channel dropped.
            return qs[:, :, 0, :].permute(1, 0, 2).detach().cpu().numpy()
        except torch.cuda.OutOfMemoryError:
            if len(windows_chunk) == 1:
                raise
            torch.cuda.empty_cache()
            mid = len(windows_chunk) // 2
            first = self._forecast_chunk(windows_chunk[:mid], T)
            second = self._forecast_chunk(windows_chunk[mid:], T)
            return np.concatenate([first, second], axis=0)

    def _auto_batch_size(self, T: int, *, safety: float = 0.8,
                         cap: int = 1024) -> int:
        # Tech: preflight that runs before every batched backtest — measure the
        #       activation memory of a B=1 and B=8 forward, read free VRAM, and solve
        #       batch = floor((free*safety - base_act) / per_sample), clamped to
        #       [1, cap]. Any probe failure falls back to a safe small batch.
        # Why:  hard-coding a batch size either wastes the 5090's headroom or OOMs on
        #       a busy GPU; measuring on the actual device/model adapts to whatever
        #       VRAM is free at launch. The _forecast_chunk OOM-split is the backstop.
        torch = self._torch
        if not torch.cuda.is_available():
            return 32  # CPU: no VRAM probe; a modest fixed batch is fine.
        dev = next(self._model.parameters()).device
        idx = dev.index if dev.index is not None else torch.cuda.current_device()

        def _activation_peak(b: int) -> int:
            # Peak *transient* memory of a B-row forward = peak allocated minus the
            # weights already resident before the forward.
            torch.cuda.synchronize(idx)
            torch.cuda.reset_peak_memory_stats(idx)
            before = torch.cuda.memory_allocated(idx)
            t = torch.zeros(b, 1, T, dtype=torch.float32, device=dev)
            m = torch.ones_like(t, dtype=torch.bool)
            sid = torch.zeros(b, 1, dtype=torch.long, device=dev)
            with torch.no_grad():
                self._model.forecast(
                    {"target": t, "target_mask": m, "series_ids": sid},
                    horizon=self.forecast_horizon_bars,
                )
            torch.cuda.synchronize(idx)
            peak = torch.cuda.max_memory_allocated(idx)
            del t, m, sid
            return peak - before

        try:
            torch.cuda.empty_cache()
            act1 = _activation_peak(1)
            act8 = _activation_peak(8)
            per_sample = max(1, (act8 - act1) // 7)
            base_act = max(0, act1 - per_sample)
            torch.cuda.empty_cache()
            free, _total = torch.cuda.mem_get_info(idx)
            budget = free * safety - base_act
            n = int(budget // per_sample)
            n = max(1, min(cap, n))
            print(
                f"[toto2] auto batch-size: {n} (free {free / 2**30:.1f}GB, "
                f"~{per_sample / 2**20:.0f}MB/sample, base {base_act / 2**20:.0f}MB)"
            )
            return n
        except Exception as exc:  # noqa: BLE001 — any probe failure is non-fatal
            print(f"[toto2] auto batch-size probe failed ({exc}); using 8")
            return 8

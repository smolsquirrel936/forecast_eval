"""An on-disk forecast cache shared across backtest runs (``real_data --forecast-cache``).

A forecast at bar ``b`` is a function of the price history through ``b`` and the
forecaster's configuration — never of trading state, stride, warmup or any
emitter/trader knob. So a strategy search that only moves those knobs re-asks
the GPU the same questions every run. This module stores each answer once, **per
bar**, and hands a run exactly the bars it needs: a stride-15 run reads a
subset of what a stride-5 run wrote, and an emitter-only change computes nothing.

**What makes two runs share an entry** (the cache key, all of it in ``meta.json``):
the forecaster's class and its forecast knobs (checkpoint, context, horizon,
``bar_freq``, ``signal_step``), the package's ``forecaster_sha``, a hash of the
in-session tape the run loop sees, and the runtime that computed it — GPU name,
torch / CUDA / toto-2 versions. The runtime is in the key because forecasts are
reproducible within one environment, not across them: two venvs on the same
RTX 5090 can differ in the last bits, which is enough to move a threshold call.

**Entries are never overwritten.** A run computes only the bars the cache lacks
and appends them as a new shard, so a row scored on the cache stays reproducible
by any later run on the same key.
"""
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .environment import Environment
from .events import Forecast

SCHEMA = 1


def runtime_identity() -> Dict[str, str]:
    """The environment a forecast was computed in: GPU and library versions."""
    # Tech: read torch's own version/CUDA/device and toto-2's installed version.
    # Why:  same card, different venv is the case this guards (the host and the
    #       VM). A torch or CUDA upgrade changes kernels, and so the floats.
    from importlib import metadata

    import torch

    def _ver(dist: str) -> str:
        try:
            return metadata.version(dist)
        except metadata.PackageNotFoundError:
            return "absent"

    gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    return {"gpu": gpu, "torch": torch.__version__,
            "cuda": str(torch.version.cuda), "toto-2": _ver("toto-2")}


def tape_digest(timestamps: Sequence[Any], prices: np.ndarray) -> str:
    """sha256 over the in-session timestamps (ns) and prices the loop consumes."""
    # Why: content, not path — the host reads the tape as D:\..., the VM as a UNC
    #      path, and a rebuilt tape at the same path must not hit old entries.
    h = hashlib.sha256()
    h.update(pd.DatetimeIndex(timestamps).asi8.astype(np.int64).tobytes())
    h.update(np.asarray(prices, dtype=np.float64).tobytes())
    return h.hexdigest()


def cache_key(forecaster: Any, *, forecaster_sha: Optional[str], tape: str,
              runtime: Dict[str, str]) -> Tuple[str, Dict[str, Any]]:
    """``(hex key, the fields it hashes)`` for this forecaster on this tape."""
    # Tech: hash a canonical JSON of every field a forecast depends on.
    # Why:  stride and warmup are deliberately absent — they choose WHICH bars
    #       are forecast, not what a bar's forecast is, so leaving them out is
    #       what lets runs at different strides share entries.
    fields = {
        "schema": SCHEMA,
        "forecaster": type(forecaster).__module__ + "." + type(forecaster).__qualname__,
        "forecaster_sha": forecaster_sha,
        "checkpoint": getattr(forecaster, "checkpoint", None),
        "context_length": getattr(forecaster, "context_length", None),
        "horizon_bars": getattr(forecaster, "forecast_horizon_bars", None),
        "bar_freq": getattr(forecaster, "bar_freq", None),
        "signal_step": getattr(forecaster, "signal_step", None),
        "tape": tape,
        "runtime": runtime,
    }
    blob = json.dumps(fields, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest(), fields


def boundaries_for(n: int, warmup: int, stride: int) -> List[int]:
    # Why: the exact rule `run.precompute_forecasts` and `run_backtest` use; a
    #      different set would leave the run loop a KeyError mid-replay.
    return [b for b in range(warmup, n) if (b - warmup) % stride == 0]


def _encode(bars: Sequence[int], forecasts: Sequence[Forecast]) -> Tuple[Dict[str, np.ndarray], List[str]]:
    # Tech: scalar payload fields become 1-D float64 columns, list fields 2-D
    #       float64 blocks; the field order is returned for `meta.json`.
    # Why:  float64 is exact for the Python floats Toto2 emits, so a decoded
    #       payload compares equal to the original. Arrays rather than pickle:
    #       a cache on a shared folder should not be able to run code on load.
    order = list(forecasts[0].payload.keys())
    arrays: Dict[str, np.ndarray] = {"bar_idx": np.asarray(bars, dtype=np.int64)}
    for k in order:
        col = [f.payload[k] for f in forecasts]
        if isinstance(col[0], (list, tuple)):
            arrays["v:" + k] = np.asarray(col, dtype=np.float64)
        elif isinstance(col[0], (int, float, np.floating, np.integer)):
            arrays["s:" + k] = np.asarray(col, dtype=np.float64)
        else:
            raise TypeError(f"payload field {k!r} is {type(col[0]).__name__}; "
                            f"the cache stores numbers and lists of numbers only")
    return arrays, order


def _decode_row(arrays: Dict[str, np.ndarray], i: int, order: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in order:
        if "s:" + k in arrays:
            out[k] = float(arrays["s:" + k][i])
        else:
            out[k] = [float(x) for x in arrays["v:" + k][i]]
    return out


def _write_shard(folder: Path, arrays: Dict[str, np.ndarray]) -> Path:
    # Tech: write to a temp name in the same folder, then rename into place.
    # Why:  a rename is atomic on one volume, so a reader on the other machine
    #       never sees a half-written shard; distinct names let the host and the
    #       VM append concurrently without a lock.
    name = f"part-{time.time_ns()}-{os.getpid()}.npz"
    tmp = folder / (name + ".tmp")
    with open(tmp, "wb") as fh:
        np.savez(fh, **arrays)
    final = folder / name
    os.replace(tmp, final)
    return final


def _read_shards(folder: Path) -> Dict[str, np.ndarray]:
    # Tech: concatenate every shard in name order; the first copy of a bar wins.
    # Why:  two writers racing on the same bars both append; keeping the earliest
    #       makes every reader pick the same copy, and a run that already scored
    #       on it keeps scoring on it.
    shards = sorted(folder.glob("part-*.npz"))
    if not shards:
        return {}
    parts = []
    for p in shards:
        with np.load(p, allow_pickle=False) as z:
            parts.append({k: z[k] for k in z.files})
    merged = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    _, first = np.unique(merged["bar_idx"], return_index=True)
    first.sort()
    return {k: v[first] for k, v in merged.items()}


def load_or_compute(forecaster: Any, ticks: pd.DataFrame, cache_dir: Path, *,
                    forecaster_sha: Optional[str] = None,
                    batch_size: Any = "auto", progress: bool = False,
                    runtime: Optional[Dict[str, str]] = None,
                    ) -> Tuple[Dict[int, Forecast], Dict[str, Any]]:
    """Every forecast the run loop will request, from the cache plus the GPU.

    Returns ``({bar_idx: Forecast}, stats)`` — the same mapping
    ``run.precompute_forecasts`` returns, ready for
    ``run_backtest(forecasts_cache=...)``.
    """
    # Tech: rebuild the in-session stream exactly as `precompute_forecasts` does
    #       and derive this run's boundaries from it.
    # Why:  bar_idx counts in-session bars only; keying on raw rows would hand
    #       the loop another bar's forecast (run.py's precompute comment).
    rows = list(Environment(ticks).stream())
    timestamps = [r.timestamp for r in rows]
    prices = np.asarray([r.price for r in rows], dtype=float)
    bars = boundaries_for(len(rows), forecaster.warmup_bars,
                          forecaster.forecast_stride_bars)

    key, fields = cache_key(forecaster, forecaster_sha=forecaster_sha,
                            tape=tape_digest(timestamps, prices),
                            runtime=runtime if runtime is not None else runtime_identity())
    folder = Path(cache_dir) / key[:16]
    folder.mkdir(parents=True, exist_ok=True)
    meta_path = folder / "meta.json"

    # Tech: on first use write the key fields; afterwards require they match.
    # Why:  a 16-hex prefix collision is implausible but would be silent; the
    #       full key in meta.json turns it into an error.
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("key") != key:
            raise RuntimeError(f"forecast cache {folder} holds key {meta.get('key')}, "
                               f"not {key}; refusing to mix streams")
    else:
        meta = {"key": key, "fields": fields, "payload_order": None,
                "created": pd.Timestamp.now().isoformat()}

    have = _read_shards(folder)
    cached = set(have["bar_idx"].tolist()) if have else set()
    missing = [b for b in bars if b not in cached]

    # Tech: forecast only the missing bars on the GPU and append them as a shard.
    # Why:  the whole point — an emitter-only iteration computes nothing, and a
    #       new stride pays only for the bars no earlier run asked for.
    if missing:
        fresh = forecaster.forecast_series_batch(timestamps, prices, missing,
                                                 batch_size=batch_size,
                                                 progress=progress)
        arrays, order = _encode(missing, fresh)
        if meta["payload_order"] is None:
            meta["payload_order"] = order
            tmp = meta_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            os.replace(tmp, meta_path)
        elif meta["payload_order"] != order:
            raise RuntimeError(f"payload fields {order} differ from the cache's "
                               f"{meta['payload_order']}")
        _write_shard(folder, arrays)
        have = _read_shards(folder)

    # Tech: decode only the requested bars, stamped with this run's timestamps.
    # Why:  a stride-15 run should not materialise a stride-5 cache's 3x more
    #       payload dicts; the timestamp comes from the tape the digest matched.
    pos = {int(b): i for i, b in enumerate(have["bar_idx"])} if have else {}
    order = meta["payload_order"] or []
    horizon = forecaster.forecast_horizon_bars
    out = {b: Forecast(timestamp=timestamps[b], horizon_bars=horizon,
                       payload=_decode_row(have, pos[b], order))
           for b in bars}
    stats = {"folder": str(folder), "key": key, "requested": len(bars),
             "hit": len(bars) - len(missing), "computed": len(missing)}
    return out, stats

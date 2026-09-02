# AUTORESEARCH_FREEZE.md — the freeze line, as it stands after the three-way split

**Status:** authoritative as of 2026-09-02, when STRATEGY_SPLIT_PLAN S1–S6 landed.

**Read this instead of `autoresearch_trading_plan.md`'s freeze table.** That file
lives at the repo root (`night/`), outside `forecast_eval/`, and is not editable
from here — so rather than leaving it silently stale, this file carries the
current line and names exactly what in it no longer holds. Everything else in
`autoresearch_trading_plan.md` — the loop mechanics, the Calmar objective, the
`n_trades >= 60` keep-gate, the evaluation command, the environment notes — is
unchanged and still governs.

> **If the two disagree, this file wins**, and the disagreement is a bug in that
> file, not a decision to re-litigate. `tests/test_strategy_isolation.py` is the
> executable version of the table below; where prose and test disagree, the test
> wins over both.

---

## 1. What changed, in one paragraph

The strategy layer used to be one set of files shared by every experiment. It is
now **three independent packages** — `strategy/day/`, `strategy/night/`,
`strategy/fullday/` — each owning its own SignalEmitter, Trader and Forecaster,
so a search for one session cannot move another. The old mutable surface
(`strategy/*.py` + `trader.py`) is therefore **frozen**, and the mutable surface
is what lives inside a session package. The Forecaster, previously frozen
outright, is now mutable *per session*.

---

## 2. The freeze line

| | Files |
|---|---|
| **FROZEN** — the world and the scoring | `data/loader.py` + the data path; `execution.py` (fill rules); fee charging + `portfolio.py`; `metrics.py`; `run.py` (the engine, including the new `make_trader` seam); `real_data.py` (the evaluation entry point, including the new `--strategy` selector) — **except** the single `write_all(...)` `ext=` argument, unchanged from before; `logging_io.py` + `reports.py`; `live/*`; **`strategy/base.py`** and **`forecaster/base.py`** (the interfaces the harness wires against); and the **shared implementations** `strategy/threshold.py`, `strategy/dummy.py`, `strategy/volmultitf.py`, `trader.py`, `forecaster/toto2.py` |
| **MUTABLE** — every decision about how to trade | **`strategy/<session>/` for your session only**, where `<session>` is `day`, `night` or `fullday`: `wiring.py` (`PARAMS` + `make_strategy`), `emitter.py`, `trader.py`, `forecaster.py`, and any new file you add inside that directory |

### The five shared implementations are frozen, but copyable

They are frozen **in place**, not off-limits. To change one, **copy it into your
session package and edit the copy** — that is what `emitter.py` and `trader.py`
already are (verbatim copies made at the split), and what `forecaster.py` becomes
the moment you need model internals rather than parameters.

Editing a shared file in place is the one action the split exists to prevent: it
moves all three sessions at once and silently invalidates the other two searches,
and it would not show up until a parity run disagreed weeks later.

`tests/test_strategy_isolation.py` enforces this by pinning a sha256 of each of
the seven frozen shared/interface files. If you edit one, that test fails and
tells you to copy instead. It also asserts that no session package imports from a
sibling — a copy that quietly imports `strategy.night.emitter` is shared code
wearing a package's name.

---

## 3. The two locked decisions that are superseded

### #1 — "Toto2 is frozen; the agent never touches the model"

**No longer true, by STRATEGY_SPLIT_PLAN D3.** Each package owns
`strategy/<session>/forecaster.py`, and may change the checkpoint, context
length, stride, horizon, or — by copying `forecaster/toto2.py` in — the model
wrapper itself.

`forecaster/toto2.py` and `forecaster/base.py` remain frozen as shared files.

### #5 — "Forecast schedule frozen ⇒ Toto2 outputs are deterministic"

**Half true, and the half that fails is the important half.** Forecasts are still
deterministic *within* a run, so `--precompute` still works and still batches the
whole span up front. What is gone is determinism *across* runs: once the
forecaster can change, two iterations need not share a forecast stream, so

- `--precompute` no longer amortises across iterations — an iteration that
  touches the forecaster pays a full GPU pass; and
- a Calmar improvement may come from the forecaster rather than the strategy,
  and the objective alone cannot tell you which.

**This is what `forecaster_sha` is for.** `results.tsv` carries a
`forecaster_sha` column: the 12-hex sha256 of the package's `forecaster.py`,
printed in `real_data.py --strategy`'s banner and recorded in `params.json`.

> **Rows sharing a `forecaster_sha` were scored on the same forecast stream and
> are directly comparable. A row whose sha differs from the incumbent's is not a
> like-for-like comparison, and its note must say so.**

The 20 rows predating the split read `pre-split`: they are comparable with each
other and not with what follows.

---

## 4. Running an experiment

```bash
# From the repo root (night/), as before.
uv run python -m forecast_eval.real_data --strategy night \
  --data "\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_night.csv" \
  --warmup-bars 538790 --precompute \
  --output-dir outputs/iterNN_night_<timestamp>
```

**`--strategy <session>` is new and is how you select your package.** It supplies
defaults for twelve knobs (checkpoint, context, horizon, stride, band, `min_prob`,
`full_confidence_prob`, aggression, max position, max aggression, forced close)
from that package's `PARAMS`; an explicit CLI flag still beats it, and the banner
prints which knob came from where. Without `--strategy`, the CLI's own historical
defaults apply and every command string already in `results.tsv` reproduces
byte-identically.

**Pick the tape that matches your session:**

| session | tape |
|---|---|
| `night` | `TXF_RPT_minute_night.csv` |
| `day` | `TXF_RPT_minute_day.csv` |
| `fullday` | `TXF_RPT_minute_fullday.csv` — **not** `TXF_RPT_minute.csv`, whose first 15.6 % is day-only because TAIFEX had no night session before 2017-05-15 |

**Baselines to beat.** `night` starts at Calmar **3.6006** (235 trades) and `day`
at **1.6937** (67 trades), both reproduced exactly through the new path on
2026-09-02. `fullday` has **no baseline** — it has never been tuned and its band
is DAY's by inheritance; the first useful work there is `tools/band_sweep.py` on
the full-day tape.

---

## 5. Two things this file does not change

- **The keep-gate is still `n_trades >= 60`**, and it still overrides Calmar.
- **Three autoresearch loops cannot share one working tree.** The loop ends each
  iteration in `git reset --hard` and `results.tsv` is a single lock-free append.
  Run the sessions **sequentially**, or give each its own `git worktree` and
  `results_<session>.tsv`. See STRATEGY_SPLIT_PLAN §7.5.

## Related

[STRATEGY_SPLIT_PLAN.md](STRATEGY_SPLIT_PLAN.md) · [docs/strategy/wiring.md](docs/strategy/wiring.md) · [SPEC.md](SPEC.md) · `../autoresearch_trading_plan.md` (the loop mechanics, still current; its freeze table is not)

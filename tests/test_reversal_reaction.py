"""tools/reversal_reaction.py: zigzag pivots, per-leg ride, pairing, windowing.

The synthetic tape below has three confirmed legs at a 3 % swing:
    idx    0    1    2    3    4    5    6    7
    px   100  105  110  104   99  103  108  102
pivots (0 low), (2 high), (4 low), (6 high); legs 0->2 up 10, 2->4 down 11,
4->6 up 9. The 6->7 move is unconfirmed and never scored.
"""
import json

import numpy as np
import pandas as pd
import pytest

import forecast_eval.tools.reversal_reaction as rr

PX = np.array([100, 105, 110, 104, 99, 103, 108, 102], dtype=float)
TS = pd.date_range("2024-01-02 08:45", periods=len(PX), freq="min")
TAPE = pd.DataFrame({"timestamp": TS, "price": PX, "volume": 1})


def _trades(*rows):
    return pd.DataFrame(
        [dict(entry_ts=TS[a], exit_ts=TS[e], side=s) for a, e, s in rows],
        columns=["entry_ts", "exit_ts", "side"])


def _run(tmp_path, name, trades, split=None):
    d = tmp_path / name
    d.mkdir()
    params = {"data": "tape.csv", "warmup_bars": 0, "train_split_bars": None,
              "train_split_ts": split}
    (d / "params.json").write_text(json.dumps(params), encoding="utf-8")
    t = trades.copy()
    t["entry_price"] = t["exit_price"] = 0.0
    t.to_csv(d / "trades.csv", index=False)
    pd.DataFrame({"timestamp": [TS[0]], "predicted_return": [0.0]}).to_csv(
        d / "forecasts.csv", index=False)
    return d


def test_zigzag_confirms_alternating_pivots_and_drops_the_open_leg():
    assert rr.zigzag(PX, 0.03) == [(0, -1), (2, 1), (4, -1), (6, 1)]


def test_zigzag_below_threshold_finds_nothing():
    assert rr.zigzag(np.array([100, 101, 100.5, 101.5]), 0.03) == []


def test_holding_through_the_turn_rides_until_the_exit():
    # Long over bars 1-2: aligned with leg 0->2, against leg 2->4 for one bar.
    legs = rr.score_legs(PX, rr.position_series(TS.values, _trades((1, 3, "LONG"))),
                         rr.zigzag(PX, 0.03))
    assert legs["ride"].tolist() == pytest.approx([0.0, 6 / 11, 0.0])
    down = legs.iloc[1]
    assert down["exposed"] and down["react_bars"] == 1 and not down["censored"]


def test_riding_the_whole_leg_is_censored_at_the_leg_end():
    legs = rr.score_legs(PX, rr.position_series(TS.values, _trades((2, 5, "LONG"))),
                         rr.zigzag(PX, 0.03))
    down = legs.iloc[1]
    assert down["ride"] == pytest.approx(1.0)
    assert down["censored"] and down["react_bars"] == 2


def test_a_counter_trend_entry_mid_leg_is_charged_but_not_exposed_at_the_turn():
    # Short opened inside the 4->6 up-leg: loses 103 -> 108 = 5 of 9.
    legs = rr.score_legs(PX, rr.position_series(TS.values, _trades((5, 6, "SHORT"))),
                         rr.zigzag(PX, 0.03))
    up = legs.iloc[2]
    assert up["ride"] == pytest.approx(5 / 9)
    assert not up["exposed"] and up["react_bars"] is None


def test_paired_delta_of_a_run_against_itself_is_zero(tmp_path):
    d = _run(tmp_path, "a", _trades((1, 3, "LONG")))
    legs = rr.evaluate(d, TAPE, window="all")["legs"]
    out = rr.paired_delta(legs, legs)
    assert out["delta_ride"] == 0.0 and out["ci_low"] == 0.0 and out["ci_high"] == 0.0


def test_paired_delta_sign_is_run_minus_baseline(tmp_path):
    slow = rr.evaluate(_run(tmp_path, "slow", _trades((2, 5, "LONG"))), TAPE, window="all")["legs"]
    quick = rr.evaluate(_run(tmp_path, "quick", _trades((1, 3, "LONG"))), TAPE, window="all")["legs"]
    out = rr.paired_delta(quick, slow)
    assert out["delta_ride"] < 0 and out["legs_better"] == 1


def test_train_window_drops_legs_that_end_in_the_held_out_tail(tmp_path):
    d = _run(tmp_path, "s", _trades((1, 3, "LONG")), split=str(TS[5]))
    train = rr.evaluate(d, TAPE, window="train")["summary"]
    test = rr.evaluate(d, TAPE, window="test")["summary"]
    assert train["legs"] == 2      # 0->2 and 2->4 end before the split
    assert test["legs"] == 0       # 4->6 starts before it, so belongs to neither


def test_train_window_without_a_split_refuses(tmp_path):
    with pytest.raises(SystemExit):
        rr.evaluate(_run(tmp_path, "n", _trades()), TAPE, window="train")


def test_runs_on_different_splits_are_not_comparable():
    assert rr._comparable({"data": "x", "train_split_bars": 1},
                          {"data": "x", "train_split_bars": 2}) == ["train_split_bars"]

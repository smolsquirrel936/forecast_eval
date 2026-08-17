"""TickAggregator — live trade prints into the 1-min bars the model expects.

The backtest is fed `TXF_RPT_minute_day.csv` via `real_data.load_minute_bars`;
live delivers raw ticks. This rebuilds the *same* bars from the tick stream, so
the model sees the input distribution it was evaluated on. Getting this wrong
changes the forecaster's input silently, which is why it gets its own validation
phase (SHIOAJI_PLAN.md V1).

The RPT convention, established empirically from the file itself:

  * **1 minute, left-labelled** — bar ``T`` covers ``[T, T+1min)``. The day
    session (08:45–13:45) yields bars 08:45 … 13:44, exactly 300 of them.
  * ``price`` is the bar's **close** — the last trade in the window.
  * **A minute with no trades produces no bar at all.** 194 of 3 756 day-session
    days carry fewer than 300 bars and none has volume 0, so the producer omits
    empty minutes rather than zero- or forward-filling them.

Two live-only concerns the file cannot express:

  * **simtrade** — TAIFEX broadcasts trial-matching prints before the open and
    Shioaji flags them ``simtrade=True``. They are not trades; they are dropped
    at ingest so they can never reach a bar (SHIOAJI_PLAN.md §2.5).
  * **late prints** — a tick belonging to a bar already emitted is dropped, not
    back-applied. The loop has already acted on that bar; silently rewriting it
    would make the live history diverge from what the strategy actually saw.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, List, Optional  # noqa: F401

from ..events import MarketEvent
from .sessions import SessionPolicy

BAR_LENGTH = timedelta(minutes=1)

# Tech: how long the live path waits past a bar's right edge before closing it on
#       the clock. Offline callers keep the default of zero.
# Why:  MEASURED 2026-08-11 (§2.16). Bar boundaries come from exchange timestamps
#       but `flush` fires on the *local* clock, so a tick stamped :59.9 can still
#       be in flight when the edge passes. Every duplicated minute in the live run
#       had its last tick within ~1.2 s of the boundary. Two seconds covers that
#       with room to spare, and costs a decision delay of at most 2 s against an
#       1 800 s stride (§2.7) — unmeasurable. It also absorbs local-clock skew
#       *ahead* of the exchange, the direction that silently makes this worse.
LIVE_FLUSH_GRACE = timedelta(seconds=2)


class TickAggregator:
    def __init__(self, *, drop_simtrade: bool = True,
                 drop_non_session: bool = True,
                 policy: Optional[SessionPolicy] = None,
                 flush_grace: timedelta = timedelta(0)):
        # Tech: hold the ingest policies and start with no open bucket. `policy`
        #       decides which sessions are emitted; it defaults to "both", which
        #       reproduces the previous behaviour.
        # Why:  `drop_non_session` mirrors `Environment(drop_non_session=True)`.
        #       `policy` is the narrower filter on top: a deployment that trades
        #       only one session must not have the other one reaching the model,
        #       and naming the session here rather than hardcoding it is what lets
        #       the same aggregator serve the night strategy and a day-session
        #       port (§2.12).
        self.drop_simtrade = drop_simtrade
        self.drop_non_session = drop_non_session
        self.policy = policy or SessionPolicy.both()

        # Tech: the clock-flush grace, defaulting to zero.
        # Why:  zero keeps the aggregator a pure function of its ticks, which is
        #       what an offline replay wants — there the whole stream is present
        #       and nothing is ever "still in flight". Only the live path has a
        #       race to lose, so only `LiveRuntime` opts in (`LIVE_FLUSH_GRACE`).
        self.flush_grace = flush_grace

        # Tech: the open bucket — its left edge, running last price and volume.
        # Why:  only the close and total volume survive into a MarketEvent, so
        #       there is nothing else to accumulate; keeping the left edge (rather
        #       than the last tick's time) is what makes the bar left-labelled.
        self._bucket_start: Optional[Any] = None
        self._last_price: Optional[float] = None
        self._volume: int = 0

        # Tech: the left edge of the most recent bucket `_close` finished, kept
        #       after the bucket itself is cleared.
        # Why:  §2.16 — `_close` sets `_bucket_start = None`, which makes the
        #       late-print guard below (`bucket < self._bucket_start`) unreachable
        #       the moment a bar is closed by `flush`. Without a separate memory a
        #       straggler for that minute falls through to the "first tick ever"
        #       branch and *reopens a minute already emitted*, producing a second
        #       bar for it. Measured live: 20 bars for 15 minutes.
        self._last_emitted: Optional[Any] = None

        # Tech: ingest counters, never reset.
        # Why:  V1 parity and the live health rails both need to see *why* bars
        #       went missing. A rising `n_late_dropped` means the feed is
        #       delivering out of order; a rising `n_simtrade_dropped` outside the
        #       pre-open window means the filter is catching real prints.
        self.n_ticks = 0
        self.n_simtrade_dropped = 0
        self.n_late_dropped = 0
        self.n_bars_emitted = 0
        self.n_bars_dropped_off_session = 0

    # ---- ingest ------------------------------------------------------------

    def add_tick(
        self,
        timestamp: Any,
        price: float,
        volume: int = 1,
        *,
        simtrade: bool = False,
    ) -> List[MarketEvent]:
        # Tech: drop trial-matching prints before they touch any bucket.
        # Why:  §2.5 — these are exchange trial prints, not trades. Filtering at
        #       the very top means no later stage has to know they exist.
        self.n_ticks += 1
        if simtrade and self.drop_simtrade:
            self.n_simtrade_dropped += 1
            return []

        bucket = _floor_minute(timestamp)

        # Tech: a tick for a minute already emitted is counted late and discarded,
        #       whether or not a bucket is currently open.
        # Why:  §2.16, and it must come *before* the `_bucket_start is None` test.
        #       Bar boundaries come from exchange timestamps but `flush` fires on
        #       the local clock, so a tick stamped 15:00:59.9 can arrive after the
        #       15:00 bar has already been closed and cleared — every duplicated
        #       minute in the live run had its last tick within ~1.2 s of the
        #       boundary. Reopening that minute is strictly worse than dropping
        #       the tick: it corrupts the context *and* inflates `bar_idx`, which
        #       the forecast stride and every ExitRule holding period count in.
        if self._last_emitted is not None and bucket <= self._last_emitted:
            self.n_late_dropped += 1
            return []

        # Tech: first tick ever — open a bucket and emit nothing.
        # Why:  a bar is only complete once time has moved past it, so the first
        #       tick can never produce one.
        if self._bucket_start is None:
            self._open(bucket, price, volume)
            return []

        # Tech: a tick for an already-closed bar is counted and discarded.
        # Why:  the loop has already forecast and traded on that bar. Back-
        #       applying a late print would rewrite history the strategy acted on
        #       and put the live bar series permanently out of step with what an
        #       offline replay would rebuild from the same ticks.
        if bucket < self._bucket_start:
            self.n_late_dropped += 1
            return []

        # Tech: still inside the open bucket — extend it.
        # Why:  the close is simply the most recent price, and volume accumulates.
        if bucket == self._bucket_start:
            self._last_price = float(price)
            self._volume += int(volume)
            return []

        # Tech: time has moved on — close the open bucket, then open the new one
        #       at this tick's minute.
        # Why:  this is the only path that emits. Note it emits *one* bar even
        #       when several minutes were skipped: the intervening minutes had no
        #       trades, and the RPT convention omits empty minutes entirely.
        bars = self._close()
        self._open(bucket, price, volume)
        return bars

    def flush(self, now: Any) -> List[MarketEvent]:
        # Tech: close the open bucket if `now` has passed its right edge.
        # Why:  `add_tick` can only close a bar when the *next* tick arrives, so a
        #       quiet minute would hold a completed bar hostage until the market
        #       moves again. A wall-clock timer calls this so the bar closes on
        #       schedule. Driven by local time, never by tick timestamps — it is
        #       the one place the live loop is allowed to consult the local clock.
        #
        #       The grace applies **only here**, not to `add_tick`'s close-on-new-
        #       minute path. Once a tick for minute M+1 has arrived, everything for
        #       M has arrived too — measured on the live capture, which held 1 106
        #       ticks with **zero** out-of-order arrivals. So the tick path can
        #       close immediately and lose nothing; it is this blind clock path,
        #       firing when *no* tick has arrived yet, that needs to wait.
        if self._bucket_start is None:
            return []
        if now < self._bucket_start + BAR_LENGTH + self.flush_grace:
            return []
        return self._close()

    # ---- internals ---------------------------------------------------------

    def _open(self, bucket: Any, price: float, volume: int) -> None:
        # Tech: start a fresh bucket at `bucket` seeded with this tick.
        # Why:  the seeding tick is both the open and (so far) the close; only the
        #       close is kept, so no separate open/high/low tracking is needed.
        self._bucket_start = bucket
        self._last_price = float(price)
        self._volume = int(volume)

    def _close(self) -> List[MarketEvent]:
        # Tech: turn the open bucket into a MarketEvent stamped at its left edge,
        #       classify its session, and clear the bucket.
        # Why:  classifying here (and dropping `None`) mirrors
        #       `Environment.stream()` exactly, so the live stream and an offline
        #       replay of the same ticks hand `LiveLoop` the identical sequence —
        #       which is what makes the V0 parity guarantee extend to live.
        assert self._bucket_start is not None and self._last_price is not None
        ts = self._bucket_start
        price = self._last_price
        volume = self._volume
        self._bucket_start = None
        self._last_price = None
        self._volume = 0

        # Tech: remember this minute as closed — set here, *before* the session
        #       filter below, so it is recorded even when the bar is dropped.
        # Why:  the minute is finished either way. Recording it only for bars that
        #       survive filtering would let a straggler reopen an off-session
        #       minute, which is the §2.16 bug again by a quieter route.
        self._last_emitted = ts

        # Tech: classify the bar, drop it when it is outside trading hours or
        #       outside the sessions this deployment trades.
        # Why:  the first test mirrors `Environment`; the second is the policy
        #       filter (§2.12). A night-only deployment that let day bars through
        #       would feed the model a session it was never evaluated on, and the
        #       bar counts the forecast stride depends on would be wrong too.
        session = self.policy.label(ts)
        if session is None:
            if self.drop_non_session:
                self.n_bars_dropped_off_session += 1
                return []
            session = "DAY"  # fallback, matching Environment
        elif session not in self.policy.sessions:
            self.n_bars_dropped_off_session += 1
            return []

        self.n_bars_emitted += 1
        return [MarketEvent(
            timestamp=ts,
            price=float(price),
            volume=int(volume),
            session=session,  # type: ignore[arg-type]
        )]


def _floor_minute(ts: Any) -> Any:
    # Tech: truncate a timestamp to its containing minute, preferring pandas'
    #       `floor` and falling back to `replace` for plain datetimes.
    # Why:  left-labelling *is* this floor — bar T covers [T, T+1min). pandas
    #       Timestamps carry nanoseconds that `replace(second=0, microsecond=0)`
    #       would leave behind, so `floor` is used where available; the fallback
    #       keeps the aggregator usable with stdlib datetimes in tests.
    try:
        return ts.floor("min")
    except AttributeError:
        return ts.replace(second=0, microsecond=0)

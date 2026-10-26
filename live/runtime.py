"""LiveRuntime — assembles a live session from one config, and holds it together.

Everything network-facing lives in `cli.py`; everything *decidable* lives here,
so the whole assembly is testable without credentials, a feed, or a GPU.

Two jobs, both of which were previously nobody's:

**1. One policy, injected everywhere.** `SessionPolicy` used to be handed
separately to the aggregator, the backfill and the rails, with nothing checking
they agreed. A mismatch would splice the model's context from two regimes and
never raise. The runtime now owns the single policy, builds the aggregator with
it, fetches warm-up with it, and **validates** that any bars handed to it satisfy
it. A mismatched pair is no longer expressible.

**2. Warm-up that leaves nothing cold.** Three distinct things must be warm
before the first live decision, and only the first is obvious:

  * the **forecaster's context** — `context_length` bars, backfilled, not waited
    for (SHIOAJI_PLAN §7.5);
  * the **model itself** — the first forecast costs ~18.8 s against ~0.09 s
    warm (§2.7), so a throwaway forecast is issued during start-up rather than
    on the first real signal;
  * the **emitter's rolling deques** — `ThresholdEmitter._trend_confirms` fails
    **open** (trades) while it holds fewer than 3 samples, so an un-primed
    restart runs with a risk filter silently disabled. Primed by replaying
    historical forecasts through `emit_signal` and discarding the results, which
    is correct for any emitter because the emitter's own code does the
    accumulating (§2.8).
"""
from __future__ import annotations

from contextlib import nullcontext
from time import monotonic as _monotonic
from typing import Any, List, Optional, Sequence

import pandas as pd

from ..events import FillEvent
from ..execution import Execution
from ..portfolio import Portfolio
from ..strategy import wiring as strategy_wiring
from ..trader import Trader
from .backfill import fetch_bars, fetch_bars_continuous
from .broker.base import Broker
from .broker.dryrun import DryRunBroker
from .broker.sim import SimBroker
from .config import LiveConfig
from .loop import LiveLoop
from .sessions import SessionPolicy
from .state import RailsConfig, SafetyRails


# Tech: the roll ladder — how far through the touch the first attempt is
#       priced, how much each retry adds, where it stops, and how often it fires.
# Why:  §2.6. A roll leg that does not fill is the only failure that matters:
#       missing the close means holding an expiring contract into a cash
#       settlement whose price we do not control. So aggression escalates until
#       it fills, in **equal 5-tick steps** rather than doubling — the step is the
#       operator's, and a linear ladder keeps every rung's cost knowable.
#
#       `ROLL_LADDER_MAX_TICKS` exists because escalation cannot be the answer
#       for ever. §2.19 measured that the simulator matches on **prints**: in a
#       minute with no trade, no price fills, and beyond a point more aggression
#       buys nothing while giving up the bound that makes this a limit order
#       rather than an MKT. Past the cap the ladder keeps retrying *at* the cap.
#
#       `ROLL_LADDER_INTERVAL` is 10 s, not the 5 s first proposed, because the
#       order-rate rail caps submissions at 10/minute and breaching it is a STOP
#       halt (`state.py`) — halted mid-roll while still holding the expiring
#       contract is the worst state this can end in. 10 s is 6/min for one leg,
#       inside the cap with room for the other leg's retries. It is also longer
#       than `IOC_TTL_SECONDS`, so attempt N+1 can never race attempt N's expiry.
ROLL_LADDER_START_TICKS = 5
ROLL_LADDER_STEP_TICKS = 5
ROLL_LADDER_MAX_TICKS = 25
ROLL_LADDER_INTERVAL = 10.0


# Tech: the fixed batch size `_prime_emitter` hands to
#       `forecast_series_batch`, instead of the default `"auto"`.
# Why:  "auto" sizes the batch to 80 % of the VRAM free *at that moment*, and
#       PyTorch's caching allocator keeps the peak reserved for the life of the
#       process. MEASURED 2026-10-03 (Toto-2.0-1B, ctx 3008, 900 boundaries, RTX
#       5090): auto picked its cap of 1024 and held 21.5 GiB, so the first of
#       three deployments claimed the card and the rest over-committed it to
#       63 GB. Windows then paged one deployment's model off the GPU entirely. At
#       32 the same replay takes 5.0 s (auto 5.2 s, 64 4.9 s, 16 5.9 s) and peaks
#       at 4.61 GiB. Batch size changes outputs only at float noise (|d return|
#       <= 1.8e-7, |d prob_up| <= 2.2e-5), so priming reads the same signals.
PRIME_BATCH_SIZE = 32


def ladder_ticks(attempt: int) -> int:
    """How far through the touch attempt `attempt` (1-based) is priced."""
    # Tech: start + step per attempt after the first, clamped at the cap.
    # Why:  a pure function of the attempt number so the schedule is stated in one
    #       place and can be asserted directly, rather than inferred from a
    #       sequence of submissions.
    if attempt < 1:
        attempt = 1
    return min(ROLL_LADDER_START_TICKS + ROLL_LADDER_STEP_TICKS * (attempt - 1),
               ROLL_LADDER_MAX_TICKS)


class LiveRuntime:
    def __init__(
        self,
        config: LiveConfig,
        *,
        broker: Optional[Broker] = None,
        recorder: Any = None,
        rails: Optional[SafetyRails] = None,
        rails_config: Optional[RailsConfig] = None,
        simulation: bool = True,
        emitter_prime_boundaries: int = 900,
        tick_capture: Any = None,
        alerter: Any = None,
        ledger_dir: Any = None,
        strategy_overrides: Optional[dict] = None,
    ):
        # Tech: build the whole stack from `config`, deriving the session policy
        #       from it rather than accepting one separately.
        # Why:  taking a policy argument would immediately reintroduce the
        #       mismatch this class exists to prevent. There is exactly one policy
        #       in a live session and it comes from the pinned config.
        self.config = config
        self.policy = config.policy
        # Tech: build this deployment's whole strategy stack once, from its own
        #       `strategy/<session>/` package, and keep the resolved knobs.
        # Why:  LIVE_STRATEGY_ROUTING_PLAN D2 -- one construction site for live
        #       and backtest alike, so what a live run trades IS what
        #       `real_data.py --strategy <session>` backtests, by construction
        #       rather than by two declarations agreeing. `warmup_bars=0` because
        #       the context arrives via `seed_history`; live it would only delay
        #       trading behind bars already held.
        self.strategy = strategy_wiring.make_strategy(
            config.strategy_package,
            warmup_bars=0,
            tick_size=config.tick_size,
            **(strategy_overrides or {}),
        )
        # Tech: the fully resolved knob set -- package defaults with any
        #       `--param` override merged in.
        # Why:  several things below are strategy values that are not components
        #       (`context_length` sizes the history window, `max_position` is
        #       checked against the rails). They must read the same resolution the
        #       components were built from, not a second copy.
        self.strategy_params = self.strategy["params"]
        self.recorder = recorder
        self.simulation = simulation
        # Tech: how many stride boundaries `_prime_emitter` replays; the slice
        #       takes all of them when the warm-up frame holds fewer.
        # Why:  900, raised from 500 on 2026-09-02 (DAY_SESSION_PLAN §2.5). K
        #       was originally sized to clear night's longest HOLD run (385
        #       boundaries) with margin, but that is the wrong statistic:
        #       samples are counted, not required to be consecutive, so what
        #       decides a start-up is the FEWEST non-HOLD signals in any
        #       K-boundary window. MEASURED over each pinned run's whole eval
        #       window on the @2026-09-01 tape: at K=500 the worst window holds
        #       3 for DAY (exactly `_check_primed`'s floor) and **1 for NIGHT**
        #       — below it, so there are historical stretches where a NIGHT
        #       start-up would have refused. At K=900 the worst windows are 10
        #       and 7. The failure is a refused start-up, never unfiltered
        #       trading (`_check_primed` raises), so this buys availability,
        #       not correctness.
        #
        #       It is deliberately LARGER than the 500 `warmup_days_default`
        #       sizes its request for, and the two are not meant to agree: the
        #       fetch guarantees 500 boundaries, while the bar cache routinely
        #       carries more (a seeded DAY cache holds 30 000 bars = 899
        #       boundaries), and this cap is what lets the replay spend them.
        #       Raising the *request* instead would widen every warm-up fetch
        #       across delisted months — C7's problem — to buy what the cache
        #       already has on disk.
        self.emitter_prime_boundaries = emitter_prime_boundaries
        self.tick_capture = tick_capture
        # Tech: what the venue said it held at start-up; None until
        #       `adopt_position` runs, and None after it when this broker
        #       has no venue to ask.
        # Why:  the ledger reconciliation needs "was a venue asked at all",
        #       which the adopt call's own return value cannot express — it
        #       is None both for a flat account and for no account. Read
        #       once and remembered, because `broker_position_detail` is a
        #       start-up query by contract and must not be issued twice.
        self.venue_position: Optional[int] = None
        self.alerter = alerter

        self.rails = rails or SafetyRails(
            rails_config or RailsConfig(),
            simulation=simulation,
            policy=self.policy,
            recorder=recorder,
        )
        # Tech: fail before the first bar if the trader is sized beyond what the
        #       accounting supports.
        # Why:  §2.10 — `Trader.on_fill` has no average-entry branch for a second
        #       partial fill, so anything above 1 is unsound rather than merely
        #       risky.
        self.rails.check_startup(max_position=self.strategy_params["max_position"])

        self.execution = Execution(fee_rate=config.fee_rate)
        self.broker: Broker = broker or DryRunBroker(self.execution,
                                                     recorder=recorder)
        # Tech: the Trader comes from the config's own strategy package rather
        #       than being constructed here.
        # Why:  STRATEGY_SPLIT_PLAN §2.3. Built inline, this was the one
        #       component a live deployment could not swap, so a per-session
        #       Trader could exist in the backtest and not live — and the two
        #       paths would then disagree about what the deployment *is*, which
        #       is what `tests/test_live_parity*.py` exist to prevent. The
        #       factory reads the same declared fields the inline construction
        #       did, so behaviour is unchanged for both tuned deployments.
        self.trader = self.strategy["make_trader"](execution=self.broker)
        self.portfolio = Portfolio(contract_multiplier=config.contract_multiplier)
        # Tech: signed size waiting to be re-established on a newly rolled
        #       contract; 0 when nothing is pending.
        # Why:  §2.6 — a roll settles the old leg at the venue and the exposure
        #       has to be re-opened on the new month, but there is no price to
        #       open at until that month prints. Holding the intent here and
        #       acting on the first bar keeps the entry on a real observed price
        #       rather than a pre-open guess.
        self._roll_entry = 0
        # Tech: where the scheduled roll has got to — None, "requested",
        #       "closing", "switch" (waiting for the caller to move the
        #       contract), or "reopening" — plus the size to restore and the
        #       timestamps of the fills that were roll legs.
        # Why:  §2.6. The roll is close-then-open across two contracts, which
        #       takes more than one bar, so the sequence has to be state rather
        #       than a call. `roll_fills` is what lets `build_trades` book the two
        #       legs as **one** round-trip instead of splitting a 4.5-day hold.
        self._roll_state = None
        self._roll_size = 0
        self.roll_fills: set = set()
        # Tech: the ladder's attempt counter, the monotonic time of the last
        #       submission, and the market event to re-price against.
        # Why:  the ladder runs on the **clock**, not on bars: a bar is a minute
        #       wide and the whole point is to retry inside that. The market event
        #       is kept from the last bar because a limit needs a reference price
        #       and the clock does not carry one — re-pricing against a stale bar
        #       is fine, since the ladder's escalation is what reaches the market,
        #       not the reference. Not across a roll: the months trade ~160
        #       points apart, far past any rung, so `switch_contract` clears it.
        self._ladder_attempt = 0
        self._ladder_last = None
        self._ladder_market = None
        # Tech: the codes a print must carry to reach the bar stream, or None
        #       before any roll (no filter).
        # Why:  2026-10-20 (simtrade_operations §11) — after the switch, prints
        #       of the old month already queued behind it would still build bars,
        #       and the re-open would be priced from them. Set only by a roll, so
        #       a code mismatch can never silence a run that has not rolled.
        self._contract_codes: Optional[set] = None
        self.n_foreign_dropped = 0
        self.emitter = self.strategy["emitter"]
        # Tech: warmup_bars=0 because the context arrives via `seed_history`.
        # Why:  in the backtest `warmup_bars` is the train/eval split; live it
        #       would only delay trading behind bars we already hold.
        self.forecaster = self.strategy["forecaster"]

        # Tech: the aggregator is built with *this* policy, from `TickAggregator`
        #       rather than by the caller.
        # Why:  one construction site for the live bar stream, so the aggregator
        #       and the backfill cannot disagree about the session.
        from .bars import LIVE_FLUSH_GRACE, TickAggregator
        self.aggregator = TickAggregator(policy=self.policy,
                                         flush_grace=LIVE_FLUSH_GRACE)

        self.loop = LiveLoop(
            forecaster=self.forecaster,
            emitter=self.emitter,
            broker=self.broker,
            trader=self.trader,
            portfolio=self.portfolio,
            exit_rule=self.strategy["exit_rule"],
            forced_close_on_session_end=self.strategy["forced_close"],
            history_window=self.strategy_params["context_length"],
            recorder=recorder,
            rails=self.rails,
            alerter=alerter,
            expected_position=self._expected_account_position,
        )
        # Tech: where the per-deployment fill ledgers live; None disables the
        #       cross-deployment sum and keeps the loop on this deployment's own
        #       position.
        # Why:  STRATEGY_SPLIT_PLAN 16. Passed in rather than derived, because
        #       only the entry point knows the run directory -- `cli.py` builds
        #       `ledger_path(out.parent, policy)` and this is the same parent.
        #       Defaulting to None keeps every offline test and the V0 parity
        #       harness on exactly the previous operand.
        self.ledger_dir = ledger_dir
        self.warm = False

    def _expected_account_position(self) -> Optional[int]:
        # Tech: what every deployment's ledger says the ACCOUNT holds, or None
        #       when there is no ledger directory or a ledger cannot be read.
        # Why:  16. Returning None -- rather than 0 or this deployment's own
        #       size -- is what lets the loop fall back cleanly: an absent or
        #       unreadable ledger must degrade to "cannot compare", never to a
        #       confident wrong number, which is the same rule
        #       `broker_position()` follows when the venue will not answer.
        if self.ledger_dir is None:
            return None
        from .ledger import account_net_position
        return account_net_position(self.ledger_dir)

    # ---- resuming into an open position ------------------------------------

    def adopt_position(self, *, timestamp: Any,
                       fallback: Optional[Any] = None) -> Optional[dict]:
        # Tech: read what the venue holds and seed the Trader's and Portfolio's
        #       mirrors from it. Returns a summary of what was adopted, or None
        #       when the account is flat or the broker cannot answer.
        # Why:  §2.20. Both mirrors start at zero, which is only true for a
        #       process that starts flat. The strategy holds a median 4.5 days, so
        #       *any* restart during a soak lands mid-position — and a zero mirror
        #       there is not merely stale: the reconciliation rail compares 0
        #       against the venue's N and halts on the first bar (§2.9), and the
        #       no-flip state machine would read "flat" and open a second lot.
        #       Adopting makes a restart continue the trade rather than refuse it.
        held = self.broker.broker_position_detail()
        # Tech: record what the venue answered — None when this broker has no
        #       venue to ask — and, in that case only, adopt from `fallback`.
        # Why:  MEASURED 2026-09-02 on `live_night_20260902_082640`. A None here
        #       has two meanings (`broker/base.py`), and the ledger
        #       reconciliation in `cli.py` read both as "the venue is flat": a
        #       dry run's ledger replayed to +1 against a venue that was never
        #       asked, so the history was refused on every start and the run left
        #       an empty `fills.csv` and `trades.csv` — weeks of price on the
        #       report with no fills drawn on it. `has_venue` is what separates
        #       "nothing to ask" from "the query failed", and only the first is
        #       safe to substitute for; a failed query still adopts nothing, so
        #       the caller refuses rather than guesses.
        self.venue_position = None if held is None else held.signed_qty
        source = "venue"
        if held is None and not self.broker.has_venue and fallback is not None:
            held, source = fallback, "ledger"
        if held is None or held.signed_qty == 0:
            return None

        # Tech: refuse rather than adopt when the size exceeds what the accounting
        #       supports, or when the configured exit rule is not `adoptable`.
        # Why:  two ways adoption could quietly produce a wrong number instead of
        #       an error. Above `max_position`, `Trader.on_fill` has no
        #       average-entry branch (§2.10) — the same reason `check_startup`
        #       raises. And the true entry *bar index* and time are unknowable
        #       here: they belong to a process that is gone, so any rule measuring
        #       bars held would measure from a sentinel. A rule that reads only
        #       side and entry price (`EntryPercentStop`, `FixedStopLoss`)
        #       declares `adoptable` and runs on the venue's seeded entry; any
        #       other rule still fails loudly rather than silently time a stop
        #       from the wrong origin.
        if abs(held.signed_qty) > self.strategy_params["max_position"]:
            raise ValueError(
                f"the account holds {held.signed_qty} lot(s), above "
                f"max_position={self.strategy_params['max_position']}. Trader.on_fill cannot "
                f"average a second lot's entry (§2.10), so this cannot be adopted "
                f"— flatten with `python -m forecast_eval.live.flatten`, then "
                f"restart.")
        rule = self.loop.exit_rule
        if rule is not None and not getattr(rule, "adoptable", False):
            raise ValueError(
                f"cannot adopt a position while the ExitRule "
                f"{type(rule).__name__} is configured: it is not adoptable — the "
                f"entry bar index belongs to the previous process and is not "
                f"recoverable, so any bars-held rule would measure from a "
                f"sentinel.")
        if self.trader.position != 0:
            raise ValueError(
                f"adopt_position ran with the mirror already at "
                f"{self.trader.position}; adoption seeds a fresh mirror and must "
                f"happen once, before the first bar.")

        # Tech: seed both mirrors — the Trader's (which drives the no-flip machine
        #       and any exit rule) and the Portfolio's (which realizes the PnL).
        # Why:  the venue's average entry, not the current price, is what goes in.
        #       It means an inherited loss lands on the day the position closes,
        #       which can put a restart's first close straight through the daily
        #       loss limit — but that is the money that was actually made or lost,
        #       and it is what the backtest books too. The alternative, resetting
        #       entry to the market, would understate a losing resume and flatter
        #       every restart. `entry_bar_idx` gets the flat sentinel because this
        #       run has no bar for an entry that predates it; the guard above is
        #       what keeps that sentinel from being read as a real index.
        self.trader.position = held.signed_qty
        self.trader.entry_price = held.avg_price
        self.trader.entry_bar_idx = -1
        self.trader.entry_timestamp = timestamp
        self.portfolio.position = held.signed_qty
        self.portfolio.avg_entry_price = held.avg_price

        adopted = {
            "position": held.signed_qty,
            "entry_price": held.avg_price,
            "adopted_at": timestamp,
            # Tech: which of the two sources the mirrors were seeded from.
            # Why:  an operator reading `position_adopted` needs to know whether
            #       a real account was asked or the deployment's own ledger stood
            #       in for one; the numbers are indistinguishable otherwise.
            "source": source,
        }
        if self.recorder is not None:
            self.recorder.record("position_adopted", **adopted)
        return adopted

    # ---- contract roll (§2.6) ----------------------------------------------
    @property
    def last_price(self):
        """The newest bar's price, or None before the first bar."""
        # Tech: expose the loop's running mark.
        # Why:  the roll has to settle an expiring leg at *some* observed price,
        #       and this is the only one the process holds. A property rather
        #       than reaching into `loop._last_price` from the CLI, so the mark
        #       has one owner and the None case is stated where it is produced.
        return self.loop._last_price


    def _settlement_session(self, timestamp: Any) -> str:
        # Tech: the session a synthetic roll settlement is booked to, derived
        #       from the deployment's policy and the fill's own timestamp.
        # Why:  DAY_SESSION_PLAN C3. This used to be the literal "NIGHT", which
        #       is correct for exactly one deployment and silently wrong for the
        #       other: on a DAY run every rolled round-trip accrued into
        #       `realized_pnl_by_session["NIGHT"]`, a session the run never
        #       traded. Nothing raises — the numbers are simply filed under the
        #       wrong heading.
        #
        #       A single-session policy answers unconditionally, ahead of the
        #       label. That is deliberate: `ROLL_AT` can fall outside the traded
        #       session (C4 — 15:30 is outside a DAY session), and booking the
        #       closing leg to whatever session the clock happened to land in
        #       would split one round-trip across two buckets. The opening leg
        #       was this deployment's; so is the close.
        sessions = self.policy.sessions
        if len(sessions) == 1:
            return next(iter(sessions))
        # Tech: a multi-session policy has no single answer, so the timestamp
        #       decides; "UNKNOWN" matches `metrics.build_trades`' own fallback
        #       for a fill it cannot classify.
        return self.policy.label(timestamp) or "UNKNOWN"

    def settle_rolled_leg(self, *, price: float, timestamp: Any,
                          session: Optional[str] = None) -> Optional[dict]:
        """Close the expiring contract's position in both mirrors, at `price`."""
        # Tech: apply a synthetic fill on the closing side for the whole position,
        #       through the same two calls `LiveLoop` makes for a venue fill, then
        #       remember the signed size so it can be re-opened on the new month.
        # Why:  the alias only moves *after* the old month is dead, so by the time
        #       a roll is detectable the venue has already cash-settled the leg —
        #       there is no order we could send to close it, and the mirrors would
        #       otherwise carry a position in a contract that no longer exists.
        #       Going through `apply_fill`/`on_fill` rather than assigning the
        #       fields keeps the round trip in `Portfolio.trades` and the realized
        #       PnL consistent with every other close.
        #
        #       `price` is an approximation and is recorded as one: the true
        #       number is the venue's final settlement price, which is set by an
        #       auction we do not see. The last observed price of the expiring
        #       contract is the closest thing this process holds.
        held = self.trader.position
        if held == 0:
            return None
        side = "SELL" if held > 0 else "BUY"
        fill = FillEvent(timestamp=timestamp, side=side, fill_price=float(price),
                         quantity=abs(held), fee=0.0)
        pnl_before = self.portfolio.realized_pnl
        # Tech: derive the session unless the caller named one explicitly.
        # Why:  C3 — see `_settlement_session`. The parameter is kept so a test
        #       can force a value, but no production caller passes it.
        if session is None:
            session = self._settlement_session(timestamp)
        self.portfolio.apply_fill(fill, session=session)
        with self._trader_roll_leg():
            self.trader.on_fill(side, abs(held), fill_price=float(price),
                                bar_idx=-1, timestamp=timestamp)
        # Tech: the settlement joins the loop's fill list.
        # Why:  that list is what the ledger appends and `trades.csv` pairs.
        #       Left out, the ledger read the settled lot and its re-open as two
        #       open lots, and `roll_fills` named a timestamp no fill carried.
        self.loop.fills.append(fill)
        self._roll_entry = held
        # Tech: the synthetic settlement counts as a roll leg.
        # Why:  it closes one contract and the re-entry opens another, which is
        #       the same economic event as a traded roll — `build_trades` must
        #       book the pair as one round-trip either way.
        self.roll_fills.add(timestamp)
        settled = {
            "position": held,
            "session": session,
            "settled_at": float(price),
            "realized_points": round(self.portfolio.realized_pnl - pnl_before, 2),
            "price_is_estimated": True,
        }
        if self.recorder is not None:
            self.recorder.record("roll_leg_settled", timestamp=timestamp,
                                 **settled)
        return settled

    def switch_contract(self, codes: Any) -> None:
        """Build bars from the new month only, starting now (§2.6)."""
        # Tech: accept only prints stamped with `codes` from here on, discard the
        #       half-built bar, and forget the ladder's reference bar.
        # Why:  2026-10-20 (simtrade_operations §11) — night re-opened BUY at
        #       46419 off the last October bar while November traded ~46578, and
        #       fullday SELL at 46413 the same way. Three things held an
        #       old-month price: the open bucket, prints still queued, and
        #       `_ladder_market`, which the clock re-priced within the second.
        #       With all three cleared, the first bar either roll leg can be
        #       priced from is one built entirely from the new month.
        self._contract_codes = {str(c) for c in codes if c}
        self.aggregator.discard_open()
        self._ladder_market = None
        if self.recorder is not None:
            self.recorder.record("runtime_contract_switched",
                                 codes=sorted(self._contract_codes))

    @property
    def roll_state(self):
        """Where the scheduled roll has got to; `None` when none is running."""
        return self._roll_state

    def request_roll(self, now: Any = None) -> None:
        # Tech: ask for a scheduled roll; the close is submitted on the next bar.
        # Why:  the request arrives from the clock, which has no bar and therefore
        #       no price to place a limit against. Deferring to the next bar means
        #       the closing order is priced off an observed print, the same as
        #       every other order this system sends.
        if self._roll_state is not None:
            return
        self._roll_state = "requested"
        self._roll_size = self.trader.position
        if self.recorder is not None:
            self.recorder.record("roll_requested", when=now,
                                 position=self._roll_size)

    def resume_roll(self, now: Any = None) -> None:
        # Tech: called once the caller has moved the contract; arms the re-entry.
        # Why:  the two halves are deliberately split across the caller: only the
        #       CLI can re-subscribe and re-point the broker, and only the runtime
        #       can trade. Arming here rather than in `request_roll` is what
        #       guarantees the re-entry lands on the **new** month.
        if self._roll_size == 0:
            self._roll_state = None
            return
        self._roll_entry = self._roll_size
        self._roll_state = "reopening"
        self._reset_ladder()
        if self.recorder is not None:
            self.recorder.record("roll_resuming", when=now,
                                 position=self._roll_size)

    def advance_roll_ladder(self, now: Any = None) -> Optional[int]:
        # Tech: re-submit the outstanding roll leg one rung higher, if the
        #       interval has elapsed and nothing is pending. Returns the rung
        #       used, or None when nothing was submitted.
        # Why:  §2.6. Driven from the CLI's 1 Hz clock because a bar is a minute
        #       wide and this must retry inside one. Submitting is safe off the
        #       bar path — only fill *collection* is the loop's, and that stays
        #       exactly where it was, so the single-consumer invariant holds.
        #
        #       `has_pending()` is the gate, which is why the broker's local IOC
        #       deadline matters: without it an unreported expiry would block
        #       every rung and the ladder would be inert (see `IOC_TTL_SECONDS`).
        if self._roll_state not in ("closing", "reopening"):
            return None
        if self._ladder_market is None or self.broker.has_pending():
            return None
        clock = _monotonic()
        if (self._ladder_last is not None
                and clock - self._ladder_last < ROLL_LADDER_INTERVAL):
            return None

        self._ladder_attempt += 1
        ticks = ladder_ticks(self._ladder_attempt)
        self._ladder_last = clock
        market = self._ladder_market
        rolling = getattr(self.broker, "rolling", None)
        if rolling is None:
            # Tech: a broker with no roll scope (sim, dry-run) submits ordinarily.
            # Why:  the ladder is about *venue* order types; the offline brokers
            #       model fills from prints and have no IOC to express. Escalation
            #       still happens, through the price, which is the half that
            #       matters to a fill model.
            order = self._submit_roll_leg(market, ticks)
        else:
            with rolling(ticks):
                order = self._submit_roll_leg(market, ticks)
        if self.recorder is not None:
            self.recorder.record(
                "roll_ladder_attempt", when=now, attempt=self._ladder_attempt,
                ticks_through=ticks, leg=self._roll_state,
                price=None if order is None else order.limit_price)
        return ticks

    def _submit_roll_leg(self, market: Any, ticks: int):
        # Tech: close on the old month, or open the size the roll banked.
        # Why:  one function for both legs so the ladder cannot escalate one and
        #       not the other. `submit_exit` and `on_signal` are the ordinary
        #       paths — the ladder changes the *price* and the time-in-force, never
        #       the state machine, so the no-flip rules and the mirrors see the
        #       same orders they always do.
        with self._trader_roll_leg():
            if self._roll_state == "closing":
                return self.trader.submit_exit(market)
            want = self._roll_size
            direction = "BUY" if want > 0 else "SELL"
            return self.trader.on_signal(direction, market, strength=1.0)

    def _trader_roll_leg(self):
        # Tech: the trader's `rolling()` context manager when it defines one,
        #       else a no-op context. Entered around every call this runtime
        #       makes into the trader on a roll's behalf: the scheduled close
        #       (`submit_exit`), the settlement's synthetic fill (`on_fill`) and
        #       both re-opens (`on_signal`).
        # Why:  a roll's re-open is an OPEN from flat on the side that was just
        #       closed, which is exactly what a trader's re-entry filter (e.g. a
        #       same-side cooldown) exists to block. A trader that filters entries
        #       needs to tell the roll's orders from the strategy's, and this
        #       runtime is the only place that knows. The hook is optional, read
        #       with getattr like the broker's `rolling`, so the frozen shared
        #       `Trader` and the packages that declare no filter are untouched.
        #       The scheduled close's own venue fill arrives later through
        #       `LiveLoop`, outside this context: a filter must exempt the
        #       re-open, not rely on the close being marked.
        rolling = getattr(self.trader, "rolling", None)
        return rolling() if rolling is not None else nullcontext()

    def _reset_ladder(self) -> None:
        # Tech: clear the ladder between legs and at the end of a roll.
        # Why:  the close's escalation says nothing about the open's — a book that
        #       was thin one second ago may be fine now, and starting the second
        #       leg at 25 ticks through would pay for the first leg's problem
        #       twice.
        self._ladder_attempt = 0
        self._ladder_last = None

    def _advance_roll(self, market: Any) -> None:
        # Tech: drive the roll one step per bar — submit the close, notice it
        #       filled, then wait for the caller; and afterwards notice the
        #       re-entry filled.
        # Why:  every transition is driven by something *observed* (a position
        #       that reached zero, a position that came back), never by a timer.
        #       A roll whose close does not fill therefore stays in "closing" and
        #       is visible as such, instead of advancing on schedule into a switch
        #       that would strand the old contract's lot.
        self._ladder_market = market
        if self._roll_state == "requested":
            if self.trader.position == 0:
                self._roll_state = "switch"
                return
            self._roll_state = "closing"
            order, ticks = self._submit_first_rung(market)
            if self.recorder is not None:
                self.recorder.record(
                    "roll_close_submitted", timestamp=market.timestamp,
                    ticks_through=ticks,
                    quantity=None if order is None else order.quantity,
                    price=None if order is None else order.limit_price)
            return

        if self._roll_state == "closing" and self.trader.position == 0:
            self._note_roll_fill("roll_leg_closed")
            self._roll_state = "switch"
            self._reset_ladder()
            return

        if self._roll_state == "reopening" and self.trader.position != 0:
            self._note_roll_fill("roll_leg_opened")
            self._roll_state = None
            self._reset_ladder()

    def _submit_first_rung(self, market: Any):
        # Tech: submit the current roll leg at rung 1 and start the ladder's
        #       clock from it; return the order and its aggression.
        # Why:  both legs start the same way, so the re-open is not left to a
        #       plain order that `has_pending` would hold the ladder behind.
        #       Starting the clock here is what makes rung 2 wait out
        #       `ROLL_LADDER_INTERVAL` instead of firing on the next second.
        self._reset_ladder()
        self._ladder_attempt = 1
        self._ladder_last = _monotonic()
        rolling = getattr(self.broker, "rolling", None)
        ticks = ladder_ticks(1)
        if rolling is None:
            order = self._submit_roll_leg(market, ticks)
        else:
            with rolling(ticks):
                order = self._submit_roll_leg(market, ticks)
        return order, ticks

    def _note_roll_fill(self, event: str) -> None:
        # Tech: mark the newest fill as a roll leg and record it.
        # Why:  `build_trades(roll_fills=…)` reads exactly this set to keep the
        #       pair one trade. Taking the *newest* fill is safe because the loop
        #       has just applied it and the state change we reacted to is what it
        #       caused.
        fills = self.loop.fills
        if not fills:
            return
        self.roll_fills.add(fills[-1].timestamp)
        if self.recorder is not None:
            self.recorder.record(event, timestamp=fills[-1].timestamp,
                                 price=fills[-1].fill_price,
                                 position_after=self.trader.position)

    def _resume_rolled_exposure(self, market: Any) -> None:
        # Tech: on the first bar after a roll, re-open the size that was settled,
        #       through the ordinary signal path; disarm either way.
        # Why:  "roll the exposure" is a decision to stay in the market across the
        #       boundary, so the entry has to be a real order on the new contract
        #       rather than a mirror edit — it goes through `on_signal` so the
        #       no-flip machine, the rails and the fill accounting all see an
        #       ordinary entry. Disarming before submitting means a broker that
        #       raises cannot leave this armed to fire again on every later bar.
        want = self._roll_entry
        self._roll_entry = 0
        if want == 0:
            return
        if self.trader.position != 0:
            # Tech: something opened a position before the re-entry could fire.
            # Why:  the strategy's own signal can land inside the roll window —
            #       stride 30 makes it uncommon, not impossible. Its position is
            #       the one to keep (it is a live decision, this is bookkeeping),
            #       but a silent skip would leave no trace of a roll that never
            #       restored what it closed.
            self._roll_state = None
            if self.recorder is not None:
                self.recorder.record("roll_resume_skipped", wanted=want,
                                     position=self.trader.position)
            return
        direction = "BUY" if want > 0 else "SELL"
        # Tech: a scheduled roll's re-open goes out as the ladder's rung 1; a
        #       settled leg's re-entry stays an ordinary signal.
        # Why:  see `_submit_first_rung`. This bar is the first new-month bar
        #       (`switch_contract`), so it is the earliest true reference.
        if self._roll_state == "reopening":
            order, _ticks = self._submit_first_rung(market)
        else:
            with self._trader_roll_leg():
                order = self.trader.on_signal(direction, market, strength=1.0)
        if self.recorder is not None:
            self.recorder.record(
                "roll_exposure_resumed", timestamp=market.timestamp,
                wanted=want, direction=direction,
                quantity=None if order is None else order.quantity,
                price=None if order is None else order.limit_price)
        if self.alerter is not None:
            self.alerter.warn(
                "roll_exposure_resumed",
                f"re-opening {direction} {abs(want)} on the new contract "
                f"after the roll")

    # ---- warm-up -----------------------------------------------------------

    def fetch_warmup_bars(self, api: Any, contract: Any,
                          start: str, end: str, *,
                          cache_path: Any = None,
                          front_month_at: Any = None) -> pd.DataFrame:
        # Tech: backfill through the splicing fetch, supplying this runtime's own
        #       policy. `contract` may be one contract or a sequence of them.
        # Why:  `fetch_bars` deliberately has no session default. Routing the call
        #       through here means the caller never gets to choose one, so the
        #       aggregator and the backfill agree by construction rather than by
        #       convention — the mismatch this class exists to prevent.
        #
        #       Several contracts, collapsed on volume, is the offline loader's
        #       own rule (§2.6): it is what makes a warm-up that spans a roll the
        #       same front-month-continuous series the model was evaluated on,
        #       rather than the new month's thin back-month history. One contract
        #       still goes down the same path and collapses to itself.
        contracts = (list(contract)
                     if isinstance(contract, (list, tuple)) else [contract])
        return fetch_bars_continuous(api, contracts, start, end,
                                     policy=self.policy, cache_path=cache_path,
                                     front_month_at=front_month_at)

    def warm_up(self, bars: pd.DataFrame) -> None:
        # Tech: validate the bars against the policy, prime the emitter by
        #       replaying historical forecasts, pre-warm the model, then seed the
        #       loop's context with the trailing `context_length` bars.
        # Why:  ordering matters — priming consumes the *earlier* bars so the
        #       emitter sees the same forecast sequence it would have seen had the
        #       process been running, and the context is seeded last so it ends at
        #       the newest bar.
        if bars.empty:
            raise ValueError("warm_up got no bars — backfill returned nothing "
                             "(over quota returns EMPTY history, not an error)")
        off = [ts for ts in bars["timestamp"] if not self.policy.includes(ts)]
        if off:
            raise ValueError(
                f"{len(off)} warm-up bar(s) fall outside {sorted(self.policy.sessions)} "
                f"(first: {off[0]}). The backfill used a different SessionPolicy "
                f"than this runtime — that mismatch would splice the model context "
                f"from two regimes."
            )

        ctx = self.strategy_params["context_length"]
        if len(bars) < ctx:
            raise ValueError(
                f"warm_up needs at least context_length={ctx} bars, got {len(bars)}. "
                f"A short context still produces plausible forecasts, from an input "
                f"distribution the strategy was never evaluated on."
            )

        self._prime_emitter(bars)
        self._check_primed(len(bars))
        self._prewarm_model(bars.iloc[-ctx:])
        self.loop.seed_history(bars.iloc[-ctx:].reset_index(drop=True))
        self.warm = True
        if self.recorder is not None:
            self.recorder.record(
                "warm_up", bars=len(bars), context=self.loop.history_len(),
                first=bars["timestamp"].iloc[0], last=bars["timestamp"].iloc[-1],
                emitter_primed=self._emitter_samples(),
            )

    def _prime_emitter(self, bars: pd.DataFrame) -> None:
        # Tech: forecast at the last N stride boundaries that still have a full
        #       context behind them, and push each through `emit_signal`,
        #       discarding the result.
        # Why:  replaying through the emitter (rather than poking its deques) is
        #       correct for *any* emitter, because the accumulation lives in the
        #       emitter's own code — `ThresholdEmitter` only appends on BUY/SELL
        #       while `VolMultiTfEmitter` appends on every forecast, and replay
        #       reproduces either faithfully. The results must be discarded: these
        #       are historical bars and acting on them would be trading the past.
        ctx = self.strategy_params["context_length"]
        stride = self.strategy_params["stride_bars"]
        n = len(bars)
        boundaries = sorted(
            b for b in range(n - 1, ctx - 2, -stride)
            if b >= ctx - 1
        )[-self.emitter_prime_boundaries:]
        if not boundaries:
            return
        timestamps = list(bars["timestamp"])
        prices = [float(p) for p in bars["price"]]
        forecasts = self.forecaster.forecast_series_batch(
            timestamps, prices, boundaries, batch_size=PRIME_BATCH_SIZE
        )
        self._release_gpu_cache()
        for f in forecasts:
            self.emitter.emit_signal(f)

    def _release_gpu_cache(self) -> None:
        # Tech: return the CUDA caching allocator's unused blocks to the driver,
        #       when the forecaster has loaded torch and a GPU is present.
        # Why:  the priming batch is the largest allocation this process will
        #       ever make; live forecasts are batch 1 and need only the model's
        #       ~4.2 GiB. Without this the batch's working set stays reserved
        #       for the whole soak and is invisible to every other process on
        #       the card. The torch handle is read off the forecaster (Toto2
        #       keeps it on `_torch` after its lazy import) so this module still
        #       never imports torch, and a CPU or test forecaster is a no-op.
        torch = getattr(self.forecaster, "_torch", None)
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _priming_required(self) -> bool:
        # Tech: priming only matters for an emitter that (a) keeps a rolling
        #       sample window and (b) has the filter that reads it enabled.
        # Why:  `VolMultiTfEmitter` appends on every forecast so it primes
        #       trivially; a `ThresholdEmitter` with `trend_filter=False` has no
        #       filter to leave open. Checking the condition rather than the class
        #       keeps this from becoming an emitter registry.
        return (
            getattr(self.emitter, "_short_prices", None) is not None
            and bool(getattr(self.emitter, "trend_filter", False))
        )

    def _check_primed(self, n_bars: int) -> None:
        # Tech: refuse to start when replay failed to accumulate enough samples
        #       for the trend filter to actually evaluate.
        # Why:  MEASURED from each pinned run's signals on the @2026-09-01 tape —
        #       `_trend_confirms` appends once per non-HOLD emission, and those
        #       are 4.11 % of NIGHT's 43 674 boundaries and 2.61 % of DAY's
        #       15 687. So a quiet stretch primes nothing. The statistic that
        #       governs this is the fewest non-HOLD signals in any K-boundary
        #       window, NOT the longest consecutive HOLD run (385 for NIGHT, 246
        #       for DAY) — samples are counted, not required to be adjacent. At
        #       K=500 those worst windows were 1 (NIGHT) and 3 (DAY) against the
        #       floor of 3, which is why K is now 900 (§2.5).
        #
        #       Starting anyway is the worse option: below 3 samples
        #       `_trend_confirms` returns True — it fails **open**, trading signals
        #       the filter exists to veto. Refusing is recoverable (widen the
        #       window); trading unfiltered is not.
        if not self._priming_required():
            return
        need = 3  # `_trend_confirms` needs an early/late split to compare
        have = self._emitter_samples()
        if have < need:
            raise RuntimeError(
                f"emitter priming incomplete: {have}/{need} samples from "
                f"{n_bars} bars over {self.emitter_prime_boundaries} boundaries. "
                f"The trend filter fails OPEN below {need} samples, so starting "
                f"now would trade signals it exists to veto. The warm-up frame "
                f"held too few non-HOLD signals, so widen it — seed the bar "
                f"cache for this policy (live/seed_cache.py) or raise "
                f"--warmup-days — or raise emitter_prime_boundaries above "
                f"{self.emitter_prime_boundaries}."
            )

    def _prewarm_model(self, window: pd.DataFrame) -> None:
        # Tech: issue one throwaway forecast so the checkpoint is resident.
        # Why:  measured at ~18.8 s cold versus ~0.09 s warm (§2.7). Without this
        #       the *first real signal* pays the load. Skipped when the emitter
        #       priming already forced a load.
        if self.forecaster._model is None:
            self.forecaster.forecast(window.reset_index(drop=True).copy())

    def _emitter_samples(self) -> int:
        deque_ = getattr(self.emitter, "_short_prices", None)
        return len(deque_) if deque_ is not None else -1

    # ---- the live event paths ----------------------------------------------

    def on_tick(self, timestamp: Any, price: float, volume: int = 1, *,
                simtrade: bool = False, code: str = "") -> None:
        # Tech: capture the raw print first, then feed the aggregator and run the
        #       loop for any completed bar; note genuine data for the watchdog.
        # Why:  capture comes **before** every filter on purpose — simtrade and
        #       out-of-session ticks are written too, because a file containing
        #       only what survived the filter cannot be used to audit the filter
        #       (V1's live half). Refuses to run before `warm_up`: trading on a
        #       partial context is what the whole start-up sequence prevents.
        if not self.warm:
            raise RuntimeError("on_tick before warm_up — context is not loaded")
        if self.tick_capture is not None:
            self.tick_capture.write(timestamp, price, volume,
                                    simtrade=simtrade, code=code)
        # Tech: after a roll, a print of another month is captured and dropped.
        # Why:  see `switch_contract`. It is not data either: counting it would
        #       let the old month's last prints keep the staleness rail quiet.
        if (self._contract_codes is not None and code
                and code not in self._contract_codes):
            self.n_foreign_dropped += 1
            return
        if not (simtrade and self.aggregator.drop_simtrade):
            self.rails.note_data(timestamp)
        for bar in self.aggregator.add_tick(timestamp, price, volume,
                                            simtrade=simtrade):
            self.loop.on_bar(bar)
            # Tech: after the bar has been processed normally, advance a scheduled
            #       roll and re-open any exposure a roll closed or settled.
            # Why:  §2.6. Both run *after* `on_bar` so orders are submitted
            #       against a bar the loop has already seen — fills, rails and
            #       reconciliation all ran first, exactly as they would for any
            #       other order placed on that bar.
            if self._roll_state is not None:
                self._advance_roll(bar)
            if self._roll_entry:
                self._resume_rolled_exposure(bar)

    def on_clock(self, now: Any) -> None:
        # Tech: wall-clock driven work — close a quiet bar, then run the staleness
        #       watchdog.
        # Why:  anything that must happen when bars *stop* cannot be driven by
        #       bars. A quiet minute would otherwise hold a completed bar hostage
        #       until the next trade, and a frozen feed produces no event at all.
        for bar in self.aggregator.flush(now):
            self.loop.on_bar(bar)
            if self._roll_state is not None:
                self._advance_roll(bar)
            if self._roll_entry:
                self._resume_rolled_exposure(bar)
        self.rails.check_staleness(now)

    def shutdown(self, now: Any = None) -> dict:
        # Tech: close the session out and report what state it ended in, flagging
        #       a position left open after a FLATTEN halt as a distinct failure.
        # Why:  "submitted a close" is not "flat" (§2.12). An order is `LMT`+`ROD`,
        #       and ROD orders are cancelled at session end — so a flatten fired
        #       near the close can silently fail, leaving the position on with the
        #       loop blind until the next session. Whatever else happens, that
        #       must never look like success: it is reported here, recorded, and
        #       surfaced as a non-zero exit by the CLI.
        pos = self.trader.position
        unflattened = bool(pos != 0 and self.rails.flatten_required)
        status = {
            "position_at_close": pos,
            "halted": self.rails.halted,
            "halt_reason": self.rails.halt_reason,
            "halt_action": getattr(self.rails.halt_action, "value", None),
            "flatten_incomplete": unflattened,
            "pending_order": self.broker.pending_order() is not None,
        }
        if self.recorder is not None:
            # Merged, not double-splatted: both dicts carry `halted`/`halt_reason`,
            # and status wins.
            self.recorder.record("session_end", **{**self.summary(), **status})
        return status

    def result(self):
        return self.loop.result()

    def summary(self) -> dict:
        r = self.loop.result()
        return {
            "config": self.config.describe(),
            "simulation": self.simulation,
            "bars": r.n_bars,
            "forecasts": r.n_forecasts,
            "signals": r.n_signals,
            "orders": r.n_orders,
            "fills": len(r.fills),
            "position": self.trader.position,
            "net_pnl_points": self.portfolio.net_pnl(),
            "halted": self.rails.halted,
            "halt_reason": self.rails.halt_reason,
            "ticks": self.aggregator.n_ticks,
            "simtrade_dropped": self.aggregator.n_simtrade_dropped,
            "late_dropped": self.aggregator.n_late_dropped,
            "off_session_dropped": self.aggregator.n_bars_dropped_off_session,
            "ticks_captured": getattr(self.tick_capture, "n", 0),
            # Tech: how many fills were contract-roll legs, and whether one is
            #       still in flight at shutdown.
            # Why:  §2.6. The live path never builds trades — it reports the
            #       portfolio's running PnL — so these two numbers are the only
            #       place a roll shows up in the summary. `roll_legs` is also what
            #       an offline `compute_metrics(roll_fills=…)` needs to book the
            #       pair as one round-trip, and it is written to the trail so a
            #       session can be re-scored months later.
            "roll_legs": len(self.roll_fills),
            "roll_in_flight": self._roll_state,
        }


def make_sim_runtime(config: LiveConfig, **kw) -> LiveRuntime:
    """A runtime backed by the offline fill model — for replay and parity work."""
    execution = Execution(fee_rate=config.fee_rate)
    return LiveRuntime(config, broker=SimBroker(execution), **kw)

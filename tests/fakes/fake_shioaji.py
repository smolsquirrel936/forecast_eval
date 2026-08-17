"""FakeShioaji — a scriptable stand-in for the Shioaji API object.

Exists so the adapter's hard cases can be driven deterministically in CI with no
network, no credentials and no `shioaji` install. It deliberately does **not**
simulate a matching engine: tests state exactly which callbacks fire, in which
order, so the nasty sequences the docs warn about (deal before order, duplicate
deals, cancel races) are reproduced on demand rather than hoped for.

Payload shapes mirror the documented futures callbacks:
  OrderState.FuturesOrder -> {operation{op_type, op_code, op_msg}, order{...},
                              status{...}, contract{...}}
  OrderState.FuturesDeal  -> {trade_id, seqno, ordno, exchange_seq, action,
                              code, price, quantity, ts, ...}
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional


# ---- the `sj` namespace the adapter builds orders from ----------------------

class Action(str, Enum):
    Buy = "Buy"
    Sell = "Sell"


class FuturesPriceType(str, Enum):
    LMT = "LMT"
    MKT = "MKT"
    MKP = "MKP"


class OrderType(str, Enum):
    ROD = "ROD"
    IOC = "IOC"
    FOK = "FOK"


class FuturesOCType(str, Enum):
    Auto = "Auto"
    New = "New"
    Cover = "Cover"
    DayTrade = "DayTrade"


class OrderState(str, Enum):
    # VERIFIED 2026-08-10 against the installed SDK: names are FuturesOrder /
    # FuturesDeal, values are the uppercase short forms. Note the values contain
    # no mixed-case "Order"/"Deal" substring, which is why the adapter upper-cases
    # before matching.
    StockOrder = "SORDER"
    StockDeal = "SDEAL"
    FuturesOrder = "FORDER"
    FuturesDeal = "FDEAL"


class FuturesOrder(SimpleNamespace):
    """Captures the kwargs the adapter passed, for assertion."""


FakeSj = SimpleNamespace(
    Action=Action,
    FuturesPriceType=FuturesPriceType,
    OrderType=OrderType,
    FuturesOCType=FuturesOCType,
    FuturesOrder=FuturesOrder,
    OrderState=OrderState,
)


# ---- contracts / positions --------------------------------------------------

def make_contract(code="TXFH6", delivery_month="202608", target_code=None):
    # TXF month letters are plain alphabetical, A=Jan .. L=Dec (measured
    # 2026-08-10), NOT the CME F/G/H/J/K/M/N/Q/U/V/X/Z convention — so H is
    # August, not March. Codes here are kept consistent with that.
    return SimpleNamespace(code=code, delivery_month=delivery_month,
                           target_code=target_code or code,
                           security_type="FUT")


def make_alias_contract():
    """The `TXFR1` alias as the API really presents it: its own `code` is the
    alias, while every order, callback and position comes back stamped with the
    resolved concrete code (measured 2026-08-10)."""
    return SimpleNamespace(code="TXFR1", delivery_month="202608",
                           security_type="FUT")


class FakePosition(SimpleNamespace):
    pass


# ---- the api object ---------------------------------------------------------

class FakeShioaji:
    def __init__(self, *, simulation: bool = True):
        self.simulation = simulation
        self.futopt_account = SimpleNamespace(account_id="F123456",
                                              broker_id="F002000")
        self._callback: Optional[Callable[[Any, Dict], None]] = None
        self._seq = 0

        # Everything the tests assert against.
        self.placed: List[Any] = []          # FuturesOrder objects, in order
        self.cancelled: List[Any] = []       # Trade objects passed to cancel
        self.trades: List[Any] = []
        self.positions: List[FakePosition] = []
        self.place_order_error: Optional[Exception] = None
        self.cancel_order_error: Optional[Exception] = None
        self.list_positions_error: Optional[Exception] = None

    # -- API surface the adapter uses --

    def set_order_callback(self, cb):
        self._callback = cb

    def place_order(self, contract, order, **kw):
        if self.place_order_error is not None:
            raise self.place_order_error
        self._seq += 1
        oid = f"id{self._seq:04d}"
        trade = SimpleNamespace(
            contract=contract,
            order=SimpleNamespace(id=oid, seqno=f"{self._seq:06d}", ordno=""),
            status=SimpleNamespace(status="PendingSubmit", deal_quantity=0,
                                   deals=[]),
        )
        self.placed.append(order)
        self.trades.append(trade)
        return trade

    def cancel_order(self, trade, **kw):
        if self.cancel_order_error is not None:
            raise self.cancel_order_error
        self.cancelled.append(trade)

    def update_status(self, account=None, **kw):
        return None

    def list_trades(self):
        return list(self.trades)

    def list_positions(self, account=None, **kw):
        if self.list_positions_error is not None:
            raise self.list_positions_error
        return list(self.positions)

    # -- scripting helpers --

    def emit_order(self, *, trade=None, op_type="New", op_code="00", op_msg="",
                   ordno="N0001", order_id=None, quantity=1, price=17500.0,
                   action="Buy", code="TXFH6"):
        """Fire an OrderState.FuturesOrder callback."""
        if order_id is None and trade is not None:
            order_id = trade.order.id
        if trade is not None and ordno:
            trade.order.ordno = ordno
        self._emit(OrderState.FuturesOrder, {
            "operation": {"op_type": op_type, "op_code": op_code,
                          "op_msg": op_msg},
            "order": {"id": order_id, "seqno": "000001", "ordno": ordno,
                      "action": action, "price": price, "quantity": quantity,
                      "order_type": "ROD", "price_type": "LMT",
                      "market_type": "Day", "oc_type": "New"},
            "status": {"id": order_id, "exchange_ts": 0, "modified_price": 0,
                       "cancel_quantity": 0, "order_quantity": quantity},
            "contract": {"code": code, "delivery_month": "202608"},
        })

    def emit_deal(self, *, trade=None, ordno="N0001", exchange_seq="000001",
                  trade_id=None, action="Buy", price=17500.0, quantity=1,
                  ts=None, code="TXFH6"):
        """Fire an OrderState.FuturesDeal callback."""
        if trade_id is None and trade is not None:
            trade_id = trade.order.id
        self._emit(OrderState.FuturesDeal, {
            "trade_id": trade_id,
            "seqno": "000001",
            "ordno": ordno,
            "exchange_seq": exchange_seq,
            "action": action,
            "code": code,
            "price": price,
            "quantity": quantity,
            "ts": ts if ts is not None else datetime(2026, 3, 5, 9, 0, 0),
            "security_type": "FUT",
            "delivery_month": "202608",
            "market_type": "Day",
        })

    def _emit(self, state, msg):
        if self._callback is None:
            raise AssertionError("no order callback registered")
        self._callback(state, msg)

from decimal import Decimal
from threading import RLock
from types import SimpleNamespace

from app.domain.states import Direction
from app.trading.tactical import TacticalPosition, TacticalTrader

D = Decimal


class Audit:
    def __init__(self):
        self.events = []

    def emit(self, code, level="INFO", **payload):
        self.events.append((code, level, payload))


class DB:
    def __init__(self):
        self.closed_trades = []
        self.deleted = []

    def update_order_state(self, *args, **kwargs):
        return None

    def save_tactical_trade(self, *args, **kwargs):
        self.closed_trades.append(args)

    def delete_tactical_position(self, symbol):
        self.deleted.append(symbol)

    def save_tactical_position(self, *args, **kwargs):
        return None


def test_wait_for_fill_applies_confirmed_cumulative_partial_fill(monkeypatch, instrument):
    trader = TacticalTrader.__new__(TacticalTrader)
    trader.config = SimpleNamespace(tactical_order_confirm_seconds=1)
    trader.db = DB()
    trader.audit = Audit()
    payload = {
        "status": "canceled",
        "vol": "10",
        "vol_exec": "4",
        "avg_price": "100",
        "txid": "O-TACTICAL",
    }
    calls = []

    def cancel_and_reconcile(intent, **kwargs):
        calls.append((intent.client_order_id, kwargs))
        return {
            "state": "CANCELED",
            "terminal": True,
            "payload": payload,
            "kraken_order_id": "O-TACTICAL",
            "executed_volume": "4",
            "requested_volume": "10",
        }

    trader.authority = SimpleNamespace(cancel_and_reconcile=cancel_and_reconcile)
    trader.gateway = SimpleNamespace(lookup_order=lambda **kwargs: [])
    clock = iter((0.0, 2.0))
    monkeypatch.setattr("app.trading.tactical.time.monotonic", lambda: next(clock))
    intent = SimpleNamespace(
        client_order_id="11111111-2222-4333-8444-555555555590",
        quantity=D("10"),
        limit_price=D("99"),
    )

    fill = trader._wait_for_fill(intent, instrument, "O-TACTICAL")

    assert fill == (D("4"), D("100"))
    assert calls and calls[0][0] == intent.client_order_id
    assert any(event[0] == "TACTICAL_PARTIAL_FILL_RECONCILED" for event in trader.audit.events)


def test_close_live_clamps_exchange_fill_to_remaining_local_position(instrument):
    trader = TacticalTrader.__new__(TacticalTrader)
    trader.config = SimpleNamespace(
        tactical_entry_fee_bps=80,
        tactical_exit_fee_bps=80,
        tactical_cooldown_seconds=120,
    )
    trader.db = DB()
    trader.audit = Audit()
    trader._lock = RLock()
    trader._positions = {}
    trader._cooldown_until = 0
    position = TacticalPosition(
        symbol=instrument.symbol,
        venue="spot",
        direction=Direction.LONG,
        quantity=D("10"),
        entry_price=D("100"),
        peak_price=D("105"),
        trough_price=D("99"),
        notional_eur=D("1000"),
        leverage=D("1"),
        opened_at=1.0,
        entry_client_order_id="11111111-2222-4333-8444-555555555591",
        setup_score=D("250"),
        state="OPEN",
    )
    trader._positions[position.symbol] = position

    trader._close_live(
        position, D("12"), D("101"), "PARTIAL_FILL_TEST", 100.0,
        "11111111-2222-4333-8444-555555555592",
    )

    assert trader.db.deleted == [instrument.symbol]
    assert trader.db.closed_trades[0][5] == D("10")
    assert position.quantity == D("10")
    assert any(event[0] == "TACTICAL_EXIT_FILL_CLAMPED" for event in trader.audit.events)

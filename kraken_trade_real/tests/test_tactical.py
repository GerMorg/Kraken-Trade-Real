from __future__ import annotations

from collections import deque
from decimal import Decimal
from types import SimpleNamespace
import json

from app.domain.states import Direction
from app.persistence import Database
from app.kraken.ws import WebSocketSupervisor, _book_checksum
from app.trading.tactical import TacticalEngine, TacticalPosition, TacticalStrategy


D = Decimal


def _history(now: float, direction: int) -> deque[tuple[float, D]]:
    history: deque[tuple[float, D]] = deque(maxlen=720)
    price = D("100")
    for index in range(48):
        ts = now - 240 + index * 5
        step = D("0.0025") if index % 2 == 0 else D("-0.0005")
        if direction < 0:
            step = -step
        price *= D("1") + step
        history.append((float(ts), price))
    return history


def _trades(now: float, buy_ratio: D) -> list[dict[str, str | float]]:
    rows: list[dict[str, str | float]] = []
    buy_count = int(buy_ratio * D("10"))
    for index in range(10):
        rows.append(
            {
                "timestamp_epoch": now - 140 + index * 10,
                "qty": "1",
                "price": "100",
                "side": "buy" if index < 5 else "sell",
            }
        )
    for index in range(10):
        side = "buy" if index < buy_count else "sell"
        rows.append(
            {
                "timestamp_epoch": now - 29 + index * 2.5,
                "qty": "2",
                "price": "100",
                "side": side,
            }
        )
    return rows


def _evaluate(
    direction: int,
    now: float,
    *,
    entry_fee: D = D("80"),
    exit_fee: D = D("80"),
    spread_bps: D = D("5"),
):
    history = _history(now, direction)
    last = history[-1][1] * (D("1") + (D("0.006") if direction > 0 else D("-0.006")))
    if direction > 0:
        bid, ask = last - D("0.025"), last + D("0.025")
        bids = [(last - D("0.10") * i, D("10")) for i in range(5)]
        asks = [(last + D("0.10") * i, D("5")) for i in range(5)]
        buy_ratio = D("0.8")
    else:
        bid, ask = last - D("0.025"), last + D("0.025")
        bids = [(last - D("0.10") * i, D("5")) for i in range(5)]
        asks = [(last + D("0.10") * i, D("10")) for i in range(5)]
        buy_ratio = D("0.2")
    # Make the test's spread independent of tiny synthetic-price rounding.
    spread = (ask - bid) / ((ask + bid) / D("2")) * D("10000")
    assert spread <= spread_bps + D("1")
    return TacticalStrategy().evaluate(
        symbol="TEST/EUR",
        bid=bid,
        ask=ask,
        last=last,
        history=history,
        trades=_trades(now, buy_ratio),
        bids=bids,
        asks=asks,
        now=now,
        notional_eur=D("12.5"),
        quote_to_eur_rate=D("1"),
        min_net_edge_bps=D("40"),
        max_spread_bps=D(str(spread_bps)),
        min_volume_ratio=D("1.75"),
        min_momentum_30s_bps=D("35"),
        min_momentum_3m_bps=D("60"),
        min_volatility_bps=D("15"),
        min_breakout_bps=D("20"),
        min_imbalance=D("0.08"),
        fee_entry_bps=entry_fee,
        fee_exit_bps=exit_fee,
        safety_buffer_bps=D("30"),
    )


def test_tactical_strategy_generates_long_signal() -> None:
    signal = _evaluate(1, 1_800_000_000)
    assert signal is not None
    assert signal.direction is Direction.LONG
    assert signal.net_edge_bps >= D("40")
    assert signal.estimated_cost_bps >= D("190")


def test_tactical_strategy_generates_short_signal() -> None:
    signal = _evaluate(-1, 1_800_000_000)
    assert signal is not None
    assert signal.direction is Direction.SHORT
    assert signal.net_edge_bps >= D("40")


def test_tactical_strategy_rejects_when_cost_destroys_edge() -> None:
    signal = _evaluate(
        1,
        1_800_000_000,
        entry_fee=D("1000"),
        exit_fee=D("1000"),
    )
    assert signal is None


def test_tactical_strategy_rejects_wide_spread() -> None:
    history = _history(1_800_000_000, 1)
    signal = TacticalStrategy().evaluate(
        symbol="TEST/EUR",
        bid=D("99"),
        ask=D("101"),
        last=D("100"),
        history=history,
        trades=_trades(1_800_000_000, D("0.8")),
        bids=[(D("99"), D("10"))] * 5,
        asks=[(D("101"), D("5"))] * 5,
        now=1_800_000_000,
        notional_eur=D("12.5"),
        quote_to_eur_rate=D("1"),
        min_net_edge_bps=D("40"),
        max_spread_bps=D("25"),
        min_volume_ratio=D("1.75"),
        min_momentum_30s_bps=D("35"),
        min_momentum_3m_bps=D("60"),
        min_volatility_bps=D("15"),
        min_breakout_bps=D("20"),
        min_imbalance=D("0.08"),
        fee_entry_bps=D("80"),
        fee_exit_bps=D("80"),
        safety_buffer_bps=D("30"),
    )
    assert signal is None


def test_short_leverage_uses_supported_level_without_exceeding_ceiling() -> None:
    engine = object.__new__(TacticalEngine)
    engine.config = SimpleNamespace(
        tactical_max_leverage=2.0,
        tactical_short_leverage=2.0,
    )
    instrument = SimpleNamespace(
        short_available=True,
        product_type=SimpleNamespace(value="SPOT_MARGIN"),
        leverage_levels=(D("1"), D("3")),
        max_leverage=D("3"),
    )
    assert engine._choose_leverage(instrument, Direction.SHORT) is None

    instrument = SimpleNamespace(
        short_available=True,
        product_type=SimpleNamespace(value="SPOT_MARGIN"),
        leverage_levels=(D("1"), D("2"), D("3")),
        max_leverage=D("3"),
    )
    assert engine._choose_leverage(instrument, Direction.SHORT) == D("2")


def test_tactical_position_persistence(tmp_path) -> None:
    db = Database(str(tmp_path / "trader.db"))
    position = TacticalPosition(
        symbol="TEST/EUR",
        direction=Direction.SHORT,
        quantity=D("0.1"),
        entry_price=D("100"),
        entry_notional_eur=D("12.5"),
        opened_at=1_800_000_000,
        peak_price=D("100"),
        leverage=D("2"),
        margin=True,
        last_price=D("100"),
    )
    db.save_tactical_position(position)
    rows = db.tactical_positions()
    assert len(rows) == 1
    assert rows[0]["direction"] == "SHORT"
    db.delete_tactical_position("TEST/EUR")
    assert db.tactical_positions() == []


class _Audit:
    def emit(self, *args, **kwargs):
        return None


class _Recovery:
    def issue(self, *args, **kwargs):
        return None


def test_websocket_book_checksum_matches_kraken_example() -> None:
    stream = WebSocketSupervisor(_Audit(), _Recovery())
    stream.set_symbols(["BTC/USD"])
    bids = [
        {"price": "45283.5", "qty": "0.10000000"},
        {"price": "45283.4", "qty": "1.54582015"},
        {"price": "45282.1", "qty": "0.10000000"},
        {"price": "45281.0", "qty": "0.10000000"},
        {"price": "45280.3", "qty": "1.54592586"},
        {"price": "45279.0", "qty": "0.07990000"},
        {"price": "45277.6", "qty": "0.03310103"},
        {"price": "45277.5", "qty": "0.30000000"},
        {"price": "45277.3", "qty": "1.54602737"},
        {"price": "45276.6", "qty": "0.15445238"},
    ]
    asks = [
        {"price": "45285.2", "qty": "0.00100000"},
        {"price": "45286.4", "qty": "1.54571953"},
        {"price": "45286.6", "qty": "1.54571109"},
        {"price": "45289.6", "qty": "1.54560911"},
        {"price": "45290.2", "qty": "0.15890660"},
        {"price": "45291.8", "qty": "1.54553491"},
        {"price": "45294.7", "qty": "0.04454749"},
        {"price": "45296.1", "qty": "0.35380000"},
        {"price": "45297.5", "qty": "0.09945542"},
        {"price": "45299.5", "qty": "0.18772827"},
    ]
    expected = 3310070434
    assert _book_checksum(
        {Decimal(row["price"]): Decimal(row["qty"]) for row in bids},
        {Decimal(row["price"]): Decimal(row["qty"]) for row in asks},
    ) == expected
    stream.on_message(json.dumps({
        "channel": "book",
        "type": "snapshot",
        "data": [{
            "symbol": "BTC/USD",
            "bids": bids,
            "asks": asks,
            "checksum": expected,
            "timestamp": "2023-10-06T17:35:55.440295Z",
        }],
    }))
    snapshot = stream.snapshots()["BTC/USD"]
    assert snapshot["book_ready"] is True
    assert len(snapshot["bids"]) == 10
    assert len(snapshot["asks"]) == 10


def test_websocket_book_checksum_mismatch_invalidates_book() -> None:
    stream = WebSocketSupervisor(_Audit(), _Recovery())
    stream.set_symbols(["BTC/USD"])
    stream.on_message(json.dumps({
        "channel": "book",
        "type": "snapshot",
        "data": [{
            "symbol": "BTC/USD",
            "bids": [{"price": "100.0", "qty": "1.0"}],
            "asks": [{"price": "101.0", "qty": "1.0"}],
            "checksum": 123,
            "timestamp": "2023-10-06T17:35:55.440295Z",
        }],
    }))
    assert stream.snapshots()["BTC/USD"]["book_ready"] is False

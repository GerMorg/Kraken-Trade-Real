from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
import time

from app.domain.models import PortfolioState, Signal
from app.domain.states import Direction, ProductType
from app.portfolio.reconcile import PortfolioReconciler
from app.trading.profit_protection import PositionProfitProtection

D = Decimal


class FakeAudit:
    def __init__(self):
        self.events = []

    def emit(self, code, level="INFO", **payload):
        self.events.append((code, level, payload))


def _lot(db, instrument, *, order_id="OOPEN", decision_id="DOPEN",
         quantity="1", entry_price="100", opened_at=None, margin=False):
    opened_at = time.time() if opened_at is None else opened_at
    db.execute(
        """INSERT INTO core_inventory_lots(
             lot_id,symbol,direction,opened_at,entry_order_id,entry_trade_id,
             opening_decision_id,entry_price,remaining_quantity,
             remaining_entry_fee_est_quote,remaining_entry_fee_verified_quote,
             entry_fee_verified,quote_asset,detail_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"lot-{order_id}-{decision_id}", instrument.symbol, "LONG", opened_at,
            order_id, f"trade-{order_id}", decision_id, entry_price, quantity,
            "0.1", "0", 0, instrument.quote,
            json.dumps({"leverage": "1", "margin": margin}),
        ),
    )


def _reconcile_cash_wallet(db, fake_gateway, instrument, wallet_quantity):
    fake_gateway.spot_balance_extended = lambda: {
        "ZEUR": {
            "balance": "50", "credit": "0", "credit_used": "0", "hold_trade": "0",
        },
        "XXBT": {
            "balance": str(wallet_quantity), "credit": "0", "credit_used": "0", "hold_trade": "0",
        },
    }
    fake_gateway.spot_private = lambda method, params=None: (
        {"eb": "150", "tb": "150", "e": "150", "m": "0", "mf": "150", "ml": "9999"}
        if method == "TradeBalance" else {}
    )
    fake_gateway.spot_open_positions = lambda: {}
    reconciler = PortfolioReconciler(fake_gateway, db)
    reconciler.set_market_context(
        [instrument],
        {
            instrument.instrument_id: {
                "b": ["109"], "a": ["111"], "c": ["110"],
            }
        },
    )
    return reconciler.reconcile()


def test_cash_spot_mark_to_market_requires_an_exact_fifo_wallet_match(
    db, fake_gateway, instrument
):
    cash_instrument = replace(instrument, product_type=ProductType.SPOT)
    _lot(db, cash_instrument, quantity="1", entry_price="100")

    matched = _reconcile_cash_wallet(db, fake_gateway, cash_instrument, "1")
    assert matched.spot_wallet_read_ok is True
    assert matched.spot_open_positions_read_ok is True
    assert matched.spot_cash_position_symbols == (cash_instrument.symbol,)
    assert matched.position_quantity[cash_instrument.symbol] == D("1")
    assert matched.position_basis_eur[cash_instrument.symbol] == D("100")
    assert matched.position_pnl_eur[cash_instrument.symbol] == D("10")
    assert matched.position_pnl_pct[cash_instrument.symbol] == D("10")

    # An unexplained wallet deposit / quantity delta disables cash protection and
    # path training for the combined holding instead of assigning a false basis.
    unmatched = _reconcile_cash_wallet(db, fake_gateway, cash_instrument, "1.1")
    assert unmatched.spot_cash_position_symbols == ()
    assert cash_instrument.symbol not in unmatched.position_pnl_pct


def test_cash_spot_stop_loss_only_applies_to_proven_app_managed_inventory(
    db, config, instrument
):
    cash_instrument = replace(instrument, product_type=ProductType.SPOT)
    signal = Signal(
        cash_instrument.symbol, Direction.LONG, D("180"), D("60"), D("0.9"),
        "TREND_UP", D("0"), D("0"), {"volatility": D("4")},
    )
    manager = PositionProfitProtection(config, db, FakeAudit())
    portfolio = PortfolioState(
        equity_eur=D("150"),
        cash_eur=D("50"),
        positions={cash_instrument.symbol: D("100")},
        position_pnl_eur={cash_instrument.symbol: D("-3")},
        position_pnl_pct={cash_instrument.symbol: D("-3")},
        position_basis_eur={cash_instrument.symbol: D("100")},
        position_quantity={cash_instrument.symbol: D("1")},
        spot_wallet_read_ok=True,
        spot_open_positions_read_ok=True,
        spot_cash_position_symbols=(cash_instrument.symbol,),
    )

    result = manager.apply(
        cycle_id="cash-stop",
        instrument=cash_instrument,
        portfolio=portfolio,
        decision=None,
        long_signal=signal,
        short_signal=Signal(
            cash_instrument.symbol, Direction.SHORT, D("180"), D("60"), D("0.9"),
            "TREND_DOWN", D("0"), D("0"), {"volatility": D("4")},
        ),
        model_version="test",
        config_hash="test",
        min_cost_eur=D("0.5"),
    )
    assert result is not None
    assert result.rationale["position_management_action"] == "STOP_LOSS_EXIT"
    assert result.target_position_eur == D("0")
    assert result.execution_direction == Direction.SHORT
    assert result.reduce_only is True

    # The same prices must not trigger an exit when the wallet balance is not
    # proven to equal the app-managed inventory ledger.
    unowned = replace(
        portfolio,
        spot_cash_position_symbols=(),
        spot_wallet_read_ok=False,
    )
    unchanged = manager.apply(
        cycle_id="cash-stop-unowned",
        instrument=cash_instrument,
        portfolio=unowned,
        decision=result,
        long_signal=signal,
        short_signal=signal,
        model_version="test",
        config_hash="test",
        min_cost_eur=D("0.5"),
    )
    assert unchanged is result


def test_cash_spot_exit_episode_is_learnable_only_for_one_app_managed_order(
    db, config, instrument
):
    cash_instrument = replace(instrument, product_type=ProductType.SPOT)
    cycle_id = "cash-entry-cycle"
    db.start_cycle(cycle_id, "test-hash")
    cycle = db.one("SELECT started_at FROM cycles WHERE cycle_id=?", (cycle_id,))
    opened_at = max(time.time(), float(cycle["started_at"]) + 0.1)
    decision_id = "DOPEN"
    order_id = "OOPEN"
    db.execute(
        """INSERT INTO decisions(
             decision_id,created_at,symbol,direction,target_notional_eur,leverage,
             expected_return_bps,expected_cost_bps,confidence,regime,news_effect_bps,
             gemini_effect_bps,strategy_version,model_version,config_hash,rationale_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            decision_id, opened_at, cash_instrument.symbol, "LONG", "100", "1",
            "250", "100", "0.8", "TRENDING", "0", "0", "core-v2", "baseline-v1",
            "test-hash", "{}",
        ),
    )
    db.execute(
        """INSERT INTO orders(
             intent_id,client_order_id,created_at,decision_id,symbol,direction,side,
             order_type,quantity,limit_price,leverage,margin,reduce_only,post_only,
             state,submitted_at,kraken_order_id,expected_edge_bps,max_slippage_bps,
             expires_seconds,last_error
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "cash-open-intent", "cash-open-client", opened_at, decision_id,
            cash_instrument.symbol, "LONG", "buy", "market", "1", None, "1",
            0, 0, 0, "FILLED", opened_at, order_id, "150", "25", 60, "",
        ),
    )
    _lot(
        db, cash_instrument, order_id=order_id, decision_id=decision_id,
        quantity="1", entry_price="100", opened_at=opened_at,
    )
    portfolio = PortfolioState(
        equity_eur=D("150"),
        cash_eur=D("50"),
        positions={cash_instrument.symbol: D("110")},
        position_pnl_eur={cash_instrument.symbol: D("10")},
        position_pnl_pct={cash_instrument.symbol: D("10")},
        position_basis_eur={cash_instrument.symbol: D("100")},
        position_quantity={cash_instrument.symbol: D("1")},
        spot_wallet_read_ok=True,
        spot_open_positions_read_ok=True,
        spot_cash_position_symbols=(cash_instrument.symbol,),
    )
    manager = PositionProfitProtection(config, db, FakeAudit())
    manager.observe(portfolio, cycle_id)

    episode = db.active_core_exit_episode(cash_instrument.symbol)
    assert episode is not None
    assert episode["eligible_for_learning"] == 1
    assert episode["open_order_ids_json"] == '["OOPEN"]'
    assert json.loads(episode["detail_json"])["source"] == "APP_MANAGED_SPOT_WALLET_LEDGER"
    path = db.core_exit_path(episode["episode_id"])
    assert len(path) == 1
    quality = json.loads(path[0]["quality_json"])
    assert quality["open_positions_read_ok"] is True
    assert quality["direction"] == "LONG"
    assert quality["open_order_ids"] == ["OOPEN"]
    assert quality["open_lot_count"] == 1

    # A wallet read failure must not close the episode or erase its high-water mark.
    stale_wallet = replace(
        portfolio,
        spot_wallet_read_ok=False,
        spot_cash_position_symbols=(),
    )
    manager.observe(stale_wallet, "cash-wallet-read-failed")
    assert db.active_core_exit_episode(cash_instrument.symbol) is not None

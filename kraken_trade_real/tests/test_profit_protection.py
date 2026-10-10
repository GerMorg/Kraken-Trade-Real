from decimal import Decimal

from app.domain.models import Decision, PortfolioState, Signal
from app.domain.states import Direction
from app.trading.profit_protection import PositionProfitProtection

D = Decimal


class FakeAudit:
    def __init__(self):
        self.events = []

    def emit(self, code, level="INFO", **payload):
        self.events.append((code, level, payload))


def signal_for(instrument, direction):
    return Signal(
        instrument.symbol,
        direction,
        D("180"),
        D("60"),
        D("0.9"),
        "TREND_DOWN" if direction == Direction.SHORT else "TREND_UP",
        D("0"),
        D("0"),
        {"volatility": D("4")},
    )


def portfolio_for(instrument, position, pnl_pct, *, read_ok=True):
    return PortfolioState(
        equity_eur=D("100"),
        cash_eur=D("80"),
        positions={instrument.symbol: D(str(position))},
        gross_eur=abs(D(str(position))),
        net_eur=D(str(position)),
        position_pnl_eur={instrument.symbol: D("0.8")},
        position_pnl_pct={instrument.symbol: D(str(pnl_pct))},
        position_basis_eur={instrument.symbol: D("20")},
        position_quantity={instrument.symbol: D("1")},
        spot_open_positions_read_ok=read_ok,
        spot_margin_position_symbols=(instrument.symbol,) if read_ok else (),
    )


def test_partial_take_profit_reduces_short_by_configured_fraction(db, config, instrument):
    audit = FakeAudit()
    manager = PositionProfitProtection(config, db, audit)
    portfolio = portfolio_for(instrument, "-20", "4.0")
    manager.observe(portfolio, "cycle-partial")

    decision = manager.apply(
        cycle_id="cycle-partial",
        instrument=instrument,
        portfolio=portfolio,
        decision=None,
        long_signal=signal_for(instrument, Direction.LONG),
        short_signal=signal_for(instrument, Direction.SHORT),
        model_version="test",
        config_hash="test",
        min_cost_eur=D("0.5"),
    )

    assert decision is not None
    assert decision.rationale["position_management_action"] == "PARTIAL_TAKE_PROFIT"
    assert decision.current_position_eur == D("-20")
    assert decision.target_position_eur == D("-10")
    assert decision.target_notional_eur == D("10")
    assert decision.execution_direction == Direction.LONG
    assert decision.reduce_only is True
    assert any(event[0] == "POSITION_PROFIT_PROTECTION_TRIGGERED" for event in audit.events)


def test_trailing_profit_exit_uses_persisted_peak_not_current_profit(db, config, instrument):
    audit = FakeAudit()
    manager = PositionProfitProtection(config, db, audit)
    peak_portfolio = portfolio_for(instrument, "-20", "8.0")
    manager.observe(peak_portfolio, "cycle-peak")
    faded_portfolio = portfolio_for(instrument, "-20", "4.0")
    manager.observe(faded_portfolio, "cycle-fade")

    decision = manager.apply(
        cycle_id="cycle-fade",
        instrument=instrument,
        portfolio=faded_portfolio,
        decision=None,
        long_signal=signal_for(instrument, Direction.LONG),
        short_signal=signal_for(instrument, Direction.SHORT),
        model_version="test",
        config_hash="test",
        min_cost_eur=D("0.5"),
    )

    assert decision is not None
    assert decision.rationale["position_management_action"] == "TRAILING_PROFIT_EXIT"
    assert decision.target_position_eur == D("0")
    assert decision.execution_direction == Direction.LONG
    assert decision.reduce_only is True
    state = db.one(
        "SELECT peak_profit_pct FROM position_profit_state WHERE symbol=?",
        (instrument.symbol,),
    )
    assert D(state["peak_profit_pct"]) == D("8.0")


def test_partial_profit_prevents_immediate_rebuild_of_same_margin_position(
    db, config, instrument
):
    manager = PositionProfitProtection(config, db, FakeAudit())
    portfolio = portfolio_for(instrument, "-10", "1.0")
    db.execute(
        """INSERT INTO position_profit_state
           (symbol,peak_profit_pct,partial_taken,pending_order_id,
            pending_start_position_eur,updated_at)
           VALUES(?,?,?,?,?,strftime('%s','now'))""",
        (instrument.symbol, "4.0", 1, "", "0"),
    )
    base_signal = signal_for(instrument, Direction.SHORT)
    attempted_reentry = Decision(
        decision_id="decision-reentry",
        instrument=instrument,
        signal=base_signal,
        target_notional_eur=D("10"),
        leverage=D("2"),
        rationale={"risk_profile": "core"},
        strategy_version="test",
        model_version="test",
        config_hash="test",
        current_position_eur=D("-10"),
        target_position_eur=D("-20"),
        execution_direction=Direction.SHORT,
        reduce_only=False,
    )

    result = manager.apply(
        cycle_id="cycle-reentry",
        instrument=instrument,
        portfolio=portfolio,
        decision=attempted_reentry,
        long_signal=signal_for(instrument, Direction.LONG),
        short_signal=base_signal,
        model_version="test",
        config_hash="test",
        min_cost_eur=D("0.5"),
    )

    assert result is None


def test_failed_open_positions_read_does_not_erase_profit_high_water_mark(
    db, config, instrument
):
    manager = PositionProfitProtection(config, db, FakeAudit())
    manager.observe(portfolio_for(instrument, "-20", "8.0"), "cycle-1")
    manager.observe(
        portfolio_for(instrument, "-20", "0", read_ok=False),
        "cycle-read-failure",
    )
    row = db.one(
        "SELECT peak_profit_pct FROM position_profit_state WHERE symbol=?",
        (instrument.symbol,),
    )
    assert D(row["peak_profit_pct"]) == D("8.0")


def test_stronger_signal_reduction_is_tagged_for_partial_profit_reconciliation(
    db, config, instrument
):
    manager = PositionProfitProtection(config, db, FakeAudit())
    portfolio = portfolio_for(instrument, "-20", "4.0")
    manager.observe(portfolio, "cycle-stronger-reduction")
    signal = signal_for(instrument, Direction.LONG)
    stronger_reduction = Decision(
        decision_id="decision-stronger-reduction",
        instrument=instrument,
        signal=signal,
        target_notional_eur=D("5"),
        leverage=D("2"),
        rationale={"risk_profile": "core"},
        strategy_version="test",
        model_version="test",
        config_hash="test",
        current_position_eur=D("-20"),
        target_position_eur=D("-5"),
        execution_direction=Direction.LONG,
        reduce_only=True,
    )

    decision = manager.apply(
        cycle_id="cycle-stronger-reduction",
        instrument=instrument,
        portfolio=portfolio,
        decision=stronger_reduction,
        long_signal=signal,
        short_signal=signal_for(instrument, Direction.SHORT),
        model_version="test",
        config_hash="test",
        min_cost_eur=D("0.5"),
    )

    assert decision is not None
    assert decision.target_position_eur == D("-5")
    assert decision.reduce_only is True
    assert decision.rationale["position_management_action"] == "PARTIAL_TAKE_PROFIT"

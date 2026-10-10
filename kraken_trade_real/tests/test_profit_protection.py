from decimal import Decimal
import json

from app.config import Config
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
    portfolio = portfolio_for(instrument, "-20", "10.0")
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
    peak_portfolio = portfolio_for(instrument, "-20", "12.0")
    manager.observe(peak_portfolio, "cycle-peak")
    faded_portfolio = portfolio_for(instrument, "-20", "7.0")
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
    assert D(state["peak_profit_pct"]) == D("12.0")


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
    portfolio = portfolio_for(instrument, "-20", "10.0")
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



def test_profit_protection_defaults_use_ten_percent_with_matching_trailing_activation(config):
    assert config.strategy_partial_profit_trigger_pct == 10.0
    assert config.strategy_partial_profit_fraction_pct == 50.0
    assert config.strategy_profit_lock_trigger_pct == 10.0
    assert config.strategy_profit_giveback_pct == 35.0
    assert config.strategy_profit_lock_floor_pct == 5.0


def test_config_migrates_untouched_legacy_profit_defaults(tmp_path):
    path = tmp_path / "legacy-options.json"
    path.write_text(
        json.dumps({
            "strategy_partial_profit_trigger_pct": 3.5,
            "strategy_partial_profit_fraction_pct": 50.0,
            "strategy_profit_lock_trigger_pct": 5.0,
            "strategy_profit_giveback_pct": 35.0,
            "strategy_profit_lock_floor_pct": 2.0,
        }),
        encoding="utf-8",
    )

    config = Config.load(str(path))

    assert config.strategy_partial_profit_trigger_pct == 10.0
    assert config.strategy_profit_lock_trigger_pct == 10.0
    assert config.strategy_profit_lock_floor_pct == 5.0
    assert config.profit_protection_legacy_defaults_overridden is True


def test_config_preserves_custom_profit_protection_triplet(tmp_path):
    path = tmp_path / "custom-options.json"
    path.write_text(
        json.dumps({
            "strategy_partial_profit_trigger_pct": 7.0,
            "strategy_profit_lock_trigger_pct": 9.0,
            "strategy_profit_lock_floor_pct": 3.0,
        }),
        encoding="utf-8",
    )

    config = Config.load(str(path))

    assert config.strategy_partial_profit_trigger_pct == 7.0
    assert config.strategy_profit_lock_trigger_pct == 9.0
    assert config.strategy_profit_lock_floor_pct == 3.0
    assert config.profit_protection_legacy_defaults_overridden is False


def test_core_position_loss_stop_forces_full_reduce_only_exit(db, config, instrument):
    audit = FakeAudit()
    manager = PositionProfitProtection(config, db, audit)
    portfolio = portfolio_for(instrument, "-20", "-2.1")
    manager.observe(portfolio, "cycle-stop-loss")

    decision = manager.apply(
        cycle_id="cycle-stop-loss",
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
    assert decision.rationale["position_management_action"] == "STOP_LOSS_EXIT"
    assert decision.target_position_eur == D("0")
    assert decision.reduce_only is True
    assert decision.execution_direction == Direction.LONG
    assert any(event[0] == "POSITION_STOP_LOSS_TRIGGERED" for event in audit.events)


def test_core_exit_episode_requires_current_cycle_app_managed_opening(
    db, config, instrument
):
    import time

    cycle_id = "core-open-cycle"
    db.start_cycle(cycle_id, "test-hash")
    cycle = db.one("SELECT started_at FROM cycles WHERE cycle_id=?", (cycle_id,))
    now = max(time.time(), float(cycle["started_at"]) + 0.1)
    decision_id = "core-open-decision"
    db.execute(
        """INSERT INTO decisions(
             decision_id,created_at,symbol,direction,target_notional_eur,leverage,
             expected_return_bps,expected_cost_bps,confidence,regime,news_effect_bps,
             gemini_effect_bps,strategy_version,model_version,config_hash,rationale_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            decision_id, now, instrument.symbol, "SHORT", "20", "2", "150", "20",
            "0.8", "TREND_DOWN", "0", "0", "core-v1", "baseline-v1", "test-hash", "{}",
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
            "core-open-intent", "core-open-client", now, decision_id, instrument.symbol,
            "SHORT", "sell", "limit", "1", "100", "2", 1, 0, 0, "FILLED",
            now, "exchange-open-1", "100", "25", 60, "",
        ),
    )

    portfolio = portfolio_for(instrument, "-20", "5.0")
    portfolio.spot_margin_open_order_ids = {instrument.symbol: ("exchange-open-1",)}
    portfolio.spot_margin_position_directions = {instrument.symbol: "SHORT"}
    portfolio.spot_margin_open_lot_count = {instrument.symbol: 1}
    manager = PositionProfitProtection(config, db, FakeAudit())
    manager.observe(portfolio, cycle_id)

    episode = db.active_core_exit_episode(instrument.symbol)
    assert episode is not None
    assert episode["eligible_for_learning"] == 1
    assert episode["opening_decision_ids_json"] == '["core-open-decision"]'
    assert episode["eligibility_reason"] == (
        "APP_MANAGED_SINGLE_ORDER_CAPTURED_FROM_ENTRY_CYCLE"
    )


def test_core_exit_path_is_preserved_on_read_failure_and_closed_on_confirmed_absence(
    db, config, instrument
):
    manager = PositionProfitProtection(config, db, FakeAudit())
    manager.observe(portfolio_for(instrument, "-20", "8.0"), "core-path-1")
    episode = db.active_core_exit_episode(instrument.symbol)
    assert episode is not None

    # Failed account reads cannot be interpreted as a flat/closed position.
    manager.observe(
        portfolio_for(instrument, "-20", "0", read_ok=False),
        "core-path-read-failure",
    )
    still_active = db.active_core_exit_episode(instrument.symbol)
    assert still_active is not None
    assert still_active["episode_id"] == episode["episode_id"]

    flat = PortfolioState(spot_open_positions_read_ok=True)
    manager.observe(flat, "core-path-confirmed-flat")
    assert db.active_core_exit_episode(instrument.symbol) is None
    closed = db.one(
        "SELECT closed_at,close_reason FROM core_exit_episodes WHERE episode_id=?",
        (episode["episode_id"],),
    )
    assert closed["closed_at"] is not None
    assert closed["close_reason"] == "EXTERNAL_OR_UNATTRIBUTED_CLOSE"


def test_core_exit_path_resets_profit_high_water_when_exchange_position_changes(
    db, config, instrument
):
    manager = PositionProfitProtection(config, db, FakeAudit())
    first = portfolio_for(instrument, "-20", "12.0")
    first.spot_margin_open_order_ids = {instrument.symbol: ("exchange-open-1",)}
    first.spot_margin_position_directions = {instrument.symbol: "SHORT"}
    first.spot_margin_open_lot_count = {instrument.symbol: 1}
    manager.observe(first, "core-path-old-position")
    old_episode = db.active_core_exit_episode(instrument.symbol)
    assert old_episode is not None
    assert D(db.one(
        "SELECT peak_profit_pct FROM position_profit_state WHERE symbol=?",
        (instrument.symbol,),
    )["peak_profit_pct"]) == D("12.0")

    second = portfolio_for(instrument, "-20", "1.0")
    second.spot_margin_open_order_ids = {instrument.symbol: ("exchange-open-2",)}
    second.spot_margin_position_directions = {instrument.symbol: "SHORT"}
    second.spot_margin_open_lot_count = {instrument.symbol: 1}
    manager.observe(second, "core-path-new-position")

    new_episode = db.active_core_exit_episode(instrument.symbol)
    assert new_episode is not None
    assert new_episode["episode_id"] != old_episode["episode_id"]
    assert D(db.one(
        "SELECT peak_profit_pct FROM position_profit_state WHERE symbol=?",
        (instrument.symbol,),
    )["peak_profit_pct"]) == D("1.0")
    old = db.one(
        "SELECT closed_at,close_reason FROM core_exit_episodes WHERE episode_id=?",
        (old_episode["episode_id"],),
    )
    assert old["closed_at"] is not None
    assert old["close_reason"] == "POSITION_IDENTITY_CHANGED"

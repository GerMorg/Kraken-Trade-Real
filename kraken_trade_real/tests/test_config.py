from app.config import Config


def test_real_trading_defaults_to_disabled():
    cfg=Config.load("/path/that/does/not/exist")
    assert cfg.live_enabled is False
    assert cfg.kill_switch is True
    assert cfg.risk_max_leverage >= 1
    assert cfg.market_history_candidate_limit >= 20
    assert cfg.market_history_cache_seconds >= 60
    assert cfg.market_exploration_candidate_limit >= 1
    assert cfg.market_exploration_slots_per_family >= 1
    assert cfg.execution_reconciliation_limit >= 1
    assert cfg.execution_reconciliation_stale_seconds >= 5
    assert cfg.execution_margin_open_fee_bps == 4.0
    assert cfg.execution_margin_rollover_fee_bps == 4.0
    # Tactical can evaluate live-intent signals on a fresh install, while real
    # submissions remain globally disabled and the kill switch stays enabled.
    assert cfg.tactical_enabled is True
    assert cfg.tactical_shadow_mode is False
    assert cfg.live_enabled is False
    assert cfg.kill_switch is True


def test_config_rejects_inconsistent_risk(tmp_path):
    path=tmp_path/"options.json"
    path.write_text('{"risk_max_net_pct":90,"risk_max_gross_pct":80}',encoding="utf-8")
    try:
        Config.load(str(path))
    except ValueError as exc:
        assert "gross risk" in str(exc)
    else:
        raise AssertionError("inconsistent risk config was accepted")


def test_legacy_generic_margin_fee_defaults_are_migrated_to_conservative_fallback(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(
        '{"execution_margin_open_fee_bps":2.0,'
        '"execution_margin_rollover_fee_bps":2.0}',
        encoding="utf-8",
    )
    cfg = Config.load(str(path))
    assert cfg.execution_margin_open_fee_bps == 4.0
    assert cfg.execution_margin_rollover_fee_bps == 4.0

    # A deliberately supplied non-legacy rate remains configurable.
    path.write_text(
        '{"execution_margin_open_fee_bps":2.1,'
        '"execution_margin_rollover_fee_bps":2.1}',
        encoding="utf-8",
    )
    cfg = Config.load(str(path))
    assert cfg.execution_margin_open_fee_bps == 2.1
    assert cfg.execution_margin_rollover_fee_bps == 2.1


def test_legacy_tactical_position_limit_migrates_only_untouched_default(tmp_path):
    path = tmp_path / "options.json"
    path.write_text('{"tactical_position_limit_pct":25.0}', encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.tactical_position_limit_pct == 80.0
    assert cfg.tactical_position_limit_legacy_default_overridden is True

    path.write_text('{"tactical_position_limit_pct":15.0}', encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.tactical_position_limit_pct == 15.0
    assert cfg.tactical_position_limit_legacy_default_overridden is False


def test_adaptive_strategy_modes_are_internal_and_cannot_be_disabled_by_legacy_options(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(
        '{"strategy_adaptive_edge_enabled":false,'
        '"tactical_adaptive_entry_enabled":false}',
        encoding="utf-8",
    )

    cfg = Config.load(str(path))

    assert cfg.strategy_adaptive_edge_enabled is True
    assert cfg.tactical_adaptive_entry_enabled is True


def test_legacy_saved_risk_options_are_clamped_to_immutable_envelope(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(
        '{"risk_max_position_pct":40,"risk_max_gross_pct":100,'
        '"risk_max_net_pct":60,"risk_max_margin_pct":70,'
        '"risk_max_leverage":4,"risk_max_open_positions":10,'
        '"risk_daily_loss_pct":5,"risk_max_drawdown_pct":25,'
        '"risk_cash_reserve_pct":0,"tactical_max_capital_eur":50,'
        '"tactical_max_positions":3,"tactical_max_trades_per_hour":8,'
        '"tactical_max_trades_per_day":30,"tactical_max_daily_loss_pct":5,'
        '"tactical_short_leverage":4}',
        encoding="utf-8",
    )

    cfg = Config.load(str(path))

    assert cfg.risk_max_position_pct == 15.0
    assert cfg.risk_max_gross_pct == 80.0
    assert cfg.risk_max_net_pct == 50.0
    assert cfg.risk_max_margin_pct == 35.0
    assert cfg.risk_max_leverage == 3.0
    assert cfg.risk_max_open_positions == 3
    assert cfg.risk_daily_loss_pct == 3.0
    assert cfg.risk_max_drawdown_pct == 8.0
    assert cfg.risk_cash_reserve_pct == 20.0
    assert cfg.tactical_max_capital_eur == 15.0
    assert cfg.tactical_max_positions == 1
    assert cfg.tactical_max_trades_per_hour == 2
    assert cfg.tactical_max_trades_per_day == 6
    assert cfg.tactical_max_daily_loss_pct == 1.5
    assert cfg.tactical_short_leverage == 3.0
    assert cfg.risk_envelope_clamped is True
    assert '"risk_daily_loss_pct"' in cfg.risk_envelope_corrections

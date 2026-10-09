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

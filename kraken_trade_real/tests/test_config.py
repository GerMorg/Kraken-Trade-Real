from app.config import Config


def test_real_trading_defaults_to_disabled():
    cfg=Config.load("/path/that/does/not/exist")
    assert cfg.live_enabled is False
    assert cfg.kill_switch is True
    assert cfg.risk_max_leverage >= 1


def test_config_rejects_inconsistent_risk(tmp_path):
    path=tmp_path/"options.json"
    path.write_text('{"risk_max_net_pct":90,"risk_max_gross_pct":80}',encoding="utf-8")
    try:
        Config.load(str(path))
    except ValueError as exc:
        assert "gross risk" in str(exc)
    else:
        raise AssertionError("inconsistent risk config was accepted")

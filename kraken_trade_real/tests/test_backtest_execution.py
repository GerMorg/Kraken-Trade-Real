from __future__ import annotations

from app.learning.backtest import BacktestEngine


def observation(i, signal, *, bid=99.9, ask=100.1, bid_depth=100.0, ask_depth=100.0):
    return {
        "decision_timestamp": float(i * 60),
        "execution_timestamp": float(i * 60 + 1),
        "signal": signal,
        "bid": bid,
        "ask": ask,
        "bid_depth_base": bid_depth,
        "ask_depth_base": ask_depth,
        "min_order_qty": 0.001,
        "min_order_cost_quote": 1.0,
    }


def test_backtest_applies_fees_spread_slippage_and_financing():
    rows = [observation(0, 1), observation(1, 0)]
    result = BacktestEngine().run(
        rows, initial_equity_quote=1000, target_notional_quote=100,
        taker_fee_bps=8, slippage_bps=3, impact_coefficient_bps=15,
        financing_bps_per_bar=2,
    )
    assert result["status"] == "OK"
    assert len(result["fills"]) == 2
    assert result["metrics"]["fees_quote"] > 0
    assert result["metrics"]["slippage_and_impact_cost_quote"] > 0
    assert result["metrics"]["financing_quote"] > 0
    assert result["metrics"]["final_equity_quote"] < 1000
    assert result["assumptions"]["automatic_promotion"] is False


def test_backtest_caps_fill_to_visible_depth_and_retries_remaining_delta_later():
    rows = [
        observation(0, 1, ask_depth=2.0),
        observation(1, 1, ask_depth=2.0),
        observation(2, 0, bid_depth=100.0, ask_depth=100.0),
    ]
    result = BacktestEngine().run(
        rows, initial_equity_quote=1000, target_notional_quote=500,
        max_depth_fraction=0.10,
    )
    assert result["status"] == "OK"
    assert result["fills"][0]["quantity"] <= 0.2
    assert result["fills"][0]["partial"] is True
    assert len(result["fills"]) >= 2


def test_backtest_skips_stale_quote_and_minimum_order_and_reports_reason():
    rows = [
        {**observation(0, 1), "execution_timestamp": 5000.0},
        {
            **observation(1, 1),
            "decision_timestamp": 5040.0,
            "execution_timestamp": 5041.0,
            "min_order_qty": 1_000_000,
        },
    ]
    result = BacktestEngine().run(
        rows, initial_equity_quote=1000, target_notional_quote=100,
        max_quote_age_seconds=30,
    )
    assert result["status"] == "NO_EXECUTABLE_FILLS"
    assert result["skipped"]["STALE_EXECUTION_QUOTE"] == 1
    assert result["skipped"]["BELOW_EXCHANGE_MINIMUM"] == 1


def test_backtest_rejects_noncausal_execution_timestamp():
    rows = [observation(1, 1)]
    rows[0]["execution_timestamp"] = rows[0]["decision_timestamp"]
    result = BacktestEngine().run(rows)
    assert result["status"] == "LOOKAHEAD_OR_NONCAUSAL_QUOTE"


def test_backtest_rejects_out_of_order_execution_observations():
    rows = [observation(1, 1), observation(0, 0)]
    result = BacktestEngine().run(rows)
    assert result["status"] == "NON_CHRONOLOGICAL_EXECUTION_DATA"


def test_backtest_rejects_nonfinite_parameters():
    result = BacktestEngine().run([observation(0, 1)], taker_fee_bps=float("nan"))
    assert result["status"] == "INVALID_PARAMETERS"


def test_backtest_reads_persisted_quotes_and_only_passes_past_to_signal_provider():
    import json

    class FakeDB:
        def query(self, sql, params):
            assert "FROM market_snapshots" in sql
            assert params[0] == "BTC/USD"
            rows = []
            for timestamp, bid, ask in (
                (100.0, 99.9, 100.1),
                (130.0, 100.9, 101.1),
                (160.0, 101.9, 102.1),
            ):
                rows.append({
                    "captured_at": timestamp,
                    "symbol": "BTC/USD",
                    "price": (bid + ask) / 2,
                    "bid": bid,
                    "ask": ask,
                    "volume_24h": "1000",
                    "age_seconds": 0,
                    "spread_bps": "20",
                    "closes_json": json.dumps(["99", "100"]),
                    "depths_bid_json": json.dumps([[bid, "100"]]),
                    "depths_ask_json": json.dumps([[ask, "100"]]),
                    "funding_rate": None,
                    "open_interest": None,
                    "basis_bps": None,
                    "liquidation_pressure": None,
                    "features_json": json.dumps({"history_sufficient": "1"}),
                })
            return rows

    observed_histories = []

    def signal_provider(history):
        observed_histories.append([row["captured_at"] for row in history])
        assert history
        assert history[-1]["captured_at"] == max(row["captured_at"] for row in history)
        return 1 if len(history) == 1 else 0

    result = BacktestEngine().run_from_database(
        FakeDB(),
        "BTC/USD",
        since=90,
        until=170,
        signal_provider=signal_provider,
        min_order_qty=0.001,
        min_order_cost_quote=1,
        initial_equity_quote=1000,
        target_notional_quote=100,
    )

    assert result["status"] == "OK"
    assert result["dataset"]["source"] == "market_snapshots"
    assert result["snapshot_count"] == 3
    assert result["decision_count"] == 2
    assert [len(items) for items in observed_histories] == [1, 2]
    assert result["fills"][0]["execution_timestamp"] == 130.0
    assert result["fills"][1]["execution_timestamp"] == 160.0
    assert result["assumptions"]["automatic_promotion"] is False


def test_backtest_database_adapter_fails_closed_on_invalid_historical_book():
    class FakeDB:
        def query(self, sql, params):
            return [
                {
                    "captured_at": 100.0, "symbol": "BTC/USD", "price": "100",
                    "bid": "99.9", "ask": "100.1", "depths_bid_json": "not-json",
                    "depths_ask_json": "[]", "features_json": "{}", "closes_json": "[]",
                },
                {
                    "captured_at": 130.0, "symbol": "BTC/USD", "price": "101",
                    "bid": "100.9", "ask": "101.1", "depths_bid_json": "[]",
                    "depths_ask_json": "[]", "features_json": "{}", "closes_json": "[]",
                },
            ]

    result = BacktestEngine().run_from_database(
        FakeDB(), "BTC/USD", since=90, until=140,
        signal_provider=lambda history: 0, min_order_qty=0.001,
    )
    assert result["status"] == "INVALID_HISTORICAL_SNAPSHOT"
    assert result["fills"] == []


def test_backtest_refuses_short_signals_without_margin_and_liquidation_model():
    result = BacktestEngine().run([observation(0, -1)])
    assert result["status"] == "UNSUPPORTED_SHORT_OR_MARGIN_PRODUCT"
    assert result["fills"] == []


def test_default_taker_fee_is_conservative_spot_taker_assumption():
    result = BacktestEngine().run(
        [observation(0, 1)],
        initial_equity_quote=1000,
        target_notional_quote=100,
    )
    assert result["status"] == "OK"
    assert result["metrics"]["fees_quote"] > 0.7


def test_standard_baseline_comparison_uses_same_database_quote_window():
    import json

    class FakeDB:
        def query(self, sql, params):
            assert params == ("BTC/USD", 90.0, 900.0)
            return [
                {
                    "captured_at": 100.0 + 30.0 * i,
                    "symbol": "BTC/USD",
                    "price": str(100 + i),
                    "bid": str(99.9 + i),
                    "ask": str(100.1 + i),
                    "volume_24h": "1000",
                    "age_seconds": 0,
                    "spread_bps": "20",
                    "closes_json": json.dumps([str(100 + k) for k in range(i + 1)]),
                    "depths_bid_json": json.dumps([[str(99.9 + i), "100"]]),
                    "depths_ask_json": json.dumps([[str(100.1 + i), "100"]]),
                    "funding_rate": None,
                    "open_interest": None,
                    "basis_bps": None,
                    "liquidation_pressure": None,
                    "features_json": "{}",
                }
                for i in range(25)
            ]

    result = BacktestEngine().compare_standard_baselines_from_database(
        FakeDB(), "BTC/USD", since=90, until=900, min_order_qty=0.001,
        min_order_cost_quote=1, initial_equity_quote=1000,
        target_notional_quote=100,
    )
    assert result["status"] == "OK"
    assert set(result["baseline_results"]) == {
        "CASH", "BUY_AND_HOLD", "MOMENTUM_12_OBS_25_BPS", "TREND_FAST5_SLOW20"
    }
    assert result["baseline_results"]["CASH"]["status"] == "NO_EXECUTABLE_FILLS"
    assert result["comparison_contract"]["same_symbol_and_time_range"] is True
    assert result["comparison_contract"]["automatic_promotion"] is False
    assert result["comparison_contract"]["automatic_winner_selection"] is False


def test_core_long_only_replay_uses_stored_features_and_zeroes_external_effects(
    db, instrument, config
):
    import json

    class FakeDB:
        def query(self, sql, params):
            assert "FROM market_snapshots" in sql
            assert params[0] == instrument.symbol
            features = {
                "trend": "10",
                "return_1": "1",
                "return_5": "10",
                "return_15": "10",
                "return_60": "10",
                "return_240": "10",
                "volatility": "1",
                "history_sufficient": "1",
                "liquidity": "10000",
                "spread_bps": "20",
                "book_depth": "100000",
                "impact_bps": "1",
            }
            rows = []
            for i in range(25):
                mid = 100.0 + i
                rows.append({
                    "id": i,
                    "captured_at": 1000.0 + 60.0 * i,
                    "symbol": instrument.symbol,
                    "price": str(mid),
                    "bid": str(mid - 0.1),
                    "ask": str(mid + 0.1),
                    "volume_24h": "1000",
                    "age_seconds": 0,
                    "spread_bps": "20",
                    "closes_json": json.dumps([str(80 + j) for j in range(30)]),
                    "depths_bid_json": json.dumps([[str(mid - 0.1), "100"]]),
                    "depths_ask_json": json.dumps([[str(mid + 0.1), "100"]]),
                    "funding_rate": None,
                    "open_interest": None,
                    "basis_bps": None,
                    "liquidation_pressure": None,
                    "features_json": json.dumps(features),
                })
            return rows

    result = BacktestEngine().run_core_long_only_from_database(
        FakeDB(),
        instrument,
        config,
        since=900,
        until=3000,
        min_order_qty=float(instrument.min_order_qty),
        min_order_cost_quote=float(instrument.min_cost),
        initial_equity_quote=1000,
        target_notional_quote=100,
        max_depth_fraction=0.5,
    )
    assert result["status"] == "OK"
    assert result["strategy"] == "CORE_LONG_ONLY_NEWS_GEMINI_ZEROED"
    assert result["signal_policy"]["news_effect_bps"] == "0"
    assert result["signal_policy"]["gemini_effect_bps"] == "0"
    assert result["metrics"]["fill_count"] > 0
    assert result["assumptions"]["automatic_promotion"] is False

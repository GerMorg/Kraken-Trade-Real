from __future__ import annotations

import asyncio
import base64
import time
from decimal import Decimal

from custom_components.kraken_ai_trader.core.authority import CentralTradingAuthority
from custom_components.kraken_ai_trader.core.execution import ExecutionPlanner
from custom_components.kraken_ai_trader.core.gemini import GeminiClient
from custom_components.kraken_ai_trader.core.history import HistoryBackfill
from custom_components.kraken_ai_trader.core.kraken import KrakenGateway
from custom_components.kraken_ai_trader.core.learning import LearningEngine, ModelRegistry
from custom_components.kraken_ai_trader.core.models import (
    Blocker,
    CostEstimate,
    DecisionState,
    Direction,
    Instrument,
    MarketSnapshot,
    NewsEvent,
    OrderBook,
    OrderIntent,
    PortfolioSnapshot,
    Position,
    ProductType,
    SafetyLimits,
    Signal,
)
from custom_components.kraken_ai_trader.core.news import NewsEngine
from custom_components.kraken_ai_trader.core.risk import RiskEngine
from custom_components.kraken_ai_trader.core.storage import Store
from custom_components.kraken_ai_trader.core.websocket import WebsocketSupervisor


def limits() -> SafetyLimits:
    return SafetyLimits(Decimal("0.02"), Decimal("1"), Decimal("1"), Decimal("0.25"), Decimal("3"), 5, Decimal("0.05"), Decimal("0.10"), Decimal("10"), Decimal("0.0025"), 20)


def instrument(product: ProductType = ProductType.SPOT_MARGIN) -> Instrument:
    return Instrument("kraken", product, "BTC/USD" if product != ProductType.FUTURES else "PI_XBTUSD", "XXBTZUSD" if product != ProductType.FUTURES else "PI_XBTUSD", "XBTUSD", "BTC", "USD", "perpetual" if product == ProductType.FUTURES else None, "online", product != ProductType.SPOT, True, product != ProductType.SPOT, (Decimal("1"), Decimal("2"), Decimal("3")), Decimal("3"), Decimal("0.0001"), Decimal("0.5"), 8, 2, Decimal("0.01"), Decimal("100"), Decimal("100"))


def portfolio(positions=(), equity="50", available="40", used="0", gross="0", net="0"):
    return PortfolioSnapshot(Decimal(equity), Decimal(equity), Decimal(available), Decimal(used), Decimal(gross), Decimal(net), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), tuple(positions), 0, 0, time.time())


def snapshot(inst: Instrument | None = None) -> MarketSnapshot:
    inst = inst or instrument()
    book = OrderBook(((Decimal("100"), Decimal("10000")),), ((Decimal("100.01"), Decimal("10000")),), time.time())
    candles = {15: tuple(Decimal(str(100 + i * 0.5)) for i in range(60))}
    return MarketSnapshot(inst, Decimal("100.01"), Decimal("100"), Decimal("100.01"), Decimal("1000000"), book, candles, None, None, time.time())


def test_execution_planner_all_paths():
    planner = ExecutionPlanner(Decimal("0.0025"))
    snap = snapshot()
    assert planner.plan(snap, Direction.LONG, Decimal("1000"), Decimal("10"), Decimal("1"))[0] == "limit"
    wider = MarketSnapshot(snap.instrument, Decimal("100"), Decimal("99.9"), Decimal("100.1"), snap.volume_24h, snap.book, snap.candles, None, None, time.time())
    assert planner.plan(wider, Direction.LONG, Decimal("100"), Decimal("1"), Decimal("1"), urgency="0.9")[0] == "market"
    too_wide = MarketSnapshot(snap.instrument, Decimal("100"), Decimal("90"), Decimal("110"), snap.volume_24h, snap.book, snap.candles, None, None, time.time())
    assert planner.plan(too_wide, Direction.LONG, Decimal("100"), Decimal("1"), Decimal("1"))[0] == "blocked"


def test_risk_all_material_safety_blocks():
    engine = RiskEngine(limits())
    sig = Signal(Decimal("0.8"), Decimal("0.2"), Decimal("0.8"), Decimal("0.02"), Decimal("0.01"), "s", "m")
    cost = CostEstimate(Decimal("0.001"), Decimal("0.001"), Decimal("0.001"), Decimal("0.001"), Decimal("0"), Decimal("0"), Decimal("0"))
    inst = instrument()
    assert engine.evaluate(portfolio(equity="0", available="0"), inst, sig, cost, DecisionState.OPEN_LONG, Decimal("10"), Decimal("1")).blocker == Blocker.BLOCKED_PORTFOLIO
    p = portfolio(equity="50", available="40", used="20")
    assert engine.evaluate(p, inst, sig, cost, DecisionState.OPEN_LONG, Decimal("10"), Decimal("1")).blocker == Blocker.BLOCKED_MARGIN
    dd = PortfolioSnapshot(Decimal("50"), Decimal("50"), Decimal("40"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0.11"), (), 0, 0, time.time())
    assert engine.evaluate(dd, inst, sig, cost, DecisionState.OPEN_LONG, Decimal("10"), Decimal("1")).blocker == Blocker.CIRCUIT_BREAKER
    low_conf = Signal(Decimal("0.52"), Decimal("0.1"), Decimal("0.52"), Decimal("0.02"), Decimal("0.01"), "s", "m")
    assert engine.evaluate(portfolio(), inst, low_conf, cost, DecisionState.OPEN_LONG, Decimal("10"), Decimal("1")).blocker == Blocker.BLOCKED_RISK
    expensive = CostEstimate(Decimal("0.01"), Decimal("0.01"), Decimal("0.01"), Decimal("0.01"), Decimal("0"), Decimal("0"), Decimal("0"))
    assert engine.evaluate(portfolio(), inst, sig, expensive, DecisionState.OPEN_LONG, Decimal("10"), Decimal("1")).blocker == Blocker.BLOCKED_COST
    assert engine.evaluate(portfolio(positions=tuple(Position(f"P{i}", Direction.LONG, Decimal("1"), Decimal("1"), Decimal("1"), Decimal("0"), Decimal("1")) for i in range(5))), inst, sig, cost, DecisionState.OPEN_LONG, Decimal("10"), Decimal("1")).blocker == Blocker.BLOCKED_PORTFOLIO
    assert engine.evaluate(portfolio(), inst, sig, cost, DecisionState.OPEN_LONG, Decimal("0.1"), Decimal("1")).blocker == Blocker.BLOCKED_POSITION_SIZE


def test_learning_registry_and_non_trade():
    learner = LearningEngine()
    pred = learner.create_prediction("p", Decimal("0.7"), Decimal("0.01"), 1.0)
    assert pred.model_version == "baseline"
    learner.record_outcome("p", Decimal("0.02"), Decimal("0.001"))
    assert learner.registry.metrics["baseline"].trades == 1
    learner.record_non_trade("nt", Decimal("0.01"))
    assert "nt" in learner.non_trade_predictions
    learner.resolve_non_trade("nt", Decimal("-0.02"))
    assert learner.non_trade_outcomes[-1] == Decimal("-0.03")
    reg = ModelRegistry()
    reg.register_candidate("candidate")
    reg.record("candidate", Decimal("0.02"), Decimal("0.001"))
    reg.record("baseline", Decimal("0.001"), Decimal("0"))
    assert reg.promote("candidate")
    reg.rollback()
    assert reg.active == reg.baseline


def test_store_roundtrip_and_order_lookup(tmp_path):
    store = Store(tmp_path / "data.db")
    store.connect()
    now = time.time()
    store.upsert_instrument("I", {"symbol": "BTC/USD"}, now)
    store.save_cycle("c", now, "CYCLE_START")
    store.save_market_snapshot("c", "BTC/USD", {"last": "100"}, now)
    store.save_portfolio_snapshot("c", {"equity": "50"}, now)
    store.save_portfolio_position("c", "BTC/USD", {"quantity": "1"}, now)
    store.save_decision("d", "c", "BTC/USD", {"x": 1}, now)
    intent = {"symbol": "BTC/USD", "direction": "long"}
    store.save_order("i", "d", "client", "INTENT_CREATED", intent, now)
    assert store.has_open_intent("BTC/USD")
    assert store.order_by_client_order_id("client")["intent_id"] == "i"
    assert store.count_orders_today(now - 1) == 1
    store.save_prediction("d", {"expected": "0.1"}, now)
    store.save_prediction_outcome("d", {"realized": "0.2"}, now)
    for table in ["app_events", "error_events", "learning_events", "risk_events", "health_snapshots", "gemini_analysis", "news_events", "news_analysis", "calibration_history"]:
        payload = {"event_code": "E", "error_code": "E", "news_id": 1}
        store.event(table, payload, now)
    store.complete_cycle("c", now + 1, "CYCLE_COMPLETE")
    assert store.get_meta("schema_version") == "1"
    store.close()


def test_history_backfill_persists(tmp_path):
    class G:
        async def market_snapshot(self, inst):
            return snapshot(inst)

    store = Store(tmp_path / "history.db")
    store.connect()
    count = asyncio.run(HistoryBackfill(G(), store).run([instrument(), instrument(ProductType.FUTURES)]))
    assert count == 2
    store.close()


def test_authority_action_helpers():
    authority = CentralTradingAuthority(object(), object(), object(), Store(":memory:"), limits(), {})
    inst = instrument()
    p = Position(inst.symbol, Direction.LONG, Decimal("1"), Decimal("100"), Decimal("1"), Decimal("0"), Decimal("100"))
    sig_same = Signal(Decimal("0.8"), Decimal("0.2"), Decimal("0.8"), Decimal("0.01"), Decimal("0.01"), "s", "m")
    state, direction, target = authority._decision_state(p, inst, sig_same, portfolio(positions=(p,), equity="5000"))
    assert state == DecisionState.NO_POSITION and direction == Direction.LONG and target == Decimal("100")
    sig_reverse = Signal(Decimal("0.2"), Decimal("0.8"), Decimal("0.8"), Decimal("0.01"), Decimal("0.01"), "s", "m")
    state, direction, _ = authority._decision_state(p, inst, sig_reverse, portfolio(positions=(p,), equity="1000"))
    assert state == DecisionState.REVERSE_LONG_TO_SHORT and direction == Direction.SHORT
    sig_none = Signal(Decimal("0.4"), Decimal("0.4"), Decimal("0.4"), Decimal("0"), Decimal("0.01"), "s", "m")
    state, direction, _ = authority._decision_state(p, inst, sig_none, portfolio(positions=(p,), equity="1000"))
    assert state == DecisionState.CLOSE_LONG and direction == Direction.LONG


def test_authority_submit_live_and_ambiguous(tmp_path):
    class G:
        async def submit(self, intent):
            return "O1", {"status": "ok"}

    class AmbiguousG(G):
        async def submit(self, intent):
            error = RuntimeError("timeout")
            error.ambiguous = True
            raise error

    def make_intent():
        return OrderIntent("c", "d", "i", "client", "XXBTZUSD", ProductType.SPOT_MARGIN, Direction.LONG, "OPEN", "limit", Decimal("100"), Decimal("0.1"), Decimal("1"), False, True, "s", "m", "hash", time.time())

    store = Store(tmp_path / "submit.db")
    authority = CentralTradingAuthority(G(), None, None, store, limits(), {"live_enabled": True})
    asyncio.run(authority._submit(make_intent()))
    assert authority.state.last_action == "ORDER_SUBMITTED"
    store.close()

    store2 = Store(tmp_path / "amb.db")
    authority2 = CentralTradingAuthority(AmbiguousG(), None, None, store2, limits(), {"live_enabled": True})
    asyncio.run(authority2._submit(make_intent()))
    assert authority2.state.last_blocker == Blocker.BLOCKED_RECONCILIATION
    store2.close()


def test_websocket_sequence_gap_triggers_recovery():
    events = []
    gaps = []

    async def on_event(value):
        events.append(value)

    async def on_gap():
        gaps.append(True)

    ws = WebsocketSupervisor(object(), on_event, on_gap)
    asyncio.run(ws._handle_message({"channel": "ticker", "sequence": 1}, private=False))
    asyncio.run(ws._handle_message({"channel": "ticker", "sequence": 3}, private=False))
    assert gaps == [True]
    assert any(item.get("event_code") == "PUBLIC_SEQUENCE_GAP" for item in events)


def test_gemini_and_news_validation():
    gemini = GeminiClient(None)
    valid = gemini._validate({"asset": "BTC", "event": "rate", "direction": "bullish", "impact": 0.7, "confidence": 0.8, "time_horizon": "day", "novelty": 0.5, "market_confirmation": 0.6, "risk_flags": ["x"]})
    assert valid is not None
    invalid = gemini._validate({"direction": "bad", "impact": 2, "confidence": 0, "novelty": 0, "market_confirmation": 0})
    assert invalid is None
    news = NewsEngine([])
    event = news._classify("https://news.example/rss", "https://news.example/a", "Bitcoin ETF approval drives inflow", time.time(), "BTC")
    assert event.asset == "BTC" and event.direction == "bullish" and event.impact > 0


def test_kraken_discovery_market_and_portfolio(monkeypatch):
    secret = base64.b64encode(b"secret-secret").decode()
    gateway = KrakenGateway("key", secret, "fkey", secret)

    async def spot_public(endpoint, params=None):
        if endpoint == "AssetPairs":
            return {"XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT", "quote": "ZUSD", "status": "online", "leverage_buy": [2, 3], "ordermin": "0.0001", "costmin": "0.5", "lot_decimals": 8, "pair_decimals": 2, "tick_size": "0.01"}}
        if endpoint == "Ticker":
            return {"XXBTZUSD": {"c": ["101"], "b": ["100"], "a": ["101"], "v": ["10", "20"]}}
        if endpoint == "Depth":
            return {"XXBTZUSD": {"bids": [["100", "10"]], "asks": [["101", "10"]]}}
        if endpoint == "OHLC":
            return {"XXBTZUSD": [["1","100","101","99","100","0","0","0"],["2","100","102","99","101","0","0","0"]], "last": 2}
        if endpoint == "SystemStatus":
            return {"status": "online"}
        return {}

    async def futures_public(endpoint, params=None):
        if endpoint == "instruments":
            return {"instruments": [{"symbol": "PI_XBTUSD", "pair": "BTC/USD", "tradeable": True, "maxLeverage": 5, "lotSize": "1", "tickSize": "0.1"}]}
        if endpoint == "tickers":
            return {"tickers": [{"symbol": "PI_XBTUSD", "last": "101", "bid": "100", "ask": "101", "volume": "20", "fundingRate": "0.001", "openInterest": "10"}]}
        if endpoint == "orderbook":
            return {"orderBook": {"bids": [["100", "10"]], "asks": [["101", "10"]]}}
        return {}

    async def spot_private(endpoint, params=None):
        if endpoint == "GetApiKeyInfo":
            return {"permissions": ["query"]}
        if endpoint == "Balance":
            return {"ZEUR": "50", "XXBT": "0.1"}
        if endpoint == "TradeBalance":
            return {"e": "50", "mf": "45", "m": "5", "n": "1"}
        if endpoint == "OpenOrders":
            return {"open": {}}
        if endpoint == "OpenPositions":
            return {"id1": {"pair": "XBTUSD", "vol": "0.1", "type": "buy", "price": "100", "value": "10", "net": "0.2"}}
        raise AssertionError(endpoint)

    async def futures_private(endpoint, params=None):
        if endpoint == "accounts":
            return {"accounts": {"flex": {"equity": "10", "availableMargin": "8", "usedMargin": "2"}}}
        if endpoint == "openpositions":
            return {"openPositions": [{"symbol": "PI_XBTUSD", "side": "long", "size": "1", "price": "100", "unrealizedPnl": "1", "value": "100"}]}
        raise AssertionError(endpoint)

    gateway.spot_public = spot_public
    gateway.futures_public = futures_public
    gateway.spot_private = spot_private
    gateway.futures_private = futures_private
    instruments = asyncio.run(gateway.discover())
    assert {i.instrument_id for i in instruments} == {"XXBTZUSD", "PI_XBTUSD"}
    coarse = asyncio.run(gateway.fast_scan(instruments))
    assert {row[0].instrument_id for row in coarse} == {"XXBTZUSD", "PI_XBTUSD"}
    spot_inst = next(i for i in instruments if i.product_type != ProductType.FUTURES)
    futures_inst = next(i for i in instruments if i.product_type == ProductType.FUTURES)
    spot_snap = asyncio.run(gateway.market_snapshot(spot_inst))
    futures_snap = asyncio.run(gateway.market_snapshot(futures_inst))
    assert spot_snap.book.bids and futures_snap.funding == Decimal("0.001")
    perms = asyncio.run(gateway.authenticate())
    assert perms == {"query"}
    p = asyncio.run(gateway.portfolio())
    assert p.cash == Decimal("50") and p.gross_exposure > 0 and len(p.positions) == 2
    asyncio.run(gateway.close())


def test_store_decision_checks_and_portfolio_derived_metrics(tmp_path):
    store = Store(tmp_path / "metrics.db")
    authority = CentralTradingAuthority(object(), None, None, store, limits(), {})
    p1 = portfolio(equity="50", available="40")
    p1 = authority._update_derived_portfolio_metrics(p1)
    assert p1.drawdown == Decimal("0") and p1.daily_pnl == Decimal("0")
    p2 = portfolio(equity="48", available="38")
    p2 = authority._update_derived_portfolio_metrics(p2)
    assert p2.drawdown == Decimal("0.04")
    store.save_decision("d", "c", "BTC/USD", {"state": "OPEN_LONG"}, time.time())
    store.save_decision_check("d", "risk", True, "passed", time.time())
    assert store._connection.execute("SELECT COUNT(*) FROM decision_checks WHERE decision_id='d'").fetchone()[0] == 1
    store.close()


def test_dynamic_news_assets_avoid_substring_false_positive():
    news = NewsEngine([])
    news.set_assets({"DOGE", "ABC"})
    assert news._entity("ABC announces adoption") == "ABC"
    assert news._entity("solitary confinement policy") is None


def test_futures_basis_feature_and_open_interest_change():
    inst = instrument(ProductType.FUTURES)
    book = OrderBook(((Decimal("100"), Decimal("10000")),), ((Decimal("101"), Decimal("10000")),), time.time())
    snap = MarketSnapshot(inst, Decimal("101"), Decimal("100"), Decimal("101"), Decimal("1000000"), book, {15: tuple(Decimal(str(100 + i * .1)) for i in range(60))}, Decimal("0.001"), Decimal("110"), time.time(), Decimal("100"))
    f = __import__('custom_components.kraken_ai_trader.core.analytics', fromlist=['compute_features']).compute_features(snap, previous_open_interest=Decimal("100"))
    assert f.basis == Decimal("0.01") and f.open_interest_change == Decimal("0.1")


class StartupGateway:
    futures_key = ""
    def __init__(self, fail_auth=False):
        self.instruments = {}
        self.fail_auth = fail_auth
    async def start(self): pass
    async def close(self): pass
    async def system_status(self): return "online"
    async def authenticate(self):
        if self.fail_auth:
            raise RuntimeError("auth failure")
        return {"query-funds", "query-open-trades"}
    async def discover(self):
        inst = instrument(ProductType.SPOT_MARGIN)
        self.instruments = {inst.instrument_id: inst}
        return [inst]
    async def market_snapshot(self, inst):
        return snapshot(inst)
    async def portfolio(self):
        return portfolio()


async def _noop_news_fetch():
    return []


def test_authority_startup_success_and_disabled(tmp_path):
    store = Store(tmp_path / "startup.db")
    gateway = StartupGateway()
    news = NewsEngine([])
    authority = CentralTradingAuthority(gateway, news, GeminiClient(None), store, limits(), {"enabled": True, "news_enabled": False, "history_backfill_instruments": 1})
    asyncio.run(authority.startup())
    assert authority.state.system_ready
    assert authority.state.metadata["instrument_count"] == 1
    asyncio.run(authority.close())

    store2 = Store(tmp_path / "disabled.db")
    authority2 = CentralTradingAuthority(StartupGateway(), NewsEngine([]), GeminiClient(None), store2, limits(), {"enabled": False})
    asyncio.run(authority2.startup())
    assert authority2.state.last_blocker == Blocker.BLOCKED_CONFIG
    asyncio.run(authority2.close())


def test_authority_startup_auth_failure_enters_safe_stop(tmp_path):
    store = Store(tmp_path / "authfail.db")
    authority = CentralTradingAuthority(StartupGateway(fail_auth=True), NewsEngine([]), GeminiClient(None), store, limits(), {"enabled": True})
    asyncio.run(authority.startup())
    assert authority.state.circuit_breaker and not authority.state.system_ready
    assert authority.state.last_blocker == Blocker.BLOCKED_PRIVATE_DATA
    asyncio.run(authority.close())


def test_authority_recovery_and_reconcile_failure(tmp_path):
    class RecoveryGateway(StartupGateway):
        async def portfolio(self):
            return portfolio(equity="50", available="40")
    store = Store(tmp_path / "recovery.db")
    gateway = RecoveryGateway()
    authority = CentralTradingAuthority(gateway, NewsEngine([]), GeminiClient(None), store, limits(), {"enabled": True})
    ok = asyncio.run(authority.recover())
    assert ok and not authority.state.circuit_breaker and authority.state.portfolio_consistent
    async def broken_portfolio():
        raise RuntimeError("reconcile down")
    gateway.portfolio = broken_portfolio
    assert asyncio.run(authority.reconcile()) is None
    assert not authority.state.portfolio_consistent and authority.state.last_blocker == Blocker.BLOCKED_RECONCILIATION
    asyncio.run(authority.close())


def test_authority_execution_event_updates_order_and_prediction(tmp_path):
    store = Store(tmp_path / "execution-event.db")
    authority = CentralTradingAuthority(object(), None, None, store, limits(), {})
    store.save_order("i", "d", "client-1", "LIVE", {"symbol": "BTC/USD"}, time.time(), "OID-1")
    authority.learning.create_prediction("d", Decimal("0.7"), Decimal("0.01"), time.time())
    store.save_prediction("d", {"prediction_id": "d", "model_version": "baseline"}, time.time())
    asyncio.run(authority.handle_execution_event({"channel": "executions", "data": [{"order_id": "OID-1", "exec_type": "trade", "symbol": "BTC/USD", "realizedPnl": "0.2", "fee": "0.01"}]}))
    row = store._connection.execute("SELECT state FROM orders WHERE intent_id='i'").fetchone()
    assert row[0] == "PARTIALLY_FILLED"
    assert store._connection.execute("SELECT COUNT(*) FROM fills WHERE intent_id='i'").fetchone()[0] == 1
    outcome = store._connection.execute("SELECT prediction_id FROM prediction_outcomes WHERE prediction_id='d'").fetchone()
    assert outcome[0] == "d"
    store.close()



def test_authority_submit_requires_post_ack_reconciliation(tmp_path):
    class ReconcileGateway:
        async def submit(self, intent):
            return "OID-2", {"sendStatus": {"status": "received"}}
        async def reconcile_order(self, intent, kraken_order_id):
            return {"orderId": kraken_order_id, "status": "filled"}
    store = Store(tmp_path / "filled.db")
    authority = CentralTradingAuthority(ReconcileGateway(), None, None, store, limits(), {"live_enabled": True})
    intent = OrderIntent("c", "d", "i", "client-2", "XXBTZUSD", ProductType.SPOT_MARGIN, Direction.LONG, "OPEN", "limit", Decimal("100"), Decimal("0.1"), Decimal("1"), False, True, "s", "m", "h", time.time())
    asyncio.run(authority._submit(intent))
    row = store._connection.execute("SELECT state FROM orders WHERE intent_id='i'").fetchone()
    assert row[0] == "FILLED"
    store.close()


def test_authority_submit_unknown_after_ack_enters_breaker(tmp_path):
    class ReconcileGateway:
        async def submit(self, intent): return "OID-3", {"status": "received"}
        async def reconcile_order(self, intent, kraken_order_id): return None
    store = Store(tmp_path / "unknown.db")
    authority = CentralTradingAuthority(ReconcileGateway(), None, None, store, limits(), {"live_enabled": True})
    intent = OrderIntent("c", "d", "i", "client-3", "XXBTZUSD", ProductType.SPOT_MARGIN, Direction.LONG, "OPEN", "limit", Decimal("100"), Decimal("0.1"), Decimal("1"), False, True, "s", "m", "h", time.time())
    asyncio.run(authority._submit(intent))
    row = store._connection.execute("SELECT state FROM orders WHERE intent_id='i'").fetchone()
    assert row[0] == "UNKNOWN_RECONCILING" and authority.state.circuit_breaker
    store.close()


def test_authority_helpers_cover_signal_cost_and_quantity_branches():
    authority = CentralTradingAuthority(object(), None, None, Store(":memory:"), limits(), {"minimum_expected_edge": "0.003"})
    inst = instrument()
    snap = snapshot(inst)
    features = __import__('custom_components.kraken_ai_trader.core.analytics', fromlist=['compute_features']).compute_features(snap)
    news = [NewsEvent("s", "u", "Bitcoin approval", time.time(), "BTC", "event", "bullish", Decimal("0.8"), Decimal("0.8"), Decimal("0.8"), "day", Decimal("0.6"))]
    gem = {news[0].title: __import__('custom_components.kraken_ai_trader.core.models', fromlist=['GeminiAnalysis']).GeminiAnalysis("BTC", "event", "bullish", Decimal("0.8"), Decimal("0.8"), "day", Decimal("0.8"), Decimal("0.8"), ())}
    signal = authority._build_signal(snap, features, news, gem)
    assert signal.expected_return >= 0
    p = Position(inst.symbol, Direction.LONG, Decimal("2"), Decimal("100"), Decimal("1"), Decimal("0"), Decimal("200"))
    assert authority._quantity_for_action(p, snap, Decimal("100"), DecisionState.CLOSE_LONG, Direction.LONG) == Decimal("2")
    assert authority._quantity_for_action(p, snap, Decimal("50"), DecisionState.REDUCE_LONG, Direction.LONG) <= Decimal("2")
    assert authority._quantity_for_action(None, snap, Decimal("50"), DecisionState.OPEN_LONG, Direction.LONG) > 0
    assert authority._find_position(portfolio(positions=(p,)), inst) == p
    costs = authority._estimate_costs(snap, risk_reducing=False)
    assert costs.total > 0
    authority.store.close()


def test_risk_leverage_scaling_and_reduce_path():
    engine = RiskEngine(limits())
    inst = instrument(ProductType.FUTURES)
    p = portfolio(equity="50", available="40", used="0")
    high = Signal(Decimal("0.95"), Decimal("0.1"), Decimal("0.95"), Decimal("0.05"), Decimal("0.01"), "s", "m")
    assert engine.choose_leverage(high, Decimal("0.01"), Decimal("0.9"), Decimal("0"), Decimal("0"), inst) <= Decimal("3")
    lowliq = engine.choose_leverage(high, Decimal("0.01"), Decimal("0.1"), Decimal("0"), Decimal("0"), inst)
    assert lowliq >= Decimal("1")
    close = engine.evaluate(p, inst, high, CostEstimate(Decimal("0.001"), Decimal("0.001"), Decimal("0.001"), Decimal("0.001"), Decimal("0"), Decimal("0"), Decimal("0")), DecisionState.CLOSE_LONG, Decimal("10"), Decimal("1"))
    assert close.allowed and close.leverage == Decimal("1")


def test_authority_derived_metrics_non_positive_equity_is_unchanged(tmp_path):
    store = Store(tmp_path / "negative.db")
    authority = CentralTradingAuthority(object(), None, None, store, limits(), {})
    bad = portfolio(equity="0", available="0")
    assert authority._update_derived_portfolio_metrics(bad) == bad
    store.close()


def test_risk_leverage_high_volatility_forces_one_x():
    engine = RiskEngine(limits())
    inst = instrument(ProductType.FUTURES)
    sig = Signal(Decimal("0.95"), Decimal("0.1"), Decimal("0.95"), Decimal("0.05"), Decimal("0.01"), "s", "m")
    assert engine.choose_leverage(sig, Decimal("0.25"), Decimal("0.9"), Decimal("0"), Decimal("0"), inst) == Decimal("1")


def test_authority_live_permission_gate_requires_query_and_modify(tmp_path):
    class GateGateway(StartupGateway):
        async def authenticate(self):
            return {"query-funds"}
    store = Store(tmp_path / "permission.db")
    authority = CentralTradingAuthority(GateGateway(), NewsEngine([]), GeminiClient(None), store, limits(), {"enabled": True, "live_enabled": True})
    asyncio.run(authority.startup())
    assert not authority.state.system_ready
    assert authority.state.circuit_breaker
    assert authority.state.last_blocker == Blocker.BLOCKED_PRIVATE_DATA
    asyncio.run(authority.close())


def test_ha_dynamic_ws_universe_is_not_truncated():
    source = __import__("pathlib").Path("custom_components/kraken_ai_trader/__init__.py").read_text(encoding="utf-8")
    assert "][:100]" not in source
    assert "spot_symbols = [" in source and "futures_symbols = [" in source


def test_authority_startup_blocks_offline_kraken(tmp_path):
    class OfflineGateway(StartupGateway):
        async def system_status(self):
            return "maintenance"
    store = Store(tmp_path / "offline.db")
    authority = CentralTradingAuthority(OfflineGateway(), NewsEngine([]), GeminiClient(None), store, limits(), {"enabled": True})
    asyncio.run(authority.startup())
    assert authority.state.last_blocker == Blocker.BLOCKED_KRAKEN_STATUS
    assert authority.state.circuit_breaker and not authority.state.system_ready
    asyncio.run(authority.close())


def test_authority_live_permission_metadata_is_explicit(tmp_path):
    class LiveGateway(StartupGateway):
        async def authenticate(self):
            return {"query-funds", "query-open-trades", "modify-trades"}
    store = Store(tmp_path / "live-perm.db")
    authority = CentralTradingAuthority(
        LiveGateway(), NewsEngine([]), GeminiClient(None), store, limits(),
        {"enabled": True, "live_enabled": True, "history_backfill_instruments": 1},
    )
    asyncio.run(authority.startup())
    assert authority.state.system_ready
    assert authority.state.metadata["close_trade_permission_missing"] is True
    assert authority.state.metadata["private_ws_permission_missing"] is True
    asyncio.run(authority.close())


def test_authority_live_permission_modify_is_required(tmp_path):
    class NoModifyGateway(StartupGateway):
        async def authenticate(self):
            return {"query-funds", "query-open-trades"}
    store = Store(tmp_path / "no-modify.db")
    authority = CentralTradingAuthority(NoModifyGateway(), NewsEngine([]), GeminiClient(None), store, limits(), {"enabled": True, "live_enabled": True})
    asyncio.run(authority.startup())
    assert authority.state.last_blocker == Blocker.BLOCKED_PRIVATE_DATA
    asyncio.run(authority.close())

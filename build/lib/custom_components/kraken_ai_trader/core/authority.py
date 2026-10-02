from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import asdict, replace
from decimal import Decimal
from typing import Any

from .analytics import compute_features, detect_regime
from .execution import ExecutionPlanner
from .history import HistoryBackfill
from .learning import LearningEngine
from .models import (
    Blocker,
    CostEstimate,
    Decision,
    DecisionState,
    Direction,
    Instrument,
    OrderIntent,
    OrderState,
    PortfolioSnapshot,
    Position,
    ProductType,
    RuntimeState,
    SafetyLimits,
    Signal,
)
from .risk import RiskEngine
from .storage import Store


class CentralTradingAuthority:
    """The single deterministic path from market observation to an exchange order."""

    def __init__(self, gateway, news, gemini, store: Store, safety_limits: SafetyLimits, config: dict[str, Any]) -> None:
        self.gateway = gateway
        self.news = news
        self.gemini = gemini
        self.store = store
        self.limits = safety_limits
        self.config = config
        self.risk = RiskEngine(safety_limits)
        self.execution = ExecutionPlanner(safety_limits.max_slippage)
        self.learning = LearningEngine()
        self.history = HistoryBackfill(gateway, store, per_instrument=1)
        self.state = RuntimeState()
        self._lock = asyncio.Lock()
        self._cooldown_until: dict[str, float] = {}
        self._decision_predictions: dict[str, str] = {}
        self._last_open_interest: dict[str, Decimal] = {}

    def _config_hash(self) -> str:
        safe = {key: value for key, value in self.config.items() if "secret" not in key.lower() and "key" not in key.lower()}
        return hashlib.sha256(repr(sorted(safe.items())).encode()).hexdigest()[:16]

    def _event(self, code: str, **payload: Any) -> None:
        self.store.event(
            "app_events",
            {"event_code": code, "app_instance_id": self.state.metadata.get("app_instance_id", ""), **payload},
            time.time(),
        )

    def _update_derived_portfolio_metrics(self, portfolio: PortfolioSnapshot) -> PortfolioSnapshot:
        """Persist monotonic peak and session-equity anchors used by immutable risk limits."""
        if portfolio.equity <= 0:
            return portfolio
        peak = Decimal(self.store.get_meta("equity_peak", "0") or "0")
        peak = max(peak, portfolio.equity)
        self.store.set_meta("equity_peak", str(peak))
        drawdown = max(Decimal("0"), (peak - portfolio.equity) / peak) if peak > 0 else Decimal("0")
        day_key = time.strftime("%Y-%m-%d", time.gmtime())
        start_key = f"daily_start_equity:{day_key}"
        daily_start = Decimal(self.store.get_meta(start_key, "0") or "0")
        if daily_start <= 0:
            daily_start = portfolio.equity
            self.store.set_meta(start_key, str(daily_start))
        daily_pnl = portfolio.equity - daily_start
        return replace(portfolio, daily_pnl=daily_pnl, drawdown=drawdown)

    async def startup(self) -> None:
        self.store.connect()
        self.state.metadata.setdefault("app_instance_id", uuid.uuid4().hex)
        self.state.system_state = "BOOT"
        self._event("BOOT_START")
        await self.gateway.start()
        self.state.system_state = "CONFIG_VALIDATING"
        self._event("CONFIG_LOADED")
        if not self.config.get("enabled", False):
            self.state.last_blocker = Blocker.BLOCKED_CONFIG
            self.state.system_state = "SAFE_STOP"
            self._event("SAFE_STOP", blocker=self.state.last_blocker)
            return

        self.state.system_state = "KRAKEN_AUTH_CHECK"
        self._event("KRAKEN_AUTH_CHECK_STARTED")
        try:
            status = await self.gateway.system_status()
            self._event("KRAKEN_STATUS", status=status)
            if status not in {"online", "post_only"}:
                self.state.circuit_breaker = True
                self.state.last_blocker = Blocker.BLOCKED_KRAKEN_STATUS
                self.state.system_state = "SAFE_STOP"
                return
            permissions = await self.gateway.authenticate()
            normalized = {str(x).lower().strip() for x in permissions}
            required_read = {"query-funds", "query-open-trades"}
            missing_read = sorted(required_read - normalized)
            if missing_read:
                raise PermissionError(f"missing Kraken read permissions: {','.join(missing_read)}")
            if self.config.get("live_enabled", False):
                if "modify-trades" not in normalized:
                    raise PermissionError("missing Kraken trade permission: modify-trades")
                if "close-trades" not in normalized:
                    self.state.metadata["close_trade_permission_missing"] = True
                if "create-ws-token" not in normalized:
                    self.state.metadata["private_ws_permission_missing"] = True
            self._event("KRAKEN_AUTH_CHECK_OK", permission_count=len(permissions))
        except Exception as exc:  # noqa: BLE001 - startup must move to a safe state
            self.state.private_data_healthy = False
            self.state.circuit_breaker = True
            self.state.last_blocker = Blocker.BLOCKED_PRIVATE_DATA
            self.state.system_state = "SAFE_STOP"
            self.store.event("error_events", {"error_code": "AUTH_ERROR", "error": type(exc).__name__, "message": str(exc)}, time.time())
            return

        self.state.system_state = "PRODUCT_DISCOVERY"
        self._event("PRODUCT_DISCOVERY_STARTED")
        instruments = await self.gateway.discover()
        if hasattr(self.news, "set_assets"):
            self.news.set_assets({instrument.base for instrument in instruments if instrument.base})
        for instrument in instruments:
            self.store.upsert_instrument(instrument.instrument_id, asdict(instrument), time.time())
        self.state.metadata["instrument_count"] = len(instruments)
        self.state.metadata["spot_instrument_count"] = sum(i.product_type != ProductType.FUTURES for i in instruments)
        self.state.metadata["futures_instrument_count"] = sum(i.product_type == ProductType.FUTURES for i in instruments)
        self._event("PRODUCT_DISCOVERY_COMPLETED", instrument_count=len(instruments))
        self.state.system_state = "INSTRUMENT_SYNC"
        self._event("INSTRUMENT_SYNC_COMPLETED", instrument_count=len(instruments))

        self.state.system_state = "HISTORY_BACKFILL"
        backfill_limit = int(self.config.get("history_backfill_instruments", 10))
        await self.history.run(instruments[: max(0, backfill_limit)])

        self.state.system_state = "READY"
        self.state.system_ready = True
        self.state.market_data_healthy = True
        self.state.private_data_healthy = True
        self.state.portfolio_consistent = True
        self.state.model_ready = True
        self.state.news_healthy = not self.config.get("news_enabled", True)
        self.state.gemini_healthy = not bool(self.gemini.api_key)
        self.state.gemini_status = "ready" if self.gemini.api_key else "disabled"
        self.state.margin_safe = True
        self._event("RECOVERY_COMPLETED")

    async def close(self) -> None:
        await self.gateway.close()
        self.store.close()
        self.state.system_ready = False

    async def recover(self) -> bool:
        """Rebuild exchange truth after disconnects, sequence gaps, unknown orders or mismatch."""
        async with self._lock:
            self.state.system_state = "RECOVERY"
            self._event("RECOVERY_STARTED")
            try:
                status = await self.gateway.system_status()
                if status not in {"online", "post_only"}:
                    raise RuntimeError(f"Kraken status {status}")
                await self.gateway.authenticate()
                instruments = await self.gateway.discover()
                if hasattr(self.news, "set_assets"):
                    self.news.set_assets({instrument.base for instrument in instruments if instrument.base})
                portfolio = await self.gateway.portfolio()
                portfolio = self._update_derived_portfolio_metrics(portfolio)
                if portfolio.equity <= 0:
                    raise RuntimeError("non-positive reconciled equity")
                for instrument in instruments:
                    self.store.upsert_instrument(instrument.instrument_id, asdict(instrument), time.time())
                self.state.portfolio = portfolio
                self.state.portfolio_consistent = True
                self.state.market_data_healthy = True
                self.state.private_data_healthy = True
                self.state.circuit_breaker = False
                self.state.system_ready = True
                self.state.system_state = "READY"
                self._event("PORTFOLIO_RECONCILIATION_OK")
                self._event("RECOVERY_COMPLETED")
                return True
            except Exception as exc:  # noqa: BLE001
                self.state.circuit_breaker = True
                self.state.portfolio_consistent = False
                self.state.system_state = "SAFE_STOP"
                self.state.last_blocker = Blocker.BLOCKED_RECONCILIATION
                self.store.event("error_events", {"error_code": "API_ERROR", "stage": "RECOVERY", "error": type(exc).__name__, "message": str(exc)}, time.time())
                return False

    async def reconcile(self) -> PortfolioSnapshot | None:
        """Explicit read-only reconciliation; never creates an order."""
        async with self._lock:
            try:
                portfolio = await self.gateway.portfolio()
                self.state.portfolio = portfolio
                self.state.portfolio_consistent = True
                self._event("PORTFOLIO_RECONCILIATION_OK")
                return portfolio
            except Exception as exc:  # noqa: BLE001
                self.state.portfolio_consistent = False
                self.state.last_blocker = Blocker.BLOCKED_RECONCILIATION
                self.store.event("error_events", {"error_code": "API_ERROR", "stage": "RECONCILIATION", "error": type(exc).__name__, "message": str(exc)}, time.time())
                return None

    async def cycle(self) -> RuntimeState:
        async with self._lock:
            if self.state.circuit_breaker or not self.state.system_ready:
                return self.state
            cycle_id = uuid.uuid4().hex
            started = time.time()
            self.store.save_cycle(cycle_id, started, "CYCLE_START")
            self.state.system_state = "CYCLE_START"
            self.state.last_blocker = ""
            self._event("CYCLE_START", cycle_id=cycle_id)
            try:
                instruments = [
                    instrument
                    for instrument in self.gateway.instruments.values()
                    if instrument.status in {"online", "post_only"}
                ]
                if not instruments:
                    self.state.last_blocker = Blocker.BLOCKED_INSTRUMENT
                    self.store.complete_cycle(cycle_id, time.time(), "NO_ACTION", self.state.last_blocker)
                    return self.state

                self.state.system_state = "PORTFOLIO_SNAPSHOT"
                portfolio = await self.gateway.portfolio()
                orders_today = self.store.count_orders_today(self._start_of_day())
                portfolio = replace(portfolio, orders_today=orders_today)
                self.state.portfolio = portfolio
                self.store.save_portfolio_snapshot(cycle_id, asdict(portfolio), time.time())
                for position in portfolio.positions:
                    self.store.save_portfolio_position(cycle_id, position.symbol, asdict(position), time.time())
                self._event("PORTFOLIO_UPDATED", cycle_id=cycle_id)
                self.state.private_data_healthy = True
                self.state.portfolio_consistent = True
                portfolio = self._update_derived_portfolio_metrics(portfolio)
                self.state.portfolio = portfolio

                if portfolio.equity <= 0:
                    self.state.last_blocker = Blocker.BLOCKED_PORTFOLIO
                    self.store.complete_cycle(cycle_id, time.time(), "NO_ACTION", self.state.last_blocker)
                    return self.state
                self.state.margin_safe = portfolio.used_margin / portfolio.equity < self.limits.max_margin
                if not self.state.margin_safe:
                    self.state.last_blocker = Blocker.BLOCKED_MARGIN
                    self.store.complete_cycle(cycle_id, time.time(), "NO_ACTION", self.state.last_blocker)
                    return self.state
                if orders_today >= self.limits.max_orders_per_day:
                    self.state.last_blocker = Blocker.BLOCKED_ORDER_LIMIT
                    self.store.complete_cycle(cycle_id, time.time(), "NO_ACTION", self.state.last_blocker)
                    return self.state

                self.state.system_state = "NEWS_ANALYSIS"
                news_events = await self.news.fetch() if self.config.get("news_enabled", True) else []
                self.state.news_healthy = self.news.last_fetch_ok or not self.config.get("news_enabled", True)
                for event in news_events:
                    self.store.event("news_events", asdict(event), time.time())
                self.state.last_news_event = news_events[0].title if news_events else ""
                self._event("NEWS_ANALYSIS", count=len(news_events))

                self.state.system_state = "GEMINI_ANALYSIS"
                gemini_map: dict[str, Any] = {}
                if news_events and self.gemini.api_key:
                    for event in news_events[:10]:
                        result = await self.gemini.analyze(event)
                        if result:
                            gemini_map[event.title] = result
                            self.store.event("gemini_analysis", asdict(result), time.time())
                self.state.gemini_healthy = self.gemini.healthy if self.gemini.api_key else True
                self.state.gemini_status = "ready" if self.gemini.api_key and self.state.gemini_healthy else "degraded" if self.gemini.api_key else "disabled"

                self.state.system_state = "MARKET_DISCOVERY"
                eligible = [
                    instrument for instrument in instruments
                    if not (instrument.product_type == ProductType.FUTURES and not self.gateway.futures_key)
                    and not (instrument.product_type == ProductType.SPOT_MARGIN and not instrument.margin)
                    and (instrument.long or instrument.short)
                ]
                coarse = await self.gateway.fast_scan(eligible) if hasattr(self.gateway, "fast_scan") else []
                if not coarse:
                    coarse = []
                    for instrument in eligible:
                        try:
                            snap = await self.gateway.market_snapshot(instrument)
                            coarse.append((instrument, snap.last, snap.bid, snap.ask, snap.volume_24h, snap.timestamp))
                        except Exception as exc:  # noqa: BLE001 - bad instruments are filtered, not fatal
                            self.store.event("error_events", {"error_code": "DATA_ERROR", "symbol": instrument.symbol, "error": type(exc).__name__, "message": str(exc)}, time.time())
                min_liquidity = Decimal(str(self.config.get("minimum_liquidity", 100000)))
                max_spread = Decimal(str(self.config.get("max_spread", "0.004")))
                coarse = [
                    row for row in coarse
                    if time.time() - row[5] <= float(self.config.get("data_freshness", 20))
                    and row[4] >= min_liquidity
                    and row[1] > 0
                    and (row[3] - row[2]) / row[1] <= max_spread
                ]
                coarse.sort(key=lambda row: (abs(row[3] - row[2]) / row[1], -row[4]))
                candidates: list[tuple[Any, Any, Any]] = []
                for instrument, *_ in coarse[:20]:
                    try:
                        snapshot = await self.gateway.market_snapshot(instrument)
                    except Exception as exc:  # noqa: BLE001 - bad instruments are filtered, not fatal
                        self.store.event("error_events", {"error_code": "DATA_ERROR", "symbol": instrument.symbol, "error": type(exc).__name__, "message": str(exc)}, time.time())
                        continue
                    if time.time() - snapshot.timestamp > float(self.config.get("data_freshness", 20)) or snapshot.volume_24h < min_liquidity or snapshot.spread_ratio > max_spread or not snapshot.book.bids or not snapshot.book.asks:
                        continue
                    previous_oi = self._last_open_interest.get(snapshot.instrument.symbol)
                    features = compute_features(snapshot, previous_open_interest=previous_oi)
                    if snapshot.open_interest is not None:
                        self._last_open_interest[snapshot.instrument.symbol] = snapshot.open_interest
                    regime = detect_regime(features)
                    self.store.save_market_snapshot(cycle_id, instrument.symbol, {"features": asdict(features), "regime": regime.value, "snapshot": asdict(snapshot)}, snapshot.timestamp)
                    candidates.append((snapshot, features, regime))
                self.state.market_data_healthy = bool(candidates)
                if not candidates:
                    self.state.last_blocker = Blocker.BLOCKED_MARKET_DATA
                    self.store.complete_cycle(cycle_id, time.time(), "NO_ACTION", self.state.last_blocker)
                    return self.state

                candidates.sort(key=lambda item: abs(item[1].momentum) + item[1].liquidity_score, reverse=True)
                for snapshot, features, regime in candidates[:20]:
                    signal = self._build_signal(snapshot, features, news_events, gemini_map)
                    self.state.active_regime = regime
                    self.state.expected_edge = signal.expected_return
                    self.state.active_strategy = signal.strategy_version
                    self.state.active_model = signal.model_version
                    self._event("SIGNAL_CREATED", cycle_id=cycle_id, symbol=snapshot.instrument.symbol, long=float(signal.long_score), short=float(signal.short_score), edge=float(signal.expected_return))

                    position = self._find_position(portfolio, snapshot.instrument)
                    state, effective_direction, target_notional = self._decision_state(position, snapshot.instrument, signal, portfolio)
                    if state == DecisionState.NO_POSITION:
                        continue
                    desired_supported = (effective_direction == Direction.LONG and snapshot.instrument.long) or (effective_direction == Direction.SHORT and snapshot.instrument.short)
                    if effective_direction is not None and not desired_supported:
                        if position is not None:
                            state = DecisionState.CLOSE_LONG if position.direction == Direction.LONG else DecisionState.CLOSE_SHORT
                            effective_direction = position.direction
                            target_notional = position.notional
                        else:
                            self.state.last_blocker = Blocker.BLOCKED_INSTRUMENT
                            self.learning.record_non_trade(uuid.uuid4().hex, signal.expected_return)
                            continue

                    costs = self._estimate_costs(snapshot, position is not None and state in {
                        DecisionState.REDUCE_LONG,
                        DecisionState.CLOSE_LONG,
                        DecisionState.REDUCE_SHORT,
                        DecisionState.CLOSE_SHORT,
                    })
                    risk_state = self._risk_state(state)
                    leverage = Decimal("1")
                    if risk_state not in {DecisionState.REDUCE_LONG, DecisionState.CLOSE_LONG, DecisionState.REDUCE_SHORT, DecisionState.CLOSE_SHORT}:
                        leverage = self.risk.choose_leverage(
                            signal,
                            features.realized_vol,
                            features.liquidity_score,
                            portfolio.used_margin / portfolio.equity,
                            portfolio.drawdown,
                            snapshot.instrument,
                        )

                    expected = signal.expected_return
                    minimum_edge = Decimal(str(self.config.get("minimum_expected_edge", "0.003")))
                    minimum_confidence = Decimal(str(self.config.get("minimum_confidence", "0.55")))
                    if state in {DecisionState.OPEN_LONG, DecisionState.OPEN_SHORT, DecisionState.INCREASE_LONG, DecisionState.INCREASE_SHORT}:
                        if expected < minimum_edge:
                            self.state.last_blocker = Blocker.BLOCKED_EXPECTED_EDGE
                            self.learning.record_non_trade(uuid.uuid4().hex, expected)
                            continue
                        if signal.confidence < minimum_confidence:
                            self.state.last_blocker = Blocker.BLOCKED_RISK
                            self.learning.record_non_trade(uuid.uuid4().hex, expected)
                            continue
                        if costs.total >= expected:
                            self.state.last_blocker = Blocker.BLOCKED_COST
                            self.learning.record_non_trade(uuid.uuid4().hex, expected)
                            continue

                    requested_notional = target_notional
                    risk = self.risk.evaluate(portfolio, snapshot.instrument, signal, costs, risk_state, requested_notional, leverage)
                    decision_id = uuid.uuid4().hex
                    prediction = self.learning.create_prediction(decision_id, signal.confidence, expected, time.time())
                    self.store.save_prediction(decision_id, asdict(prediction), prediction.created_at)
                    self._decision_predictions[decision_id] = decision_id
                    decision = Decision(
                        state,
                        snapshot.instrument.symbol,
                        effective_direction,
                        risk.notional,
                        risk.leverage,
                        expected,
                        signal.confidence,
                        risk.blocker,
                        risk.reason,
                        decision_id,
                        cycle_id,
                    )
                    decision_time = time.time()
                    self.store.save_decision(decision_id, cycle_id, snapshot.instrument.symbol, asdict(decision), decision_time)
                    self.store.save_decision_check(decision_id, "risk", risk.allowed, risk.reason, decision_time)
                    self.store.save_decision_check(decision_id, "edge", expected >= minimum_edge or state not in {DecisionState.OPEN_LONG, DecisionState.OPEN_SHORT, DecisionState.INCREASE_LONG, DecisionState.INCREASE_SHORT}, f"expected={expected} minimum={minimum_edge}", decision_time)
                    self.store.save_decision_check(decision_id, "confidence", signal.confidence >= minimum_confidence or state not in {DecisionState.OPEN_LONG, DecisionState.OPEN_SHORT, DecisionState.INCREASE_LONG, DecisionState.INCREASE_SHORT}, f"confidence={signal.confidence} minimum={minimum_confidence}", decision_time)
                    self.store.save_decision_check(decision_id, "cost", costs.total < expected or state not in {DecisionState.OPEN_LONG, DecisionState.OPEN_SHORT, DecisionState.INCREASE_LONG, DecisionState.INCREASE_SHORT}, f"cost={costs.total} edge={expected}", decision_time)
                    self.store.event("risk_events", {"decision_id": decision_id, "blocker": risk.blocker.value, "allowed": risk.allowed, "reason": risk.reason}, decision_time)
                    self.store.event("learning_events", {"decision_id": decision_id, "type": "prediction", "expected_edge": expected}, time.time())

                    self.state.last_symbol = snapshot.instrument.symbol
                    self.state.last_direction = effective_direction.value if effective_direction else ""
                    if not risk.allowed:
                        self.state.last_blocker = risk.blocker
                        self.learning.record_non_trade(decision_id, expected)
                        continue

                    if self._cooldown_until.get(snapshot.instrument.symbol, 0) > time.time() or self.store.has_open_intent(snapshot.instrument.symbol):
                        self.state.last_blocker = Blocker.BLOCKED_ORDER_LIMIT
                        self.learning.record_non_trade(decision_id, expected)
                        continue

                    quantity = self._quantity_for_action(position, snapshot, risk.notional, state, effective_direction)
                    quantity = max(Decimal("0"), self._quantize_quantity(quantity, snapshot.instrument.lot_precision))
                    if state in {DecisionState.OPEN_LONG, DecisionState.INCREASE_LONG, DecisionState.OPEN_SHORT, DecisionState.INCREASE_SHORT} and effective_direction is not None:
                        position_qty = position.quantity if position and position.direction == effective_direction else Decimal("0")
                        limit = snapshot.instrument.position_limit_long if effective_direction == Direction.LONG else snapshot.instrument.position_limit_short
                        if limit is not None and position_qty + quantity > limit:
                            self.state.last_blocker = Blocker.BLOCKED_RISK
                            self.store.save_decision_check(decision_id, "instrument_position_limit", False, f"requested={position_qty + quantity} limit={limit}", time.time())
                            self.learning.record_non_trade(decision_id, expected)
                            continue
                    self.store.save_decision_check(decision_id, "quantity", quantity >= snapshot.instrument.order_min and (quantity * snapshot.mid) >= snapshot.instrument.cost_min, f"quantity={quantity}", time.time())
                    price = None
                    order_type = "market"
                    post_only = False
                    if state in {DecisionState.OPEN_LONG, DecisionState.OPEN_SHORT, DecisionState.INCREASE_LONG, DecisionState.INCREASE_SHORT}:
                        order_type, price, post_only = self.execution.plan(snapshot, effective_direction, risk.notional, quantity, risk.leverage, urgency="0.5")
                    else:
                        order_type, price, post_only = self.execution.plan(snapshot, effective_direction, risk.notional, quantity, Decimal("1"), urgency="0.9")
                    if order_type == "blocked" or quantity <= 0:
                        self.state.last_blocker = Blocker.BLOCKED_COST if order_type == "blocked" else Blocker.BLOCKED_POSITION_SIZE
                        self.learning.record_non_trade(decision_id, expected)
                        continue

                    client_order_id = str(uuid.uuid4())
                    intent_direction = effective_direction
                    effect = "OPEN"
                    reduce_only = False
                    if state in {DecisionState.REDUCE_LONG, DecisionState.CLOSE_LONG, DecisionState.REDUCE_SHORT, DecisionState.CLOSE_SHORT}:
                        effect = "CLOSE" if state in {DecisionState.CLOSE_LONG, DecisionState.CLOSE_SHORT} else "REDUCE"
                        reduce_only = snapshot.instrument.product_type == ProductType.FUTURES or snapshot.instrument.product_type == ProductType.SPOT_MARGIN
                    elif state in {DecisionState.REVERSE_LONG_TO_SHORT, DecisionState.REVERSE_SHORT_TO_LONG}:
                        effect = "CLOSE"
                        reduce_only = True
                        intent_direction = position.direction if position else effective_direction

                    intent_symbol = snapshot.instrument.symbol if snapshot.instrument.product_type == ProductType.FUTURES else snapshot.instrument.altname
                    intent = OrderIntent(
                        cycle_id,
                        decision_id,
                        uuid.uuid4().hex,
                        client_order_id,
                        intent_symbol,
                        snapshot.instrument.product_type,
                        intent_direction,
                        effect,
                        order_type,
                        price,
                        quantity,
                        risk.leverage,
                        reduce_only,
                        post_only,
                        signal.strategy_version,
                        signal.model_version,
                        self._config_hash(),
                        time.time(),
                    )
                    await self._submit(intent)
                    break

                self.state.system_state = "CYCLE_COMPLETE"
                self.store.complete_cycle(cycle_id, time.time(), "CYCLE_COMPLETE", self.state.last_blocker)
                self._event("CYCLE_END", cycle_id=cycle_id, blocker=self.state.last_blocker)
                return self.state
            except Exception as exc:  # noqa: BLE001 - unexpected failure is fail-safe
                self.state.system_state = "SAFE_STOP"
                self.state.circuit_breaker = True
                self.state.last_blocker = Blocker.CIRCUIT_BREAKER
                self.store.event("error_events", {"error_code": "API_ERROR", "stage": self.state.system_state, "error": type(exc).__name__, "message": str(exc)}, time.time())
                self.store.complete_cycle(cycle_id, time.time(), "FAILED", self.state.last_blocker)
                return self.state

    def _build_signal(self, snapshot, features, news_events, gemini_map) -> Signal:
        news_score = Decimal("0")
        gemini_score = Decimal("0")
        asset_news = [event for event in news_events if event.asset and event.asset.upper() == snapshot.instrument.base.upper()]
        if asset_news:
            news_score = sum(
                (
                    event.impact * (Decimal("1") if event.direction == "bullish" else Decimal("-1") if event.direction == "bearish" else Decimal("0"))
                    * event.novelty
                    * event.credibility
                    for event in asset_news
                ),
                Decimal("0"),
            ) / Decimal(len(asset_news))
        for event in asset_news:
            analysis = gemini_map.get(event.title)
            if analysis:
                sign = Decimal("1") if analysis.direction == "bullish" else Decimal("-1") if analysis.direction == "bearish" else Decimal("0")
                gemini_score += analysis.impact * analysis.confidence * analysis.novelty * analysis.market_confirmation * sign
        long_score = Decimal("0.5") + features.momentum * Decimal("3") + features.imbalance * Decimal("0.2") + news_score * Decimal("0.25") + gemini_score * Decimal("0.10")
        short_score = Decimal("0.5") - features.momentum * Decimal("3") - features.imbalance * Decimal("0.2") - news_score * Decimal("0.25") - gemini_score * Decimal("0.10")
        long_score = min(Decimal("0.99"), max(Decimal("0.01"), long_score))
        short_score = min(Decimal("0.99"), max(Decimal("0.01"), short_score))
        confidence = self.learning.calibrated(max(long_score, short_score))
        expected = abs(long_score - short_score) * Decimal("0.05") * self.learning.calibration.edge_factor
        return Signal(
            long_score,
            short_score,
            confidence,
            max(Decimal("0"), expected),
            max(Decimal("0.01"), features.downside_vol),
            "v1-baseline",
            self.learning.registry.active,
        )

    @staticmethod
    def _risk_state(state: DecisionState) -> DecisionState:
        if state == DecisionState.REVERSE_LONG_TO_SHORT:
            return DecisionState.CLOSE_LONG
        if state == DecisionState.REVERSE_SHORT_TO_LONG:
            return DecisionState.CLOSE_SHORT
        return state

    def _decision_state(self, position: Position | None, instrument: Instrument, signal: Signal, portfolio: PortfolioSnapshot) -> tuple[DecisionState, Direction | None, Decimal]:
        direction = signal.preferred_direction
        if position is None:
            if direction is None:
                return DecisionState.NO_POSITION, None, Decimal("0")
            return (
                DecisionState.OPEN_LONG if direction == Direction.LONG else DecisionState.OPEN_SHORT,
                direction,
                portfolio.equity * self.limits.max_position_risk,
            )
        current = position.direction
        if direction is None:
            return (
                DecisionState.CLOSE_LONG if current == Direction.LONG else DecisionState.CLOSE_SHORT,
                current,
                position.notional,
            )
        if direction != current:
            return (
                DecisionState.REVERSE_LONG_TO_SHORT if current == Direction.LONG else DecisionState.REVERSE_SHORT_TO_LONG,
                direction,
                position.notional,
            )
        target = portfolio.equity * self.limits.max_position_risk
        if position.notional > target * Decimal("1.20"):
            return (DecisionState.REDUCE_LONG if current == Direction.LONG else DecisionState.REDUCE_SHORT, current, position.notional - target)
        if position.notional < target * Decimal("0.80") and signal.expected_return >= Decimal(str(self.config.get("minimum_expected_edge", "0.003"))):
            return (DecisionState.INCREASE_LONG if current == Direction.LONG else DecisionState.INCREASE_SHORT, current, target - position.notional)
        return DecisionState.NO_POSITION, current, position.notional

    @staticmethod
    def _find_position(portfolio: PortfolioSnapshot, instrument: Instrument) -> Position | None:
        aliases = {instrument.symbol, instrument.instrument_id, instrument.altname}
        for position in portfolio.positions:
            if position.symbol in aliases:
                return position
        return None

    @staticmethod
    def _quantize_quantity(value: Decimal, decimals: int) -> Decimal:
        quantum = Decimal(1).scaleb(-decimals)
        return value.quantize(quantum)

    @staticmethod
    def _quantity_for_action(position: Position | None, snapshot, notional: Decimal, state: DecisionState, direction: Direction | None) -> Decimal:
        mid = max(snapshot.mid, Decimal("0.00000001"))
        if state in {DecisionState.CLOSE_LONG, DecisionState.CLOSE_SHORT, DecisionState.REVERSE_LONG_TO_SHORT, DecisionState.REVERSE_SHORT_TO_LONG} and position:
            return position.quantity
        if state in {DecisionState.REDUCE_LONG, DecisionState.REDUCE_SHORT} and position:
            target_quantity = max(Decimal("0"), notional / mid)
            return min(position.quantity, target_quantity)
        return max(Decimal("0"), notional / mid)

    def _estimate_costs(self, snapshot, risk_reducing: bool = False) -> CostEstimate:
        fee = snapshot.last * snapshot.instrument.fee_rate / max(snapshot.last, Decimal("1"))
        spread = snapshot.spread_ratio
        slippage = max(spread * Decimal("0.5"), Decimal("0.0002"))
        depth_value = sum((quantity * price for price, quantity in snapshot.book.asks[:10]), Decimal("0"))
        impact = min(Decimal("0.01"), snapshot.spread_ratio + Decimal("1") / max(Decimal("1"), depth_value))
        funding = snapshot.funding or Decimal("0")
        financing = Decimal("0") if risk_reducing else Decimal("0.0002")
        safety_buffer = Decimal("0.0002")
        return CostEstimate(fee / max(snapshot.last, Decimal("1")), spread, slippage, impact, funding, financing, safety_buffer)

    async def handle_execution_event(self, message: dict[str, Any]) -> None:
        """Materialize exchange execution state and complete predictions when realized PnL is supplied."""
        rows = message.get("data") if isinstance(message.get("data"), list) else [message]
        state_map = {
            "trade": "PARTIALLY_FILLED",
            "filled": "FILLED",
            "canceled": "CANCELED",
            "expired": "EXPIRED",
            "new": "LIVE",
            "pending_new": "SUBMITTING",
        }
        for row in rows:
            if not isinstance(row, dict):
                continue
            client_id = str(row.get("cl_ord_id", row.get("cliOrdId", "")))
            order_id = str(row.get("order_id", row.get("orderId", "")))
            if not client_id and not order_id:
                continue
            existing = self.store.order_by_client_order_id(client_id) if client_id else None
            if existing is None and order_id:
                existing = self.store.order_by_kraken_order_id(order_id)
            if existing is None:
                continue
            exec_type = str(row.get("exec_type", row.get("event", ""))).lower()
            if exec_type == "trade":
                self.store.save_fill(existing["intent_id"], row, time.time())
            new_state = state_map.get(exec_type)
            if new_state:
                self.store.update_order(existing["intent_id"], new_state, row, time.time(), order_id)
                self.store.event("app_events", {
                    "event_code": "ORDER_STATUS_CHANGED",
                    "intent_id": existing["intent_id"],
                    "kraken_order_id": order_id,
                    "state": new_state,
                }, time.time())
            self.store.event("app_events", {
                "event_code": "ORDER_FILL" if exec_type == "trade" else "ORDER_STATUS_CHANGED",
                "intent_id": existing["intent_id"],
                "execution": row,
            }, time.time())
            realized = row.get("realized_pnl", row.get("realizedPnl", row.get("pnl")))
            if realized is not None and existing["decision_id"]:
                try:
                    realized_decimal = Decimal(str(realized))
                    fee = Decimal(str(row.get("fees", row.get("fee", "0"))))
                    self.learning.record_outcome(existing["decision_id"], realized_decimal, abs(fee))
                    self.store.save_prediction_outcome(existing["decision_id"], {"realized": realized_decimal, "fee": fee, "execution": row}, time.time())
                    self.store.event("learning_events", {"decision_id": existing["decision_id"], "type": "outcome", "realized": realized_decimal}, time.time())
                except Exception:  # noqa: BLE001 - malformed exchange metadata must not crash event processing
                    self.store.event("error_events", {"error_code": "DATA_ERROR", "stage": "OUTCOME_TRACKING", "intent_id": existing["intent_id"]}, time.time())

    async def _submit(self, intent: OrderIntent) -> None:
        now = time.time()
        self.store.save_order(intent.intent_id, intent.decision_id, intent.client_order_id, OrderState.INTENT_CREATED, asdict(intent), now)
        self._event("PRETRADE_CHECK", intent_id=intent.intent_id, decision_id=intent.decision_id, symbol=intent.symbol)
        if not self.config.get("live_enabled", False):
            self.store.update_order(intent.intent_id, OrderState.CANCELED, {**asdict(intent), "paper": True, "terminal_reason": "LIVE_DISABLED"}, time.time())
            self.state.last_action = "PAPER_INTENT"
            self.state.last_trade = f"paper:{intent.symbol}:{intent.direction.value}:{intent.effect}"
            self._cooldown_until[intent.symbol] = time.time() + 30
            self.store.event("app_events", {"event_code": "ORDER_CANCELED", "intent_id": intent.intent_id, "reason": "PAPER"}, time.time())
            return
        try:
            self.store.update_order(intent.intent_id, OrderState.PRECHECK_PASSED, asdict(intent), time.time())
            self.store.update_order(intent.intent_id, OrderState.SUBMITTING, asdict(intent), time.time())
            self._event("ORDER_SUBMITTING", intent_id=intent.intent_id, client_order_id=intent.client_order_id)
            order_id, payload = await self.gateway.submit(intent)
            if not order_id:
                raise RuntimeError("Kraken acknowledged without order id")
            self.store.update_order(intent.intent_id, OrderState.ACKNOWLEDGED, payload, time.time(), order_id)
            self._event("ORDER_RECONCILING", intent_id=intent.intent_id, kraken_order_id=order_id)
            reconciled = None
            if hasattr(self.gateway, "reconcile_order"):
                try:
                    reconciled = await self.gateway.reconcile_order(intent, order_id)
                except Exception as exc:  # noqa: BLE001 - after submission uncertainty is fail-safe
                    self.store.event("error_events", {"error_code": "API_ERROR", "stage": "ORDER_RECONCILIATION", "intent_id": intent.intent_id, "error": type(exc).__name__}, time.time())
            if hasattr(self.gateway, "reconcile_order") and reconciled is None:
                self.store.update_order(intent.intent_id, OrderState.UNKNOWN_RECONCILING, payload, time.time(), order_id)
                self.state.last_blocker = Blocker.BLOCKED_RECONCILIATION
                self.state.circuit_breaker = True
                self._event("ORDER_SUBMISSION_AMBIGUOUS", intent_id=intent.intent_id, kraken_order_id=order_id)
                return
            if isinstance(reconciled, dict):
                raw_status = str(reconciled.get("status", reconciled.get("state", ""))).lower()
                terminal = {"filled": OrderState.FILLED, "canceled": OrderState.CANCELED, "cancelled": OrderState.CANCELED, "expired": OrderState.EXPIRED, "rejected": OrderState.REJECTED}
                final_state = terminal.get(raw_status, OrderState.LIVE)
                self.store.update_order(intent.intent_id, final_state, reconciled, time.time(), order_id)
            else:
                self.store.update_order(intent.intent_id, OrderState.LIVE, payload, time.time(), order_id)
            self.state.last_action = "ORDER_SUBMITTED"
            self.state.last_trade = f"{intent.symbol}:{intent.direction.value}:{order_id}"
            self._cooldown_until[intent.symbol] = time.time() + 30
            self._event("ORDER_ACKNOWLEDGED", intent_id=intent.intent_id, kraken_order_id=order_id)
        except Exception as exc:  # noqa: BLE001
            ambiguous = bool(getattr(exc, "ambiguous", False))
            state = OrderState.UNKNOWN_RECONCILING if ambiguous else OrderState.REJECTED
            self.store.update_order(intent.intent_id, state, {"error": type(exc).__name__, "message": str(exc)}, time.time())
            self.state.last_blocker = Blocker.BLOCKED_RECONCILIATION if ambiguous else Blocker.BLOCKED_ORDER_LIMIT
            self._event("ORDER_SUBMISSION_AMBIGUOUS" if ambiguous else "ORDER_REJECTED", intent_id=intent.intent_id)

    @staticmethod
    def _start_of_day() -> float:
        now = time.time()
        return now - (now % 86400)

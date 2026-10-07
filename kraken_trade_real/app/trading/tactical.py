from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import threading
import time
from typing import Any, Iterable

from app.domain.models import Decision, Instrument, MarketSnapshot, Signal, new_id
from app.domain.states import Direction, OrderState
from app.trading.authority import TradingAuthority


D = Decimal
STRATEGY_VERSION = "tactical-volatility-v1"


@dataclass(frozen=True)
class TacticalSignal:
    symbol: str
    direction: Direction
    score: D
    expected_move_bps: D
    estimated_cost_bps: D
    net_edge_bps: D
    momentum_30s_bps: D
    momentum_3m_bps: D
    volatility_bps: D
    volume_ratio: D
    breakout_bps: D
    book_imbalance: D
    taker_buy_ratio: D
    spread_bps: D
    rationale: dict[str, Any]

    @property
    def confidence(self) -> D:
        return max(D("0"), min(D("1"), self.score))


@dataclass
class TacticalPosition:
    symbol: str
    direction: Direction
    quantity: D
    entry_price: D
    entry_notional_eur: D
    opened_at: float
    peak_price: D
    leverage: D
    margin: bool
    client_order_id: str = ""
    last_price: D = D("0")


class TacticalStrategy:
    """Deterministic short-horizon volatility/momentum evaluator.

    The strategy intentionally trades infrequently: transaction costs dominate
    small-account short-horizon trading, so the signal must clear a conservative
    round-trip cost hurdle before it can request an order.
    """

    @staticmethod
    def _pct_bps(new: D, old: D) -> D:
        if new <= 0 or old <= 0:
            return D("0")
        return (new / old - D("1")) * D("10000")

    @staticmethod
    def _window_price(
        history: deque[tuple[float, D]],
        now: float,
        seconds: float,
    ) -> D | None:
        cutoff = now - seconds
        eligible = [price for ts, price in history if ts <= cutoff and price > 0]
        if eligible:
            return eligible[-1]
        return None

    @staticmethod
    def _realized_volatility_bps(
        history: deque[tuple[float, D]],
        now: float,
        seconds: float,
    ) -> D:
        cutoff = now - seconds
        prices = [price for ts, price in history if ts >= cutoff and price > 0]
        if len(prices) < 6:
            return D("0")
        returns: list[float] = []
        for previous, current in zip(prices, prices[1:]):
            if previous <= 0 or current <= 0:
                continue
            returns.append(math.log(float(current / previous)) * 10000.0)
        if len(returns) < 3:
            return D("0")
        mean = sum(returns) / len(returns)
        variance = sum((item - mean) ** 2 for item in returns) / max(1, len(returns) - 1)
        return D(str(math.sqrt(max(0.0, variance))))

    @staticmethod
    def _volume_ratio(
        trades: Iterable[dict[str, Any]],
        now: float,
    ) -> tuple[D, D]:
        rows = []
        for trade in trades:
            try:
                ts = float(trade.get("timestamp_epoch", trade.get("ts", 0)))
                qty = D(str(trade.get("qty", "0")))
                price = D(str(trade.get("price", "0")))
            except (TypeError, ValueError):
                continue
            if ts > 0 and qty > 0 and price > 0:
                rows.append((ts, qty, price, str(trade.get("side", "")).lower()))
        recent = [(qty * price, side) for ts, qty, price, side in rows if ts >= now - 30]
        baseline = [
            qty * price
            for ts, qty, price, _ in rows
            if now - 150 <= ts < now - 30
        ]
        if not recent or len(baseline) < 3:
            return D("0"), D("0")
        recent_value = sum((value for value, _ in recent), D("0"))
        baseline_value = sum(baseline, D("0")) / D("4")
        ratio = recent_value / baseline_value if baseline_value > 0 else D("0")
        buy_value = sum((value for value, side in recent if side == "buy"), D("0"))
        sell_value = sum((value for value, side in recent if side == "sell"), D("0"))
        total = buy_value + sell_value
        buy_ratio = buy_value / total if total > 0 else D("0.5")
        return ratio, buy_ratio

    @staticmethod
    def _book_imbalance(
        bids: Iterable[tuple[D, D]],
        asks: Iterable[tuple[D, D]],
        levels: int = 5,
    ) -> tuple[D, D]:
        bid_rows = list(bids)[:levels]
        ask_rows = list(asks)[:levels]
        bid_value = sum((price * qty for price, qty in bid_rows if price > 0 and qty > 0), D("0"))
        ask_value = sum((price * qty for price, qty in ask_rows if price > 0 and qty > 0), D("0"))
        total = bid_value + ask_value
        imbalance = (bid_value - ask_value) / total if total > 0 else D("0")
        return imbalance, total

    @staticmethod
    def _impact_bps(quantity: D, bids: Iterable[tuple[D, D]], asks: Iterable[tuple[D, D]]) -> D:
        bid_qty = sum((qty for _, qty in list(bids)[:5] if qty > 0), D("0"))
        ask_qty = sum((qty for _, qty in list(asks)[:5] if qty > 0), D("0"))
        depth = max(D("0.00000001"), bid_qty + ask_qty)
        participation = min(D("1"), quantity / depth)
        return max(D("3"), participation * D("1000"))

    def evaluate(
        self,
        *,
        symbol: str,
        bid: D,
        ask: D,
        last: D,
        history: deque[tuple[float, D]],
        trades: Iterable[dict[str, Any]],
        bids: Iterable[tuple[D, D]],
        asks: Iterable[tuple[D, D]],
        now: float,
        notional_eur: D,
        quote_to_eur_rate: D,
        min_net_edge_bps: D,
        max_spread_bps: D,
        min_volume_ratio: D,
        min_momentum_30s_bps: D,
        min_momentum_3m_bps: D,
        min_volatility_bps: D,
        min_breakout_bps: D,
        min_imbalance: D,
        fee_entry_bps: D,
        fee_exit_bps: D,
        safety_buffer_bps: D,
    ) -> TacticalSignal | None:
        if bid <= 0 or ask <= 0 or last <= 0 or ask < bid:
            return None
        spread = (ask - bid) / ((ask + bid) / D("2")) * D("10000")
        if spread > max_spread_bps:
            return None

        old_30s = self._window_price(history, now, 30)
        old_3m = self._window_price(history, now, 180)
        if old_30s is None or old_3m is None:
            return None

        momentum_30s = self._pct_bps(last, old_30s)
        momentum_3m = self._pct_bps(last, old_3m)
        volatility = self._realized_volatility_bps(history, now, 60)
        if volatility < min_volatility_bps:
            return None

        prior_60 = [price for ts, price in history if now - 60 <= ts < now and price > 0]
        if len(prior_60) < 8:
            return None
        previous_high = max(prior_60)
        previous_low = min(prior_60)
        breakout_long = self._pct_bps(last, previous_high)
        breakout_short = self._pct_bps(last, previous_low)

        volume_ratio, buy_ratio = self._volume_ratio(trades, now)
        if volume_ratio < min_volume_ratio:
            return None

        imbalance, depth_value = self._book_imbalance(bids, asks)
        if not bids or not asks:
            return None

        quote_rate = max(D("0.00000001"), quote_to_eur_rate)
        notional_quote = notional_eur / quote_rate
        quantity = notional_quote / last
        impact = self._impact_bps(quantity, bids, asks)

        # All entries are conservatively costed as taker executions. This
        # prevents a false edge from depending on an optimistic maker fill.
        slippage = max(D("8"), volatility * D("0.75"))
        common_cost = fee_entry_bps + fee_exit_bps + spread + slippage + impact + safety_buffer_bps

        def score_for(
            direction: Direction,
            momentum_fast: D,
            momentum_slow: D,
            breakout: D,
            flow_ratio: D,
            imbalance_value: D,
        ) -> tuple[D, D, dict[str, Any]]:
            aligned_fast = max(D("0"), momentum_fast if direction == Direction.LONG else -momentum_fast)
            aligned_slow = max(D("0"), momentum_slow if direction == Direction.LONG else -momentum_slow)
            aligned_breakout = max(D("0"), breakout if direction == Direction.LONG else -breakout)
            aligned_flow = (
                buy_ratio if direction == Direction.LONG else D("1") - buy_ratio
            )
            aligned_imbalance = (
                imbalance_value if direction == Direction.LONG else -imbalance_value
            )

            momentum_component = min(D("1"), aligned_fast / max(D("1"), min_momentum_30s_bps * D("2")))
            trend_component = min(D("1"), aligned_slow / max(D("1"), min_momentum_3m_bps * D("2")))
            breakout_component = min(D("1"), aligned_breakout / max(D("1"), min_breakout_bps * D("2")))
            volume_component = min(D("1"), (flow_ratio - min_volume_ratio) / max(D("1"), min_volume_ratio))
            flow_component = max(D("0"), min(D("1"), (aligned_flow - D("0.5")) * D("5")))
            book_component = max(D("0"), min(D("1"), (aligned_imbalance - D("0.02")) / D("0.25")))
            spread_component = max(D("0"), min(D("1"), D("1") - spread / max(D("1"), max_spread_bps)))
            score = (
                momentum_component * D("0.20")
                + trend_component * D("0.20")
                + breakout_component * D("0.20")
                + volume_component * D("0.15")
                + flow_component * D("0.10")
                + book_component * D("0.10")
                + spread_component * D("0.05")
            )
            expected_move = max(
                aligned_slow * D("1.10") + aligned_breakout * D("0.75"),
                aligned_fast * D("1.80") + volatility * D("1.10"),
            )
            net_edge = expected_move - common_cost
            rationale = {
                "momentum_30s_bps": str(momentum_fast),
                "momentum_3m_bps": str(momentum_slow),
                "volatility_bps": str(volatility),
                "volume_ratio": str(flow_ratio),
                "breakout_bps": str(breakout),
                "book_imbalance": str(imbalance_value),
                "taker_buy_ratio": str(buy_ratio),
                "spread_bps": str(spread),
                "depth_quote": str(depth_value),
                "impact_bps": str(impact),
                "slippage_bps": str(slippage),
                "cost_bps": str(common_cost),
            }
            return score, net_edge, rationale

        candidates = []
        if momentum_30s >= min_momentum_30s_bps and momentum_3m >= min_momentum_3m_bps:
            if breakout_long >= min_breakout_bps and buy_ratio >= D("0.55") and imbalance >= min_imbalance:
                score, edge, rationale = score_for(
                    Direction.LONG, momentum_30s, momentum_3m, breakout_long, volume_ratio, imbalance
                )
                candidates.append((score, edge, Direction.LONG, max(momentum_30s, momentum_3m), breakout_long, rationale))

        if momentum_30s <= -min_momentum_30s_bps and momentum_3m <= -min_momentum_3m_bps:
            if breakout_short <= -min_breakout_bps and buy_ratio <= D("0.45") and imbalance <= -min_imbalance:
                score, edge, Direction.SHORT, aligned_momentum, breakout, rationale = (
                    score_for(
                        Direction.SHORT, momentum_30s, momentum_3m, breakout_short, volume_ratio, imbalance
                    )[0],
                    score_for(
                        Direction.SHORT, momentum_30s, momentum_3m, breakout_short, volume_ratio, imbalance
                    )[1],
                    Direction.SHORT,
                    max(-momentum_30s, -momentum_3m),
                    breakout_short,
                    score_for(
                        Direction.SHORT, momentum_30s, momentum_3m, breakout_short, volume_ratio, imbalance
                    )[2],
                )
                candidates.append((score, edge, Direction.SHORT, aligned_momentum, breakout, rationale))

        if not candidates:
            return None

        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        score, edge, direction, _, breakout, rationale = candidates[0]
        if score < D("0.68") or edge < min_net_edge_bps:
            return None

        return TacticalSignal(
            symbol=symbol,
            direction=direction,
            score=score,
            expected_move_bps=edge + common_cost,
            estimated_cost_bps=common_cost,
            net_edge_bps=edge,
            momentum_30s_bps=momentum_30s,
            momentum_3m_bps=momentum_3m,
            volatility_bps=volatility,
            volume_ratio=volume_ratio,
            breakout_bps=breakout,
            book_imbalance=imbalance,
            taker_buy_ratio=buy_ratio,
            spread_bps=spread,
            rationale=rationale,
        )


class TacticalEngine:
    """Fast tactical controller sharing the central TradingAuthority."""

    def __init__(
        self,
        config: Any,
        db: Any,
        audit: Any,
        gateway: Any,
        authority: TradingAuthority,
        intents: Any,
        portfolio: Any,
        news: Any,
        stream: Any,
        instruments: list[Instrument],
    ) -> None:
        self.config = config
        self.db = db
        self.audit = audit
        self.gateway = gateway
        self.authority = authority
        self.intents = intents
        self.portfolio = portfolio
        self.news = news
        self.stream = stream
        self.instruments = instruments
        self.strategy = TacticalStrategy()
        self.positions: dict[str, TacticalPosition] = {}
        self.instrument_by_symbol = {i.symbol: i for i in instruments}
        self.price_history: dict[str, deque[tuple[float, D]]] = defaultdict(lambda: deque(maxlen=600))
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._last_portfolio_refresh = 0.0
        self._last_scan_log = 0.0
        self._last_signal: TacticalSignal | None = None
        self._last_reason = "DISABLED"
        self._trades_today = 0
        self._realized_pnl_eur = D("0")
        self._load_from_db()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "tactical_enabled", False))

    def start(self) -> None:
        if not self.enabled or (self.thread and self.thread.is_alive()):
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="kraken-tactical", daemon=True)
        self.thread.start()
        self.audit.emit(
            "TACTICAL_ENGINE_STARTED",
            "INFO",
            mode="SHADOW" if self.config.tactical_shadow_mode else "LIVE",
            poll_seconds=self.config.tactical_poll_interval_seconds,
            strategy_version=STRATEGY_VERSION,
        )

    def stop(self) -> None:
        self.stop_event.set()

    def set_instruments(self, instruments: list[Instrument]) -> None:
        self.instruments = instruments
        self.instrument_by_symbol = {i.symbol: i for i in instruments}

    def configure_stream(self) -> None:
        if not self.enabled:
            return
        candidates = [
            i for i in self.instruments
            if i.venue == "spot"
            and i.tradeable
            and i.product_type.value in {"SPOT", "SPOT_MARGIN"}
            and self._is_tactical_family(i)
        ]
        self.stream.set_symbols([i.symbol for i in candidates[: int(self.config.tactical_universe_size)]])
    
    @staticmethod
    def _is_tactical_family(instrument: Instrument) -> bool:
        base = str(instrument.base or "").upper()
        if base.endswith("X") and len(base) >= 4:
            return False
        blob = json.dumps(instrument.metadata or {}, sort_keys=True, default=str).lower()
        return not any(token in blob for token in ("xstock", "tokenized equity", "tokenised equity", "stock"))

    def stats(self) -> dict[str, Any]:
        open_position = next(iter(self.positions.values()), None)
        today = datetime.now(timezone.utc).date()
        count = self.db.one(
            "SELECT COUNT(*) AS n FROM tactical_trades WHERE date(datetime(exit_time,'unixepoch'))=?",
            (today.isoformat(),),
        )
        pnl = self.db.one(
            "SELECT COALESCE(SUM(CAST(net_pnl_eur AS REAL)),0) AS pnl FROM tactical_trades "
            "WHERE date(datetime(exit_time,'unixepoch'))=?",
            (today.isoformat(),),
        )
        trades_today = int(count["n"]) if count else 0
        pnl_today = D(str(pnl["pnl"] if pnl else "0"))
        self._trades_today = trades_today
        self._realized_pnl_eur = pnl_today
        return {
            "enabled": self.enabled,
            "mode": "SHADOW" if getattr(self.config, "tactical_shadow_mode", True) else "LIVE",
            "status": "POSITION_OPEN" if open_position else ("RUNNING" if self.enabled else "DISABLED"),
            "symbol": open_position.symbol if open_position else "",
            "direction": open_position.direction.value if open_position else "",
            "position_eur": str(open_position.entry_notional_eur if open_position else D("0")),
            "score": str(self._last_signal.score if self._last_signal else D("0")),
            "net_edge_bps": str(self._last_signal.net_edge_bps if self._last_signal else D("0")),
            "trades_today": trades_today,
            "pnl_today_eur": str(pnl_today),
            "last_reason": self._last_reason,
            "stream_symbols": len(self.stream.symbols()),
        }

    def _load_from_db(self) -> None:
        rows = self.db.tactical_positions()
        for row in rows:
            try:
                direction = Direction(str(row["direction"]).upper())
                self.positions[row["symbol"]] = TacticalPosition(
                    symbol=row["symbol"],
                    direction=direction,
                    quantity=D(str(row["quantity"])),
                    entry_price=D(str(row["entry_price"])),
                    entry_notional_eur=D(str(row["entry_notional_eur"])),
                    opened_at=float(row["opened_at"]),
                    peak_price=D(str(row["peak_price"])),
                    leverage=D(str(row["leverage"])),
                    margin=bool(row["margin"]),
                    client_order_id=str(row.get("client_order_id") or ""),
                    last_price=D(str(row.get("last_price") or "0")),
                )
            except (KeyError, TypeError, ValueError):
                self.audit.emit("TACTICAL_POSITION_LOAD_FAILED", "WARNING", symbol=str(row.get("symbol", "")))

    def _run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                if self.enabled:
                    self._reconcile_pending_orders()
                    self._refresh_portfolio_if_due()
                    self._poll()
            except Exception as exc:
                self._last_reason = f"ENGINE_EXCEPTION:{type(exc).__name__}"
                self.audit.emit(
                    "TACTICAL_ENGINE_ERROR",
                    "ERROR",
                    error_type=type(exc).__name__,
                    error=str(exc)[:700],
                )
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.25, float(self.config.tactical_poll_interval_seconds) - elapsed))

    def _poll(self) -> None:
        now = time.time()
        raw_snapshots = self.stream.snapshots()
        if not raw_snapshots:
            self._last_reason = "STREAM_EMPTY"
            return

        for symbol, snap in raw_snapshots.items():
            try:
                last = D(str(snap.get("last", "0")))
                if last > 0:
                    self.price_history[symbol].append((now, last))
            except (TypeError, ValueError):
                continue

        # Exits are always evaluated before new entries.
        for symbol, position in list(self.positions.items()):
            self._manage_position(position, raw_snapshots.get(symbol), now)

        pending = self._pending_tactical_orders()
        if pending or self.positions:
            if pending:
                self._last_reason = "ORDER_PENDING"
            return

        best: TacticalSignal | None = None
        failures: dict[str, int] = {}
        for symbol, snap in raw_snapshots.items():
            instrument = self.instrument_by_symbol.get(symbol)
            if instrument is None:
                continue
            signal = self._evaluate(instrument, snap, now)
            if signal is None:
                continue
            contrary = self._news_filter(instrument, signal)
            if contrary:
                failures[contrary] = failures.get(contrary, 0) + 1
                continue
            if best is None or (signal.score, signal.net_edge_bps) > (best.score, best.net_edge_bps):
                best = signal

        if best is not None:
            self._last_signal = best
            self.audit.emit(
                "TACTICAL_SIGNAL",
                "INFO",
                symbol=best.symbol,
                direction=best.direction.value,
                score=str(best.score),
                expected_move_bps=str(best.expected_move_bps),
                estimated_cost_bps=str(best.estimated_cost_bps),
                net_edge_bps=str(best.net_edge_bps),
                momentum_30s_bps=str(best.momentum_30s_bps),
                momentum_3m_bps=str(best.momentum_3m_bps),
                volatility_bps=str(best.volatility_bps),
                volume_ratio=str(best.volume_ratio),
                breakout_bps=str(best.breakout_bps),
                book_imbalance=str(best.book_imbalance),
            )
            self._enter(best, self.instrument_by_symbol[best.symbol], raw_snapshots[best.symbol])
        elif now - self._last_scan_log >= 30:
            self._last_scan_log = now
            self.audit.emit(
                "TACTICAL_SCAN",
                "INFO",
                subscribed=len(raw_snapshots),
                positions=len(self.positions),
                reason="NO_QUALIFIED_SIGNAL",
                filters=failures,
            )

    def _evaluate(self, instrument: Instrument, raw: dict[str, Any], now: float) -> TacticalSignal | None:
        bid = D(str(raw.get("bid", "0")))
        ask = D(str(raw.get("ask", "0")))
        last = D(str(raw.get("last", "0")))
        rate = self.portfolio.quote_to_eur_rate(instrument.quote)
        if rate is None or rate <= 0:
            return None
        return self.strategy.evaluate(
            symbol=instrument.symbol,
            bid=bid,
            ask=ask,
            last=last,
            history=self.price_history[instrument.symbol],
            trades=raw.get("trades", []),
            bids=[(D(str(x[0])), D(str(x[1]))) for x in raw.get("bids", [])],
            asks=[(D(str(x[0])), D(str(x[1]))) for x in raw.get("asks", [])],
            now=now,
            notional_eur=self._target_notional(),
            quote_to_eur_rate=rate,
            min_net_edge_bps=D(str(self.config.tactical_min_net_edge_bps)),
            max_spread_bps=D(str(self.config.tactical_max_spread_bps)),
            min_volume_ratio=D(str(self.config.tactical_min_volume_ratio)),
            min_momentum_30s_bps=D(str(self.config.tactical_min_momentum_30s_bps)),
            min_momentum_3m_bps=D(str(self.config.tactical_min_momentum_3m_bps)),
            min_volatility_bps=D(str(self.config.tactical_min_volatility_bps)),
            min_breakout_bps=D(str(self.config.tactical_min_breakout_bps)),
            min_imbalance=D(str(self.config.tactical_min_imbalance)),
            fee_entry_bps=D(str(self.config.tactical_entry_fee_bps)),
            fee_exit_bps=D(str(self.config.tactical_exit_fee_bps)),
            safety_buffer_bps=D(str(self.config.tactical_safety_buffer_bps)),
        )

    def _news_filter(self, instrument: Instrument, signal: TacticalSignal) -> str:
        try:
            effect = D(str(self.news.effect_for(instrument.symbol, self.news.collect() if False else [])))
        except Exception:
            return ""
        # The core cycle's last collected news is intentionally not fetched here.
        # The live tactical loop must never perform a network news refresh.
        _ = effect
        return ""

    def _target_notional(self) -> D:
        return D(str(self.config.tactical_max_capital_eur))

    def _risk_ok(self, instrument: Instrument, signal: TacticalSignal, notional: D, leverage: D) -> tuple[bool, str]:
        portfolio = self.portfolio.cached_state() if hasattr(self.portfolio, "cached_state") else None
        if portfolio is None:
            try:
                portfolio = self.portfolio.reconcile()
            except Exception:
                return False, "PORTFOLIO_UNAVAILABLE"
        equity = portfolio.equity_eur
        if equity <= 0:
            return False, "NO_POSITIVE_EQUITY"
        sleeve = min(
            notional,
            equity * D(str(self.config.tactical_portfolio_pct)) / D("100"),
            D(str(self.config.tactical_max_capital_eur)),
        )
        if notional > sleeve:
            return False, "TACTICAL_CAP_EXCEEDED"
        if len(self.positions) >= int(self.config.tactical_max_positions):
            return False, "TACTICAL_POSITION_LIMIT"
        projected_gross = portfolio.gross_eur + notional
        projected_net = abs(portfolio.net_eur + (notional if signal.direction == Direction.LONG else -notional))
        if projected_gross > equity * D(str(self.config.risk_max_gross_pct)) / D("100"):
            return False, "GLOBAL_GROSS_LIMIT"
        if projected_net > equity * D(str(self.config.risk_max_net_pct)) / D("100"):
            return False, "GLOBAL_NET_LIMIT"
        if portfolio.daily_pnl_eur < -(equity * D(str(self.config.risk_daily_loss_pct)) / D("100")):
            return False, "DAILY_LOSS_LIMIT"
        if portfolio.drawdown_pct > D(str(self.config.risk_max_drawdown_pct)):
            return False, "DRAWDOWN_LIMIT"
        if leverage > D(str(self.config.tactical_max_leverage)) or leverage > instrument.max_leverage:
            return False, "TACTICAL_LEVERAGE_LIMIT"
        if leverage > D("1"):
            account = self.portfolio.spot_margin_account
            if not isinstance(account, dict):
                return False, "MARGIN_ACCOUNT_UNAVAILABLE"
            free = D(str(account.get("free_margin") or "0"))
            if free < notional / leverage:
                return False, "MARGIN_FREE_INSUFFICIENT"
        elif signal.direction == Direction.LONG and instrument.quote.upper() in {"EUR", "USD", "GBP", "CHF", "CAD", "JPY", "AUD"}:
            required_quote = notional / max(D("0.00000001"), self.portfolio.quote_to_eur_rate(instrument.quote) or D("0"))
            available = self.portfolio.cash_balance(instrument.quote)
            if available > 0 and available < required_quote:
                return False, "QUOTE_CASH_INSUFFICIENT"
        return True, "TACTICAL_RISK_OK"

    def _choose_leverage(self, instrument: Instrument, direction: Direction) -> D | None:
        if direction == Direction.LONG:
            return D("1")
        if not instrument.short_available:
            return None
        if instrument.product_type.value == "SPOT_MARGIN":
            preferred = D(str(self.config.tactical_short_leverage))
            levels = sorted(level for level in instrument.leverage_levels if level >= D("2"))
            if not levels:
                return None
            return min(preferred, max(levels))
        return D("1")

    def _enter(self, signal: TacticalSignal, instrument: Instrument, raw: dict[str, Any]) -> None:
        notional = self._target_notional()
        leverage = self._choose_leverage(instrument, signal.direction)
        if leverage is None:
            self._last_reason = "SHORT_NOT_AVAILABLE_OR_NO_LEVERAGE"
            return
        ok, reason = self._risk_ok(instrument, signal, notional, leverage)
        if not ok:
            self._last_reason = reason
            self.audit.emit("TACTICAL_ORDER_BLOCKED", "INFO", symbol=instrument.symbol, direction=signal.direction.value, reason=reason)
            return

        rate = self.portfolio.quote_to_eur_rate(instrument.quote)
        if rate is None or rate <= 0:
            self._last_reason = "FX_RATE_UNAVAILABLE"
            return
        price = D(str(raw.get("ask" if signal.direction == Direction.LONG else "bid", raw.get("last", "0"))))
        quantity = self.portfolio.quantity_for_eur(instrument, notional, price)
        if quantity is None or quantity <= 0:
            self._last_reason = "QUANTITY_UNAVAILABLE"
            return
        if quantity < instrument.min_order_qty:
            self._last_reason = "MIN_ORDER_QTY"
            return
        if instrument.venue != "futures" and quantity * price < instrument.min_cost:
            self._last_reason = "MIN_ORDER_COST"
            return

        if self.config.tactical_shadow_mode:
            self._shadow_entry(signal, instrument, quantity, price, notional, leverage)
            return

        signal_model = Signal(
            symbol=instrument.symbol,
            direction=signal.direction,
            expected_return_bps=signal.expected_move_bps,
            expected_cost_bps=signal.estimated_cost_bps,
            confidence=signal.confidence,
            regime="TACTICAL_VOLATILITY",
            news_effect_bps=D("0"),
            gemini_effect_bps=D("0"),
            features={
                "tactical_score": signal.score,
                "tactical_momentum_30s_bps": signal.momentum_30s_bps,
                "tactical_momentum_3m_bps": signal.momentum_3m_bps,
                "tactical_volatility_bps": signal.volatility_bps,
                "tactical_volume_ratio": signal.volume_ratio,
                "tactical_breakout_bps": signal.breakout_bps,
                "tactical_book_imbalance": signal.book_imbalance,
                "tactical_net_edge_bps": signal.net_edge_bps,
            },
        )
        decision = Decision(
            decision_id=new_id("decision"),
            instrument=instrument,
            signal=signal_model,
            target_notional_eur=notional,
            leverage=leverage,
            rationale={
                "strategy": STRATEGY_VERSION,
                "action": "ENTRY",
                "tactical_signal": signal.rationale,
                "entry_price": str(price),
                "entry_notional_eur": str(notional),
            },
            strategy_version=STRATEGY_VERSION,
            model_version=STRATEGY_VERSION,
            config_hash="tactical",
            current_position_eur=D("0"),
            target_position_eur=notional if signal.direction == Direction.LONG else -notional,
            execution_direction=signal.direction,
            reduce_only=False,
        )
        self.db.save_decision(decision)
        intent = self.intents.build(
            decision,
            leverage,
            "market",
            quantity,
            None,
            reduce_only=False,
            post_only=False,
        )
        result = self.authority.submit(intent, MarketSnapshot(
            instrument.symbol, price, D(str(raw["bid"])), D(str(raw["ask"])),
            D("0"), time.time(),
        ))
        self.db.learning_event(
            "TACTICAL_ORDER",
            decision.decision_id,
            {"action": "ENTRY", "result": result, "signal": signal.rationale},
        )
        self._last_reason = str(result.get("reason") or result.get("state") or "ENTRY_SUBMITTED")
        if result.get("state") == OrderState.ACKNOWLEDGED.value:
            self.audit.emit(
                "TACTICAL_ENTRY_SUBMITTED",
                "INFO",
                symbol=instrument.symbol,
                direction=signal.direction.value,
                quantity=str(quantity),
                notional_eur=str(notional),
                leverage=str(leverage),
            )

    def _shadow_entry(self, signal: TacticalSignal, instrument: Instrument, quantity: D, price: D, notional: D, leverage: D) -> None:
        position = TacticalPosition(
            symbol=instrument.symbol,
            direction=signal.direction,
            quantity=quantity,
            entry_price=price,
            entry_notional_eur=notional,
            opened_at=time.time(),
            peak_price=price,
            leverage=leverage,
            margin=instrument.product_type.value != "SPOT",
            last_price=price,
        )
        self.positions[instrument.symbol] = position
        self.db.save_tactical_position(position)
        self.db.learning_event(
            "TACTICAL_SHADOW_ENTRY",
            signal.symbol,
            {
                "direction": signal.direction.value,
                "price": str(price),
                "quantity": str(quantity),
                "notional_eur": str(notional),
                "leverage": str(leverage),
                "signal": signal.rationale,
            },
        )
        self.audit.emit(
            "TACTICAL_SHADOW_ENTRY",
            "INFO",
            symbol=signal.symbol,
            direction=signal.direction.value,
            price=str(price),
            notional_eur=str(notional),
            leverage=str(leverage),
        )
        self._last_reason = "SHADOW_ENTRY"

    def _manage_position(self, position: TacticalPosition, raw: dict[str, Any] | None, now: float) -> None:
        if raw is None:
            raw = self._fallback_ticker(position.symbol)
        if not raw:
            self._last_reason = "POSITION_MARKET_DATA_UNAVAILABLE"
            return
        last = D(str(raw.get("last", "0")))
        bid = D(str(raw.get("bid", last)))
        ask = D(str(raw.get("ask", last)))
        if last <= 0:
            return

        if position.direction == Direction.LONG:
            favorable = max(position.peak_price, last)
            position.peak_price = favorable
            profit_pct = (last / position.entry_price - D("1")) * D("100")
            stop_trigger = last <= position.entry_price * (D("1") - D(str(self.config.tactical_stop_loss_pct)) / D("100"))
            target_trigger = last >= position.entry_price * (D("1") + D(str(self.config.tactical_take_profit_pct)) / D("100"))
            trailing_trigger = (
                favorable > position.entry_price
                and last <= favorable * (D("1") - D(str(self.config.tactical_trailing_stop_pct)) / D("100"))
            )
        else:
            favorable = min(position.peak_price if position.peak_price > 0 else position.entry_price, last)
            position.peak_price = favorable
            profit_pct = (position.entry_price / last - D("1")) * D("100")
            stop_trigger = last >= position.entry_price * (D("1") + D(str(self.config.tactical_stop_loss_pct)) / D("100"))
            target_trigger = last <= position.entry_price * (D("1") - D(str(self.config.tactical_take_profit_pct)) / D("100"))
            trailing_trigger = (
                favorable < position.entry_price
                and last >= favorable * (D("1") + D(str(self.config.tactical_trailing_stop_pct)) / D("100"))
            )

        time_trigger = now >= position.opened_at + int(self.config.tactical_max_hold_seconds)
        reason = ""
        if stop_trigger:
            reason = "STOP_LOSS"
        elif target_trigger:
            reason = "TAKE_PROFIT"
        elif trailing_trigger:
            reason = "TRAILING_STOP"
        elif time_trigger:
            reason = "TIME_STOP"
        if not reason:
            self.db.save_tactical_position(position)
            return

        self._exit(position, reason, bid, ask, last)

    def _exit(self, position: TacticalPosition, reason: str, bid: D, ask: D, last: D) -> None:
        instrument = self.instrument_by_symbol.get(position.symbol)
        if instrument is None:
            return
        exit_direction = Direction.SHORT if position.direction == Direction.LONG else Direction.LONG
        exit_price = bid if position.direction == Direction.LONG else ask
        notional = position.entry_notional_eur
        if self.config.tactical_shadow_mode:
            self._shadow_exit(position, reason, exit_price)
            return

        signal = Signal(
            symbol=instrument.symbol,
            direction=exit_direction,
            expected_return_bps=D("0"),
            expected_cost_bps=D("0"),
            confidence=D("1"),
            regime="TACTICAL_EXIT",
            news_effect_bps=D("0"),
            gemini_effect_bps=D("0"),
            features={"tactical_exit_reason": D("1")},
        )
        decision = Decision(
            decision_id=new_id("decision"),
            instrument=instrument,
            signal=signal,
            target_notional_eur=notional,
            leverage=position.leverage,
            rationale={
                "strategy": STRATEGY_VERSION,
                "action": "EXIT",
                "exit_reason": reason,
                "entry_price": str(position.entry_price),
                "exit_price": str(exit_price),
            },
            strategy_version=STRATEGY_VERSION,
            model_version=STRATEGY_VERSION,
            config_hash="tactical",
            current_position_eur=notional if position.direction == Direction.LONG else -notional,
            target_position_eur=D("0"),
            execution_direction=exit_direction,
            reduce_only=True,
        )
        self.db.save_decision(decision)
        intent = self.intents.build(
            decision,
            position.leverage,
            "market",
            position.quantity,
            None,
            reduce_only=True,
            post_only=False,
        )
        result = self.authority.submit(
            intent,
            MarketSnapshot(instrument.symbol, last, bid, ask, D("0"), time.time()),
        )
        self.db.learning_event(
            "TACTICAL_ORDER",
            decision.decision_id,
            {"action": "EXIT", "reason": reason, "result": result},
        )
        self._last_reason = str(result.get("reason") or result.get("state") or "EXIT_SUBMITTED")
        if result.get("state") == OrderState.ACKNOWLEDGED.value:
            self.audit.emit(
                "TACTICAL_EXIT_SUBMITTED",
                "INFO",
                symbol=position.symbol,
                reason=reason,
                direction=exit_direction.value,
            )

    def _shadow_exit(self, position: TacticalPosition, reason: str, exit_price: D) -> None:
        rate = self.portfolio.quote_to_eur_rate(self.instrument_by_symbol[position.symbol].quote) or D("1")
        if position.direction == Direction.LONG:
            gross = (exit_price - position.entry_price) * position.quantity * rate
        else:
            gross = (position.entry_price - exit_price) * position.quantity * rate
        fees = position.entry_notional_eur * (
            D(str(self.config.tactical_entry_fee_bps)) + D(str(self.config.tactical_exit_fee_bps))
        ) / D("10000")
        net = gross - fees
        trade_id = new_id("tactical_trade")
        self.db.save_tactical_trade(
            trade_id=trade_id,
            position=position,
            exit_price=exit_price,
            gross_pnl_eur=gross,
            estimated_fees_eur=fees,
            net_pnl_eur=net,
            reason=reason,
            exited_at=time.time(),
        )
        self.db.delete_tactical_position(position.symbol)
        self.positions.pop(position.symbol, None)
        self.db.learning_event(
            "TACTICAL_SHADOW_EXIT",
            position.symbol,
            {
                "reason": reason,
                "exit_price": str(exit_price),
                "gross_pnl_eur": str(gross),
                "estimated_fees_eur": str(fees),
                "net_pnl_eur": str(net),
            },
        )
        self.audit.emit(
            "TACTICAL_SHADOW_EXIT",
            "INFO",
            symbol=position.symbol,
            reason=reason,
            exit_price=str(exit_price),
            gross_pnl_eur=str(gross),
            estimated_fees_eur=str(fees),
            net_pnl_eur=str(net),
        )
        self._last_reason = reason

    def _pending_tactical_orders(self) -> list[dict[str, Any]]:
        return self.db.query(
            """SELECT o.client_order_id,o.symbol,o.state,o.submitted_at,o.kraken_order_id,
                      o.quantity,o.direction,o.reduce_only,d.strategy_version,d.rationale_json
               FROM orders o JOIN decisions d ON d.decision_id=o.decision_id
               WHERE d.strategy_version=? AND o.state IN
                 ('SUBMITTING','ACKNOWLEDGED','LIVE','PARTIALLY_FILLED','UNKNOWN_RECONCILING')""",
            (STRATEGY_VERSION,),
        )

    def _reconcile_pending_orders(self) -> None:
        rows = self._pending_tactical_orders()
        for row in rows:
            instrument = self.instrument_by_symbol.get(str(row["symbol"]))
            if instrument is None:
                continue
            try:
                found = self.gateway.lookup_order(
                    client_order_id=str(row["client_order_id"]),
                    instrument=instrument,
                    kraken_order_id=str(row.get("kraken_order_id") or "") or None,
                )
                if not found:
                    continue
                payload = found[0]
                status = str(payload.get("status") or payload.get("state") or "").lower()
                if status not in {"closed", "filled", "canceled", "cancelled", "expired", "rejected"}:
                    continue
                self.authority.reconciler.state_from_exchange(payload)
                executed = D(str(payload.get("vol_exec") or payload.get("executedVolume") or payload.get("vol") or "0"))
                fill_price = D(str(payload.get("price") or payload.get("avg_price") or "0"))
                if fill_price <= 0:
                    fill_price = D(str(payload.get("descr", {}).get("price") or "0"))
                if executed <= 0 or fill_price <= 0:
                    if not row["reduce_only"]:
                        self._last_reason = "ENTRY_NOT_FILLED"
                    continue
                details = json.loads(row.get("rationale_json") or "{}")
                action = str(details.get("action") or "ENTRY")
                if action == "ENTRY":
                    notional = D(str(details.get("entry_notional_eur") or "0"))
                    position = TacticalPosition(
                        symbol=instrument.symbol,
                        direction=Direction(str(row["direction"]).upper()),
                        quantity=executed,
                        entry_price=fill_price,
                        entry_notional_eur=notional,
                        opened_at=time.time(),
                        peak_price=fill_price,
                        leverage=D(str(row.get("leverage") or "1")),
                        margin=bool(instrument.product_type.value != "SPOT"),
                        client_order_id=str(row["client_order_id"]),
                        last_price=fill_price,
                    )
                    self.positions[instrument.symbol] = position
                    self.db.save_tactical_position(position)
                    self.audit.emit("TACTICAL_ENTRY_FILLED", "INFO", symbol=instrument.symbol, direction=position.direction.value, quantity=str(executed), price=str(fill_price))
                else:
                    position = self.positions.get(instrument.symbol)
                    if position:
                        self._finalize_live_exit(position, fill_price, str(details.get("exit_reason") or "EXIT"))
            except Exception as exc:
                self.audit.emit(
                    "TACTICAL_ORDER_RECONCILIATION_FAILED",
                    "WARNING",
                    symbol=str(row.get("symbol", "")),
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                )

    def _finalize_live_exit(self, position: TacticalPosition, exit_price: D, reason: str) -> None:
        rate = self.portfolio.quote_to_eur_rate(self.instrument_by_symbol[position.symbol].quote) or D("1")
        gross = (
            (exit_price - position.entry_price) * position.quantity * rate
            if position.direction == Direction.LONG
            else (position.entry_price - exit_price) * position.quantity * rate
        )
        fees = position.entry_notional_eur * (
            D(str(self.config.tactical_entry_fee_bps)) + D(str(self.config.tactical_exit_fee_bps))
        ) / D("10000")
        net = gross - fees
        self.db.save_tactical_trade(
            trade_id=new_id("tactical_trade"),
            position=position,
            exit_price=exit_price,
            gross_pnl_eur=gross,
            estimated_fees_eur=fees,
            net_pnl_eur=net,
            reason=reason,
            exited_at=time.time(),
        )
        self.db.delete_tactical_position(position.symbol)
        self.positions.pop(position.symbol, None)
        self.audit.emit("TACTICAL_EXIT_FILLED", "INFO", symbol=position.symbol, reason=reason, net_pnl_eur=str(net))

    def _refresh_portfolio_if_due(self) -> None:
        if time.monotonic() - self._last_portfolio_refresh < 30:
            return
        self.portfolio.reconcile()
        self._last_portfolio_refresh = time.monotonic()

    def _fallback_ticker(self, symbol: str) -> dict[str, Any] | None:
        instrument = self.instrument_by_symbol.get(symbol)
        if instrument is None or instrument.venue != "spot":
            return None
        try:
            payload = self.gateway.spot_public("Ticker", {"pair": instrument.instrument_id})
            row = payload.get(instrument.instrument_id) or payload.get(instrument.altname)
            if not isinstance(row, dict):
                row = next((v for v in payload.values() if isinstance(v, dict)), None)
            if not isinstance(row, dict):
                return None
            value = lambda key, default="0": D(str(row.get(key, default)[0] if isinstance(row.get(key), list) else row.get(key, default)))
            return {"bid": value("b"), "ask": value("a"), "last": value("c")}
        except Exception:
            return None

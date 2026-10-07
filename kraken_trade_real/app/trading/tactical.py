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
CASH_QUOTES = {"EUR", "USD", "GBP", "CHF", "CAD", "JPY", "AUD", "NZD"}
TACTICAL_QUOTES = {"EUR", "USD", "USDC", "USDT", "GBP", "CHF"}


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

    The signal is deliberately cost-aware. A move that looks attractive before
    fees is not an entry unless the expected move clears the complete modeled
    round-trip cost plus a safety reserve.
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
        return eligible[-1] if eligible else None

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
        rows: list[tuple[float, D, D, str]] = []
        for trade in trades:
            try:
                ts = float(trade.get("timestamp_epoch", trade.get("ts", 0)))
                qty = D(str(trade.get("qty", "0")))
                price = D(str(trade.get("price", "0")))
            except (TypeError, ValueError, ArithmeticError):
                continue
            if ts > 0 and qty > 0 and price > 0:
                rows.append(
                    (ts, qty, price, str(trade.get("side", "")).lower())
                )
        recent = [
            (qty * price, side)
            for ts, qty, price, side in rows
            if ts >= now - 30
        ]
        baseline = [
            qty * price
            for ts, qty, price, _ in rows
            if now - 150 <= ts < now - 30
        ]
        if not recent or len(baseline) < 3:
            return D("0"), D("0")
        recent_value = sum((value for value, _ in recent), D("0"))
        baseline_value = sum(baseline, D("0")) / D("4")
        if baseline_value <= 0:
            return D("0"), D("0.5")
        ratio = recent_value / baseline_value
        buy_value = sum(
            (value for value, side in recent if side == "buy"),
            D("0"),
        )
        sell_value = sum(
            (value for value, side in recent if side == "sell"),
            D("0"),
        )
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
        bid_value = sum(
            (price * qty for price, qty in bid_rows if price > 0 and qty > 0),
            D("0"),
        )
        ask_value = sum(
            (price * qty for price, qty in ask_rows if price > 0 and qty > 0),
            D("0"),
        )
        total = bid_value + ask_value
        imbalance = (bid_value - ask_value) / total if total > 0 else D("0")
        return imbalance, total

    @staticmethod
    def _impact_bps(
        quantity: D,
        bids: Iterable[tuple[D, D]],
        asks: Iterable[tuple[D, D]],
    ) -> D:
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

        prior_60 = [
            price
            for ts, price in history
            if now - 60 <= ts < now and price > 0
        ]
        if len(prior_60) < 8:
            return None
        previous_high = max(prior_60)
        previous_low = min(prior_60)
        breakout_long = self._pct_bps(last, previous_high)
        breakout_short = self._pct_bps(last, previous_low)

        volume_ratio, buy_ratio = self._volume_ratio(trades, now)
        if volume_ratio < min_volume_ratio:
            return None

        bid_rows = list(bids)
        ask_rows = list(asks)
        if not bid_rows or not ask_rows:
            return None
        imbalance, depth_value = self._book_imbalance(bid_rows, ask_rows)

        if quote_to_eur_rate <= 0 or notional_eur <= 0:
            return None
        notional_quote = notional_eur / quote_to_eur_rate
        quantity = notional_quote / last

        impact = self._impact_bps(quantity, bid_rows, ask_rows)
        slippage = max(D("8"), volatility * D("0.75"))
        common_cost = (
            fee_entry_bps
            + fee_exit_bps
            + spread
            + slippage
            + impact
            + safety_buffer_bps
        )

        def score_for(
            direction: Direction,
            momentum_fast: D,
            momentum_slow: D,
            breakout: D,
            flow_ratio: D,
            order_imbalance: D,
        ) -> tuple[D, D, dict[str, Any]]:
            aligned_fast = max(
                D("0"),
                momentum_fast if direction == Direction.LONG else -momentum_fast,
            )
            aligned_slow = max(
                D("0"),
                momentum_slow if direction == Direction.LONG else -momentum_slow,
            )
            aligned_breakout = max(
                D("0"),
                breakout if direction == Direction.LONG else -breakout,
            )
            aligned_flow = (
                buy_ratio if direction == Direction.LONG else D("1") - buy_ratio
            )
            aligned_imbalance = (
                order_imbalance
                if direction == Direction.LONG
                else -order_imbalance
            )

            momentum_component = min(
                D("1"),
                aligned_fast
                / max(D("1"), min_momentum_30s_bps * D("2")),
            )
            trend_component = min(
                D("1"),
                aligned_slow
                / max(D("1"), min_momentum_3m_bps * D("2")),
            )
            breakout_component = min(
                D("1"),
                aligned_breakout
                / max(D("1"), min_breakout_bps * D("2")),
            )
            volume_component = min(
                D("1"),
                max(D("0"), flow_ratio - min_volume_ratio)
                / max(D("1"), min_volume_ratio),
            )
            flow_component = max(
                D("0"),
                min(D("1"), (aligned_flow - D("0.5")) * D("5")),
            )
            book_component = max(
                D("0"),
                min(D("1"), (aligned_imbalance - D("0.02")) / D("0.25")),
            )
            spread_component = max(
                D("0"),
                min(D("1"), D("1") - spread / max(D("1"), max_spread_bps)),
            )
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
            return score, net_edge, {
                "momentum_30s_bps": str(momentum_fast),
                "momentum_3m_bps": str(momentum_slow),
                "volatility_bps": str(volatility),
                "volume_ratio": str(flow_ratio),
                "breakout_bps": str(breakout),
                "book_imbalance": str(order_imbalance),
                "taker_buy_ratio": str(buy_ratio),
                "spread_bps": str(spread),
                "depth_quote": str(depth_value),
                "impact_bps": str(impact),
                "slippage_bps": str(slippage),
                "cost_bps": str(common_cost),
            }

        candidates: list[
            tuple[D, D, Direction, D, dict[str, Any]]
        ] = []
        if (
            momentum_30s >= min_momentum_30s_bps
            and momentum_3m >= min_momentum_3m_bps
            and breakout_long >= min_breakout_bps
            and buy_ratio >= D("0.55")
            and imbalance >= min_imbalance
        ):
            score, edge, rationale = score_for(
                Direction.LONG,
                momentum_30s,
                momentum_3m,
                breakout_long,
                volume_ratio,
                imbalance,
            )
            candidates.append((score, edge, Direction.LONG, breakout_long, rationale))

        if (
            momentum_30s <= -min_momentum_30s_bps
            and momentum_3m <= -min_momentum_3m_bps
            and breakout_short <= -min_breakout_bps
            and buy_ratio <= D("0.45")
            and imbalance <= -min_imbalance
        ):
            score, edge, rationale = score_for(
                Direction.SHORT,
                momentum_30s,
                momentum_3m,
                breakout_short,
                volume_ratio,
                imbalance,
            )
            candidates.append((score, edge, Direction.SHORT, breakout_short, rationale))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        score, edge, direction, breakout, rationale = candidates[0]
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
    """Fast tactical controller using the central TradingAuthority for writes."""

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
        self.price_history: dict[str, deque[tuple[float, D]]] = defaultdict(
            lambda: deque(maxlen=720)
        )
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._last_portfolio_refresh = 0.0
        self._last_scan_log = 0.0
        self._last_signal: TacticalSignal | None = None
        self._last_reason = "DISABLED"
        self._news_items: list[Any] = []
        self._gemini_effect_bps = D("0")
        self._gemini_status = "UNKNOWN"
        self._ai_context_at = 0.0
        self._portfolio_state: Any | None = None
        self._cash_balances: dict[str, D] = {}
        self._margin_account: dict[str, Any] | None = None
        self._fx_rates: dict[str, D] = {"EUR": D("1")}
        self._portfolio_context_at = 0.0
        self._last_learning_log_at = 0.0
        self._portfolio_lock = threading.RLock()
        self._load_from_db()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "tactical_enabled", False))

    def start(self) -> None:
        if not self.enabled or (self.thread and self.thread.is_alive()):
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run,
            name="kraken-tactical",
            daemon=True,
        )
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

    def set_portfolio_state(self, portfolio: Any) -> None:
        rates: dict[str, D] = {"EUR": D("1")}
        quotes = set(TACTICAL_QUOTES)
        for instrument in self.instruments:
            if instrument.symbol in self.stream.symbols() and instrument.venue == "spot":
                quotes.add(str(instrument.quote).upper())
        for quote in quotes:
            if str(quote).upper() == "EUR":
                continue
            try:
                rate = self.portfolio.quote_to_eur_rate(quote)
            except Exception:
                rate = None
            if rate is not None and rate > 0:
                rates[str(quote).upper()] = D(str(rate))
        with self._portfolio_lock:
            self._portfolio_state = portfolio
            self._cash_balances = dict(
                getattr(self.portfolio, "cash_balances", {}) or {}
            )
            account = getattr(self.portfolio, "spot_margin_account", None)
            self._margin_account = dict(account) if isinstance(account, dict) else None
            self._fx_rates = rates
            self._portfolio_context_at = time.time()

    def set_ai_context(
        self,
        news_items: list[Any],
        gemini_effect_bps: D,
        gemini_status: str,
    ) -> None:
        self._news_items = list(news_items)
        self._gemini_effect_bps = D(str(gemini_effect_bps))
        self._gemini_status = str(gemini_status)
        self._ai_context_at = time.time()

    def configure_stream(self, spot_payload: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        payload = spot_payload or {}
        ranked: list[tuple[float, Instrument]] = []
        with self._portfolio_lock:
            cash_balances = dict(self._cash_balances)
        cash_assets = {
            asset
            for asset in CASH_QUOTES
            if cash_balances.get(asset, D("0")) > 0
        }
        for instrument in self.instruments:
            if not self._eligible_stream_instrument(instrument):
                continue
            raw = self._find_ticker(instrument, payload)
            if not raw:
                continue
            bid = self._dec_ticker(raw, "b")
            ask = self._dec_ticker(raw, "a")
            last = self._dec_ticker(raw, "c")
            if min(bid, ask, last) <= 0:
                continue
            spread = (ask - bid) / ((ask + bid) / D("2")) * D("10000")
            if spread > D(str(self.config.tactical_max_spread_bps)):
                continue
            volume = self._dec_ticker(raw, "v", 1)
            turnover = max(D("0"), volume * last)
            change = abs(self._dec_ticker(raw, "p", 1))
            high = self._dec_ticker(raw, "h", 1)
            low = self._dec_ticker(raw, "l", 1)
            range_pct = (
                (high - low) / ((high + low) / D("2")) * D("100")
                if high > low > 0
                else D("0")
            )
            quote = instrument.quote.upper()
            quote_preference = (
                D("2.0") if quote == "EUR"
                else D("1.7") if quote in {"USD", "USDC", "USDT"}
                else D("1.2")
            )
            cash_preference = D("1.35") if quote in cash_assets else (
                D("1.0") if instrument.short_available else D("0.65")
            )
            short_preference = D("1.40") if instrument.short_available else D("1.0")
            score = (
                float((turnover + D("1")).ln()) * 4.0
                + float(range_pct) * 8.0
                + float(change) * 2.0
                + float(quote_preference)
                + float(cash_preference)
                + float(short_preference)
            )
            ranked.append((score, instrument))

        ranked.sort(key=lambda item: item[0], reverse=True)
        selected: list[Instrument] = [item[1] for item in ranked[: int(self.config.tactical_universe_size)]]

        selected_symbols = {item.symbol for item in selected}
        for position in self.positions.values():
            held_instrument = self.instrument_by_symbol.get(position.symbol)
            if (
                held_instrument is not None
                and held_instrument.symbol not in selected_symbols
            ):
                selected.append(held_instrument)

        self.stream.set_symbols([i.symbol for i in selected])
        self.audit.emit(
            "TACTICAL_STREAM_CONFIGURED",
            "INFO",
            subscribed=len(selected),
            short_eligible=sum(1 for i in selected if i.short_available),
            families={
                "spot": sum(1 for i in selected if i.product_type.value == "SPOT"),
                "spot_margin": sum(
                    1 for i in selected if i.product_type.value == "SPOT_MARGIN"
                ),
            },
        )

    @staticmethod
    def _eligible_stream_instrument(instrument: Instrument) -> bool:
        if instrument.venue != "spot" or not instrument.tradeable:
            return False
        if instrument.product_type.value not in {"SPOT", "SPOT_MARGIN"}:
            return False
        if str(instrument.quote).upper() not in TACTICAL_QUOTES:
            return False
        base = str(instrument.base or "").upper()
        if base.endswith("X") and len(base) >= 4:
            return False
        blob = json.dumps(
            instrument.metadata or {},
            sort_keys=True,
            default=str,
        ).lower()
        if any(
            token in blob
            for token in (
                "xstock",
                "tokenized stock",
                "tokenized equity",
                "tokenised stock",
                "tokenised equity",
            )
        ):
            return False
        return True

    @staticmethod
    def _find_ticker(
        instrument: Instrument,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        for key in (
            instrument.instrument_id,
            instrument.altname,
            instrument.symbol,
            instrument.symbol.replace("/", ""),
        ):
            raw = payload.get(key)
            if isinstance(raw, dict):
                return raw
        wanted = instrument.symbol.replace("/", "").upper()
        for key, value in payload.items():
            if (
                str(key).replace("/", "").upper() == wanted
                and isinstance(value, dict)
            ):
                return value
        return None

    @staticmethod
    def _dec_ticker(
        raw: dict[str, Any],
        key: str,
        index: int = 0,
    ) -> D:
        value = raw.get(key, "0")
        if isinstance(value, list):
            value = value[index] if len(value) > index else "0"
        try:
            return D(str(value or "0"))
        except (TypeError, ValueError):
            return D("0")

    def stats(self) -> dict[str, Any]:
        open_position = next(iter(self.positions.values()), None)
        today = datetime.now(timezone.utc).date().isoformat()
        count = self.db.one(
            "SELECT COUNT(*) AS n FROM tactical_trades "
            "WHERE date(datetime(exit_time,'unixepoch'))=?",
            (today,),
        )
        pnl = self.db.one(
            "SELECT COALESCE(SUM(CAST(net_pnl_eur AS REAL)),0) AS pnl "
            "FROM tactical_trades WHERE date(datetime(exit_time,'unixepoch'))=?",
            (today,),
        )
        trades_today = int(count["n"]) if count else 0
        pnl_today = D(str(pnl["pnl"] if pnl else "0"))
        return {
            "enabled": self.enabled,
            "mode": "SHADOW" if getattr(self.config, "tactical_shadow_mode", True) else "LIVE",
            "status": (
                "POSITION_OPEN"
                if open_position
                else ("RUNNING" if self.enabled else "DISABLED")
            ),
            "symbol": open_position.symbol if open_position else "",
            "direction": open_position.direction.value if open_position else "",
            "position_eur": str(
                open_position.entry_notional_eur if open_position else D("0")
            ),
            "score": str(
                self._last_signal.score if self._last_signal else D("0")
            ),
            "net_edge_bps": str(
                self._last_signal.net_edge_bps if self._last_signal else D("0")
            ),
            "trades_today": trades_today,
            "pnl_today_eur": str(pnl_today),
            "last_reason": self._last_reason,
            "stream_symbols": len(self.stream.symbols()),
            "short_capable_symbols": sum(
                1 for i in self.instruments
                if i.symbol in set(self.stream.symbols()) and i.short_available
            ),
            "gemini_status": self._gemini_status,
        }

    def _load_from_db(self) -> None:
        for row in self.db.tactical_positions():
            try:
                direction = Direction(str(row["direction"]).upper())
                self.positions[row["symbol"]] = TacticalPosition(
                    symbol=str(row["symbol"]),
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
            except (KeyError, TypeError, ValueError, ArithmeticError):
                self.audit.emit(
                    "TACTICAL_POSITION_LOAD_FAILED",
                    "WARNING",
                    symbol=str(row.get("symbol", "")),
                )

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
            self.stop_event.wait(
                max(
                    0.25,
                    float(self.config.tactical_poll_interval_seconds) - elapsed,
                )
            )

    def _poll(self) -> None:
        now = time.time()
        raw_snapshots = self.stream.snapshots()
        if not raw_snapshots:
            self._last_reason = "STREAM_EMPTY"
            return

        for symbol, position in list(self.positions.items()):
            self._manage_position(position, raw_snapshots.get(symbol), now)

        pending = self._pending_tactical_orders()
        if pending or self.positions:
            self._record_prices(raw_snapshots, now)
            if pending:
                self._last_reason = "ORDER_PENDING"
            return

        best: TacticalSignal | None = None
        blocked: dict[str, int] = {}
        for symbol, snap in raw_snapshots.items():
            instrument = self.instrument_by_symbol.get(symbol)
            if instrument is None:
                continue
            timestamp = self._dec_raw(snap.get("timestamp"))
            max_age = int(self.config.tactical_data_max_age_seconds)
            if timestamp <= 0 or now - float(timestamp) > max_age:
                blocked["STALE_MARKET_DATA"] = blocked.get("STALE_MARKET_DATA", 0) + 1
                continue
            if not bool(snap.get("book_ready", True)):
                blocked["BOOK_NOT_READY"] = blocked.get("BOOK_NOT_READY", 0) + 1
                continue
            signal = self._evaluate(instrument, snap, now)
            if signal is None:
                continue
            veto = self._ai_veto(instrument, signal, now)
            if veto:
                blocked[veto] = blocked.get(veto, 0) + 1
                continue
            if best is None or (
                signal.score,
                signal.net_edge_bps,
            ) > (
                best.score,
                best.net_edge_bps,
            ):
                best = signal

        self._record_prices(raw_snapshots, now)

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
            self._enter(
                best,
                self.instrument_by_symbol[best.symbol],
                raw_snapshots[best.symbol],
            )
        elif now - self._last_scan_log >= 30:
            self._last_scan_log = now
            self.audit.emit(
                "TACTICAL_SCAN",
                "INFO",
                subscribed=len(raw_snapshots),
                positions=len(self.positions),
                reason="NO_QUALIFIED_SIGNAL",
                filters=blocked,
            )

    def _record_prices(
        self,
        raw_snapshots: dict[str, dict[str, Any]],
        now: float,
    ) -> None:
        for symbol, snap in raw_snapshots.items():
            last = self._dec_raw(snap.get("last"))
            if last > 0:
                self.price_history[symbol].append((now, last))

    def _evaluate(
        self,
        instrument: Instrument,
        raw: dict[str, Any],
        now: float,
    ) -> TacticalSignal | None:
        bid = self._dec_raw(raw.get("bid"))
        ask = self._dec_raw(raw.get("ask"))
        last = self._dec_raw(raw.get("last"))
        with self._portfolio_lock:
            rate = self._fx_rates.get(str(instrument.quote).upper())
        notional = self._target_notional()
        if rate is None or rate <= 0 or notional <= 0:
            return None
        return self.strategy.evaluate(
            symbol=instrument.symbol,
            bid=bid,
            ask=ask,
            last=last,
            history=self.price_history[instrument.symbol],
            trades=raw.get("trades", []),
            bids=self._book_rows(raw.get("bids", [])),
            asks=self._book_rows(raw.get("asks", [])),
            now=now,
            notional_eur=notional,
            quote_to_eur_rate=rate,
            min_net_edge_bps=self._effective_min_edge(),
            max_spread_bps=D(str(self.config.tactical_max_spread_bps)),
            min_volume_ratio=D(str(self.config.tactical_min_volume_ratio)),
            min_momentum_30s_bps=D(
                str(self.config.tactical_min_momentum_30s_bps)
            ),
            min_momentum_3m_bps=D(
                str(self.config.tactical_min_momentum_3m_bps)
            ),
            min_volatility_bps=D(
                str(self.config.tactical_min_volatility_bps)
            ),
            min_breakout_bps=D(
                str(self.config.tactical_min_breakout_bps)
            ),
            min_imbalance=D(str(self.config.tactical_min_imbalance)),
            fee_entry_bps=D(str(self.config.tactical_entry_fee_bps)),
            fee_exit_bps=D(str(self.config.tactical_exit_fee_bps)),
            safety_buffer_bps=D(
                str(self.config.tactical_safety_buffer_bps)
            ),
        )

    def _ai_veto(
        self,
        instrument: Instrument,
        signal: TacticalSignal,
        now: float,
    ) -> str:
        max_age = max(
            60,
            int(getattr(self.config, "news_refresh_minutes", 10)) * 180,
        )
        if self._ai_context_at <= 0 or now - self._ai_context_at > max_age:
            return ""
        try:
            news_effect = self.news.effect_for(
                instrument.symbol,
                self._news_items,
            )
        except Exception:
            news_effect = D("0")
        veto = D(str(self.config.tactical_ai_veto_bps))
        if signal.direction == Direction.LONG and news_effect <= -veto:
            return "NEWS_CONTRARY"
        if signal.direction == Direction.SHORT and news_effect >= veto:
            return "NEWS_CONTRARY"
        if signal.direction == Direction.LONG and self._gemini_effect_bps <= -veto:
            return "GEMINI_CONTRARY"
        if signal.direction == Direction.SHORT and self._gemini_effect_bps >= veto:
            return "GEMINI_CONTRARY"
        return ""

    def _target_notional(self) -> D:
        with self._portfolio_lock:
            portfolio = self._portfolio_state
        if portfolio is None or portfolio.equity_eur <= 0:
            return D("0")
        return min(
            D(str(self.config.tactical_max_capital_eur)),
            portfolio.equity_eur * D(str(self.config.tactical_portfolio_pct)) / D("100"),
        )

    def _effective_min_edge(self) -> D:
        base = D(str(self.config.tactical_min_net_edge_bps))
        rows = self.db.query(
            "SELECT net_pnl_eur FROM tactical_trades "
            "ORDER BY exit_time DESC LIMIT 20"
        )
        if len(rows) < 5:
            return base
        pnl = [
            D(str(row.get("net_pnl_eur") or "0"))
            for row in rows
        ]
        wins = sum(1 for value in pnl if value > 0)
        win_rate = D(wins) / D(len(pnl))
        total = sum(pnl, D("0"))
        penalty = D("0")
        if win_rate < D("0.40") or total < 0:
            penalty = D("40")
        effective = base + penalty
        if penalty > 0 and time.time() - self._last_learning_log_at >= 300:
            self._last_learning_log_at = time.time()
            self.audit.emit(
                "TACTICAL_LEARNING_TIGHTENED",
                "INFO",
                samples=len(pnl),
                win_rate=str(win_rate),
                net_pnl_eur=str(total),
                min_edge_bps=str(effective),
            )
        return effective

    def _trade_limits(self) -> tuple[bool, str]:
        now = time.time()
        hour_cutoff = now - 3600
        day = datetime.now(timezone.utc).date().isoformat()
        hour = self.db.one(
            "SELECT COUNT(*) AS n FROM tactical_trades WHERE exit_time>=?",
            (hour_cutoff,),
        )
        daily = self.db.one(
            "SELECT COUNT(*) AS n FROM tactical_trades "
            "WHERE date(datetime(exit_time,'unixepoch'))=?",
            (day,),
        )
        if hour and int(hour["n"]) >= int(self.config.tactical_max_round_trips_per_hour):
            return False, "TACTICAL_HOURLY_LIMIT"
        if daily and int(daily["n"]) >= int(self.config.tactical_max_round_trips_per_day):
            return False, "TACTICAL_DAILY_LIMIT"
        last = self.db.one(
            "SELECT exit_time FROM tactical_trades "
            "WHERE exit_time IS NOT NULL ORDER BY exit_time DESC LIMIT 1"
        )
        if last:
            age = now - float(last["exit_time"])
            if age < int(self.config.tactical_reentry_cooldown_seconds):
                return False, "TACTICAL_REENTRY_COOLDOWN"
        return True, "TACTICAL_RATE_LIMIT_OK"

    def _risk_ok(
        self,
        instrument: Instrument,
        signal: TacticalSignal,
        notional: D,
        leverage: D,
    ) -> tuple[bool, str]:
        with self._portfolio_lock:
            portfolio = self._portfolio_state
        if portfolio is None or portfolio.equity_eur <= 0:
            return False, "PORTFOLIO_UNAVAILABLE"
        equity = portfolio.equity_eur
        max_sleeve = min(
            equity * D(str(self.config.tactical_portfolio_pct)) / D("100"),
            D(str(self.config.tactical_max_capital_eur)),
        )
        if notional > max_sleeve or notional <= 0:
            return False, "TACTICAL_CAP_EXCEEDED"
        if len(self.positions) >= int(self.config.tactical_max_positions):
            return False, "TACTICAL_POSITION_LIMIT"
        allowed_gross = equity * D(str(self.config.risk_max_gross_pct)) / D("100")
        allowed_net = equity * D(str(self.config.risk_max_net_pct)) / D("100")
        projected_gross = portfolio.gross_eur + notional
        projected_net = abs(
            portfolio.net_eur
            + (notional if signal.direction == Direction.LONG else -notional)
        )
        if projected_gross > allowed_gross:
            return False, "GLOBAL_GROSS_LIMIT"
        if projected_net > allowed_net:
            return False, "GLOBAL_NET_LIMIT"
        if portfolio.daily_pnl_eur < -(
            equity * D(str(self.config.risk_daily_loss_pct)) / D("100")
        ):
            return False, "DAILY_LOSS_LIMIT"
        if portfolio.drawdown_pct > D(str(self.config.risk_max_drawdown_pct)):
            return False, "DRAWDOWN_LIMIT"
        if leverage > D(str(self.config.tactical_max_leverage)):
            return False, "TACTICAL_LEVERAGE_LIMIT"
        if leverage > instrument.max_leverage:
            return False, "INSTRUMENT_LEVERAGE_LIMIT"
        if leverage > D("1"):
            allowed_margin = equity * D(str(self.config.risk_max_margin_pct)) / D("100")
            projected_margin = portfolio.margin_used_eur + notional / leverage
            if projected_margin > allowed_margin:
                return False, "GLOBAL_MARGIN_LIMIT"

        if signal.direction == Direction.SHORT:
            if not instrument.short_available:
                return False, "SHORT_NOT_AVAILABLE"
            if instrument.product_type.value == "SPOT_MARGIN":
                if leverage < D("2") or not instrument.margin_available:
                    return False, "SPOT_MARGIN_SHORT_REQUIRES_LEVERAGE"
                account = self.portfolio.spot_margin_account
                if not isinstance(account, dict):
                    return False, "MARGIN_ACCOUNT_UNAVAILABLE"
                free = D(str(account.get("free_margin") or "0"))
                if free < notional / leverage:
                    return False, "MARGIN_FREE_INSUFFICIENT"
        else:
            with self._portfolio_lock:
                rate = self._fx_rates.get(str(instrument.quote).upper())
            if rate is None or rate <= 0:
                return False, "FX_RATE_UNAVAILABLE"
            required_quote = notional / rate
            with self._portfolio_lock:
                available = self._cash_balances.get(
                    str(instrument.quote).upper(),
                    D("0"),
                )
            if available + D("0.00000001") < required_quote:
                return False, "QUOTE_CASH_INSUFFICIENT"

        return True, "TACTICAL_RISK_OK"

    def _choose_leverage(
        self,
        instrument: Instrument,
        direction: Direction,
    ) -> D | None:
        if direction == Direction.LONG:
            return D("1")
        if not instrument.short_available:
            return None
        if instrument.product_type.value != "SPOT_MARGIN":
            return D("1")
        maximum = min(
            D(str(self.config.tactical_max_leverage)),
            D(str(self.config.tactical_short_leverage)),
            instrument.max_leverage,
        )
        supported = sorted(
            level
            for level in instrument.leverage_levels
            if D("2") <= level <= maximum
        )
        return supported[-1] if supported else None

    @staticmethod
    def _book_rows(value: Any) -> list[tuple[D, D]]:
        rows: list[tuple[D, D]] = []
        if not isinstance(value, list):
            return rows
        for row in value:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                price = D(str(row[0]))
                qty = D(str(row[1]))
            except (TypeError, ValueError):
                continue
            if price > 0 and qty > 0:
                rows.append((price, qty))
        return rows

    @staticmethod
    def _dec_raw(value: Any) -> D:
        try:
            return D(str(value or "0"))
        except (TypeError, ValueError):
            return D("0")

    @staticmethod
    def _snapshot_for_authority(
        instrument: Instrument,
        raw: dict[str, Any],
        history: deque[tuple[float, D]],
    ) -> MarketSnapshot:
        closes = tuple(price for _, price in history if price > 0)[-60:]
        return MarketSnapshot(
            instrument.symbol,
            TacticalEngine._dec_raw(raw.get("last")),
            TacticalEngine._dec_raw(raw.get("bid")),
            TacticalEngine._dec_raw(raw.get("ask")),
            TacticalEngine._dec_raw(raw.get("volume_24h")),
            float(raw.get("timestamp", time.time())),
            closes=closes,
            depths_bid=tuple(TacticalEngine._book_rows(raw.get("bids", []))),
            depths_ask=tuple(TacticalEngine._book_rows(raw.get("asks", []))),
        )

    def _enter(
        self,
        signal: TacticalSignal,
        instrument: Instrument,
        raw: dict[str, Any],
    ) -> None:
        allowed, limit_reason = self._trade_limits()
        if not allowed:
            self._last_reason = limit_reason
            return
        notional = self._target_notional()
        leverage = self._choose_leverage(instrument, signal.direction)
        if leverage is None:
            self._last_reason = "SHORT_NOT_AVAILABLE_OR_NO_LEVERAGE"
            self.audit.emit(
                "TACTICAL_ORDER_BLOCKED",
                "INFO",
                symbol=instrument.symbol,
                direction=signal.direction.value,
                reason=self._last_reason,
            )
            return
        ok, reason = self._risk_ok(
            instrument,
            signal,
            notional,
            leverage,
        )
        if not ok:
            self._last_reason = reason
            self.audit.emit(
                "TACTICAL_ORDER_BLOCKED",
                "INFO",
                symbol=instrument.symbol,
                direction=signal.direction.value,
                reason=reason,
            )
            return

        with self._portfolio_lock:
            rate = self._fx_rates.get(str(instrument.quote).upper())
        if rate is None or rate <= 0:
            self._last_reason = "FX_RATE_UNAVAILABLE"
            return
        price = (
            self._dec_raw(raw.get("ask"))
            if signal.direction == Direction.LONG
            else self._dec_raw(raw.get("bid"))
        )
        quantity = (notional / rate) / price
        if quantity is None or quantity <= 0:
            self._last_reason = "QUANTITY_UNAVAILABLE"
            return
        if quantity < instrument.min_order_qty:
            self._last_reason = "MIN_ORDER_QTY"
            return
        if (
            instrument.venue != "futures"
            and instrument.min_cost > 0
            and quantity * price < instrument.min_cost
        ):
            self._last_reason = "MIN_ORDER_COST"
            return

        if self.config.tactical_shadow_mode:
            self._shadow_entry(
                signal,
                instrument,
                quantity,
                price,
                notional,
                leverage,
            )
            return

        features = {
            "tactical_score": signal.score,
            "tactical_momentum_30s_bps": signal.momentum_30s_bps,
            "tactical_momentum_3m_bps": signal.momentum_3m_bps,
            "tactical_volatility_bps": signal.volatility_bps,
            "tactical_volume_ratio": signal.volume_ratio,
            "tactical_breakout_bps": signal.breakout_bps,
            "tactical_book_imbalance": signal.book_imbalance,
            "tactical_net_edge_bps": signal.net_edge_bps,
        }
        signal_model = Signal(
            instrument.symbol,
            signal.direction,
            signal.expected_move_bps,
            signal.estimated_cost_bps,
            signal.confidence,
            "TACTICAL_VOLATILITY",
            D("0"),
            self._gemini_effect_bps,
            features,
        )
        target = (
            notional
            if signal.direction == Direction.LONG
            else -notional
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
            target_position_eur=target,
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
            max_slippage_bps=D(str(self.config.tactical_max_slippage_bps)),
        )
        market = self._snapshot_for_authority(
            instrument,
            raw,
            self.price_history[instrument.symbol],
        )
        result = self.authority.submit(intent, market)
        self.db.learning_event(
            "TACTICAL_ORDER",
            decision.decision_id,
            {
                "action": "ENTRY",
                "result": result,
                "signal": signal.rationale,
            },
        )
        self._last_reason = str(
            result.get("reason")
            or result.get("state")
            or "ENTRY_SUBMITTED"
        )
        self.audit.emit(
            "TACTICAL_ENTRY_RESULT",
            "INFO" if result.get("state") == OrderState.ACKNOWLEDGED.value else "WARNING",
            symbol=instrument.symbol,
            direction=signal.direction.value,
            result_state=str(result.get("state", "")),
            reason=str(result.get("reason", "")),
        )

    def _shadow_entry(
        self,
        signal: TacticalSignal,
        instrument: Instrument,
        quantity: D,
        price: D,
        notional: D,
        leverage: D,
    ) -> None:
        position = TacticalPosition(
            symbol=instrument.symbol,
            direction=signal.direction,
            quantity=quantity,
            entry_price=price,
            entry_notional_eur=notional,
            opened_at=time.time(),
            peak_price=price,
            leverage=leverage,
            margin=instrument.product_type.value == "SPOT_MARGIN",
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

    def _manage_position(
        self,
        position: TacticalPosition,
        raw: dict[str, Any] | None,
        now: float,
    ) -> None:
        if raw is None:
            raw = self._fallback_ticker(position.symbol)
        else:
            timestamp = self._dec_raw(raw.get("timestamp"))
            if timestamp <= 0 or now - float(timestamp) > int(self.config.tactical_data_max_age_seconds):
                raw = self._fallback_ticker(position.symbol)
        if not raw:
            self._last_reason = "POSITION_MARKET_DATA_UNAVAILABLE"
            return
        last = self._dec_raw(raw.get("last"))
        bid = self._dec_raw(raw.get("bid")) or last
        ask = self._dec_raw(raw.get("ask")) or last
        if last <= 0:
            return
        position.last_price = last

        if position.direction == Direction.LONG:
            position.peak_price = max(position.peak_price, last)
            stop_trigger = last <= position.entry_price * (
                D("1") - D(str(self.config.tactical_stop_loss_pct)) / D("100")
            )
            target_trigger = last >= position.entry_price * (
                D("1") + D(str(self.config.tactical_take_profit_pct)) / D("100")
            )
            trailing_trigger = (
                position.peak_price > position.entry_price
                and last <= position.peak_price * (
                    D("1")
                    - D(str(self.config.tactical_trailing_stop_pct)) / D("100")
                )
            )
        else:
            position.peak_price = min(
                position.peak_price if position.peak_price > 0 else position.entry_price,
                last,
            )
            stop_trigger = last >= position.entry_price * (
                D("1") + D(str(self.config.tactical_stop_loss_pct)) / D("100")
            )
            target_trigger = last <= position.entry_price * (
                D("1") - D(str(self.config.tactical_take_profit_pct)) / D("100")
            )
            trailing_trigger = (
                position.peak_price < position.entry_price
                and last >= position.peak_price * (
                    D("1")
                    + D(str(self.config.tactical_trailing_stop_pct)) / D("100")
                )
            )

        time_trigger = now >= (
            position.opened_at + int(self.config.tactical_max_hold_seconds)
        )
        reason = (
            "STOP_LOSS" if stop_trigger
            else "TAKE_PROFIT" if target_trigger
            else "TRAILING_STOP" if trailing_trigger
            else "TIME_STOP" if time_trigger
            else ""
        )
        if reason:
            self._exit(
                position,
                reason,
                bid,
                ask,
                last,
            )
        else:
            self.db.save_tactical_position(position)

    def _exit(
        self,
        position: TacticalPosition,
        reason: str,
        bid: D,
        ask: D,
        last: D,
    ) -> None:
        instrument = self.instrument_by_symbol.get(position.symbol)
        if instrument is None:
            return
        if self.config.tactical_shadow_mode:
            exit_price = bid if position.direction == Direction.LONG else ask
            self._shadow_exit(position, reason, exit_price)
            return

        exit_direction = (
            Direction.SHORT
            if position.direction == Direction.LONG
            else Direction.LONG
        )
        exit_price = bid if position.direction == Direction.LONG else ask
        signal_model = Signal(
            instrument.symbol,
            exit_direction,
            D("0"),
            D("0"),
            D("1"),
            "TACTICAL_EXIT",
            D("0"),
            self._gemini_effect_bps,
            {"tactical_exit_reason": D("1")},
        )
        current = (
            position.entry_notional_eur
            if position.direction == Direction.LONG
            else -position.entry_notional_eur
        )
        decision = Decision(
            decision_id=new_id("decision"),
            instrument=instrument,
            signal=signal_model,
            target_notional_eur=position.entry_notional_eur,
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
            current_position_eur=current,
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
            max_slippage_bps=D(str(self.config.tactical_max_slippage_bps)),
        )
        raw = {
            "last": str(last),
            "bid": str(bid),
            "ask": str(ask),
            "timestamp": time.time(),
            "volume_24h": "0",
            "bids": [],
            "asks": [],
        }
        market = self._snapshot_for_authority(
            instrument,
            raw,
            self.price_history[instrument.symbol],
        )
        result = self.authority.submit(intent, market)
        self.db.learning_event(
            "TACTICAL_ORDER",
            decision.decision_id,
            {
                "action": "EXIT",
                "reason": reason,
                "result": result,
            },
        )
        self._last_reason = str(
            result.get("reason")
            or result.get("state")
            or "EXIT_SUBMITTED"
        )
        self.audit.emit(
            "TACTICAL_EXIT_RESULT",
            "INFO" if result.get("state") == OrderState.ACKNOWLEDGED.value else "WARNING",
            symbol=position.symbol,
            reason=reason,
            result_state=str(result.get("state", "")),
        )

    def _shadow_exit(
        self,
        position: TacticalPosition,
        reason: str,
        exit_price: D,
    ) -> None:
        instrument = self.instrument_by_symbol[position.symbol]
        with self._portfolio_lock:
            rate = self._fx_rates.get(
                str(instrument.quote).upper(),
                D("1"),
            )
        if rate <= 0:
            return
        gross = (
            (exit_price - position.entry_price) * position.quantity * rate
            if position.direction == Direction.LONG
            else (position.entry_price - exit_price) * position.quantity * rate
        )
        fees = position.entry_notional_eur * (
            D(str(self.config.tactical_entry_fee_bps))
            + D(str(self.config.tactical_exit_fee_bps))
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
                      o.quantity,o.direction,o.reduce_only,o.leverage,d.rationale_json
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
                resolved_state = self.authority.reconciler.state_from_exchange(
                    payload
                )
                self.db.update_order_state(
                    str(row["client_order_id"]),
                    resolved_state.value,
                    kraken_order_id=str(
                        payload.get("order_id")
                        or payload.get("txid")
                        or payload.get("id")
                        or row.get("kraken_order_id")
                        or ""
                    ) or None,
                )
                if resolved_state not in {
                    OrderState.FILLED,
                    OrderState.PARTIALLY_FILLED,
                    OrderState.CANCELED,
                    OrderState.EXPIRED,
                    OrderState.REJECTED,
                }:
                    continue

                executed = D(
                    str(
                        payload.get("vol_exec")
                        or payload.get("executedVolume")
                        or payload.get("vol")
                        or "0"
                    )
                )
                fill_price = D(
                    str(
                        payload.get("price")
                        or payload.get("avg_price")
                        or payload.get("averagePrice")
                        or "0"
                    )
                )
                if fill_price <= 0:
                    descr = payload.get("descr")
                    if isinstance(descr, dict):
                        fill_price = D(str(descr.get("price") or "0"))
                if (
                    resolved_state in {OrderState.FILLED, OrderState.PARTIALLY_FILLED}
                    and executed > 0
                    and fill_price > 0
                ):
                    details = json.loads(row.get("rationale_json") or "{}")
                    action = str(details.get("action") or "ENTRY")
                    exchange_status = str(
                        payload.get("status") or payload.get("state") or ""
                    ).lower()
                    closed_partial = (
                        resolved_state == OrderState.PARTIALLY_FILLED
                        and exchange_status in {"closed", "filled"}
                    )
                    if closed_partial:
                        # Kraken confirms that the order is closed, so there
                        # is no remaining exchange-side order to reconcile.
                        self.db.update_order_state(
                            str(row["client_order_id"]),
                            OrderState.FILLED.value,
                            kraken_order_id=str(
                                payload.get("order_id")
                                or payload.get("txid")
                                or payload.get("id")
                                or row.get("kraken_order_id")
                                or ""
                            ) or None,
                            last_error="EXCHANGE_CLOSED_PARTIAL_EXECUTION",
                        )
                    if action == "ENTRY":
                        self._register_live_entry(
                            row,
                            instrument,
                            executed,
                            fill_price,
                        )
                    elif action == "EXIT":
                        position = self.positions.get(instrument.symbol)
                        if position:
                            self._finalize_live_exit(
                                position,
                                fill_price,
                                str(details.get("exit_reason") or "EXIT"),
                                executed,
                            )
                elif resolved_state == OrderState.REJECTED:
                    self._last_reason = "ENTRY_REJECTED"
            except Exception as exc:
                self.audit.emit(
                    "TACTICAL_ORDER_RECONCILIATION_FAILED",
                    "WARNING",
                    symbol=str(row.get("symbol", "")),
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                )

    def _register_live_entry(
        self,
        row: dict[str, Any],
        instrument: Instrument,
        executed: D,
        fill_price: D,
    ) -> None:
        details = json.loads(row.get("rationale_json") or "{}")
        requested_notional = D(str(details.get("entry_notional_eur") or "0"))
        with self._portfolio_lock:
            rate = self._fx_rates.get(
                str(instrument.quote).upper(),
                D("1"),
            )
        actual_notional = executed * fill_price * rate
        notional = actual_notional if actual_notional > 0 else requested_notional
        direction = Direction(str(row["direction"]).upper())
        position = TacticalPosition(
            symbol=instrument.symbol,
            direction=direction,
            quantity=executed,
            entry_price=fill_price,
            entry_notional_eur=notional,
            opened_at=time.time(),
            peak_price=fill_price,
            leverage=D(str(row.get("leverage") or "1")),
            margin=instrument.product_type.value == "SPOT_MARGIN",
            client_order_id=str(row["client_order_id"]),
            last_price=fill_price,
        )
        self.positions[instrument.symbol] = position
        self.db.save_tactical_position(position)
        self.audit.emit(
            "TACTICAL_ENTRY_FILLED",
            "INFO",
            symbol=instrument.symbol,
            direction=position.direction.value,
            quantity=str(executed),
            price=str(fill_price),
            notional_eur=str(notional),
        )

    def _finalize_live_exit(
        self,
        position: TacticalPosition,
        exit_price: D,
        reason: str,
        executed_quantity: D | None = None,
    ) -> None:
        instrument = self.instrument_by_symbol[position.symbol]
        quantity = (
            min(position.quantity, executed_quantity)
            if executed_quantity is not None and executed_quantity > 0
            else position.quantity
        )
        if quantity <= 0:
            return
        with self._portfolio_lock:
            rate = self._fx_rates.get(
                str(instrument.quote).upper(),
                D("1"),
            )
        gross = (
            (exit_price - position.entry_price) * quantity * rate
            if position.direction == Direction.LONG
            else (position.entry_price - exit_price) * quantity * rate
        )
        entry_notional = (
            position.entry_notional_eur * quantity / position.quantity
        )
        fees = entry_notional * (
            D(str(self.config.tactical_entry_fee_bps))
            + D(str(self.config.tactical_exit_fee_bps))
        ) / D("10000")
        net = gross - fees
        from dataclasses import replace
        trade_position = replace(
            position,
            quantity=quantity,
            entry_notional_eur=entry_notional,
        )
        self.db.save_tactical_trade(
            trade_id=new_id("tactical_trade"),
            position=trade_position,
            exit_price=exit_price,
            gross_pnl_eur=gross,
            estimated_fees_eur=fees,
            net_pnl_eur=net,
            reason=reason,
            exited_at=time.time(),
        )
        remaining = position.quantity - quantity
        if remaining > 0:
            position.quantity = remaining
            position.entry_notional_eur = position.entry_notional_eur - entry_notional
            position.last_price = exit_price
            self.db.save_tactical_position(position)
            self.audit.emit(
                "TACTICAL_EXIT_PARTIAL",
                "WARNING",
                symbol=position.symbol,
                reason=reason,
                executed_quantity=str(quantity),
                remaining_quantity=str(remaining),
                net_pnl_eur=str(net),
            )
            return
        self.db.delete_tactical_position(position.symbol)
        self.positions.pop(position.symbol, None)
        self.audit.emit(
            "TACTICAL_EXIT_FILLED",
            "INFO",
            symbol=position.symbol,
            reason=reason,
            net_pnl_eur=str(net),
        )

    def _refresh_portfolio_if_due(self) -> None:
        interval = max(
            15,
            int(getattr(self.config, "tactical_portfolio_refresh_seconds", 30)),
        )
        if time.monotonic() - self._last_portfolio_refresh < interval:
            return
        portfolio = self.portfolio.reconcile()
        self.set_portfolio_state(portfolio)
        self._last_portfolio_refresh = time.monotonic()
        self.audit.emit(
            "TACTICAL_PORTFOLIO_REFRESHED",
            "INFO",
            equity_eur=str(portfolio.equity_eur),
            cash_eur=str(portfolio.cash_eur),
            gross_eur=str(portfolio.gross_eur),
            net_eur=str(portfolio.net_eur),
        )

    def _fallback_ticker(self, symbol: str) -> dict[str, Any] | None:
        instrument = self.instrument_by_symbol.get(symbol)
        if instrument is None or instrument.venue != "spot":
            return None
        try:
            payload = self.gateway.spot_public(
                "Ticker",
                {"pair": instrument.instrument_id},
            )
            row = (
                payload.get(instrument.instrument_id)
                or payload.get(instrument.altname)
            )
            if not isinstance(row, dict):
                row = next(
                    (value for value in payload.values() if isinstance(value, dict)),
                    None,
                )
            if not isinstance(row, dict):
                return None
            return {
                "bid": self._dec_ticker(row, "b"),
                "ask": self._dec_ticker(row, "a"),
                "last": self._dec_ticker(row, "c"),
                "timestamp": time.time(),
                "volume_24h": self._dec_ticker(row, "v", 1),
                "trades": [],
                "bids": [],
                "asks": [],
            }
        except Exception:
            return None

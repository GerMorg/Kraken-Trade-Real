from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import threading
import time
from typing import Any

from app.domain.models import Decision, Instrument, MarketSnapshot, PortfolioState, Signal, new_id
from app.domain.states import Direction, ProductType
from app.domain.symbols import canonical_asset, resolve_instrument_symbol


D = Decimal


@dataclass(frozen=True)
class TacticalSignal:
    symbol: str
    direction: Direction
    score: D
    expected_move_bps: D
    expected_cost_bps: D
    spread_bps: D
    momentum_60_bps: D
    momentum_180_bps: D
    volatility_bps: D
    volume_ratio: D
    breakout_bps: D
    imbalance: D
    confidence: D
    reason: str

    @property
    def net_edge_bps(self) -> D:
        return self.expected_move_bps - self.expected_cost_bps


@dataclass
class TacticalPosition:
    symbol: str
    venue: str
    direction: Direction
    quantity: D
    entry_price: D
    peak_price: D
    trough_price: D
    notional_eur: D
    leverage: D
    opened_at: float
    entry_client_order_id: str
    setup_score: D
    state: str = "OPEN"


class TacticalTrader:
    """Short-horizon volatility/momentum engine isolated from the core strategy."""

    STRATEGY_VERSION = "tactical-volatility-v1"

    def __init__(
        self,
        config: Any,
        db: Any,
        audit: Any,
        gateway: Any,
        websocket: Any,
        authority: Any,
        portfolio: Any,
        intents: Any,
        risk: Any,
    ) -> None:
        self.config = config
        self.db = db
        self.audit = audit
        self.gateway = gateway
        self.websocket = websocket
        self.authority = authority
        self.portfolio = portfolio
        self.intents = intents
        self.risk = risk
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._candidates: dict[str, Instrument] = {}
        self._snapshots: dict[str, MarketSnapshot] = {}
        self._news_effects: dict[str, D] = {}
        self._gemini_bps = D("0")
        self._portfolio: PortfolioState | None = None
        self._positions: dict[str, TacticalPosition] = {}
        self._cooldown_until: float = 0.0
        self._last_run = 0.0
        self._last_signal: TacticalSignal | None = None
        self._last_action = "NONE"
        self._last_signal_reason = "NOT_EVALUATED"
        self._signal_rejections: Counter[str] = Counter()
        self._last_diagnostic_at = 0.0
        self._candidate_diagnostics: dict[str, Any] = {
            "entry_candidates": 0,
            "active_position_streams": 0,
            "unique_base_assets": 0,
            "duplicate_quote_pairs_removed": 0,
            "held_asset_pairs_excluded": 0,
            "duplicate_symbol_rows_removed": 0,
            "quote_pair_choices": {},
        }
        self._ranked_instruments: dict[str, Instrument] = {}
        self._load_positions()

    def start(self) -> None:
        if not bool(getattr(self.config, "tactical_enabled", False)):
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self.websocket.start()
            self._thread = threading.Thread(
                target=self._run,
                name="kraken-tactical-trader",
                daemon=True,
            )
            self._thread.start()
        self.audit.emit(
            "TACTICAL_STARTED",
            "INFO",
            shadow_mode=bool(getattr(self.config, "tactical_shadow_mode", True)),
            short_enabled=bool(getattr(self.config, "tactical_allow_short", True)),
            strategy_version=self.STRATEGY_VERSION,
        )

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self.websocket.stop()
        except Exception as exc:
            self.audit.emit(
                "TACTICAL_WS_STOP_FAILED",
                "WARNING",
                error_type=type(exc).__name__,
                error=str(exc)[:300],
            )

    def update_portfolio(self, portfolio: PortfolioState) -> None:
        with self._lock:
            self._portfolio = portfolio

    def update_context(
        self,
        instruments: list[Instrument],
        snapshots: dict[str, MarketSnapshot],
        news_effects: dict[str, D],
        gemini_bps: D,
    ) -> None:
        candidates = self._rank_candidates(instruments, snapshots)
        with self._lock:
            ordered_instruments = dict(self._ranked_instruments)
            # Keep a current instrument object for every ranked symbol; never rebuild
            # this mapping by iterating the original universe (which loses score order).
            for instrument in instruments:
                ordered_instruments.setdefault(instrument.symbol, instrument)
            for symbol, instrument in self._candidates.items():
                ordered_instruments.setdefault(symbol, instrument)
            self._candidates = {
                symbol: ordered_instruments[symbol]
                for symbol in candidates
                if symbol in ordered_instruments
            }
            self._snapshots = {
                symbol: snapshots[symbol]
                for symbol in candidates
                if symbol in snapshots
            }
            self._news_effects = {
                symbol: news_effects.get(symbol, D("0"))
                for symbol in self._candidates
            }
            self._gemini_bps = D(str(gemini_bps))
            selected_instruments = list(self._candidates.values())
            candidate_diagnostics = dict(self._candidate_diagnostics)

        aliases = {
            instrument.symbol: str(
                (instrument.metadata or {}).get("wsname") or instrument.symbol
            )
            for instrument in selected_instruments
        }
        # Open Tactical positions stay subscribed even when their market falls
        # outside the entry-candidate ranking; otherwise their exit logic loses
        # live prices and reversal signals.
        self.websocket.set_symbols(list(self._candidates), aliases=aliases)
        if bool(getattr(self.config, "tactical_seed_history", True)):
            for instrument in selected_instruments:
                snapshot = snapshots.get(instrument.symbol)
                if snapshot is not None and len(snapshot.closes) >= 2:
                    self.websocket.seed_price_history(
                        instrument.symbol,
                        snapshot.closes,
                        snapshot.timestamp,
                    )
        symbols = list(self._candidates)
        self.audit.emit(
            "TACTICAL_CANDIDATES_UPDATED",
            "INFO",
            # candidates/symbols both describe the complete live WebSocket set.
            candidates=len(symbols),
            symbols=symbols,
            entry_candidates=candidate_diagnostics.get("entry_candidates", 0),
            active_position_streams=candidate_diagnostics.get("active_position_streams", 0),
            unique_base_assets=candidate_diagnostics.get("unique_base_assets", 0),
            duplicate_quote_pairs_removed=candidate_diagnostics.get(
                "duplicate_quote_pairs_removed", 0
            ),
            held_asset_pairs_excluded=candidate_diagnostics.get("held_asset_pairs_excluded", 0),
            duplicate_symbol_rows_removed=candidate_diagnostics.get(
                "duplicate_symbol_rows_removed", 0
            ),
            quote_pair_choices=candidate_diagnostics.get("quote_pair_choices", {}),
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            position = next(iter(self._positions.values()), None)
            return {
                "enabled": bool(getattr(self.config, "tactical_enabled", False)),
                "shadow_mode": bool(getattr(self.config, "tactical_shadow_mode", True)),
                "ws_connected": bool(getattr(self.websocket, "connected", False)),
                "candidate_count": len(self._candidates),
                "entry_candidate_count": int(self._candidate_diagnostics.get("entry_candidates", 0)),
                "unique_base_asset_count": int(self._candidate_diagnostics.get("unique_base_assets", 0)),
                "duplicate_quote_pairs_removed": int(self._candidate_diagnostics.get("duplicate_quote_pairs_removed", 0)),
                "position_count": len(self._positions),
                "position_symbol": position.symbol if position else "",
                "position_direction": position.direction.value if position else "",
                "last_action": self._last_action,
                "last_signal": self._last_signal.symbol if self._last_signal else "",
                "last_signal_direction": self._last_signal.direction.value if self._last_signal else "",
                "last_net_edge_bps": str(self._last_signal.net_edge_bps if self._last_signal else D("0")),
            }

    def run_once(self) -> None:
        if not bool(getattr(self.config, "tactical_enabled", False)):
            return
        now = time.time()
        poll = max(2.0, float(getattr(self.config, "tactical_poll_seconds", 10)))
        if now - self._last_run < poll:
            return
        self._last_run = now
        try:
            self._reconcile_pending()
            self._manage_positions(now)
            with self._lock:
                has_capacity = len(self._positions) < int(
                    getattr(self.config, "tactical_max_positions", 1)
                )
            if has_capacity and now >= self._cooldown_until:
                self._evaluate_entries(now)
        except Exception as exc:
            self._last_action = "ERROR"
            self.audit.emit(
                "TACTICAL_CYCLE_FAILED",
                "ERROR",
                error_type=type(exc).__name__,
                error=str(exc)[:500],
            )

    def _run(self) -> None:
        while not self._stop_event.wait(1.0):
            self.run_once()

    def tracked_position_symbols(self) -> tuple[str, ...]:
        """Return all persisted Tactical positions that must remain monitored."""
        with self._lock:
            return tuple(sorted(self._positions))

    @staticmethod
    def _asset_key_from_symbol(
        symbol: str,
        instruments: list[Instrument] | tuple[Instrument, ...],
    ) -> str:
        instrument = resolve_instrument_symbol(str(symbol), instruments)
        if instrument is not None:
            return canonical_asset(instrument.base)
        raw = str(symbol or "").strip()
        if "/" in raw:
            return canonical_asset(raw.split("/", 1)[0])
        return ""

    @staticmethod
    def _held_asset_keys(
        portfolio: PortfolioState | None,
        instruments: list[Instrument] | tuple[Instrument, ...],
    ) -> set[str]:
        held: set[str] = set()
        positions = getattr(portfolio, "positions", {}) if portfolio is not None else {}
        if not isinstance(positions, dict):
            return held
        for symbol, value in positions.items():
            try:
                position_value = D(str(value or "0"))
            except (ArithmeticError, TypeError, ValueError):
                # Ignore malformed legacy position values without skipping
                # subsequent positions or hiding candidate ranking failures.
                position_value = D("0")
            if position_value == 0:
                continue
            instrument = resolve_instrument_symbol(str(symbol), instruments)
            if instrument is not None:
                key = canonical_asset(instrument.base)
            else:
                raw = str(symbol or "").strip()
                key = canonical_asset(raw.split("/", 1)[0]) if "/" in raw else ""
            if key:
                held.add(key)
        return held

    def _rank_candidates(
        self,
        instruments: list[Instrument],
        snapshots: dict[str, MarketSnapshot],
    ) -> list[str]:
        # Candidate slots represent assets, not quote pairs. EUR/USD listings for
        # the same base must not consume multiple slots or permit duplicate exposure.
        max_candidates = max(
            4, int(getattr(self.config, "tactical_candidate_limit", 12))
        )
        with self._lock:
            active_positions = dict(self._positions)
            previous_candidates = dict(self._candidates)
            portfolio = self._portfolio

        known_instruments: dict[str, Instrument] = {
            instrument.symbol: instrument for instrument in instruments
        }
        for symbol, instrument in previous_candidates.items():
            known_instruments.setdefault(symbol, instrument)

        active_instruments: dict[str, Instrument] = {}
        for symbol in sorted(active_positions):
            active_instrument: Instrument | None = known_instruments.get(symbol)
            if active_instrument is None:
                resolved = resolve_instrument_symbol(
                    symbol, list(known_instruments.values())
                )
                active_instrument = (
                    resolved if isinstance(resolved, Instrument) else None
                )
            if active_instrument is None:
                try:
                    from_db = self._instrument_from_db(symbol)
                    active_instrument = (
                        from_db if isinstance(from_db, Instrument) else None
                    )
                except Exception:
                    active_instrument = None
            if active_instrument is not None and active_instrument.venue == "spot":
                active_instruments[active_instrument.symbol] = active_instrument
                known_instruments.setdefault(active_instrument.symbol, active_instrument)

        active_symbols = list(active_instruments)
        active_base_keys = {
            canonical_asset(instrument.base) for instrument in active_instruments.values()
        }
        blocked_base_keys = self._held_asset_keys(
            portfolio, list(known_instruments.values())
        ) | active_base_keys

        # Duplicate rows for the exact same symbol are a separate data-quality
        # issue; discard them before grouping different quote markets by base.
        unique_by_symbol: dict[str, Instrument] = {}
        duplicate_symbol_rows_removed = 0
        for instrument in instruments:
            if instrument.symbol in unique_by_symbol:
                duplicate_symbol_rows_removed += 1
                continue
            unique_by_symbol[instrument.symbol] = instrument

        by_base: dict[str, list[tuple[D, D, D, str, Instrument]]] = {}
        held_asset_pairs_excluded = 0
        eligible_markets = 0
        for instrument in unique_by_symbol.values():
            # Active positions are force-included in the stream separately, not
            # treated as fresh entry candidates.
            if instrument.symbol in active_instruments:
                continue
            if instrument.venue != "spot" or not instrument.tradeable:
                continue
            if (instrument.metadata or {}).get("asset_class") == "tokenized_asset":
                continue
            snapshot = snapshots.get(instrument.symbol)
            if snapshot is None or snapshot.price <= 0:
                continue
            if snapshot.spread_bps > D(
                str(getattr(self.config, "tactical_max_spread_bps", 25))
            ):
                continue

            base_key = canonical_asset(instrument.base)
            if not base_key or base_key in blocked_base_keys:
                if base_key in blocked_base_keys:
                    held_asset_pairs_excluded += 1
                continue

            volatility = self._realized_volatility(snapshot.closes)
            quote_rate = D("1")
            rate_lookup = getattr(self.portfolio, "quote_to_eur_rate", None)
            if callable(rate_lookup):
                try:
                    converted_rate = rate_lookup(instrument.quote)
                    if converted_rate is not None and D(str(converted_rate)) > 0:
                        quote_rate = D(str(converted_rate))
                except Exception:
                    # Do not silently discard FX valuation failures: use the
                    # explicit neutral rate only for ranking, not order funding.
                    quote_rate = D("1")
            turnover_eur = (
                max(D("0"), D(str(snapshot.volume_24h)))
                * snapshot.price
                * quote_rate
            )
            liquidity_score = max(D("1"), turnover_eur).ln()
            score = (
                volatility * D("3")
                + liquidity_score
                - snapshot.spread_bps * D("0.25")
            )
            by_base.setdefault(base_key, []).append(
                (score, snapshot.spread_bps, turnover_eur, instrument.symbol, instrument)
            )
            eligible_markets += 1

        best_by_base: list[tuple[D, D, D, str, Instrument]] = []
        duplicate_quote_pairs_removed = 0
        quote_pair_choices: dict[str, dict[str, Any]] = {}
        for base_key, rows in by_base.items():
            # Higher score first; use tighter spread, greater EUR turnover and
            # finally symbol order as deterministic tie breakers.
            rows.sort(key=lambda row: (-row[0], row[1], -row[2], row[3]))
            best_by_base.append(rows[0])
            duplicate_quote_pairs_removed += max(0, len(rows) - 1)
            if len(rows) > 1:
                quote_pair_choices[base_key] = {
                    "selected": rows[0][3],
                    "alternatives": [
                        {
                            "symbol": row[3],
                            "score": str(row[0]),
                            "spread_bps": str(row[1]),
                            "turnover_24h_eur": str(row[2]),
                        }
                        for row in rows[1:]
                    ],
                }

        best_by_base.sort(key=lambda row: (-row[0], row[1], -row[2], row[3]))
        entry_rows = best_by_base[:max_candidates]
        entry_symbols = [row[3] for row in entry_rows]
        ordered_symbols = list(dict.fromkeys(active_symbols + entry_symbols))
        ranked_instruments = dict(active_instruments)
        ranked_instruments.update({row[3]: row[4] for row in entry_rows})
        unique_asset_keys = {
            canonical_asset(ranked_instruments[symbol].base)
            for symbol in ordered_symbols
            if symbol in ranked_instruments
        }

        diagnostics = {
            "eligible_markets": eligible_markets,
            "entry_candidates": len(entry_symbols),
            "active_position_streams": len(active_symbols),
            "unique_base_assets": len(unique_asset_keys),
            "duplicate_quote_pairs_removed": duplicate_quote_pairs_removed,
            "held_asset_pairs_excluded": held_asset_pairs_excluded,
            "duplicate_symbol_rows_removed": duplicate_symbol_rows_removed,
            "quote_pair_choices": quote_pair_choices,
        }
        with self._lock:
            self._candidate_diagnostics = diagnostics
            self._ranked_instruments = ranked_instruments
        return ordered_symbols

    def _evaluate_entries(self, now: float) -> None:
        with self._lock:
            candidates = list(self._candidates.values())
            portfolio = self._portfolio
            active_position_symbols = tuple(self._positions)
        if portfolio is None or portfolio.equity_eur <= 0:
            return
        if self._daily_loss_blocked(portfolio):
            self._last_action = "DAILY_LOSS_BLOCK"
            return
        if self._trade_budget_blocked():
            self._last_action = "TRADE_BUDGET_BLOCK"
            return

        blocked_base_keys = self._held_asset_keys(portfolio, candidates)
        for active_symbol in active_position_symbols:
            active_key = self._asset_key_from_symbol(active_symbol, candidates)
            if active_key:
                blocked_base_keys.add(active_key)

        signals: list[TacticalSignal] = []
        rejection_counts: Counter[str] = Counter()
        evaluated = 0
        for instrument in candidates:
            if canonical_asset(instrument.base) in blocked_base_keys:
                rejection_counts["BASE_ASSET_ALREADY_HELD"] += 1
                continue
            evaluated += 1
            state = self.websocket.market_snapshot(instrument.symbol)
            signal = self._build_signal(instrument, state, now)
            if signal is not None:
                signals.append(signal)
            else:
                rejection_counts[self._last_signal_reason] += 1

        if not signals:
            self._last_action = "NO_SIGNAL"
            if now - self._last_diagnostic_at >= float(
                getattr(self.config, "tactical_diagnostics_interval_seconds", 60)
            ):
                self.audit.emit(
                    "TACTICAL_EVALUATION",
                    "INFO",
                    evaluated=evaluated,
                    signals_found=0,
                    rejection_counts=dict(rejection_counts),
                    last_reason=self._last_signal_reason,
                    candidate_count=len(candidates),
                    position_count=len(portfolio.positions),
                )
                self._last_diagnostic_at = now
            return
        signal = max(signals, key=lambda item: (item.net_edge_bps, item.score))
        with self._lock:
            self._last_signal = signal
        self.audit.emit(
            "TACTICAL_SIGNAL",
            "INFO",
            symbol=signal.symbol,
            direction=signal.direction.value,
            score=str(signal.score),
            expected_move_bps=str(signal.expected_move_bps),
            expected_cost_bps=str(signal.expected_cost_bps),
            net_edge_bps=str(signal.net_edge_bps),
            spread_bps=str(signal.spread_bps),
            momentum_60_bps=str(signal.momentum_60_bps),
            momentum_180_bps=str(signal.momentum_180_bps),
            volatility_bps=str(signal.volatility_bps),
            volume_ratio=str(signal.volume_ratio),
            breakout_bps=str(signal.breakout_bps),
            imbalance=str(signal.imbalance),
            confidence=str(signal.confidence),
            reason=signal.reason,
        )
        self._enter(signal, now)

    def _build_signal(
        self,
        instrument: Instrument,
        state: dict[str, Any] | None,
        now: float,
    ) -> TacticalSignal | None:
        if state is None:
            return self._signal_reject(instrument.symbol, "NO_STREAM_DATA")
        bid = D(str(state.get("bid", "0")))
        ask = D(str(state.get("ask", "0")))
        price = D(str(state.get("price", "0")))
        spread = D(str(state.get("spread_bps", "999999")))
        timestamp = float(state.get("timestamp", 0) or 0)
        if min(bid, ask, price) <= 0:
            return self._signal_reject(instrument.symbol, "INVALID_MARKET_DATA")
        max_spread = D(str(getattr(self.config, "tactical_max_spread_bps", 25)))
        max_age = D(str(getattr(self.config, "tactical_market_max_age_seconds", 5)))
        if spread > max_spread or now - timestamp > float(max_age):
            return self._signal_reject(instrument.symbol, "STALE_OR_WIDE_MARKET")

        points = tuple(
            (float(item[0]), D(str(item[1])))
            for item in state.get("price_points", ())
            if isinstance(item, (list, tuple)) and len(item) >= 2 and D(str(item[1])) > 0
        )
        sampled = self._sample_prices(points, now, 180.0, 10.0)
        if len(sampled) < 8:
            return self._signal_reject(instrument.symbol, "INSUFFICIENT_PRICE_HISTORY")
        momentum_60 = self._return_since(sampled, 60.0)
        momentum_180 = self._return_since(sampled, 180.0)
        volatility = self._realized_volatility(tuple(price for _, price in sampled))
        if volatility < D(str(getattr(self.config, "tactical_min_volatility_bps", 12))):
            return self._signal_reject(instrument.symbol, "LOW_VOLATILITY")
        if volatility > D(str(getattr(self.config, "tactical_max_volatility_bps", 55))):
            return self._signal_reject(instrument.symbol, "HIGH_VOLATILITY")

        recent = [price for ts, price in sampled if now - ts <= 60.0]
        if len(recent) < 4:
            return self._signal_reject(instrument.symbol, "INSUFFICIENT_RECENT_HISTORY")
        recent_high = max(recent[:-1])
        recent_low = min(recent[:-1])
        breakout_long = (price / recent_high - D("1")) * D("10000") if recent_high else D("0")
        breakout_short = (recent_low / price - D("1")) * D("10000") if price else D("0")

        metrics = self.websocket.trade_metrics(
            instrument.symbol,
            float(getattr(self.config, "tactical_trade_lookback_seconds", 900)),
        )
        volume_ratio = D(str(metrics.get("volume_ratio", "0")))
        if volume_ratio < D(str(getattr(self.config, "tactical_min_volume_ratio", 2))):
            return self._signal_reject(instrument.symbol, "LOW_VOLUME_EXPANSION")
        imbalance = self._book_imbalance(state.get("depths_bid", ()), state.get("depths_ask", ()))
        min_imbalance = D(str(getattr(self.config, "tactical_min_imbalance", 0.10)))
        min_momentum = D(str(getattr(self.config, "tactical_min_momentum_bps", 40)))
        min_breakout = D(str(getattr(self.config, "tactical_min_breakout_bps", 25)))

        context = self._news_effects.get(instrument.symbol, D("0")) + self._gemini_bps
        context_block = D(str(getattr(self.config, "tactical_context_block_bps", 80)))

        long_score = self._score(
            momentum_60,
            momentum_180,
            volatility,
            volume_ratio,
            breakout_long,
            imbalance,
        )
        short_score = self._score(
            -momentum_60,
            -momentum_180,
            volatility,
            volume_ratio,
            breakout_short,
            -imbalance,
        )

        long_ok = (
            instrument.long_available
            and momentum_60 >= min_momentum
            and momentum_180 >= min_momentum
            and breakout_long >= min_breakout
            and imbalance >= min_imbalance
            and context >= -context_block
        )
        short_ok = (
            bool(getattr(self.config, "tactical_allow_short", True))
            and instrument.short_available
            and momentum_60 <= -min_momentum
            and momentum_180 <= -min_momentum
            and breakout_short >= min_breakout
            and imbalance <= -min_imbalance
            and context <= context_block
        )

        candidates: list[tuple[D, Direction, D, D, D, str]] = []
        if long_ok:
            expected_move = max(
                momentum_60 * D("2"),
                momentum_180 * D("1.25"),
                volatility * D("3"),
                breakout_long * D("2"),
            )
            candidates.append(
                (long_score, Direction.LONG, expected_move, breakout_long, imbalance, "VOL_BREAKOUT_LONG")
            )
        if short_ok:
            expected_move = max(
                -momentum_60 * D("2"),
                -momentum_180 * D("1.25"),
                volatility * D("3"),
                breakout_short * D("2"),
            )
            candidates.append(
                (short_score, Direction.SHORT, expected_move, breakout_short, -imbalance, "VOL_BREAKOUT_SHORT")
            )
        if not candidates:
            return self._signal_reject(instrument.symbol, "DIRECTIONAL_SETUP_NOT_MET")

        score, direction, expected_move, breakout, signed_imbalance, reason = max(
            candidates, key=lambda item: item[0]
        )
        entry_fee = D(str(getattr(self.config, "tactical_entry_fee_bps", 80)))
        exit_fee = D(str(getattr(self.config, "tactical_exit_fee_bps", 80)))
        slippage = D(str(getattr(self.config, "tactical_expected_slippage_bps", 25)))
        safety = D(str(getattr(self.config, "tactical_safety_buffer_bps", 30)))
        margin_open_fee = (
            D(str(getattr(self.config, "tactical_margin_open_fee_bps", 5.0)))
            if direction == Direction.SHORT
            and instrument.product_type.value == "SPOT_MARGIN"
            else D("0")
        )
        cost = entry_fee + exit_fee + spread + slippage + safety + margin_open_fee
        confidence = max(
            D("0"),
            min(D("1"), D("0.50") + score / D("400")),
        )
        adaptive_enabled = bool(
            getattr(self.config, "tactical_adaptive_entry_enabled", True)
        )
        adaptive_min_move = D(str(
            getattr(self.config, "tactical_adaptive_min_expected_move_bps", 230.0)
        ))
        adaptive_min_confidence = D(str(
            getattr(self.config, "tactical_adaptive_min_confidence", 0.75)
        ))
        adaptive_min_edge = D(str(
            getattr(self.config, "tactical_adaptive_min_net_edge_bps", 15.0)
        ))
        adaptive_ok = self._adaptive_entry_allowed(
            expected_move,
            cost,
            confidence,
            adaptive_enabled,
            adaptive_min_move,
            adaptive_min_confidence,
            adaptive_min_edge,
        )
        configured_min_move = D(str(
            getattr(self.config, "tactical_min_expected_move_bps", 280)
        ))
        min_move = min(configured_min_move, adaptive_min_move) if adaptive_ok else configured_min_move
        if expected_move < min_move or expected_move - cost < (
            adaptive_min_edge if adaptive_ok else D("0")
        ):
            self._signal_rejections["EXPECTED_MOVE_OR_EDGE_TOO_SMALL"] += 1
            return self._signal_reject(
                instrument.symbol, "EXPECTED_MOVE_OR_EDGE_TOO_SMALL"
            )
        return TacticalSignal(
            instrument.symbol,
            direction,
            score,
            expected_move,
            cost,
            spread,
            momentum_60,
            momentum_180,
            volatility,
            volume_ratio,
            breakout,
            signed_imbalance,
            confidence,
            reason,
        )

    def _enter(self, signal: TacticalSignal, now: float) -> None:
        with self._lock:
            instrument = self._candidates.get(signal.symbol)
            portfolio = self._portfolio
        if instrument is None or portfolio is None or portfolio.equity_eur <= 0:
            return

        asset_key = canonical_asset(instrument.base)
        with self._lock:
            tracked_symbols = tuple(self._positions)
            instrument_universe = list(self._candidates.values())
        if instrument not in instrument_universe:
            instrument_universe.append(instrument)
        blocked_assets = self._held_asset_keys(portfolio, instrument_universe)
        for tracked_symbol in tracked_symbols:
            tracked_key = self._asset_key_from_symbol(tracked_symbol, instrument_universe)
            if tracked_key:
                blocked_assets.add(tracked_key)
        if asset_key in blocked_assets:
            self.audit.emit(
                "TACTICAL_ENTRY_BLOCKED",
                "WARNING",
                symbol=signal.symbol,
                direction=signal.direction.value,
                reason="BASE_ASSET_ALREADY_HELD",
                base_asset=asset_key,
            )
            self._last_action = "ENTRY_BLOCKED_DUPLICATE_ASSET"
            return

        notional = min(
            portfolio.equity_eur
            * D(str(getattr(self.config, "tactical_portfolio_pct", 25)))
            / D("100"),
            D(str(getattr(self.config, "tactical_max_capital_eur", 15))),
        )
        min_cost_eur = self.portfolio.min_cost_eur(instrument)
        if min_cost_eur is None or notional < min_cost_eur:
            self._last_action = "ENTRY_MINIMUM_COST"
            return

        leverage = self._entry_leverage(instrument, signal.direction)
        if leverage <= 0:
            self._last_action = "ENTRY_LEVERAGE_UNAVAILABLE"
            return

        if (
            signal.direction == Direction.LONG
            and instrument.venue == "spot"
            and not self._shadow_mode()
        ):
            available_quote = self.portfolio.cash_balance(instrument.quote)
            quote_rate = self.portfolio.quote_to_eur_rate(instrument.quote)
            required_quote = (
                notional / quote_rate if quote_rate and quote_rate > 0 else D("0")
            )
            if quote_rate is None or available_quote < required_quote:
                self.audit.emit(
                    "TACTICAL_ENTRY_BLOCKED",
                    "INFO",
                    symbol=signal.symbol,
                    direction=signal.direction.value,
                    reason="QUOTE_FUNDS_UNAVAILABLE",
                    quote=instrument.quote,
                    available_quote=str(available_quote),
                    required_quote=str(required_quote),
                )
                self._last_action = "ENTRY_QUOTE_FUNDS_BLOCK"
                return

        state = self.websocket.market_snapshot(signal.symbol)
        if state is None:
            return
        snapshot = self._stream_snapshot(signal.symbol, state)
        target = notional if signal.direction == Direction.LONG else -notional
        decision = Decision(
            decision_id=new_id("decision"),
            instrument=instrument,
            signal=Signal(
                signal.symbol,
                signal.direction,
                signal.expected_move_bps,
                signal.expected_cost_bps,
                signal.confidence,
                "TACTICAL_VOLATILITY",
                self._news_effects.get(signal.symbol, D("0")),
                self._gemini_bps,
                {
                    "volatility": signal.volatility_bps,
                    "spread_bps": signal.spread_bps,
                    "tactical_score": signal.score,
                    "volume_ratio": signal.volume_ratio,
                    "breakout_bps": signal.breakout_bps,
                    "book_imbalance": signal.imbalance,
                },
            ),
            target_notional_eur=target,
            leverage=leverage,
            rationale={
                "risk_profile": "tactical",
                "risk_position_limit_pct": D(str(getattr(self.config, "tactical_position_limit_pct", 25))),
                "risk_volatility_max": D(str(getattr(self.config, "tactical_max_volatility_bps", 55))),
                "tactical_setup_score": signal.score,
                "tactical_exit_reason": "",
                "tactical_direction": signal.direction.value,
            },
            strategy_version=self.STRATEGY_VERSION,
            model_version="tactical-v1",
            config_hash="",
            current_position_eur=portfolio.positions.get(signal.symbol, D("0")),
            target_position_eur=target,
            execution_direction=signal.direction,
            reduce_only=False,
        )

        quantity = self.portfolio.quantity_for_eur(instrument, notional, snapshot.price)
        if quantity is None or quantity <= 0:
            return
        if quantity < instrument.min_order_qty or (
            instrument.min_cost > 0 and quantity * snapshot.price < instrument.min_cost
        ):
            self.audit.emit(
                "TACTICAL_ENTRY_BLOCKED",
                "INFO",
                symbol=signal.symbol,
                reason="MIN_ORDER_SIZE",
                quantity=str(quantity),
                minimum_qty=str(instrument.min_order_qty),
                minimum_cost=str(instrument.min_cost),
            )
            return

        risk = self.risk.evaluate(
            decision,
            portfolio,
            snapshot,
            self.portfolio.spot_margin_account
            if instrument.product_type.value == "SPOT_MARGIN"
            else self.portfolio.futures_margin_account,
        )
        self.audit.emit(
            "TACTICAL_RISK_DECISION",
            "INFO",
            symbol=signal.symbol,
            direction=signal.direction.value,
            allowed=risk.allowed,
            reason=risk.reason,
            checks=risk.checks,
            leverage=str(leverage),
            notional_eur=str(notional),
        )
        if not risk.allowed:
            self._last_action = f"RISK_{risk.reason}"
            return

        if self._shadow_mode():
            client_order_id = "SHADOW-" + new_id("order")[-30:]
            self._open_position(
                decision,
                signal,
                quantity,
                snapshot.price,
                client_order_id,
                now,
            )
            self.audit.emit(
                "TACTICAL_SHADOW_ENTRY",
                "INFO",
                symbol=signal.symbol,
                direction=signal.direction.value,
                quantity=str(quantity),
                entry_price=str(snapshot.price),
                notional_eur=str(notional),
                leverage=str(leverage),
            )
            self._last_action = "SHADOW_ENTRY"
            return

        method = "market"
        limit_price = None
        if instrument.venue == "spot":
            # A marketable limit is used for live tactical execution so the
            # price cannot run away while the order is crossing the spread.
            method = "limit"
            limit_price = snapshot.ask if signal.direction == Direction.LONG else snapshot.bid
        intent = self.intents.build(
            decision,
            leverage,
            method,
            quantity,
            limit_price,
            reduce_only=False,
            post_only=False,
        )
        result = self.authority.submit(intent, snapshot)
        self.audit.emit(
            "TACTICAL_ENTRY_ORDER_RESULT",
            "INFO",
            symbol=signal.symbol,
            direction=signal.direction.value,
            state=str(result.get("state", "")),
            reason=str(result.get("reason", "")),
            client_order_id=intent.client_order_id,
        )
        if result.get("state") not in {"ACKNOWLEDGED", "LIVE", "PARTIALLY_FILLED", "FILLED"}:
            self._last_action = "ENTRY_BLOCKED"
            return
        fill = self._wait_for_fill(intent, instrument, result.get("kraken_order_id"))
        if fill is None:
            self._last_action = "ENTRY_PENDING_RECONCILIATION"
            return
        fill_qty, fill_price = fill
        self._open_position(
            decision,
            signal,
            fill_qty,
            fill_price,
            intent.client_order_id,
            now,
        )
        self._last_action = "LIVE_ENTRY"

    def _manage_positions(self, now: float) -> None:
        with self._lock:
            positions = list(self._positions.values())
        for position in positions:
            state = self._fresh_position_state(position, now)
            if state is None:
                self.audit.emit(
                    "TACTICAL_POSITION_DATA_STALE",
                    "WARNING",
                    symbol=position.symbol,
                    direction=position.direction.value,
                )
                continue
            price = D(str(state.get("price", "0")))
            if price <= 0:
                continue
            if position.direction == Direction.LONG:
                position.peak_price = max(position.peak_price, price)
                position.trough_price = min(position.trough_price, price)
                pnl_bps = (price / position.entry_price - D("1")) * D("10000")
                peak_gain = (position.peak_price / position.entry_price - D("1")) * D("10000")
                trailing_triggered = (
                    peak_gain >= D(str(getattr(self.config, "tactical_trailing_trigger_bps", 100)))
                    and price <= position.peak_price
                    * (D("1") - D(str(getattr(self.config, "tactical_trailing_stop_pct", 0.7))) / D("100"))
                )
            else:
                position.trough_price = min(position.trough_price, price)
                position.peak_price = max(position.peak_price, price)
                pnl_bps = (position.entry_price / price - D("1")) * D("10000")
                peak_gain = (position.entry_price / position.trough_price - D("1")) * D("10000")
                trailing_triggered = (
                    peak_gain >= D(str(getattr(self.config, "tactical_trailing_trigger_bps", 100)))
                    and price >= position.trough_price
                    * (D("1") + D(str(getattr(self.config, "tactical_trailing_stop_pct", 0.7))) / D("100"))
                )

            reason = ""
            if pnl_bps <= -D(str(getattr(self.config, "tactical_stop_loss_pct", 1.0))) * D("100"):
                reason = "STOP_LOSS"
            elif pnl_bps >= D(str(getattr(self.config, "tactical_take_profit_pct", 2.2))) * D("100"):
                reason = "TAKE_PROFIT"
            elif trailing_triggered:
                reason = "TRAILING_STOP"
            elif now - position.opened_at >= float(
                getattr(self.config, "tactical_max_hold_seconds", 1800)
            ):
                reason = "TIME_STOP"

            opposite = self._opposite_signal(position)
            reversal_edge = D(str(getattr(self.config, "tactical_reversal_exit_bps", 120)))
            if not reason and opposite is not None and opposite.net_edge_bps >= reversal_edge:
                reason = "SIGNAL_REVERSAL"
            self.db.save_tactical_position(
                position.symbol,
                position.venue,
                position.direction.value,
                position.quantity,
                position.entry_price,
                position.peak_price,
                position.trough_price,
                position.notional_eur,
                position.leverage,
                position.opened_at,
                position.entry_client_order_id,
                position.setup_score,
                position.state,
            )
            if reason:
                self._exit(position, price, reason, now)

    def _exit(
        self,
        position: TacticalPosition,
        price: D,
        reason: str,
        now: float,
    ) -> None:
        instrument = self._candidates.get(position.symbol)
        if instrument is None:
            instrument = self._instrument_from_db(position.symbol)
        if instrument is None:
            return
        state = self._fresh_position_state(position, now)
        if state is None:
            return
        snapshot = self._stream_snapshot(position.symbol, state)
        signal_direction = Direction.SHORT if position.direction == Direction.LONG else Direction.LONG
        current = (
            position.notional_eur
            if position.direction == Direction.LONG
            else -position.notional_eur
        )
        signal = Signal(
            position.symbol,
            signal_direction,
            D("0"),
            D("0"),
            D("1"),
            "TACTICAL_EXIT",
            D("0"),
            D("0"),
            {
                "volatility": self._realized_volatility(snapshot.closes),
                "spread_bps": snapshot.spread_bps,
            },
        )
        decision = Decision(
            new_id("decision"),
            instrument,
            signal,
            abs(current),
            position.leverage,
            {
                "risk_profile": "tactical",
                "risk_position_limit_pct": D(str(getattr(self.config, "tactical_position_limit_pct", 25))),
                "risk_volatility_max": D(str(getattr(self.config, "tactical_max_volatility_bps", 55))),
                "tactical_setup_score": position.setup_score,
                "tactical_exit_reason": reason,
                "tactical_direction": signal_direction.value,
            },
            self.STRATEGY_VERSION,
            "tactical-v1",
            "",
            current,
            D("0"),
            signal_direction,
            True,
        )
        portfolio = self._portfolio
        if portfolio is None:
            return
        risk = self.risk.evaluate(
            decision,
            portfolio,
            snapshot,
            self.portfolio.spot_margin_account
            if instrument.product_type.value == "SPOT_MARGIN"
            else self.portfolio.futures_margin_account,
        )
        if not risk.allowed:
            self.audit.emit(
                "TACTICAL_EXIT_BLOCKED",
                "WARNING",
                symbol=position.symbol,
                reason=risk.reason,
                exit_reason=reason,
            )
            return

        if self._shadow_mode():
            self._close_shadow(position, price, reason, now)
            self._last_action = "SHADOW_EXIT"
            return

        method = "market"
        limit_price = None
        if instrument.venue == "spot":
            method = "limit"
            limit_price = snapshot.bid if position.direction == Direction.LONG else snapshot.ask
        intent = self.intents.build(
            decision,
            position.leverage,
            method,
            position.quantity,
            limit_price,
            reduce_only=True,
            post_only=False,
        )
        result = self.authority.submit(intent, snapshot)
        self.audit.emit(
            "TACTICAL_EXIT_ORDER_RESULT",
            "INFO",
            symbol=position.symbol,
            exit_reason=reason,
            state=str(result.get("state", "")),
            reason_result=str(result.get("reason", "")),
            client_order_id=intent.client_order_id,
        )
        if result.get("state") not in {"ACKNOWLEDGED", "LIVE", "PARTIALLY_FILLED", "FILLED"}:
            return
        fill = self._wait_for_fill(
            intent, instrument,
            str(result.get("kraken_order_id") or "") or None,
        )
        if fill is None:
            return
        fill_qty, fill_price = fill
        if fill_qty <= 0:
            return
        self._close_live(position, fill_qty, fill_price, reason, now, intent.client_order_id)

    def _wait_for_fill(
        self,
        intent: Any,
        instrument: Instrument,
        kraken_order_id: str | None = None,
    ) -> tuple[D, D] | None:
        deadline = time.monotonic() + max(
            1.0, float(getattr(self.config, "tactical_order_confirm_seconds", 5))
        )
        last_payload: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                rows = self.gateway.lookup_order(
                    client_order_id=intent.client_order_id,
                    instrument=instrument,
                    kraken_order_id=kraken_order_id,
                )
                if rows:
                    last_payload = rows[0]
                    status = self.authority.reconciler.state_from_exchange(rows[0])
                    if status.value == "FILLED":
                        quantity = D(str(rows[0].get("vol_exec") or rows[0].get("executed_volume") or intent.quantity))
                        price = D(
                            str(
                                rows[0].get("price")
                                or rows[0].get("avg_price")
                                or rows[0].get("avgPrice")
                                or intent.limit_price
                                or "0"
                            )
                        )
                        if quantity > 0 and price > 0:
                            self.db.update_order_state(
                                intent.client_order_id,
                                status.value,
                                kraken_order_id=str(rows[0].get("txid") or rows[0].get("order_id") or "") or None,
                            )
                            return quantity, price
            except Exception as exc:
                self.audit.emit(
                    "TACTICAL_FILL_CHECK_FAILED",
                    "WARNING",
                    symbol=instrument.symbol,
                    error_type=type(exc).__name__,
                )
            time.sleep(0.5)
        self.audit.emit(
            "TACTICAL_FILL_PENDING",
            "WARNING",
            symbol=instrument.symbol,
            client_order_id=intent.client_order_id,
            state=(
                str(last_payload.get("status") or last_payload.get("state") or "UNKNOWN")
                if last_payload
                else "UNKNOWN"
            ),
        )
        return None

    def _open_position(
        self,
        decision: Decision,
        signal: TacticalSignal,
        quantity: D,
        price: D,
        client_order_id: str,
        now: float,
    ) -> None:
        position = TacticalPosition(
            decision.instrument.symbol,
            decision.instrument.venue,
            signal.direction,
            quantity,
            price,
            price,
            price,
            abs(decision.target_notional_eur),
            decision.leverage,
            now,
            client_order_id,
            signal.score,
        )
        with self._lock:
            self._positions[position.symbol] = position
        self.db.save_tactical_position(
            position.symbol,
            position.venue,
            position.direction.value,
            position.quantity,
            position.entry_price,
            position.peak_price,
            position.trough_price,
            position.notional_eur,
            position.leverage,
            position.opened_at,
            position.entry_client_order_id,
            position.setup_score,
            position.state,
        )

    @staticmethod
    def _trade_gross_pnl(
        direction: Direction, notional_eur: D, entry_price: D, exit_price: D
    ) -> D:
        exposure = abs(notional_eur)
        if exposure <= 0 or entry_price <= 0 or exit_price <= 0:
            return D("0")
        return (
            (exit_price / entry_price - D("1"))
            if direction == Direction.LONG
            else (entry_price / exit_price - D("1"))
        ) * exposure

    def _trade_fees(self, notional_eur: D) -> D:
        return abs(notional_eur) * (
            D(str(getattr(self.config, "tactical_entry_fee_bps", 80)))
            + D(str(getattr(self.config, "tactical_exit_fee_bps", 80)))
        ) / D("10000")

    def _close_shadow(
        self,
        position: TacticalPosition,
        price: D,
        reason: str,
        now: float,
    ) -> None:
        gross = self._trade_gross_pnl(
            position.direction, position.notional_eur, position.entry_price, price
        )
        fees = self._trade_fees(position.notional_eur)
        net = gross - fees
        trade_id = new_id("tactical_trade")
        self.db.save_tactical_trade(
            trade_id,
            position.symbol,
            position.direction.value,
            position.entry_price,
            price,
            position.quantity,
            gross,
            fees,
            net,
            position.opened_at,
            now,
            now - position.opened_at,
            reason,
            position.setup_score,
            {"mode": "SHADOW"},
        )
        self.db.delete_tactical_position(position.symbol)
        with self._lock:
            self._positions.pop(position.symbol, None)
            self._cooldown_until = now + float(
                getattr(self.config, "tactical_cooldown_seconds", 120)
            )
        self.audit.emit(
            "TACTICAL_SHADOW_EXIT",
            "INFO",
            symbol=position.symbol,
            direction=position.direction.value,
            exit_reason=reason,
            gross_pnl_eur=str(gross),
            fees_eur=str(fees),
            net_pnl_eur=str(net),
            hold_seconds=round(now - position.opened_at, 2),
        )

    def _close_live(
        self,
        position: TacticalPosition,
        fill_qty: D,
        fill_price: D,
        reason: str,
        now: float,
        client_order_id: str,
    ) -> None:
        ratio = min(D("1"), fill_qty / position.quantity) if position.quantity > 0 else D("1")
        closed_notional = abs(position.notional_eur) * ratio
        gross = self._trade_gross_pnl(
            position.direction, closed_notional, position.entry_price, fill_price
        )
        fees = self._trade_fees(closed_notional)
        net = gross - fees
        trade_id = new_id("tactical_trade")
        self.db.save_tactical_trade(
            trade_id,
            position.symbol,
            position.direction.value,
            position.entry_price,
            fill_price,
            fill_qty,
            gross,
            fees,
            net,
            position.opened_at,
            now,
            now - position.opened_at,
            reason,
            position.setup_score,
            {"mode": "LIVE", "client_order_id": client_order_id},
        )
        if ratio >= D("0.999999"):
            self.db.delete_tactical_position(position.symbol)
            with self._lock:
                self._positions.pop(position.symbol, None)
        else:
            position.quantity -= fill_qty
            position.notional_eur *= (D("1") - ratio)
            self.db.save_tactical_position(
                position.symbol,
                position.venue,
                position.direction.value,
                position.quantity,
                position.entry_price,
                position.peak_price,
                position.trough_price,
                position.notional_eur,
                position.leverage,
                position.opened_at,
                position.entry_client_order_id,
                position.setup_score,
                position.state,
            )
        with self._lock:
            self._cooldown_until = now + float(
                getattr(self.config, "tactical_cooldown_seconds", 120)
            )
        self.audit.emit(
            "TACTICAL_LIVE_EXIT",
            "INFO",
            symbol=position.symbol,
            direction=position.direction.value,
            exit_reason=reason,
            gross_pnl_eur=str(gross),
            fees_eur=str(fees),
            net_pnl_eur=str(net),
            hold_seconds=round(now - position.opened_at, 2),
        )

    def _fresh_position_state(
        self, position: TacticalPosition, now: float
    ) -> dict[str, Any] | None:
        state = self.websocket.market_snapshot(position.symbol)
        max_age = float(getattr(self.config, "tactical_market_max_age_seconds", 5))
        if state is not None:
            try:
                if now - float(state.get("timestamp") or 0) <= max_age:
                    return state
            except (TypeError, ValueError):
                pass
        instrument = self._candidates.get(position.symbol) or self._instrument_from_db(position.symbol)
        if instrument is None:
            return None
        try:
            raw = self.gateway.spot_public("Ticker", {"pair": instrument.instrument_id})
            row = None
            if isinstance(raw, dict):
                row = raw.get(instrument.instrument_id) or raw.get(instrument.altname)
                if row is None and raw:
                    row = next(iter(raw.values()))
            if not isinstance(row, dict):
                return None
            bid = D(str((row.get("b") or [0])[0]))
            ask = D(str((row.get("a") or [0])[0]))
            last = D(str((row.get("c") or [0])[0]))
            if min(bid, ask, last) <= 0:
                return None
            spread = (ask - bid) / ((ask + bid) / D("2")) * D("10000")
            return {
                "symbol": position.symbol, "price": last, "bid": bid, "ask": ask,
                "timestamp": time.time(),
                "closes": tuple(state.get("closes", ())) if state else (),
                "price_points": tuple(state.get("price_points", ())) if state else (),
                "depths_bid": tuple(state.get("depths_bid", ())) if state else (),
                "depths_ask": tuple(state.get("depths_ask", ())) if state else (),
                "spread_bps": spread,
            }
        except Exception as exc:
            self.audit.emit(
                "TACTICAL_POSITION_TICKER_FALLBACK_FAILED", "WARNING",
                symbol=position.symbol, error_type=type(exc).__name__,
                error=str(exc)[:300],
            )
            return None

    def _opposite_signal(self, position: TacticalPosition) -> TacticalSignal | None:
        instrument = self._candidates.get(position.symbol)
        if instrument is None:
            return None
        state = self.websocket.market_snapshot(position.symbol)
        return self._build_signal(instrument, state, time.time())

    def _entry_leverage(self, instrument: Instrument, direction: Direction) -> D:
        if direction == Direction.LONG:
            return D("1")
        if instrument.product_type.value == "DERIVATIVE":
            return D("1")
        if instrument.product_type.value != "SPOT_MARGIN" or not instrument.short_available:
            return D("0")
        levels = tuple(sorted(
            D(str(value))
            for value in instrument.metadata.get("leverage_sell", [])
            if str(value)
        ))
        if not levels:
            levels = instrument.leverage_levels
        requested = max(
            D("2"),
            D(str(getattr(self.config, "tactical_short_leverage", 2.0))),
        )
        eligible = [level for level in levels if level >= requested]
        return eligible[0] if eligible else D("0")

    def _daily_loss_blocked(self, portfolio: PortfolioState) -> bool:
        limit_pct = D(str(getattr(self.config, "tactical_max_daily_loss_pct", 1.5)))
        limit = portfolio.equity_eur * limit_pct / D("100")
        today = self.db.tactical_today_net_pnl()
        return today <= -limit

    def _trade_budget_blocked(self) -> bool:
        hour = self.db.tactical_trade_count(time.time() - 3600)
        day = self.db.tactical_trade_count(
            datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        return (
            hour >= int(getattr(self.config, "tactical_max_trades_per_hour", 2))
            or day >= int(getattr(self.config, "tactical_max_trades_per_day", 6))
        )

    def _shadow_mode(self) -> bool:
        return bool(getattr(self.config, "tactical_shadow_mode", True))

    def _load_positions(self) -> None:
        rows = self.db.tactical_positions()
        for row in rows:
            try:
                position = TacticalPosition(
                    str(row["symbol"]),
                    str(row["venue"]),
                    Direction(str(row["direction"])),
                    D(str(row["quantity"])),
                    D(str(row["entry_price"])),
                    D(str(row["peak_price"])),
                    D(str(row["trough_price"])),
                    abs(D(str(row["notional_eur"]))),
                    D(str(row["leverage"])),
                    float(row["opened_at"]),
                    str(row["entry_client_order_id"]),
                    D(str(row["setup_score"])),
                    str(row.get("state") or "OPEN"),
                )
                self._positions[position.symbol] = position
                self.db.save_tactical_position(
                    position.symbol, position.venue, position.direction.value,
                    position.quantity, position.entry_price, position.peak_price,
                    position.trough_price, position.notional_eur, position.leverage,
                    position.opened_at, position.entry_client_order_id,
                    position.setup_score, position.state,
                )
            except Exception:
                self.db.delete_tactical_position(str(row.get("symbol", "")))

    def _reconcile_pending(self) -> None:
        rows = self.db.query(
            """SELECT o.client_order_id,o.symbol,o.state,o.kraken_order_id,o.quantity,o.side,
                      o.direction,o.leverage,o.created_at,
                      d.rationale_json,d.target_notional_eur
               FROM orders o JOIN decisions d ON d.decision_id=o.decision_id
               WHERE d.strategy_version=? AND o.state IN
                 ('SUBMITTING','ACKNOWLEDGED','LIVE','PARTIALLY_FILLED','UNKNOWN_RECONCILING')""",
            (self.STRATEGY_VERSION,),
        )
        for row in rows:
            symbol = str(row.get("symbol") or "")
            client_order_id = str(row.get("client_order_id") or "")
            instrument = self._instrument_from_db(symbol)
            order_id = str(row.get("kraken_order_id") or "")
            if instrument is None or not order_id:
                continue
            try:
                found = self.gateway.lookup_order(
                    client_order_id=client_order_id,
                    instrument=instrument,
                    kraken_order_id=order_id,
                )
                if not found:
                    continue
                payload = found[0]
                state = self.authority.reconciler.state_from_exchange(payload)
                resolved_id = str(payload.get("txid") or payload.get("order_id") or order_id)
                if state.value != str(row.get("state") or "") or resolved_id != order_id:
                    self.db.update_order_state(
                        client_order_id, state.value, kraken_order_id=resolved_id or None
                    )
                if state.value != "FILLED":
                    continue
                qty = D(str(
                    payload.get("vol_exec")
                    or payload.get("executed_volume")
                    or row.get("quantity")
                    or "0"
                ))
                price = D(str(
                    payload.get("price")
                    or payload.get("avg_price")
                    or payload.get("avgPrice")
                    or "0"
                ))
                if qty <= 0 or price <= 0:
                    continue
                try:
                    rationale = __import__("json").loads(row.get("rationale_json") or "{}")
                except (TypeError, ValueError):
                    rationale = {}
                exit_reason = str(rationale.get("tactical_exit_reason") or "")
                if exit_reason:
                    with self._lock:
                        position = self._positions.get(symbol)
                    if position is not None:
                        self._close_live(
                            position, qty, price, exit_reason, time.time(), client_order_id
                        )
                    self.audit.emit(
                        "TACTICAL_ORDER_RECOVERED", "INFO",
                        symbol=symbol, direction=str(row.get("direction") or ""),
                        action="EXIT_FILLED", quantity=str(qty), price=str(price),
                    )
                    continue
                direction = Direction(str(row.get("direction") or ""))
                with self._lock:
                    existing = self._positions.get(symbol)
                if existing is None:
                    self._restore_position(
                        instrument=instrument,
                        direction=direction,
                        quantity=qty,
                        price=price,
                        notional_eur=abs(D(str(row.get("target_notional_eur") or "0"))),
                        leverage=D(str(row.get("leverage") or "1")),
                        opened_at=float(row.get("created_at") or time.time()),
                        client_order_id=client_order_id,
                        setup_score=D(str(rationale.get("tactical_setup_score") or "0")),
                    )
                self.audit.emit(
                    "TACTICAL_ORDER_RECOVERED", "INFO",
                    symbol=symbol, direction=direction.value,
                    action="ENTRY_FILLED", quantity=str(qty), price=str(price),
                )
            except Exception as exc:
                self.audit.emit(
                    "TACTICAL_ORDER_RECONCILIATION_FAILED", "WARNING",
                    symbol=symbol, client_order_id=client_order_id,
                    error_type=type(exc).__name__, error=str(exc)[:500],
                )
        if rows:
            self.audit.emit(
                "TACTICAL_PENDING_ORDERS", "WARNING",
                count=len(rows),
                symbols=sorted({str(row.get("symbol", "")) for row in rows}),
            )

    def _restore_position(
        self, *, instrument: Instrument, direction: Direction, quantity: D, price: D,
        notional_eur: D, leverage: D, opened_at: float, client_order_id: str, setup_score: D,
    ) -> None:
        if quantity <= 0 or price <= 0:
            return
        position = TacticalPosition(
            instrument.symbol, instrument.venue, direction, quantity, price, price, price,
            abs(notional_eur), leverage, opened_at, client_order_id, setup_score,
        )
        with self._lock:
            self._positions[position.symbol] = position
        self.db.save_tactical_position(
            position.symbol, position.venue, position.direction.value, position.quantity,
            position.entry_price, position.peak_price, position.trough_price,
            position.notional_eur, position.leverage, position.opened_at,
            position.entry_client_order_id, position.setup_score, position.state,
        )

    def _signal_reject(self, symbol: str, reason: str) -> TacticalSignal | None:
        self._last_signal_reason = reason
        self._signal_rejections[reason] += 1
        return None

    @staticmethod
    def _adaptive_entry_allowed(
        expected_move: D,
        cost: D,
        confidence: D,
        enabled: bool,
        min_move: D,
        min_confidence: D,
        min_edge: D,
    ) -> bool:
        return bool(
            enabled
            and confidence >= min_confidence
            and expected_move >= min_move
            and expected_move - cost >= min_edge
        )

    @staticmethod
    def _sample_prices(
        points: tuple[tuple[float, D], ...],
        now: float,
        horizon_seconds: float,
        interval_seconds: float,
    ) -> list[tuple[float, D]]:
        if not points:
            return []
        cutoff = now - horizon_seconds
        usable = [(ts, price) for ts, price in points if cutoff <= ts <= now and price > 0]
        if not usable:
            return []
        sampled: list[tuple[float, D]] = []
        cursor = cutoff
        index = 0
        latest: tuple[float, D] | None = None
        while cursor <= now and index < len(usable):
            while index < len(usable) and usable[index][0] <= cursor:
                latest = usable[index]
                index += 1
            if latest is not None:
                sampled.append((cursor, latest[1]))
            cursor += interval_seconds
        if usable and (not sampled or sampled[-1][1] != usable[-1][1]):
            sampled.append(usable[-1])
        return sampled[-60:]

    @staticmethod
    def _return_since(points: list[tuple[float, D]], seconds: float) -> D:
        if len(points) < 2:
            return D("0")
        cutoff = points[-1][0] - seconds
        base = next((price for ts, price in reversed(points) if ts <= cutoff), points[0][1])
        last = points[-1][1]
        return (last / base - D("1")) * D("10000") if base > 0 else D("0")

    @staticmethod
    def _realized_volatility(closes: tuple[D, ...]) -> D:
        if len(closes) < 2:
            return D("0")
        returns = [
            abs(closes[i] / closes[i - 1] - D("1")) * D("10000")
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]
        return sum(returns, D("0")) / D(max(1, len(returns)))

    @staticmethod
    def _book_imbalance(
        bids: tuple[tuple[D, D], ...],
        asks: tuple[tuple[D, D], ...],
    ) -> D:
        bid_value = sum((price * qty for price, qty in bids), D("0"))
        ask_value = sum((price * qty for price, qty in asks), D("0"))
        total = bid_value + ask_value
        return (bid_value - ask_value) / total if total > 0 else D("0")

    @staticmethod
    def _score(
        momentum_60: D,
        momentum_180: D,
        volatility: D,
        volume_ratio: D,
        breakout: D,
        imbalance: D,
    ) -> D:
        return (
            abs(momentum_60) * D("0.25")
            + abs(momentum_180) * D("0.15")
            + volatility * D("0.15")
            + min(D("4"), volume_ratio) * D("35")
            + max(D("0"), breakout) * D("0.25")
            + max(D("0"), imbalance) * D("100")
        )

    @staticmethod
    def _stream_snapshot(symbol: str, state: dict[str, Any]) -> MarketSnapshot:
        return MarketSnapshot(
            symbol=symbol,
            price=D(str(state["price"])),
            bid=D(str(state["bid"])),
            ask=D(str(state["ask"])),
            volume_24h=D("1000000"),
            timestamp=float(state.get("timestamp") or time.time()),
            closes=tuple(D(str(x)) for x in state.get("closes", ())),
            depths_bid=tuple(state.get("depths_bid", ())),
            depths_ask=tuple(state.get("depths_ask", ())),
        )

    def _instrument_from_db(self, symbol: str) -> Instrument | None:
        row = self.db.one(
            "SELECT * FROM instruments WHERE symbol=? ORDER BY venue LIMIT 1",
            (symbol,),
        )
        if not row:
            return None
        try:
            leverage = tuple(
                D(str(value))
                for value in __import__("json").loads(row.get("leverage_levels") or "[]")
            )
            metadata = __import__("json").loads(row.get("metadata_json") or "{}")
            return Instrument(
                venue=str(row["venue"]),
                product_type=ProductType(str(row["product_type"])),
                symbol=str(row["symbol"]),
                instrument_id=str(row["instrument_id"]),
                altname=str(row["altname"]),
                base=str(row["base"]),
                quote=str(row["quote"]),
                status=str(row["status"]),
                margin_available=bool(row["margin_available"]),
                long_available=bool(row["long_available"]),
                short_available=bool(row["short_available"]),
                leverage_levels=leverage or (D("1"),),
                min_order_qty=D(str(row["min_order_qty"])),
                min_cost=D(str(row["min_cost"])),
                lot_decimals=int(row["lot_decimals"]),
                price_decimals=int(row["price_decimals"]),
                tick_size=D(str(row["tick_size"])),
                margin_class=str(row["margin_class"]),
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        except Exception:
            return None

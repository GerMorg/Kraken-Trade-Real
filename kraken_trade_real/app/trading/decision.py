from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.domain.models import Decision, Instrument, PortfolioState, Signal, digest_config, new_id


D=Decimal


class DecisionEngine:
    STRATEGY_VERSION="baseline-v1"

    def __init__(self, config: Any) -> None:
        self.config=config

    def choose(self, instrument: Instrument, long_signal: Signal, short_signal: Signal,
               portfolio: PortfolioState, model_version: str, config_hash: str) -> Decision | None:
        candidates=[long_signal,short_signal]
        candidates=[s for s in candidates
                    if s.net_edge_bps>=D(str(self.config.strategy_min_edge_bps))
                    and s.confidence>=D(str(self.config.strategy_min_confidence))]
        if not candidates:
            return None
        candidates.sort(key=lambda s:(s.net_edge_bps,s.confidence),reverse=True)
        signal=candidates[0]
        target=portfolio.equity_eur*D(str(self.config.risk_max_position_pct))/100
        target*=max(D("0.25"),min(D("1"),signal.confidence))
        min_cost=instrument.min_cost
        if target<min_cost:
            return None
        return Decision(
            decision_id=new_id("decision"),
            instrument=instrument,
            signal=signal,
            target_notional_eur=target,
            leverage=D("1"),
            rationale={
                "long_net_edge_bps":str(long_signal.net_edge_bps),
                "short_net_edge_bps":str(short_signal.net_edge_bps),
                "selected_direction":signal.direction.value,
                "regime":signal.regime,
                "news_effect_bps":str(signal.news_effect_bps),
                "gemini_effect_bps":str(signal.gemini_effect_bps),
            },
            strategy_version=self.STRATEGY_VERSION,
            model_version=model_version,
            config_hash=config_hash,
        )

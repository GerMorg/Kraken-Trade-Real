from decimal import Decimal
from types import SimpleNamespace
from app.domain.models import Decision,MarketSnapshot,PortfolioState,Signal
from app.domain.states import Direction
from app.execution import CostModel,ExecutionPolicy,ExecutionReconciler
from app.risk import LeverageEngine,MarginEngine,RiskEngine

def test_cost_model_includes_all_cost_buckets():
    market=SimpleNamespace(spread_bps=Decimal("10"),volume_24h=Decimal("1000"),metadata={"volatility":Decimal("3")})
    cost=CostModel().estimate(market,Decimal("10"),Decimal("2"))
    assert cost.total_bps>cost.fee_bps and cost.financing_bps>0

def test_execution_policy_fails_when_slippage_clears_edge():
    ok,reason=ExecutionPolicy(20,2).validate({"method":"market"},Decimal("10"),Decimal("20"))
    assert not ok and "SLIPPAGE" in reason

def test_margin_and_leverage_bounds():
    assert MarginEngine().validate_leverage(Decimal("5"),Decimal("10"),Decimal("5"))[0] is True
    assert MarginEngine().validate_leverage(Decimal("6"),Decimal("10"),Decimal("5"))[0] is False
    assert LeverageEngine.IMMUTABLE_MAX_LEVERAGE==Decimal("5")

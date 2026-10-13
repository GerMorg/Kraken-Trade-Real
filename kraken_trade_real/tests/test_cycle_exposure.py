from decimal import Decimal
from types import SimpleNamespace

from app.domain.models import PortfolioState
from app.risk.cycle import CycleExposureLedger


D = Decimal


def _decision(symbol, target, current=D("0")):
    return SimpleNamespace(
        instrument=SimpleNamespace(symbol=symbol),
        target_position_eur=D(target),
        current_position_eur=D(current),
    )


def test_cycle_exposure_reserves_cash_gross_net_positions_and_margin():
    base = PortfolioState(
        equity_eur=D("100"),
        cash_eur=D("80"),
        positions={"EXISTING/EUR": D("20")},
        gross_eur=D("20"),
        net_eur=D("20"),
        margin_used_eur=D("5"),
    )
    ledger = CycleExposureLedger()

    first = ledger.reserve(_decision("BTC/USD", "15"), D("1"))
    projected = ledger.project(base)
    assert first["incremental_notional_eur"] == "15"
    assert projected.positions == {
        "EXISTING/EUR": D("20"),
        "BTC/USD": D("15"),
    }
    assert projected.gross_eur == D("35")
    assert projected.net_eur == D("35")
    assert projected.cash_eur == D("65")
    assert projected.margin_used_eur == D("5")

    second = ledger.reserve(_decision("MINA/USD", "-10"), D("2"))
    projected = ledger.project(base)
    assert second["incremental_notional_eur"] == "10"
    assert projected.positions["MINA/USD"] == D("-10")
    assert projected.gross_eur == D("45")
    assert projected.net_eur == D("25")
    assert projected.cash_eur == D("60")
    assert projected.margin_used_eur == D("10")


def test_cycle_exposure_reserves_full_new_side_on_direction_change():
    ledger = CycleExposureLedger()
    ledger.reserve(_decision("BTC/USD", "-18", current="12"), D("3"))
    projected = ledger.project(PortfolioState(
        equity_eur=D("100"),
        cash_eur=D("50"),
        positions={"BTC/USD": D("12")},
        gross_eur=D("12"),
        net_eur=D("12"),
        margin_used_eur=D("4"),
    ))
    assert projected.positions["BTC/USD"] == D("-18")
    assert projected.gross_eur == D("18")
    assert projected.net_eur == D("-18")
    # Reversal reserves the full target side, not just target-current delta.
    assert projected.cash_eur == D("44")
    assert projected.margin_used_eur == D("10")



def test_cycle_projection_does_not_credit_unfilled_close_as_free_capacity():
    base = PortfolioState(
        equity_eur=D("100"), cash_eur=D("80"),
        positions={"OPEN/EUR": D("20")}, gross_eur=D("20"), net_eur=D("20"),
    )
    ledger = CycleExposureLedger()
    ledger.reserve(_decision("OPEN/EUR", "0", current="20"), D("1"))
    projected = ledger.project(base)
    assert projected.positions["OPEN/EUR"] == D("20")
    assert projected.gross_eur == D("20")
    assert len(projected.positions) == 1

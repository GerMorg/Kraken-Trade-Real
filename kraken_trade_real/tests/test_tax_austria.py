from datetime import datetime, timezone
from decimal import Decimal

from app.tax import AustrianTaxLedger


def ts(year):
    return datetime(year, 1, 2, tzinfo=timezone.utc).timestamp()


def test_asset_regime():
    assert AustrianTaxLedger.classify_asset_regime(ts(2020)) == "ALTVERSMÖGEN"
    assert AustrianTaxLedger.classify_asset_regime(ts(2024)) == "NEUVERMÖGEN"


def test_new_asset_moving_average():
    ledger = AustrianTaxLedger()
    ledger.acquire("BTC", Decimal("1"), Decimal("1000"), ts(2024))
    ledger.acquire("BTC", Decimal("1"), Decimal("3000"), ts(2024))
    gain, pool = ledger.dispose("BTC", Decimal("1"), Decimal("2500"), ts(2024))
    assert gain == Decimal("500")
    assert pool.quantity == Decimal("1")
    assert pool.cost_basis_eur == Decimal("2000")


def test_crypto_swap_carries_basis():
    ledger = AustrianTaxLedger()
    ledger.acquire("BTC", Decimal("1"), Decimal("1000"), ts(2024))
    pool = ledger.crypto_to_crypto_swap("BTC", Decimal("1"), "ETH", Decimal("10"), ts(2024))
    assert pool.asset == "ETH"
    assert pool.cost_basis_eur == Decimal("1000")


def test_2025_foreign_e1kv_mapping():
    ledger = AustrianTaxLedger(provider_tax_classification="FOREIGN")
    event = ledger.build_event(
        event_id="c1", timestamp=ts(2025), venue="kraken", product_type="SPOT",
        asset="BTC", quote_asset="EUR", event_type="DISPOSAL", quantity=Decimal("1"),
        proceeds_eur=Decimal("1000"), acquisition_cost_eur=Decimal("800"),
        realized_gain_eur=Decimal("200"),
    )
    report = ledger.summarize(2025, [event])
    prep = ledger.e1kv_preparation(2025, report)
    assert prep["values"]["crypto_gains"]["kennzahl"] == 174
    assert prep["values"]["crypto_losses"]["kennzahl"] == 176


def test_non_eur_kraken_trade_is_incomplete():
    ledger = AustrianTaxLedger()
    event = ledger.ingest_kraken_spot_trade(
        "tx1",
        {"time": ts(2026), "pair": "XBTUSD", "type": "buy", "vol": "0.01",
         "price": "60000", "cost": "600", "fee": "1"},
    )
    assert event.complete is False


def test_e1kv_keeps_gains_and_losses_separate():
    ledger = AustrianTaxLedger(provider_tax_classification="FOREIGN")
    gain = ledger.build_event(
        event_id="g", timestamp=ts(2025), venue="kraken", product_type="SPOT",
        asset="BTC", quote_asset="EUR", event_type="DISPOSAL", quantity=Decimal("1"),
        realized_gain_eur=Decimal("300"),
    )
    loss = ledger.build_event(
        event_id="l", timestamp=ts(2025), venue="kraken", product_type="SPOT",
        asset="ETH", quote_asset="EUR", event_type="DISPOSAL", quantity=Decimal("1"),
        realized_gain_eur=Decimal("-100"),
    )
    summary = ledger.summarize(2025, [gain, loss])
    assert summary["crypto_realized_gains_eur"] == "300"
    assert summary["crypto_realized_losses_eur"] == "100"
    assert summary["crypto_realized_net_result_eur"] == "200"
    prep = ledger.e1kv_preparation(2025, summary)
    assert prep["values"]["crypto_gains"]["value_eur"] == "300"
    assert prep["values"]["crypto_losses"]["value_eur"] == "100"


def test_markdown_report_tolerates_incomplete_e1kv_payload():
    report = {
        "tax_year": 2025,
        "summary": {},
        "e1kv_preparation": {
            "provider_classification": "FOREIGN",
            "values": {"crypto_gains": {}},
        },
    }
    rendered = AustrianTaxLedger._markdown_report(report)
    assert "Datenstatus" in rendered
    assert "KZ **?**" in rendered

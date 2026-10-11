from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import csv
import json
from pathlib import Path
from typing import Any, Iterable

D = Decimal
CRYPTO_REFORM_DATE = datetime(2021, 3, 1, tzinfo=timezone.utc)
CRYPTO_TAX_EFFECTIVE_DATE = datetime(2022, 3, 1, tzinfo=timezone.utc)
CRYPTO_TAX_RATE = D("0.275")
FIAT_QUOTES = {"EUR", "ZEUR", "USD", "ZUSD", "GBP", "ZGBP", "CHF", "JPY"}


def dec(value: Any, default: D = D("0")) -> D:
    try:
        return D(str(value if value not in (None, "") else default))
    except (InvalidOperation, ValueError):
        return default


def year_from_ts(ts: float) -> int:
    return datetime.fromtimestamp(ts, timezone.utc).year


@dataclass(frozen=True)
class TaxPool:
    asset: str
    regime: str
    quantity: D
    cost_basis_eur: D

    @property
    def average_cost_eur(self) -> D:
        return self.cost_basis_eur / self.quantity if self.quantity else D("0")


@dataclass(frozen=True)
class TaxEvent:
    event_id: str
    timestamp: float
    year: int
    venue: str
    product_type: str
    asset: str
    quote_asset: str
    event_type: str
    quantity: D
    proceeds_eur: D
    acquisition_cost_eur: D
    realized_gain_eur: D
    fee_eur: D
    fee_asset: str
    tax_class: str
    asset_regime: str
    tax_neutral: bool
    kest_withheld_eur: D
    foreign_tax_eur: D
    complete: bool
    source: str
    detail: dict[str, Any]


class AustrianTaxLedger:
    """Austrian income-tax bookkeeping/reporting support; not tax advice."""

    def __init__(
        self,
        db: Any | None = None,
        report_dir: str = "/config/reports/tax",
        provider_tax_classification: str = "FOREIGN",
    ) -> None:
        self.db = db
        self.report_dir = Path(report_dir)
        self.provider_tax_classification = provider_tax_classification.upper()
        self._pools: dict[tuple[str, str], TaxPool] = {}

    @staticmethod
    def classify_asset_regime(timestamp: float) -> str:
        return "NEUVERMÖGEN" if datetime.fromtimestamp(timestamp, timezone.utc) >= CRYPTO_REFORM_DATE else "ALTVERSMÖGEN"

    @staticmethod
    def crypto_to_crypto_tax_neutral() -> dict[str, str]:
        return {
            "status": "TAX_NEUTRAL",
            "basis": "§ 27b Abs. 3 Z 2 EStG 1988",
            "handling": "Anschaffungskosten auf den erhaltenen Kryptowert übertragen",
        }

    def acquire(self, asset: str, quantity: D, cost_eur: D, timestamp: float) -> TaxPool:
        if quantity <= 0 or cost_eur < 0:
            raise ValueError("invalid acquisition")
        regime = self.classify_asset_regime(timestamp)
        key = (asset.upper(), regime)
        old = self._pools.get(key, TaxPool(asset.upper(), regime, D("0"), D("0")))
        pool = TaxPool(asset.upper(), regime, old.quantity + quantity, old.cost_basis_eur + cost_eur)
        self._pools[key] = pool
        return pool

    def dispose(
        self,
        asset: str,
        quantity: D,
        proceeds_eur: D,
        timestamp: float,
        specific_cost_eur: D | None = None,
        asset_regime: str | None = None,
    ) -> tuple[D, TaxPool]:
        # The disposal timestamp does not determine whether inventory is
        # Altvermögen or Neuvermögen. Resolve against the tracked inventory;
        # if both regimes could supply this disposal, require an explicit choice.
        asset_key = asset.upper()
        allowed_regimes = ("ALTVERSMÖGEN", "NEUVERMÖGEN")
        if quantity <= 0 or proceeds_eur < 0:
            raise ValueError("invalid disposal quantity or proceeds")
        if asset_regime is not None:
            regime = str(asset_regime).upper()
            if regime not in allowed_regimes:
                raise ValueError("invalid asset regime")
            key = (asset_key, regime)
            pool = self._pools.get(key, TaxPool(asset_key, regime, D("0"), D("0")))
            if pool.quantity < quantity:
                raise ValueError("insufficient tracked quantity for selected regime")
        else:
            candidates = [
                regime for regime in allowed_regimes
                if self._pools.get(
                    (asset_key, regime), TaxPool(asset_key, regime, D("0"), D("0"))
                ).quantity >= quantity
            ]
            if len(candidates) != 1:
                raise ValueError("disposal regime is ambiguous or tracked quantity is insufficient")
            regime = candidates[0]
            key = (asset_key, regime)
            pool = self._pools[key]
        if regime == "NEUVERMÖGEN":
            cost = pool.average_cost_eur * quantity
        elif specific_cost_eur is not None:
            cost = specific_cost_eur
        else:
            raise ValueError("Altvermögen requires documented specific cost basis")
        if cost < 0 or cost > pool.cost_basis_eur:
            raise ValueError("specific cost basis is outside tracked inventory cost")
        gain = proceeds_eur - cost
        remaining = TaxPool(pool.asset, pool.regime, pool.quantity - quantity, pool.cost_basis_eur - cost)
        self._pools[key] = remaining
        return gain, remaining

    def crypto_to_crypto_swap(
        self,
        given_asset: str,
        given_quantity: D,
        received_asset: str,
        received_quantity: D,
        timestamp: float,
        asset_regime: str | None = None,
    ) -> TaxPool:
        asset_key = given_asset.upper()
        allowed_regimes = ("ALTVERSMÖGEN", "NEUVERMÖGEN")
        if asset_regime is None:
            candidates = [
                regime for regime in allowed_regimes
                if self._pools.get(
                    (asset_key, regime), TaxPool(asset_key, regime, D("0"), D("0"))
                ).quantity >= given_quantity
            ]
            if len(candidates) != 1:
                raise ValueError("swap regime is ambiguous or tracked quantity is insufficient")
            regime = candidates[0]
        else:
            regime = str(asset_regime).upper()
            if regime not in allowed_regimes:
                raise ValueError("invalid asset regime")
        if regime != "NEUVERMÖGEN":
            raise ValueError("Altvermögen crypto-to-crypto swaps require tax review")
        if datetime.fromtimestamp(timestamp, timezone.utc) < CRYPTO_TAX_EFFECTIVE_DATE:
            raise ValueError("pre-2022 crypto-to-crypto swaps require tax review")
        key = (asset_key, regime)
        pool = self._pools.get(key, TaxPool(asset_key, regime, D("0"), D("0")))
        if given_quantity <= 0 or received_quantity <= 0 or pool.quantity < given_quantity:
            raise ValueError("insufficient tracked quantity or invalid swap quantity")
        carried_cost = pool.average_cost_eur * given_quantity
        self._pools[key] = TaxPool(
            pool.asset, pool.regime, pool.quantity - given_quantity, pool.cost_basis_eur - carried_cost
        )
        return self.acquire(received_asset, received_quantity, carried_cost, timestamp)

    def build_event(
        self,
        *,
        event_id: str,
        timestamp: float,
        venue: str,
        product_type: str,
        asset: str,
        quote_asset: str,
        event_type: str,
        quantity: D,
        proceeds_eur: D = D("0"),
        acquisition_cost_eur: D = D("0"),
        realized_gain_eur: D = D("0"),
        fee_eur: D = D("0"),
        fee_asset: str = "EUR",
        tax_class: str = "CRYPTO_27_5",
        kest_withheld_eur: D = D("0"),
        foreign_tax_eur: D = D("0"),
        complete: bool = True,
        source: str = "kraken",
        tax_neutral: bool = False,
        detail: dict[str, Any] | None = None,
        asset_regime: str | None = None,
    ) -> TaxEvent:
        return TaxEvent(
            event_id, timestamp, year_from_ts(timestamp), venue, product_type, asset, quote_asset,
            event_type, quantity, proceeds_eur, acquisition_cost_eur, realized_gain_eur, fee_eur,
            fee_asset, tax_class, asset_regime or self.classify_asset_regime(timestamp), tax_neutral,
            kest_withheld_eur, foreign_tax_eur, complete, source, detail or {},
        )

    def record(self, event: TaxEvent) -> None:
        if self.db is not None:
            self.db.record_tax_event(event)

    def sync_kraken_spot_history(self, gateway: Any, max_pages: int = 200) -> int:
        if self.db is None or not gateway.api_key:
            return 0
        try:
            all_rows: list[tuple[str, dict[str, Any]]] = []
            offset = 0
            for _ in range(max_pages):
                payload = gateway.spot_trades_history({"ofs": offset})
                trades = payload.get("trades", {}) if isinstance(payload, dict) else {}
                rows = [(str(k), v) for k, v in trades.items() if isinstance(v, dict)]
                all_rows.extend(rows)
                if len(rows) < 50:
                    break
                offset += 50
            all_rows.sort(key=lambda item: float(dec(item[1].get("time"))))
            self._pools.clear()
            self.db.execute("DELETE FROM tax_events WHERE source='kraken_trades_history'")
        except Exception as exc:
            self.db.event("TAX_HISTORY_FETCH_FAILED", "WARNING", {"error": type(exc).__name__})
            return 0

        for tx_id, raw in all_rows:
            self.record(self.ingest_kraken_spot_trade(tx_id, raw))
        return len(all_rows)

    def ingest_kraken_spot_trade(
        self, tx_id: str, raw: dict[str, Any], unit_price_eur: D | None = None
    ) -> TaxEvent:
        timestamp = float(dec(raw.get("time")))
        pair = str(raw.get("pair") or "")
        side = str(raw.get("type") or "").upper()
        qty = dec(raw.get("vol"))
        price = dec(raw.get("price"))
        cost = dec(raw.get("cost"))
        fee = dec(raw.get("fee"))
        base, quote = self._split_pair(pair)
        if self._is_margin_trade(raw):
            return self.build_event(
                event_id=tx_id, timestamp=timestamp, venue="kraken", product_type="SPOT_MARGIN",
                asset=base, quote_asset=quote, event_type="MARGIN_TRADE_REVIEW", quantity=qty,
                fee_eur=fee if quote in {"EUR", "ZEUR"} else D("0"), fee_asset=quote,
                tax_class="CRYPTO_MARGIN_REVIEW", complete=False, source="kraken_trades_history",
                detail={"pair": pair, "raw_cost": str(cost), "manual_review": True},
            )

        is_crypto_quote = quote not in FIAT_QUOTES
        if is_crypto_quote and side in {"BUY", "SELL"}:
            # Crypto-to-crypto tax neutrality applies to qualifying Neuvermögen
            # from 1 March 2022. The traded-away asset's acquisition regime must
            # be known; never silently treat an Altvermögen disposal as neutral.
            sold_asset = quote if side == "BUY" else base
            sold_quantity = cost if side == "BUY" else qty
            alt_pool = self._pools.get(
                (sold_asset.upper(), "ALTVERSMÖGEN"),
                TaxPool(sold_asset.upper(), "ALTVERSMÖGEN", D("0"), D("0")),
            )
            if alt_pool.quantity > 0:
                received_asset = base if side == "BUY" else quote
                return self.build_event(
                    event_id=tx_id, timestamp=timestamp, venue="kraken", product_type="SPOT",
                    asset=sold_asset, quote_asset=received_asset,
                    event_type="ALT_CRYPTO_SWAP_REVIEW", quantity=sold_quantity,
                    tax_class="CRYPTO_ALT_REVIEW", tax_neutral=False, complete=False,
                    source="kraken_trades_history",
                    asset_regime="ALTVERSMÖGEN",
                    detail={
                        "pair": pair, "sold_asset": sold_asset,
                        "received_asset": received_asset,
                        "manual_review": "Altvermögen crypto-to-crypto disposition needs separate Austrian tax treatment; no gain or carried basis was fabricated.",
                    },
                )
            if datetime.fromtimestamp(timestamp, timezone.utc) < CRYPTO_TAX_EFFECTIVE_DATE:
                return self.build_event(
                    event_id=tx_id, timestamp=timestamp, venue="kraken", product_type="SPOT",
                    asset=base, quote_asset=quote, event_type="CRYPTO_SWAP_REVIEW",
                    quantity=qty, tax_class="CRYPTO_REVIEW", tax_neutral=False, complete=False,
                    source="kraken_trades_history",
                    asset_regime=self.classify_asset_regime(timestamp),
                    detail={
                        "pair": pair,
                        "manual_review": "Crypto-to-crypto neutral rollover is only recognized by this ledger from 1 March 2022; historical tax regime requires review.",
                    },
                )
            if side == "BUY":
                pool = self._pools.get((quote, self.classify_asset_regime(timestamp)))
                if pool and pool.quantity >= cost:
                    received = self.crypto_to_crypto_swap(quote, cost, base, qty, timestamp)
                    return self.build_event(
                        event_id=tx_id, timestamp=timestamp, venue="kraken", product_type="SPOT",
                        asset=base, quote_asset=quote, event_type="CRYPTO_SWAP_IN", quantity=qty,
                        acquisition_cost_eur=received.cost_basis_eur, tax_neutral=True,
                        complete=True, source="kraken_trades_history", detail={"pair": pair},
                    )
            else:
                pool = self._pools.get((base, self.classify_asset_regime(timestamp)))
                if pool and pool.quantity >= qty:
                    carried = pool.average_cost_eur * qty
                    self.crypto_to_crypto_swap(base, qty, quote, cost, timestamp)
                    return self.build_event(
                        event_id=tx_id, timestamp=timestamp, venue="kraken", product_type="SPOT",
                        asset=base, quote_asset=quote, event_type="CRYPTO_SWAP_OUT", quantity=qty,
                        acquisition_cost_eur=carried, tax_neutral=True, complete=True,
                        source="kraken_trades_history", detail={"pair": pair},
                    )
            return self.build_event(
                event_id=tx_id, timestamp=timestamp, venue="kraken", product_type="SPOT",
                asset=base, quote_asset=quote, event_type="CRYPTO_SWAP_REVIEW", quantity=qty,
                tax_class="CRYPTO_REVIEW", tax_neutral=False, complete=False,
                source="kraken_trades_history", asset_regime="REGIME_UNRESOLVED",
                detail={
                    "pair": pair,
                    "manual_review": "cost basis or asset regime unavailable; tax-neutral treatment not asserted",
                },
            )

        price_eur = unit_price_eur if unit_price_eur is not None else (price if quote in {"EUR", "ZEUR"} else D("0"))
        complete = qty > 0 and price_eur > 0
        acquisition = qty * price_eur if side == "BUY" and complete else D("0")
        proceeds = qty * price_eur if side == "SELL" and complete else D("0")
        gain = D("0")
        event_type = "OTHER"
        tax_class = "CRYPTO_27_5"
        event_regime: str | None = None
        detail = {"pair": pair, "price": str(price), "cost_original": str(cost)}
        if side == "BUY" and complete:
            self.acquire(base, qty, acquisition, timestamp)
            event_type = "ACQUISITION"
        elif side == "SELL" and complete:
            alt_pool = self._pools.get(
                (base.upper(), "ALTVERSMÖGEN"),
                TaxPool(base.upper(), "ALTVERSMÖGEN", D("0"), D("0")),
            )
            new_pool = self._pools.get(
                (base.upper(), "NEUVERMÖGEN"),
                TaxPool(base.upper(), "NEUVERMÖGEN", D("0"), D("0")),
            )
            if alt_pool.quantity > 0:
                event_type = "DISPOSAL_REGIME_REVIEW" if new_pool.quantity > 0 else "ALT_DISPOSAL_REVIEW"
                tax_class = "CRYPTO_ALT_REVIEW"
                event_regime = "ALT_NEU_UNCLEAR" if new_pool.quantity > 0 else "ALTVERSMÖGEN"
                complete = False
                detail.update({
                    "alt_inventory_quantity": str(alt_pool.quantity),
                    "new_inventory_quantity": str(new_pool.quantity),
                    "manual_review": "Altvermögen is still tracked for this asset; the inventory regime/choice and legacy tax treatment must be reviewed before any gain is reported.",
                })
            else:
                try:
                    gain, _ = self.dispose(base, qty, proceeds, timestamp)
                    event_type = "DISPOSAL"
                except ValueError:
                    complete = False
                    event_type = "DISPOSAL_BASIS_REVIEW"
                    tax_class = "CRYPTO_REVIEW"
                    event_regime = "REGIME_UNRESOLVED"
                    detail["manual_review"] = "No unique, sufficient Neuvermögen cost basis could be established."
        return self.build_event(
            event_id=tx_id, timestamp=timestamp, venue="kraken", product_type="SPOT",
            asset=base, quote_asset=quote, event_type=event_type, quantity=qty,
            proceeds_eur=proceeds, acquisition_cost_eur=acquisition, realized_gain_eur=gain,
            fee_eur=fee if quote in {"EUR", "ZEUR"} else D("0"), fee_asset=quote,
            tax_class=tax_class, asset_regime=event_regime, complete=complete, source="kraken_trades_history",
            detail=detail,
        )

    def events(self) -> list[TaxEvent]:
        return self.db.tax_events() if self.db is not None else []

    def summarize(self, year: int, events: Iterable[TaxEvent]) -> dict[str, Any]:
        rows = [e for e in events if e.year == year]
        crypto = [e for e in rows if e.tax_class.startswith("CRYPTO")]
        positive_gains = sum((max(D("0"), e.realized_gain_eur) for e in crypto), D("0"))
        negative_losses = sum((max(D("0"), -e.realized_gain_eur) for e in crypto), D("0"))
        crypto_result = positive_gains - negative_losses
        current_income = sum(
            (e.realized_gain_eur for e in rows if e.tax_class == "CRYPTO_CURRENT_INCOME"), D("0")
        )
        derivative = sum(
            (e.realized_gain_eur for e in rows if e.tax_class == "DERIVATIVE_27_5"), D("0")
        )
        kest = sum((e.kest_withheld_eur for e in rows), D("0"))
        foreign_tax = sum((e.foreign_tax_eur for e in rows), D("0"))
        incomplete = [e.event_id for e in rows if not e.complete]
        indicative_base = max(D("0"), crypto_result + current_income)
        tax = indicative_base * CRYPTO_TAX_RATE
        return {
            "tax_year": year,
            "provider_tax_classification": self.provider_tax_classification,
            "crypto_realized_gains_eur": str(positive_gains),
            "crypto_realized_losses_eur": str(negative_losses),
            "crypto_realized_net_result_eur": str(crypto_result),
            "crypto_current_income_eur": str(current_income),
            "derivative_result_eur": str(derivative),
            "kest_withheld_eur": str(kest),
            "foreign_tax_eur": str(foreign_tax),
            "indicative_crypto_27_5_tax_eur": str(tax),
            "indicative_crypto_balance_after_kest_eur": str(tax - kest),
            "event_count": len(rows),
            "incomplete_event_count": len(incomplete),
            "status": "INCOMPLETE_DATA" if incomplete else "READY_FOR_REVIEW",
        }

    def e1kv_preparation(self, year: int, summary: dict[str, Any]) -> dict[str, Any]:
        if year != 2025:
            return {
                "tax_year": year,
                "form": "E1 + E1kv",
                "official_form_mapping_available": False,
                "values": {},
                "note": "Für spätere Jahre die aktuelle BMF-E1kv-Fassung verwenden.",
            }
        foreign = self.provider_tax_classification == "FOREIGN"
        return {
            "tax_year": year,
            "form": "E1 + E1kv",
            "official_form_mapping_available": True,
            "provider_classification": self.provider_tax_classification,
            "values": {
                "crypto_current_income": {"kennzahl": 172 if foreign else 171, "value_eur": summary.get("crypto_current_income_eur","0")},
                "crypto_gains": {"kennzahl": 174 if foreign else 173, "value_eur": summary.get("crypto_realized_gains_eur","0")},
                "crypto_losses": {"kennzahl": 176 if foreign else 175, "value_eur": summary.get("crypto_realized_losses_eur","0")},
                "withheld_kest": {"kennzahl": 899, "value_eur": summary.get("kest_withheld_eur","0")},
            },
        }

    def build_report(self, year: int) -> dict[str, Any]:
        events = self.events()
        summary = self.summarize(year, events)
        return {
            "report_version": "AT-2026.2",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "tax_year": year,
            "rules": {
                "crypto_special_rate": "27.5%",
                "crypto_reform_date": "2021-03-01",
                "crypto_to_crypto_swap": "tax_neutral_if_requirements_met",
                "new_asset_cost_method": "moving_average",
            },
            "summary": summary,
            "e1kv_preparation": self.e1kv_preparation(year, summary),
            "data_quality": {
                "complete": summary["incomplete_event_count"] == 0,
                "requires_manual_review": summary["incomplete_event_count"] > 0,
                "warnings": [
                    "Provider-/KESt-Klassifikation anhand der konkreten Unterlagen prüfen.",
                    "Derivate/Futures separat klassifizieren.",
                    "Nicht-EUR-Transaktionen benötigen belastbare EUR-Bewertung.",
                    "Bericht ersetzt keine Steuerberatung und keine Prüfung durch FinanzOnline/BMF.",
                ],
            },
        }

    def write_report(self, year: int) -> dict[str, str]:
        report = self.build_report(year)
        events = [event for event in self.events() if event.year == year]
        target = self.report_dir / "AT" / str(year)
        target.mkdir(parents=True, exist_ok=True)
        json_path = target / "income-tax-report.json"
        csv_path = target / "tax-events.csv"
        md_path = target / "income-tax-information.md"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "event_id","timestamp","tax_year","venue","product_type","asset","quote_asset","event_type",
                "quantity","proceeds_eur","acquisition_cost_eur","realized_gain_eur","fee_eur","fee_asset",
                "tax_class","asset_regime","tax_neutral","kest_withheld_eur","foreign_tax_eur","complete","source",
            ])
            for e in events:
                writer.writerow([
                    e.event_id,e.timestamp,e.year,e.venue,e.product_type,e.asset,e.quote_asset,e.event_type,
                    e.quantity,e.proceeds_eur,e.acquisition_cost_eur,e.realized_gain_eur,e.fee_eur,e.fee_asset,
                    e.tax_class,e.asset_regime,e.tax_neutral,e.kest_withheld_eur,e.foreign_tax_eur,e.complete,e.source,
                ])
        md_path.write_text(self._markdown_report(report), encoding="utf-8")
        return {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path)}

    @staticmethod
    def _markdown_report(report: dict[str, Any]) -> str:
        s = report["summary"]
        p = report["e1kv_preparation"]
        lines = [
            f"# Österreich Einkommensteuer – Krypto/Trading {report['tax_year']}",
            "",
            f"- Krypto realisierte Gewinne: **{s.get('crypto_realized_gains_eur','0')} EUR**",
            f"- Krypto realisierte Verluste: **{s.get('crypto_realized_losses_eur','0')} EUR**",
            f"- Krypto Nettoergebnis: **{s.get('crypto_realized_net_result_eur','0')} EUR**",
            f"- Laufende Krypto-Einkünfte: **{s.get('crypto_current_income_eur','0')} EUR**",
            f"- Derivate-Ergebnis: **{s.get('derivative_result_eur','0')} EUR**",
            f"- Einbehaltene KESt: **{s.get('kest_withheld_eur','0')} EUR**",
            f"- Indikativer 27,5-%-Betrag: **{s.get('indicative_crypto_27_5_tax_eur','0')} EUR**",
            f"- Datenstatus: **{s.get('status','UNKNOWN')}**",
            "",
            "## E1/E1kv Vorbereitung",
            f"- Providerklassifikation: **{p.get('provider_classification', 'UNVERIFIED')}**",
        ]
        for name, value in p.get("values", {}).items():
            if not isinstance(value, dict):
                continue
            lines.append(
                f"- {name}: KZ **{value.get('kennzahl','?')}** → **{value.get('value_eur','0')} EUR**"
            )
        return "\\n".join(lines) + "\\n"

    @staticmethod
    def _split_pair(pair: str) -> tuple[str, str]:
        p = pair.replace("_", "/").upper()
        if "/" in p:
            base, quote = p.split("/", 1)
            return base, quote
        for quote in ("ZEUR","ZUSD","ZGBP","EUR","USD","GBP","CHF","JPY","USDT","USDC","XBT"):
            if p.endswith(quote) and len(p) > len(quote):
                return p[:-len(quote)], quote
        return p, "UNKNOWN"

    @staticmethod
    def _is_margin_trade(raw: dict[str, Any]) -> bool:
        return str(raw.get("margin", "0")).lower() not in {"0", "false", "", "none"}

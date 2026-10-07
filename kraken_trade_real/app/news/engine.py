from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from decimal import Decimal
from email.utils import parsedate_to_datetime
import hashlib
import re
import time
from typing import Any
from urllib.request import Request, urlopen

from defusedxml import ElementTree as ET

from app.domain.models import NewsItem

D = Decimal
SOURCES = (
    ("Kraken Blog", "https://blog.kraken.com/feed"),
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("ECB", "https://www.ecb.europa.eu/rss/press.html"),
)


@dataclass(frozen=True)
class RawNews:
    source: str
    url: str
    title: str
    summary: str
    published_at: float


class NewsEngine:
    POS = {
        "approval", "approved", "growth", "surge", "rally", "adoption",
        "partnership", "launch", "profit", "record", "easing",
    }
    NEG = {
        "hack", "fraud", "ban", "banned", "lawsuit", "crisis", "collapse",
        "sanction", "war", "recession", "loss", "liquidation", "outage",
        "investigation",
    }
    ALIASES = {
        "BTC": {"BTC", "BITCOIN", "XBT"},
        "ETH": {"ETH", "ETHEREUM"},
        "SOL": {"SOL", "SOLANA"},
        "XRP": {"XRP", "RIPPLE"},
        "DOGE": {"DOGE", "DOGECOIN"},
        "ADA": {"ADA", "CARDANO"},
    }

    def __init__(
        self,
        db: Any,
        refresh_minutes: int = 10,
        source_timeout_seconds: int = 10,
    ) -> None:
        self.db = db
        self.refresh_seconds = max(60, refresh_minutes * 60)
        self.source_timeout_seconds = max(3, min(30, source_timeout_seconds))
        self._cache: list[NewsItem] = []
        self._last_fetch = 0.0
        self.last_status: dict[str, Any] = {
            "cached": False,
            "duration_seconds": 0.0,
            "sources_ok": 0,
            "sources_failed": 0,
            "sources_timeout": 0,
        }

    def collect(self, limit_per_source: int = 30) -> list[NewsItem]:
        now = time.monotonic()
        if self._cache and now - self._last_fetch < self.refresh_seconds:
            self.last_status = {
                "cached": True,
                "duration_seconds": 0.0,
                "sources_ok": 0,
                "sources_failed": 0,
                "sources_timeout": 0,
            }
            self.db.event(
                "NEWS_CACHE_HIT",
                "INFO",
                {"count": len(self._cache)},
            )
            return list(self._cache)

        started = time.monotonic()
        out: list[NewsItem] = []
        sources_ok = 0
        sources_failed = 0
        sources_timeout = 0
        executor = ThreadPoolExecutor(max_workers=len(SOURCES))
        futures = {
            executor.submit(self._fetch, source, url, limit_per_source): (source, url)
            for source, url in SOURCES
        }
        done, pending = wait(
            futures,
            timeout=self.source_timeout_seconds,
        )
        for future in done:
            source, url = futures[future]
            try:
                rows = future.result()
                out.extend(self._classify(x) for x in rows)
                sources_ok += 1
                self.db.event(
                    "NEWS_SOURCE_RESULT",
                    "INFO",
                    {"source": source, "count": len(rows)},
                )
            except Exception as exc:
                sources_failed += 1
                self.db.event(
                    "NEWS_FETCH_FAILED",
                    "WARNING",
                    {
                        "source": source,
                        "error": type(exc).__name__,
                    },
                )
        for future in pending:
            source, _ = futures[future]
            sources_timeout += 1
            self.db.event(
                "NEWS_SOURCE_TIMEOUT",
                "WARNING",
                {"source": source, "timeout_seconds": self.source_timeout_seconds},
            )
        executor.shutdown(wait=False, cancel_futures=True)

        result = sorted(out, key=lambda x: x.published_at, reverse=True)
        if result:
            for item in result:
                self.db.save_news(item)
            self._cache = result
            self._last_fetch = time.monotonic()
        elif self._cache:
            result = list(self._cache)

        self.last_status = {
            "cached": False,
            "duration_seconds": round(time.monotonic() - started, 2),
            "sources_ok": sources_ok,
            "sources_failed": sources_failed,
            "sources_timeout": sources_timeout,
        }
        self.db.event(
            "NEWS_COLLECTION_COMPLETED",
            "INFO",
            {
                "count": len(result),
                **self.last_status,
            },
        )
        return result

    def _fetch(self, source: str, url: str, limit: int) -> list[RawNews]:
        with urlopen(
            Request(
                url,
                headers={"User-Agent": "Kraken-Trade-Real/0.1.27"},
            ),
            timeout=self.source_timeout_seconds,
        ) as resp:  # nosec B310
            root = ET.fromstring(resp.read())
        out: list[RawNews] = []
        for n in root.findall(".//item")[:limit]:
            title = (n.findtext("title") or "").strip()
            link = (n.findtext("link") or url).strip()
            summary = (n.findtext("description") or "").strip()
            raw_date = (n.findtext("pubDate") or "").strip()
            try:
                published = parsedate_to_datetime(raw_date).timestamp()
            except (TypeError, ValueError, OverflowError):
                published = time.time()
            if title:
                out.append(
                    RawNews(
                        source,
                        link,
                        title,
                        re.sub("<[^>]+>", " ", summary),
                        published,
                    )
                )
        return out

    def _classify(self, r: RawNews) -> NewsItem:
        text = r.title + " " + r.summary
        low = text.lower()
        pos = sum(
            bool(re.search(r"\b" + re.escape(w) + r"\b", low))
            for w in self.POS
        )
        neg = sum(
            bool(re.search(r"\b" + re.escape(w) + r"\b", low))
            for w in self.NEG
        )
        direction = (
            "POSITIVE" if pos > neg else
            "NEGATIVE" if neg > pos else
            "NEUTRAL"
        )
        impact = D(min(80, abs(pos - neg) * 10))
        if direction == "NEGATIVE":
            impact = -impact
        assets = tuple(
            asset
            for asset, aliases in self.ALIASES.items()
            if any(
                re.search(r"\b" + re.escape(alias) + r"\b", text.upper())
                for alias in aliases
            )
        )
        digest = hashlib.sha256(
            f"{r.source}|{r.title}|{r.url}".encode()
        ).hexdigest()
        return NewsItem(
            digest[:32],
            r.source,
            r.url,
            r.title,
            r.summary,
            r.published_at,
            ("macro",),
            assets,
            direction,
            impact,
            D(".7"),
            D(".8"),
            "24h",
            False,
            digest,
        )

    def effect_for(self, symbol: str, items: list[NewsItem]) -> D:
        base = symbol.split("/", 1)[0].upper()
        base = {"XBT": "BTC", "XXBT": "BTC"}.get(base, base)
        result = D(0)
        now = time.time()
        for item in items:
            if item.affected_assets and base not in item.affected_assets:
                continue
            age = (now - item.published_at) / 3600
            if 0 <= age < 48:
                result += (
                    item.impact_bps
                    * item.credibility
                    * item.novelty
                    * (D(1) - D(str(age)) / D(48))
                )
        return max(D(-100), min(D(100), result))

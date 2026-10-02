from __future__ import annotations

import asyncio
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import aiohttp

from .models import NewsEvent


DEFAULT_ENTITY_WORDS = {
    "BTC": ("bitcoin", "btc", "xbt"),
    "ETH": ("ethereum", "eth"),
    "XRP": ("xrp",),
    "SOL": ("solana", "sol"),
}


class NewsEngine:
    def __init__(self, urls: list[str], timeout: float = 10.0) -> None:
        self.urls = tuple(urls)
        self.timeout = timeout
        self.last_fetch_ok = False
        self.seen: set[str] = set()
        self.entity_words: dict[str, tuple[str, ...]] = dict(DEFAULT_ENTITY_WORDS)

    def set_assets(self, assets: set[str]) -> None:
        words = dict(DEFAULT_ENTITY_WORDS)
        for asset in assets:
            normalized = asset.upper()
            if not normalized:
                continue
            words.setdefault(normalized, (normalized.lower(),))
        self.entity_words = words

    async def fetch(self) -> list[NewsEvent]:
        if not self.urls:
            self.last_fetch_ok = False
            return []
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        events: list[NewsEvent] = []
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                results = await asyncio.gather(*(self._fetch_one(session, u) for u in self.urls), return_exceptions=True)
            for result in results:
                if isinstance(result, list):
                    events.extend(result)
            self.last_fetch_ok = bool(events) or bool(self.urls)
            return events
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self.last_fetch_ok = False
            return []

    async def _fetch_one(self, session: aiohttp.ClientSession, url: str) -> list[NewsEvent]:
        async with session.get(url, headers={"User-Agent": "KrakenAITrader/1.0"}) as response:
            response.raise_for_status()
            text = await response.text()
        root = ET.fromstring(text)
        events: list[NewsEvent] = []
        for item in root.findall(".//item")[:40]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            published = item.findtext("pubDate") or item.findtext("published") or ""
            if not title or not link:
                continue
            key = hashlib.sha256(f"{url}|{title}|{link}".encode()).hexdigest()
            if key in self.seen:
                continue
            self.seen.add(key)
            try:
                published_at = parsedate_to_datetime(published).timestamp() if published else time.time()
            except (TypeError, ValueError, OverflowError):
                published_at = time.time()
            asset = self._entity(title)
            events.append(self._classify(url, link, title, published_at, asset))
        return events

    def _entity(self, title: str) -> str | None:
        lower = title.lower()
        for asset, words in self.entity_words.items():
            if any(re.search(rf"(?<![A-Za-z0-9]){re.escape(word.lower())}(?![A-Za-z0-9])", lower) for word in words):
                return asset
        return None

    def _classify(self, source_url: str, link: str, title: str, published_at: float, asset: str | None) -> NewsEvent:
        lower = title.lower()
        bullish = sum(w in lower for w in ("approve", "approval", "launch", "adoption", "inflow", "bullish", "record"))
        bearish = sum(w in lower for w in ("ban", "hack", "lawsuit", "outflow", "bearish", "crash", "liquidation"))
        direction = "bullish" if bullish > bearish else "bearish" if bearish > bullish else "neutral"
        impact = min(Decimal("1"), Decimal(abs(bullish - bearish)) / Decimal("4"))
        return NewsEvent(urlparse(source_url).netloc, link, title, published_at, asset, "headline_event", direction, impact, Decimal("0.6"), Decimal("0.8"), "intraday", Decimal("0"))

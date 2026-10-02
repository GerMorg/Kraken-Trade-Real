from __future__ import annotations

import asyncio
import json
from decimal import Decimal, InvalidOperation

import aiohttp

from .models import GeminiAnalysis, NewsEvent


SCHEMA = {
    "type": "object",
    "properties": {
        "asset": {"type": "string"},
        "event": {"type": "string"},
        "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "impact": {"type": "number"},
        "confidence": {"type": "number"},
        "time_horizon": {"type": "string"},
        "novelty": {"type": "number"},
        "market_confirmation": {"type": "number"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["asset", "event", "direction", "impact", "confidence", "time_horizon", "novelty", "market_confirmation", "risk_flags"],
}


class GeminiClient:
    def __init__(self, api_key: str | None, model: str = "gemini-3.8-flash", timeout: float = 15.0) -> None:
        self.api_key = api_key or ""
        self.model = model
        self.timeout = timeout
        self.healthy = False

    async def analyze(self, event: NewsEvent) -> GeminiAnalysis | None:
        if not self.api_key:
            self.healthy = False
            return None
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        prompt = {
            "headline": event.title,
            "asset": event.asset,
            "event": event.event,
            "source": event.source,
            "already_classified": {
                "direction": event.direction,
                "impact": float(event.impact),
                "novelty": float(event.novelty),
            },
        }
        body = {
            "contents": [{"parts": [{"text": json.dumps(prompt, ensure_ascii=False)}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": SCHEMA,
                "temperature": 0.0,
            },
        }
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, params={"key": self.api_key}, json=body) as response:
                    response.raise_for_status()
                    data = await response.json()
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw)
            result = self._validate(parsed)
            self.healthy = result is not None
            return result
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, json.JSONDecodeError):
            self.healthy = False
            return None

    def _validate(self, value: dict) -> GeminiAnalysis | None:
        try:
            direction = str(value.get("direction", "neutral"))
            if direction not in {"bullish", "bearish", "neutral"}:
                return None
            nums = [Decimal(str(value.get(k, 0))) for k in ("impact", "confidence", "novelty", "market_confirmation")]
            if any(n < 0 or n > 1 for n in nums):
                return None
            return GeminiAnalysis(
                asset=str(value.get("asset") or "") or None,
                event=str(value.get("event") or "unknown"),
                direction=direction,
                impact=nums[0],
                confidence=nums[1],
                time_horizon=str(value.get("time_horizon") or "intraday"),
                novelty=nums[2],
                market_confirmation=nums[3],
                risk_flags=tuple(str(x) for x in value.get("risk_flags", []))[:10],
            )
        except (InvalidOperation, TypeError, ValueError):
            return None

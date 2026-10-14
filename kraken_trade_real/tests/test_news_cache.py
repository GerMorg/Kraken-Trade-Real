from app.domain.models import NewsItem
from decimal import Decimal as D
from app.news.engine import NewsEngine, RawNews


def test_news_refresh_cache_prevents_repeat_fetch(db):
    engine = NewsEngine(db, refresh_minutes=10, source_timeout_seconds=3)
    calls = []

    def fake_fetch(source, url, limit):
        calls.append(source)
        return [RawNews(source, url, f"title {source}", "growth", 1_800_000_000)]

    engine._fetch = fake_fetch
    first = engine.collect()
    second = engine.collect()

    assert len(first) == 3
    assert second == first
    assert len(calls) == 3
    assert engine.last_status["cached"] is True



def test_news_collection_deduplicates_cross_source_items_before_effects_are_summed(db):
    engine = NewsEngine(db, refresh_minutes=10, source_timeout_seconds=3)
    now = 1_800_000_000.0

    def fake_fetch(source, url, limit):
        if source == "Kraken Blog":
            return [RawNews(
                source, "https://news.example/story?utm_source=kraken",
                "Exchange outage resolved", "growth adoption", now,
            )]
        if source == "Federal Reserve":
            return [RawNews(
                source, "https://news.example/story?fbclid=tracking",
                "Exchange outage resolved", "growth adoption", now - 60,
            )]
        return [RawNews(
            source, "https://other.example/story", "Different policy news",
            "growth", now - 30,
        )]

    engine._fetch = fake_fetch
    result = engine.collect()
    assert len(result) == 2
    assert result[0].title == "Exchange outage resolved"
    assert result[0].source == "Kraken Blog"
    assert any(item.title == "Different policy news" for item in result)
    assert db.one("SELECT COUNT(*) AS n FROM events WHERE code=?", ("NEWS_DUPLICATES_REMOVED",))["n"] >= 1


def test_news_deduplication_keeps_same_title_more_than_24_hours_apart():
    first = NewsItem(
        "one", "Source A", "https://news.example/story", "Same title", "summary",
        1_700_000_000, ("macro",), (), "NEUTRAL", D("0"), D(".7"), D(".8"),
        "24h", False, "hash-one",
    )
    second = NewsItem(
        "two", "Source B", "https://another.example/story", "Same title", "summary",
        1_700_100_000, ("macro",), (), "NEUTRAL", D("0"), D(".7"), D(".8"),
        "24h", False, "hash-two",
    )
    kept, duplicates = NewsEngine._deduplicate_items([first, second])
    assert len(kept) == 2
    assert duplicates == 0

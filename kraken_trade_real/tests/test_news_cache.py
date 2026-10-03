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

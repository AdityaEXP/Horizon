"""Tests for the Codeforces blog scraper (no network)."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from src.models import CodeforcesConfig, SourceType
from src.scrapers import codeforces as cf


def _ts(hours_ago: float) -> int:
    return int((datetime.now(timezone.utc) - timedelta(hours=hours_ago)).timestamp())


ENTRIES = {
    1: {"title": "<p>A practical guide to segment tree beats</p>", "rating": 150, "created": 30},
    2: {"title": "Codeforces Round 1130 (Div. 2)", "rating": 300, "created": 30},
    3: {"title": "Appeal against the account foo", "rating": 140, "created": 30},
    4: {"title": "How I prepared for Google OA", "rating": 5, "created": 30},  # low rating
    5: {"title": "Too fresh tutorial on DP", "rating": 90, "created": 3},  # next run's window
    6: {"title": "Old tutorial on Fenwick trees", "rating": 200, "created": 24 * 30},  # too old
    7: {"title": "Русский пост", "rating": 120, "created": 30, "locale": "ru"},
    8: {"title": "Finally Expert!", "rating": 60, "created": 30},
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/recentActions"):
        actions = []
        for eid, e in ENTRIES.items():
            entry = {
                "id": eid,
                "title": e["title"],
                "rating": e["rating"],
                "creationTimeSeconds": _ts(e["created"]),
                "originalLocale": e.get("locale", "en"),
                "authorHandle": "author",
            }
            actions += [{"blogEntry": entry}, {"blogEntry": entry}]  # duplicates
        return httpx.Response(200, json={"status": "OK", "result": actions})
    if path.endswith("/blogEntry.view"):
        eid = int(request.url.params["blogEntryId"])
        return httpx.Response(200, json={"status": "OK", "result": {
            "id": eid, "content": "<p>Body <b>text</b></p>", "tags": ["data structures"]}})
    if path.endswith("/blogEntry.comments"):
        return httpx.Response(200, json={"status": "OK", "result": [
            {"commentatorHandle": "low", "rating": 1, "text": "meh"},
            {"commentatorHandle": "top", "rating": 40, "text": "<p>Great writeup</p>"},
        ]})
    return httpx.Response(404)


def test_codeforces_keeps_only_useful_posts_from_delayed_window(monkeypatch):
    monkeypatch.setattr(cf, "API_INTERVAL_SEC", 0)
    config = CodeforcesConfig(enabled=True, min_rating=20, category="dsa", profile="builder")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            scraper = cf.CodeforcesScraper(config, client)
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            return await scraper.fetch(since)

    result = asyncio.run(run())
    assert result.failures == {}
    assert [i.id for i in result.items] == ["codeforces:blog:1"]
    item = result.items[0]
    assert item.source_type == SourceType.CODEFORCES
    assert item.title == "A practical guide to segment tree beats"
    assert str(item.url) == "https://codeforces.com/blog/entry/1"
    assert "Body text" in item.content
    assert "data structures" in item.content
    assert item.content.index("[top]") < item.content.index("[low]")
    assert item.metadata["category"] == "dsa"
    assert item.profile == "builder"


def test_codeforces_disabled_by_default():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
            return await cf.CodeforcesScraper(CodeforcesConfig(), client).fetch(
                datetime.now(timezone.utc) - timedelta(hours=24)
            )

    assert asyncio.run(run()).items == []


def test_codeforces_api_error_is_reported():
    def bad(request):
        return httpx.Response(200, json={"status": "FAILED", "comment": "Call limit exceeded"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(bad)) as client:
            return await cf.CodeforcesScraper(CodeforcesConfig(enabled=True), client).fetch(
                datetime.now(timezone.utc) - timedelta(hours=24)
            )

    result = asyncio.run(run())
    assert result.items == []
    assert "Call limit exceeded" in result.failures["recentActions"]

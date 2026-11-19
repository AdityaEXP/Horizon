"""Codeforces blog scraper.

Codeforces has no RSS feed, so this uses the official public API:
``recentActions`` lists blog entries that are currently active, and
``blogEntry.view`` / ``blogEntry.comments`` return full text and comments.

Most Codeforces blog traffic is contest announcements, editorials, ban
appeals and rank-up posts. Those are filtered out by title, and only English
posts with enough community rating are kept.

To give posts time to collect votes, items are taken from a window that is
``delay_hours`` older than the normal collection window: with the default of
24 hours, a daily run picks up posts created 24-48 hours earlier. Each post
therefore appears in exactly one daily run.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import List

import httpx
from bs4 import BeautifulSoup

from .base import BaseScraper, FetchResult
from ..models import CodeforcesConfig, ContentItem, SourceType

API = "https://codeforces.com/api"
# Codeforces asks API clients to make at most one call every two seconds.
API_INTERVAL_SEC = 2.1
MAX_CONTENT_CHARS = 6000
TOP_COMMENTS = 3

DEFAULT_EXCLUDE = [
    r"\bround\b",
    r"\bdiv\.?\s*[1-4]\b",
    r"\beditorial\b",
    r"\bannouncement\b",
    r"\binvitation\b",
    r"\binvite\b",
    r"\bcontest\b",
    r"\bcup\b",
    r"\bappeal\b",
    r"\bcheat",
    r"\bplagiar",
    r"\bdisabled\b",
    r"\bbanned\b",
    r"\bban\b",
    r"\bskipped\b",
    r"\brating changes?\b",
    r"\bunrated\b",
    r"\bfinally\b",
    r"\breached\b",
    r"\bbecame\b",
    r"\b(newbie|pupil|specialist|expert|candidate master|master|grandmaster)\b.*!",
]


def _html_to_text(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(("p", "div", "li", "pre", "blockquote", "h1", "h2", "h3", "h4")):
        block.append("\n")
    text = soup.get_text()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class CodeforcesScraper(BaseScraper):
    """Fetch useful, well-rated Codeforces blog posts."""

    def __init__(self, config: CodeforcesConfig, http_client: httpx.AsyncClient):
        super().__init__(config.model_dump(), http_client)
        patterns = config.exclude_title_patterns or DEFAULT_EXCLUDE
        self.exclude = [re.compile(p, re.IGNORECASE) for p in patterns]
        self._last_call = 0.0

    async def _get(self, method: str, params: dict) -> object:
        loop = asyncio.get_running_loop()
        wait = self._last_call + API_INTERVAL_SEC - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = loop.time()
        response = await self.client.get(f"{API}/{method}", params=params)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "OK":
            raise ValueError(f"Codeforces {method}: {data.get('comment', 'not OK')}")
        return data["result"]

    def _is_excluded(self, title: str) -> bool:
        return any(p.search(title) for p in self.exclude)

    async def fetch(self, since: datetime) -> FetchResult:
        result = FetchResult()
        if not self.config.get("enabled", False):
            return result

        delay = timedelta(hours=self.config.get("delay_hours", 24))
        window_start = since - delay
        window_end = datetime.now(timezone.utc) - delay

        try:
            actions = await self._get("recentActions", {"maxCount": 100})
        except Exception as exc:
            result.fail("recentActions", exc)
            return result
        result.successful += 1

        candidates: dict[int, dict] = {}
        for action in actions or []:
            entry = action.get("blogEntry") or {}
            entry_id = entry.get("id")
            if not entry_id or entry_id in candidates:
                continue
            created = datetime.fromtimestamp(entry.get("creationTimeSeconds", 0), timezone.utc)
            title = _html_to_text(entry.get("title", ""))
            if not (window_start <= created < window_end):
                continue
            if entry.get("originalLocale", "en") != "en":
                continue
            if entry.get("rating", 0) < self.config.get("min_rating", 20):
                continue
            if self._is_excluded(title):
                continue
            candidates[entry_id] = {**entry, "title": title, "created": created}

        ranked = sorted(candidates.values(), key=lambda e: e.get("rating", 0), reverse=True)
        for entry in ranked[: self.config.get("max_posts", 8)]:
            await result.collect(f"blog:{entry['id']}", self._build_item(entry))
        return result

    async def _build_item(self, entry: dict) -> List[ContentItem]:
        entry_id = entry["id"]
        full = await self._get("blogEntry.view", {"blogEntryId": entry_id})
        body = _html_to_text(full.get("content", ""))
        if len(body) > MAX_CONTENT_CHARS:
            body = body[: MAX_CONTENT_CHARS - 3] + "..."

        comments_text = ""
        try:
            comments = await self._get("blogEntry.comments", {"blogEntryId": entry_id})
            top = sorted(comments or [], key=lambda c: c.get("rating", 0), reverse=True)
            lines = []
            for c in top[:TOP_COMMENTS]:
                text = _html_to_text(c.get("text", ""))
                if len(text) > 500:
                    text = text[:497] + "..."
                lines.append(f"[{c.get('commentatorHandle', 'anon')}] (+{c.get('rating', 0)}): {text}")
            if lines:
                comments_text = "\n\n--- Top Comments ---\n" + "\n\n".join(lines)
        except Exception:
            comments_text = ""

        tags = ", ".join(full.get("tags", []) or [])
        content = body + (f"\n\nTags: {tags}" if tags else "") + comments_text
        url = f"https://codeforces.com/blog/entry/{entry_id}"

        return [
            ContentItem(
                id=self._generate_id("codeforces", "blog", str(entry_id)),
                source_type=SourceType.CODEFORCES,
                title=entry["title"],
                url=url,
                content=content,
                author=entry.get("authorHandle"),
                published_at=entry["created"],
                profile=self.config.get("profile"),
                metadata={
                    "score": entry.get("rating", 0),
                    "discussion_url": url,
                    "tags": full.get("tags", []),
                    "category": self.config.get("category"),
                },
            )
        ]

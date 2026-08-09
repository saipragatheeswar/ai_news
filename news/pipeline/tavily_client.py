"""Thin wrapper over the Tavily search API with normalisation and retries."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlparse

from django.conf import settings
from tavily import TavilyClient

logger = logging.getLogger("news.tavily")


class TavilyNotConfigured(RuntimeError):
    pass


@dataclass
class SearchHit:
    title: str
    url: str
    content: str
    score: float = 0.0
    raw_content: str = ""
    published_at: datetime | None = None
    query: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def domain(self) -> str:
        host = urlparse(self.url).netloc.lower()
        return host[4:] if host.startswith("www.") else host

    @property
    def reference_text(self) -> str:
        return f"{self.title}\n{self.raw_content or self.content}".strip()


# Domains that syndicate paywalled wire copy or are low-signal for our purposes.
BLOCKED_DOMAINS = {
    "reddit.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "quora.com",
    "pinterest.com",
    "youtube.com",
}


class NewsSearch:
    """Fetches search results for topic discovery and per-topic research."""

    def __init__(self, api_key: str | None = None):
        key = api_key or settings.TAVILY_API_KEY
        if not key:
            raise TavilyNotConfigured(
                "TAVILY_API_KEY is missing. Add it to your .env file."
            )
        self._client = TavilyClient(api_key=key)

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        days: int = 2,
        topic: str = "news",
        include_raw_content: bool = False,
        search_depth: str = "basic",
    ) -> list[SearchHit]:
        payload = {
            "query": query,
            "max_results": max_results,
            "topic": topic,
            "search_depth": search_depth,
            "include_raw_content": include_raw_content,
        }
        if topic == "news":
            payload["days"] = days

        response = self._call_with_retry(payload)
        if response is None:
            return []

        hits: list[SearchHit] = []
        for item in response.get("results", []):
            url = (item.get("url") or "").strip()
            if not url:
                continue
            hit = SearchHit(
                title=(item.get("title") or "").strip(),
                url=url,
                content=(item.get("content") or "").strip(),
                score=float(item.get("score") or 0.0),
                raw_content=_clean_raw(item.get("raw_content")),
                published_at=_parse_date(item.get("published_date")),
                query=query,
            )
            if hit.domain in BLOCKED_DOMAINS:
                continue
            hits.append(hit)
        logger.debug("query=%r returned %d usable hits", query, len(hits))
        return hits

    def extract(self, urls: list[str]) -> dict[str, str]:
        """Fetch full article text for URLs, returning {url: text}.

        Search snippets are only a couple of hundred words, which is far too
        little to write a long article from. The extract endpoint returns the
        whole page, which is what makes a substantial piece possible.
        """
        if not urls:
            return {}

        results: dict[str, str] = {}
        # The endpoint accepts batches; keep them small so one bad URL cannot
        # cost us the whole set.
        for batch in _chunked(urls, 5):
            try:
                response = self._client.extract(urls=batch, extract_depth="advanced")
            except Exception as exc:
                logger.warning("extract failed for %s: %s", batch, exc)
                continue

            for item in response.get("results", []) or []:
                url = (item.get("url") or "").strip()
                text = _clean_raw(item.get("raw_content"), limit=40000)
                if url and text:
                    results[url] = text
            for failed in response.get("failed_results", []) or []:
                logger.debug("extract could not read %s", failed)

        logger.info("extracted full text for %d/%d urls", len(results), len(urls))
        return results

    def _call_with_retry(self, payload: dict, attempts: int = 3) -> dict | None:
        delay = 2.0
        for attempt in range(1, attempts + 1):
            try:
                return self._client.search(**payload)
            except Exception as exc:  # tavily raises a family of client errors
                if attempt == attempts:
                    logger.error(
                        "Tavily search failed permanently for %r: %s",
                        payload.get("query"),
                        exc,
                    )
                    return None
                logger.warning(
                    "Tavily search failed (attempt %d/%d) for %r: %s",
                    attempt,
                    attempts,
                    payload.get("query"),
                    exc,
                )
                time.sleep(delay)
                delay *= 2
        return None


def _chunked(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _clean_raw(raw: str | None, limit: int = 6000) -> str:
    if not raw:
        return ""
    collapsed = " ".join(raw.split())
    return collapsed[:limit]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    candidates = (
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    )
    for fmt in candidates:
        try:
            parsed = datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_timezone.utc)
        return parsed
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

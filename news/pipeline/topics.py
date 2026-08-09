"""Hot-topic discovery: cluster the day's search results and rank them by heat."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from news.models import Category, Source, Topic
from news.pipeline.tavily_client import NewsSearch, SearchHit

logger = logging.getLogger("news.topics")

_STOPWORDS = {
    "about", "after", "against", "amid", "and", "are", "back", "before", "being",
    "breaking", "but", "day", "for", "from", "has", "have", "headlines", "his",
    "how", "into", "its", "latest", "live", "more", "new", "news", "not", "now",
    "off", "one", "out", "over", "report", "reports", "said", "says", "she",
    "than", "that", "the", "their", "them", "there", "these", "they", "this",
    "those", "today", "top", "update", "updates", "was", "were", "what", "when",
    "where", "which", "while", "who", "why", "will", "with", "you", "your",
}

# Trailing outlet branding that search engines append to headlines.
_OUTLET_SUFFIX = re.compile(
    r"\s+[-|–—]\s+[A-Z][A-Za-z0-9.&'’ ]{2,40}$"
)


@dataclass
class TopicCluster:
    """A group of search hits that appear to describe the same story."""

    label: str
    category_slug: str
    keywords: set[str]
    hits: list[SearchHit] = field(default_factory=list)

    @property
    def domains(self) -> set[str]:
        return {hit.domain for hit in self.hits if hit.domain}

    @property
    def heat_score(self) -> float:
        """Independent outlets carrying a story is the strongest heat signal."""
        distinct_domains = len(self.domains)
        breadth = distinct_domains * 2.0
        relevance = sum(hit.score for hit in self.hits) / max(len(self.hits), 1)
        return round(breadth + relevance * 1.5 + self._recency_bonus(), 4)

    def _recency_bonus(self) -> float:
        now = timezone.now()
        freshest = None
        for hit in self.hits:
            if hit.published_at and (freshest is None or hit.published_at > freshest):
                freshest = hit.published_at
        if freshest is None:
            return 0.0
        age_hours = max((now - freshest).total_seconds() / 3600.0, 0.0)
        if age_hours <= 6:
            return 2.0
        if age_hours <= 24:
            return 1.0
        if age_hours <= 48:
            return 0.25
        return 0.0

    def best_hits(self, limit: int) -> list[SearchHit]:
        """Prefer one hit per domain so the model sees independent reporting."""
        seen: set[str] = set()
        primary: list[SearchHit] = []
        spares: list[SearchHit] = []
        for hit in sorted(self.hits, key=lambda h: h.score, reverse=True):
            if hit.domain in seen:
                spares.append(hit)
                continue
            seen.add(hit.domain)
            primary.append(hit)
        return (primary + spares)[:limit]


def keywords_of(text: str) -> set[str]:
    tokens = re.findall(r"[A-Za-z0-9']+", text.lower())
    return {t for t in tokens if len(t) > 2 and t not in _STOPWORDS}


def clean_headline(title: str) -> str:
    cleaned = _OUTLET_SUFFIX.sub("", title).strip(" -|–—\u00a0")
    return " ".join(cleaned.split())


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    overlap = len(a & b)
    return overlap / min(len(a), len(b))


def cluster_hits(
    hits_by_category: dict[str, list[SearchHit]], threshold: float = 0.5
) -> list[TopicCluster]:
    clusters: list[TopicCluster] = []
    for category_slug, hits in hits_by_category.items():
        for hit in sorted(hits, key=lambda h: h.score, reverse=True):
            headline = clean_headline(hit.title)
            if len(headline) < 15:
                continue
            words = keywords_of(headline)
            if len(words) < 3:
                continue
            match = next(
                (
                    c
                    for c in clusters
                    if c.category_slug == category_slug
                    and _similarity(c.keywords, words) >= threshold
                ),
                None,
            )
            if match is None:
                clusters.append(
                    TopicCluster(
                        label=headline,
                        category_slug=category_slug,
                        keywords=words,
                        hits=[hit],
                    )
                )
            else:
                match.hits.append(hit)
                match.keywords |= words
    return clusters


def discover(
    search: NewsSearch,
    *,
    target: int,
    pool_size: int | None = None,
    days: int = 2,
) -> list[Topic]:
    """Run seed queries, cluster the results, and persist the hottest topics."""
    pool_size = pool_size or settings.TOPIC_CANDIDATE_POOL
    category_queries: dict[str, list[str]] = settings.NEWS_CATEGORY_QUERIES
    per_query = max(6, pool_size // max(sum(len(v) for v in category_queries.values()), 1))

    hits_by_category: dict[str, list[SearchHit]] = {}
    for category_slug, queries in category_queries.items():
        collected: list[SearchHit] = []
        for query in queries:
            collected.extend(
                search.search(
                    query,
                    max_results=per_query,
                    days=days,
                    topic="news",
                )
            )
        hits_by_category[category_slug] = collected
        logger.info("category=%s collected %d hits", category_slug, len(collected))

    clusters = cluster_hits(hits_by_category)
    logger.info("clustered %d candidate topics", len(clusters))

    selected = _select_with_category_spread(clusters, target=target)
    return _persist(selected)


def _select_with_category_spread(
    clusters: list[TopicCluster], *, target: int
) -> list[TopicCluster]:
    """Round-robin across categories so one busy beat cannot fill the whole day."""
    by_category: dict[str, list[TopicCluster]] = {}
    for cluster in sorted(clusters, key=lambda c: c.heat_score, reverse=True):
        by_category.setdefault(cluster.category_slug, []).append(cluster)

    # A rumour needs corroboration before it is worth writing at all.
    min_rumour_sources = settings.MIN_SOURCES_FOR_RUMOUR
    if "rumours" in by_category:
        by_category["rumours"] = [
            c for c in by_category["rumours"] if len(c.domains) >= min_rumour_sources
        ]

    selected: list[TopicCluster] = []
    exhausted: set[str] = set()
    # Overshoot: later gates reject some topics, so build a buffer.
    limit = int(target * 1.6) + 4
    while len(selected) < limit and len(exhausted) < len(by_category):
        for category_slug, bucket in by_category.items():
            if not bucket:
                exhausted.add(category_slug)
                continue
            selected.append(bucket.pop(0))
            if len(selected) >= limit:
                break
    return selected


def _persist(clusters: list[TopicCluster]) -> list[Topic]:
    today = timezone.localdate()
    recent_cutoff = today - timedelta(days=7)
    recent_fingerprints = set(
        Topic.objects.filter(discovered_for__gte=recent_cutoff).values_list(
            "fingerprint", flat=True
        )
    )

    created: list[Topic] = []
    for cluster in clusters:
        fingerprint = Topic.build_fingerprint(cluster.label)
        if fingerprint in recent_fingerprints:
            logger.debug("skipping duplicate topic: %s", cluster.label)
            continue
        recent_fingerprints.add(fingerprint)

        category = _category_for(cluster.category_slug)
        topic = Topic.objects.create(
            label=cluster.label[:300],
            fingerprint=fingerprint,
            category=category,
            query=(cluster.hits[0].query if cluster.hits else "")[:300],
            heat_score=cluster.heat_score,
            source_count=len(cluster.hits),
            domain_count=len(cluster.domains),
            discovered_for=today,
        )
        for hit in cluster.best_hits(settings.SOURCES_PER_TOPIC):
            Source.objects.update_or_create(
                topic=topic,
                url=hit.url[:1000],
                defaults={
                    "domain": hit.domain[:200],
                    "title": hit.title[:500],
                    "snippet": hit.content,
                    "raw_content": hit.raw_content,
                    "relevance": hit.score,
                    "published_at": hit.published_at,
                },
            )
        created.append(topic)
    logger.info("persisted %d new topics", len(created))
    return created


_CATEGORY_LABELS = {
    "world": "World",
    "india": "India",
    "business": "Business",
    "technology": "Technology",
    "sports": "Sports",
    "entertainment": "Entertainment",
    "rumours": "Rumours & Reports",
    "science": "Science & Health",
}


def _category_for(slug: str) -> Category:
    category, _ = Category.objects.get_or_create(
        slug=slug,
        defaults={"name": _CATEGORY_LABELS.get(slug, slug.title())},
    )
    return category


def enrich_sources(search: NewsSearch, topic: Topic) -> int:
    """Fetch full article text for a topic's sources before writing."""
    needed = settings.SOURCES_PER_TOPIC
    existing = list(topic.sources.all())
    thin = [s for s in existing if len(s.raw_content) < 400]

    if len(existing) >= needed and not thin:
        return len(existing)

    hits = search.search(
        topic.label,
        max_results=needed + 3,
        days=4,
        topic="news",
        include_raw_content=True,
        search_depth="advanced",
    )
    known_urls = {s.url for s in existing}
    added = 0
    for hit in hits:
        if not hit.reference_text.strip():
            continue
        if hit.url in known_urls:
            source = topic.sources.filter(url=hit.url).first()
            if source and len(hit.raw_content) > len(source.raw_content):
                source.raw_content = hit.raw_content
                source.save(update_fields=["raw_content"])
            continue
        Source.objects.update_or_create(
            topic=topic,
            url=hit.url[:1000],
            defaults={
                "domain": hit.domain[:200],
                "title": hit.title[:500],
                "snippet": hit.content,
                "raw_content": hit.raw_content,
                "relevance": hit.score,
                "published_at": hit.published_at,
            },
        )
        added += 1

    total = topic.sources.count()
    topic.source_count = total
    topic.domain_count = len(
        {d for d in topic.sources.values_list("domain", flat=True) if d}
    )
    topic.save(update_fields=["source_count", "domain_count"])
    logger.debug("topic=%s enriched with %d new sources", topic.pk, added)
    return total

"""Find, fetch and store article images.

Preference order:
1. Hero/social image from a source article page (og:image / twitter:image),
   downloaded into our own media storage (never hotlinked).
2. Openly-licensed fallback from Openverse / Wikimedia Commons when no
   usable source image is found.
"""

from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from news.models import Article, ArticleImage

logger = logging.getLogger("news.images")

USER_AGENT = (
    "DailyNewsBot/1.0 (https://github.com/saipragatheeswar/ai_news; "
    "article image fetcher)"
)

# Licences that permit reuse for the open-licence fallback path.
ALLOWED_OPENVERSE_LICENCES = {"cc0", "pdm", "by", "by-sa"}

_META_IMAGE_PATTERNS = [
    re.compile(
        r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image(?::src)?["\']',
        re.I,
    ),
]


def _http_get(url: str, *, params: dict | None = None, stream: bool = False):
    """GET with a short retry on rate limits."""
    last_response: requests.Response | None = None
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                timeout=settings.IMAGE_HTTP_TIMEOUT,
                stream=stream,
            )
            last_response = response
            if response.status_code in {429, 503}:
                time.sleep(2.5 * (attempt + 1))
                continue
            return response
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    if last_response is not None:
        return last_response
    assert last_exc is not None
    raise last_exc


@dataclass
class ImageCandidate:
    url: str
    provider: str
    title: str = ""
    creator: str = ""
    licence: str = ""
    licence_url: str = ""
    source_page: str = ""

    @property
    def credit(self) -> str:
        if self.creator:
            return f"{self.creator} via {self.provider}"
        return self.provider


def attach_image(
    article: Article,
    queries: list[str],
    source_urls: list[str] | None = None,
) -> ArticleImage | None:
    """Attach one image: prefer source-page heroes, then open-licence search."""
    if not settings.FETCH_IMAGES:
        return None
    if article.images.exists():
        return article.images.first()

    stored = attach_image_from_sources(article, source_urls or [])
    if stored is not None:
        return stored

    for query in _usable_queries(queries):
        for finder in (_search_openverse, _search_wikimedia):
            try:
                candidate = finder(query)
            except requests.RequestException as exc:
                logger.warning("%s lookup failed for %r: %s", finder.__name__, query, exc)
                continue
            if candidate is None:
                continue
            stored = _download_and_store(article, candidate, query)
            if stored is not None:
                logger.info(
                    "image for article %s from %s (%s)",
                    article.pk,
                    candidate.provider,
                    candidate.licence,
                )
                return stored

    logger.info("no image found for article %s", article.pk)
    return None


def attach_image_from_sources(
    article: Article, source_urls: list[str]
) -> ArticleImage | None:
    """Download the first usable og/twitter image from source article pages."""
    if not settings.FETCH_IMAGES:
        return None
    if article.images.exists():
        return article.images.first()

    for page_url in _dedupe_urls(source_urls):
        try:
            image_url = extract_image_url(page_url)
        except requests.RequestException as exc:
            logger.debug("source page fetch failed for %s: %s", page_url, exc)
            continue
        if not image_url:
            continue

        domain = _domain(page_url)
        candidate = ImageCandidate(
            url=image_url,
            provider="Source page",
            title=article.title,
            creator=domain,
            licence="Source article image",
            source_page=page_url,
        )
        stored = _download_and_store(article, candidate, article.title)
        if stored is not None:
            logger.info(
                "image for article %s from source %s",
                article.pk,
                domain,
            )
            return stored
    return None


def extract_image_url(page_url: str) -> str | None:
    """Return absolute og:image / twitter:image URL from a source page."""
    response = _http_get(page_url)
    if response.status_code != 200 or not response.content:
        return None

    content_type = (response.headers.get("Content-Type") or "").lower()
    # Some outlets serve the image URL itself; accept direct image responses.
    if content_type.startswith("image/"):
        return page_url

    # Only need the head for meta tags; keep parse cheap.
    html = response.text[:200_000]
    for pattern in _META_IMAGE_PATTERNS:
        match = pattern.search(html)
        if not match:
            continue
        raw = unescape(match.group(1).strip())
        if not raw or raw.startswith("data:"):
            continue
        absolute = urljoin(page_url, raw)
        if absolute.startswith(("http://", "https://")):
            return absolute
    return None


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        text = (url or "").strip()
        if not text.startswith(("http://", "https://")):
            continue
        key = text.split("#", 1)[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        ordered.append(text)
    return ordered[:8]


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _usable_queries(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned = []
    for query in queries:
        text = " ".join(re.findall(r"[A-Za-z0-9'&-]+", query or ""))[:80].strip()
        if len(text) < 3:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned[:4]


def _search_openverse(query: str) -> ImageCandidate | None:
    response = _http_get(
        "https://api.openverse.org/v1/images/",
        params={
            "q": query,
            "license": ",".join(sorted(ALLOWED_OPENVERSE_LICENCES)),
            "size": "large",
            "mature": "false",
            "page_size": 8,
        },
    )
    if response.status_code != 200:
        logger.debug("openverse returned %s for %r", response.status_code, query)
        return None

    for item in response.json().get("results", []) or []:
        licence = (item.get("license") or "").lower()
        if licence not in ALLOWED_OPENVERSE_LICENCES:
            continue
        url = item.get("url")
        if not url:
            continue
        return ImageCandidate(
            url=url,
            provider=(item.get("source") or item.get("provider") or "Openverse").title(),
            title=item.get("title") or "",
            creator=item.get("creator") or "",
            licence=_licence_label(licence, item.get("license_version")),
            licence_url=item.get("license_url") or "",
            source_page=item.get("foreign_landing_url") or "",
        )
    return None


def _search_wikimedia(query: str) -> ImageCandidate | None:
    response = _http_get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": 6,
            "gsrlimit": 8,
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            "iiurlwidth": settings.IMAGE_MAX_WIDTH,
        },
    )
    if response.status_code != 200:
        return None

    pages = (response.json().get("query") or {}).get("pages") or {}
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        meta = info.get("extmetadata") or {}
        licence = _meta_value(meta, "LicenseShortName")
        if not _wikimedia_licence_ok(licence):
            continue
        return ImageCandidate(
            url=url,
            provider="Wikimedia Commons",
            title=page.get("title", "").removeprefix("File:"),
            creator=_strip_html(_meta_value(meta, "Artist")),
            licence=licence or "See Commons",
            licence_url=_meta_value(meta, "LicenseUrl"),
            source_page=info.get("descriptionurl") or "",
        )
    return None


def _wikimedia_licence_ok(licence: str) -> bool:
    if not licence:
        return False
    lowered = licence.lower()
    if "no derivatives" in lowered or "-nd" in lowered:
        return False
    return any(
        token in lowered
        for token in ("cc0", "public domain", "cc by", "cc-by", "attribution")
    )


def _download_and_store(
    article: Article, candidate: ImageCandidate, query: str
) -> ArticleImage | None:
    try:
        response = _http_get(candidate.url, stream=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("could not download %s: %s", candidate.url, exc)
        return None

    content_type = (response.headers.get("Content-Type") or "").lower()
    # Some CDNs omit content-type; still try to decode as an image.
    if content_type and not content_type.startswith("image/") and "octet-stream" not in content_type:
        logger.debug("skipping non-image response %s", content_type)
        return None

    raw = io.BytesIO()
    size = 0
    for chunk in response.iter_content(64 * 1024):
        size += len(chunk)
        if size > settings.IMAGE_MAX_BYTES:
            logger.debug("image too large, skipping: %s", candidate.url)
            return None
        raw.write(chunk)
    raw.seek(0)

    try:
        processed, width, height = _normalise(raw)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning("could not process image %s: %s", candidate.url, exc)
        return None

    image = ArticleImage(
        article=article,
        alt_text=(candidate.title or query)[:300],
        credit=candidate.credit[:200],
        licence=candidate.licence[:80],
        licence_url=candidate.licence_url or "",
        source_page=candidate.source_page or "",
        provider=candidate.provider[:60],
        width=width,
        height=height,
    )
    image.file.save(f"{article.slug[:60]}.jpg", ContentFile(processed), save=False)
    image.save()
    return image


def _normalise(buffer: io.BytesIO) -> tuple[bytes, int, int]:
    """Downscale, strip metadata and re-encode as JPEG to keep pages light."""
    with Image.open(buffer) as source:
        source = ImageOps.exif_transpose(source)
        if source.width < 400 or source.height < 250:
            raise ValueError("image too small to use")

        image = source.convert("RGB")
        if image.width > settings.IMAGE_MAX_WIDTH:
            ratio = settings.IMAGE_MAX_WIDTH / image.width
            image = image.resize(
                (settings.IMAGE_MAX_WIDTH, max(1, round(image.height * ratio))),
                Image.LANCZOS,
            )

        out = io.BytesIO()
        image.save(
            out,
            format="JPEG",
            quality=settings.IMAGE_JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )
        return out.getvalue(), image.width, image.height


def _licence_label(licence: str, version: str | None) -> str:
    if licence in {"cc0", "pdm"}:
        return {"cc0": "CC0", "pdm": "Public domain"}[licence]
    label = f"CC {licence.upper()}"
    return f"{label} {version}" if version else label


def _meta_value(meta: dict, key: str) -> str:
    entry = meta.get(key) or {}
    return str(entry.get("value") or "").strip()


def _strip_html(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", value).split())[:200]


def queries_for(topic_label: str, entities: list[str], category_name: str) -> list[str]:
    """Build image search terms, most specific first."""
    queries = []
    if entities:
        queries.append(" ".join(entities[:2]))
        queries.append(entities[0])
    keywords = [
        word
        for word in re.findall(r"[A-Za-z][A-Za-z'&-]{2,}", topic_label)
        if word[0].isupper()
    ]
    if keywords:
        queries.append(" ".join(keywords[:3]))
    queries.append(category_name)
    return queries

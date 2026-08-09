"""Find, fetch and store openly-licensed images for articles.

We never copy pictures from the outlets we summarise. News photography is
normally licensed to the outlet by an agency (Reuters, AP, PTI, Getty), so the
outlet could not grant us permission even if it wanted to, and image claims are
the most aggressively enforced form of copyright online.

Instead we search sources that publish explicit reuse licences - Openverse
(which aggregates Creative Commons and public domain works) and Wikimedia
Commons - download the file to our own storage, and record the licence and
creator so the credit can be displayed with the picture.

These images illustrate the subject rather than depicting the specific event, so
they are captioned as such and never used to imply they are news photographs of
what happened.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from news.models import Article, ArticleImage

logger = logging.getLogger("news.images")

USER_AGENT = "PulseWireBot/1.0 (openly-licensed image fetcher)"

# Licences that permit reuse. Anything outside this set is ignored, including
# "nd" (no derivatives) since we resize, and "nc" is kept only if the site is
# non-commercial - so we exclude it by default to stay safe.
ALLOWED_OPENVERSE_LICENCES = {"cc0", "pdm", "by", "by-sa"}


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


def attach_image(article: Article, queries: list[str]) -> ArticleImage | None:
    """Find and store one openly-licensed image for an article."""
    if not settings.FETCH_IMAGES:
        return None
    if article.images.exists():
        return article.images.first()

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

    logger.info("no openly-licensed image found for article %s", article.pk)
    return None


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
    response = requests.get(
        "https://api.openverse.org/v1/images/",
        params={
            "q": query,
            "license": ",".join(sorted(ALLOWED_OPENVERSE_LICENCES)),
            "size": "large",
            "mature": "false",
            "page_size": 8,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=settings.IMAGE_HTTP_TIMEOUT,
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
    response = requests.get(
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
        headers={"User-Agent": USER_AGENT},
        timeout=settings.IMAGE_HTTP_TIMEOUT,
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
        response = requests.get(
            candidate.url,
            headers={"User-Agent": USER_AGENT},
            timeout=settings.IMAGE_HTTP_TIMEOUT,
            stream=True,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("could not download %s: %s", candidate.url, exc)
        return None

    content_type = (response.headers.get("Content-Type") or "").lower()
    if not content_type.startswith("image/"):
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

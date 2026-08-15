"""Turn gathered sources into original copy.

The rewriter deliberately works in stages:

1. **Extract** - the model reads each source article separately and returns a
   structured list of bare facts, with all phrasing stripped away.
2. **Write** - the model composes the article from *only* that merged fact list,
   one section at a time. It never sees the source prose while writing.
3. **Headline** - written last, from our own finished copy.

Passing facts rather than text through the bottleneck is what makes the output
original: there is no source wording available to copy. Stage 2 also converts
every direct quotation into indirect speech, since verbatim quotes are both a
copyright risk and the main driver of n-gram overlap.

Sections exist because a 2 GB model cannot hold a 600-word article in a single
response; asked for one it produces a short, padded summary. Writing in three
passes, each aware of what came before, reliably reaches full length.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from django.conf import settings

from news.models import Source, Topic
from news.pipeline.llm import LocalModel, LocalModelError

logger = logging.getLogger("news.rewriter")


@dataclass
class FactSheet:
    facts: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return len(self.facts) >= 3

    def render(self, limit: int = 30) -> str:
        lines = ["VERIFIED FACTS (reported by a source or an official):"]
        lines += [f"- {fact}" for fact in self.facts[:limit]]
        if self.unverified:
            lines.append("")
            lines.append("UNVERIFIED / SINGLE-SOURCE CLAIMS (must be hedged):")
            lines += [f"- {claim}" for claim in self.unverified[:10]]
        if self.entities:
            lines.append("")
            lines.append("KEY NAMES: " + ", ".join(self.entities[:12]))
        return "\n".join(lines)


@dataclass
class Draft:
    title: str
    summary: str
    body: str
    attempts: int = 1


_EXTRACT_SYSTEM = """You are a news researcher. You read press coverage and
extract the underlying facts, discarding the original phrasing entirely.

Rules:
- Write each fact as a short, neutral statement in your own plain words.
- Never copy a phrase of more than three consecutive words from the source.
- Convert every direct quotation into a paraphrase, naming who said it.
- Put a claim in "unverified" if it is presented as a rumour or speculation, or
  if it accuses someone of wrongdoing.
- Include concrete detail: numbers, dates, places, scores, job titles, amounts.
- Extract the 10 most newsworthy facts. Keep each one under 30 words.
- Do not invent anything. If the source does not say it, leave it out.

Reply with JSON only:
{"facts": ["...", "..."], "unverified": ["..."], "entities": ["..."]}"""


# Each pass is a separate model call. Keeping them short is what makes a small
# model produce full-length copy instead of a padded summary.
ARTICLE_SECTIONS = [
    (
        "opening",
        """Write the OPENING of the report.
Start with a single-sentence lede answering who, what, when and where. Then two
further paragraphs giving the most important supporting detail: the numbers,
names, places and immediate consequences.
Write 3 paragraphs, about 200 words.""",
    ),
    (
        "detail",
        """Write the MIDDLE of the report, continuing directly from the copy so
far. Cover the secondary detail and the background a reader needs to make sense
of the story: how the situation came about, what was said by the parties
involved, and any figures not yet used.
Do not repeat anything already written. Do not re-introduce the story.
Write 2 or 3 paragraphs, about 220 words.""",
    ),
    (
        "outlook",
        """Write the CLOSING of the report, continuing directly from the copy so
far. Cover reaction from the people involved, what happens next, and what
remains unknown or unconfirmed.
Do not repeat anything already written. Do not write a conclusion that
summarises the article; end on the next concrete development instead.
Write 2 paragraphs, about 160 words.""",
    ),
]

# Used when the three passes land short. Adding another pass is far cheaper than
# rewriting the article from scratch, and it is what a short draft actually
# needs: more material, not different wording.
_EXPAND_INSTRUCTION = """The report is {short_by} words shorter than required.
Continue it with further paragraphs, using facts from the sheet that have not
been used yet.
Do not repeat, rephrase or summarise anything already written, and do not write a
closing summary. Add new information only.
Write about {words} more words."""

MAX_EXPANSION_PASSES = 3


_WRITE_SYSTEM = """You are a staff reporter writing original copy for a
general-audience news site. You are given a fact sheet, never source articles.

Write using ONLY the supplied facts.

Absolute rules:
- Plain, neutral, third-person reporting voice. No "I", "we" or "our".
- No headings, no bullet points, no markdown, no emoji, no bylines.
- Do not echo the fact sheet's phrasing or its order. Reorganise by news value
  and write flowing prose with proper connective language.
- Attribute every unverified claim: say it is reported, alleged or unconfirmed,
  and name who is making the claim.
- Never state as fact that a person or company committed a crime or misconduct
  unless the fact sheet says a court, regulator or official body established it.
- No sexual, adult or graphic content. No slurs or demeaning generalisations.
- No phone numbers, emails, addresses or ID numbers.
- Do not speculate beyond the fact sheet and never invent quotations.

Reply with JSON only: {"text": "paragraph\\n\\nparagraph"}"""


_HEADLINE_SYSTEM = """You write headlines for a news site. You are given a
finished article and must title it.

Rules:
- The headline states the single most important thing that happened.
- Name the specific subject. Never write a vague label like "World News",
  "Latest Update" or "News Roundup".
- Under 100 characters, factual, no clickbait, no questions, no colons at the
  start, sentence case.
- The summary is one sentence under 180 characters describing the development.

Reply with JSON only: {"headline": "...", "summary": "..."}"""


def build_fact_sheet(model: LocalModel, topic: Topic, sources: list[Source]) -> FactSheet:
    """Extract facts one source at a time, then merge.

    Per-source calls keep each prompt inside the model's context window, which
    matters now that sources carry full article text rather than a snippet.
    """
    per_source = max(settings.SOURCE_TEXT_BUDGET // max(len(sources), 1), 2000)
    sheet = FactSheet()
    seen: set[str] = set()

    for index, source in enumerate(sources, start=1):
        text = (source.raw_content or source.snippet or "").strip()
        if len(text) < 200:
            continue

        prompt = (
            f"Topic: {topic.label}\n"
            f"Beat: {topic.category.name}\n"
            f"Outlet: {source.domain}\n"
            f"Headline: {source.title}\n\n"
            f"{text[:per_source]}"
        )
        try:
            result = model.chat_json(
                _EXTRACT_SYSTEM, prompt, temperature=0.1, num_predict=1100
            )
        except LocalModelError as exc:
            logger.warning(
                "fact extraction failed for topic %s source %d: %s", topic.pk, index, exc
            )
            continue

        for fact in _clean_list(result.get("facts")):
            key = _dedupe_key(fact)
            if key and key not in seen:
                seen.add(key)
                sheet.facts.append(fact)
        for claim in _clean_list(result.get("unverified")):
            key = _dedupe_key(claim)
            if key and key not in seen:
                seen.add(key)
                sheet.unverified.append(claim)
        for entity in _clean_list(result.get("entities"), limit=12, max_len=80):
            if entity not in sheet.entities:
                sheet.entities.append(entity)

    logger.info(
        "topic=%s fact sheet: %d facts, %d unverified, from %d sources",
        topic.pk,
        len(sheet.facts),
        len(sheet.unverified),
        len(sources),
    )
    return sheet


def write_article(
    model: LocalModel,
    topic: Topic,
    fact_sheet: FactSheet,
    *,
    feedback: str = "",
    attempt: int = 1,
) -> Draft | None:
    """Compose the article section by section, then headline it."""
    context = [
        f"Beat: {topic.category.name}",
        f"Working topic: {topic.label}",
        "",
        fact_sheet.render(),
    ]
    if topic.category.slug == "rumours":
        context += [
            "",
            "This story is an unconfirmed report. Frame the whole article as "
            "reported-but-unverified and state plainly that it has not been "
            "officially confirmed.",
        ]
    if feedback:
        context += [
            "",
            "A previous attempt was rejected. Avoid these problems, and choose "
            "completely different wording this time:",
            feedback,
        ]

    base = "\n".join(context)
    # Nudge wording further from the source on each retry.
    temperature = min(0.6 + 0.15 * (attempt - 1), 0.95)
    paragraphs: list[str] = []

    for name, instruction in ARTICLE_SECTIONS:
        prompt = [base, "", instruction]
        if paragraphs:
            written = "\n\n".join(paragraphs)
            prompt += [
                "",
                "THE COPY SO FAR (continue from this; do not repeat it):",
                written[-2600:],
            ]
        try:
            result = model.chat_json(
                _WRITE_SYSTEM, "\n".join(prompt), temperature=temperature
            )
        except LocalModelError as exc:
            logger.warning("section %r failed for topic %s: %s", name, topic.pk, exc)
            continue

        section = _clean_body(result.get("text") or result.get("body"))
        if not section:
            logger.info("section %r came back empty for topic %s", name, topic.pk)
            continue
        paragraphs.extend(p for p in section.split("\n\n") if p.strip())

    body = _dedupe_paragraphs(paragraphs)
    if len(body.split()) < 120:
        logger.info(
            "topic %s produced only %d words across sections",
            topic.pk,
            len(body.split()),
        )
        return None

    body = _expand_to_length(model, topic, base, body, temperature)

    title, summary = _headline_for(model, topic, body)
    if not title:
        return None

    return Draft(title=title, summary=summary, body=body, attempts=attempt)


def _expand_to_length(
    model: LocalModel, topic: Topic, base: str, body: str, temperature: float
) -> str:
    """Keep writing until the article is long enough, or we run out of passes."""
    target = settings.TARGET_ARTICLE_WORDS
    minimum = settings.MIN_ARTICLE_WORDS

    for pass_number in range(1, MAX_EXPANSION_PASSES + 1):
        words = len(body.split())
        if words >= minimum:
            return body

        short_by = max(target - words, 60)
        prompt = [
            base,
            "",
            _EXPAND_INSTRUCTION.format(short_by=minimum - words, words=short_by),
            "",
            "THE COPY SO FAR (continue from this; do not repeat it):",
            body[-3000:],
        ]
        try:
            result = model.chat_json(
                _WRITE_SYSTEM, "\n".join(prompt), temperature=temperature
            )
        except LocalModelError as exc:
            logger.warning("expansion pass %d failed: %s", pass_number, exc)
            break

        addition = _clean_body(result.get("text") or result.get("body"))
        if not addition:
            break

        combined = _dedupe_paragraphs(
            [p for p in body.split("\n\n") if p.strip()]
            + [p for p in addition.split("\n\n") if p.strip()]
        )
        if len(combined.split()) <= words:
            # The model only restated existing copy; another pass will not help.
            logger.info("expansion pass %d added nothing new", pass_number)
            break

        logger.info(
            "expansion pass %d: %d -> %d words", pass_number, words, len(combined.split())
        )
        body = combined

    return body


def _headline_for(model: LocalModel, topic: Topic, body: str) -> tuple[str, str]:
    prompt = (
        f"Beat: {topic.category.name}\n\n"
        f"Article:\n{body[:3500]}"
    )
    try:
        result = model.chat_json(_HEADLINE_SYSTEM, prompt, temperature=0.3)
    except LocalModelError as exc:
        logger.warning("headline failed for topic %s: %s", topic.pk, exc)
        return "", ""

    title = _clean_text(result.get("headline"), 240)
    summary = _clean_text(result.get("summary"), 380)
    if not summary:
        summary = body.split("\n\n")[0][:380]
    return title, summary


def _dedupe_paragraphs(paragraphs: list[str]) -> str:
    """Sections sometimes restate each other; drop near-duplicates."""
    kept: list[str] = []
    seen: set[str] = set()
    for paragraph in paragraphs:
        text = paragraph.strip()
        if len(text.split()) < 5:
            continue
        key = _dedupe_key(text)
        if key in seen:
            continue
        if any(_paragraphs_overlap(text, existing) for existing in kept):
            continue
        seen.add(key)
        kept.append(text)
    return "\n\n".join(kept).strip()


def _dedupe_key(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return " ".join(words[:12])


def _paragraphs_overlap(left: str, right: str) -> bool:
    """True when two paragraphs mostly restate the same material."""
    a = left.lower().strip()
    b = right.lower().strip()
    if not a or not b:
        return False
    if SequenceMatcher(None, a, b).ratio() >= 0.48:
        return True

    aw = re.findall(r"[a-z0-9]+", a)
    bw = re.findall(r"[a-z0-9]+", b)
    if len(aw) < 12 or len(bw) < 12:
        return False

    # Shared 8-grams catch copy that starts differently but repeats a block.
    a_grams = {" ".join(aw[i : i + 8]) for i in range(len(aw) - 7)}
    b_grams = {" ".join(bw[i : i + 8]) for i in range(len(bw) - 7)}
    shared = len(a_grams & b_grams)
    smaller = min(len(a_grams), len(b_grams)) or 1
    if shared >= 3 and shared / smaller >= 0.30:
        return True

    jaccard = len(set(aw) & set(bw)) / len(set(aw) | set(bw))
    return jaccard >= 0.62


def _clean_list(value, limit: int = 20, max_len: int = 400) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        text = _clean_text(item, max_len)
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:limit]


def _clean_text(value, max_len: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split()).strip(" \"'*#")
    return text[:max_len]


def _clean_body(value) -> str:
    if not isinstance(value, str):
        if isinstance(value, list):
            value = "\n\n".join(str(v) for v in value)
        else:
            return ""

    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", text)

    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    paragraphs = [p for p in paragraphs if len(p) > 1]

    # Some models emit one line per paragraph instead of blank-line separated.
    if len(paragraphs) == 1 and "\n" in text:
        paragraphs = [" ".join(p.split()) for p in text.split("\n") if p.strip()]

    # Always store Unix paragraph breaks so the site template can split them.
    return "\n\n".join(paragraphs).strip()


def source_reference_pairs(sources: list[Source]) -> list[tuple[str, str]]:
    return [
        (source.domain or source.url, source.reference_text)
        for source in sources
        if source.reference_text.strip()
    ]


def trim_to_maximum(draft: Draft) -> int | None:
    """Drop trailing paragraphs from an overlong draft, in place.

    Cutting whole paragraphs is safe and free; asking the model to shorten its
    own copy costs another minute and usually reintroduces source phrasing.
    """
    maximum = settings.MAX_ARTICLE_WORDS
    if len(draft.body.split()) <= maximum:
        return None

    kept: list[str] = []
    total = 0
    for paragraph in draft.body.split("\n\n"):
        words = len(paragraph.split())
        if kept and total + words > maximum:
            break
        kept.append(paragraph)
        total += words

    draft.body = "\n\n".join(kept)
    return total


def feedback_for(originality_summary: str | None, safety_summary: str | None) -> str:
    notes = []
    if originality_summary:
        notes.append(
            "- Your wording was too close to the published sources "
            f"({originality_summary}). Restructure the sentences completely: "
            "change the order of information, merge and split sentences, and "
            "choose different vocabulary. Do not reuse any phrase of five or "
            "more consecutive words."
        )
    if safety_summary:
        notes.append(f"- Editorial policy problems to fix: {safety_summary}")
    return "\n".join(notes)


def attempt_budget() -> int:
    return max(settings.MAX_REWRITE_ATTEMPTS, 1)

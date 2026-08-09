"""Turn gathered sources into original copy.

The rewriter deliberately works in two stages:

1. **Extract** - the model reads the source articles and returns a structured
   list of bare facts, with all phrasing stripped away.
2. **Write** - the model composes the article from *only* that fact list. It
   never sees the source prose while writing.

Passing facts rather than text through the bottleneck is what makes the output
original: there is no source wording available to copy. Stage 2 also converts
every direct quotation into indirect speech, since verbatim quotes are both a
copyright risk and the main driver of n-gram overlap.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

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

    def render(self) -> str:
        lines = ["VERIFIED FACTS (reported by two or more sources or by an official):"]
        lines += [f"- {fact}" for fact in self.facts]
        if self.unverified:
            lines.append("")
            lines.append("UNVERIFIED / SINGLE-SOURCE CLAIMS (must be hedged):")
            lines += [f"- {claim}" for claim in self.unverified]
        if self.entities:
            lines.append("")
            lines.append("KEY NAMES: " + ", ".join(self.entities))
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
- Put a claim in "unverified" if only one outlet reports it, if it is presented
  as a rumour or speculation, or if it accuses someone of wrongdoing.
- Include concrete details: numbers, dates, places, scores, job titles.
- Do not invent anything. If the sources do not say it, leave it out.

Reply with JSON only:
{"facts": ["...", "..."], "unverified": ["..."], "entities": ["..."]}"""


_WRITE_SYSTEM = """You are a staff reporter writing original copy for a
general-audience news site. You are given a fact sheet, never source articles.

Write the article using ONLY the supplied facts.

Structure:
- Open with a single-sentence lede that answers who, what, when and where.
- Then give the supporting detail, then the reaction or context, in that order.
- Write 4 to 6 flowing paragraphs of connected prose, 200 to 320 words total.
  Anything under 200 words will be rejected, so develop each paragraph into
  two or three full sentences.
- Plain, neutral, third-person reporting voice. No "I", "we" or "our".
- No headings, no bullet points, no markdown, no emoji.

How to use the fact sheet:
- Do not walk through the facts in the order they are listed and do not echo
  their phrasing. Reorganise them by news value and write them out as prose.
- Combine related facts into single sentences with proper connective language.
- Every fact you use must be attributed the way the fact sheet attributes it.

Editorial rules you must follow:
- Attribute every unverified claim: say it is reported, alleged or unconfirmed,
  and name who is making the claim.
- Never state as fact that a person or company committed a crime or misconduct
  unless the fact sheet says a court, regulator or official body established it.
- No sexual, adult or graphic content. No slurs or demeaning generalisations.
- No phone numbers, emails, addresses or ID numbers.
- Do not speculate beyond the fact sheet and do not add invented quotations.
- Close with a short paragraph on what is still unknown or what happens next.

Reply with JSON only:
{"headline": "...", "summary": "...", "body": "paragraph\\n\\nparagraph"}

The headline must be under 110 characters, factual, and not clickbait.
The summary must be one sentence under 200 characters."""


def build_fact_sheet(model: LocalModel, topic: Topic, sources: list[Source]) -> FactSheet:
    corpus = _render_sources(sources)
    prompt = (
        f"Topic: {topic.label}\n"
        f"Beat: {topic.category.name}\n"
        f"Number of independent outlets: {topic.domain_count}\n\n"
        f"{corpus}"
    )
    try:
        result = model.chat_json(_EXTRACT_SYSTEM, prompt, temperature=0.1)
    except LocalModelError as exc:
        logger.warning("fact extraction failed for topic %s: %s", topic.pk, exc)
        return FactSheet()

    return FactSheet(
        facts=_clean_list(result.get("facts")),
        unverified=_clean_list(result.get("unverified")),
        entities=_clean_list(result.get("entities"), limit=12, max_len=80),
    )


def write_article(
    model: LocalModel,
    topic: Topic,
    fact_sheet: FactSheet,
    *,
    feedback: str = "",
    attempt: int = 1,
) -> Draft | None:
    prompt_parts = [
        f"Beat: {topic.category.name}",
        f"Working topic: {topic.label}",
        "",
        fact_sheet.render(),
    ]
    if topic.category.slug == "rumours":
        prompt_parts += [
            "",
            "This story is an unconfirmed report. Frame the entire article as "
            "reported-but-unverified, and state plainly that it has not been "
            "officially confirmed.",
        ]
    if feedback:
        prompt_parts += [
            "",
            "Your previous attempt was rejected. Fix these problems and rewrite "
            "the article from scratch in completely different words:",
            feedback,
        ]

    # Nudge the wording further away from the source on each retry.
    temperature = min(0.6 + 0.15 * (attempt - 1), 0.95)

    try:
        result = model.chat_json(
            _WRITE_SYSTEM, "\n".join(prompt_parts), temperature=temperature
        )
    except LocalModelError as exc:
        logger.warning("article writing failed for topic %s: %s", topic.pk, exc)
        return None

    title = _clean_text(result.get("headline"), 240)
    summary = _clean_text(result.get("summary"), 380)
    body = _clean_body(result.get("body"))

    if not title or not body:
        logger.info("draft for topic %s missing a headline or body", topic.pk)
        return None

    if not summary:
        summary = body.split("\n\n")[0][:380]

    return Draft(title=title, summary=summary, body=body, attempts=attempt)


def _render_sources(sources: list[Source], budget: int = 4500) -> str:
    per_source = max(budget // max(len(sources), 1), 700)
    blocks = []
    for index, source in enumerate(sources, start=1):
        text = (source.raw_content or source.snippet or "").strip()
        if not text:
            continue
        blocks.append(
            f"--- SOURCE {index} ({source.domain}) ---\n"
            f"Headline: {source.title}\n"
            f"{text[:per_source]}"
        )
    return "\n\n".join(blocks)


def _clean_list(value, limit: int = 25, max_len: int = 400) -> list[str]:
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

    text = value.replace("\r\n", "\n")
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", text)

    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    paragraphs = [p for p in paragraphs if len(p) > 1]

    # Some models emit one line per paragraph instead of blank-line separated.
    if len(paragraphs) == 1 and "\n" in text:
        paragraphs = [" ".join(p.split()) for p in text.split("\n") if p.strip()]

    return "\n\n".join(paragraphs).strip()


def source_reference_pairs(sources: list[Source]) -> list[tuple[str, str]]:
    return [
        (source.domain or source.url, source.reference_text)
        for source in sources
        if source.reference_text.strip()
    ]


def length_problem(draft: Draft) -> str | None:
    """Small models under-write, so length gets its own retry with feedback."""
    words = len(draft.body.split())
    minimum, maximum = settings.MIN_ARTICLE_WORDS, settings.MAX_ARTICLE_WORDS
    if words < minimum:
        return (
            f"- The article was only {words} words. It must be at least "
            f"{minimum} words. Keep every fact you already used, then develop "
            "each paragraph further with the remaining facts, the context and "
            "what happens next. Do not pad with repetition."
        )
    if words > maximum:
        return (
            f"- The article ran to {words} words, over the {maximum}-word limit. "
            "Cut the least newsworthy material and tighten the writing."
        )
    return None


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

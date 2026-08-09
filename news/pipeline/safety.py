"""Editorial safety gate.

Every draft passes two independent checks before it can go live:

1. Deterministic rules (this module's regex sets) - fast, predictable, and the
   only thing we trust to *block* content outright.
2. A model review pass - catches nuance the rules miss, but a small local model
   is not reliable enough to be the sole gate, so its verdict can only ever
   downgrade an article to human review.

Anything a rule blocks is rejected. Anything either check merely flags is held
in the review queue instead of being published.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from news.pipeline.llm import LocalModel, LocalModelError

logger = logging.getLogger("news.safety")


class Verdict:
    PASS = "pass"
    REVIEW = "review"
    BLOCK = "block"


@dataclass
class SafetyIssue:
    code: str
    severity: str  # Verdict.REVIEW or Verdict.BLOCK
    detail: str

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


@dataclass
class SafetyReport:
    issues: list[SafetyIssue] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if any(i.severity == Verdict.BLOCK for i in self.issues):
            return Verdict.BLOCK
        if self.issues:
            return Verdict.REVIEW
        return Verdict.PASS

    @property
    def flags(self) -> list[dict]:
        return [issue.as_dict() for issue in self.issues]

    @property
    def summary(self) -> str:
        if not self.issues:
            return "No issues detected."
        return "; ".join(f"[{i.severity}] {i.code}: {i.detail}" for i in self.issues)


# --- Rule sets ------------------------------------------------------------

# Sexual / adult material. Blocked outright: this is a general-audience site.
ADULT_PATTERNS = [
    r"\bporn(?:o|ographic|ography)?\b",
    r"\bxxx\b",
    r"\bsex tape\b",
    r"\bnude (?:photos?|pics?|images?|scenes?|leak)\b",
    r"\bnudes\b",
    r"\bonlyfans\b",
    r"\bescort service\b",
    r"\bexplicit (?:sexual|content|images?)\b",
    r"\bsexually explicit\b",
    r"\berotic\b",
    r"\bfetish\b",
]

# Content we will not host regardless of newsworthiness.
HARD_BLOCK_PATTERNS = [
    (r"\bchild (?:porn|sexual abuse material)\b", "child sexual abuse material"),
    (r"\bcsam\b", "child sexual abuse material"),
    (r"\bhow to (?:make|build) (?:a )?(?:bomb|explosive|ied)\b", "weapon instructions"),
    (r"\bhow to (?:kill|poison)\b", "violence instructions"),
    (r"\b(?:step[- ]by[- ]step|instructions?) (?:to|for) (?:suicide|self[- ]harm)\b",
     "self-harm instructions"),
]

# Accusatory language that needs attribution and proof to be publishable.
ACCUSATION_PATTERNS = [
    r"\bfrauds?ter\b", r"\bscammer\b", r"\bcorrupt\b", r"\bcriminal\b",
    r"\bcheated\b", r"\bstole\b", r"\bembezzl\w+\b", r"\bbribe[sd]?\b",
    r"\bguilty\b", r"\bmurderer\b", r"\brapist\b", r"\bpaedophile\b",
    r"\bpedophile\b", r"\bterrorist\b", r"\btraitor\b", r"\bliar\b",
    r"\bmoney launder\w*\b", r"\btax evasion\b", r"\bdrug addict\b",
    r"\bhad an affair\b", r"\bcheating on\b", r"\bis a crook\b",
]

# Markers that turn an accusation into responsible, sourced reporting.
ATTRIBUTION_MARKERS = [
    r"\balleged(?:ly)?\b", r"\baccus\w+\b", r"\bclaim\w*\b", r"\breported(?:ly)?\b",
    r"\baccording to\b", r"\bsaid\b", r"\bsays\b", r"\bstated\b", r"\bper\b",
    r"\bcourt\b", r"\bpolice\b", r"\bfir\b", r"\bcharge(?:d|s|sheet)?\b",
    r"\bindict\w*\b", r"\bconvict\w*\b", r"\bverdict\b", r"\bjudg\w+\b",
    r"\binvestigat\w+\b", r"\bprobe\b", r"\bsuspect\w*\b", r"\bdenied\b",
    r"\bofficials?\b", r"\bstatement\b", r"\bfiled\b", r"\bsources? said\b",
]

# Hedging required for anything in the rumours beat.
HEDGE_MARKERS = [
    r"\brumour\w*\b", r"\brumor\w*\b", r"\bunconfirmed\b", r"\bspeculat\w+\b",
    r"\breported(?:ly)?\b", r"\balleged(?:ly)?\b", r"\bnot (?:been )?confirmed\b",
    r"\bhas(?:n't| not) confirmed\b", r"\bno official (?:word|confirmation|statement)\b",
    r"\bclaim\w*\b", r"\baccording to\b", r"\bsuggest\w*\b", r"\bcould\b", r"\bmay\b",
]

# Personal data that must never appear in a published article.
PII_PATTERNS = [
    (r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b", "email address"),
    (r"(?<!\d)(?:\+91[\-\s]?)?[6-9]\d{9}(?!\d)", "phone number"),
    (r"\b\d{4}\s?\d{4}\s?\d{4}\b", "government ID number"),
    (r"\b(?:\d[ -]*?){13,16}\b", "payment card number"),
]

# Case matters here: "I" and "us" must stay capital-sensitive so the country
# abbreviation "US" and the pronoun "I" are not confused with each other.
FIRST_PERSON_PATTERNS = [
    r"\bI\b",
    r"\bI'(?:m|ve|ll|d)\b",
    r"\b[Ww]e\b",
    r"\b[Ww]e'(?:re|ve|ll|d)\b",
    r"\b[Oo]ur[s]?\b",
    r"\bus\b",
]


@lru_cache(maxsize=1)
def _custom_blocklist() -> list[str]:
    """Optional user-maintained term list at data/blocklist.txt (one term per line)."""
    path = Path(settings.BASE_DIR) / "data" / "blocklist.txt"
    if not path.exists():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if term and not term.startswith("#"):
            terms.append(re.escape(term))
    return terms


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _matches(patterns, text: str) -> list[str]:
    found = []
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            found.append(match.group(0))
    return found


def check_rules(
    title: str, body: str, *, category_slug: str, source_count: int
) -> SafetyReport:
    report = SafetyReport()
    text = f"{title}\n{body}"

    for pattern, label in HARD_BLOCK_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            report.issues.append(
                SafetyIssue("prohibited_content", Verdict.BLOCK, label)
            )

    adult_hits = _matches(ADULT_PATTERNS, text)
    if adult_hits:
        report.issues.append(
            SafetyIssue(
                "adult_content",
                Verdict.BLOCK,
                f"adult/explicit terms present: {', '.join(sorted(set(adult_hits))[:5])}",
            )
        )

    custom = _custom_blocklist()
    if custom:
        custom_hits = _matches([rf"\b{term}\b" for term in custom], text)
        if custom_hits:
            report.issues.append(
                SafetyIssue(
                    "blocklist_term",
                    Verdict.BLOCK,
                    f"blocklist terms present: {', '.join(sorted(set(custom_hits))[:5])}",
                )
            )

    for pattern, label in PII_PATTERNS:
        match = re.search(pattern, text)
        if match:
            report.issues.append(
                SafetyIssue("personal_data", Verdict.BLOCK, f"possible {label} in copy")
            )

    # An accusation is only publishable when the same sentence attributes it.
    for sentence in _sentences(text):
        accusations = _matches(ACCUSATION_PATTERNS, sentence)
        if not accusations:
            continue
        if not _matches(ATTRIBUTION_MARKERS, sentence):
            report.issues.append(
                SafetyIssue(
                    "unattributed_accusation",
                    Verdict.BLOCK,
                    f"'{accusations[0]}' used without attribution: "
                    f"{sentence[:140]}",
                )
            )

    if category_slug == "rumours":
        if source_count < settings.MIN_SOURCES_FOR_RUMOUR:
            report.issues.append(
                SafetyIssue(
                    "insufficient_corroboration",
                    Verdict.BLOCK,
                    f"rumour backed by only {source_count} source(s)",
                )
            )
        if not _matches(HEDGE_MARKERS, text):
            report.issues.append(
                SafetyIssue(
                    "missing_hedging",
                    Verdict.BLOCK,
                    "rumour written as established fact",
                )
            )

    if source_count == 0:
        report.issues.append(
            SafetyIssue("no_sources", Verdict.BLOCK, "no sources recorded for topic")
        )

    first_person = [p for p in FIRST_PERSON_PATTERNS if re.search(p, body)]
    if first_person:
        report.issues.append(
            SafetyIssue(
                "editorial_voice", Verdict.REVIEW, "first-person voice in a news report"
            )
        )

    word_count = len(body.split())
    if word_count < settings.MIN_ARTICLE_WORDS:
        report.issues.append(
            SafetyIssue(
                "too_short",
                Verdict.REVIEW,
                f"only {word_count} words, below the "
                f"{settings.MIN_ARTICLE_WORDS}-word minimum",
            )
        )

    return report


_REVIEW_SYSTEM = """You are a cautious news desk compliance reviewer.
You judge whether a draft article is safe to publish on a general-audience site.

Reply with JSON only, in exactly this shape:
{"safe": true/false, "severity": "none"|"low"|"high", "issues": ["short reason", ...]}

Mark it unsafe when the draft:
- contains sexual, adult or graphic content
- accuses a named person or company of a crime or misconduct without
  attributing the claim to a source, investigation or official statement
- presents a rumour or unconfirmed report as established fact
- contains hate speech, harassment, or demeaning generalisations about a group
- gives dangerous instructions, or medical/financial advice stated as certainty
- includes someone's private contact details

Facts reported neutrally and attributed to sources are safe. Be strict about
accusations, relaxed about ordinary news."""


def review_with_model(
    model: LocalModel, title: str, body: str, *, category_slug: str
) -> SafetyReport:
    """Second-opinion pass. Can only downgrade to review, never hard-block."""
    report = SafetyReport()
    prompt = (
        f"Category: {category_slug}\n"
        f"Headline: {title}\n\n"
        f"Article:\n{body[:6000]}"
    )
    try:
        result = model.chat_json(_REVIEW_SYSTEM, prompt, temperature=0.0)
    except LocalModelError as exc:
        logger.warning("model safety review unavailable: %s", exc)
        report.issues.append(
            SafetyIssue(
                "review_unavailable",
                Verdict.REVIEW,
                "model safety review could not run",
            )
        )
        return report

    safe = bool(result.get("safe", True))
    severity = str(result.get("severity", "none")).lower()
    raw_issues = result.get("issues") or []
    issues = [str(i)[:200] for i in raw_issues if str(i).strip()][:5]

    if safe and severity in {"none", "low"} and not issues:
        return report

    if not safe or severity == "high" or issues:
        detail = "; ".join(issues) if issues else f"model severity={severity}"
        report.issues.append(
            SafetyIssue("model_review", Verdict.REVIEW, detail[:280])
        )
    return report


def combine(*reports: SafetyReport) -> SafetyReport:
    merged = SafetyReport()
    for report in reports:
        merged.issues.extend(report.issues)
    return merged

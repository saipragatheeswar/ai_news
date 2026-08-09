"""Originality gate: make sure generated copy does not reuse source wording.

Copyright risk comes from reproducing expression, not facts. We therefore
measure two things against every source we consulted:

* n-gram overlap - the share of the draft's 5-word sequences that also appear
  in a source. Independent writing about the same facts lands low; paraphrase
  that merely swaps a few words lands high.
* longest common run - the single longest verbatim stretch of words. A long
  run is the shape of a copied sentence even when overall overlap looks fine.

Runs made up almost entirely of names, titles and figures are exempt. A phrase
like "Spider-Man: Brand New Day earned $355 million in its opening weekend" is
the fact itself: there is no way to report it without those words in that order,
and facts are not protected. Counting them produced retries that could never
succeed, so the run measurement skips them and keeps looking for genuine reused
phrasing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from django.conf import settings

_TOKEN_RE = re.compile(r"[a-z0-9']+")
_RAW_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")

# Words that carry no expressive weight, so they don't rescue a run from being
# classed as factual.
_FILLER = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "is", "its", "of",
    "on", "or", "the", "to", "with", "was", "were", "has", "have", "had",
    "million", "billion", "crore", "lakh", "percent", "per", "cent", "year",
    "quarter", "week", "weekend", "day", "s",
}

# Share of a run that must be plain prose for it to count as reused expression.
_EXPRESSIVE_RATIO = 0.4


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _raw_tokens(text: str) -> list[str]:
    """Tokens with capitalisation intact, aligned 1:1 with ``tokenize``."""
    return _RAW_TOKEN_RE.findall(text)


def is_factual_run(raw_tokens: list[str]) -> bool:
    """True when a matched run is names, numbers and figures rather than prose."""
    if not raw_tokens:
        return True
    expressive = 0
    for index, token in enumerate(raw_tokens):
        lowered = token.lower()
        if lowered in _FILLER or any(character.isdigit() for character in token):
            continue
        # A capitalised word mid-run is almost always part of a proper noun.
        if index > 0 and token[:1].isupper():
            continue
        expressive += 1
    return expressive / len(raw_tokens) < _EXPRESSIVE_RATIO


def ngrams(tokens: list[str], size: int) -> set[tuple[str, ...]]:
    if len(tokens) < size:
        return set()
    return {tuple(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


@dataclass
class OriginalityReport:
    max_overlap: float
    longest_run: int
    worst_source: str
    passed: bool
    longest_run_text: str = ""

    @property
    def summary(self) -> str:
        detail = (
            f"{self.max_overlap:.1%} 5-gram overlap, "
            f"longest verbatim run {self.longest_run} words"
            + (f" (vs {self.worst_source})" if self.worst_source else "")
        )
        if self.longest_run_text:
            detail += f': "{self.longest_run_text}"'
        return detail


def check(draft: str, sources: list[tuple[str, str]]) -> OriginalityReport:
    """Compare a draft against ``(label, text)`` source pairs."""
    size = settings.ORIGINALITY_NGRAM_SIZE
    draft_tokens = tokenize(draft)
    draft_raw = _raw_tokens(draft)
    draft_ngrams = ngrams(draft_tokens, size)

    max_overlap = 0.0
    longest_run = 0
    worst_source = ""
    longest_run_text = ""

    for label, text in sources:
        source_tokens = tokenize(text)
        if not source_tokens:
            continue

        overlap = 0.0
        if draft_ngrams:
            shared = draft_ngrams & ngrams(source_tokens, size)
            overlap = len(shared) / len(draft_ngrams)

        run, run_text = _longest_expressive_run(draft_tokens, draft_raw, source_tokens)

        if overlap > max_overlap or run > longest_run:
            worst_source = label
        if run > longest_run:
            longest_run_text = run_text
        max_overlap = max(max_overlap, overlap)
        longest_run = max(longest_run, run)

    passed = (
        max_overlap <= settings.MAX_NGRAM_OVERLAP
        and longest_run <= settings.MAX_LONGEST_COMMON_RUN
    )
    return OriginalityReport(
        max_overlap=round(max_overlap, 4),
        longest_run=longest_run,
        worst_source=worst_source,
        passed=passed,
        longest_run_text=longest_run_text,
    )


def _longest_expressive_run(
    draft_tokens: list[str], draft_raw: list[str], source_tokens: list[str]
) -> tuple[int, str]:
    """Longest shared run that is actual phrasing rather than names and figures."""
    matcher = SequenceMatcher(None, draft_tokens, source_tokens, autojunk=False)
    best_size = 0
    best_text = ""

    for block in matcher.get_matching_blocks():
        if block.size <= best_size:
            continue
        raw = draft_raw[block.a : block.a + block.size]
        if len(raw) == block.size and is_factual_run(raw):
            continue
        best_size = block.size
        best_text = " ".join(raw)

    return best_size, best_text

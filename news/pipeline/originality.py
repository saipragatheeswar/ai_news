"""Originality gate: make sure generated copy does not reuse source wording.

Copyright risk comes from reproducing expression, not facts. We therefore
measure two things against every source we consulted:

* n-gram overlap - the share of the draft's 5-word sequences that also appear
  in a source. Independent writing about the same facts lands low; paraphrase
  that merely swaps a few words lands high.
* longest common run - the single longest verbatim stretch of words. A long
  run is the shape of a copied sentence even when overall overlap looks fine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from django.conf import settings

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


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

    @property
    def summary(self) -> str:
        return (
            f"{self.max_overlap:.1%} 5-gram overlap, "
            f"longest verbatim run {self.longest_run} words"
            + (f" (vs {self.worst_source})" if self.worst_source else "")
        )


def check(draft: str, sources: list[tuple[str, str]]) -> OriginalityReport:
    """Compare a draft against ``(label, text)`` source pairs."""
    size = settings.ORIGINALITY_NGRAM_SIZE
    draft_tokens = tokenize(draft)
    draft_ngrams = ngrams(draft_tokens, size)

    max_overlap = 0.0
    longest_run = 0
    worst_source = ""

    for label, text in sources:
        source_tokens = tokenize(text)
        if not source_tokens:
            continue

        overlap = 0.0
        if draft_ngrams:
            shared = draft_ngrams & ngrams(source_tokens, size)
            overlap = len(shared) / len(draft_ngrams)

        matcher = SequenceMatcher(None, draft_tokens, source_tokens, autojunk=False)
        run = matcher.find_longest_match(
            0, len(draft_tokens), 0, len(source_tokens)
        ).size

        if overlap > max_overlap or run > longest_run:
            if overlap >= max_overlap and run >= longest_run:
                worst_source = label
            elif not worst_source:
                worst_source = label
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
    )

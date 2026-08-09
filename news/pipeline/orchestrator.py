"""End-to-end daily run: discover topics, write articles, apply gates, publish."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from news.models import Article, Attribution, PipelineRun, Source, Topic
from news.pipeline import (
    images,
    originality,
    rewriter,
    safety,
    topics as topic_discovery,
)
from news.pipeline.llm import LocalModel
from news.pipeline.tavily_client import NewsSearch

logger = logging.getLogger("news.orchestrator")


@dataclass
class RunResult:
    published: int = 0
    held: int = 0
    rejected: int = 0
    failed: int = 0
    topics_discovered: int = 0

    @property
    def total_written(self) -> int:
        return self.published + self.held + self.rejected


class Pipeline:
    def __init__(
        self,
        *,
        search: NewsSearch | None = None,
        model: LocalModel | None = None,
        auto_publish: bool | None = None,
    ):
        self.search = search or NewsSearch()
        self.model = model or LocalModel()
        self.auto_publish = (
            settings.AUTO_PUBLISH if auto_publish is None else auto_publish
        )

    # --- public entry points ---------------------------------------------

    def run_daily(self, target: int | None = None) -> RunResult:
        target = target or settings.DAILY_ARTICLE_TARGET
        run = PipelineRun.objects.create(
            run_for=timezone.localdate(), target_count=target
        )
        result = RunResult()

        try:
            self.model.health_check()
            result.topics_discovered = self.ensure_topics(target)
            self.write_pending(target, result)
        except Exception as exc:
            logger.exception("daily run failed")
            run.status = PipelineRun.Status.FAILED
            run.notes = f"{type(exc).__name__}: {exc}"[:2000]
        else:
            if result.published >= target:
                run.status = PipelineRun.Status.SUCCESS
            elif result.published > 0:
                run.status = PipelineRun.Status.PARTIAL
                run.notes = (
                    f"Published {result.published} of {target} target articles."
                )
            else:
                run.status = PipelineRun.Status.FAILED
                run.notes = "No articles cleared the editorial gates."
        finally:
            run.topics_discovered = result.topics_discovered
            run.articles_published = result.published
            run.articles_held = result.held
            run.articles_rejected = result.rejected
            run.finished_at = timezone.now()
            run.save()

        logger.info(
            "run complete: published=%d held=%d rejected=%d failed=%d",
            result.published,
            result.held,
            result.rejected,
            result.failed,
        )
        return result

    def ensure_topics(self, target: int) -> int:
        """Discover topics unless today already has enough unwritten candidates."""
        available = self.pending_topics().count()
        if available >= target:
            logger.info("reusing %d already-discovered topics", available)
            return 0
        discovered = topic_discovery.discover(
            self.search, target=target - available
        )
        return len(discovered)

    def pending_topics(self):
        return Topic.objects.filter(
            discovered_for=timezone.localdate(),
            status__in=[Topic.Status.DISCOVERED, Topic.Status.SELECTED],
        ).select_related("category")

    def write_pending(self, target: int, result: RunResult) -> None:
        published_today = Article.objects.filter(
            status=Article.Status.PUBLISHED,
            published_at__date=timezone.localdate(),
        ).count()
        if published_today >= target:
            logger.info("target already met for today (%d articles)", published_today)
            return

        remaining = target - published_today
        for topic in self.pending_topics().order_by("-heat_score"):
            if result.published >= remaining:
                break
            try:
                self.process_topic(topic, result)
            except Exception:
                logger.exception("topic %s crashed the writer", topic.pk)
                result.failed += 1
                topic.status = Topic.Status.FAILED
                topic.skip_reason = "unexpected error while writing"
                topic.save(update_fields=["status", "skip_reason"])

    # --- per-topic work ---------------------------------------------------

    def process_topic(self, topic: Topic, result: RunResult) -> Article | None:
        logger.info("writing topic #%s: %s", topic.pk, topic.label)

        topic_discovery.enrich_sources(self.search, topic)
        sources = list(topic.sources.all()[: settings.SOURCES_PER_TOPIC])
        usable = [s for s in sources if s.reference_text.strip()]
        if not usable:
            return self._skip(topic, "no readable source text", result)

        if (
            topic.category.slug == "rumours"
            and topic.domain_count < settings.MIN_SOURCES_FOR_RUMOUR
        ):
            return self._skip(topic, "rumour lacks corroborating outlets", result)

        fact_sheet = rewriter.build_fact_sheet(self.model, topic, usable)
        if not fact_sheet.is_usable:
            return self._skip(topic, "could not extract enough facts", result)

        references = rewriter.source_reference_pairs(usable)
        feedback = ""
        last_report: safety.SafetyReport | None = None
        last_originality: originality.OriginalityReport | None = None

        for attempt in range(1, rewriter.attempt_budget() + 1):
            draft = rewriter.write_article(
                self.model, topic, fact_sheet, feedback=feedback, attempt=attempt
            )
            if draft is None:
                feedback = "- Your last response was unusable. Return valid JSON."
                continue

            # Length is handled inside the writer by adding further passes, not
            # by rewriting: a short draft needs more material, not new wording.
            # Anything still short falls through to the safety gate, which holds
            # it for review rather than discarding the work.
            if trimmed := rewriter.trim_to_maximum(draft):
                logger.info("trimmed overlong draft to %d words", trimmed)

            full_text = f"{draft.title}\n\n{draft.body}"
            originality_report = originality.check(full_text, references)
            rule_report = safety.check_rules(
                draft.title,
                draft.body,
                category_slug=topic.category.slug,
                source_count=topic.domain_count,
            )
            last_originality = originality_report
            last_report = rule_report

            if rule_report.verdict == safety.Verdict.BLOCK:
                logger.info(
                    "attempt %d blocked by rules: %s", attempt, rule_report.summary
                )
                feedback = rewriter.feedback_for(None, rule_report.summary)
                continue

            if not originality_report.passed:
                logger.info(
                    "attempt %d too close to sources: %s",
                    attempt,
                    originality_report.summary,
                )
                feedback = rewriter.feedback_for(originality_report.summary, None)
                continue

            model_report = safety.review_with_model(
                self.model, draft.title, draft.body, category_slug=topic.category.slug
            )
            combined = safety.combine(rule_report, model_report)
            article = self._store(
                topic, draft, combined, originality_report, usable, result
            )
            self._attach_image(article, topic, fact_sheet)
            return article

        # Every attempt failed a gate: keep the last draft for a human to see.
        reason_parts = []
        if last_report and last_report.issues:
            reason_parts.append(last_report.summary)
        if last_originality and not last_originality.passed:
            reason_parts.append(f"originality: {last_originality.summary}")
        return self._skip(
            topic,
            "; ".join(reason_parts) or "no draft cleared the editorial gates",
            result,
        )

    @transaction.atomic
    def _store(
        self,
        topic: Topic,
        draft: rewriter.Draft,
        report: safety.SafetyReport,
        originality_report: originality.OriginalityReport,
        sources: list[Source],
        result: RunResult,
    ) -> Article:
        if report.verdict == safety.Verdict.BLOCK:
            status = Article.Status.REJECTED
        elif report.verdict == safety.Verdict.REVIEW or not self.auto_publish:
            status = Article.Status.NEEDS_REVIEW
        else:
            status = Article.Status.PUBLISHED

        notes = [f"Originality: {originality_report.summary}"]
        if report.issues:
            notes.append(f"Safety: {report.summary}")
        if not self.auto_publish and report.verdict == safety.Verdict.PASS:
            notes.append("Held because AUTO_PUBLISH is disabled.")

        article = Article.objects.create(
            topic=topic,
            category=topic.category,
            title=draft.title[:250],
            summary=draft.summary[:400],
            body=draft.body,
            status=status,
            safety_flags=report.flags,
            review_notes="\n".join(notes)[:4000],
            max_ngram_overlap=originality_report.max_overlap,
            longest_common_run=originality_report.longest_run,
            attempts=draft.attempts,
            model_name=self.model.model,
        )
        for source in sources:
            Attribution.objects.update_or_create(
                article=article,
                url=source.url,
                defaults={"title": source.title[:500], "domain": source.domain[:200]},
            )

        topic.status = Topic.Status.WRITTEN
        topic.save(update_fields=["status"])

        if status == Article.Status.PUBLISHED:
            result.published += 1
            logger.info("published: %s", article.title)
        elif status == Article.Status.NEEDS_REVIEW:
            result.held += 1
            logger.info("held for review: %s (%s)", article.title, report.summary)
        else:
            result.rejected += 1
            logger.info("rejected: %s (%s)", article.title, report.summary)
        return article

    def _attach_image(
        self, article: Article, topic: Topic, fact_sheet: rewriter.FactSheet
    ) -> None:
        """Best-effort illustration. Never fail an article over a missing image."""
        if not settings.FETCH_IMAGES:
            return
        try:
            images.attach_image(
                article,
                images.queries_for(
                    topic.label, fact_sheet.entities, topic.category.name
                ),
            )
        except Exception:
            logger.exception("image lookup failed for article %s", article.pk)

    def _skip(self, topic: Topic, reason: str, result: RunResult) -> None:
        logger.info("skipping topic #%s: %s", topic.pk, reason)
        topic.status = Topic.Status.SKIPPED
        topic.skip_reason = reason[:300]
        topic.save(update_fields=["status", "skip_reason"])
        result.rejected += 1
        return None

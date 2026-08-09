"""Exercise the writing and gate pipeline on fixture sources.

Useful for checking that the local model, the originality gate and the safety
rules all behave, without spending Tavily credits or waiting for a full run.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from news.models import Category, Source, Topic
from news.pipeline import originality, rewriter, safety
from news.pipeline.llm import LocalModel, LocalModelError

FIXTURES = {
    "sports": {
        "label": "City edge past United in stoppage time to reach cup semi-final",
        "sources": [
            (
                "examplesport.com",
                "City reach semi-final after late winner",
                "City booked a place in the cup semi-final on Tuesday night with a "
                "2-1 win over United at a soaked Riverside Stadium. Substitute Dara "
                "Okafor struck in the third minute of stoppage time, turning in a "
                "low cross from the left after United failed to clear a corner. "
                "United had led at the interval through a first-half penalty from "
                "captain Milan Petrov, awarded after a handball on the line. City "
                "levelled on 68 minutes when defender Ana Ruiz headed home. City "
                "manager Teresa Whitlock said her side deserved the result for "
                "their persistence. The attendance was given as 41,208. City will "
                "meet the winner of Wednesday's tie in the semi-final next month.",
            ),
            (
                "matchreportdaily.com",
                "Okafor's late strike sends City through",
                "A stoppage-time goal from Dara Okafor sent City into the cup "
                "semi-finals and left United to reflect on a lead they could not "
                "protect. Petrov converted from the spot on 23 minutes after a "
                "handball, but Ruiz equalised with a header just after the hour. "
                "United had two players booked in the closing stages. Their "
                "manager, Louis Brennan, described the defeat as cruel and said "
                "the squad would regroup for the league fixture on Saturday. "
                "Okafor, 21, had only come on in the 74th minute. It was his "
                "fourth goal of the season in all competitions.",
            ),
        ],
    },
    "technology": {
        "label": "Regulator opens inquiry into data handling at a large app developer",
        "sources": [
            (
                "exampletech.com",
                "Regulator opens data inquiry into app developer",
                "The national data protection authority said on Monday it has "
                "opened a formal inquiry into how a large mobile app developer "
                "collects and shares location data. The regulator said the "
                "inquiry follows complaints from two consumer groups filed in "
                "March. It will examine whether users gave valid consent and "
                "whether data was shared with advertising partners without "
                "disclosure. The company said in a statement that it complies "
                "with the law and will cooperate fully. No findings have been "
                "made and no penalty has been proposed. The authority said "
                "inquiries of this type typically take nine to twelve months.",
            ),
            (
                "policywatch.example.org",
                "Consumer groups welcome data inquiry",
                "Consumer groups have welcomed the data protection authority's "
                "decision to investigate location data practices at a major app "
                "developer. The groups allege that consent screens were designed "
                "to steer users toward accepting tracking. The developer denies "
                "wrongdoing. The authority confirmed the inquiry is at a "
                "preliminary stage and stressed that no conclusion has been "
                "reached about whether any rule was broken.",
            ),
        ],
    },
}


class Command(BaseCommand):
    help = "Write one article from built-in fixture sources and show the gate results."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fixture",
            choices=sorted(FIXTURES),
            default="sports",
            help="Which fixture story to write.",
        )
        parser.add_argument(
            "--keep",
            action="store_true",
            help="Keep the fixture topic in the database instead of deleting it.",
        )

    def handle(self, *args, **options):
        fixture = FIXTURES[options["fixture"]]
        model = LocalModel()

        try:
            model.health_check()
        except LocalModelError as exc:
            raise CommandError(str(exc)) from exc

        category, _ = Category.objects.get_or_create(
            slug=options["fixture"], defaults={"name": options["fixture"].title()}
        )
        topic = Topic.objects.create(
            label=fixture["label"],
            fingerprint=f"demo-{timezone.now().timestamp()}",
            category=category,
            heat_score=99.0,
            domain_count=len(fixture["sources"]),
            source_count=len(fixture["sources"]),
        )
        for domain, title, text in fixture["sources"]:
            Source.objects.create(
                topic=topic,
                url=f"https://{domain}/story",
                domain=domain,
                title=title,
                snippet=text[:300],
                raw_content=text,
                relevance=0.9,
            )

        try:
            self._write(model, topic)
        finally:
            if not options["keep"]:
                topic.delete()

    def _write(self, model: LocalModel, topic: Topic) -> None:
        sources = list(topic.sources.all())

        self.stdout.write("Extracting facts...")
        fact_sheet = rewriter.build_fact_sheet(model, topic, sources)
        if not fact_sheet.is_usable:
            raise CommandError(
                "Fact extraction produced too little. Try a larger model."
            )
        for fact in fact_sheet.facts:
            self.stdout.write(f"  - {fact}")
        for claim in fact_sheet.unverified:
            self.stdout.write(self.style.WARNING(f"  ? {claim}"))

        references = rewriter.source_reference_pairs(sources)
        feedback = ""

        for attempt in range(1, rewriter.attempt_budget() + 1):
            self.stdout.write("")
            self.stdout.write(f"Writing (attempt {attempt})...")
            draft = rewriter.write_article(
                model, topic, fact_sheet, feedback=feedback, attempt=attempt
            )
            if draft is None:
                feedback = "- Your last response was unusable. Return valid JSON."
                self.stdout.write(self.style.ERROR("  unusable response"))
                continue

            if trimmed := rewriter.trim_to_maximum(draft):
                self.stdout.write(f"  trimmed to {trimmed} words")

            full_text = f"{draft.title}\n\n{draft.body}"
            originality_report = originality.check(full_text, references)
            rule_report = safety.check_rules(
                draft.title,
                draft.body,
                category_slug=topic.category.slug,
                source_count=topic.domain_count,
            )

            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING(draft.title))
            self.stdout.write(f"  {draft.summary}")
            self.stdout.write("")
            for paragraph in draft.body.split("\n\n"):
                self.stdout.write(f"  {paragraph}")
            self.stdout.write("")
            self.stdout.write(f"  words       : {len(draft.body.split())}")
            self.stdout.write(f"  originality : {originality_report.summary}")
            self.stdout.write(f"  safety      : {rule_report.summary}")

            if rule_report.verdict == safety.Verdict.BLOCK:
                feedback = rewriter.feedback_for(None, rule_report.summary)
                self.stdout.write(self.style.ERROR("  -> blocked, rewriting"))
                continue
            if not originality_report.passed:
                feedback = rewriter.feedback_for(originality_report.summary, None)
                self.stdout.write(self.style.ERROR("  -> too close to sources, rewriting"))
                continue

            self.stdout.write("")
            self.stdout.write("Running model safety review...")
            model_report = safety.review_with_model(
                model, draft.title, draft.body, category_slug=topic.category.slug
            )
            combined = safety.combine(rule_report, model_report)
            verdict = combined.verdict
            style = {
                safety.Verdict.PASS: self.style.SUCCESS,
                safety.Verdict.REVIEW: self.style.WARNING,
                safety.Verdict.BLOCK: self.style.ERROR,
            }[verdict]
            self.stdout.write(style(f"  verdict: {verdict} - {combined.summary}"))
            return

        self.stdout.write(self.style.ERROR("No draft cleared the gates."))

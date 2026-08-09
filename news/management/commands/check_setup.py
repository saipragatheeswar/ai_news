"""Verify that the search API and the local model are both reachable."""

from django.conf import settings
from django.core.management.base import BaseCommand

from news.pipeline.llm import LocalModel, LocalModelError
from news.pipeline.tavily_client import NewsSearch, TavilyNotConfigured


class Command(BaseCommand):
    help = "Check Tavily and Ollama connectivity before running the pipeline."

    def handle(self, *args, **options):
        ok = True

        self.stdout.write("Tavily search API")
        if not settings.TAVILY_API_KEY:
            self.stdout.write(
                self.style.ERROR("  TAVILY_API_KEY is not set in .env")
            )
            ok = False
        else:
            try:
                hits = NewsSearch().search("technology news today", max_results=3)
            except TavilyNotConfigured as exc:
                self.stdout.write(self.style.ERROR(f"  {exc}"))
                ok = False
            else:
                if hits:
                    self.stdout.write(
                        self.style.SUCCESS(f"  OK - {len(hits)} results")
                    )
                    for hit in hits:
                        self.stdout.write(f"    {hit.domain}: {hit.title[:70]}")
                else:
                    self.stdout.write(
                        self.style.ERROR("  No results returned; check the API key.")
                    )
                    ok = False

        self.stdout.write("")
        self.stdout.write(f"Local model ({settings.OLLAMA_MODEL})")
        model = LocalModel()
        try:
            model.health_check()
        except LocalModelError as exc:
            self.stdout.write(self.style.ERROR(f"  {exc}"))
            ok = False
        else:
            try:
                reply = model.chat(
                    "You reply with a single word.",
                    "Reply with the word: ready",
                    temperature=0.0,
                )
            except LocalModelError as exc:
                self.stdout.write(self.style.ERROR(f"  generation failed: {exc}"))
                ok = False
            else:
                self.stdout.write(self.style.SUCCESS(f"  OK - replied {reply[:40]!r}"))

        self.stdout.write("")
        self.stdout.write("Openly-licensed image search")
        if not settings.FETCH_IMAGES:
            self.stdout.write("  disabled (FETCH_IMAGES=0)")
        else:
            from news.pipeline import images

            found = False
            for finder in (images._search_openverse, images._search_wikimedia):
                name = finder.__name__.replace("_search_", "")
                try:
                    candidate = finder("parliament building")
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f"  {name}: failed ({exc})"))
                    continue
                if candidate:
                    found = True
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  {name}: OK - {candidate.licence} by {candidate.credit}"
                        )
                    )
                else:
                    self.stdout.write(f"  {name}: no usable result")
            if not found:
                self.stdout.write(
                    self.style.WARNING(
                        "  No image provider responded; articles will use "
                        "generated category cards instead."
                    )
                )

        self.stdout.write("")
        if ok:
            self.stdout.write(
                self.style.SUCCESS("Setup looks good. Run: python manage.py run_daily")
            )
        else:
            self.stdout.write(self.style.ERROR("Fix the errors above, then re-run."))

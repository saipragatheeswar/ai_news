"""Tests for the editorial gates and topic clustering."""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from news.models import Article, Category, Topic
from news.pipeline import originality, rewriter, safety, topics
from news.pipeline.llm import extract_json


class OriginalityTests(TestCase):
    source = (
        "The central bank raised its benchmark interest rate by a quarter point "
        "on Tuesday, citing stubborn food inflation and a weaker rupee."
    )

    def test_verbatim_copy_is_rejected(self):
        report = originality.check(self.source, [("example.com", self.source)])
        self.assertFalse(report.passed)
        self.assertGreater(report.max_overlap, 0.5)

    def test_independent_wording_passes(self):
        draft = (
            "Policymakers lifted borrowing costs at their meeting this week. "
            "Officials pointed to persistent pressure on food prices and a "
            "currency that has lost ground as the reasons behind the decision. "
            "Households with floating-rate loans will feel the change first."
        )
        report = originality.check(draft, [("example.com", self.source)])
        self.assertTrue(report.passed, report.summary)

    def test_long_verbatim_sentence_is_caught(self):
        draft = (
            "Borrowing costs went up again. The central bank raised its "
            "benchmark interest rate by a quarter point on Tuesday, citing "
            "stubborn food inflation. Analysts expect another move later."
        )
        report = originality.check(draft, [("example.com", self.source)])
        self.assertFalse(report.passed)
        self.assertGreaterEqual(
            report.longest_run, 10, f"longest run was {report.longest_run}"
        )


# Neutral filler so test bodies clear the 120-word minimum length rule and each
# test exercises only the condition it names.
FILLER = (
    "The department published the timetable on its website the same afternoon. "
    "Ticket prices will stay at existing levels for the first three months, "
    "after which a review is scheduled. Local traders welcomed the change and "
    "said they expected more footfall on weekends. A spokesperson confirmed "
    "that two additional vehicles have been assigned to the route to cope with "
    "peak demand. Residents in the surrounding villages had petitioned for the "
    "service for close to four years. Officials added that a further extension "
    "towards the industrial estate is under consideration but has not yet been "
    "funded. Work on the connecting stretch of road is expected to continue "
    "through the rest of the quarter, weather permitting, and updated timings "
    "will be shared once that work finishes."
)


def pad(body: str) -> str:
    """Bring a short test body up to publishable length.

    Repeats as needed so the helper keeps working if the minimum changes.
    """
    text = body
    while len(text.split()) < settings.MIN_ARTICLE_WORDS + 20:
        text = f"{text} {FILLER}"
    return text


class SafetyRuleTests(TestCase):
    body = (
        "The state transport department opened a new bus corridor on Monday. "
        "Officials said the route will cut travel time between the two "
        "districts by about twenty minutes. Services begin next week."
    )

    def check(self, title, body, *, category="world", sources=3):
        return safety.check_rules(
            title, pad(body), category_slug=category, source_count=sources
        )

    def test_ordinary_news_passes(self):
        report = self.check("New bus corridor opens between districts", self.body)
        self.assertEqual(report.verdict, safety.Verdict.PASS, report.summary)

    def test_adult_content_is_blocked(self):
        report = self.check("Star's sex tape leak spreads online", self.body)
        self.assertEqual(report.verdict, safety.Verdict.BLOCK)
        self.assertIn("adult_content", [i.code for i in report.issues])

    def test_unattributed_accusation_is_blocked(self):
        report = self.check(
            "Local news",
            "The mayor stole public money from the road repair fund. "
            "Residents are upset about the state of the roads.",
        )
        self.assertEqual(report.verdict, safety.Verdict.BLOCK)
        self.assertIn("unattributed_accusation", [i.code for i in report.issues])

    def test_attributed_accusation_is_allowed(self):
        report = self.check(
            "Audit flags road fund spending",
            "Investigators allege the mayor stole public money from the road "
            "repair fund, according to a state audit filed on Monday. The "
            "mayor's office denied the finding and said it will respond in "
            "writing. No charges have been filed so far.",
        )
        self.assertEqual(report.verdict, safety.Verdict.PASS, report.summary)

    def test_personal_data_is_blocked(self):
        report = self.check(
            "Contact details published",
            self.body + " Reach the officer at rakesh.kumar@example.com anytime.",
        )
        self.assertEqual(report.verdict, safety.Verdict.BLOCK)
        self.assertIn("personal_data", [i.code for i in report.issues])

    def test_rumour_without_hedging_is_blocked(self):
        report = self.check(
            "Striker joins rival club",
            "The striker has signed for the rival club. The deal is worth a "
            "record fee and he will wear the number nine shirt this season. "
            "Training begins on Thursday with the rest of the squad.",
            category="rumours",
        )
        self.assertEqual(report.verdict, safety.Verdict.BLOCK)
        self.assertIn("missing_hedging", [i.code for i in report.issues])

    def test_hedged_rumour_passes(self):
        report = self.check(
            "Reports link striker to rival club",
            "The striker is reportedly close to joining the rival club, "
            "according to two outlets covering the transfer window. Neither "
            "club has confirmed the move and the fee remains unconfirmed. "
            "Any transfer would still need a medical before it is registered.",
            category="rumours",
        )
        self.assertEqual(report.verdict, safety.Verdict.PASS, report.summary)

    def test_rumour_needs_corroboration(self):
        report = self.check(
            "Reports link striker to rival club",
            "The striker is reportedly close to a move, according to one "
            "outlet. Nothing has been confirmed by either club so far. "
            "A fee has not been agreed, sources suggest.",
            category="rumours",
            sources=1,
        )
        self.assertEqual(report.verdict, safety.Verdict.BLOCK)
        self.assertIn("insufficient_corroboration", [i.code for i in report.issues])

    def test_topic_without_sources_is_blocked(self):
        report = self.check("Anything", self.body, sources=0)
        self.assertEqual(report.verdict, safety.Verdict.BLOCK)

    def test_generic_headline_is_blocked(self):
        for title in [
            "News from Around the World",
            "World News",
            "Today's News Update",
            "Top Stories",
            "News Roundup",
        ]:
            report = self.check(title, self.body)
            self.assertEqual(
                report.verdict, safety.Verdict.BLOCK, f"should block: {title}"
            )
            self.assertIn("generic_headline", [i.code for i in report.issues])

    def test_specific_headline_is_allowed(self):
        report = self.check(
            "Transport department opens new bus corridor between districts",
            self.body,
        )
        self.assertEqual(report.verdict, safety.Verdict.PASS, report.summary)

    def test_short_body_holds_for_review(self):
        report = safety.check_rules(
            "Corridor opens", self.body, category_slug="world", source_count=3
        )
        self.assertEqual(report.verdict, safety.Verdict.REVIEW)
        self.assertIn("too_short", [i.code for i in report.issues])

    def test_first_person_only_holds_for_review(self):
        report = self.check(
            "Corridor opens",
            "We visited the new bus corridor on Monday and it looked busy. "
            "Officials said the route will cut travel time between the two "
            "districts by about twenty minutes. Services begin next week.",
        )
        self.assertEqual(report.verdict, safety.Verdict.REVIEW)


class BodyCleaningTests(TestCase):
    def test_markdown_is_stripped(self):
        cleaned = rewriter._clean_body(
            "## Heading\n\n**Bold** lead paragraph here.\n\n- a bullet point"
        )
        self.assertNotIn("#", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertIn("Bold lead paragraph here.", cleaned)

    def test_single_newline_paragraphs_are_split(self):
        cleaned = rewriter._clean_body("First para.\nSecond para.\nThird para.")
        self.assertEqual(len(cleaned.split("\n\n")), 3)

    def test_restated_paragraph_block_is_dropped(self):
        first = (
            "The numbers behind this partnership are substantial: IBM will provide "
            "its Cloud Pak for Private on OpenAI's Daybreak platform, while OpenAI "
            "Codex and ChatGPT will be integrated with IBM Consulting Advantage."
        )
        restated = (
            "The deal is expected to create new opportunities for developers. "
            "The numbers behind this partnership are substantial: IBM will provide "
            "its Cloud Pak for Private on OpenAI's Daybreak platform, while OpenAI "
            "Codex and ChatGPT will be integrated with IBM Consulting Advantage."
        )
        other = (
            "Industry experts will watch how the companies implement the new tools "
            "across regulated industries in the coming months."
        )
        cleaned = rewriter._dedupe_paragraphs([first, other, restated])
        parts = cleaned.split("\n\n")
        self.assertEqual(len(parts), 2)
        self.assertIn(first, cleaned)
        self.assertIn(other, cleaned)
        self.assertNotIn("new opportunities for developers", cleaned)


class ClusteringTests(TestCase):
    def test_similar_headlines_cluster_together(self):
        from news.pipeline.tavily_client import SearchHit

        hits = [
            SearchHit(
                title="Central bank raises interest rate by quarter point - Reuters",
                url="https://reuters.com/a",
                content="",
                score=0.9,
            ),
            SearchHit(
                title="Interest rate raised quarter point by central bank",
                url="https://bbc.co.uk/b",
                content="",
                score=0.8,
            ),
            SearchHit(
                title="Monsoon floods displace thousands in coastal districts",
                url="https://thehindu.com/c",
                content="",
                score=0.7,
            ),
        ]
        clusters = topics.cluster_hits({"world": hits})
        self.assertEqual(len(clusters), 2)
        biggest = max(clusters, key=lambda c: len(c.hits))
        self.assertEqual(len(biggest.hits), 2)
        self.assertEqual(len(biggest.domains), 2)

    def test_roundup_and_index_headlines_are_rejected(self):
        for headline in [
            "Video. Latest news bulletin | August 9th, 2026",
            "Today News Headlines for School Assembly, August 3, 2026",
            "Stock Market News Today - NYSE, NASDAQ & OTC Headlines",
            "Ukraine News Today: Breaking Updates & Live Coverage",
            "Licensable picture: World News - August 6, 2026",
            "5 Box Office Bombs From The '70s That Changed Hollywood",
            "The Best-Performing Stocks in 2026 By One-Year Returns",
            "Top 10 gadgets you can buy this year",
            "Morning briefing: what to know before the bell",
            "IPL 2026 points table and full schedule",
        ]:
            self.assertFalse(
                topics.is_story_headline(headline), f"should reject: {headline}"
            )

    def test_real_story_headlines_are_kept(self):
        for headline in [
            "US President Trump warns Iran's power plants will be destroyed",
            "Man City eye Chelsea winger Pedro Neto in January window",
            "OpenAI acquires startup that builds presentation software",
            "Korea's film industry grapples over six-month holdback rule",
            "Central bank raises benchmark rate by quarter point",
            "Floods displace thousands across two coastal districts",
        ]:
            self.assertTrue(
                topics.is_story_headline(headline), f"should keep: {headline}"
            )

    def test_non_story_headlines_never_become_clusters(self):
        from news.pipeline.tavily_client import SearchHit

        hits = [
            SearchHit(
                title="Video. Latest news bulletin | August 9th, 2026",
                url="https://example.com/a",
                content="",
                score=0.99,
            ),
            SearchHit(
                title="Central bank raises benchmark rate by quarter point",
                url="https://example.com/b",
                content="",
                score=0.5,
            ),
        ]
        clusters = topics.cluster_hits({"world": hits})
        self.assertEqual(len(clusters), 1)
        self.assertIn("Central bank", clusters[0].label)

    def test_outlet_suffix_is_stripped(self):
        self.assertEqual(
            topics.clean_headline("Something big happened today - BBC News"),
            "Something big happened today",
        )

    def test_near_duplicate_spider_man_style_titles(self):
        Category.objects.get_or_create(
            slug="entertainment", defaults={"name": "Entertainment"}
        )
        cat = Category.objects.get(slug="entertainment")
        Article.objects.create(
            category=cat,
            title="Spider-Man: Brand New Day Shatters Box Office Records",
            slug="spider-man-shatters",
            summary="s",
            body="word " * 50,
            status=Article.Status.PUBLISHED,
            published_at=timezone.now(),
        )
        match = topics.find_similar_coverage(
            "Spider-Man: Brand New Day Smashes Records"
        )
        self.assertIsNotNone(match)
        self.assertIn("Spider-Man", match)
        self.assertIsNone(
            topics.find_similar_coverage(
                "Federal Reserve holds interest rates steady in September"
            )
        )


class ModelTests(TestCase):
    def test_fingerprint_ignores_word_order_and_stopwords(self):
        a = Topic.build_fingerprint("Central bank raises interest rate today")
        b = Topic.build_fingerprint("Interest rate raises by the central bank")
        self.assertEqual(a, b)

    def test_different_stories_get_different_fingerprints(self):
        a = Topic.build_fingerprint("Central bank raises interest rate")
        b = Topic.build_fingerprint("Monsoon floods displace thousands")
        self.assertNotEqual(a, b)

    def test_publishing_sets_timestamp_and_word_count(self):
        category = Category.objects.create(name="World", slug="world")
        article = Article.objects.create(
            category=category,
            title="A test headline",
            body="One two three.\n\nFour five.",
            status=Article.Status.PUBLISHED,
        )
        self.assertIsNotNone(article.published_at)
        self.assertEqual(article.word_count, 5)
        self.assertEqual(article.slug, "a-test-headline")
        self.assertEqual(len(article.paragraphs), 2)

    def test_crlf_paragraphs_are_split_for_display(self):
        category = Category.objects.create(name="World", slug="world")
        article = Article.objects.create(
            category=category,
            title="CRLF body",
            body="First paragraph here.\r\n\r\nSecond paragraph here.\r\n\r\nThird one.",
            status=Article.Status.PUBLISHED,
        )
        self.assertEqual(len(article.paragraphs), 3)
        self.assertEqual(article.paragraphs[0], "First paragraph here.")

    def test_slugs_stay_unique(self):
        category = Category.objects.create(name="World", slug="world")
        first = Article.objects.create(
            category=category, title="Same headline", body="Body text here."
        )
        second = Article.objects.create(
            category=category, title="Same headline", body="Different body text."
        )
        self.assertNotEqual(first.slug, second.slug)


class ViewTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Sports", slug="sports")
        self.live = Article.objects.create(
            category=self.category,
            title="Published story about a match",
            summary="A short summary of the match.",
            body="First paragraph of the report.\n\nSecond paragraph of the report.",
            status=Article.Status.PUBLISHED,
        )
        self.held = Article.objects.create(
            category=self.category,
            title="Held story awaiting review",
            body="This one has not been approved yet.",
            status=Article.Status.NEEDS_REVIEW,
        )

    def test_home_lists_only_published(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.live.title)
        self.assertNotContains(response, self.held.title)
        self.assertContains(response, 'name="q"')  # header search

    def test_search_finds_published_by_title(self):
        response = self.client.get("/search/", {"q": "match"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.live.title)
        self.assertNotContains(response, self.held.title)

    def test_search_ignores_short_queries(self):
        response = self.client.get("/search/", {"q": "a"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["articles"]), 0)

    def test_home_shows_lead_plus_remaining_stories(self):
        for index in range(4):
            Article.objects.create(
                category=self.category,
                title=f"Extra published story number {index}",
                summary="Summary text.",
                body="Body paragraph text here.",
                status=Article.Status.PUBLISHED,
            )
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        # The front page splits into a lead, two secondary cards, then the rest.
        self.assertIsNotNone(response.context["lead"])
        self.assertEqual(len(response.context["secondary"]), 2)
        self.assertEqual(len(response.context["articles"]), 2)
        for index in range(4):
            self.assertContains(response, f"Extra published story number {index}")

    def test_published_article_is_readable(self):
        response = self.client.get(self.live.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Second paragraph of the report.")

    def test_held_article_is_hidden_from_the_public(self):
        response = self.client.get(self.held.get_absolute_url())
        self.assertEqual(response.status_code, 404)

    def test_staff_can_preview_held_article(self):
        User.objects.create_user("editor", password="pw12345!", is_staff=True)
        self.client.login(username="editor", password="pw12345!")
        response = self.client.get(self.held.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Preview only")

    def test_category_page_renders(self):
        response = self.client.get(self.category.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.live.title)

    def test_feed_and_sitemap_render(self):
        self.assertEqual(self.client.get("/feed/").status_code, 200)
        self.assertEqual(self.client.get("/sitemap.xml").status_code, 200)

    def test_robots_txt_points_at_sitemap(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Sitemap:", response.content)
        self.assertIn(b"/sitemap.xml", response.content)

    def test_policy_page_renders(self):
        self.assertEqual(self.client.get("/about/").status_code, 200)

    def test_legal_and_contact_pages_render(self):
        for path in ("/privacy/", "/terms/", "/contact/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertContains(response, "MobilesHub360.com")
            self.assertContains(response, "@")

    def test_status_page_requires_staff(self):
        self.assertEqual(self.client.get("/status/").status_code, 302)
        User.objects.create_user("ed", password="pw12345!", is_staff=True)
        self.client.login(username="ed", password="pw12345!")
        self.assertEqual(self.client.get("/status/").status_code, 200)

    def test_public_home_hides_staff_links(self):
        response = self.client.get("/")
        self.assertNotContains(response, "Pipeline status")
        self.assertNotContains(response, ">Desk</a>")


class JsonRecoveryTests(TestCase):
    """Small models run out of tokens mid-array; salvage what they did produce."""

    def test_plain_json(self):
        self.assertEqual(extract_json('{"facts": ["one"]}'), {"facts": ["one"]})

    def test_fenced_json(self):
        self.assertEqual(
            extract_json('```json\n{"facts": ["one"]}\n```'), {"facts": ["one"]}
        )

    def test_json_embedded_in_prose(self):
        raw = 'Sure! Here you go:\n{"facts": ["one", "two"]}\nHope that helps.'
        self.assertEqual(extract_json(raw), {"facts": ["one", "two"]})

    def test_truncated_array_is_recovered(self):
        raw = '{"facts": [\n"Revenue rose 48% to $22.97 billion.",\n"Guidance was raised.",\n"The company recorded a charge of'
        recovered = extract_json(raw)
        self.assertIsNotNone(recovered)
        self.assertEqual(
            recovered["facts"],
            ["Revenue rose 48% to $22.97 billion.", "Guidance was raised."],
        )

    def test_truncated_after_complete_element(self):
        raw = '{"facts": ["One complete fact.", "Another one.",'
        recovered = extract_json(raw)
        self.assertEqual(recovered["facts"], ["One complete fact.", "Another one."])

    def test_unrecoverable_returns_none(self):
        self.assertIsNone(extract_json("I cannot help with that request."))


class OriginalityEntityTests(TestCase):
    """Facts are not copyrightable, so shared names and figures must not fail a draft."""

    def test_factual_run_is_not_treated_as_copying(self):
        source = (
            "Spider-Man: Brand New Day earned $355 million in its opening weekend "
            "at the North American box office, distributor Sony said on Sunday."
        )
        draft = (
            "Spider-Man: Brand New Day earned $355 million in its opening weekend, "
            "a figure confirmed by the studio behind the release."
        )
        report = originality.check(draft, [("sony.example", source)])
        self.assertLessEqual(report.longest_run, 9)

    def test_reused_prose_is_still_caught(self):
        source = (
            "In a move that surprised nearly everyone watching the industry, the "
            "decision arrived after weeks of quiet negotiation behind closed doors."
        )
        draft = (
            "In a move that surprised nearly everyone watching the industry, the "
            "decision arrived after weeks of quiet negotiation behind closed doors."
        )
        report = originality.check(draft, [("example.com", source)])
        self.assertGreater(report.longest_run, 9)
        self.assertFalse(report.passed)
        self.assertIn("surprised nearly everyone", report.longest_run_text)

    def test_factual_run_classifier(self):
        factual = "Brand New Day earned 355 million in its opening weekend".split()
        prose = "the decision arrived after weeks of quiet negotiation".split()
        self.assertTrue(originality.is_factual_run(factual))
        self.assertFalse(originality.is_factual_run(prose))


class ViewCountingTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="World", slug="world")
        self.article = Article.objects.create(
            category=self.category,
            title="A story readers might open",
            body="Body copy.",
            status=Article.Status.PUBLISHED,
        )

    def read(self, agent="Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36"):
        return self.client.get(
            self.article.get_absolute_url(), headers={"user-agent": agent}
        )

    def test_reader_increments_view_count(self):
        self.read()
        self.read()
        self.article.refresh_from_db()
        self.assertEqual(self.article.view_count, 2)
        self.assertIsNotNone(self.article.last_viewed_at)

    def test_bots_are_not_counted(self):
        self.read(agent="Googlebot/2.1 (+http://www.google.com/bot.html)")
        self.read(agent="python-requests/2.32")
        self.article.refresh_from_db()
        self.assertEqual(self.article.view_count, 0)

    def test_staff_previews_are_not_counted(self):
        User.objects.create_user("ed", password="pw12345!", is_staff=True)
        self.client.login(username="ed", password="pw12345!")
        self.read()
        self.article.refresh_from_db()
        self.assertEqual(self.article.view_count, 0)


class RetentionTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="World", slug="world")

    def make(self, title, *, age_days, views, status=Article.Status.PUBLISHED):
        article = Article.objects.create(
            category=self.category, title=title, body="Body.", status=status
        )
        # created_at is auto_now_add, so age has to be forced after insert.
        Article.objects.filter(pk=article.pk).update(
            created_at=timezone.now() - timedelta(days=age_days), view_count=views
        )
        return article

    def test_unread_old_articles_are_purged(self):
        stale = self.make("Nobody read this one", age_days=10, views=0)
        popular = self.make("Plenty of readers here", age_days=10, views=50)
        recent = self.make("Published this morning", age_days=0, views=0)

        call_command("purge_stale", verbosity=0)

        self.assertFalse(Article.objects.filter(pk=stale.pk).exists())
        self.assertTrue(Article.objects.filter(pk=popular.pk).exists())
        self.assertTrue(Article.objects.filter(pk=recent.pk).exists())

    def test_dry_run_deletes_nothing(self):
        stale = self.make("Nobody read this one", age_days=10, views=0)
        call_command("purge_stale", "--dry-run", verbosity=0)
        self.assertTrue(Article.objects.filter(pk=stale.pk).exists())

    def test_articles_awaiting_review_are_kept(self):
        held = self.make(
            "Still needs an editor",
            age_days=30,
            views=0,
            status=Article.Status.NEEDS_REVIEW,
        )
        call_command("purge_stale", verbosity=0)
        self.assertTrue(Article.objects.filter(pk=held.pk).exists())


class DeskTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="World", slug="world")
        self.article = Article.objects.create(
            category=self.category,
            title="A story on the desk",
            body="Body copy.",
            status=Article.Status.NEEDS_REVIEW,
        )

    def login_staff(self):
        User.objects.create_user("ed", password="pw12345!", is_staff=True)
        self.client.login(username="ed", password="pw12345!")

    def test_desk_requires_staff(self):
        response = self.client.get("/desk/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_desk_lists_articles_grouped_by_day(self):
        self.login_staff()
        response = self.client.get("/desk/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.article.title)
        self.assertEqual(len(list(response.context["days"])), 1)

    def test_staff_can_publish_from_desk(self):
        self.login_staff()
        self.client.post(
            f"/desk/{self.article.slug}/action/", {"action": "publish"}, follow=True
        )
        self.article.refresh_from_db()
        self.assertEqual(self.article.status, Article.Status.PUBLISHED)

    def test_staff_can_delete_from_desk(self):
        self.login_staff()
        self.client.post(
            f"/desk/{self.article.slug}/action/", {"action": "delete"}, follow=True
        )
        self.assertFalse(Article.objects.filter(pk=self.article.pk).exists())

    def test_delete_requires_post(self):
        self.login_staff()
        response = self.client.get(f"/desk/{self.article.slug}/action/")
        self.assertEqual(response.status_code, 405)
        self.assertTrue(Article.objects.filter(pk=self.article.pk).exists())


class ShareLinkTests(TestCase):
    def test_article_page_offers_share_links(self):
        category = Category.objects.create(name="World", slug="world")
        article = Article.objects.create(
            category=category,
            title="A story worth sharing",
            body="Body copy.",
            status=Article.Status.PUBLISHED,
        )
        response = self.client.get(article.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        for key in ("whatsapp", "x", "facebook", "linkedin", "telegram", "email"):
            self.assertIn(key, response.context["share"])
        self.assertContains(response, "api.whatsapp.com")
        self.assertContains(response, 'property="og:title"')


class AdminActionTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="World", slug="world")
        User.objects.create_superuser("boss", "boss@example.com", "pw12345!")
        self.client.login(username="boss", password="pw12345!")

    def make(self, title, flags=None):
        return Article.objects.create(
            category=self.category,
            title=title,
            body="Body copy for the review queue.",
            status=Article.Status.NEEDS_REVIEW,
            safety_flags=flags or [],
        )

    def post_action(self, action, articles):
        return self.client.post(
            "/admin/news/article/",
            {
                "action": action,
                "_selected_action": [str(a.pk) for a in articles],
            },
            follow=True,
        )

    def test_approve_publishes_and_warns_about_blocked_flags(self):
        clean = self.make("A clean story")
        blocked = self.make(
            "A blocked story",
            [{"code": "adult_content", "severity": "block", "detail": "explicit"}],
        )
        response = self.post_action("approve_and_publish", [clean, blocked])
        self.assertEqual(response.status_code, 200)

        clean.refresh_from_db()
        blocked.refresh_from_db()
        self.assertEqual(clean.status, Article.Status.PUBLISHED)
        self.assertIsNotNone(clean.published_at)
        self.assertEqual(blocked.status, Article.Status.PUBLISHED)
        self.assertContains(response, "hard safety blocks")

    def test_reject_clears_publication(self):
        article = self.make("A story to reject")
        article.status = Article.Status.PUBLISHED
        article.save()
        self.post_action("reject", [article])
        article.refresh_from_db()
        self.assertEqual(article.status, Article.Status.REJECTED)
        self.assertIsNone(article.published_at)


@override_settings(MAX_NGRAM_OVERLAP=1.0, MAX_LONGEST_COMMON_RUN=200)
class ThresholdTests(TestCase):
    def test_thresholds_are_configurable(self):
        source = "The council approved the budget after a long debate on Friday."
        report = originality.check(source, [("example.com", source)])
        self.assertTrue(report.passed, report.summary)

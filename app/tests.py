"""Tests for the public site: the homepage, the contact form, and caching.

Companion to `cms/tests.py`, which covers the content models, the seed
command and admin access.

A note on why several of these look paranoid: the homepage is cached, and it
carries two pieces of per-visitor state (the contact form's CSRF token and
flash messages). Caching the whole response broke both during Phase 8 in a
way no single-user click-through would have revealed, because whoever warms
the cache always works. The multi-visitor tests below are what stop that
returning.
"""

import re
from pathlib import Path
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from cms.test_utils import TempMediaRootMixin

from cms.models import (
    AwardLogo,
    CommunityPhoto,
    ContactMessage,
    CoreValue,
    FAQItem,
    FeatureRow,
    HeroContent,
    HeroImpactStat,
    NewsPost,
    ProblemCard,
    ProblemStat,
    SiteSettings,
    TeamMember,
    Testimonial,
)


class CacheIsolatedTestCase(TestCase):
    """The homepage content cache is process-wide, so it leaks between tests
    unless each one starts clean."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


class HomePageEmptyStateTests(CacheIsolatedTestCase):
    """Every section has to disappear cleanly when its content is gone — the
    "degrades gracefully" goal in design §8. This is the state a brand-new
    install is in before anyone seeds or types anything."""

    def test_renders_with_every_table_empty(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_optional_sections_are_absent_rather_than_broken(self):
        body = self.client.get("/").content.decode()
        for label, marker in (
            ("about", 'id="about"'),
            ("problem", 'id="problem"'),
            ("services", 'id="services"'),
            ("why us", 'id="why-us"'),
            ("awards", 'id="awards"'),
            ("community", 'id="portfolio"'),
            ("team", 'id="team"'),
            ("news", "news-card"),
            ("faq", "faq-list"),
            ("testimonials", "testimonial-card"),
            ("map", "map-strip"),
        ):
            with self.subTest(section=label):
                self.assertNotIn(marker, body)

    def test_the_page_still_has_its_shell(self):
        """Contact and footer are not content-driven, so they stay."""
        body = self.client.get("/").content.decode()
        self.assertIn('id="contact"', body)
        self.assertIn("<footer", body)

    def test_singletons_are_created_on_first_view(self):
        """`.load()` means a fresh install renders instead of raising
        DoesNotExist."""
        self.client.get("/")
        self.assertEqual(SiteSettings.objects.count(), 1)
        self.assertEqual(HeroContent.objects.count(), 1)

    def test_each_table_can_be_emptied_individually(self):
        """Emptying one table must not take out its neighbours."""
        call_command("seed_home_content", verbosity=0)
        for model in (HeroImpactStat, CoreValue, ProblemCard, ProblemStat, FeatureRow,
                      AwardLogo, Testimonial, CommunityPhoto, NewsPost, FAQItem, TeamMember):
            with self.subTest(model=model.__name__):
                with self.captureOnCommitCallbacks(execute=True):
                    model.objects.all().delete()
                cache.clear()
                self.assertEqual(self.client.get("/").status_code, 200)
                call_command("seed_home_content", verbosity=0)
                cache.clear()


class HomePageSeededTests(TempMediaRootMixin, CacheIsolatedTestCase):
    """The homepage renders the real AGKI content once seeded."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_home_content", verbosity=0)

    def test_renders_200(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_shows_content_from_every_section(self):
        response = self.client.get("/")
        for label, needle in (
            ("hero kicker", "Afya Mfukoni"),
            ("hero headline", "Reaches Every"),
            ("about badge", "Est. 2022"),
            ("core value", "Compassion"),
            ("problem heading", "The Healthcare Gap in Sub-Saharan Countries"),
            ("problem stat", "Millions"),
            ("solution", "Mobile Clinics"),
            ("why us", "Community empowerment"),
            ("award", "Machel Trust"),
            ("community photo", "Mobile clinic outreach"),
            ("news post", "Maternal health launch"),
            ("faq", "What are your hours?"),
            ("team member", "Agnes Kimweri"),
            ("footer phone", "+255 717 308 796"),
        ):
            with self.subTest(section=label):
                self.assertContains(response, needle)

    def test_hero_counter_only_targets_numeric_values(self):
        """`parseInt("24/7")` is 24, so emitting data-count-to for a
        non-numeric stat would animate the label to a wrong "24"."""
        body = self.client.get("/").content.decode()
        self.assertIn('data-count-to="500000"', body)
        self.assertIn("500,000+", body)  # displayed grouped, counted raw
        match = re.search(r'<strong class="hero-impact-number"[^>]*>24/7</strong>', body)
        self.assertIsNotNone(match, "the 24/7 stat should still render")
        self.assertNotIn("data-count-to", match.group(0))

    def test_carousels_render_their_duplicate_loop_set(self):
        """Awards and team are rendered twice from one queryset so the CSS
        marquee loops seamlessly. Driving both copies from the same rows is
        what stopped them drifting apart, as the hand-written ones had."""
        body = self.client.get("/").content.decode()
        self.assertEqual(body.count('class="awards-logo'), AwardLogo.objects.count() * 2)
        self.assertEqual(body.count("team-photo-card"), TeamMember.objects.count() * 2)

    def test_team_roles_agree_across_both_copies(self):
        body = self.client.get("/").content.decode()
        self.assertEqual(body.count("Lab Manager"), 2)
        self.assertNotIn("Nurse Midwife", body)

    def test_media_is_served_from_the_cms_paths(self):
        body = self.client.get("/").content.decode()
        for folder in ("cms/team/", "cms/awards/", "cms/news/", "cms/community/", "cms/problem/"):
            with self.subTest(folder=folder):
                self.assertIn(folder, body)

    def test_rich_text_renders_as_html_not_escaped_markup(self):
        body = self.client.get("/").content.decode()
        self.assertNotIn("&lt;p&gt;", body)

    def test_unpublished_content_is_hidden(self):
        NewsPost.objects.update(is_published=False)
        FAQItem.objects.update(is_published=False)
        TeamMember.objects.update(is_active=False)
        cache.clear()

        body = self.client.get("/").content.decode()
        self.assertNotIn("Maternal health launch", body)
        self.assertNotIn("What are your hours?", body)
        self.assertNotIn("Agnes Kimweri", body)

    def test_social_icons_only_render_when_a_url_is_set(self):
        """The footer icons were bare <i> tags with no href, so there was no
        URL to import; they must not render as dead links."""
        self.assertNotContains(self.client.get("/"), "footer-socials")

        site = SiteSettings.load()
        site.facebook_url = "https://facebook.com/agki"
        site.save()
        cache.clear()

        response = self.client.get("/")
        self.assertContains(response, "footer-socials")
        self.assertContains(response, "https://facebook.com/agki")


class ContactFormTests(CacheIsolatedTestCase):
    """The form previously had no action and no `name` attribute on four of
    its five inputs, and faked success in JS (design §1)."""

    VALID = {
        "name": "Neema Joseph",
        "email": "neema@example.com",
        "phone": "+255 700 000 111",
        "subject": "Partnership Opportunity",
        "message": "We would like to discuss a mobile clinic partnership.",
    }

    def setUp(self):
        super().setUp()
        site = SiteSettings.load()
        site.email = "info@agkimedicallaboratory.co.tz"
        site.save()
        cache.clear()
        mail.outbox = []

    def test_form_is_wired_into_the_page(self):
        body = self.client.get("/").content.decode()
        self.assertIn('action="/contact/"', body)
        self.assertIn("csrfmiddlewaretoken", body)
        for field in ("name", "email", "phone", "subject", "message"):
            with self.subTest(field=field):
                self.assertIn(f'name="{field}"', body)

    def test_no_fake_success_handler_remains(self):
        body = self.client.get("/").content.decode()
        self.assertNotIn("We will respond soon", body)
        self.assertNotIn("Message Sent Successfully", body)

    def test_valid_submission_creates_a_message(self):
        response = self.client.post(reverse("contact_submit"), self.VALID)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/#contact")
        message = ContactMessage.objects.get()
        self.assertEqual(message.name, "Neema Joseph")
        self.assertEqual(message.email, "neema@example.com")
        self.assertEqual(message.subject, "Partnership Opportunity")
        self.assertFalse(message.is_read)

    def test_valid_submission_sends_the_notification(self):
        self.client.post(reverse("contact_submit"), self.VALID)

        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, "New contact form message: Partnership Opportunity")
        self.assertEqual(sent.to, ["info@agkimedicallaboratory.co.tz"])
        self.assertIn("mobile clinic partnership", sent.body)
        self.assertIn("neema@example.com", sent.body)

    def test_notification_goes_to_the_editable_site_address(self):
        site = SiteSettings.load()
        site.email = "reception@agki.example"
        site.save()
        cache.clear()

        self.client.post(reverse("contact_submit"), self.VALID)
        self.assertEqual(mail.outbox[0].to, ["reception@agki.example"])

    @mock.patch("app.views.send_mail")
    def test_send_mail_is_called(self, send_mail):
        """The plan asks for this explicitly, mocked."""
        self.client.post(reverse("contact_submit"), self.VALID)
        send_mail.assert_called_once()
        self.assertEqual(send_mail.call_args.kwargs["recipient_list"], ["info@agkimedicallaboratory.co.tz"])

    @mock.patch("app.views.send_mail", side_effect=OSError("SMTP is down"))
    def test_the_message_survives_a_mail_outage(self, send_mail):
        """`fail_silently=True` only protects against SMTP *rejecting* the
        send. A connection-level failure would still propagate, so the row
        being saved first is what actually guarantees nothing is lost."""
        with self.assertRaises(OSError):
            self.client.post(reverse("contact_submit"), self.VALID)
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_invalid_submission_keeps_what_was_typed(self):
        response = self.client.post(reverse("contact_submit"), {
            "name": "Ana", "email": "not-an-email", "phone": "", "subject": "Feedback", "message": "",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactMessage.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)
        body = response.content.decode()
        self.assertIn('value="Ana"', body)
        self.assertIn('value="Feedback"', body)
        self.assertIn("is-invalid", body)

    def test_whitespace_in_the_name_is_normalised(self):
        self.client.post(reverse("contact_submit"), {**self.VALID, "name": "  Neema   Joseph "})
        self.assertEqual(ContactMessage.objects.get().name, "Neema Joseph")

    def test_header_injection_is_neutralised_not_a_500(self):
        """`subject` arrives from a hidden input and becomes part of the
        email's Subject header, where Django raises BadHeaderError on a
        newline — which would surface as a 500 rather than a handled
        submission."""
        response = self.client.post(reverse("contact_submit"), {
            **self.VALID, "subject": "Feedback\nBcc: attacker@evil.test",
        })

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("\n", ContactMessage.objects.get().subject)
        self.assertNotIn("\n", mail.outbox[0].subject)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(reverse("contact_submit")).status_code, 405)

    def test_csrf_is_enforced(self):
        client = self.client_class(enforce_csrf_checks=True)
        response = client.post(reverse("contact_submit"), self.VALID)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_success_message_is_shown_after_the_redirect(self):
        self.client.post(reverse("contact_submit"), self.VALID)
        self.assertContains(self.client.get("/"), "Thank you")


class HomePageCacheTests(CacheIsolatedTestCase):
    """Phase 8. Every test here is a guard against a specific failure that
    `@cache_page` produced when it was tried first."""

    def test_the_second_request_costs_no_queries(self):
        """Exact counts aren't asserted — the number differs between a cold
        DB (where each singleton's get_or_create also inserts) and a warm
        one. What matters is that the page queries once and then stops."""
        with CaptureQueriesContext(connection) as cold:
            first = self.client.get("/")
        self.assertEqual(first.status_code, 200)
        self.assertGreater(len(cold), 10)

        with self.assertNumQueries(0):
            second = self.client.get("/")
        self.assertEqual(second.status_code, 200)

    def test_an_edit_is_stale_until_the_cache_expires(self):
        hero = HeroContent.load()
        hero.kicker_text = "Before"
        hero.save()
        cache.clear()
        self.assertContains(self.client.get("/"), "Before")

        hero.kicker_text = "After"
        hero.save()
        self.assertNotContains(self.client.get("/"), "After")

        cache.clear()  # stands in for the five minutes elapsing
        self.assertContains(self.client.get("/"), "After")

    def test_each_visitor_gets_their_own_csrf_token(self):
        """`cache_page` served the first visitor's token to everyone, and
        gave later visitors no csrftoken cookie at all — their submissions
        came back 403."""
        first = self.client_class()
        second = self.client_class()

        token_one = self._token(first.get("/").content.decode())
        token_two = self._token(second.get("/").content.decode())

        self.assertIsNotNone(token_one)
        self.assertIsNotNone(token_two)
        self.assertNotEqual(token_one, token_two)
        self.assertIn("csrftoken", first.cookies)
        self.assertIn("csrftoken", second.cookies)

    def test_a_visitor_served_cached_content_can_still_submit(self):
        """The regression that matters most: with CSRF enforcement on, as in
        production."""
        self.client_class().get("/")  # someone else warms the cache

        visitor = self.client_class(enforce_csrf_checks=True)
        token = self._token(visitor.get("/").content.decode())
        response = visitor.post(reverse("contact_submit"), {
            "csrfmiddlewaretoken": token, "name": "Cached Visitor",
            "email": "cached@example.com", "phone": "", "subject": "Feedback",
            "message": "submitted from a cached page",
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_flash_messages_do_not_leak_to_other_visitors(self):
        sender = self.client_class()
        sender.post(reverse("contact_submit"), {
            "name": "Sender", "email": "sender@example.com", "phone": "",
            "subject": "Feedback", "message": "hello",
        })
        self.assertContains(sender.get("/"), "Thank you")

        stranger = self.client_class()
        self.assertNotContains(stranger.get("/"), "Thank you")

    @staticmethod
    def _token(html):
        match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
        return match.group(1) if match else None


class HeroActionStylingTests(TestCase):
    """Every control in the hero must be styled by the stylesheet it ships
    with.

    This exists because a class was added to the markup and its rules were
    written, and the page still showed a bare blue underlined link - which
    looked exactly like the case this test does catch: a class the CSS has
    never heard of. The two are indistinguishable in a screenshot, so the
    cheap half is worth automating.

    Reading the file rather than the response on purpose: these rules live in
    `app/static/app/home.css`, and a template that references a class no rule
    defines is broken whether or not any particular browser has the file.
    """

    #: Where the rules actually are. `findstatic app/home.css` resolves here.
    STYLESHEET = Path(__file__).resolve().parent / "static" / "app" / "home.css"

    def hero_markup(self):
        html = self.client.get(reverse("home")).content.decode()
        return html.split('class="hero-actions"', 1)[1].split("</div>", 1)[0]

    def test_every_hero_button_class_has_rules_behind_it(self):
        css = self.STYLESHEET.read_text(encoding="utf-8")

        used = set()
        for attribute in re.findall(r'class="([^"]*)"', self.hero_markup()):
            used.update(
                name for name in attribute.split() if name.startswith("hero-button")
            )

        self.assertTrue(used, "the hero should offer at least one control")
        for name in sorted(used):
            with self.subTest(control=name):
                self.assertIn(".%s {" % name, css)

    @staticmethod
    def declarations_for(css, name):
        """Every declaration block whose selector list mentions `.name`.

        Written this way after the first version of this test let a real
        mutation through: it looked for `text-decoration` in the class's own
        rule *or* in the shared `.hero-button-primary, .hero-button-secondary`
        block, and the tertiary is not in that block - so deleting the
        declaration from the tertiary still passed on the strength of a rule
        that does not apply to it.
        """
        blocks = []
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            names = re.findall(r"\.([A-Za-z][\w-]*)", selectors)
            if name in names:
                blocks.append(body)
        return blocks

    def test_the_hero_controls_do_not_rely_on_the_blanket_link_reset(self):
        """Bootstrap's reboot underlines every `<a>`. These are buttons, and a
        button that reads as an underlined link the moment one unrelated rule
        moves is not styled, it is lucky."""
        css = self.STYLESHEET.read_text(encoding="utf-8")

        for name in ("hero-button-primary", "hero-button-secondary", "hero-button-tertiary"):
            with self.subTest(control=name):
                blocks = self.declarations_for(css, name)

                self.assertTrue(blocks, "no rule mentions .%s" % name)
                self.assertTrue(
                    any("text-decoration" in block for block in blocks),
                    "nothing that applies to .%s sets text-decoration" % name,
                )

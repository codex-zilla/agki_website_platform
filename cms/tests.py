"""Tests for the cms app: content models, the seed command, and admin access.

Companion to `app/tests.py`, which covers the public homepage and contact
form. Between them these lock in the behaviour the implementation plan's
Phase 9 asks for, plus the specific bugs found while building Phases 5-8 —
a regression test is the only thing that stops a fixed bug coming back.
"""

import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings

from cms.test_utils import TempMediaRootMixin, make_staff_user
from cms.models import (
    AboutContent,
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
    ProblemSectionContent,
    ProblemStat,
    SiteSettings,
    TeamMember,
    Testimonial,
)

User = get_user_model()


class SingletonModelTests(TestCase):
    """`SingletonModel` is what keeps the homepage's settings-style blocks to
    exactly one row (design §4.3)."""

    def test_load_returns_the_same_row_every_time(self):
        first = SiteSettings.load()
        second = SiteSettings.load()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_repeated_loads_never_accumulate_rows(self):
        for _ in range(10):
            SiteSettings.load()
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_racing_saves_collapse_onto_one_row(self):
        """The plan asks that concurrent `load()` calls can't create a second
        row. The protection is `save()` pinning `pk = 1`, so two callers that
        both believed they were creating the row still write to the same one.

        Simulated deterministically rather than with real threads: the test DB
        is in-memory SQLite, where genuine thread concurrency is flaky enough
        that the test would fail for reasons unrelated to this behaviour. Two
        independently-built instances saved in sequence is precisely the state
        a lost race produces.
        """
        HeroContent(kicker_text="first writer").save()
        HeroContent(kicker_text="second writer").save()

        self.assertEqual(HeroContent.objects.count(), 1)
        self.assertEqual(HeroContent.objects.get().kicker_text, "second writer")
        self.assertEqual(HeroContent.load().pk, 1)

    def test_delete_is_a_no_op(self):
        """A deleted singleton would leave `home` with nothing to render, so
        deletion is disabled on the model rather than only in the admin."""
        settings_row = SiteSettings.load()
        settings_row.delete()
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_every_singleton_behaves_the_same(self):
        for model in (SiteSettings, HeroContent, AboutContent, ProblemSectionContent):
            with self.subTest(model=model.__name__):
                model.load()
                model.load()
                self.assertEqual(model.objects.count(), 1)


class HeroImpactStatTests(TestCase):
    """`count_to` decides whether the homepage's count-up animation runs. It
    exists because `parseInt("24/7")` is 24, which would animate a "24/7"
    label to a wrong, truncated "24"."""

    def setUp(self):
        self.hero = HeroContent.load()

    def _stat(self, number):
        return HeroImpactStat(hero=self.hero, icon_class="fa", number=number, label="x")

    def test_plain_number(self):
        self.assertEqual(self._stat("500000").count_to, 500000)

    def test_number_with_separators(self):
        self.assertEqual(self._stat("500,000").count_to, 500000)

    def test_surrounding_whitespace(self):
        self.assertEqual(self._stat("  75 ").count_to, 75)

    def test_non_numeric_value_has_no_count(self):
        self.assertIsNone(self._stat("24/7").count_to)

    def test_empty_value_has_no_count(self):
        self.assertIsNone(self._stat("").count_to)


class ProblemStatTests(TestCase):
    """`is_word_value` drives the smaller type used for a worded figure."""

    def test_worded_value(self):
        self.assertTrue(ProblemStat(number="Millions").is_word_value)

    def test_percentage_is_not_a_word(self):
        self.assertFalse(ProblemStat(number="75%").is_word_value)

    def test_mixed_value_is_not_a_word(self):
        self.assertFalse(ProblemStat(number="60%+").is_word_value)


class TestimonialTests(TestCase):
    """`clean()` requires one of the two video sources. The design originally
    made `youtube_url` required outright, which would have blocked the
    uploaded-file escape hatch it was meant to allow (see plan 2.8)."""

    def test_rejects_a_row_with_neither_source(self):
        with self.assertRaises(ValidationError):
            Testimonial(patient_name="No video").clean()

    def test_accepts_a_youtube_url_alone(self):
        Testimonial(youtube_url="https://youtube.com/embed/abc").clean()

    def test_accepts_an_uploaded_file_alone(self):
        Testimonial(video_file="cms/testimonials/x.mp4").clean()


class SeedHomeContentCommandTests(TempMediaRootMixin, TestCase):
    """`seed_home_content` is how today's live copy reaches a fresh
    environment, including production at Phase 10, so its idempotency is a
    deployment-safety property rather than a nicety."""

    def seed(self, **options):
        call_command("seed_home_content", verbosity=0, **options)

    def test_populates_every_content_model(self):
        self.seed()
        expected = {
            HeroImpactStat: 3,
            CoreValue: 4,
            ProblemCard: 3,
            ProblemStat: 3,
            AwardLogo: 8,
            Testimonial: 2,
            CommunityPhoto: 6,
            NewsPost: 3,
            FAQItem: 4,
            TeamMember: 9,
        }
        for model, count in expected.items():
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), count)

        self.assertEqual(FeatureRow.objects.filter(category=FeatureRow.Category.SOLUTION).count(), 6)
        self.assertEqual(FeatureRow.objects.filter(category=FeatureRow.Category.WHY_US).count(), 6)

    def test_seeds_the_real_agki_copy(self):
        self.seed()
        site = SiteSettings.load()
        self.assertEqual(site.phone, "+255 717 308 796")
        self.assertEqual(site.email, "info@agkimedicallaboratory.co.tz")
        self.assertEqual(HeroContent.load().kicker_text, "Afya Mfukoni")
        self.assertTrue(TeamMember.objects.filter(name="Agnes Kimweri").exists())

    def test_map_embed_url_survives_intact(self):
        """A regression guard for plan 5.5: this value is 252 characters and
        did not fit `URLField`'s 200-character default, which would have made
        seeding fail outright."""
        self.seed()
        self.assertGreater(len(SiteSettings.load().map_embed_url), 200)
        self.assertTrue(SiteSettings.load().map_embed_url.startswith("https://www.google.com/maps/embed"))

    def test_running_twice_changes_nothing(self):
        self.seed()
        counts = {m: m.objects.count() for m in (TeamMember, FeatureRow, FAQItem, AwardLogo, NewsPost)}
        photos = sorted(TeamMember.objects.values_list("photo", flat=True))

        self.seed()

        for model, count in counts.items():
            with self.subTest(model=model.__name__):
                self.assertEqual(model.objects.count(), count)
        # Re-running must not append a random suffix to the stored paths;
        # that would orphan the previous file on every run.
        self.assertEqual(sorted(TeamMember.objects.values_list("photo", flat=True)), photos)

    def test_installs_media_at_deterministic_paths(self):
        self.seed()
        member = TeamMember.objects.get(name="Agnes Kimweri")
        self.assertEqual(member.photo.name, "cms/team/TAg.jpg")
        self.assertTrue((self.media_root / "cms" / "team" / "TAg.jpg").exists())

    def test_every_seeded_file_exists_on_disk(self):
        self.seed()
        for model, field in (
            (TeamMember, "photo"),
            (AwardLogo, "image"),
            (NewsPost, "image"),
            (CommunityPhoto, "image"),
            (ProblemCard, "image"),
            (Testimonial, "video_file"),
        ):
            for row in model.objects.all():
                file = getattr(row, field)
                with self.subTest(model=model.__name__, file=file.name):
                    self.assertTrue((self.media_root / file.name).exists())

    def test_seeded_rows_would_pass_admin_validation(self):
        self.seed()
        for model in (TeamMember, AwardLogo, NewsPost, FAQItem, FeatureRow, ProblemCard, ProblemStat):
            for row in model.objects.all():
                with self.subTest(model=model.__name__, row=str(row)):
                    row.full_clean()

    def test_dry_run_writes_nothing(self):
        self.seed(dry_run=True)
        self.assertEqual(TeamMember.objects.count(), 0)
        self.assertEqual(FeatureRow.objects.count(), 0)

    def test_flush_rebuilds_from_scratch(self):
        self.seed()
        TeamMember.objects.create(name="Someone Removed", role="Former", photo="cms/team/TAg.jpg")
        self.seed(flush=True)
        self.assertEqual(TeamMember.objects.count(), 9)
        self.assertFalse(TeamMember.objects.filter(name="Someone Removed").exists())

    def test_flush_never_touches_contact_messages(self):
        """Those are real messages from the public, not seedable content."""
        ContactMessage.objects.create(name="A", email="a@example.com", subject="s", message="m")
        self.seed(flush=True)
        self.assertEqual(ContactMessage.objects.count(), 1)


class ContentEditorPermissionTests(TestCase):
    """Least privilege, verified by signing in as the scoped account rather
    than by reading the permission list (design §5)."""

    @classmethod
    def setUpTestData(cls):
        cls.editor = make_staff_user(username="editor1", is_staff=True)
        cls.editor.groups.add(Group.objects.get(name="Content Editors"))

    def setUp(self):
        self.client.force_login(self.editor)

    def test_group_exists_from_a_migration(self):
        """Created by a data migration so it exists in every environment,
        production included, without anyone assembling it by hand."""
        self.assertTrue(Group.objects.filter(name="Content Editors").exists())

    def test_can_open_the_admin(self):
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_can_reach_every_cms_changelist(self):
        for model in (TeamMember, NewsPost, FAQItem, AwardLogo, FeatureRow,
                      CommunityPhoto, Testimonial, ProblemCard, ProblemStat):
            url = f"/admin/cms/{model._meta.model_name}/"
            with self.subTest(model=model.__name__):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_singleton_changelist_redirects_to_the_one_row(self):
        response = self.client.get("/admin/cms/sitesettings/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)

    def test_cannot_add_or_delete_a_singleton(self):
        self.assertEqual(self.client.get("/admin/cms/sitesettings/add/").status_code, 403)

    def test_blocked_from_other_apps(self):
        """`doctorDashboard` registers no models at all, so it is unreachable
        by not existing rather than by permission."""
        for url in ("/admin/appointments/appointment/",
                    "/admin/appointments/availableslot/",
                    "/admin/useraccount/customuser/",
                    "/admin/useraccount/profile/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_other_apps_are_not_even_linked_on_the_index(self):
        body = self.client.get("/admin/").content.decode()
        self.assertNotIn("/admin/useraccount/customuser/", body)
        self.assertNotIn("/admin/appointments/appointment/", body)


class ContactMessageAdminTests(TestCase):
    """The inbox is written by the public form, never by staff, and deleting
    inbound messages is reserved for superusers (design §5)."""

    @classmethod
    def setUpTestData(cls):
        cls.editor = make_staff_user(username="editor2", email="e2@example.com", is_staff=True)
        cls.editor.groups.add(Group.objects.get(name="Content Editors"))
        cls.superuser = User.objects.create_superuser(
            username="root", email="root@example.com", password="test-pass-12345"
        )
        cls.message = ContactMessage.objects.create(
            name="Visitor", email="v@example.com", subject="Hello", message="Hi"
        )

    def test_editor_can_view_the_inbox(self):
        self.client.force_login(self.editor)
        response = self.client.get("/admin/cms/contactmessage/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visitor")

    def test_nobody_can_add_one_by_hand(self):
        for user in (self.editor, self.superuser):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get("/admin/cms/contactmessage/add/").status_code, 403)

    def test_editor_cannot_delete(self):
        self.client.force_login(self.editor)
        url = f"/admin/cms/contactmessage/{self.message.pk}/delete/"
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_superuser_can_delete(self):
        self.client.force_login(self.superuser)
        url = f"/admin/cms/contactmessage/{self.message.pk}/delete/"
        self.assertEqual(self.client.get(url).status_code, 200)


class NewsPostAdminTests(TestCase):
    """Regression guard for the Phase 6 finding: `NewsPostAdmin` used to
    inherit `SortableAdminMixin`, which resolves its sort field from
    `Meta.ordering[0]`. For `NewsPost` that is `-published_date`, so the
    reorder endpoint would have written integers into a DateField."""

    def test_news_is_not_drag_sortable(self):
        from adminsortable2.admin import SortableAdminMixin
        from django.contrib import admin as django_admin

        self.assertNotIsInstance(django_admin.site._registry[NewsPost], SortableAdminMixin)

    def test_every_sortable_admin_sorts_on_an_integer_field(self):
        """The general form of the same bug, so a future model can't
        reintroduce it silently."""
        from adminsortable2.admin import SortableAdminMixin
        from django.contrib import admin as django_admin

        for model, model_admin in django_admin.site._registry.items():
            if isinstance(model_admin, SortableAdminMixin):
                field = model._meta.get_field(model_admin.default_order_field)
                with self.subTest(model=model.__name__, field=field.name):
                    self.assertEqual(field.get_internal_type(), "PositiveIntegerField")

"""Import the homepage content that is currently hardcoded in
``app/templates/app/home.html`` into the cms models (design doc §7.2).

Run once per environment at cutover so staff inherit the exact site that is
live today and edit from there, instead of re-typing ~14 sections by hand.

    python manage.py seed_home_content [--dry-run] [--flush]

**Idempotent.** Every row is matched on a natural key (a team member's name, a
news post's slug, …) and updated in place, so re-running converges on the same
state rather than duplicating. Files are copied to deterministic paths and the
field's ``.name`` is assigned directly, rather than going through
``FieldFile.save()`` — that matters, because ``save()`` would append a random
suffix on every run (``TAg_x7Kd2.jpg``) and leave a pile of orphans behind.

Because re-running *updates* rows, it will overwrite later edits to the rows it
manages. That is the intended meaning of "import today's live content", but it
does make this a command to run deliberately, not on every deploy.

``--flush`` deletes existing rows in the seeded list models first, for a clean
re-import against a wiped or half-populated dev DB. It never touches
``ContactMessage`` — those are real inbound messages from the public, not
seedable content.
"""

import shutil
from datetime import date
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.urls import NoReverseMatch, reverse

from cms.models import (
    AboutContent,
    AwardLogo,
    CommunityPhoto,
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

from . import _home_content_data as content

# List models the seeder owns, in dependency order. Used by --flush.
# ContactMessage is deliberately absent — see the module docstring.
SEEDED_LIST_MODELS = [
    HeroImpactStat,
    CoreValue,
    ProblemCard,
    ProblemStat,
    FeatureRow,
    AwardLogo,
    Testimonial,
    CommunityPhoto,
    NewsPost,
    FAQItem,
    TeamMember,
]


class Command(BaseCommand):
    help = "Import the currently-hardcoded home.html content into the cms models."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything to the database or copying any files.",
        )
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete existing rows in the seeded content models first (never touches contact messages).",
        )

    def _say(self, message):
        """Write progress output unless the caller asked for silence.

        Django passes `verbosity` to every command; honouring it is what lets
        the test suite call this without flooding its output.
        """
        if self.verbosity:
            self.stdout.write(message)

    def handle(self, *args, **options):
        self.verbosity = options["verbosity"]
        self.dry_run = options["dry_run"]
        self.created = 0
        self.updated = 0
        self.copied = 0
        self.skipped_files = 0

        # Command output stays ASCII-only on purpose: this also has to run over
        # a cPanel/shared-host shell at Phase 10, where a non-UTF-8 codepage
        # turns decorative dashes and middots into mojibake.
        if self.dry_run:
            self._say(self.style.WARNING("DRY RUN - no database writes, no file copies.\n"))

        try:
            with transaction.atomic():
                if options["flush"]:
                    self._flush()

                self._seed_site_settings()
                self._seed_hero()
                self._seed_about()
                self._seed_problem()
                self._seed_feature_rows()
                self._seed_awards()
                self._seed_testimonials()
                self._seed_community()
                self._seed_news()
                self._seed_faqs()
                self._seed_team()

                if self.dry_run:
                    # Roll the whole thing back. Everything above ran for real
                    # against the DB, so this exercises the actual writes
                    # (including any constraint violation) rather than just
                    # predicting them.
                    raise _DryRunRollback()
        except _DryRunRollback:
            pass

        self._say("")
        verb = "would be" if self.dry_run else ""
        self._say(
            self.style.SUCCESS(
                f"Rows {verb} created: {self.created} | {verb} updated: {self.updated} | "
                f"files {verb} copied: {self.copied} (unchanged: {self.skipped_files})"
            )
        )
        if self.dry_run:
            self._say(self.style.WARNING("Rolled back - nothing was saved."))

    # --- Assets -------------------------------------------------------------

    def _resolve_source(self, spec):
        """Turn a "<store>:<path>" source spec into an absolute Path."""
        store, _, rel = spec.partition(":")
        if store == "static":
            found = finders.find(rel)
            if not found:
                raise CommandError(f"Static asset not found via staticfiles finders: {rel!r}")
            return Path(found)
        if store == "media":
            path = Path(settings.MEDIA_ROOT) / rel
            if not path.exists():
                raise CommandError(f"Media asset not found: {path}")
            return path
        raise CommandError(f"Unknown asset store {store!r} in spec {spec!r}")

    def _install_asset(self, asset):
        """Copy a seed asset into MEDIA_ROOT and return its media-relative path.

        The returned string is what gets assigned to the model's Image/FileField,
        so it must always use forward slashes regardless of host OS.
        """
        spec, dest_rel = asset
        dest_rel = PurePosixPath(dest_rel).as_posix()
        source = self._resolve_source(spec)
        dest = Path(settings.MEDIA_ROOT) / dest_rel

        # Same size is a good enough "already installed" test here: these are
        # fixed release assets, not files that get edited in place.
        if dest.exists() and dest.stat().st_size == source.stat().st_size:
            self.skipped_files += 1
            return dest_rel

        if not self.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
        self.copied += 1
        return dest_rel

    # --- Row helpers --------------------------------------------------------

    def _upsert(self, model, lookup, defaults):
        obj, created = model.objects.update_or_create(**lookup, defaults=defaults)
        if created:
            self.created += 1
        else:
            self.updated += 1
        return obj

    def _singleton(self, model, fields):
        obj = model.load()
        for name, value in fields.items():
            setattr(obj, name, value)
        obj.save()
        self.updated += 1
        return obj

    def _report(self, label, count):
        # str() is required, not decorative: model _meta names are lazy
        # translation proxies, and a __proxy__ does not implement __format__
        # for an alignment spec like :<24 (it raises TypeError).
        self._say(f"  {str(label):<24} {count}")

    def _flush(self):
        self._say(self.style.WARNING("Flushing existing seeded content rows..."))
        for model in reversed(SEEDED_LIST_MODELS):
            deleted, _ = model.objects.all().delete()
            if deleted:
                self._report(model._meta.verbose_name_plural, f"-{deleted}")

    # --- Sections -----------------------------------------------------------

    def _seed_site_settings(self):
        fields = dict(content.SITE)
        fields["logo"] = self._install_asset(fields["logo"])
        fields["favicon"] = self._install_asset(fields["favicon"])
        self._singleton(SiteSettings, fields)
        self._report("Site settings", "1")

    def _seed_hero(self):
        fields = dict(content.HERO)
        # Resolved rather than hardcoded so the seeded link can't silently drift
        # from the URLconf. Falls back to the literal path if the name ever goes
        # away, so seeding never hard-fails over a CTA link.
        try:
            fields["primary_cta_url"] = reverse("appointment_home")
        except NoReverseMatch:
            self.stderr.write(self.style.WARNING("URL name 'appointment_home' not found — using '/appointments/'."))
            fields["primary_cta_url"] = "/appointments/"

        hero = self._singleton(HeroContent, fields)
        for index, stat in enumerate(content.HERO_IMPACT_STATS):
            self._upsert(
                HeroImpactStat,
                {"hero": hero, "label": stat["label"]},
                {"icon_class": stat["icon_class"], "number": stat["number"], "suffix": stat["suffix"], "order": index},
            )
        self._report("Hero section", f"1 + {len(content.HERO_IMPACT_STATS)} impact stats")

    def _seed_about(self):
        about = self._singleton(AboutContent, content.ABOUT)
        for index, value in enumerate(content.CORE_VALUES):
            self._upsert(
                CoreValue,
                {"about": about, "label": value["label"]},
                {"icon_class": value["icon_class"], "order": index},
            )
        self._report("About section", f"1 + {len(content.CORE_VALUES)} core values")

    def _seed_problem(self):
        self._singleton(ProblemSectionContent, content.PROBLEM_SECTION)

        for index, card in enumerate(content.PROBLEM_CARDS):
            self._upsert(
                ProblemCard,
                {"title": card["title"]},
                {
                    "image": self._install_asset(card["asset"]),
                    "image_alt": card["image_alt"],
                    "icon_class": card["icon_class"],
                    "body": card["body"],
                    "order": index,
                },
            )

        for index, stat in enumerate(content.PROBLEM_STATS):
            self._upsert(
                ProblemStat,
                {"label": stat["label"]},
                {
                    "icon_class": stat["icon_class"],
                    "number": stat["number"],
                    "description": stat["description"],
                    "order": index,
                },
            )
        self._report(
            "Problem section",
            f"1 + {len(content.PROBLEM_CARDS)} cards + {len(content.PROBLEM_STATS)} stats",
        )

    def _seed_feature_rows(self):
        for category, rows in (
            (FeatureRow.Category.SOLUTION, content.SOLUTIONS),
            (FeatureRow.Category.WHY_US, content.WHY_US),
        ):
            for index, row in enumerate(rows):
                self._upsert(
                    FeatureRow,
                    {"category": category, "title": row["title"]},
                    {"icon_class": row["icon_class"], "description": row["description"], "order": index},
                )
        self._report("Feature rows", f"{len(content.SOLUTIONS)} solutions + {len(content.WHY_US)} why-us")

    def _seed_awards(self):
        for index, logo in enumerate(content.AWARD_LOGOS):
            self._upsert(
                AwardLogo,
                {"alt_text": logo["alt_text"]},
                {"image": self._install_asset(logo["asset"]), "size": logo["size"], "order": index},
            )
        self._report("Award logos", len(content.AWARD_LOGOS))

    def _seed_testimonials(self):
        # Matched on the installed file path: both live testimonials have no
        # caption or attribution, so there is no text natural key available.
        for index, item in enumerate(content.TESTIMONIALS):
            path = self._install_asset(item["asset"])
            self._upsert(
                Testimonial,
                {"video_file": path},
                {"patient_name": item["patient_name"], "is_published": True, "order": index},
            )
        self._report("Testimonials", len(content.TESTIMONIALS))

    def _seed_community(self):
        for index, photo in enumerate(content.COMMUNITY_PHOTOS):
            self._upsert(
                CommunityPhoto,
                {"caption": photo["caption"]},
                {"image": self._install_asset(photo["asset"]), "is_published": True, "order": index},
            )
        self._report("Community photos", len(content.COMMUNITY_PHOTOS))

    def _seed_news(self):
        for index, post in enumerate(content.NEWS_POSTS):
            self._upsert(
                NewsPost,
                {"slug": post["slug"]},
                {
                    "image": self._install_asset(post["asset"]),
                    "title": post["title"],
                    "excerpt": post["excerpt"],
                    "body": "",
                    "published_date": date.fromisoformat(post["published_date"]),
                    "is_published": True,
                    "order": index,
                },
            )
        self._report("News posts", len(content.NEWS_POSTS))

    def _seed_faqs(self):
        for index, item in enumerate(content.FAQ_ITEMS):
            self._upsert(
                FAQItem,
                {"question": item["question"]},
                {"icon_class": item["icon_class"], "answer": item["answer"], "is_published": True, "order": index},
            )
        self._report("FAQ items", len(content.FAQ_ITEMS))

    def _seed_team(self):
        for index, member in enumerate(content.TEAM_MEMBERS):
            self._upsert(
                TeamMember,
                {"name": member["name"]},
                {
                    "photo": self._install_asset(member["asset"]),
                    "role": member["role"],
                    "is_active": True,
                    "order": index,
                },
            )
        self._report("Team members", len(content.TEAM_MEMBERS))


class _DryRunRollback(Exception):
    """Internal control-flow signal to roll back the --dry-run transaction."""

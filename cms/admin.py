from adminsortable2.admin import SortableAdminBase, SortableAdminMixin, SortableTabularInline
from django import forms
from django.contrib import admin
from django.db import models
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html

from .models import (
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


def image_thumbnail(field_name, height=50):
    """Build a readonly admin list column rendering a small <img> preview
    for the given ImageField/FileField name — reused across every model
    that has one, so editors can see what they uploaded without opening
    the row.
    """

    def _thumbnail(self, obj):
        file = getattr(obj, field_name, None)
        if not file:
            return "—"
        try:
            url = file.url
        except ValueError:
            # FieldFile.url raises ValueError when no file is attached —
            # the exact crash the pre-CMS templates hit unguarded
            # (see docs/website-cms-design.md and the appointments audit
            # for the same bug elsewhere in this codebase). Guard it here
            # once, in the one place every model's admin list routes through.
            return "—"
        return format_html(
            '<img src="{}" style="height: {}px; border-radius: 6px; object-fit: cover;">',
            url,
            height,
        )

    _thumbnail.short_description = "Preview"
    return _thumbnail


class CardFieldsetAdminMixin:
    """Wraps a change/add form's fields in jazzmin's bordered `.card` look.

    jazzmin only renders that border when the admin has more than one
    fieldset, OR a single fieldset with a non-None name — see
    jazzmin/includes/single.html's `has_fieldsets` check. Django's own
    default `get_fieldsets()` for an admin with no explicit `fieldsets`
    returns exactly the case jazzmin treats as "none": one fieldset named
    `None`. Left as Django's default, a change/add form renders as bare
    fields directly on the page background — no visible card at all.

    First caught and hand-fixed on HeroContentAdmin alone; turned out to
    affect every other admin in this file that doesn't define its own
    `fieldsets` (About, Problem, ProblemCard, ProblemStat, FeatureRow,
    AwardLogo, Testimonial, CommunityPhoto, NewsPost, FAQItem, TeamMember,
    ContactMessage) — fixed once, here, rather than patched per-admin as
    each one gets noticed. Only takes over when `fieldsets` isn't already
    set explicitly, so SiteSettings' multi-section layout and Hero's
    paired-field layout (both intentionally customized already) are
    untouched.
    """

    def get_fieldsets(self, request, obj=None):
        if self.fieldsets:
            return self.fieldsets
        return [("", {"fields": self.get_fields(request, obj)})]


class SingletonModelAdmin(CardFieldsetAdminMixin, admin.ModelAdmin):
    """For models with exactly one row (SiteSettings, HeroContent,
    AboutContent, ProblemSectionContent). No changelist, no add, no
    delete — clicking the section in the sidebar jumps straight to
    editing the one row that always exists via `Model.load()`.
    """

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = self.model.load()
        opts = self.model._meta
        url = reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk])
        return redirect(url)


# ---------------------------------------------------------------------------
# Site settings
# ---------------------------------------------------------------------------


@admin.register(SiteSettings)
class SiteSettingsAdmin(SingletonModelAdmin):
    fieldsets = (
        (None, {"fields": ("site_name", "tagline", "logo", "favicon")}),
        ("Contact", {"fields": ("phone", "email", "address_line")}),
        ("Social links", {"fields": ("facebook_url", "twitter_url", "instagram_url", "linkedin_url")}),
        ("Advanced", {"fields": ("google_analytics_id", "map_embed_url", "footer_copyright_text")}),
    )


# ---------------------------------------------------------------------------
# Hero — HeroImpactStat is edited inline on the Hero section page, not as
# its own top-level admin entry, since it only ever exists attached to the
# one HeroContent row.
# ---------------------------------------------------------------------------


class AgkiSortableTabularInline(SortableTabularInline):
    """Shared base for this project's sortable tabular inlines.

    Only carries the media for the "Remove" affordance — see
    cms/static/cms/admin/agki-inline-remove.js for why saved rows need
    it. Kept as a base class rather than repeated `Media` declarations so
    a future inline picks the behaviour up by inheriting, and so the
    script is declared once.
    """

    class Media:
        js = ("cms/admin/agki-inline-remove.js",)


class HeroImpactStatInline(AgkiSortableTabularInline):
    model = HeroImpactStat
    extra = 1
    fields = ("icon_class", "number", "suffix", "label", "order")
    # Decoupled from Meta.verbose_name/verbose_name_plural ("Hero impact
    # stat(s)") so the inline's own "Add another …" link reads "Add
    # another impact stat" while the card heading above the table (which
    # reads verbose_name_plural) keeps the "Hero" prefix.
    verbose_name = "impact stat"
    verbose_name_plural = "Hero impact stats"


@admin.register(HeroContent)
class HeroContentAdmin(SortableAdminBase, SingletonModelAdmin):
    # SortableAdminBase (not the full SortableAdminMixin) is required here
    # purely so adminsortable2 accepts a SortableTabularInline below — this
    # admin's own changelist is redirected away by SingletonModelAdmin, so
    # there's no drag-reorder on HeroContent itself, only on the inline.
    inlines = [HeroImpactStatInline]
    # A named (non-None) fieldset is what makes jazzmin wrap the form in a
    # bordered .card — see jazzmin/includes/single.html's has_fieldsets
    # check, which treats a single fieldset named None as "no fieldsets"
    # and skips the card. Name is "" (not None) so has_fieldsets is True
    # but the card-header block (guarded on `fieldset.name`) still renders
    # nothing — one plain card, no redundant header bar.
    # Django's default admin Textarea is 10 rows — far taller than the
    # two-or-three-sentence lede actually holds, which left a large dead
    # box in the middle of the form. `lede` is HeroContent's only
    # TextField, so this override is unambiguous.
    formfield_overrides = {
        models.TextField: {"widget": forms.Textarea(attrs={"rows": 4})},
    }
    # Paired tuples render two fields side by side on one row.
    fieldsets = (
        (
            "",
            {
                "fields": (
                    ("kicker_text", "headline_accent"),
                    ("headline_line1", "headline_line2"),
                    "lede",
                    ("primary_cta_text", "primary_cta_url"),
                    ("secondary_cta_text", "secondary_cta_url"),
                )
            },
        ),
    )


# ---------------------------------------------------------------------------
# About — same pattern: CoreValue is inline under the one About row.
# ---------------------------------------------------------------------------


class CoreValueInline(AgkiSortableTabularInline):
    model = CoreValue
    extra = 1
    fields = ("icon_class", "label", "order")


@admin.register(AboutContent)
class AboutContentAdmin(SortableAdminBase, SingletonModelAdmin):
    inlines = [CoreValueInline]


# ---------------------------------------------------------------------------
# Problem section
# ---------------------------------------------------------------------------


@admin.register(ProblemSectionContent)
class ProblemSectionContentAdmin(SingletonModelAdmin):
    pass


@admin.register(ProblemCard)
class ProblemCardAdmin(CardFieldsetAdminMixin, SortableAdminMixin, admin.ModelAdmin):
    list_display = ("thumbnail", "title", "order")
    search_fields = ("title", "body")

    thumbnail = image_thumbnail("image")


@admin.register(ProblemStat)
class ProblemStatAdmin(CardFieldsetAdminMixin, SortableAdminMixin, admin.ModelAdmin):
    list_display = ("number", "label", "order")
    search_fields = ("label", "description")


# ---------------------------------------------------------------------------
# Solutions / Why Us — one table, two sections. Reorder with the category
# filter applied: adminsortable2 drags within the *currently filtered*
# changelist queryset, so filtering to one category first keeps a drag
# operation from mixing the two sections' order values together.
# ---------------------------------------------------------------------------


@admin.register(FeatureRow)
class FeatureRowAdmin(CardFieldsetAdminMixin, SortableAdminMixin, admin.ModelAdmin):
    list_display = ("title", "category", "order")
    list_filter = ("category",)
    search_fields = ("title", "description")
    # Model Meta.ordering is ("category", "order") for correct queryset
    # ordering elsewhere (the home view filters by category then orders
    # by "order"). adminsortable2 reads ordering[0] to decide which field
    # it drags — override at the admin level so it targets "order", not
    # "category".
    ordering = ("order",)


# ---------------------------------------------------------------------------
# Awards
# ---------------------------------------------------------------------------


@admin.register(AwardLogo)
class AwardLogoAdmin(CardFieldsetAdminMixin, SortableAdminMixin, admin.ModelAdmin):
    list_display = ("thumbnail", "alt_text", "size", "order")

    thumbnail = image_thumbnail("image")


# ---------------------------------------------------------------------------
# Testimonials
# ---------------------------------------------------------------------------


@admin.register(Testimonial)
class TestimonialAdmin(CardFieldsetAdminMixin, SortableAdminMixin, admin.ModelAdmin):
    list_display = ("patient_name", "youtube_url", "is_published", "order")
    list_filter = ("is_published",)


# ---------------------------------------------------------------------------
# Community photos
# ---------------------------------------------------------------------------


@admin.register(CommunityPhoto)
class CommunityPhotoAdmin(CardFieldsetAdminMixin, SortableAdminMixin, admin.ModelAdmin):
    list_display = ("thumbnail", "caption", "is_published", "order")
    list_filter = ("is_published",)

    thumbnail = image_thumbnail("image")


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


@admin.register(NewsPost)
class NewsPostAdmin(CardFieldsetAdminMixin, SortableAdminMixin, admin.ModelAdmin):
    list_display = ("thumbnail", "title", "published_date", "is_published", "order")
    list_filter = ("is_published",)
    search_fields = ("title", "excerpt", "body")
    prepopulated_fields = {"slug": ("title",)}
    date_hierarchy = "published_date"

    thumbnail = image_thumbnail("image")


# ---------------------------------------------------------------------------
# FAQ
# ---------------------------------------------------------------------------


@admin.register(FAQItem)
class FAQItemAdmin(CardFieldsetAdminMixin, SortableAdminMixin, admin.ModelAdmin):
    list_display = ("question", "is_published", "order")
    list_filter = ("is_published",)
    search_fields = ("question", "answer")


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


@admin.register(TeamMember)
class TeamMemberAdmin(CardFieldsetAdminMixin, SortableAdminMixin, admin.ModelAdmin):
    list_display = ("thumbnail", "name", "role", "is_active", "order")
    list_filter = ("is_active",)
    search_fields = ("name", "role")

    thumbnail = image_thumbnail("photo")


# ---------------------------------------------------------------------------
# Contact messages — an inbox filled by the public contact form (see
# design doc §1, §6), not staff-authored content. Content editors can
# view/mark-read; only superusers can delete (design doc §5). Every field
# is read-only because these are records of what a visitor submitted, not
# something staff should be able to silently rewrite.
# ---------------------------------------------------------------------------


@admin.register(ContactMessage)
class ContactMessageAdmin(CardFieldsetAdminMixin, admin.ModelAdmin):
    list_display = ("name", "subject", "email", "created_at", "is_read")
    list_filter = ("is_read", "created_at")
    search_fields = ("name", "email", "subject", "message")
    readonly_fields = ("name", "email", "phone", "subject", "message", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # "Change" here only ever means toggling is_read — everything
        # else is readonly above — but keep this explicit rather than
        # relying on the readonly_fields list alone.
        return True

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


# ---------------------------------------------------------------------------
# Branding — five minutes of work that stops this looking like a generic
# Django admin to non-technical AGKI staff (design doc §2).
# ---------------------------------------------------------------------------

admin.site.site_header = "AGKI Content Manager"
admin.site.site_title = "AGKI Content Manager"
admin.site.index_title = "Website content"

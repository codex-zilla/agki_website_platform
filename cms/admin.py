from adminsortable2.admin import SortableAdminBase, SortableAdminMixin, SortableTabularInline
from django.contrib import admin
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


class SingletonModelAdmin(admin.ModelAdmin):
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


class HeroImpactStatInline(SortableTabularInline):
    model = HeroImpactStat
    extra = 1
    fields = ("icon_class", "number", "suffix", "label", "order")


@admin.register(HeroContent)
class HeroContentAdmin(SortableAdminBase, SingletonModelAdmin):
    # SortableAdminBase (not the full SortableAdminMixin) is required here
    # purely so adminsortable2 accepts a SortableTabularInline below — this
    # admin's own changelist is redirected away by SingletonModelAdmin, so
    # there's no drag-reorder on HeroContent itself, only on the inline.
    inlines = [HeroImpactStatInline]


# ---------------------------------------------------------------------------
# About — same pattern: CoreValue is inline under the one About row.
# ---------------------------------------------------------------------------


class CoreValueInline(SortableTabularInline):
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
class ProblemCardAdmin(SortableAdminMixin, admin.ModelAdmin):
    list_display = ("thumbnail", "title", "order")
    search_fields = ("title", "body")

    thumbnail = image_thumbnail("image")


@admin.register(ProblemStat)
class ProblemStatAdmin(SortableAdminMixin, admin.ModelAdmin):
    list_display = ("number", "label", "order")
    search_fields = ("label", "description")


# ---------------------------------------------------------------------------
# Solutions / Why Us — one table, two sections. Reorder with the category
# filter applied: adminsortable2 drags within the *currently filtered*
# changelist queryset, so filtering to one category first keeps a drag
# operation from mixing the two sections' order values together.
# ---------------------------------------------------------------------------


@admin.register(FeatureRow)
class FeatureRowAdmin(SortableAdminMixin, admin.ModelAdmin):
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
class AwardLogoAdmin(SortableAdminMixin, admin.ModelAdmin):
    list_display = ("thumbnail", "alt_text", "size", "order")

    thumbnail = image_thumbnail("image")


# ---------------------------------------------------------------------------
# Testimonials
# ---------------------------------------------------------------------------


@admin.register(Testimonial)
class TestimonialAdmin(SortableAdminMixin, admin.ModelAdmin):
    list_display = ("patient_name", "youtube_url", "is_published", "order")
    list_filter = ("is_published",)


# ---------------------------------------------------------------------------
# Community photos
# ---------------------------------------------------------------------------


@admin.register(CommunityPhoto)
class CommunityPhotoAdmin(SortableAdminMixin, admin.ModelAdmin):
    list_display = ("thumbnail", "caption", "is_published", "order")
    list_filter = ("is_published",)

    thumbnail = image_thumbnail("image")


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


@admin.register(NewsPost)
class NewsPostAdmin(SortableAdminMixin, admin.ModelAdmin):
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
class FAQItemAdmin(SortableAdminMixin, admin.ModelAdmin):
    list_display = ("question", "is_published", "order")
    list_filter = ("is_published",)
    search_fields = ("question", "answer")


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


@admin.register(TeamMember)
class TeamMemberAdmin(SortableAdminMixin, admin.ModelAdmin):
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
class ContactMessageAdmin(admin.ModelAdmin):
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

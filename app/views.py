from django.shortcuts import render

from cms.models import (
    AboutContent,
    AwardLogo,
    CommunityPhoto,
    FAQItem,
    FeatureRow,
    HeroContent,
    NewsPost,
    ProblemCard,
    ProblemSectionContent,
    ProblemStat,
    SiteSettings,
    TeamMember,
    Testimonial,
)


def home(request):
    """The public homepage, rendered from staff-editable cms content.

    Every section of the template guards on its own context value, so any of
    these being empty makes that section disappear rather than render a broken
    shell — see the empty-state pass in the implementation plan (6.14).

    The singletons use `.load()`, which creates the row on first access, so a
    fresh install renders (with blank copy) instead of raising DoesNotExist.

    Naming note: the context key for `SiteSettings` is `site_settings`, not
    `settings` as sketched in design §8 — `{{ settings.x }}` in a Django
    template reads like `django.conf.settings` to anyone maintaining this
    later, which it very much is not.
    """
    hero = HeroContent.load()
    about = AboutContent.load()

    context = {
        "site_settings": SiteSettings.load(),
        "hero": hero,
        "about": about,
        "problem": ProblemSectionContent.load(),
        # `.all()` on the reverse FK relies on each child model's
        # Meta.ordering = ["order"], which adminsortable2 maintains.
        "hero_stats": hero.impact_stats.all(),
        "core_values": about.core_values.all(),
        "problem_cards": ProblemCard.objects.all(),
        "problem_stats": ProblemStat.objects.all(),
        "solutions": FeatureRow.objects.filter(category=FeatureRow.Category.SOLUTION),
        "why_us": FeatureRow.objects.filter(category=FeatureRow.Category.WHY_US),
        "awards": AwardLogo.objects.all(),
        "testimonials": Testimonial.objects.filter(is_published=True),
        "community_photos": CommunityPhoto.objects.filter(is_published=True),
        # Newest first, from the date itself — news is not drag-sortable,
        # see the docstring on NewsPostAdmin.
        "news_posts": NewsPost.objects.filter(is_published=True),
        "faqs": FAQItem.objects.filter(is_published=True),
        "team_members": TeamMember.objects.filter(is_active=True),
    }
    return render(request, "app/home.html", context)

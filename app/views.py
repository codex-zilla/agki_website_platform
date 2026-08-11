from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from cms.forms import CONTACT_SUBJECTS, ContactForm
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


def _home_context(contact_form=None):
    """Build the homepage context.

    Extracted from `home` so `contact_submit` can re-render the same page with
    a bound (invalid) form without duplicating any of it.

    Every section of the template guards on its own value here, so any of them
    being empty makes that section disappear rather than render a broken shell
    — see the empty-state pass in the implementation plan (6.14).

    The singletons use `.load()`, which creates the row on first access, so a
    fresh install renders (with blank copy) instead of raising DoesNotExist.

    Naming note: the context key for `SiteSettings` is `site_settings`, not
    `settings` as sketched in design §8 — `{{ settings.x }}` in a Django
    template reads like `django.conf.settings` to anyone maintaining this
    later, which it very much is not.
    """
    hero = HeroContent.load()
    about = AboutContent.load()

    return {
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
        "contact_form": contact_form if contact_form is not None else ContactForm(),
        "contact_subjects": CONTACT_SUBJECTS,
    }


def home(request):
    """The public homepage, rendered from staff-editable cms content.

    GET only by design: the contact form POSTs to `contact_submit` instead, so
    this view stays a pure read and can sit behind `cache_page` in Phase 8
    without a POST ever hitting a cached response path (design §9).
    """
    return render(request, "app/home.html", _home_context())


def _notify_new_contact_message(contact_message):
    """Email staff about a new contact-form submission.

    Sent to the address in Site settings (the one already published in the
    footer, and editable without a deploy), falling back to
    `DEFAULT_FROM_EMAIL` if an editor has cleared it.

    `fail_silently=True` is deliberate and safe here specifically because the
    row is already committed before this runs: a mail outage costs a
    notification, never the message itself, which is still sitting in the
    admin's Contact messages inbox.
    """
    site_settings = SiteSettings.load()
    recipient = site_settings.email or settings.DEFAULT_FROM_EMAIL
    if not recipient:
        return

    body = (
        f"New message from the AGKI website contact form.\n\n"
        f"Name:    {contact_message.name}\n"
        f"Email:   {contact_message.email}\n"
        f"Phone:   {contact_message.phone or '-'}\n"
        f"Subject: {contact_message.subject}\n\n"
        f"{contact_message.message}\n"
    )
    send_mail(
        subject=f"New contact form message: {contact_message.subject}",
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient],
        fail_silently=True,
    )


@require_POST
def contact_submit(request):
    """Handle a real contact-form submission.

    POST-only: a GET here would render the homepage at the wrong URL and give
    the page two addresses, so `require_POST` returns 405 instead.

    On success this redirects back to `home#contact` (POST/redirect/GET, so a
    refresh can't resubmit). On failure it re-renders the homepage in place
    with the bound form, which keeps what the visitor typed and shows the
    per-field errors — a redirect would silently discard both.
    """
    form = ContactForm(request.POST)
    if form.is_valid():
        contact_message = form.save()
        _notify_new_contact_message(contact_message)
        messages.success(request, "Thank you — we'll respond soon.")
        return redirect(f"{reverse('home')}#contact")

    messages.error(request, "Please check the highlighted fields and try again.")
    return render(request, "app/home.html", _home_context(form))

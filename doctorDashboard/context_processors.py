"""Numbers the dashboard chrome needs on every page (plan 5.13).

The sidebar showed a hardcoded `12` next to Appointments and a hardcoded `8`
on the notification bell. Two views computed a real `pending_count` that no
template read.

A context processor rather than adding the count to each view, for the same
reason §3.1 gives for `SiteSettings`: the sidebar renders on *every* page in
this app, so a per-view value would be right on two pages and stale or absent
on the rest — and a badge that is sometimes real is worse than one that is
obviously decorative, because nobody can tell which they are looking at.
"""

from django.templatetags.static import static
from django.utils.functional import SimpleLazyObject

from appointments.models import Appointment

from .scoping import scope_appointments


def console_brand(request):
    """The mark in the sidebar, from the same place the public site takes it.

    Separate from `dashboard_badges` because that one returns nothing for an
    account with no console role, and a superuser without a role does reach
    these pages - a brand that disappears for some staff is worse than one
    that costs a query.

    Lazy, so the query only happens on a page that renders the mark. This
    processor runs on every request in the project, including the public site
    and `/admin/`, neither of which reads this key.
    """
    def resolve():
        from cms.models import SiteSettings

        site = SiteSettings.load()
        return site.logo.url if site.logo else static("app/logo.png")

    return {"console_logo_url": SimpleLazyObject(resolve)}


def dashboard_badges(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    if not getattr(user, "role", ""):
        # Content Editors and the developer superusers reach `/admin/`, not
        # this console; there is no meaningful count for them.
        return {}

    from .views import DOCTOR_ACTION_STATUSES, WAITING_STATUSES

    waiting = scope_appointments(Appointment.objects.all(), user)
    return {
        "sidebar_waiting_count": waiting.filter(status__in=WAITING_STATUSES).count(),
        # The bell counts only what *this* user has to answer. Including the
        # patient's turn (`PENDING_MODE`) would nag a doctor about somebody
        # else's inaction, which is how a badge stops being read.
        "sidebar_action_count": waiting.filter(status__in=DOCTOR_ACTION_STATUSES).count(),
    }

"""`site_settings` for pages that are not the public home page.

`app.views` puts `SiteSettings` into the context of the pages it renders. The
patient booking flow needs the same object — the logo in its header, the phone
number and address in its footer — across seven views in another app, and a
patient stuck mid-booking should be one glance from a real phone number rather
than from a dead "Support" link.

Lazy, so the query only happens on a page that reads it. This processor runs on
every request in the project, and most of them (`/admin/`, the API, the console
pages that use `console_brand` instead) never touch it.

A view that passes its own `site_settings` still wins: context processors are
applied first and view context overrides them. That is what keeps `app.views`
authoritative for the public site.
"""

from django.utils.functional import SimpleLazyObject


def site_settings(request):
    def resolve():
        from .models import SiteSettings

        return SiteSettings.load()

    return {"site_settings": SimpleLazyObject(resolve)}

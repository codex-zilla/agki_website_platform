"""Adds the AGKI stat cards to the default admin index page.

Django's AdminSite.index() already accepts extra_context, but there's no
setting to inject extra_context into the /admin/ route without either
(a) subclassing AdminSite and re-pointing every existing @admin.register
call at the new instance, or (b) patching the method on the running
instance. (a) would touch every admin.py in the project for no real
benefit; (b) is the standard, narrowly-scoped way to do this and is what
we do here, applied once at app startup via CmsConfig.ready().

This only wraps admin.site (the one instance every app in this project
registers against) — it does not touch the AdminSite class globally, so
nothing else in Django is affected.
"""

import types

from django.contrib.admin.sites import AdminSite


def _cms_dashboard_stats():
    # Imported lazily inside the function, not at module level - this
    # module is imported from AppConfig.ready(), which runs before the
    # app registry is fully populated; importing models at that point
    # can raise AppRegistryNotReady.
    from .models import ContactMessage, NewsPost, TeamMember, Testimonial

    return {
        "cms_stats": {
            "published_news": NewsPost.objects.filter(is_published=True).count(),
            "team_members": TeamMember.objects.filter(is_active=True).count(),
            "unread_messages": ContactMessage.objects.filter(is_read=False).count(),
            "live_testimonials": Testimonial.objects.filter(is_published=True).count(),
        }
    }


def patch_admin_index_with_stats(site):
    """Wrap `site.index` so every render of /admin/ carries cms_stats in
    its context, without touching the AdminSite class or re-registering
    any models.
    """
    original_index = site.index

    def index(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context.update(_cms_dashboard_stats())
        return original_index(request, extra_context)

    site.index = types.MethodType(index, site)

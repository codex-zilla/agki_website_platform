"""Access-control decorators for the operational consoles.

Design §4.1 describes four independent access mechanisms, and the bug this
module exists to prevent is conflating them:

* ``is_superuser`` / ``is_staff`` are Django flags about the *admin site*.
* ``CustomUser.role`` says which **operational console** an account belongs
  to. It is not a Django permission and nothing in Django enforces it.

Before this, ``doctorDashboard`` enforced none of the four. Four of its URLs
returned a clean 200 to anonymous visitors and the rest returned 500 — because
``AnonymousUser`` blew up inside a queryset filter, not because anything
refused them. A crash is not an access control.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def role_required(*roles):
    """Require an authenticated account whose ``role`` is one of ``roles``.

    Anonymous visitors are redirected to the sign-in page (Django's usual
    behaviour, and what makes the "every URL bounces anonymous users" test
    meaningful). A signed-in account with the wrong role gets a 403 — it is
    not a login problem, and bouncing them to a login form they are already
    past would just loop.

    **Superusers pass every role check.** The two design sections disagree on
    this in their letter: §4.1 says ``is_superuser`` grants "everything",
    while §4.1.1 lists the superuser developer accounts under "no
    doctorDashboard access". Resolving it towards §4.1 because the strict
    reading buys nothing real — a superuser already reads and writes every
    appointment through ``/admin/`` — while costing the two accounts that
    maintain this system their access to it. Roles gate *jobs*, and the
    superuser flag is a separate axis, which is exactly why §4.4's
    ecommerce rule (superuser-only) is described as stricter than any role
    rather than as another role.
    """

    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not (user.is_superuser or user.role in roles):
                raise PermissionDenied(
                    f"This page is for {' or '.join(roles)} accounts."
                )
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def superuser_required(view):
    """Require ``is_superuser`` — stricter than any role (design §4.4).

    Used for the frozen ecommerce surface. Deliberately *not*
    ``user_passes_test``: that redirects a signed-in non-superuser to the
    login page, which reads as "sign in again" for something no amount of
    signing in will grant.
    """

    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied("This page is restricted to superusers.")
        return view(request, *args, **kwargs)

    return wrapper

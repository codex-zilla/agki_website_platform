"""Email verification codes: issuing them, checking them, and slowing guesses.

One module because the rules were spread across three places that disagreed.
The signal in `useraccount.signals` built a code and an email; `resend_otp`
built a second copy of the same email with different wording; and
`verify_email` checked the code with no limit on how many times it could be
asked. Nothing linked to the page at all, so in practice the whole thing was
reachable only through the URL inside the email.

**A verification code is now a login credential.** Using it signs the account
in (that is the flow AGKI asked for on 2026-08-31), so it has to be treated
like one rather than like a formality:

* only one code is valid at a time — issuing a new one deletes the old, so
  resending narrows the guess surface instead of widening it;
* a used code is deleted, so it cannot be replayed;
* codes expire after `OTP_TTL`, which is short because the email arrives in
  seconds and the code is worth stealing;
* attempts are rate-limited per IP **and** per account, because six digits is
  a million guesses and a million guesses is not many.

Not to be confused with `useraccount.utils.send_otp`, which is a *login*
second-factor from an experiment that was never wired up — the alternative
`signin` it belonged to is still in `views.py`, commented out.
"""

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

# `hit` and `client_ip` are a general fixed-window limiter over Django's cache
# (design §8.2). It lives in `appointments` because the booking lookup needed
# it first; it imports nothing from that app, so this is a cross-app import of
# a utility rather than a dependency between the two apps. Copying it here to
# avoid that would be the same mistake as the duplicated email body above.
from appointments.ratelimit import client_ip, hit
from .models import OtpToken

#: How long a code stays usable. Two minutes was already the value in the
#: signal and in the email copy; keeping it means the wording stays true.
OTP_TTL = timezone.timedelta(minutes=2)

#: How long before another code can be requested. Deliberately shorter than
#: `OTP_TTL`: a registrant whose email is slow should not have to wait for the
#: code they never received to expire before asking for another.
RESEND_COOLDOWN = timezone.timedelta(seconds=60)

#: Guessing limits. Per IP, so one attacker cannot spread guesses across
#: accounts; per account, so they cannot spread them across addresses either.
ATTEMPT_LIMIT = 5
ATTEMPT_WINDOW = 10 * 60


def latest_code(user):
    """The one code that counts, or `None`.

    `.last()` was used here before, on a model with no `Meta.ordering`, which
    means "whatever the database returns last" rather than "the newest". It
    happened to work; it is not something to rely on while it decides whether
    somebody can sign in.
    """
    return OtpToken.objects.filter(user=user).order_by("otp_created_at", "id").last()


def issue_code(user):
    """Replace any outstanding code with a fresh one and email it.

    The delete is the point, not housekeeping. Every resend used to leave the
    previous code valid until it expired, so asking twice doubled the number
    of codes that would let someone in.
    """
    OtpToken.objects.filter(user=user).delete()
    token = OtpToken.objects.create(
        user=user, otp_expires_at=timezone.now() + OTP_TTL
    )
    _email(user, token)
    return token


def seconds_until_resend(user):
    """Seconds left on the cooldown; 0 when another code can be requested."""
    token = latest_code(user)
    if token is None or token.otp_created_at is None:
        return 0
    remaining = (token.otp_created_at + RESEND_COOLDOWN) - timezone.now()
    return max(0, int(remaining.total_seconds()))


def seconds_until_expiry(user):
    """Seconds the current code has left; 0 if there is none or it is spent."""
    token = latest_code(user)
    if token is None or token.otp_expires_at is None:
        return 0
    return max(0, int((token.otp_expires_at - timezone.now()).total_seconds()))


def is_rate_limited(request, user):
    """True when this attempt should be refused without being checked.

    Both counters are hit on every attempt rather than short-circuiting on the
    first: an attacker who trips the per-account limit must not get a free
    pass on the per-IP one by switching targets.
    """
    by_ip = hit("otp-verify-ip", client_ip(request), ATTEMPT_LIMIT, ATTEMPT_WINDOW)
    by_account = hit("otp-verify-user", str(user.pk), ATTEMPT_LIMIT, ATTEMPT_WINDOW)
    return by_ip or by_account


def mask_email(address):
    """`baraka.mk26@gmail.com` -> `b********6@gmail.com`.

    The page has to say *where* the code went — "check your email" is useless
    to somebody who has two — without printing the address in full for whoever
    is looking over their shoulder or reading a screenshot in a support thread.
    """
    local, _, domain = (address or "").partition("@")
    if not domain:
        return address or ""
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


def _email(user, token):
    """One body, sent from one place.

    The signal and `resend_otp` each had their own copy, worded differently,
    so a registrant who asked for a second code got a different-looking email
    than the first — which is exactly what a phishing filter, and a suspicious
    person, both notice.
    """
    verification_url = (
        f"{settings.SITE_URL}"
        f"{reverse('verify-email', kwargs={'username': user.username})}"
    )
    send_mail(
        "Email Verification - Your OTP Code",
        (
            f"Dear {user.get_full_name() or user.username},\n\n"
            f"Your verification code is: {token.otp_code}\n\n"
            f"It expires in {int(OTP_TTL.total_seconds() // 60)} minutes. "
            f"Enter it on this page to finish setting up your account:\n"
            f"{verification_url}\n\n"
            f"If you did not create an account, you can ignore this email."
        ),
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        fail_silently=False,
    )

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone
from .models import OtpToken, CustomUser, Profile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_profile(sender, instance, created, **kwargs):
    """Every account gets a Profile at creation (design §5.3).

    They used to be created lazily by the `profile()` view, which meant a
    doctor who had never opened their own profile page was **invisible to the
    public booking site** — `search_results` iterates Profile rows, so no
    profile meant no listing, no matter what their role said. Three real
    accounts were in that state.

    `get_or_create` rather than `create` because this fires for every save
    where `created` is True, and a duplicate would raise on the OneToOne.

    Skipped entirely during fixture loading (`raw=True`). A dump carries its
    own Profile rows, so creating one here would race the fixture's own — and
    `--natural-primary` omits primary keys, so the fixture's row arrives as an
    insert and collides with the one this just made. Same flag, same reason as
    `create_token` below.
    """
    if kwargs.get("raw"):
        return
    if created:
        Profile.objects.get_or_create(user=instance)


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_token(sender, instance, created, **kwargs):
    # `raw=True` means this save is `loaddata` restoring a fixture, not a
    # person signing up. Django sets it for exactly this reason, and skipping
    # is not a nicety here: without it, importing the production accounts into
    # a new database treats all six as brand-new self-signups. Rehearsing the
    # SQLite -> MySQL migration is what caught it — the import **deactivated
    # five of six real accounts** and generated OTP tokens for them, and with
    # live SMTP would have emailed five people a verification code they never
    # asked for.
    #
    # First, before anything else in this handler: a fixture already carries
    # whatever `is_approved` and `is_active` it is supposed to have, and none
    # of the decisions below apply to a row being restored.
    if kwargs.get("raw"):
        return

    # A `createsuperuser` special case used to sit here. Plan 1.10 needed it
    # because `is_approved` defaulted to False, so a fresh superuser could not
    # use the site's own login form until it approved itself through the
    # admin. Decision F flips that default (plan 10.1), so the workaround has
    # nothing left to work around — and two mechanisms asserting the same
    # thing is one more than can be kept in step. The outcome it guaranteed is
    # still guarded, by `AccountApprovalTests
    # .test_a_new_superuser_is_approved_automatically`.

    if created and not instance.is_superuser:
        # Check if this user was created by social login
        # We can check this by looking at the session or by checking if they have a usable password
        
        # Social users (Google) don't have a usable password
        if not instance.has_usable_password():
            # This is a social login user (Google) - activate them
            print(f"Social user detected: {instance.username}")
            instance.is_active = True
            instance.save()
            print(f"Google user {instance.username} activated automatically")
        else:
            # This is a regular signup user - require OTP verification
            print(f"Regular user detected: {instance.username}")
            # Held until the emailed code is used. `.update()` rather than
            # `instance.save()` so this does not re-enter the signal.
            CustomUser.objects.filter(id=instance.id).update(is_active=False)

            # The code and its email used to be built here, and a second,
            # differently worded copy lived in `resend_otp`. Both come from
            # `useraccount.otp` now, so a registrant who asks for another
            # code gets the same email rather than a different-looking one.
            # Imported inside the handler because `otp` imports `.models`,
            # which is the module this signal is registered from.
            from .otp import issue_code

            issue_code(instance)

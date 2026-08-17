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

    if created and instance.is_superuser and not instance.is_approved:
        # `is_approved` gates sign-in as of plan 1.10, and it defaults to
        # False. Without this, `manage.py createsuperuser` produces an account
        # that cannot use the site's own login form and has to approve itself
        # through the admin first — a footgun with no upside, since
        # `is_superuser` already means "everything" (design §4.1).
        # `.update()` rather than `.save()` to avoid re-entering this signal.
        CustomUser.objects.filter(pk=instance.pk).update(is_approved=True)
        instance.is_approved = True

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
            otp_token = OtpToken.objects.create(
                user=instance, 
                otp_expires_at=timezone.now() + timezone.timedelta(minutes=2)
            )
            
            # Ensure user is not active
            CustomUser.objects.filter(id=instance.id).update(is_active=False)
            
            # Send OTP email
            subject = 'Email Verification - Your OTP Code'
            verification_url = (
                f"{settings.SITE_URL}"
                f"{reverse('verify-email', kwargs={'username': instance.username})}"
            )
            message = f"""
                Dear {instance.username},
                Your OTP code is: {otp_token.otp_code}
                This code will expire in 2 minutes. 
                Use the url below to verify your account:
                {verification_url}
                If you did not request this, please ignore this email.      
            """
            sender = settings.EMAIL_HOST_USER
            receiver = [instance.email]
            send_mail(
                subject, 
                message, 
                sender, 
                receiver, 
                fail_silently=False
            )

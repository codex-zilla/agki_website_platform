from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import reverse
from .models import CustomUser
from allauth.account.models import EmailAddress


class AccountAdapter(DefaultAccountAdapter):
    """Apply the admin-approval gate to allauth's login paths too.

    `EmailBackend.user_can_authenticate` covers the site's own sign-in form,
    but allauth logs users in through its own backend and never calls it — so
    without this, "Sign in with Google" was a way around the approval gate
    that plan 1.10 exists to create. Which is worse than no gate, because the
    admin toggling the switch would believe it worked.

    `save_user`/`pre_social_login` below set `is_active` for social accounts;
    neither sets `is_approved`, and deliberately so — approving an account is
    a decision a human makes.
    """

    def pre_login(self, request, user, **kwargs):
        response = super().pre_login(request, user, **kwargs)
        if response is not None:  # allauth already refused (e.g. inactive)
            return response
        if not user.is_approved:
            messages.warning(
                request,
                'Your account is pending admin approval. You will be able to '
                'sign in once an administrator approves it.',
            )
            return HttpResponseRedirect(reverse('signin'))
        return None


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        email = sociallogin.account.extra_data.get('email')
        
        if not email:
            return
        
        if sociallogin.is_existing:
            user = sociallogin.user
            # Activate existing user
            if not user.is_active:
                user.is_active = True
                user.save()
                
            email_address, created = EmailAddress.objects.get_or_create(
                user=user,
                email=email
            )
            if not email_address.verified:
                email_address.verified = True
                email_address.save()

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        
        # Activate social users immediately
        user.is_active = True
        user.save()
        
        email = user.email
        email_address, created = EmailAddress.objects.get_or_create(
            user=user,
            email=email
        )
        if not email_address.verified:
            email_address.verified = True
            email_address.save()
            
        print(f"Social user {user.username} activated and verified")
        return user
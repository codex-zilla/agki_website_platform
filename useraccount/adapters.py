from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import reverse
from .models import CustomUser
from .notices import ACCESS_WITHDRAWN
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
            messages.warning(request, ACCESS_WITHDRAWN)
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
        """A Google registration is a doctor registering (plan 10.4).

        Only doctors self-register on this platform (design §0.1, decision F):
        patients have no accounts at all (§2), and administrators and content
        editors are created by a superuser. So "somebody new arriving through
        Google" has exactly one meaning, and `role` is assigned from that
        meaning rather than asked for — which keeps the rule §4.1.1 states,
        that `role` is server-assigned from a closed set and never read out of
        a request. Google ends up the strictest of the three routes: the
        public form still lets a registrant choose between two values, and
        this chooses for them.

        **This runs only for a genuinely new account.** allauth calls it on
        the social *signup* path; an existing user signing in with Google is
        matched on their address and connected by
        `SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT` without passing
        through here — so the lab administrator or a content editor who
        happens to use Google keeps the role they already have. That has its
        own test, because it is an assumption this method rests on rather than
        something it enforces itself.

        Before this, `role` was left empty, and that single omission is what
        stopped "register with Google" working at all: a role-less account is
        answered **403** by every dashboard route (plan 1.3), so the doctor
        arrived signed in, approved, and locked out of the one console they
        came for.
        """
        user = super().save_user(request, sociallogin, form)

        user.role = CustomUser.Role.DOCTOR
        # An `is_active = True` sat here, and the 10.4 mutation check proved
        # it was dead — then proved the thing it was duplicating is dead too.
        # `signals.create_token` activates social signups explicitly, so this
        # looked like the redundant copy (plan 5.3 and 5.6, same call); but
        # breaking *that* branch changes nothing either. The reason is
        # simpler than either mechanism: `is_active` defaults to True, and
        # only the form branch ever sets it False, to hold an account until
        # its emailed code is used. A social signup is active because nothing
        # deactivated it, not because two places activated it.
        #
        # So `test_a_google_registration_can_sign_in_at_once` guards an
        # outcome that today follows from a field default. It is worth
        # keeping and worth not overstating: it will catch somebody later
        # deciding social registrations should be held for review, which is a
        # plausible answer to design §4.5 and would silently strand every
        # Google doctor if it landed unnoticed.
        #
        # Worth naming what that account state means next to decision F:
        # Google has already verified the address, so there is no one-time
        # code to wait for, and approval is granted at registration. A Google
        # registration clears both gates at once — the shortest path there is
        # from a stranger to a listed doctor. That is design §4.5's open
        # problem arriving here, not a defect in this method.
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
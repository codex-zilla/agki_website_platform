from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from .models import CustomUser
from allauth.account.models import EmailAddress

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
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone
from .models import OtpToken, CustomUser

@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_token(sender, instance, created, **kwargs):
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

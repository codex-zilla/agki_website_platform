import pyotp
from datetime import timedelta
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings

def send_otp(user):
    totp = pyotp.TOTP(pyotp.random_base32(), interval=120)  # OTP valid for 2 minutes
    otp = totp.now()

    # Save OTP details in user record
    user.otp_secret_key = totp.secret
    user.otp_valid_date = timezone.now() + timedelta(minutes=2)
    user.save(update_fields=['otp_secret_key', 'otp_valid_date'])

    # Send OTP via email
    subject = "Your One-Time Password (OTP)"
    message = f"""
    Hello {user.first_name or user.username},

    Your OTP is: {otp}
    It will expire in 2 minutes.

    If you did not request this, please ignore this email.

    Regards,
    Your Website Team
    """
    from_email = settings.DEFAULT_FROM_EMAIL
    recipient_list = [user.email]

    send_mail(subject, message, from_email, recipient_list)

    print(f"OTP {otp} sent to {user.email}")  # For debugging

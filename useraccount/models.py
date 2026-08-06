import secrets
from django.db import models
from django.contrib.auth.models import AbstractUser


class CustomUser(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        DOCTOR = "doctor", "Doctor"
        RECEPTIONIST = "receptionist", "Receptionist"

    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=15, null=True, blank=True, default="null")
    image = models.ImageField(blank=True, null=True)
    # Account type - drives permissions/business logic (e.g. who shows up
    # as bookable on the public site). Admin-assigned only, not
    # self-editable - see useraccount/admin.py CustomUserAdmin and the
    # (now removed) role field on the self-service profile-edit form.
    role = models.CharField(max_length=20, choices=Role.choices, blank=True)
    # Free-text professional credentials (e.g. "MD, MBBS, PhD") - split out
    # from `role` because the two were previously the same field doing two
    # unrelated jobs, which is how e.g. "4491" ended up as someone's role.
    qualifications = models.CharField(max_length=255, blank=True)
    signature = models.ImageField(blank=True, null=True)
    # login otp verification
    otp_secret_key = models.CharField(max_length=255, blank=True, null=True)
    otp_valid_date = models.DateTimeField(blank=True, null=True)
    # added so as to add admin approval to login
    is_approved = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.username} ({self.email})"

class Profile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    gender = models.CharField(max_length=20, blank=True, null=True)
    bio = models.TextField(blank=True, null=True)
    location = models.CharField(max_length=100, blank=True, null=True)
    google_map_embed = models.TextField(blank=True, null=True)
    birth_date = models.DateField(blank=True, null=True)
    expertise = models.CharField(max_length=255, blank=True, null=True)
    region = models.CharField(max_length=100, blank=True, null=True)
    years_of_experience = models.IntegerField(blank=True, null=True)
    languages_spoken = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return f"Profile of {self.user.username}"

def generate_otp():
    # Returns a 6-digit numeric OTP
    return str(secrets.randbelow(10**6)).zfill(6)


class OtpToken(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    otp_code = models.CharField(max_length=6, default=generate_otp)
    otp_created_at = models.DateTimeField(auto_now_add=True)
    otp_expires_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"OTP for {self.user.email}: {self.otp_code}"

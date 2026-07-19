from django import forms
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm, AuthenticationForm
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from .models import *

# Get your custom user model
User = get_user_model()  # This will automatically use your CustomUser model

class CustomPasswordResetForm(PasswordResetForm):
    email = forms.EmailField(
        label="Email",
        max_length=254,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter your email address',
            'autocomplete': 'email'
        })
    )
    
    def clean_email(self):
        email = self.cleaned_data['email']
        # Using your CustomUser model
        if not User.objects.filter(email=email).exists():
            raise ValidationError("No user found with this email address.")
        return email

class CustomSetPasswordForm(SetPasswordForm):
    new_password1 = forms.CharField(
        label="New password",
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter new password',
            'autocomplete': 'new-password'
        }),
        strip=False,
        help_text="Your password must contain at least 8 characters and cannot be too common."
    )
    new_password2 = forms.CharField(
        label="New password confirmation",
        strip=False,
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Confirm new password',
            'autocomplete': 'new-password'
        }),
    )

class CustomLoginForm(AuthenticationForm):
    username = forms.EmailField(  # Changed to EmailField since you use email as username
        label="Email",
        max_length=254,
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter your email address',
            'autocomplete': 'username',
            'autofocus': True
        })
    )
    
    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter your password',
            'autocomplete': 'current-password'
        })
    )
    
    remember_me = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={
            'class': 'form-check-input'
        })
    )

    error_messages = {
        'invalid_login': "Please enter a correct email and password. "
                        "Note that both fields may be case-sensitive.",
        'inactive': "This account is inactive.",
    }        




# change password ==========================================================
from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth import authenticate
from .models import CustomUser

class CustomPasswordChangeForm(PasswordChangeForm):
    old_password = forms.CharField(
        label="Current Password",
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter current password'
        })
    )
    new_password1 = forms.CharField(
        label="New Password",
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter new password'
        })
    )
    new_password2 = forms.CharField(
        label="Confirm New Password",
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Confirm new password'
        })
    )

    def clean_old_password(self):
        old_password = self.cleaned_data.get("old_password")
        user = self.user
        
        if not authenticate(username=user.username, password=old_password):
            raise forms.ValidationError("Your current password was entered incorrectly. Please enter it again.")
        
        return old_password

    class Meta:
        model = CustomUser
        fields = []

# ============================================================================================

class ProfileEditForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ['bio', 'location', 'birth_date']
class UserEditForm(forms.ModelForm):
    class Meta:
        model = CustomUser
        fields = ['first_name', 'last_name', 'email','image', 'phone', 'role']
        
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
    """Everything about a doctor that a patient sees (plan 11.6).

    **This form used to offer three fields of the ten on `Profile`**, and the
    seven it left out included `specialties` - which the patient search filters
    on (plan 4.2) and the browse grid counts (§5.5). So the feature AGKI asked
    for was fed by a field no doctor had any way to fill in, and the one live
    value in the table was put there by a management command.

    `specialties` and `qualifications` are **picked from lists**, never typed.
    The evidence for that is in the data: `Profile.expertise` is the one
    free-text professional field anybody ever completed and its single value is
    "Cardiograph" - an ECG machine - while `CustomUser.qualifications` is empty
    for every doctor in the database.

    Two fields are still not offered, deliberately. `google_map_embed` is a
    block of provider HTML rather than anything a doctor writes, and `gender`
    was not part of the design AGKI approved; both stay editable only through
    the admin rather than being added unasked.
    """

    class Meta:
        model = Profile
        fields = [
            'qualifications', 'specialties', 'bio',
            'region', 'location', 'languages_spoken',
            'years_of_experience', 'birth_date',
        ]
        widgets = {
            # Rendered by the template as pellets and a search box; the
            # underlying control stays a plain multiple-select so the form
            # works with JavaScript switched off.
            'qualifications': forms.CheckboxSelectMultiple,
            'specialties': forms.CheckboxSelectMultiple,
            # **`type="date"` has to be asked for.** Django's `DateInput`
            # renders `type="text"`, so a `DateField` gets a plain text box and
            # no calendar - which is what AGKI saw, and why 11.7's styling for
            # `input[type="date"]` matched nothing at all.
            #
            # `format` matters as much as `type`: a browser date control only
            # shows a value it can parse, and only `YYYY-MM-DD` qualifies.
            # Without it an existing date renders as "1 September 2026" and the
            # field comes up **empty**, which reads as data loss.
            'birth_date': forms.DateInput(
                attrs={'type': 'date'}, format='%Y-%m-%d'
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Retired rows stay linked to whoever already holds them but are not
        # offered again - which is what `is_active` is for, and the reason the
        # admin can retire a specialty without deleting anybody's link to it.
        self.fields['qualifications'].queryset = Qualification.objects.filter(
            is_active=True
        )
        self.fields['specialties'].queryset = Specialty.objects.filter(is_active=True)


class UserEditForm(forms.ModelForm):
    class Meta:
        model = CustomUser
        # `role` intentionally excluded - it is the account-type field that
        # drives permissions, assigned by the server and never read out of a
        # request (design §4.1.1).
        #
        # `qualifications` is excluded as of plan 11.6. The free-text column
        # stays on the model for a release, with nothing writing to it, the
        # same way `Profile.expertise` is being retired - so an existing value
        # can still be read while no new one can be typed. A later migration
        # drops both.
        fields = ['first_name', 'last_name', 'email', 'image', 'phone']
        
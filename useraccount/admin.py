from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import *
# Register your models here.


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    """Plain `admin.site.register(CustomUser)` (the previous registration)
    has no password-hashing widget — UserAdmin is what actually makes the
    "Add user" / "change password" forms hash correctly. Without it, a
    password typed into the default admin form is saved as literal
    plaintext and the account can never log in.
    """

    model = CustomUser
    list_display = ("username", "email", "role", "is_staff", "is_approved", "is_active")
    list_filter = UserAdmin.list_filter + ("role", "is_approved")

    fieldsets = UserAdmin.fieldsets + (
        ("AGKI profile", {"fields": ("phone", "image", "role", "qualifications", "signature", "is_approved")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            # Django's UserAdmin normally keeps is_staff/groups off the add
            # form (create minimal account, then edit to grant access) -
            # included here so granting e.g. Content Editors doesn't need
            # a second trip through the form.
            "fields": ("username", "email", "role", "is_staff", "groups", "password1", "password2"),
        }),
    )

    def save_model(self, request, obj, form, change):
        is_new = obj.pk is None
        super().save_model(request, obj, form, change)
        if is_new:
            # useraccount/signals.py's post_save handler treats any brand
            # new account with a real (hashed) password as a public
            # self-signup: it force-sets is_active=False and emails a
            # one-time code that expires in 2 minutes. That's the right
            # behavior for the public /useraccount/signup/ form, but an
            # account an admin just created here is already vetted by the
            # person creating it — there's no one else to verify an email
            # address for. Undo the signal's effect for this path only.
            CustomUser.objects.filter(pk=obj.pk).update(is_active=True, is_approved=True)
            OtpToken.objects.filter(user=obj).delete()


admin.site.register(OtpToken)
admin.site.register(Profile)
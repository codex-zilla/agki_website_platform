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


class SuperuserOnlyAdmin(admin.ModelAdmin):
    """Visible and editable only to superusers (design §4.3, binding).

    Not merely "we didn't grant the Content Editors group these permissions".
    That is true too — `cms/migrations/0003_content_editors_group.py`
    enumerates that group's permissions and nothing here is in the list — but
    it relies on nobody ever running a well-meaning "grant all". This makes
    the rule code, so granting the permission is not enough to get in.

    Used for booking *configuration*: appointment settings, the specialty
    taxonomy. Note `cms.SiteSettings` is deliberately **not** in this
    category — shared company identity stays with the CMS editors (decision
    6). The boundary is identity vs. configuration, not which app renders it.
    """

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(Specialty)
class SpecialtyAdmin(SuperuserOnlyAdmin):
    """The taxonomy patients search doctors by (§5.5).

    Intentionally empty until AGKI names their real specialties (step 9.6) —
    seeding it from here, or from a migration, would be putting words in their
    mouth and creating rows somebody then has to hunt down.
    """

    list_display = ("name", "slug", "is_active", "order", "doctor_count")
    list_editable = ("is_active", "order")
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}

    @admin.display(description="doctors")
    def doctor_count(self, obj):
        return obj.doctors.count()


admin.site.register(OtpToken)
admin.site.register(Profile)
"""Admin registrations for the booking models.

Two rules shape this file:

* **Configuration is superuser-only** (design §4.3). `AppointmentSettings`
  changes how booking behaves, so the Content Editors group must not reach it,
  and neither should a `role=doctor` account — providers manage their own
  availability and patients, not the system.
* **The audit trail is not editable.** `AppointmentTransition` and
  `NotificationLog` answer "who confirmed this, and was the patient ever told"
  months later. A record anyone can edit answers neither.

`Rating` also loses its add form here, and that is not housekeeping: it was
registered with a plain `admin.site.register`, someone added two ratings by
hand, and they were still sitting in the database with no visit attached when
migration 0021 came to give the model a mandatory link to one.
"""

from django.contrib import admin

from useraccount.admin import SuperuserOnlyAdmin

from .models import (
    Appointment,
    AppointmentApproval,
    AppointmentNote,
    AppointmentSettings,
    AppointmentTransition,
    AvailableSlot,
    NotificationLog,
    Rating,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Look, filter and export; never add or edit."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(AppointmentSettings)
class AppointmentSettingsAdmin(SuperuserOnlyAdmin):
    """One row, superuser-only. `clean()` on the model refuses a booking
    notice that is shorter than the confirmation cut-off — the combination
    that would make every booking expire the moment it is created."""

    def has_add_permission(self, request):
        return False  # singleton: the row is created by load()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        from django.urls import reverse

        if not request.user.is_superuser:
            return super().changelist_view(request, extra_context)
        settings_row = AppointmentSettings.load()
        return redirect(
            reverse("admin:appointments_appointmentsettings_change", args=[settings_row.pk])
        )


@admin.register(AppointmentTransition)
class AppointmentTransitionAdmin(ReadOnlyAdmin):
    list_display = ("appointment", "from_status", "to_status", "actor", "created_at")
    list_filter = ("to_status", "created_at")
    search_fields = ("appointment__bookingID", "appointment__fullName", "note")
    date_hierarchy = "created_at"


@admin.register(NotificationLog)
class NotificationLogAdmin(ReadOnlyAdmin):
    list_display = ("event", "recipient", "channel", "status", "created_at")
    list_filter = ("channel", "status", "event")
    search_fields = ("recipient", "appointment__bookingID")
    date_hierarchy = "created_at"


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("bookingID", "fullName", "status", "slot", "appointmentMode", "created_at")
    list_filter = ("status", "appointmentMode", "visitType", "reasonVisit")
    search_fields = ("bookingID", "fullName", "email", "phone")
    date_hierarchy = "created_at"
    # Editable in the admin for correcting a genuine mistake, but `status` is
    # not something to nudge by hand once `transition_to()` exists (plan 4.1):
    # a status changed here writes no transition row, so the audit trail will
    # not explain it. Left editable deliberately — an admin who needs to fix a
    # stuck booking at 9pm should be able to — but that is why the transition
    # log records what it does know rather than pretending to be complete.
    readonly_fields = ("bookingID", "created_at", "updated_at")


@admin.register(AvailableSlot)
class AvailableSlotAdmin(admin.ModelAdmin):
    list_display = ("user", "date", "time", "is_booked")
    list_filter = ("is_booked", "date")
    search_fields = ("user__username", "user__email")


@admin.register(Rating)
class RatingAdmin(ReadOnlyAdmin):
    """Read-only until the feedback feature exists (decision 7).

    Nothing writes ratings yet, so anything appearing here would be
    hand-typed — which is exactly how two orphan rows came to block a
    migration. Delete is left available so those can be cleared.
    """

    list_display = ("appointment", "provider", "rating_value", "recommended", "created_at")
    list_filter = ("rating_value", "recommended")


admin.site.register(AppointmentApproval, ReadOnlyAdmin)
admin.site.register(AppointmentNote)

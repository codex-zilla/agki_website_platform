"""`Appointment.slot`: ForeignKey -> OneToOneField, CASCADE -> PROTECT.

The riskiest migration in the project, and the reason `audit_appointment_data`
exists. Two things change:

**OneToOne.** Double-booking was never a race condition — `booking_form` never
read `is_booked` at all, so two *sequential* bookings on the same slot both
succeeded (design §10 #6). Production may therefore already violate this
constraint, which is why the check below runs first and reports the specific
rows rather than letting the database raise an opaque IntegrityError.

**PROTECT.** `CASCADE` meant deleting an availability slot silently deleted the
patient's appointment and their only record of it. The delete-slot endpoint is
supposed to refuse that (plan 5.6); `PROTECT` makes the database enforce what
the view is meant to check, rather than trusting every future caller to.
"""

from django.db import migrations, models
from django.db.models import Count
import django.db.models.deletion


def check_no_slot_has_two_appointments(apps, schema_editor):
    AvailableSlot = apps.get_model("appointments", "AvailableSlot")

    oversubscribed = (
        AvailableSlot.objects.annotate(bookings=Count("appointment"))
        .filter(bookings__gt=1)
        .order_by("date", "time")
    )
    if not oversubscribed.exists():
        return

    Appointment = apps.get_model("appointments", "Appointment")
    lines = []
    for slot in oversubscribed:
        booking_ids = list(
            Appointment.objects.filter(slot_id=slot.id).values_list("bookingID", flat=True)
        )
        lines.append(f"slot id={slot.id} {slot.date} {slot.time}: {booking_ids}")

    raise RuntimeError(
        "Cannot make Appointment.slot a OneToOneField: these slots hold more "
        "than one appointment, so somebody has to decide which patient keeps "
        "each one — and be told.\n  "
        + "\n  ".join(lines)
        + "\n\nMove or cancel the extra bookings, then re-run `manage.py "
        "migrate`. `manage.py audit_appointment_data` lists them with the "
        "patient names and booking dates."
    )


def noop(apps, schema_editor):
    """Nothing to undo — the check only reads."""


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0019_dedupe_and_unique_slots"),
    ]

    operations = [
        migrations.RunPython(check_no_slot_has_two_appointments, noop),
        migrations.AlterField(
            model_name="appointment",
            name="slot",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                to="appointments.availableslot",
            ),
        ),
    ]

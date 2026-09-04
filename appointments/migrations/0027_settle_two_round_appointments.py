"""Move appointments caught mid-negotiation into the single-confirmation flow.

AGKI collapsed the two provider confirmations into one on 2026-09-03 (plan
12.1). Two states existed only to express the gap between them:

* ``MODE_PROPOSED`` — the patient had chosen a mode and the doctor was about to
  confirm it. That is exactly what ``PENDING_TIME`` means now: waiting for the
  doctor's one decision, with everything they need to make it.
* ``PENDING_MODE`` — the doctor had confirmed the time and the patient had not
  yet chosen a mode. **There is no page left for them to choose on**, so these
  rows would sit waiting for an action nobody can take.

The second case is the awkward one and it is handled by picking a mode rather
than by leaving the row stuck: ``patient_travels``, the lowest-commitment
option for the doctor and the one AGKI's own description treats as ordinary. It
is not a silent edit — every row gets an ``AppointmentTransition`` saying the
flow changed and that the mode was assigned here, so the doctor sees it in the
audit trail before confirming and the patient sees it on their own page, where
cancelling is one click.

Deliberately not reversible in the sense of restoring the old state: the
appointments this touches are live bookings, and inventing a "put it back
half-negotiated" step would be a second guess at data the first guess already
moved. The reverse is a no-op, which leaves the rows valid under either flow.
"""

from django.db import migrations

#: Written into the audit trail so the change has a name in the history rather
#: than appearing as an unexplained status jump.
NOTE = (
    "Booking flow changed: the doctor now confirms the time and the meeting "
    "mode together. %s"
)
MODE_KEPT = "Your chosen mode was kept."
MODE_ASSIGNED = (
    "No mode had been chosen yet, so this was set to the patient travelling to "
    "the provider. Cancel and rebook if that does not suit."
)


def settle(apps, schema_editor):
    Appointment = apps.get_model("appointments", "Appointment")
    Transition = apps.get_model("appointments", "AppointmentTransition")

    waiting = Appointment.objects.filter(
        status__in=["pending_mode", "mode_proposed"]
    )

    for appointment in waiting:
        was = appointment.status
        assigned = not appointment.appointmentMode
        if assigned:
            appointment.appointmentMode = "patient_travels"
        appointment.status = "pending_time"
        appointment.save(update_fields=["status", "appointmentMode"])

        # `actor` stays null: nobody decided this, a migration did, and the
        # note is where that is said.
        Transition.objects.create(
            appointment=appointment,
            from_status=was,
            to_status="pending_time",
            actor=None,
            note=NOTE % (MODE_ASSIGNED if assigned else MODE_KEPT),
        )

    if waiting.model.objects.filter(status__in=["pending_mode", "mode_proposed"]).exists():
        raise RuntimeError("appointments were left mid-negotiation")


def leave_as_they_are(apps, schema_editor):
    """No-op. See the module docstring: these are live bookings, and a second
    guess at them would be worse than the first."""


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0026_slot_meeting_modes"),
    ]

    operations = [
        migrations.RunPython(settle, leave_as_they_are),
    ]

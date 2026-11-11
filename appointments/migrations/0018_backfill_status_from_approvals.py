"""Backfill `Appointment.status` from `AppointmentApproval` (plan 2.6).

`is_approved=True` -> CONFIRMED; False or no approval row at all ->
PENDING_TIME (which is already the column default, so this migration only has
to move the approved ones).

Every existing approval also becomes a seed `AppointmentTransition`, with the
actor and timestamp taken from `approved_by`/`approved_at`, so the history the
old table held survives its retirement rather than being thrown away with it.

**The backfill is lossy in one direction, and that is correct.** A historically
approved appointment lands in CONFIRMED with `appointmentMode` blank, because
the old model never held a mode. Do not invent one: "we don't know how this
appointment was going to happen" is a true statement about pre-existing data,
and guessing would put a fact in the record that nobody ever said.

`AppointmentApproval` itself is left in place, read-only, for one release
(plan 2.7) — cheap insurance against a mistake here being unrecoverable.
"""

from django.db import migrations


def backfill_status(apps, schema_editor):
    Appointment = apps.get_model("appointments", "Appointment")
    AppointmentApproval = apps.get_model("appointments", "AppointmentApproval")
    AppointmentTransition = apps.get_model("appointments", "AppointmentTransition")

    confirmed = 0
    seeded = 0

    for approval in AppointmentApproval.objects.select_related("appointment"):
        appointment = approval.appointment
        if approval.is_approved:
            appointment.status = "confirmed"
            # The old table had no separate "time confirmed" moment, so its
            # single approval timestamp is the best available answer.
            appointment.time_confirmed_at = approval.approved_at
            appointment.save(update_fields=["status", "time_confirmed_at"])
            confirmed += 1

        transition = AppointmentTransition.objects.create(
            appointment=appointment,
            from_status="",
            to_status="confirmed" if approval.is_approved else "pending_time",
            actor_id=approval.approved_by_id,
            note="Seeded from AppointmentApproval during the Phase 2 backfill.",
        )
        seeded += 1

        if approval.approved_at:
            # `created_at` is auto_now_add, so it just stamped this row with
            # the deploy's timestamp. Put the real approval time back: an audit
            # trail claiming every historic approval happened during a
            # migration is worse than one that admits it doesn't know.
            AppointmentTransition.objects.filter(pk=transition.pk).update(
                created_at=approval.approved_at
            )

    total = Appointment.objects.count()
    print(
        f"  backfilled {total} appointment(s): {confirmed} -> confirmed, "
        f"{total - confirmed} left at pending_time; "
        f"{seeded} seed transition(s) written"
    )


def unbackfill(apps, schema_editor):
    """Reverse: drop the seeded transitions and put every status back to the
    default. Deliberately does not try to reconstruct `AppointmentApproval`
    rows — they were never deleted, so they are still there."""
    Appointment = apps.get_model("appointments", "Appointment")
    AppointmentTransition = apps.get_model("appointments", "AppointmentTransition")

    AppointmentTransition.objects.filter(
        note__startswith="Seeded from AppointmentApproval"
    ).delete()
    Appointment.objects.update(status="pending_time", time_confirmed_at=None)


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0017_appointment_state_machine"),
    ]

    operations = [
        migrations.RunPython(backfill_status, unbackfill),
    ]

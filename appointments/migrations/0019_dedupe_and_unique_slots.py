"""Dedupe availability slots, then make duplicates impossible (plan 2.8).

`AvailableSlot` had no uniqueness constraint and the database has duplicates to
prove it — two identical rows for one doctor, date and time, i.e. two bookable
slots for one real appointment.

The dedupe is automatic **only where it is safe**: a duplicate copy with no
appointment on it is deleted, keeping whichever copy is actually spoken for. If
two copies of the same slot each carry an appointment, this refuses to run,
because choosing which patient keeps the slot is not a decision a migration
gets to make silently. `audit_appointment_data` reports both cases ahead of
time so this is never the first anyone hears of it.

Note the ordering: this runs while `slot` is still a CASCADE ForeignKey, so
deleting a slot that *did* carry an appointment would take the appointment with
it. That is exactly why only appointment-free copies are touched here, and why
0020 (`PROTECT`) comes after.
"""

from django.db import migrations
from django.db.models import Count


def dedupe_slots(apps, schema_editor):
    AvailableSlot = apps.get_model("appointments", "AvailableSlot")
    Appointment = apps.get_model("appointments", "Appointment")

    duplicate_groups = (
        AvailableSlot.objects.values("user_id", "date", "time")
        .annotate(copies=Count("id"))
        .filter(copies__gt=1)
        .order_by("user_id", "date", "time")
    )

    unresolvable = []
    deleted = 0

    for group in duplicate_groups:
        rows = list(
            AvailableSlot.objects.filter(
                user_id=group["user_id"], date=group["date"], time=group["time"]
            ).order_by("id")
        )
        spoken_for = [
            row for row in rows if Appointment.objects.filter(slot_id=row.id).exists()
        ]

        if len(spoken_for) > 1:
            unresolvable.append(
                f"user_id={group['user_id']} {group['date']} {group['time']}: "
                f"slot ids {[row.id for row in spoken_for]} each carry an appointment"
            )
            continue

        # Prefer the copy with an appointment; then a booked one; then the
        # oldest, since anything pointing at this slot by id most likely points
        # at the one that has been there longest.
        if spoken_for:
            keeper = spoken_for[0]
        else:
            keeper = next((row for row in rows if row.is_booked), rows[0])

        for row in rows:
            if row.id != keeper.id:
                row.delete()
                deleted += 1

    if unresolvable:
        raise RuntimeError(
            "Cannot apply the AvailableSlot uniqueness constraint: some "
            "duplicated slots each carry an appointment, and deciding which "
            "patient keeps the slot is a human's call, not a migration's.\n  "
            + "\n  ".join(unresolvable)
            + "\n\nResolve these by moving one appointment to another slot (or "
            "cancelling it, telling the patient either way), then re-run "
            "`manage.py migrate`. `manage.py audit_appointment_data` lists them."
        )

    if deleted:
        print(f"  removed {deleted} duplicate availability slot(s) with no appointment")


def noop(apps, schema_editor):
    """Irreversible in substance: the deleted rows were duplicates and are
    gone. Declared so the constraint below can still be un-applied."""


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0018_backfill_status_from_approvals"),
    ]

    operations = [
        migrations.RunPython(dedupe_slots, noop),
        migrations.AlterUniqueTogether(
            name="availableslot",
            unique_together={("user", "date", "time")},
        ),
    ]

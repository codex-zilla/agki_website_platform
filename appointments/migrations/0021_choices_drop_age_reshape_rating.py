"""Close the free-text fields, drop the dead column, reshape Rating (plan 2.10-2.12).

The normalisation below folds case and punctuation only — "Follow-Up" becomes
`follow_up`, and that is the entire licence it has. Anything it cannot place
stops the migration with the offending values listed, because this codebase has
already paid for the alternative: `CustomUser.role` was free text, production
accumulated `role='4491'`, and a real doctor silently vanished from the booking
page. A migration that quietly picks a plausible choice for a value nobody
understands reproduces that bug with better formatting.

`Rating` is reshaped rather than dropped (decision 7). Nothing has ever created
one, so the table should be empty; if it is not, that assumption was wrong
somewhere and the migration stops instead of inventing an `appointment` link
for rows it knows nothing about.
"""

from django.db import migrations, models
import django.core.validators
import django.db.models.deletion


VISIT_TYPES = {"new_patient": "New patient", "follow_up": "Follow-up"}

REASONS_FOR_VISIT = {
    "lab_test": "Lab test / panel",
    "referred_test": "Doctor-referred test",
    "result_followup": "Result collection / follow-up",
    "consultation": "Consultation",
    "other": "Other",
}

GENDERS = {
    "male": "Male",
    "female": "Female",
    "other": "Other",
    "prefer_not_to_say": "Prefer not to say",
}


def _normalise(value):
    return "_".join(str(value).strip().lower().replace("-", " ").replace("_", " ").split())


def _suggest(value, choices):
    """The choice key `value` obviously means, or None if it needs a human."""
    candidate = _normalise(value)
    if candidate in choices:
        return candidate
    for key, label in choices.items():
        if candidate == _normalise(label):
            return key
    return None


def normalise_choice_fields(apps, schema_editor):
    Appointment = apps.get_model("appointments", "Appointment")

    unmappable = []
    changed = 0

    for field, choices in (
        ("visitType", VISIT_TYPES),
        ("reasonVisit", REASONS_FOR_VISIT),
        ("gender", GENDERS),
    ):
        for stored in (
            Appointment.objects.values_list(field, flat=True).distinct()
        ):
            if stored in choices:
                continue
            mapped = _suggest(stored or "", choices)
            if mapped is None:
                affected = Appointment.objects.filter(**{field: stored}).count()
                unmappable.append(f"{field}={stored!r} ({affected} appointment(s))")
                continue
            changed += Appointment.objects.filter(**{field: stored}).update(
                **{field: mapped}
            )

    if unmappable:
        raise RuntimeError(
            "Cannot convert the clinical fields to closed choice lists: these "
            "stored values do not map to any choice, and guessing is how "
            "role='4491' happened.\n  "
            + "\n  ".join(unmappable)
            + "\n\nCorrect them (or add the missing choice to "
            "appointments.models.Appointment) and re-run `manage.py migrate`. "
            "`manage.py audit_appointment_data` lists them with counts."
        )

    # `appointmentMode` was null=True; it is `blank=True` from here on, so
    # "no mode yet" is one value rather than two that behave differently in
    # forms, templates and queries.
    blanked = Appointment.objects.filter(appointmentMode__isnull=True).update(
        appointmentMode=""
    )

    if changed or blanked:
        print(f"  normalised {changed} choice value(s), blanked {blanked} null mode(s)")


def check_rating_is_empty(apps, schema_editor):
    """Refuse to reshape `Rating` if it holds rows.

    The design said nothing in the codebase creates a Rating. True of the
    application — and false in practice: `appointments/admin.py` registered the
    model, so any superuser could add one by hand, and someone did. Two rows
    turned up here, on a doctor with no appointments at all.

    There is no correct automatic answer for those rows: the new schema says a
    rating belongs to exactly one visit, and a rating with no visit cannot be
    given one without inventing the link. So this stops and names them.
    """
    Rating = apps.get_model("appointments", "Rating")
    rows = list(Rating.objects.all().order_by("id"))
    if rows:
        # Only fields this migration does not rename are named here: `doctor`
        # becomes `provider` a few operations below, so referring to either one
        # would tie this message to whichever side of the rename the caller
        # happens to be on. The row ids are what an operator needs anyway.
        listed = "\n  ".join(
            f"id={row.id} value={row.rating_value} "
            f"created={row.created_at:%Y-%m-%d %H:%M} feedback={row.feedback!r}"
            for row in rows
        )
        raise RuntimeError(
            f"Rating holds {len(rows)} row(s). A rating now belongs to exactly "
            "one appointment, and these predate that link — there is no way to "
            "say which visit they refer to without guessing.\n  "
            + listed
            + "\n\nThey were almost certainly added by hand in the Django admin "
            "(the model was registered there). If that is what they are, delete "
            "them and re-run `manage.py migrate`. If any is real patient "
            "feedback, record what it says before deleting it — this schema has "
            "nowhere to put an unattached rating. `manage.py "
            "audit_appointment_data` reports them ahead of time."
        )
    Rating.objects.filter(feedback__isnull=True).update(feedback="")


def noop(apps, schema_editor):
    """Read-only checks and value normalisation; the original free text is not
    recoverable, and re-widening the columns is what the reverse of the schema
    operations below already does."""


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0020_appointment_slot_one_to_one"),
    ]

    operations = [
        migrations.RunPython(normalise_choice_fields, noop),
        migrations.RunPython(check_rating_is_empty, noop),
        migrations.AlterField(
            model_name="appointment",
            name="visitType",
            field=models.CharField(
                choices=[("new_patient", "New patient"), ("follow_up", "Follow-up")],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="appointment",
            name="reasonVisit",
            field=models.CharField(
                choices=[
                    ("lab_test", "Lab test / panel"),
                    ("referred_test", "Doctor-referred test"),
                    ("result_followup", "Result collection / follow-up"),
                    ("consultation", "Consultation"),
                    ("other", "Other"),
                ],
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="appointment",
            name="gender",
            field=models.CharField(
                choices=[
                    ("male", "Male"),
                    ("female", "Female"),
                    ("other", "Other"),
                    ("prefer_not_to_say", "Prefer not to say"),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="appointment",
            name="appointmentMode",
            field=models.CharField(
                blank=True,
                choices=[
                    ("online", "Online video call"),
                    ("provider_travels", "Provider travels to the patient"),
                    ("patient_travels", "Patient travels to the provider"),
                ],
                max_length=20,
            ),
        ),
        migrations.RemoveField(model_name="appointment", name="age"),
        migrations.RenameField(
            model_name="rating", old_name="doctor", new_name="provider"
        ),
        migrations.AddField(
            model_name="rating",
            name="appointment",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="appointments.appointment",
            ),
        ),
        migrations.AlterField(
            model_name="rating",
            name="appointment",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                to="appointments.appointment",
            ),
        ),
        migrations.AlterField(
            model_name="rating",
            name="rating_value",
            field=models.PositiveSmallIntegerField(
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(5),
                ]
            ),
        ),
        migrations.AlterField(
            model_name="rating",
            name="feedback",
            field=models.TextField(blank=True),
        ),
    ]

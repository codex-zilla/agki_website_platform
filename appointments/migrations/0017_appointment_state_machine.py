"""Additive half of the Phase 2 model work (design §11 step 1, plan 2.1/2.4/2.5/2.14).

Nothing here can fail on existing data: every column is added with a default or
as nullable, and the three new tables are empty. The migrations that *can* fail
(0019's uniqueness, 0020's one-to-one, 0021's choice lists) are separate files
on purpose, so a rollout that hits one of them has already banked this.
"""

from django.conf import settings
from django.db import migrations, models
import django.core.validators
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("appointments", "0016_appointmentapproval_approved_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending_time", "Awaiting provider's confirmation of the time"),
                    ("time_declined", "Time declined by provider"),
                    ("pending_mode", "Time confirmed - awaiting patient's choice of mode"),
                    ("mode_proposed", "Awaiting provider's confirmation of the mode"),
                    ("confirmed", "Confirmed"),
                    ("completed", "Completed"),
                    ("cancelled", "Cancelled"),
                    ("no_show", "No-show"),
                    ("expired", "Expired - not confirmed in time"),
                ],
                default="pending_time",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="appointment",
            name="meeting_url",
            field=models.URLField(blank=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="time_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="mode_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="expired_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="cancelled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="cancellation_reason",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name="appointment",
            name="symptoms",
            field=models.TextField(blank=True),
        ),
        migrations.CreateModel(
            name="AppointmentTransition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("from_status", models.CharField(blank=True, max_length=20)),
                ("to_status", models.CharField(max_length=20)),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "appointment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transitions",
                        to="appointments.appointment",
                    ),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="NotificationLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event", models.CharField(max_length=50)),
                ("recipient", models.CharField(max_length=255)),
                (
                    "channel",
                    models.CharField(
                        choices=[("email", "Email"), ("sms", "SMS")], max_length=10
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("sent", "Sent"), ("failed", "Failed"), ("skipped", "Skipped")],
                        max_length=10,
                    ),
                ),
                ("error", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "appointment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notifications",
                        to="appointments.appointment",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="AppointmentSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "minimum_booking_notice_minutes",
                    models.PositiveIntegerField(
                        default=120,
                        help_text=(
                            "A slot cannot be booked once it is closer than this. Must be "
                            "greater than the confirmation cut-off. AGKI: 2 hours."
                        ),
                    ),
                ),
                (
                    "confirmation_cutoff_minutes",
                    models.PositiveIntegerField(
                        default=30,
                        help_text=(
                            "An appointment still unanswered this close to its slot expires "
                            "and the slot is released. AGKI: 30 minutes."
                        ),
                    ),
                ),
                (
                    "cancellation_cutoff_minutes",
                    models.PositiveIntegerField(
                        default=120,
                        help_text=(
                            "A patient may cancel up to this long before the slot. Decision 9 "
                            "left the value open - confirm it with AGKI."
                        ),
                    ),
                ),
            ],
            options={
                "verbose_name": "Appointment settings",
                "verbose_name_plural": "Appointment settings",
                "abstract": False,
            },
        ),
    ]

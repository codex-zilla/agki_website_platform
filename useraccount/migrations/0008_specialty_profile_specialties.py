"""`Specialty` taxonomy + `Profile.specialties` (plan 2.2, design §5.5).

Schema only, and deliberately **unseeded**. Design §11 step 5a describes
seeding the taxonomy here, but the specialties are AGKI's to name (step 9.6) —
a migration that invents "Cardiology, Dermatology, ..." would be putting words
in their mouth and creating rows someone then has to hunt down and delete.

`expertise` is left in place. Migrating it needs the taxonomy to exist first,
and the one live value ("Cardiograph" — an ECG machine, not a specialty) needs
a person to decide what it meant. That is `manage.py
map_expertise_to_specialties`, run after seeding; the column is dropped only
once the mapping is confirmed.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("useraccount", "0007_role_qualifications_split"),
    ]

    operations = [
        migrations.CreateModel(
            name="Specialty",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=80, unique=True)),
                ("slug", models.SlugField(unique=True)),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Uncheck to retire a specialty without deleting it or the links to it.",
                    ),
                ),
                (
                    "order",
                    models.PositiveIntegerField(default=0, help_text="Position in the browse grid."),
                ),
            ],
            options={
                "verbose_name_plural": "specialties",
                "ordering": ["order", "name"],
            },
        ),
        migrations.AddField(
            model_name="profile",
            name="specialties",
            field=models.ManyToManyField(
                blank=True, related_name="doctors", to="useraccount.specialty"
            ),
        ),
    ]

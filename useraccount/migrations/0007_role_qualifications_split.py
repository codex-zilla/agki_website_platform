# Splits CustomUser.role (previously free text, doing double duty as both
# "account type" via signup.html and "professional qualifications" via
# profile_edit.html - see docs/website-cms-implementation-plan.md Phase 4
# addendum 2) into:
#   - role: now choice-constrained (admin/doctor/receptionist)
#   - qualifications: new free-text field for credentials
#
# Data cleanup runs BEFORE the schema change on purpose. role goes from
# null=True to null=False (blank=True stays - empty string is now the
# "unset" representation, not NULL, matching normal CharField convention).
# Cleaning existing NULL/invalid values first avoids ever asking Django's
# schema editor to enforce NOT NULL against a row that still holds NULL.
#
# The two garbage values found in production ('4491' on username
# "Dr. Magembe", '1' on username "Kyla") were identified by hand and
# confirmed with AGKI to both actually be doctors - not guessed. Any
# OTHER value that isn't a known role is blanked rather than guessed at,
# with a printed report so it doesn't silently disappear.

from django.db import migrations, models

KNOWN_ROLES = {"admin", "doctor", "receptionist"}

# Specific accounts whose real role was confirmed by hand (see migration
# docstring above) rather than inferred automatically.
CONFIRMED_FIXES = {
    "Dr. Magembe": "doctor",
    "Kyla": "doctor",
}


def clean_role_data(apps, schema_editor):
    CustomUser = apps.get_model("useraccount", "CustomUser")

    for username, role in CONFIRMED_FIXES.items():
        updated = CustomUser.objects.filter(username=username).update(role=role)
        if updated:
            print(f"  useraccount 0007: set role={role!r} for username={username!r}")
        else:
            print(f"  useraccount 0007: WARNING - username={username!r} not found, skipped")

    # Normalize NULL -> '' (empty string is the one "unset" representation
    # going forward, not NULL).
    null_count = CustomUser.objects.filter(role__isnull=True).update(role="")
    if null_count:
        print(f"  useraccount 0007: normalized {null_count} NULL role value(s) to ''")

    # Anything left that isn't '' or a known role is unrecognized garbage -
    # blank it rather than guess, and report exactly who was affected so
    # it doesn't disappear silently (relevant if production has bad rows
    # beyond the two confirmed above).
    leftover = CustomUser.objects.exclude(role="").exclude(role__in=KNOWN_ROLES)
    for user in leftover:
        print(f"  useraccount 0007: blanking unrecognized role={user.role!r} for username={user.username!r} - needs manual follow-up")
    leftover.update(role="")


def noop_reverse(apps, schema_editor):
    # Deliberately not reversible: there is no way to recover what the
    # original free-text role/qualifications values were once merged back.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("useraccount", "0006_customuser_is_approved"),
    ]

    operations = [
        migrations.RunPython(clean_role_data, noop_reverse),
        migrations.AlterField(
            model_name="customuser",
            name="role",
            field=models.CharField(
                blank=True,
                choices=[
                    ("admin", "Admin"),
                    ("doctor", "Doctor"),
                    ("receptionist", "Receptionist"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="customuser",
            name="qualifications",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]

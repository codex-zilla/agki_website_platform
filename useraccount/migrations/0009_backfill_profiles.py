"""Give every account a `Profile` (plan 2.13, design §5.3).

Profiles were created lazily, by `get_or_create` in the `profile()` view the
first time someone opened their own profile page. Nothing created one at
account creation — and `search_results` iterates `Profile` rows, so **a doctor
who had never visited that page did not exist to the public booking site.**
Not hypothetical: three `role='doctor'` accounts in the current database are
invisible for exactly this reason, and two more views raised
`Profile.DoesNotExist` for them.

This backfills the rows. The `post_save` signal in `useraccount/signals.py`
stops the gap reopening, so a doctor's visibility follows from their role
rather than from whether they once clicked something.
"""

from django.db import migrations


def create_missing_profiles(apps, schema_editor):
    CustomUser = apps.get_model("useraccount", "CustomUser")
    Profile = apps.get_model("useraccount", "Profile")

    missing = CustomUser.objects.filter(profile__isnull=True).order_by("id")
    created = [Profile(user=user) for user in missing]
    if created:
        Profile.objects.bulk_create(created)
        print(f"  created {len(created)} missing profile(s)")


def noop(apps, schema_editor):
    """Not reversed: deleting profiles would throw away whatever a user has
    since put in them, and an empty profile costs nothing."""


class Migration(migrations.Migration):

    dependencies = [
        ("useraccount", "0008_specialty_profile_specialties"),
    ]

    operations = [
        migrations.RunPython(create_missing_profiles, noop),
    ]

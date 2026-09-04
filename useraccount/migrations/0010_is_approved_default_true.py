from django.db import migrations, models


class Migration(migrations.Migration):
    """`is_approved` defaults to True — decision F, plan 10.1.

    The default is the whole change, and the default is what the field
    *means*: it stops being **admission** (an administrator lets you in) and
    becomes **revocation** (an administrator switches you off). Doctors
    register themselves on this platform (design §0.1), so there is nobody
    whose job it is to work an approval queue, and a queue nobody works is a
    slower way of saying no.

    **Nothing about enforcement moves.** `EmailBackend.user_can_authenticate`
    and `AccountAdapter.pre_login` still refuse an account with this false,
    and the backend still refuses in `get_user`, so withdrawing approval ends
    a live session rather than only the next login (plan 1.10).

    **A changed default does not touch existing rows**, which is what 0011 is
    for. They are separate files but one release, and the ordering matters
    only in that this one alone would leave every account created before it
    exactly as locked out as it is today — the failure 1.10 already warned
    about, arriving a second time under a decision meant to prevent it.
    """

    dependencies = [
        ("useraccount", "0009_backfill_profiles"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customuser",
            name="is_approved",
            field=models.BooleanField(default=True),
        ),
    ]

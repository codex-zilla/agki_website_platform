from django.db import migrations


def approve_accounts_predating_decision_f(apps, schema_editor):
    """Grant `is_approved` to every account that predates decision F.

    0010 changed the default; a default only applies to rows created after
    it. Without this, the accounts that already exist keep the value the old
    meaning gave them — and under the new meaning that value reads as
    "an administrator switched this account off", which is false. Nobody
    switched them off. They were created before there was a switch.

    **Inactive accounts are approved too, deliberately.** `is_active` and
    `is_approved` are independent gates (design §4.2 point 5) and this
    migration is only qualified to speak about one of them. Skipping the
    inactive rows would look cautious and would strand exactly the wrong
    person: a self-signup part-way through email verification is inactive
    right now, and would verify their address only to meet a second refusal
    nobody ever intended them to meet.

    **It grants approval only to rows that exist when it runs**, which is
    inherent to a migration and is the reason this is a migration rather than
    a signal or a default-fixing management command anyone might re-run. It
    is a bulk grant on a security-relevant field, so its blast radius should
    be a fixed set of rows, decided once, on the day the decision landed.

    A data migration rather than a click-through in the admin because the
    same rows exist on production — six of ten locally, including both
    developer superusers and four doctors — and doing it by hand is how one
    gets missed. This replaces the manual pre-deploy approval sweep that
    Phase 1's checklist and plan 9.3 described.
    """
    CustomUser = apps.get_model("useraccount", "CustomUser")
    CustomUser.objects.filter(is_approved=False).update(is_approved=True)


class Migration(migrations.Migration):
    """Backfill for 0010 — decision F, plan 10.2.

    **The reverse is a deliberate no-op, and that is not laziness.** This
    migration cannot know which rows it changed, so the only reversal it
    could implement is "un-approve everybody" — which would switch off every
    account that was legitimately approved before or since, including the one
    the person rolling back needs in order to fix anything. Reversing the
    schema change in 0010 is safe and works; reversing its effect on data is
    not, so this says so rather than pretending.

    Practically: `migrate useraccount 0009` succeeds, and leaves the
    previously unapproved accounts approved. That asymmetry is the intended
    outcome of a rollback, not a defect of it.
    """

    dependencies = [
        ("useraccount", "0010_is_approved_default_true"),
    ]

    operations = [
        migrations.RunPython(
            approve_accounts_predating_decision_f,
            migrations.RunPython.noop,
        ),
    ]

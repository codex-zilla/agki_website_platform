"""Who sees whose appointments (design §4.1.1, §4.2 point 3).

Object-level scoping was written correctly in the original code — it just had
no auth wall in front of it, and no way for the lab administrator to see past
her own (empty) slot list. Plan 1.9 shipped her a route to the console and
said so explicitly: she lands on a dashboard filtered to `slot__user=self`,
which for an administrator who takes no appointments means nothing at all.
This is where that closes.

**The rule is `role`, not `is_superuser`.** A superuser who is also a doctor
exists in the live data (`mccoln`), and design §4.1.2 settles what they are:
"a doctor is a doctor first". Widening their dashboard to every doctor's
appointments would take away the page they actually use, to give them one they
already have in `/admin/`.
"""

from useraccount.models import CustomUser


def sees_every_doctor(user):
    """True for the lab administrator, false for a doctor."""
    return getattr(user, "role", "") == CustomUser.Role.ADMIN


def scope_appointments(queryset, user):
    if sees_every_doctor(user):
        return queryset
    return queryset.filter(slot__user=user)


def scope_slots(queryset, user):
    if sees_every_doctor(user):
        return queryset
    return queryset.filter(user=user)

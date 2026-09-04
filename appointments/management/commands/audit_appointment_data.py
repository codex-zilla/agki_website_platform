"""Pre-migration data audit for the appointment system redesign (plan §0.3, §0.4).

Phase 2 of the implementation plan applies constraints to tables that already
hold live bookings. Three of those constraints can *fail on real data*, and two
more need a human to make a mapping call that a migration must not guess. This
command reports all of them, so the answer to "is production safe to migrate?"
is a command anyone can re-run rather than a shell session someone once did.

    python manage.py audit_appointment_data
    python manage.py audit_appointment_data --strict      # exit 1 if blockers
    python manage.py audit_appointment_data --json        # machine-readable
    python manage.py audit_appointment_data --database=prod_copy

Read-only: it opens no transaction and writes nothing. Safe to run against
production directly.

**Several checks go permanently quiet once Phase 2 is applied, and that is the
point.** `duplicate_slots`, `multi_appointment_slots` and `orphan_ratings` ask
whether the data violates a constraint that does not exist yet; afterwards the
database enforces each of them, so the questions can only be answered "no". The
command's job is the window before that, on a production database that has not
been migrated — which is also why it must never assume the new columns exist.

**Severities.**

``BLOCKER``  A Phase 2 migration will fail (or silently lose data) until this
             is resolved by hand. These are the gate.
``REVIEW``   A person must decide something before the related step — a value
             mapping, a lockout risk. Not fatal, but not automatable either.
``INFO``     Scope sizing. Nothing to fix; useful for knowing how big a
             backfill is before running it.

The choice lists below are the *proposed* ones from design §5.1. They live here
rather than on the model because the model does not have them yet — that is
step 2.10. When 2.10 lands, delete these constants and import the model's
``TextChoices`` instead, so this command and the schema cannot drift apart.
"""

import json
import sys
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import connections
from django.db.models import Count

from appointments.models import Appointment, AppointmentApproval, AvailableSlot, Rating
from useraccount.models import CustomUser, Profile

# --- proposed choice sets (design §5.1) — see module docstring ---------------

VISIT_TYPES = {
    "new_patient": "New patient",
    "follow_up": "Follow-up",
}

REASONS_FOR_VISIT = {
    "lab_test": "Lab test / panel",
    "referred_test": "Doctor-referred test",
    "result_followup": "Result collection / follow-up",
    "consultation": "Consultation",
    "other": "Other",
}

# Provisional. Design §5.1 requires `gender` to become a closed set but does
# not enumerate it — AGKI confirms the members as part of step 2.10. Anything
# reported as unmappable here is a prompt for that conversation, not a defect.
GENDERS = {
    "male": "Male",
    "female": "Female",
    "other": "Other",
    "prefer_not_to_say": "Prefer not to say",
}

BLOCKER = "BLOCKER"
REVIEW = "REVIEW"
INFO = "INFO"

SEVERITY_ORDER = [BLOCKER, REVIEW, INFO]

# The report is written to be readable in a terminal, and this one is run from
# a Windows console as often as from the production shell. cmd.exe's default
# code page has no em dash or section sign, so text authored with them comes
# out as `?` at best. Translate at the write boundary rather than writing ugly
# source: see `Command.out`.
ASCII_FALLBACKS = {
    ord("—"): "-",     # em dash
    ord("–"): "-",     # en dash
    ord("…"): "...",   # ellipsis
    ord("§"): "sec ",  # section sign
    ord("’"): "'",
    ord("“"): '"',
    ord("”"): '"',
}


def normalise(value):
    """Fold a free-text value towards a choice key: 'Follow Up' -> 'follow_up'."""
    return "_".join(str(value).strip().lower().replace("-", " ").replace("_", " ").split())


def suggest(value, choices):
    """Return the choice key `value` obviously means, or None if it needs a human.

    Matches on the key and on the human label, both normalised. Deliberately
    conservative: it will not reach for a fuzzy match, because a near-miss that
    looks like a match is exactly how `expertise='Cardiograph'` happened.
    """
    candidate = normalise(value)
    if candidate in choices:
        return candidate
    for key, label in choices.items():
        if candidate == normalise(label):
            return key
    return None


class Finding:
    def __init__(self, code, severity, headline, blocks="", rows=None, detail=""):
        self.code = code
        self.severity = severity
        self.headline = headline
        self.blocks = blocks
        self.rows = rows or []
        self.detail = detail

    def as_dict(self):
        return {
            "code": self.code,
            "severity": self.severity,
            "headline": self.headline,
            "blocks": self.blocks,
            "detail": self.detail,
            "count": len(self.rows),
            "rows": self.rows,
        }


class Command(BaseCommand):
    help = "Audit appointment data for the known Phase 2 migration blockers (plan §0.3)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="default",
            help="Database alias to audit. Point this at a copy of production.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Max example rows printed per finding (default 20). 0 = no limit.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit 1 if any BLOCKER is found. For use as a migration gate.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit JSON instead of the human report.",
        )

    def handle(self, *args, **options):
        self.db = options["database"]
        findings = [
            check
            for check in (
                self.check_duplicate_slots(),
                self.check_slots_with_multiple_appointments(),
                self.check_free_text_choices(),
                self.check_missing_profiles(),
                self.check_expertise_values(),
                self.check_unknown_roles(),
                self.check_login_lockout_risk(),
                self.check_slot_booking_flags(),
                self.check_orphan_ratings(),
                self.check_approval_backfill(),
            )
            if check is not None
        ]

        if options["as_json"]:
            self.stdout.write(
                json.dumps(
                    {
                        "database": self.db,
                        "findings": [f.as_dict() for f in findings],
                        "blockers": sum(1 for f in findings if f.severity == BLOCKER),
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            self.report(findings, limit=options["limit"])

        blockers = sum(1 for f in findings if f.severity == BLOCKER)
        if options["strict"] and blockers:
            # SystemExit rather than CommandError: the command worked fine, the
            # data did not. A traceback would misrepresent which one failed.
            raise SystemExit(1)

    # --- checks ------------------------------------------------------------

    def check_duplicate_slots(self):
        """Plan 2.8 — `AvailableSlot.unique_together = (user, date, time)`."""
        dupes = (
            AvailableSlot.objects.using(self.db)
            .values("user_id", "date", "time")
            .annotate(n=Count("id"))
            .filter(n__gt=1)
            .order_by("user_id", "date", "time")
        )
        rows = []
        for group in dupes:
            ids = list(
                AvailableSlot.objects.using(self.db)
                .filter(
                    user_id=group["user_id"], date=group["date"], time=group["time"]
                )
                .order_by("id")
                .values_list("id", flat=True)
            )
            rows.append(
                {
                    "user_id": group["user_id"],
                    "user": self.username(group["user_id"]),
                    "date": str(group["date"]),
                    "time": str(group["time"]),
                    "copies": group["n"],
                    "slot_ids": ids,
                    "booked_slot_ids": list(
                        AvailableSlot.objects.using(self.db)
                        .filter(id__in=ids, is_booked=True)
                        .values_list("id", flat=True)
                    ),
                }
            )
        if not rows:
            return Finding(
                "duplicate_slots", INFO, "No duplicate availability slots.",
                blocks="2.8 unique_together",
            )
        return Finding(
            "duplicate_slots",
            BLOCKER,
            f"{len(rows)} doctor/date/time combination(s) have more than one AvailableSlot row.",
            blocks="2.8 — AvailableSlot.unique_together",
            rows=rows,
            detail=(
                "Dedupe before migrating. Keep the row carrying the appointment "
                "(or the booked one) and delete the rest; `booked_slot_ids` says "
                "which copies are spoken for. Deleting a copy with no appointment "
                "is safe."
            ),
        )

    def check_slots_with_multiple_appointments(self):
        """Plan 2.9 — `Appointment.slot` becomes a OneToOneField."""
        rows = []
        oversubscribed = (
            AvailableSlot.objects.using(self.db)
            .annotate(n=Count("appointment"))
            .filter(n__gt=1)
            .order_by("date", "time")
        )
        for slot in oversubscribed:
            rows.append(
                {
                    "slot_id": slot.id,
                    "doctor": self.username(slot.user_id),
                    "date": str(slot.date),
                    "time": str(slot.time),
                    # Named columns only, for the same reason as `username()`:
                    # the Appointment model declares `status`, `meeting_url`
                    # and friends, none of which exist before migration 0017.
                    "appointments": [
                        {
                            "id": row["id"],
                            "bookingID": row["bookingID"],
                            "patient": row["fullName"],
                            "created_at": str(row["created_at"]),
                        }
                        for row in Appointment.objects.using(self.db)
                        .filter(slot=slot)
                        .order_by("created_at")
                        .values("id", "bookingID", "fullName", "created_at")
                    ],
                }
            )
        if not rows:
            return Finding(
                "multi_appointment_slots", INFO,
                "No slot holds more than one appointment.",
                blocks="2.9 OneToOneField",
            )
        return Finding(
            "multi_appointment_slots",
            BLOCKER,
            f"{len(rows)} slot(s) hold more than one appointment.",
            blocks="2.9 — Appointment.slot -> OneToOneField",
            rows=rows,
            detail=(
                "These are real double-bookings (design §10 #6: booking_form never "
                "reads is_booked). Someone has to decide per slot which patient keeps "
                "it and what happens to the other — a migration must not pick. Resolve "
                "by moving the loser to another slot or cancelling it, with the patient "
                "told either way."
            ),
        )

    def check_free_text_choices(self):
        """Plan 2.10 — closed choice lists on visitType / reasonVisit / gender."""
        fields = [
            ("visitType", VISIT_TYPES),
            ("reasonVisit", REASONS_FOR_VISIT),
            ("gender", GENDERS),
        ]
        rows = []
        unmappable = 0
        for field, choices in fields:
            counts = Counter(
                Appointment.objects.using(self.db).values_list(field, flat=True)
            )
            for value, count in sorted(counts.items(), key=lambda item: -item[1]):
                mapped = suggest(value, choices)
                if value in choices:
                    continue  # already a valid key, nothing to do
                if mapped is None:
                    unmappable += 1
                rows.append(
                    {
                        "field": field,
                        "value": value,
                        "rows_affected": count,
                        "maps_to": mapped,
                    }
                )
        if not rows:
            return Finding(
                "free_text_choices", INFO,
                "visitType / reasonVisit / gender already hold only valid choice keys.",
                blocks="2.10 choices",
            )
        severity = REVIEW if unmappable else INFO
        headline = (
            f"{len(rows)} stored value(s) are not valid choice keys yet; "
            f"{unmappable} of them have no obvious mapping."
        )
        return Finding(
            "free_text_choices",
            severity,
            headline,
            blocks="2.10 — choices on visitType / reasonVisit / gender",
            rows=rows,
            detail=(
                "`maps_to` is filled in only where the value differs from a choice "
                "key by case or spacing. A null `maps_to` needs a person: pick the "
                "right key, or add one to the list. Note home_collection is no longer "
                "a reason for visit — it is the PROVIDER_TRAVELS mode (design §5.1)."
            ),
        )

    def check_missing_profiles(self):
        """Plan 0.4 / 2.13 — a doctor with no Profile is invisible to booking."""
        missing = list(
            CustomUser.objects.using(self.db)
            .filter(profile__isnull=True)
            .order_by("id")
            .values("id", "username", "role", "is_active")
        )
        total = CustomUser.objects.using(self.db).count()
        doctors_missing = [u for u in missing if u["role"] == CustomUser.Role.DOCTOR]
        if not missing:
            return Finding(
                "missing_profiles", INFO,
                f"Every account ({total}) has a Profile row.",
                blocks="2.13 Profile backfill",
            )
        return Finding(
            "missing_profiles",
            REVIEW if doctors_missing else INFO,
            f"{len(missing)} of {total} account(s) have no Profile row "
            f"({len(doctors_missing)} of them role=doctor).",
            blocks="2.13 — Profile auto-creation signal + backfill",
            rows=missing,
            detail=(
                "Profiles are created lazily by the profile() view, so a doctor who "
                "never opened their own profile page does not appear on the public "
                "booking page at all (design §5.3). The role=doctor rows here are "
                "currently unbookable; the rest are just backfill volume."
            ),
        )

    def check_expertise_values(self):
        """Plan 2.3 / 9.6 — expertise -> Specialty, reported not guessed."""
        counts = Counter(
            value
            for value in Profile.objects.using(self.db).values_list(
                "expertise", flat=True
            )
            if value and value.strip()
        )
        if not counts:
            return Finding(
                "expertise_values", INFO,
                "No profile carries an `expertise` value to migrate.",
                blocks="2.3 expertise -> specialties",
            )
        rows = [
            {"expertise": value, "profiles": count}
            for value, count in sorted(counts.items(), key=lambda item: -item[1])
        ]
        return Finding(
            "expertise_values",
            REVIEW,
            f"{len(rows)} distinct `expertise` value(s) need a Specialty mapping.",
            blocks="2.3 / 9.6 — expertise -> Profile.specialties",
            rows=rows,
            detail=(
                "Every one of these needs a human to name the Specialty it means "
                "before the data migration runs. Design §5.5 is explicit that the "
                "migration must not guess: 'Cardiograph' is an ECG machine, and only "
                "a person can say it was meant to read Cardiology."
            ),
        )

    def check_unknown_roles(self):
        """Cross-check for the role='4491' class of bug (design §0)."""
        valid = set(CustomUser.Role.values) | {""}
        counts = Counter(
            CustomUser.objects.using(self.db).values_list("role", flat=True)
        )
        rows = [
            {"role": value, "accounts": count}
            for value, count in counts.items()
            if value not in valid
        ]
        if not rows:
            return Finding(
                "unknown_roles", INFO, "All `role` values are valid choices.",
                blocks="doctor visibility",
            )
        return Finding(
            "unknown_roles",
            REVIEW,
            f"{len(rows)} account role value(s) are outside Role.choices.",
            blocks="doctor visibility on the public booking page",
            rows=rows,
            detail=(
                "`role` gained choices in useraccount migration 0007, but existing "
                "rows were not re-validated. An account with a role outside the set "
                "is silently excluded from every role-filtered query, including the "
                "public doctor list."
            ),
        )

    def check_login_lockout_risk(self):
        """Accounts that cannot sign in because approval was withdrawn.

        Written for plan 1.10, when this reported who *enforcing* the flag
        would lock out and the answer was "most of the staff". Decision F and
        the 10.2 backfill removed that question — every account that predates
        them is approved, and new ones are approved on creation — so on any
        migrated database this reports nothing unless somebody has been
        deliberately switched off.

        **Kept, with the wording corrected rather than the check deleted.**
        "These accounts cannot sign in" is still worth knowing before a
        deploy, and after 10.2 an unexplained row here is a more interesting
        finding than it used to be: it means an administrator made a decision,
        not that a default was never set. The finding id is unchanged because
        plan 0.3 and 9.2 name it.
        """
        at_risk = list(
            CustomUser.objects.using(self.db)
            .filter(is_active=True, is_approved=False)
            .order_by("id")
            .values("id", "username", "email", "role", "is_staff", "is_superuser")
        )
        if not at_risk:
            return Finding(
                "login_lockout_risk", INFO,
                "No active account has had its access withdrawn.",
                blocks="10.2 is_approved backfill",
            )
        return Finding(
            "login_lockout_risk",
            REVIEW,
            f"{len(at_risk)} active account(s) have is_approved=False and cannot "
            f"sign in.",
            blocks="10.2 — is_approved backfill",
            rows=at_risk,
            detail=(
                "Approval is granted at registration since decision F, and 10.2 "
                "backfilled every account that predates it — so a row here is an "
                "administrator having switched the account off, not a default nobody "
                "set. Confirm each one is meant to be switched off; anything "
                "unexplained is someone locked out who should not be."
            ),
        )

    def check_slot_booking_flags(self):
        """Plan 4.4 — is_booked is written but never read, so it can be wrong."""
        annotated = AvailableSlot.objects.using(self.db).annotate(
            n=Count("appointment")
        )
        booked_but_empty = list(
            annotated.filter(is_booked=True, n=0)
            .order_by("date", "time")
            .values("id", "date", "time")
        )
        taken_but_free = list(
            annotated.filter(is_booked=False, n__gt=0)
            .order_by("date", "time")
            .values("id", "date", "time")
        )
        if not booked_but_empty and not taken_but_free:
            return Finding(
                "slot_booking_flags", INFO,
                "`is_booked` agrees with the appointments on every slot.",
                blocks="4.4 real slot claim",
            )
        rows = [
            {"slot_id": r["id"], "date": str(r["date"]), "time": str(r["time"]),
             "problem": "is_booked=True with no appointment"}
            for r in booked_but_empty
        ] + [
            {"slot_id": r["id"], "date": str(r["date"]), "time": str(r["time"]),
             "problem": "has an appointment but is_booked=False"}
            for r in taken_but_free
        ]
        return Finding(
            "slot_booking_flags",
            REVIEW,
            f"{len(rows)} slot(s) have an `is_booked` flag that disagrees with reality.",
            blocks="4.4 — booking_form's real slot claim",
            rows=rows,
            detail=(
                "Nothing reads is_booked today, so nothing has kept it honest. Once "
                "4.4 starts enforcing it, a stale True hides a genuinely free slot and "
                "a stale False offers a taken one. Reconcile as part of Phase 4, not "
                "before — the flag is not load-bearing until then."
            ),
        )

    def check_orphan_ratings(self):
        """Plan 2.12 — `Rating` gains a mandatory link to an appointment.

        This check exists because the design's premise was wrong. It said
        nothing in the codebase creates a `Rating`; nothing in the
        *application* does, but the model was registered in the Django admin,
        so any superuser could add one by hand — and two turned up in the local
        database, on a doctor with no appointments. Those rows cannot be given
        the `appointment` link the new schema requires, so migration 0021
        refuses to run until someone decides what they are.
        """
        # Once 0021 has run, a Rating is attached to an appointment by the
        # schema itself, so every row is legitimate and there is nothing to
        # report. Before it runs, no row has a link and all of them block.
        # Which world we are in is a question about the database, not the
        # models — hence introspection rather than a query that would raise on
        # exactly the un-migrated databases this command exists to inspect.
        with connections[self.db].cursor() as cursor:
            columns = {
                column.name
                for column in connections[self.db].introspection.get_table_description(
                    cursor, Rating._meta.db_table
                )
            }
        if "appointment_id" in columns:
            return Finding(
                "orphan_ratings", INFO,
                "Rating is linked to appointments by the schema; nothing to check.",
                blocks="2.12 Rating reshape (already applied)",
            )

        # Only columns that exist both before and after 0021 are selected:
        # that migration also renames `doctor` to `provider`, and asking for
        # the provider here would crash on an un-migrated database. The
        # migration's own error message names it, where the historical model
        # still has the old field.
        rows = [
            {
                "id": row["id"],
                "rating_value": row["rating_value"],
                "created_at": str(row["created_at"]),
                "feedback": row["feedback"] or "",
            }
            for row in Rating.objects.using(self.db)
            .order_by("id")
            .values("id", "rating_value", "created_at", "feedback")
        ]
        if not rows:
            return Finding(
                "orphan_ratings", INFO, "Rating is empty, as the reshape expects.",
                blocks="2.12 Rating reshape",
            )
        return Finding(
            "orphan_ratings",
            BLOCKER,
            f"{len(rows)} Rating row(s) exist with no appointment to attach them to.",
            blocks="2.12 — Rating gains a mandatory `appointment` link",
            rows=rows,
            detail=(
                "A rating now belongs to exactly one visit, and these predate "
                "that link. Almost certainly added by hand in the Django admin. "
                "If that is what they are, delete them; if any is real patient "
                "feedback, record what it says first — the new schema has "
                "nowhere to put an unattached rating."
            ),
        )

    def check_approval_backfill(self):
        """Plan 2.6 — preview of the status backfill from AppointmentApproval."""
        total = Appointment.objects.using(self.db).count()
        approved = AppointmentApproval.objects.using(self.db).filter(
            is_approved=True
        ).count()
        not_approved = AppointmentApproval.objects.using(self.db).filter(
            is_approved=False
        ).count()
        no_approval_row = (
            Appointment.objects.using(self.db)
            .filter(appointmentapproval__isnull=True)
            .count()
        )
        rows = [
            {"outcome": "CONFIRMED", "appointments": approved,
             "from": "AppointmentApproval.is_approved=True"},
            {"outcome": "PENDING_TIME", "appointments": not_approved,
             "from": "AppointmentApproval.is_approved=False"},
            {"outcome": "PENDING_TIME", "appointments": no_approval_row,
             "from": "no AppointmentApproval row"},
        ]
        return Finding(
            "approval_backfill",
            INFO,
            f"{total} appointment(s) to backfill: {approved} -> CONFIRMED, "
            f"{not_approved + no_approval_row} -> PENDING_TIME.",
            blocks="2.6 — backfill status from AppointmentApproval",
            rows=rows,
            detail=(
                "Every CONFIRMED row lands with appointmentMode blank, because the "
                "old model never held one. That is the intended lossy direction "
                "(plan 2.6) — do not invent a mode."
            ),
        )

    # --- output ------------------------------------------------------------

    def username(self, user_id):
        # `.values_list` rather than fetching the object: loading a CustomUser
        # selects every column the *model* declares, including ones added by
        # migrations the target database has not run yet. Production is two
        # apps behind, so `SELECT *` here raised "no such column:
        # qualifications" and took the whole audit down — on exactly the
        # un-migrated database this command exists to inspect.
        name = (
            CustomUser.objects.using(self.db)
            .filter(id=user_id)
            .values_list("username", flat=True)
            .first()
        )
        return name or f"<deleted user {user_id}>"

    def out(self, text, style=None):
        """Write a line the terminal can actually render.

        Typographic characters are translated first; anything else the target
        encoding can't take is replaced rather than raising, so a patient name
        with an accent in it can never crash the audit.
        """
        text = str(text).translate(ASCII_FALLBACKS)
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        text = text.encode(encoding, errors="replace").decode(encoding)
        self.stdout.write(style(text) if style else text)

    def report(self, findings, limit):
        style = {
            BLOCKER: self.style.ERROR,
            REVIEW: self.style.WARNING,
            INFO: self.style.SUCCESS,
        }
        self.out("")
        self.out(f"Appointment data audit - database '{self.db}'", self.style.MIGRATE_HEADING)
        self.out(
            f"{Appointment.objects.using(self.db).count()} appointments, "
            f"{AvailableSlot.objects.using(self.db).count()} slots, "
            f"{CustomUser.objects.using(self.db).count()} accounts."
        )
        self.out("")

        for severity in SEVERITY_ORDER:
            for finding in (f for f in findings if f.severity == severity):
                self.out(f"[{severity}] {finding.code} - {finding.headline}", style[severity])
                if finding.blocks:
                    self.out(f"    blocks: {finding.blocks}")
                shown = finding.rows if limit == 0 else finding.rows[:limit]
                for row in shown:
                    self.out(f"    - {row}")
                hidden = len(finding.rows) - len(shown)
                if hidden > 0:
                    self.out(f"    ... and {hidden} more (use --limit 0)")
                if finding.detail and severity != INFO:
                    self.out(f"    {finding.detail}")
                self.out("")

        blockers = [f for f in findings if f.severity == BLOCKER]
        reviews = [f for f in findings if f.severity == REVIEW]
        if blockers:
            self.out(
                f"{len(blockers)} blocker(s): Phase 2 migrations will fail on this "
                f"data until they are resolved.",
                self.style.ERROR,
            )
        elif reviews:
            self.out(
                f"No blockers. {len(reviews)} item(s) need a decision before the "
                f"steps they name.",
                self.style.WARNING,
            )
        else:
            self.out("Clean: no blockers, nothing awaiting a decision.", self.style.SUCCESS)
        self.out("")

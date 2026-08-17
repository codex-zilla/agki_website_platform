"""Tests for the appointments app.

Currently covers Phase 0 of the implementation plan: the pre-migration data
audit (plan §0.3). The rest arrives with Phase 1 onwards.

The audit exists to answer "is production safe to migrate?", so its own
failure mode matters more than most: a check that silently never fires reads
exactly like a clean database. The local dev DB holds **zero appointments**,
which means the two riskiest checks — the ones guarding the `OneToOneField`
and the choice-list conversion — return "clean" there without ever having been
exercised. These tests construct each blocker on purpose and assert it is
found, so a green audit against production means something.
"""

import json
from datetime import date, time, timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from appointments.management.commands.audit_appointment_data import (
    GENDERS,
    REASONS_FOR_VISIT,
    VISIT_TYPES,
    suggest,
)
from appointments.models import Appointment, AppointmentApproval, AvailableSlot
from cms.test_utils import make_staff_user
from useraccount.models import CustomUser, Profile


def run_audit(**options):
    """Run the audit and return its findings keyed by check code.

    Asserts against `--json` rather than the printed report: the report's
    wording is meant to change as the plan does, its structure is not.
    """
    out = StringIO()
    call_command("audit_appointment_data", "--json", stdout=out, **options)
    payload = json.loads(out.getvalue())
    return payload, {finding["code"]: finding for finding in payload["findings"]}


class SuggestMappingTests(TestCase):
    """`suggest()` decides what a migration may do without asking a human."""

    def test_matches_a_choice_key_regardless_of_case_and_spacing(self):
        self.assertEqual(suggest("Follow Up", VISIT_TYPES), "follow_up")
        self.assertEqual(suggest("  NEW-PATIENT ", VISIT_TYPES), "new_patient")

    def test_matches_the_human_label(self):
        self.assertEqual(
            suggest("Result collection / follow-up", REASONS_FOR_VISIT), "result_followup"
        )
        self.assertEqual(suggest("Consultation", REASONS_FOR_VISIT), "consultation")
        self.assertEqual(suggest("Female", GENDERS), "female")

    def test_refuses_a_near_miss(self):
        """The whole point: `expertise='Cardiograph'` must not resolve to
        Cardiology by a fuzzy match. Near-misses go to a person."""
        self.assertIsNone(suggest("Cardiograph", REASONS_FOR_VISIT))
        self.assertIsNone(suggest("follow", VISIT_TYPES))
        self.assertIsNone(suggest("home_collection", REASONS_FOR_VISIT))


class AppointmentDataAuditTests(TestCase):
    """Each known Phase 2 blocker, built deliberately and detected."""

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="dr-audit",
            email="dr-audit@example.com",
            role=CustomUser.Role.DOCTOR,
        )

    def make_slot(self, day=None, at=time(9, 0), **extra):
        return AvailableSlot.objects.create(
            user=self.doctor,
            date=day or (timezone.localdate() + timedelta(days=3)),
            time=at,
            **extra,
        )

    def make_appointment(self, slot, **overrides):
        fields = {
            "fullName": "Asha Mbwana",
            "birthDate": date(1990, 5, 1),
            "gender": "female",
            "email": "asha@example.com",
            "phone": "0700000000",
            "visitType": "new_patient",
            "reasonVisit": "lab_test",
            "symptoms": "",
            "slot": slot,
        }
        fields.update(overrides)
        return Appointment.objects.create(**fields)

    # --- blockers ----------------------------------------------------------

    def test_duplicate_slots_block_the_unique_constraint(self):
        day = timezone.localdate() + timedelta(days=5)
        first = self.make_slot(day=day, at=time(11, 30))
        second = AvailableSlot.objects.create(
            user=self.doctor, date=day, time=time(11, 30), is_booked=True
        )

        payload, findings = run_audit()
        duplicates = findings["duplicate_slots"]

        self.assertEqual(duplicates["severity"], "BLOCKER")
        self.assertEqual(duplicates["count"], 1)
        row = duplicates["rows"][0]
        self.assertEqual(row["copies"], 2)
        self.assertEqual(sorted(row["slot_ids"]), sorted([first.id, second.id]))
        # Which copy is spoken for is the whole reason this row is printed.
        self.assertEqual(row["booked_slot_ids"], [second.id])
        self.assertEqual(payload["blockers"], 1)

    def test_two_appointments_on_one_slot_block_the_onetoone(self):
        """`Appointment.slot` is still a ForeignKey, so this is creatable today
        (design §10 #6) and will break migration 2.9 on real data."""
        slot = self.make_slot()
        first = self.make_appointment(slot, fullName="First Patient")
        second = self.make_appointment(slot, fullName="Second Patient")

        _, findings = run_audit()
        oversubscribed = findings["multi_appointment_slots"]

        self.assertEqual(oversubscribed["severity"], "BLOCKER")
        self.assertEqual(oversubscribed["count"], 1)
        booked = oversubscribed["rows"][0]["appointments"]
        self.assertEqual(
            [entry["bookingID"] for entry in booked],
            [first.bookingID, second.bookingID],
        )
        # Names both patients: someone has to be told their booking moved.
        self.assertIn("Second Patient", [entry["patient"] for entry in booked])

    def test_a_single_appointment_per_slot_is_not_a_blocker(self):
        self.make_appointment(self.make_slot())

        payload, findings = run_audit()

        self.assertEqual(findings["multi_appointment_slots"]["severity"], "INFO")
        self.assertEqual(payload["blockers"], 0)

    # --- values needing a person -------------------------------------------

    def test_free_text_values_are_mapped_where_obvious_and_flagged_where_not(self):
        slot_one, slot_two = self.make_slot(at=time(9, 0)), self.make_slot(at=time(10, 0))
        self.make_appointment(slot_one, visitType="Follow-Up", gender="Male")
        self.make_appointment(slot_two, reasonVisit="home collection", gender="female")

        _, findings = run_audit()
        free_text = findings["free_text_choices"]
        by_value = {row["value"]: row for row in free_text["rows"]}

        self.assertEqual(free_text["severity"], "REVIEW")
        # Case and punctuation drift is safe to map automatically.
        self.assertEqual(by_value["Follow-Up"]["maps_to"], "follow_up")
        self.assertEqual(by_value["Male"]["maps_to"], "male")
        # "home collection" is a *mode* now, not a reason (design §5.1) — the
        # migration must not quietly file it under one of the reasons.
        self.assertIsNone(by_value["home collection"]["maps_to"])
        # Values already valid are not reported as work.
        self.assertNotIn("lab_test", by_value)
        self.assertNotIn("female", by_value)

    def test_valid_choice_values_only_reports_info(self):
        self.make_appointment(self.make_slot())

        _, findings = run_audit()

        self.assertEqual(findings["free_text_choices"]["severity"], "INFO")

    def test_expertise_values_are_reported_never_guessed(self):
        Profile.objects.create(user=self.doctor, expertise="Cardiograph")

        _, findings = run_audit()
        expertise = findings["expertise_values"]

        self.assertEqual(expertise["severity"], "REVIEW")
        self.assertEqual(expertise["rows"], [{"expertise": "Cardiograph", "profiles": 1}])
        # No `maps_to` key anywhere: this check reports, it does not propose.
        self.assertNotIn("maps_to", expertise["rows"][0])

    # --- scope sizing -------------------------------------------------------

    def test_doctors_without_a_profile_are_flagged_for_review(self):
        _, findings = run_audit()
        missing = findings["missing_profiles"]

        self.assertEqual(missing["severity"], "REVIEW")
        self.assertIn("dr-audit", [row["username"] for row in missing["rows"]])

    def test_a_missing_profile_on_a_non_doctor_is_only_informational(self):
        Profile.objects.create(user=self.doctor)
        make_staff_user(username="editor-audit", email="editor-audit@example.com")

        _, findings = run_audit()

        self.assertEqual(findings["missing_profiles"]["severity"], "INFO")

    def test_active_unapproved_accounts_are_named_as_a_lockout_risk(self):
        locked_out = make_staff_user(username="pending", email="pending@example.com")
        CustomUser.objects.filter(pk=locked_out.pk).update(is_approved=False)

        _, findings = run_audit()
        risk = findings["login_lockout_risk"]

        self.assertEqual(risk["severity"], "REVIEW")
        self.assertEqual([row["username"] for row in risk["rows"]], ["pending"])

    def test_stale_is_booked_flags_are_reported(self):
        stale = self.make_slot(at=time(14, 0), is_booked=True)  # no appointment
        unflagged = self.make_slot(at=time(15, 0))
        self.make_appointment(unflagged)
        AvailableSlot.objects.filter(pk=unflagged.pk).update(is_booked=False)

        _, findings = run_audit()
        flags = findings["slot_booking_flags"]

        self.assertEqual(flags["severity"], "REVIEW")
        problems = {row["slot_id"]: row["problem"] for row in flags["rows"]}
        self.assertEqual(problems[stale.id], "is_booked=True with no appointment")
        self.assertEqual(
            problems[unflagged.id], "has an appointment but is_booked=False"
        )

    def test_approval_backfill_preview_matches_the_2_6_rules(self):
        confirmed = self.make_appointment(self.make_slot(at=time(8, 0)))
        pending = self.make_appointment(self.make_slot(at=time(8, 30)))
        self.make_appointment(self.make_slot(at=time(9, 30)))  # no approval row
        AppointmentApproval.objects.create(
            appointment=confirmed, is_approved=True, approved_by=self.doctor
        )
        AppointmentApproval.objects.create(
            appointment=pending, is_approved=False, approved_by=self.doctor
        )

        _, findings = run_audit()
        outcomes = findings["approval_backfill"]["rows"]

        self.assertEqual(findings["approval_backfill"]["severity"], "INFO")
        self.assertEqual(outcomes[0], {
            "outcome": "CONFIRMED", "appointments": 1,
            "from": "AppointmentApproval.is_approved=True",
        })
        self.assertEqual(outcomes[1]["appointments"], 1)
        self.assertEqual(outcomes[2]["appointments"], 1)  # missing approval row

    # --- the gate -----------------------------------------------------------

    def test_strict_exits_nonzero_only_when_a_blocker_exists(self):
        out = StringIO()
        call_command("audit_appointment_data", "--strict", stdout=out)  # clean: no raise

        slot = self.make_slot()
        self.make_appointment(slot)
        self.make_appointment(slot)

        with self.assertRaises(SystemExit) as raised:
            call_command("audit_appointment_data", "--strict", stdout=StringIO())
        self.assertEqual(raised.exception.code, 1)

    def test_review_findings_alone_do_not_fail_strict(self):
        """A doctor with no Profile needs a decision, not a migration halt."""
        Profile.objects.create(user=self.doctor, expertise="Cardiograph")

        call_command("audit_appointment_data", "--strict", stdout=StringIO())

    def test_report_renders_without_a_unicode_error(self):
        """The report is read from a Windows console as often as a prod shell."""
        self.make_appointment(self.make_slot(), fullName="Aisha Müller")

        out = StringIO()
        call_command("audit_appointment_data", stdout=out)

        self.assertIn("Appointment data audit", out.getvalue())


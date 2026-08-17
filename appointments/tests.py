"""Tests for the appointments app.

Two groups so far:

* **Phase 0** — the pre-migration data audit (plan §0.3).
* **Phase 1** — patient-record access: the IDOR, the second verification
  field on the lookup form, and the rate limit that makes guessing at it
  slow (plan 1.6-1.8).

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
from unittest import mock

from django.core.cache import caches
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from appointments.management.commands.audit_appointment_data import (
    GENDERS,
    REASONS_FOR_VISIT,
    VISIT_TYPES,
    suggest,
)
from appointments.access import has_appointment_access
from appointments.models import Appointment, AppointmentApproval, AvailableSlot
from appointments.ratelimit import LOOKUP_LIMIT
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


# ---------------------------------------------------------------------------
# Phase 1 - patient record access (plan 1.6-1.8, design §8.1-8.2)
# ---------------------------------------------------------------------------

PATIENT_PII = ("Asha Mbwana", "asha@example.com", "0712345678", "night sweats")


class PatientRecordAccessTestCase(TestCase):
    """Shared fixture: one real appointment belonging to one doctor."""

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="idor-doctor", email="idor-doctor@example.com",
            role=CustomUser.Role.DOCTOR,
        )
        cls.slot = AvailableSlot.objects.create(
            user=cls.doctor,
            date=timezone.localdate() + timedelta(days=4),
            time=time(10, 0),
        )
        cls.appointment = Appointment.objects.create(
            fullName="Asha Mbwana",
            birthDate=date(1990, 5, 1),
            gender="female",
            email="asha@example.com",
            phone="0712345678",
            visitType="new_patient",
            reasonVisit="lab_test",
            symptoms="night sweats",
            slot=cls.slot,
        )

    def setUp(self):
        # The limiter is shared state by design, so it has to be reset between
        # tests or the order they run in would change their outcome.
        caches["ratelimit"].clear()

    def detail_url(self, appointment=None):
        appointment = appointment or self.appointment
        return reverse("appointment_detail", kwargs={"booking_id": appointment.bookingID})

    def lookup(self, **data):
        return self.client.post(reverse("booking_id_lookup"), data)


class AppointmentDetailIdorTests(PatientRecordAccessTestCase):
    """Before this, incrementing the integer in `/appointments/detail/<pk>/`
    returned a stranger's name, date of birth, phone, email and symptoms."""

    def test_a_visitor_with_no_marker_is_sent_to_the_lookup_form(self):
        response = self.client.get(self.detail_url())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("booking_id_lookup"))

    def test_no_pii_is_rendered_without_a_marker(self):
        """The redirect is the mechanism; this is the thing that matters."""
        response = self.client.get(self.detail_url(), follow=True)

        for value in PATIENT_PII:
            with self.subTest(field=value):
                self.assertNotContains(response, value)

    def test_the_sequential_integer_url_is_gone(self):
        """Enumeration was the attack. `<str:booking_id>` still *matches* "1",
        so the check that matters is that it yields nothing."""
        response = self.client.get("/appointments/detail/%s/" % self.appointment.pk)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("booking_id_lookup"))

    def test_a_marker_for_one_appointment_does_not_open_another(self):
        other_slot = AvailableSlot.objects.create(
            user=self.doctor, date=self.slot.date, time=time(11, 0),
        )
        other = Appointment.objects.create(
            fullName="Someone Else", birthDate=date(1985, 2, 2), gender="male",
            email="else@example.com", phone="0799999999", visitType="follow_up",
            reasonVisit="consultation", symptoms="", slot=other_slot,
        )
        self.lookup(bookingID=self.appointment.bookingID, verification="0712345678")

        self.assertEqual(self.client.get(self.detail_url()).status_code, 200)
        self.assertEqual(self.client.get(self.detail_url(other)).status_code, 302)

    def test_an_unknown_booking_id_is_answered_exactly_like_a_forbidden_one(self):
        """Otherwise the response itself confirms which booking IDs are real."""
        forbidden = self.client.get(self.detail_url())
        unknown = self.client.get("/appointments/detail/AGKI-2026-00000/")

        self.assertEqual(forbidden.status_code, unknown.status_code)
        self.assertEqual(forbidden["Location"], unknown["Location"])

    def test_an_expired_marker_stops_working(self):
        self.lookup(bookingID=self.appointment.bookingID, verification="0712345678")
        self.assertEqual(self.client.get(self.detail_url()).status_code, 200)

        with mock.patch("appointments.access.ACCESS_MAX_AGE", -1):
            self.assertEqual(self.client.get(self.detail_url()).status_code, 302)


class BookingIdLookupTests(PatientRecordAccessTestCase):
    """Plan 1.7. `AGKI-<year>-<5 digits>` is 100,000 possibilities a year, so
    the booking ID alone was never a credential."""

    def test_the_booking_id_alone_is_not_enough(self):
        response = self.lookup(bookingID=self.appointment.bookingID, verification="")

        self.assertEqual(response.status_code, 200)  # re-rendered, not redirected
        self.assertFalse(has_appointment_access(response.wsgi_request, self.appointment))

    def test_the_phone_number_verifies(self):
        response = self.lookup(
            bookingID=self.appointment.bookingID, verification="0712345678"
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.detail_url())
        self.assertContains(self.client.get(self.detail_url()), "Asha Mbwana")

    def test_a_phone_number_in_another_format_still_verifies(self):
        """Stored numbers are inconsistent; a patient typing their own number
        correctly should not be told their details are wrong."""
        for typed in ("+255 712 345 678", "0712 345 678", "712345678"):
            with self.subTest(typed=typed):
                client = self.client_class()
                response = client.post(
                    reverse("booking_id_lookup"),
                    {"bookingID": self.appointment.bookingID, "verification": typed},
                )
                self.assertEqual(response.status_code, 302, typed)

    def test_the_date_of_birth_verifies_in_several_formats(self):
        for typed in ("1990-05-01", "01/05/1990", "01-05-1990"):
            with self.subTest(typed=typed):
                client = self.client_class()
                response = client.post(
                    reverse("booking_id_lookup"),
                    {"bookingID": self.appointment.bookingID, "verification": typed},
                )
                self.assertEqual(response.status_code, 302, typed)

    def test_a_wrong_verification_value_is_refused(self):
        response = self.lookup(
            bookingID=self.appointment.bookingID, verification="0700000000"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(self.detail_url()).status_code, 302)

    def test_the_error_never_says_which_field_was_wrong(self):
        """A different message for "no such booking" and "wrong phone number"
        would turn this form into a booking-ID oracle."""
        wrong_verification = self.lookup(
            bookingID=self.appointment.bookingID, verification="0700000000"
        )
        unknown_booking = self.lookup(
            bookingID="AGKI-2026-00000", verification="0712345678"
        )

        self.assertContains(wrong_verification, "find an appointment matching")
        self.assertContains(unknown_booking, "find an appointment matching")

    def test_the_form_is_a_plain_post_with_no_javascript_gate(self):
        """The old page could not work at all: its submit handler called
        preventDefault() unconditionally, and it rewrote what the patient
        typed into MediBook's `MB-` format."""
        page = self.client.get(reverse("booking_id_lookup")).content.decode()

        self.assertNotIn("preventDefault", page)
        self.assertNotIn("MB-", page)
        self.assertIn('name="verification"', page)


class BookingLookupRateLimitTests(PatientRecordAccessTestCase):
    """Plan 1.8 / design §8.2. A second field only helps if it cannot be
    guessed at thousands of times."""

    def test_attempts_are_capped_per_ip(self):
        for attempt in range(LOOKUP_LIMIT):
            response = self.lookup(bookingID="AGKI-2026-00000", verification="x")
            self.assertEqual(response.status_code, 200, "attempt %s" % (attempt + 1))

        blocked = self.lookup(bookingID="AGKI-2026-00000", verification="x")

        self.assertEqual(blocked.status_code, 429)
        self.assertContains(blocked, "Too many attempts", status_code=429)

    def test_a_throttled_attacker_cannot_get_through_with_correct_details(self):
        """The limit applies to the request, not to whether the guess was
        right - otherwise it would only slow down people who are already
        failing."""
        for _ in range(LOOKUP_LIMIT):
            self.lookup(bookingID="AGKI-2026-00000", verification="x")

        response = self.lookup(
            bookingID=self.appointment.bookingID, verification="0712345678"
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.client.get(self.detail_url()).status_code, 302)

    def test_the_limit_is_not_shared_between_different_addresses(self):
        for _ in range(LOOKUP_LIMIT + 1):
            self.lookup(bookingID="AGKI-2026-00000", verification="x")

        response = self.client.post(
            reverse("booking_id_lookup"),
            {"bookingID": self.appointment.bookingID, "verification": "0712345678"},
            REMOTE_ADDR="203.0.113.9",
        )

        self.assertEqual(response.status_code, 302)

    def test_a_forwarded_for_header_cannot_reset_the_counter(self):
        """`X-Forwarded-For` is attacker-controlled here - no trusted proxy is
        configured - so honouring it would let anyone rotate their own limit
        away by inventing a header."""
        for _ in range(LOOKUP_LIMIT + 1):
            self.lookup(bookingID="AGKI-2026-00000", verification="x")

        response = self.client.post(
            reverse("booking_id_lookup"),
            {"bookingID": self.appointment.bookingID, "verification": "0712345678"},
            HTTP_X_FORWARDED_FOR="198.51.100.7",
        )

        self.assertEqual(response.status_code, 429)


class BookingSuccessPageTests(PatientRecordAccessTestCase):
    """The success page used to key off the same sequential integer, so it
    handed out access rather than checking for it."""

    def booking_payload(self):
        return {
            "fullName": "New Patient",
            "birthDate": "1992-03-04",
            "gender": "female",
            "email": "new@example.com",
            "phone": "0755000111",
            "visitType": "new_patient",
            "reasonVisit": "lab_test",
            # Required by the form today: `symptoms` is a bare TextField, so
            # the UI's "optional" styling is a lie the model does not back.
            # Plan 2.4 makes it `blank=True`; until then a booking without it
            # is simply rejected, which is what this fixture reflects.
            "symptoms": "persistent cough",
        }

    def test_booking_grants_access_to_the_patient_who_booked(self):
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=self.slot.date, time=time(13, 0),
        )

        response = self.client.post(
            reverse("booking_form", kwargs={"id": slot.pk}), self.booking_payload()
        )
        created = Appointment.objects.get(slot=slot)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("booking_success_page", kwargs={"booking_id": created.bookingID}),
        )
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)

    def test_a_stranger_cannot_open_someone_elses_success_page(self):
        url = reverse(
            "booking_success_page", kwargs={"booking_id": self.appointment.bookingID}
        )

        response = self.client.get(url, follow=True)

        self.assertNotContains(response, "Asha Mbwana")

    def test_the_booking_page_works_for_a_doctor_with_no_profile(self):
        """Profiles are created lazily, so three doctors in the live database
        have none. That used to 404 the booking page (design §5.3)."""
        self.assertFalse(Profile.objects.filter(user=self.doctor).exists())
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=self.slot.date, time=time(14, 0),
        )

        response = self.client.get(reverse("booking_form", kwargs={"id": slot.pk}))

        self.assertEqual(response.status_code, 200)

    def test_the_detail_page_works_for_a_doctor_with_no_profile(self):
        self.lookup(bookingID=self.appointment.bookingID, verification="0712345678")

        self.assertEqual(self.client.get(self.detail_url()).status_code, 200)


class PublicPageImageGuardTests(PatientRecordAccessTestCase):
    """Plan 1.1 on the public side: a doctor with no photo used to crash the
    pages patients see."""

    def test_the_doctor_list_renders_for_a_doctor_with_no_photo(self):
        Profile.objects.create(user=self.doctor, expertise="Cardiology")
        self.assertFalse(self.doctor.image)

        response = self.client.get(reverse("search_results"))

        self.assertEqual(response.status_code, 200)

    def test_the_detail_page_renders_for_a_doctor_with_no_photo(self):
        self.lookup(bookingID=self.appointment.bookingID, verification="0712345678")

        response = self.client.get(self.detail_url())

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<img src=""')

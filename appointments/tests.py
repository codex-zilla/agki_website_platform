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
import re
from datetime import date, time, timedelta
from importlib import import_module
from io import StringIO
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth.models import Group, Permission
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from appointments.management.commands.audit_appointment_data import (
    GENDERS,
    REASONS_FOR_VISIT,
    VISIT_TYPES,
    Command,
    Finding,
    suggest,
)
from appointments.access import has_appointment_access
from appointments.models import (
    Appointment,
    AppointmentApproval,
    AppointmentSettings,
    AppointmentTransition,
    AvailableSlot,
    Rating,
)
from appointments.ratelimit import LOOKUP_LIMIT
from cms.test_utils import make_staff_user
from useraccount.models import CustomUser, Profile, Specialty

# Migration module names start with a digit, so they cannot be imported with
# `from ... import ...`. The data migrations' decision-making is worth testing
# directly — see `BackfillLogicTests` for why testing it through `migrate`
# is not an option once the constraints are in place.
_m0018 = import_module("appointments.migrations.0018_backfill_status_from_approvals")
_m0021 = import_module("appointments.migrations.0021_choices_drop_age_reshape_rating")
backfill_status = _m0018.backfill_status
normalise_choice_fields = _m0021.normalise_choice_fields
check_rating_is_empty = _m0021.check_rating_is_empty


def set_expertise(user, value):
    """Set `expertise` on an account's profile.

    A helper rather than `Profile.objects.create(...)` because every account
    now gets a profile from a `post_save` signal (plan 2.13) — creating a
    second one raises on the OneToOne. Tests that want a doctor with *no*
    profile delete it explicitly, which is the honest way to say "this is the
    pre-backfill state production is still in".
    """
    Profile.objects.filter(user=user).update(expertise=value)


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

    # --- the constraints these blockers were about -------------------------
    #
    # The three blocking checks (`duplicate_slots`, `multi_appointment_slots`,
    # `orphan_ratings`) can no longer be reproduced here, because Phase 2
    # applied the constraints they were warning about — the database now
    # refuses to hold the state the audit was looking for. They are kept in
    # the command for the one production run that happens *before* those
    # migrations; the proof that they detect the state is in the Phase 0
    # commit, where the suite was mutation-checked against real duplicate rows.
    #
    # What replaces them is stronger: tests that the constraints themselves
    # hold, which is what the audit was protecting in the first place.

    def test_a_slot_cannot_be_published_twice(self):
        day = timezone.localdate() + timedelta(days=5)
        self.make_slot(day=day, at=time(11, 30))

        with self.assertRaises(IntegrityError):
            AvailableSlot.objects.create(
                user=self.doctor, date=day, time=time(11, 30)
            )

    def test_the_same_doctor_can_publish_the_same_time_on_another_day(self):
        """The constraint is per doctor/date/time, not per time."""
        self.make_slot(day=timezone.localdate() + timedelta(days=6), at=time(11, 30))
        self.make_slot(day=timezone.localdate() + timedelta(days=7), at=time(11, 30))

    def test_a_slot_cannot_hold_two_appointments(self):
        """Double-booking was never a race: `booking_form` never read
        `is_booked`, so two sequential bookings both succeeded (design §10 #6).
        Now the database refuses the second one outright."""
        slot = self.make_slot()
        self.make_appointment(slot, fullName="First Patient")

        with self.assertRaises(IntegrityError):
            self.make_appointment(slot, fullName="Second Patient")

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
        set_expertise(self.doctor, "Cardiograph")

        _, findings = run_audit()
        expertise = findings["expertise_values"]

        self.assertEqual(expertise["severity"], "REVIEW")
        self.assertEqual(expertise["rows"], [{"expertise": "Cardiograph", "profiles": 1}])
        # No `maps_to` key anywhere: this check reports, it does not propose.
        self.assertNotIn("maps_to", expertise["rows"][0])

    # --- scope sizing -------------------------------------------------------

    def test_doctors_without_a_profile_are_flagged_for_review(self):
        """The signal now gives every new account a profile (plan 2.13), so
        this has to be engineered — which is the improvement. The check stays
        because production has accounts that predate the signal until the
        backfill migration runs there."""
        Profile.objects.filter(user=self.doctor).delete()

        _, findings = run_audit()
        missing = findings["missing_profiles"]

        self.assertEqual(missing["severity"], "REVIEW")
        self.assertIn("dr-audit", [row["username"] for row in missing["rows"]])

    def test_a_missing_profile_on_a_non_doctor_is_only_informational(self):
        editor = make_staff_user(username="editor-audit", email="editor-audit@example.com")
        Profile.objects.filter(user=editor).delete()

        _, findings = run_audit()

        self.assertEqual(findings["missing_profiles"]["severity"], "INFO")

    def test_a_new_account_gets_a_profile_without_visiting_a_page(self):
        """Design §5.3: doctor visibility must follow from `role`, never from
        whether someone once opened their profile page. Three real doctors
        were invisible to the booking site for exactly that reason."""
        newcomer = make_staff_user(
            username="fresh-doctor", email="fresh-doctor@example.com",
            role=CustomUser.Role.DOCTOR,
        )

        self.assertTrue(Profile.objects.filter(user=newcomer).exists())

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
        """No blocker is reachable in a migrated database — that is what the
        constraints are for — so the finding is injected. What is under test
        is the gate: `--strict` must turn a BLOCKER into a non-zero exit, so a
        deploy script can stop on it."""
        call_command("audit_appointment_data", "--strict", stdout=StringIO())  # clean

        blocker = Finding("injected", "BLOCKER", "a blocker, for the gate's sake")
        with mock.patch.object(Command, "check_duplicate_slots", return_value=blocker):
            with self.assertRaises(SystemExit) as raised:
                call_command("audit_appointment_data", "--strict", stdout=StringIO())

        self.assertEqual(raised.exception.code, 1)

    def test_review_findings_alone_do_not_fail_strict(self):
        """An `expertise` value nobody has mapped yet needs a decision before
        step 2.3, not a halt on every migration in the project."""
        set_expertise(self.doctor, "Cardiograph")

        _, findings = run_audit()
        self.assertEqual(findings["expertise_values"]["severity"], "REVIEW")

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
        """Used to 404 the booking page (design §5.3). Plan 2.13's signal means
        this state is no longer reachable by accident, but production still
        holds accounts that predate the backfill, so the guard stays and is
        still tested — with the profile removed deliberately."""
        Profile.objects.filter(user=self.doctor).delete()
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
        set_expertise(self.doctor, "Cardiology")
        self.assertFalse(self.doctor.image)

        response = self.client.get(reverse("search_results"))

        self.assertEqual(response.status_code, 200)

    def test_the_detail_page_renders_for_a_doctor_with_no_photo(self):
        self.lookup(bookingID=self.appointment.bookingID, verification="0712345678")

        response = self.client.get(self.detail_url())

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<img src=""')


# ---------------------------------------------------------------------------
# Phase 2 - data model & migrations
# ---------------------------------------------------------------------------


class AppointmentSettingsTests(TestCase):
    """Plan 2.1 / design §6.1. One row, superuser-only, with one invariant
    that matters more than the values themselves."""

    def test_load_returns_the_same_row_every_time(self):
        self.assertEqual(AppointmentSettings.load().pk, AppointmentSettings.load().pk)

    def test_the_defaults_are_agkis_agreed_values(self):
        settings_row = AppointmentSettings.load()

        self.assertEqual(settings_row.minimum_booking_notice_minutes, 120)
        self.assertEqual(settings_row.confirmation_cutoff_minutes, 30)

    def test_notice_shorter_than_the_cutoff_is_rejected(self):
        """The combination that would make every booking expire on creation:
        the site would accept a booking and immediately kill it, which reads
        as "the site is broken" rather than "the setting is wrong"."""
        settings_row = AppointmentSettings.load()
        settings_row.minimum_booking_notice_minutes = 15
        settings_row.confirmation_cutoff_minutes = 30

        with self.assertRaises(ValidationError) as raised:
            settings_row.full_clean()

        self.assertIn("minimum_booking_notice_minutes", raised.exception.message_dict)

    def test_equal_values_are_rejected_too(self):
        """Equal leaves a zero-length window to answer in, which is the same
        failure with a nicer-looking pair of numbers."""
        settings_row = AppointmentSettings.load()
        settings_row.minimum_booking_notice_minutes = 30
        settings_row.confirmation_cutoff_minutes = 30

        with self.assertRaises(ValidationError):
            settings_row.full_clean()

    def test_the_invariant_cannot_be_bypassed_by_saving_from_code(self):
        """A validator only the admin form runs is a validator that a
        management command, a shell session or a fixture walks straight past."""
        settings_row = AppointmentSettings.load()
        settings_row.minimum_booking_notice_minutes = 10

        with self.assertRaises(ValidationError):
            settings_row.save()

    def test_the_windows_are_exposed_as_durations(self):
        settings_row = AppointmentSettings.load()

        self.assertEqual(settings_row.minimum_booking_notice, timedelta(hours=2))
        self.assertEqual(settings_row.confirmation_cutoff, timedelta(minutes=30))


class AppointmentStateFieldTests(TestCase):
    """The state machine's data, not its transitions — `transition_to()` is
    plan 4.1. What matters here is that the columns can hold the flow."""

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="state-doctor", email="state-doctor@example.com",
            role=CustomUser.Role.DOCTOR,
        )

    def make_appointment(self, **overrides):
        slot = AvailableSlot.objects.create(
            user=self.doctor,
            date=timezone.localdate() + timedelta(days=3),
            time=overrides.pop("at", time(9, 0)),
        )
        fields = {
            "fullName": "Asha Mbwana", "birthDate": date(1990, 5, 1),
            "gender": Appointment.Gender.FEMALE, "email": "asha@example.com",
            "phone": "0712345678", "visitType": Appointment.VisitType.NEW_PATIENT,
            "reasonVisit": Appointment.ReasonForVisit.LAB_TEST, "slot": slot,
        }
        fields.update(overrides)
        return Appointment.objects.create(**fields)

    def test_a_new_booking_waits_on_the_doctor(self):
        self.assertEqual(self.make_appointment().status, Appointment.Status.PENDING_TIME)

    def test_the_mode_is_blank_until_round_two(self):
        """It is not known at booking: the patient chooses it after the doctor
        confirms the time (design §0.1)."""
        self.assertEqual(self.make_appointment().appointmentMode, "")

    def test_symptoms_are_optional(self):
        """The model required them while the form never marked them required."""
        self.make_appointment().full_clean()

    def test_an_invalid_visit_type_fails_validation(self):
        appointment = self.make_appointment(visitType="whatever")

        with self.assertRaises(ValidationError) as raised:
            appointment.full_clean()

        self.assertIn("visitType", raised.exception.message_dict)

    def test_home_collection_is_not_a_reason_for_visit(self):
        """It is a *mode* now — how the appointment happens, not why it was
        booked (design §5.1). Someone can book a lab test and agree the
        provider travels to them; those are independent answers."""
        self.assertNotIn(
            "home_collection", [value for value, _ in Appointment.ReasonForVisit.choices]
        )
        self.assertIn(
            "provider_travels", [value for value, _ in Appointment.Mode.choices]
        )

    def test_the_age_column_is_gone(self):
        self.assertNotIn("age", [field.name for field in Appointment._meta.get_fields()])

    def test_deleting_a_slot_with_an_appointment_is_refused(self):
        """CASCADE meant deleting an availability slot silently deleted the
        patient's appointment and their only record of it."""
        appointment = self.make_appointment()

        with self.assertRaises(ProtectedError):
            appointment.slot.delete()

    def test_a_transition_records_who_moved_it_and_when(self):
        appointment = self.make_appointment()

        AppointmentTransition.objects.create(
            appointment=appointment,
            from_status=Appointment.Status.PENDING_TIME,
            to_status=Appointment.Status.PENDING_MODE,
            actor=self.doctor,
        )
        system = AppointmentTransition.objects.create(
            appointment=appointment,
            from_status=Appointment.Status.PENDING_MODE,
            to_status=Appointment.Status.EXPIRED,
            note="Patient never chose a mode.",
        )

        self.assertEqual(appointment.transitions.count(), 2)
        # actor=None is the system or the patient - nobody with an account.
        self.assertIsNone(system.actor)
        self.assertEqual(
            [t.to_status for t in appointment.transitions.all()],
            ["pending_mode", "expired"],
        )

    def test_one_rating_per_appointment(self):
        appointment = self.make_appointment()
        Rating.objects.create(appointment=appointment, provider=self.doctor, rating_value=5)

        with self.assertRaises(IntegrityError):
            Rating.objects.create(
                appointment=appointment, provider=self.doctor, rating_value=1
            )


class SpecialtyTests(TestCase):
    """Plan 2.2 / design §5.5 — the taxonomy that makes doctor search real."""

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="spec-doctor", email="spec-doctor@example.com",
            role=CustomUser.Role.DOCTOR,
        )

    def test_a_doctor_can_hold_more_than_one_specialty(self):
        """Forcing a single value would make someone unfindable under half
        their practice."""
        cardiology = Specialty.objects.create(name="Cardiology", slug="cardiology")
        pathology = Specialty.objects.create(name="Pathology", slug="pathology")
        profile = Profile.objects.get(user=self.doctor)

        profile.specialties.add(cardiology, pathology)

        self.assertEqual(profile.specialties.count(), 2)
        self.assertIn(profile, cardiology.doctors.all())

    def test_specialty_names_are_unique(self):
        Specialty.objects.create(name="Cardiology", slug="cardiology")

        with self.assertRaises(IntegrityError):
            Specialty.objects.create(name="Cardiology", slug="cardiology-2")

    def test_the_taxonomy_ships_empty(self):
        """Seeding it from a migration would be putting words in AGKI's mouth;
        the real specialties are entered at rollout (step 9.6)."""
        self.assertEqual(Specialty.objects.count(), 0)


class ExpertiseMappingCommandTests(TestCase):
    """Plan 2.3. The command maps what is unambiguous and refuses the rest —
    `expertise='Cardiograph'` is an ECG machine, and only a person can say it
    was meant to read Cardiology."""

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="map-doctor", email="map-doctor@example.com",
            role=CustomUser.Role.DOCTOR,
        )

    def run_command(self, *args):
        out = StringIO()
        call_command("map_expertise_to_specialties", *args, stdout=out)
        return out.getvalue()

    def test_it_does_nothing_before_the_taxonomy_exists(self):
        set_expertise(self.doctor, "Cardiology")

        output = self.run_command("--apply")

        self.assertIn("No Specialty rows exist yet", output)
        self.assertEqual(Profile.objects.get(user=self.doctor).specialties.count(), 0)

    def test_an_exact_match_is_linked(self):
        Specialty.objects.create(name="Cardiology", slug="cardiology")
        set_expertise(self.doctor, "  cardiology ")

        self.run_command("--apply")

        self.assertEqual(
            list(Profile.objects.get(user=self.doctor).specialties.values_list("name", flat=True)),
            ["Cardiology"],
        )

    def test_reporting_is_the_default_and_writes_nothing(self):
        Specialty.objects.create(name="Cardiology", slug="cardiology")
        set_expertise(self.doctor, "Cardiology")

        output = self.run_command()

        self.assertIn("would link", output)
        self.assertEqual(Profile.objects.get(user=self.doctor).specialties.count(), 0)

    def test_a_near_miss_is_reported_and_left_alone(self):
        Specialty.objects.create(name="Cardiology", slug="cardiology")
        set_expertise(self.doctor, "Cardiograph")

        output = self.run_command("--apply")

        self.assertIn("NEEDS A DECISION", output)
        self.assertIn("Cardiograph", output)
        self.assertEqual(Profile.objects.get(user=self.doctor).specialties.count(), 0)

    def test_it_is_safe_to_run_twice(self):
        Specialty.objects.create(name="Cardiology", slug="cardiology")
        set_expertise(self.doctor, "Cardiology")

        self.run_command("--apply")
        output = self.run_command("--apply")

        self.assertIn("already linked", output)
        self.assertEqual(Profile.objects.get(user=self.doctor).specialties.count(), 1)


class BackfillLogicTests(TestCase):
    """The data migrations' logic, exercised directly.

    These call the migration functions with the live app registry rather than
    running the migration itself. That tests what the functions decide, not
    Django's ability to apply an operation — and it is the only way to reach
    them at all now that the constraints they precede are in place.

    `dedupe_slots` (0019) is the exception and is deliberately untested here:
    it needs duplicate slots to exist, and the uniqueness constraint it
    installs makes them unconstructable. Its proof is in the Phase 0 commit,
    where the audit that finds the same rows was mutation-checked.
    """

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="backfill-doctor", email="backfill-doctor@example.com",
            role=CustomUser.Role.DOCTOR,
        )

    def make_appointment(self, at=time(9, 0), **overrides):
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=timezone.localdate() + timedelta(days=3), time=at
        )
        fields = {
            "fullName": "Asha Mbwana", "birthDate": date(1990, 5, 1),
            "gender": "female", "email": "asha@example.com", "phone": "0712345678",
            "visitType": "new_patient", "reasonVisit": "lab_test", "slot": slot,
        }
        fields.update(overrides)
        return Appointment.objects.create(**fields)

    def test_an_approved_appointment_becomes_confirmed_with_its_history(self):
        appointment = self.make_appointment()
        approved_at = timezone.now() - timedelta(days=9)
        AppointmentApproval.objects.create(
            appointment=appointment, is_approved=True,
            approved_by=self.doctor, approved_at=approved_at,
        )

        backfill_status(django_apps, None)

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)
        self.assertEqual(appointment.time_confirmed_at, approved_at)

        transition = appointment.transitions.get()
        self.assertEqual(transition.to_status, "confirmed")
        self.assertEqual(transition.actor, self.doctor)
        # The seeded row keeps the real approval time. An audit trail claiming
        # every historic approval happened during a deploy is worse than one
        # that admits it doesn't know.
        self.assertEqual(transition.created_at, approved_at)

    def test_an_unapproved_appointment_stays_pending_and_keeps_a_record(self):
        appointment = self.make_appointment(at=time(10, 0))
        AppointmentApproval.objects.create(
            appointment=appointment, is_approved=False, approved_by=self.doctor
        )

        backfill_status(django_apps, None)

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)
        self.assertEqual(appointment.transitions.get().to_status, "pending_time")

    def test_a_confirmed_appointment_gets_no_invented_mode(self):
        """The backfill is lossy in one direction and that is correct: the old
        model never held a mode, and "we don't know" is a true statement about
        pre-existing data."""
        appointment = self.make_appointment(at=time(11, 0))
        AppointmentApproval.objects.create(
            appointment=appointment, is_approved=True, approved_by=self.doctor
        )

        backfill_status(django_apps, None)

        appointment.refresh_from_db()
        self.assertEqual(appointment.appointmentMode, "")

    def test_an_appointment_with_no_approval_row_is_left_pending(self):
        appointment = self.make_appointment(at=time(12, 0))

        backfill_status(django_apps, None)

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)
        self.assertEqual(appointment.transitions.count(), 0)

    def test_case_and_punctuation_drift_is_normalised(self):
        appointment = self.make_appointment(at=time(13, 0))
        # .update() bypasses validation, which is how these values got into
        # the database in the first place.
        Appointment.objects.filter(pk=appointment.pk).update(
            visitType="Follow-Up", gender="Male"
        )

        normalise_choice_fields(django_apps, None)

        appointment.refresh_from_db()
        self.assertEqual(appointment.visitType, "follow_up")
        self.assertEqual(appointment.gender, "male")

    def test_a_value_with_no_obvious_meaning_stops_the_migration(self):
        """`role='4491'` is why: a migration that picks a plausible choice for
        a value nobody understands reproduces that bug with better
        formatting."""
        appointment = self.make_appointment(at=time(14, 0))
        Appointment.objects.filter(pk=appointment.pk).update(reasonVisit="home collection")

        with self.assertRaises(RuntimeError) as raised:
            normalise_choice_fields(django_apps, None)

        self.assertIn("home collection", str(raised.exception))
        self.assertIn("role='4491'", str(raised.exception))

    def test_a_rating_with_no_appointment_stops_the_reshape(self):
        """The design said nothing ever creates a Rating. The admin did, and
        two orphan rows were sitting in the database when this migration came
        to give the model a mandatory link to a visit."""
        appointment = self.make_appointment(at=time(15, 0))
        Rating.objects.create(appointment=appointment, provider=self.doctor, rating_value=4)

        with self.assertRaises(RuntimeError) as raised:
            check_rating_is_empty(django_apps, None)

        self.assertIn("belongs to exactly", str(raised.exception))


class ConfigurationIsSuperuserOnlyTests(TestCase):
    """Design §4.3, binding: the CMS's Content Editors group must never gain
    control over booking or clinical data. Asserted rather than remembered —
    a future `makemigrations` or a well-meaning "grant all" would otherwise
    undo it silently."""

    PROTECTED_APPS = ("appointments", "doctorDashboard", "ecommerce")

    @classmethod
    def setUpTestData(cls):
        cls.editor = make_staff_user(
            username="cfg-editor", email="cfg-editor@example.com", is_staff=True
        )
        cls.editor.groups.add(Group.objects.get(name="Content Editors"))
        cls.superuser = CustomUser.objects.create_superuser(
            username="cfg-root", email="cfg-root@example.com", password="test-pass-12345"
        )

    def test_content_editors_hold_no_permissions_on_the_protected_apps(self):
        held = (
            Group.objects.get(name="Content Editors")
            .permissions.filter(content_type__app_label__in=self.PROTECTED_APPS)
            .values_list("content_type__app_label", "codename")
        )

        self.assertEqual(list(held), [])

    def test_content_editors_hold_no_permissions_on_the_specialty_taxonomy(self):
        """It lives in `useraccount`, so an app-label check would miss it, and
        it configures how patients find doctors — configuration, not identity."""
        held = (
            Group.objects.get(name="Content Editors")
            .permissions.filter(codename__endswith="specialty")
            .values_list("codename", flat=True)
        )

        self.assertEqual(list(held), [])

    def test_an_editor_cannot_open_appointment_settings(self):
        self.client.force_login(self.editor)

        response = self.client.get("/admin/appointments/appointmentsettings/")

        self.assertIn(response.status_code, (302, 403))

    def test_an_editor_cannot_open_the_specialty_admin(self):
        self.client.force_login(self.editor)

        response = self.client.get("/admin/useraccount/specialty/")

        self.assertIn(response.status_code, (302, 403))

    def test_a_superuser_can(self):
        """The other half of the assertion: this is a boundary, not a wall."""
        self.client.force_login(self.superuser)

        self.assertEqual(self.client.get("/admin/useraccount/specialty/").status_code, 200)

    def test_the_gate_holds_even_when_the_permission_is_granted(self):
        """The two tests above pass on an ordinary ModelAdmin, because the
        group simply has no permission — which is what a future "grant all"
        would change. This is the one that tests the gate itself: with the
        permission explicitly granted, a non-superuser still cannot get in.

        Found by mutation-checking: removing `SuperuserOnlyAdmin` from the
        specialty admin broke nothing until this existed.
        """
        for codename in ("view_specialty", "change_specialty"):
            self.editor.user_permissions.add(
                Permission.objects.get(codename=codename)
            )
        self.editor.user_permissions.add(
            Permission.objects.get(codename="change_appointmentsettings")
        )
        editor = CustomUser.objects.get(pk=self.editor.pk)  # drop the perm cache
        self.client.force_login(editor)

        self.assertTrue(editor.has_perm("useraccount.change_specialty"))
        self.assertIn(self.client.get("/admin/useraccount/specialty/").status_code, (302, 403))
        self.assertIn(
            self.client.get("/admin/appointments/appointmentsettings/").status_code,
            (302, 403),
        )

    def test_the_audit_trail_cannot_be_edited(self):
        """"Who confirmed this and when" is worth nothing if anyone can revise
        the answer."""
        self.client.force_login(self.superuser)

        response = self.client.get("/admin/appointments/appointmenttransition/add/")

        self.assertIn(response.status_code, (302, 403))


class BookingFormOptionsMatchTheModelTests(TestCase):
    """Plan 2.10, the half that is easy to forget: closing the choice list on
    the model is only half a fix if the form the public uses still offers
    something else.

    It did. This form posted visitType="first" and reasonVisit="routine",
    "symptoms", "medication" - MediBook's list, hand-typed into the template
    and never reconciled with the model. Every one of those would have been
    rejected the moment the choices landed, which is to say booking would have
    broken on the day this phase shipped.
    """

    LEGACY_VALUES = ("first", "followup", "routine", "symptoms", "medication", "test-results")

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="opt-doctor", email="opt-doctor@example.com",
            role=CustomUser.Role.DOCTOR,
        )
        cls.slot = AvailableSlot.objects.create(
            user=cls.doctor, date=timezone.localdate() + timedelta(days=3), time=time(9, 0)
        )

    def rendered(self):
        return self.client.get(
            reverse("booking_form", kwargs={"id": self.slot.pk})
        ).content.decode()

    def offered(self, html, field):
        if field == "visitType":
            return set(re.findall(r'name="visitType" value="([^"]+)"', html))
        block = re.search(rf'<select[^>]*name="{field}".*?</select>', html, re.S).group(0)
        return {value for value in re.findall(r'<option value="([^"]*)"', block) if value}

    def test_the_form_offers_exactly_the_values_the_model_accepts(self):
        html = self.rendered()

        for field, choices in (
            ("gender", Appointment.Gender),
            ("visitType", Appointment.VisitType),
            ("reasonVisit", Appointment.ReasonForVisit),
        ):
            with self.subTest(field=field):
                self.assertEqual(self.offered(html, field), set(choices.values))

    def test_no_medibook_value_survives(self):
        html = self.rendered()

        for legacy in self.LEGACY_VALUES:
            with self.subTest(value=legacy):
                self.assertNotIn(f'value="{legacy}"', html)

    def test_a_booking_made_with_the_offered_values_is_accepted(self):
        """The end of the chain: what the page offers, the server takes."""
        html = self.rendered()
        payload = {
            "fullName": "Asha Mbwana", "birthDate": "1990-05-01",
            "email": "asha@example.com", "phone": "0712345678",
            "symptoms": "",
            "gender": sorted(self.offered(html, "gender"))[0],
            "visitType": sorted(self.offered(html, "visitType"))[0],
            "reasonVisit": sorted(self.offered(html, "reasonVisit"))[0],
        }

        response = self.client.post(
            reverse("booking_form", kwargs={"id": self.slot.pk}), payload
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Appointment.objects.filter(slot=self.slot).exists())


class TemplateCommentHygieneTests(TestCase):
    """Django's one-line comment syntax is exactly that: one line.

    The template lexer's token regex is not DOTALL, so a comment spanning two
    lines is not recognised as a comment at all — it renders to the page as
    literal text, explanation and all. Multi-line notes need the block comment
    tag instead.

    This is here because it happened. Several explanatory notes added while
    closing the security holes were being served to visitors, including one
    describing the verification field on the booking-ID lookup form. A page
    that explains its own security reasoning to the public is not the worst
    outcome imaginable, but it is not the intended one — and nothing caught
    it, because a template comment that fails to be a comment still renders a
    valid page.

    Scans every app's templates, not just this one's: the mistake belongs to
    the syntax, not to any app.
    """

    def test_no_template_comment_spans_more_than_one_line(self):
        import re
        from pathlib import Path

        from django.conf import settings

        pattern = re.compile(r"\{#((?:(?!#\}).)*?)#\}", re.S)
        root = Path(settings.BASE_DIR)
        offenders = []

        for path in root.rglob("*.html"):
            if ".venv" in path.parts or "site-packages" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            for match in pattern.finditer(source):
                if "\n" in match.group(1):
                    line = source[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(root)}:{line}")

        self.assertEqual(
            offenders,
            [],
            "These one-line template comments span multiple lines and will "
            "render to the page as visible text. Use a block comment tag "
            "instead: " + ", ".join(offenders),
        )

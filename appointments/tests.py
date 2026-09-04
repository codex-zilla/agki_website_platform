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
from django.core import mail
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.template.defaultfilters import date as date_filter
from django.utils.html import escape, strip_tags

from appointments.management.commands.audit_appointment_data import (
    GENDERS,
    REASONS_FOR_VISIT,
    VISIT_TYPES,
    Command,
    Finding,
    suggest,
)
import inspect
from django.db import IntegrityError, transaction
from appointments import models as appointment_models
from appointments.access import has_appointment_access
from appointments.journey import journey_before_booking, journey_for
from appointments.notifications import deliver, delivery_mode, notify
from appointments.notifications import channels, events
from appointments.notifications import events as event_registry
from appointments.models import (
    Appointment,
    AppointmentApproval,
    AppointmentSettings,
    AppointmentTransition,
    AvailableSlot,
    InvalidTransition,
    NotificationLog,
    Rating,
    new_booking_reference,
)
from appointments.forms import (
    SLOT_TAKEN,
    AppointmentForm,
    AvailableSlotForm,
    BulkAvailabilityForm,
)
from appointments.ratelimit import LOOKUP_LIMIT
from appointments.views import DOCTOR_UNAVAILABLE, SlotUnavailable, claim_slot
from cms.models import SiteSettings
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
        typed into MediBook's `MB-` format.

        Asserted on submit handlers rather than on the word `preventDefault`,
        which the page now contains innocently: the shared chrome ships the
        themed dropdown's keyboard handler (plan 11.23), and that suppresses
        the default on an arrow key - on a page which has no dropdown at all.
        Searching for the word turned a real rule into a tripwire for
        unrelated code.
        """
        page = self.client.get(reverse("booking_id_lookup")).content.decode()
        script = chr(10).join(re.findall(r"<script>(.*?)</script>", page, re.S))

        self.assertNotIn("addEventListener('submit'", script)
        self.assertNotIn("onsubmit", page)
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
            # Required since plan 12.1: the patient says how they want to meet
            # when they book, so the doctor answers once.
            "appointmentMode": Appointment.Mode.PATIENT_TRAVELS,
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
            to_status=Appointment.Status.PENDING_TIME,
            actor=self.doctor,
        )
        system = AppointmentTransition.objects.create(
            appointment=appointment,
            from_status=Appointment.Status.PENDING_TIME,
            to_status=Appointment.Status.EXPIRED,
            note="Nobody confirmed it in time.",
        )

        self.assertEqual(appointment.transitions.count(), 2)
        # actor=None is the system or the patient - nobody with an account.
        self.assertIsNone(system.actor)
        self.assertEqual(
            [t.to_status for t in appointment.transitions.all()],
            ["pending_time", "expired"],
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
        cardiology = Specialty.objects.create(name="Test Cardiology", slug="test-cardiology")
        pathology = Specialty.objects.create(name="Test Pathology", slug="test-pathology")
        profile = Profile.objects.get(user=self.doctor)

        profile.specialties.add(cardiology, pathology)

        self.assertEqual(profile.specialties.count(), 2)
        self.assertIn(profile, cardiology.doctors.all())

    def test_specialty_names_are_unique(self):
        Specialty.objects.create(name="Test Cardiology", slug="test-cardiology")

        with self.assertRaises(IntegrityError):
            Specialty.objects.create(name="Test Cardiology", slug="test-cardiology-2")

    def test_the_taxonomy_ships_seeded_without_duplicating_what_existed(self):
        """Inverted by AGKI's request of 2026-09-01 (plan 11.6).

        This asserted the table shipped **empty**, because "seeding it from a
        migration would be putting words in AGKI's mouth". That was right while
        nobody had asked. AGKI asked - "add the most common qualifications and
        specialization" - so migration 0013 seeds a starting list.

        Inverted rather than deleted, because the half of the old guarantee
        that still matters is the half about **not trampling what is already
        there**: `Cardiology` was in the live table before the seed, put there
        by `map_expertise_to_specialties` when AGKI decided "Cardiograph" meant
        Cardiology (plan 2.3), and a second row would split that doctor's link
        across two entries reading identically in the admin.
        """
        self.assertGreater(Specialty.objects.count(), 0)
        self.assertEqual(Specialty.objects.filter(name="Cardiology").count(), 1)
        names = list(Specialty.objects.values_list("name", flat=True))
        self.assertEqual(len(names), len(set(names)), "the seed duplicated a name")


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
        # The table is seeded from migration 0013 since plan 11.6, so the state
        # this test is about - a taxonomy that does not exist yet - has to be
        # made rather than assumed. The guarantee is unchanged: with nothing to
        # map onto, the command says so instead of guessing.
        Specialty.objects.all().delete()
        set_expertise(self.doctor, "Cardiology")

        output = self.run_command("--apply")

        self.assertIn("No Specialty rows exist yet", output)
        self.assertEqual(Profile.objects.get(user=self.doctor).specialties.count(), 0)

    def test_an_exact_match_is_linked(self):
        Specialty.objects.create(name="Test Cardiology", slug="test-cardiology")
        set_expertise(self.doctor, "  cardiology ")

        self.run_command("--apply")

        self.assertEqual(
            list(Profile.objects.get(user=self.doctor).specialties.values_list("name", flat=True)),
            ["Cardiology"],
        )

    def test_reporting_is_the_default_and_writes_nothing(self):
        Specialty.objects.create(name="Test Cardiology", slug="test-cardiology")
        set_expertise(self.doctor, "Cardiology")

        output = self.run_command()

        self.assertIn("would link", output)
        self.assertEqual(Profile.objects.get(user=self.doctor).specialties.count(), 0)

    def test_a_near_miss_is_reported_and_left_alone(self):
        Specialty.objects.create(name="Test Cardiology", slug="test-cardiology")
        set_expertise(self.doctor, "Cardiograph")

        output = self.run_command("--apply")

        self.assertIn("NEEDS A DECISION", output)
        self.assertIn("Cardiograph", output)
        self.assertEqual(Profile.objects.get(user=self.doctor).specialties.count(), 0)

    def test_it_is_safe_to_run_twice(self):
        Specialty.objects.create(name="Test Cardiology", slug="test-cardiology")
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
        if field in ("visitType", "appointmentMode"):
            return set(re.findall(r'name="%s" value="([^"]+)"' % field, html))
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
            # Taken from the page like the rest: the modes offered depend on
            # the slot (plan 12.1), so hard-coding one here would stop testing
            # "what the page offers, the server takes".
            "appointmentMode": sorted(self.offered(html, "appointmentMode"))[0],
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


# ---------------------------------------------------------------------------
# Phase 3 - notification layer
# ---------------------------------------------------------------------------


class NotificationTestCase(TestCase):
    """One appointment, one doctor, one patient email address."""

    #: An event with exactly one recipient. The queue-mechanics tests count
    #: messages ("was this sent twice?"), so they need an event whose audience
    #: is one person. `appointment.booked` used to be that event and no longer
    #: is: it reaches the doctor *and* the patient since plan 4.6, so counting
    #: against it would prove nothing about the queue.
    ONE_RECIPIENT = "appointment.confirmed"

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="notify-doctor", email="notify-doctor@example.com",
            role=CustomUser.Role.DOCTOR, first_name="Anna", last_name="Magembe",
        )
        cls.slot = AvailableSlot.objects.create(
            user=cls.doctor,
            date=timezone.localdate() + timedelta(days=3),
            time=time(14, 30),
        )
        cls.appointment = Appointment.objects.create(
            fullName="Asha Mbwana", birthDate=date(1990, 5, 1),
            gender=Appointment.Gender.FEMALE, email="asha@example.com",
            phone="0712345678", visitType=Appointment.VisitType.NEW_PATIENT,
            reasonVisit=Appointment.ReasonForVisit.LAB_TEST, slot=cls.slot,
        )

    def setUp(self):
        mail.outbox = []


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class NotifyApiTests(NotificationTestCase):
    """Plan 3.1: call sites emit events, never messages."""

    def test_every_declared_event_renders_and_queues(self):
        """All eight, because a template that does not exist is an event that
        silently never reaches anyone."""
        for name in sorted(event_registry.REGISTRY):
            with self.subTest(event=name):
                NotificationLog.objects.all().delete()
                queued = notify(name, self.appointment)

                self.assertTrue(queued, f"{name} queued nothing")
                for message in queued:
                    self.assertTrue(message.subject.strip())
                    self.assertTrue(message.body.strip())
                    self.assertEqual(message.status, NotificationLog.Status.PENDING)

    def test_a_new_booking_notifies_the_doctor_being_booked(self):
        """Decision 8: not a central AGKI inbox. The platform hosts doctors
        from other organisations, who cannot watch someone else's dashboard."""
        queued = notify("appointment.booked", self.appointment)

        self.assertIn(self.doctor.email, [m.recipient for m in queued])

    def test_a_new_booking_also_tells_the_patient_what_happens_next(self):
        """Design §6 step 4. The patient half of this event was added with the
        booking flow (plan 4.6) — they need their reference and, more to the
        point, to understand that nothing is confirmed yet."""
        queued = notify("appointment.booked", self.appointment)
        patient = next(m for m in queued if m.recipient == "asha@example.com")

        self.assertIn(self.appointment.bookingID, patient.body)
        self.assertNotIn("confirmed", patient.subject.lower())
        self.assertIn("not confirmed yet", patient.body.lower())

    def test_patient_facing_events_go_to_the_patient(self):
        for name in (
            "appointment.confirmed",
            "appointment.time_declined",
            "appointment.confirmed",
            "appointment.completed",
        ):
            with self.subTest(event=name):
                queued = notify(name, self.appointment)
                self.assertEqual([m.recipient for m in queued], ["asha@example.com"])

    def test_expiry_tells_both_parties(self):
        """Never silent, on either side (design §6.1)."""
        recipients = {m.recipient for m in notify("appointment.expired", self.appointment)}

        self.assertEqual(recipients, {"asha@example.com", self.doctor.email})

    def test_an_unknown_event_fails_loudly(self):
        """A typo would otherwise be a message that is simply never sent."""
        with self.assertRaises(LookupError) as raised:
            notify("appointment.definitely_not_an_event", self.appointment)

        self.assertIn("Known events", str(raised.exception))

    def test_a_missing_address_is_recorded_rather_than_dropped(self):
        CustomUser.objects.filter(pk=self.doctor.pk).update(email="")
        # Re-read: the class fixture holds the doctor from before the update,
        # and  is cached against it.
        appointment = Appointment.objects.select_related("slot__user").get(
            pk=self.appointment.pk
        )

        notify("appointment.booked", appointment)

        # The patient's copy still queues normally; it is the doctor's, with
        # nowhere to go, that must be recorded rather than dropped.
        skipped = NotificationLog.objects.get(status=NotificationLog.Status.SKIPPED)
        self.assertEqual(skipped.event, "appointment.booked")
        self.assertIn("No email address", skipped.error)

    def test_the_message_body_is_stored_not_just_the_fact_of_it(self):
        """"Did the patient get told, and what did it say?" is one question in
        practice, and the second half is the useful one."""
        message = notify("appointment.confirmed", self.appointment)[0]

        self.assertIn("Asha Mbwana", message.body)
        self.assertIn(self.appointment.bookingID, message.body)


@override_settings(NOTIFICATIONS_DELIVERY="queued", SITE_URL="https://agki.example")
class MessageContentTests(NotificationTestCase):
    """Plan 3.5 and 3.6 — what the messages actually say."""

    def body(self, event_name, audience=None):
        messages = notify(event_name, self.appointment)
        if audience is None:
            return messages[0].body
        return next(m.body for m in messages if m.recipient == audience)

    def test_every_patient_facing_message_links_back_to_the_appointment(self):
        """The flow needs the patient to return twice and there are no patient
        accounts, so the link is load-bearing, not a courtesy (design §0.1)."""
        for name in (
            "appointment.confirmed",
            "appointment.time_declined",
            "appointment.confirmed",
            "appointment.completed",
        ):
            with self.subTest(event=name):
                self.assertIn("https://agki.example", self.body(name))

    def test_the_link_carries_the_booking_reference(self):
        body = self.body("appointment.confirmed")

        self.assertIn(f"bookingID={self.appointment.bookingID}", body)

    def test_the_confirmation_says_it_is_settled_and_how(self):
        """One confirmation (plan 12.1), so this message ends the negotiation
        rather than handing it back. It used to say "your turn" and ask the
        patient to choose a mode; there is nothing left for them to choose.

        The mode is set on the fixture here rather than assumed: this class's
        appointment is a fresh request, and "how" is the half of the message
        that the single-confirmation flow made load-bearing."""
        Appointment.objects.filter(pk=self.appointment.pk).update(
            appointmentMode=Appointment.Mode.ONLINE
        )
        self.appointment.refresh_from_db()

        body = self.body("appointment.confirmed")

        self.assertIn("confirmed", body.lower())
        self.assertNotIn("your turn", body.lower())
        self.assertIn("Online video call", body)

    def test_expiry_tells_the_patient_plainly_what_happened(self):
        body = self.body("appointment.expired", audience="asha@example.com")

        self.assertIn("expired", body.lower())
        self.assertIn("released", body.lower())
        self.assertIn("Nothing was wrong with your booking", body)

    def test_the_confirmation_includes_the_meeting_link_when_there_is_one(self):
        Appointment.objects.filter(pk=self.appointment.pk).update(
            appointmentMode=Appointment.Mode.ONLINE,
            meeting_url="https://meet.example/agki-123",
        )
        self.appointment.refresh_from_db()

        self.assertIn("https://meet.example/agki-123", self.body("appointment.confirmed"))

    def test_no_meeting_link_is_promised_when_there_is_none(self):
        self.assertNotIn("Join the call", self.body("appointment.confirmed"))

    def test_the_provider_message_links_to_the_dashboard_not_the_lookup_form(self):
        body = self.body("appointment.booked", audience=self.doctor.email)

        self.assertIn("/doctorDashboard/appointment-details/", body)
        self.assertNotIn("bookingID=", body)

    def test_the_patients_copy_of_a_booking_links_to_the_lookup_not_the_dashboard(self):
        """Same event, two audiences, two links — the patient has no dashboard
        to be sent to, and no account to sign into if they followed one."""
        body = self.body("appointment.booked", audience="asha@example.com")

        self.assertIn(f"bookingID={self.appointment.bookingID}", body)
        self.assertNotIn("/doctorDashboard/", body)

    def test_no_message_promises_an_sms(self):
        """Plan 3.6: nothing says "you'll get a text" until SmsChannel is real.
        The old templates promised exactly that with no integration anywhere."""
        for name in sorted(event_registry.REGISTRY):
            with self.subTest(event=name):
                NotificationLog.objects.all().delete()
                for message in notify(name, self.appointment):
                    self.assertNotIn("SMS", message.body)
                    self.assertNotIn("text message", message.body.lower())


class CopyDisciplineTests(TestCase):
    """Plan 3.6, applied to the pages as well as the emails."""

    # The orphan `appointment_details.html` is excluded: no view renders it and
    # step 5.8 deletes it. Guarding copy in a template scheduled for deletion
    # is the same waste as guarding its image tags was in 1.1.
    EXCLUDED = {"appointment_details.html"}

    def test_no_page_promises_an_sms_reminder(self):
        from pathlib import Path

        from django.conf import settings

        offenders = []
        for app in ("appointments", "doctorDashboard"):
            root = Path(settings.BASE_DIR) / app / "templates"
            for path in root.rglob("*.html"):
                if path.name in self.EXCLUDED:
                    continue
                text = path.read_text(encoding="utf-8")
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if "sms" in line.lower() and "SmsChannel" not in line:
                        offenders.append(f"{path.name}:{line_number}: {line.strip()[:70]}")

        self.assertEqual(
            offenders,
            [],
            "These pages mention SMS, which this system does not send: "
            + " | ".join(offenders),
        )


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class DeliveryTests(NotificationTestCase):
    """Plan 3.2 and 3.4 — the queue, and what it records."""

    def test_nothing_is_sent_until_the_queue_is_drained(self):
        """The point of the queue: a patient submitting a booking is not
        waiting on an SMTP handshake."""
        notify(self.ONE_RECIPIENT, self.appointment)

        self.assertEqual(len(mail.outbox), 0)

        call_command("send_pending_notifications", stdout=StringIO())

        self.assertEqual(len(mail.outbox), 1)

    def test_a_delivered_message_is_marked_sent_and_timestamped(self):
        message = notify(self.ONE_RECIPIENT, self.appointment)[0]

        call_command("send_pending_notifications", stdout=StringIO())

        message.refresh_from_db()
        self.assertEqual(message.status, NotificationLog.Status.SENT)
        self.assertIsNotNone(message.sent_at)
        self.assertEqual(message.attempts, 1)

    def test_the_email_carries_the_stored_subject_and_body(self):
        message = notify("appointment.confirmed", self.appointment)[0]

        call_command("send_pending_notifications", stdout=StringIO())

        self.assertEqual(mail.outbox[0].subject, message.subject)
        self.assertEqual(mail.outbox[0].body, message.body)
        self.assertEqual(mail.outbox[0].to, ["asha@example.com"])

    def test_draining_twice_does_not_send_twice(self):
        notify(self.ONE_RECIPIENT, self.appointment)

        call_command("send_pending_notifications", stdout=StringIO())
        call_command("send_pending_notifications", stdout=StringIO())

        self.assertEqual(len(mail.outbox), 1)

    def test_a_failed_send_is_retried_and_then_given_up_on(self):
        message = notify(self.ONE_RECIPIENT, self.appointment)[0]

        with mock.patch(
            "appointments.notifications.channels.EmailChannel.send",
            side_effect=OSError("mail server refused the connection"),
        ):
            for _ in range(NotificationLog.MAX_ATTEMPTS + 2):
                call_command("send_pending_notifications", stdout=StringIO())

        message.refresh_from_db()
        self.assertEqual(message.status, NotificationLog.Status.FAILED)
        self.assertEqual(message.attempts, NotificationLog.MAX_ATTEMPTS)
        self.assertIn("mail server refused", message.error)

    def test_a_failure_does_not_stop_the_rest_of_the_run(self):
        """One bad address must not hold up everybody else's mail."""
        first = notify(self.ONE_RECIPIENT, self.appointment)[0]
        # A different recipient, so "the rest of the run went out" is checked
        # by who received it rather than only by a count. `booked` goes to the
        # doctor as well as the patient, so this queues one of each and the
        # patient's is the one made to fail.
        for message in notify("appointment.booked", self.appointment):
            if message.recipient != self.doctor.email:
                message.delete()

        original = channels.EmailChannel.send

        def fail_first(self, message):
            if message.pk == first.pk:
                raise OSError("nope")
            return original(self, message)

        with mock.patch.object(channels.EmailChannel, "send", fail_first):
            call_command("send_pending_notifications", stdout=StringIO())

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.doctor.email])

    def test_a_dry_run_changes_nothing(self):
        notify(self.ONE_RECIPIENT, self.appointment)
        out = StringIO()

        call_command("send_pending_notifications", "--dry-run", stdout=out)

        self.assertIn("would send", out.getvalue())
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(
            NotificationLog.objects.filter(status=NotificationLog.Status.PENDING).count(), 1
        )

    def test_exhausted_messages_are_reported_on_every_run(self):
        """Somebody who was never told stays visible until a person looks."""
        NotificationLog.objects.create(
            appointment=self.appointment, event="appointment.booked",
            recipient="x@example.com", channel="email",
            status=NotificationLog.Status.FAILED, attempts=3,
        )
        out = StringIO()

        call_command("send_pending_notifications", stdout=out)

        self.assertIn("exhausted their retries", out.getvalue())

    @override_settings(NOTIFICATIONS_DELIVERY="immediate")
    def test_immediate_mode_sends_without_the_cron_job(self):
        """The dev default, so a developer sees their message in the console.

        Wrapped in captureOnCommitCallbacks because immediate delivery is
        deliberately deferred to transaction commit — a booking that rolls back
        must not email anybody about an appointment that does not exist.
        """
        with self.captureOnCommitCallbacks(execute=True):
            notify(self.ONE_RECIPIENT, self.appointment)

        self.assertEqual(len(mail.outbox), 1)

    @override_settings(NOTIFICATIONS_DELIVERY="immediate")
    def test_a_rolled_back_booking_emails_nobody(self):
        with self.captureOnCommitCallbacks(execute=False):
            notify(self.ONE_RECIPIENT, self.appointment)

        self.assertEqual(len(mail.outbox), 0)


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class SmsSeamTests(NotificationTestCase):
    """Plan 3.3. The stub exists so the seam is real and tested rather than
    imagined — a seam nobody has run is a guess about a seam."""

    def test_the_sms_channel_knows_where_to_reach_both_parties(self):
        """Recipient data already exists, which is why decision 3 could say
        "email now, architected for SMS" and mean it: SMS needs an integration,
        not a new data-capture form."""
        sms = channels.get("sms")

        self.assertEqual(sms.address_for(events.PATIENT, self.appointment), "0712345678")
        self.assertEqual(
            sms.address_for(events.PROVIDER, self.appointment), self.doctor.phone
        )

    def test_registering_sms_for_an_event_queues_an_sms(self):
        queued = notify("appointment.booked", self.appointment, channels=("sms",))

        self.assertEqual({m.channel for m in queued}, {"sms"})
        # Both audiences resolved to a phone number rather than an email
        # address: that is the seam working, not merely the channel being
        # selected.
        self.assertEqual(
            {m.recipient for m in queued},
            {self.doctor.phone, self.appointment.phone},
        )

    def test_sending_it_fails_loudly_and_is_recorded(self):
        """It must not silently look like a success, and it must not take the
        booking down with it."""
        message = notify(self.ONE_RECIPIENT, self.appointment, channels=("sms",))[0]

        call_command("send_pending_notifications", stdout=StringIO())

        message.refresh_from_db()
        self.assertIn("NotImplementedError", message.error)
        self.assertNotEqual(message.status, NotificationLog.Status.SENT)


class NoDirectMailTests(TestCase):
    """Plan 3.x phase gate: no call site imports `send_mail`.

    That coupling is exactly what forces a rewrite when a second channel
    appears, and the old code had it — `send_mail` inline in a view with the
    copy in a Python string. The one legitimate importer is the email channel
    itself.
    """

    ALLOWED = {"appointments/notifications/channels.py"}

    def test_only_the_email_channel_talks_to_the_mail_api(self):
        from pathlib import Path

        from django.conf import settings

        offenders = []
        root = Path(settings.BASE_DIR)
        for app in ("appointments", "doctorDashboard"):
            for path in (root / app).rglob("*.py"):
                relative = path.relative_to(root).as_posix()
                if relative in self.ALLOWED or "/migrations/" in relative:
                    continue
                # A call, not a mention: several docstrings in this project
                # name the mail API precisely to explain why nothing calls it.
                # The needle is assembled rather than written out so that this
                # file does not match itself.
                needle = "send_mail" + "("
                if needle in path.read_text(encoding="utf-8"):
                    offenders.append(relative)

        self.assertEqual(
            offenders,
            [],
            "These modules call send_mail directly instead of emitting an "
            "event: " + ", ".join(offenders),
        )


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class ConcurrentDrainTests(NotificationTestCase):
    """Two cron runs can overlap — a slow SMTP host is exactly when that
    happens, and exactly when sending the same message twice would be most
    confusing to the person receiving it.

    Both tests here exist because mutation-checking found them missing:
    deleting the claim broke nothing, since "drain twice in sequence" is a
    different situation from "two runs holding the same pending row".
    """

    def test_two_runs_holding_the_same_row_send_it_once(self):
        message = notify(self.ONE_RECIPIENT, self.appointment)[0]
        # Two runs that each read the row while it was still pending: same
        # primary key, same `attempts`, no knowledge of each other.
        first_run_view = NotificationLog.objects.get(pk=message.pk)
        second_run_view = NotificationLog.objects.get(pk=message.pk)

        self.assertTrue(deliver(first_run_view))
        self.assertFalse(deliver(second_run_view))
        self.assertEqual(len(mail.outbox), 1)

    def test_a_run_starting_mid_send_does_not_send_again(self):
        """The case the first version of this got wrong. Counting attempts was
        not enough: a second run starting while the first was inside `send()`
        saw the row still pending and delivered it a second time. The claim is
        a status transition now, so the row is no longer pending by then."""
        notify(self.ONE_RECIPIENT, self.appointment)
        overlapping = []

        real_send = channels.EmailChannel.send

        def send_then_overlap(self, message):
            if not overlapping:
                overlapping.append(True)
                # A second cron tick, fired while this one is mid-delivery.
                call_command("send_pending_notifications", stdout=StringIO())
            return real_send(self, message)

        with mock.patch.object(channels.EmailChannel, "send", send_then_overlap):
            call_command("send_pending_notifications", stdout=StringIO())

        self.assertTrue(overlapping, "the overlapping run never happened")
        self.assertEqual(len(mail.outbox), 1)

    def test_a_run_killed_mid_send_does_not_strand_its_message(self):
        """Passenger kills application processes as it pleases, so a claim can
        outlive the run that took it. Left alone, that message is somebody who
        is never told — the silent failure the outbox exists to avoid."""
        message = notify(self.ONE_RECIPIENT, self.appointment)[0]
        NotificationLog.objects.filter(pk=message.pk).update(
            status=NotificationLog.Status.SENDING,
            created_at=timezone.now() - NotificationLog.STALE_CLAIM - timedelta(minutes=1),
        )

        call_command("send_pending_notifications", stdout=StringIO())

        message.refresh_from_db()
        self.assertEqual(message.status, NotificationLog.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_claim_that_is_still_fresh_is_left_alone(self):
        """Otherwise the recovery would itself cause the double-send it is
        meant to clean up after."""
        message = notify(self.ONE_RECIPIENT, self.appointment)[0]
        NotificationLog.objects.filter(pk=message.pk).update(
            status=NotificationLog.Status.SENDING
        )

        call_command("send_pending_notifications", stdout=StringIO())

        message.refresh_from_db()
        self.assertEqual(message.status, NotificationLog.Status.SENDING)
        self.assertEqual(len(mail.outbox), 0)


class DeliveryModeDefaultTests(TestCase):
    """The safe default matters more than the configured value.

    Which mode production runs in is a deployment setting, and a test cannot
    meaningfully assert an environment variable. What it can assert is the
    fallback: if the setting goes missing entirely, notifications must queue
    rather than block a patient's request on a mail server.
    """

    def test_an_absent_setting_falls_back_to_queued(self):
        with mock.patch("appointments.notifications.settings", spec=[]):
            self.assertEqual(delivery_mode(), "queued")


# ---------------------------------------------------------------------------
# Phase 4 — the public booking flow
# ---------------------------------------------------------------------------
#
# The phase gate is one sentence: "a booking can be made, confirmed, given a
# mode, confirmed again and cancelled — end to end, by test, with the right
# email at each step." `BookingFlowEndToEndTests` at the bottom is literally
# that sentence; everything above it is the pieces, tested where they can fail
# individually.


class FlowTestCase(TestCase):
    """One doctor, one free slot far enough out to be bookable."""

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="flow-doctor", email="flow-doctor@example.com",
            role=CustomUser.Role.DOCTOR, first_name="Anna", last_name="Magembe",
        )
        cls.settings_row = AppointmentSettings.load()

    def setUp(self):
        mail.outbox = []
        caches["ratelimit"].clear()
        self._slot_minute = 0

    def make_slot(self, days=3, hour=10, minute=None, doctor=None):
        """A fresh slot, at a minute nobody has used yet in this test.

        `AvailableSlot` is unique on (user, date, time) since plan 2.8, so a
        fixed time would make the second call inside a subTest loop raise —
        and the failure would look like a bug in whatever the loop was
        actually testing.
        """
        if minute is None:
            hour_offset, minute = divmod(self._slot_minute, 60)
            hour += hour_offset
            self._slot_minute += 1
        return AvailableSlot.objects.create(
            user=doctor or self.doctor,
            date=timezone.localdate() + timedelta(days=days),
            time=time(hour, minute),
        )

    def make_appointment(self, slot=None, status=None, **extra):
        fields = dict(
            fullName="Asha Mbwana", birthDate=date(1990, 5, 1),
            gender=Appointment.Gender.FEMALE, email="asha@example.com",
            phone="0712345678", visitType=Appointment.VisitType.NEW_PATIENT,
            reasonVisit=Appointment.ReasonForVisit.LAB_TEST,
            slot=slot or self.make_slot(),
        )
        # Every appointment has a mode now: the patient chooses one on the
        # booking form (plan 12.1), so an appointment without one is a state
        # the flow cannot produce - and `transition_to` refuses to confirm it.
        # The rule itself is tested directly in `MeetingLinkRequiredTests`, not
        # through this default.
        fields.setdefault("appointmentMode", Appointment.Mode.PATIENT_TRAVELS)
        fields.update(extra)
        appointment = Appointment.objects.create(**fields)
        if status is not None:
            # Straight to the column: the point of most of these fixtures is a
            # starting state, and going through `transition_to` would emit
            # notifications the test has not asked about.
            Appointment.objects.filter(pk=appointment.pk).update(status=status)
            appointment.refresh_from_db()
        return appointment

    def verify(self, appointment):
        """Prove ownership of `appointment` the way a patient does.

        Resets the limiter first. These tests stand in for many different
        patients arriving from many different addresses; without this, the
        sixth `verify()` in a subTest loop is throttled and the test fails for
        a reason that has nothing to do with what it is checking. The limiter
        itself is tested by `BookingLookupRateLimitTests`.
        """
        caches["ratelimit"].clear()
        return self.client.post(
            reverse("booking_id_lookup"),
            {"bookingID": appointment.bookingID, "verification": appointment.phone},
        )

    def booking_payload(self, **overrides):
        payload = {
            "fullName": "Asha Mbwana", "birthDate": "1990-05-01",
            "gender": Appointment.Gender.FEMALE, "email": "asha@example.com",
            "phone": "0712345678", "visitType": Appointment.VisitType.NEW_PATIENT,
            "reasonVisit": Appointment.ReasonForVisit.LAB_TEST,
            # Required since plan 12.1: how to meet is asked at booking, so the
            # doctor can answer once.
            "appointmentMode": Appointment.Mode.PATIENT_TRAVELS,
            "symptoms": "night sweats",
        }
        payload.update(overrides)
        return payload

    def events_queued(self):
        return list(
            NotificationLog.objects.order_by("created_at", "id").values_list("event", flat=True)
        )


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class TransitionTests(FlowTestCase):
    """Plan 4.1. `transition_to` is the only supported way to change `status`.

    That is not tidiness: a view assigning `status` directly can skip a gate,
    forget the audit row, forget to free the slot, or forget to tell the
    patient — and every one of those fails silently.
    """

    def test_the_legal_moves_are_allowed(self):
        for from_status, to_status in (
            # One confirmation (plan 12.1): request straight to settled.
            (Appointment.Status.PENDING_TIME, Appointment.Status.CONFIRMED),
            (Appointment.Status.PENDING_TIME, Appointment.Status.TIME_DECLINED),
            (Appointment.Status.CONFIRMED, Appointment.Status.COMPLETED),
            (Appointment.Status.CONFIRMED, Appointment.Status.NO_SHOW),
            (Appointment.Status.PENDING_TIME, Appointment.Status.EXPIRED),
            (Appointment.Status.CONFIRMED, Appointment.Status.CANCELLED),
            # The legacy pair, kept answerable for a row that escaped
            # migration 0027 rather than left stranded with no legal move.
            (Appointment.Status.PENDING_MODE, Appointment.Status.CONFIRMED),
            (Appointment.Status.MODE_PROPOSED, Appointment.Status.CONFIRMED),
            (Appointment.Status.PENDING_MODE, Appointment.Status.EXPIRED),
            (Appointment.Status.MODE_PROPOSED, Appointment.Status.EXPIRED),
        ):
            with self.subTest(move=f"{from_status}->{to_status}"):
                appointment = self.make_appointment(status=from_status)

                appointment.transition_to(to_status, actor=self.doctor)

                appointment.refresh_from_db()
                self.assertEqual(appointment.status, to_status)

    def test_a_settled_appointment_cannot_be_confirmed_again(self):
        """What replaces "skipping both gates is refused" (plan 12.1). There is
        one gate now, so a booking *may* go straight from requested to
        confirmed - that is the point. What must still be refused is answering
        an appointment that has already been answered, which is what a doctor
        double-clicking, or two doctors on one account, would do."""
        appointment = self.make_appointment(status=Appointment.Status.CONFIRMED)

        with self.assertRaises(InvalidTransition):
            appointment.transition_to(Appointment.Status.CONFIRMED, actor=self.doctor)

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)

    def test_every_other_illegal_move_is_refused_too(self):
        """Enumerated from the map rather than hand-listed, so a state added
        later cannot quietly acquire moves nobody decided on."""
        every_status = set(Appointment.Status.values)
        for from_status in sorted(every_status):
            illegal = every_status - Appointment.TRANSITIONS[from_status] - {from_status}
            for to_status in sorted(illegal):
                with self.subTest(move=f"{from_status}->{to_status}"):
                    appointment = self.make_appointment(status=from_status)
                    with self.assertRaises(InvalidTransition):
                        appointment.transition_to(to_status, actor=self.doctor)

    def test_a_terminal_state_has_no_way_out(self):
        for terminal in (
            Appointment.Status.COMPLETED, Appointment.Status.CANCELLED,
            Appointment.Status.EXPIRED, Appointment.Status.NO_SHOW,
            Appointment.Status.TIME_DECLINED,
        ):
            with self.subTest(status=terminal):
                self.assertEqual(Appointment.TRANSITIONS[terminal], set())

    def test_declining_the_time_frees_the_slot(self):
        appointment = self.make_appointment()
        self.assertTrue(appointment.slot.is_booked)

        appointment.transition_to(Appointment.Status.TIME_DECLINED, actor=self.doctor)

        appointment.slot.refresh_from_db()
        self.assertFalse(appointment.slot.is_booked)

    def test_cancelling_and_expiring_free_the_slot_too(self):
        for status in (Appointment.Status.CANCELLED, Appointment.Status.EXPIRED):
            with self.subTest(status=status):
                appointment = self.make_appointment()
                appointment.transition_to(status)

                appointment.slot.refresh_from_db()
                self.assertFalse(appointment.slot.is_booked)

    def test_completing_does_not_free_the_slot(self):
        """That time was used. Re-offering it would be inventing availability
        that never existed."""
        appointment = self.make_appointment(status=Appointment.Status.CONFIRMED)

        appointment.transition_to(Appointment.Status.COMPLETED, actor=self.doctor)

        appointment.slot.refresh_from_db()
        self.assertTrue(appointment.slot.is_booked)

    def test_each_move_writes_one_audit_row(self):
        appointment = self.make_appointment()

        appointment.transition_to(
            Appointment.Status.CONFIRMED, actor=self.doctor, note="Time works.",
        )

        row = appointment.transitions.get()
        self.assertEqual(row.from_status, Appointment.Status.PENDING_TIME)
        self.assertEqual(row.to_status, Appointment.Status.CONFIRMED)
        self.assertEqual(row.actor, self.doctor)
        self.assertEqual(row.note, "Time works.")

    def test_an_actorless_move_says_who_it_was_waiting_on(self):
        """`actor=None` means "nobody with an account" — the patient, or the
        system. An audit that cannot tell those apart is worse than none
        (design §6.1)."""
        appointment = self.make_appointment(status=Appointment.Status.PENDING_TIME)

        appointment.transition_to(
            Appointment.Status.EXPIRED, actor=None, note="Patient never chose a mode.",
        )

        row = appointment.transitions.get()
        self.assertIsNone(row.actor)
        self.assertIn("Patient", row.note)

    def test_the_timestamps_are_stamped(self):
        """One confirmation stamps both columns (plan 12.1): the time and the
        mode are settled in the same act, and two columns that always agree
        beat one column that half the reporting still looks for."""
        appointment = self.make_appointment()

        appointment.transition_to(Appointment.Status.CONFIRMED, actor=self.doctor)

        appointment.refresh_from_db()
        self.assertIsNotNone(appointment.time_confirmed_at)
        self.assertIsNotNone(appointment.mode_confirmed_at)

    def test_a_stale_page_cannot_move_an_appointment_twice(self):
        """Two doctors on the same appointment, or a doctor racing the expiry
        job. The second is working from a `status` that stopped being true
        while their page was open, so the check has to be against the database
        rather than against their copy."""
        appointment = self.make_appointment()
        stale = Appointment.objects.get(pk=appointment.pk)

        appointment.transition_to(Appointment.Status.TIME_DECLINED, actor=self.doctor)

        with self.assertRaises(InvalidTransition):
            stale.transition_to(Appointment.Status.CONFIRMED, actor=self.doctor)
        self.assertEqual(
            Appointment.objects.get(pk=appointment.pk).status,
            Appointment.Status.TIME_DECLINED,
        )

    def test_each_move_emits_its_own_event(self):
        for from_status, to_status, event in (
            (Appointment.Status.PENDING_TIME, Appointment.Status.CONFIRMED,
             "appointment.confirmed"),
            (Appointment.Status.PENDING_TIME, Appointment.Status.TIME_DECLINED,
             "appointment.time_declined"),
            (Appointment.Status.CONFIRMED, Appointment.Status.COMPLETED,
             "appointment.completed"),
            (Appointment.Status.PENDING_TIME, Appointment.Status.CANCELLED,
             "appointment.cancelled"),
            (Appointment.Status.PENDING_TIME, Appointment.Status.EXPIRED,
             "appointment.expired"),
            # A legacy row still reaches the patient with the message that
            # matters, rather than with one named after a flow they were never
            # told about.
            (Appointment.Status.MODE_PROPOSED, Appointment.Status.CONFIRMED,
             "appointment.confirmed"),
        ):
            with self.subTest(move=f"{from_status}->{to_status}"):
                NotificationLog.objects.all().delete()
                appointment = self.make_appointment(status=from_status)

                appointment.transition_to(to_status, actor=self.doctor)

                self.assertEqual(set(self.events_queued()), {event})

    def test_the_same_move_can_send_a_different_message(self):
        """What replaces the old "arriving at PENDING_MODE twice" test.

        One move now carries two meanings: a confirmation the doctor changed
        the meeting mode on is still PENDING_TIME -> CONFIRMED, but "confirmed"
        and "confirmed, though not the way you asked" are different facts to
        the patient. The caller names the event; the map is the default."""
        appointment = self.make_appointment()

        appointment.transition_to(
            Appointment.Status.CONFIRMED,
            actor=self.doctor,
            event="appointment.confirmed_other_mode",
            requested_mode="Online video call",
            reason="I do not do video on Fridays.",
        )

        message = NotificationLog.objects.get(recipient=appointment.email)
        self.assertEqual(message.event, "appointment.confirmed_other_mode")
        self.assertIn("Online video call", message.body)
        self.assertIn("I do not do video on Fridays.", message.body)

    def test_a_no_show_tells_nobody(self):
        """There is no useful message here: the patient did not turn up, and
        the doctor is the one recording it."""
        appointment = self.make_appointment(status=Appointment.Status.CONFIRMED)

        appointment.transition_to(Appointment.Status.NO_SHOW, actor=self.doctor)

        self.assertEqual(NotificationLog.objects.count(), 0)

    def test_a_refused_move_emits_nothing_and_writes_nothing(self):
        appointment = self.make_appointment()

        with self.assertRaises(InvalidTransition):
            appointment.transition_to(Appointment.Status.COMPLETED)

        self.assertEqual(NotificationLog.objects.count(), 0)
        self.assertEqual(AppointmentTransition.objects.count(), 0)


class CancellationWindowTests(FlowTestCase):
    """Decision 9 — cancellation is allowed up to the configured cut-off."""

    def test_a_booking_well_ahead_can_be_cancelled(self):
        self.assertTrue(self.make_appointment().can_be_cancelled())

    def test_a_booking_inside_the_cutoff_cannot(self):
        appointment = self.make_appointment()
        now = appointment.slot.starts_at - timedelta(minutes=30)

        self.assertFalse(appointment.can_be_cancelled(now=now))

    def test_an_appointment_already_ended_cannot_be_cancelled(self):
        for status in (
            Appointment.Status.CANCELLED, Appointment.Status.EXPIRED,
            Appointment.Status.COMPLETED, Appointment.Status.TIME_DECLINED,
        ):
            with self.subTest(status=status):
                appointment = self.make_appointment(status=status)
                self.assertFalse(appointment.can_be_cancelled())


class SlotBookabilityTests(FlowTestCase):
    """Decision D, at the model. One definition, used by both the page that
    offers slots and the view that accepts one — offering a slot the server
    then refuses is the failure this exists to prevent."""

    def test_a_free_slot_far_enough_out_is_bookable(self):
        self.assertTrue(self.make_slot().is_bookable())

    def test_a_booked_slot_is_not(self):
        appointment = self.make_appointment()
        appointment.slot.refresh_from_db()
        self.assertFalse(appointment.slot.is_bookable())

    def test_a_slot_inside_the_notice_window_is_not(self):
        soon = timezone.localtime() + timedelta(minutes=90)
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=soon.date(), time=soon.time(),
        )
        self.assertFalse(slot.is_bookable())

    def test_a_slot_just_outside_the_window_is(self):
        later = timezone.localtime() + timedelta(minutes=150)
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=later.date(), time=later.time(),
        )
        self.assertTrue(slot.is_bookable())


class SearchResultsTests(FlowTestCase):
    """Plan 4.2, 4.3 and the offering half of 4.5."""

    def get(self, **params):
        return self.client.get(reverse("search_results"), params)

    def test_a_doctor_with_no_profile_is_still_listed(self):
        """Design §5.3, and the reason this was a Critical: profiles were
        created lazily, so a doctor who had never opened their own profile page
        was invisible to every patient on the site."""
        Profile.objects.filter(user=self.doctor).delete()
        self.make_slot()

        response = self.get()

        self.assertContains(response, "Anna Magembe")

    def test_a_doctor_is_listed_by_role_not_by_having_a_profile_row(self):
        self.make_slot()
        not_a_doctor = make_staff_user(
            username="ledger", email="ledger@example.com", first_name="Neema",
        )
        AvailableSlot.objects.create(
            user=not_a_doctor, date=timezone.localdate() + timedelta(days=3),
            time=time(9, 0),
        )

        response = self.get()

        self.assertContains(response, "Anna Magembe")
        self.assertNotContains(response, "Neema")

    def test_a_doctor_with_something_free_is_shown_as_bookable(self):
        """The card answers one question - can this doctor be booked, and how
        soon. It used to answer it by listing every bookable minute of the next
        30 days (plan 11.15)."""
        self.make_slot(days=20)

        response = self.get()

        self.assertContains(response, "Bookable")
        self.assertContains(response, reverse("doctor_detail", args=[self.doctor.id]))

    def test_a_doctor_with_nothing_free_says_so_and_stays_listed(self):
        """They are still a real doctor. Dropping them would make the practice
        look smaller than it is (design §5.3)."""
        response = self.get()

        self.assertContains(response, "Anna Magembe")
        self.assertContains(response, "No times published")
        self.assertNotContains(response, "Bookable")

    def test_the_card_names_the_soonest_time_rather_than_all_of_them(self):
        """The soonest time is the most useful fact about a doctor, and it used
        to be buried as the first chip of the first day-group."""
        self.make_slot(days=9, hour=8)
        soonest = self.make_slot(days=3, hour=14)

        # Tags stripped, then whitespace collapsed: the date and the time sit
        # either side of a `<time>` element, so the words a reader sees as one
        # phrase are not contiguous in the markup.
        body = " ".join(strip_tags(self.get().content.decode()).split())

        # Read from the slot rather than rebuilt from the hour asked for:
        # `make_slot` gives each slot its own minute so a subTest loop cannot
        # collide with the unique constraint, so the 14 above is 2:01 PM by the
        # second call. Formatted with Django's `date` filter rather than
        # `strftime`, since `%-d` is not portable to Windows.
        self.assertIn(
            "Next: %s, %s" % (
                date_filter(soonest.date, "D j M"), date_filter(soonest.time, "g:i A"),
            ),
            body,
        )

    def test_the_card_counts_what_is_there(self):
        for day in (3, 4, 5):
            self.make_slot(days=day)
            self.make_slot(days=day, hour=11)

        body = " ".join(self.get().content.decode().split())

        self.assertIn("6 times over 3 days", body)

    def test_the_card_no_longer_lists_the_times_themselves(self):
        """The change itself: 59 chips across 20 day-groups, six cards to a
        page, was not a list a patient could scan."""
        slot = self.make_slot(days=20)

        self.assertNotContains(self.get(), reverse("booking_form", args=[slot.id]))

    def test_the_specialty_filter_is_server_side(self):
        cardiology = Specialty.objects.create(name="Test Cardiology", slug="test-cardiology")
        Specialty.objects.create(name="Test Haematology", slug="test-haematology")
        self.doctor.profile.specialties.add(cardiology)
        self.make_slot()

        self.assertContains(self.get(specialty="test-cardiology"), "Anna Magembe")
        self.assertNotContains(self.get(specialty="test-haematology"), "Anna Magembe")

    def test_a_specialty_with_no_bookable_doctor_is_never_a_live_link(self):
        """Design §5.5. The template's old "Popular Specialties" grid was a
        hardcoded list of links to nothing at all."""
        empty = Specialty.objects.create(name="Test Radiology", slug="test-radiology")

        response = self.get()

        self.assertContains(response, "Radiology")
        self.assertNotContains(response, f"?specialty={empty.slug}")

    def test_a_specialty_whose_doctor_has_no_bookable_slot_is_not_a_link_either(self):
        """The doctor exists and holds the specialty; they simply have nothing
        free. Counting doctors rather than *bookable* doctors would render this
        as a link to an empty page."""
        cardiology = Specialty.objects.create(name="Test Cardiology", slug="test-cardiology")
        self.doctor.profile.specialties.add(cardiology)

        self.assertNotContains(self.get(), "?specialty=cardiology")

    def test_the_location_filter_matches_the_doctors_region(self):
        Profile.objects.filter(user=self.doctor).update(region="Mwanza")
        self.make_slot()

        self.assertContains(self.get(location="mwanza"), "Anna Magembe")
        self.assertNotContains(self.get(location="dodoma"), "Anna Magembe")

    def test_the_removed_fake_filters_are_gone(self):
        """Rating and "Accepting New Patients" filtered on a table nothing
        writes to and on a hardcoded `true` (design §6 step 2)."""
        body = self.get().content.decode()

        for fake in ("Accepting New Patients", "Highest Rated", "Show more times"):
            with self.subTest(control=fake):
                self.assertNotIn(fake, body)


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class BookingFormTests(FlowTestCase):
    """Plan 4.4 and 4.5 — claiming the slot, and refusing to."""

    def book(self, slot, **overrides):
        return self.client.post(
            reverse("booking_form", args=[slot.id]), self.booking_payload(**overrides)
        )

    def test_a_booking_creates_the_appointment_and_marks_the_slot(self):
        slot = self.make_slot()

        response = self.book(slot)

        appointment = Appointment.objects.get()
        self.assertRedirects(
            response, reverse("booking_success_page", args=[appointment.bookingID])
        )
        slot.refresh_from_db()
        self.assertTrue(slot.is_booked)
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)

    def test_a_second_booking_on_the_same_slot_is_refused(self):
        """Design §10 #6, and note this was never a race: `booking_form` did
        not read `is_booked` at all, so two *sequential* bookings both
        succeeded and the database held two appointments for one real time."""
        slot = self.make_slot()
        self.book(slot)

        response = self.book(slot, fullName="Someone Else", email="else@example.com")

        self.assertEqual(response.status_code, 200)  # re-rendered with the error
        self.assertContains(response, "just been taken")
        self.assertEqual(Appointment.objects.count(), 1)

    def test_the_row_lock_catches_a_slot_taken_after_the_form_validated(self):
        """**The layer the other tests were hiding.**

        Mutation-checking found this: deleting the `is_booked` check inside
        `claim_slot` broke nothing, because the form's own check refuses the
        second booking first. That made the lock — the part that exists for the
        actual race — completely untested. Two patients submitting within
        milliseconds both validate against a free slot; only this catches the
        second one.
        """
        slot = self.make_slot()
        form = AppointmentForm(self.booking_payload(), slot=slot)
        self.assertTrue(form.is_valid())  # validated while the slot was free

        self.make_appointment(slot=slot)  # somebody else got there first

        with self.assertRaises(SlotUnavailable):
            claim_slot(slot, form, AppointmentSettings.load())
        self.assertEqual(Appointment.objects.count(), 1)

    def test_a_slot_flagged_booked_with_no_appointment_is_still_refused(self):
        """The case only the `is_booked` check can see.

        Mutation-checking the test above showed it still passed with that check
        deleted — the `OneToOneField` underneath caught the duplicate instead.
        That is the layering working, but it also means the flag itself was
        untested, and the flag can be true with no appointment behind it: the
        pre-flight audit has a `stale_is_booked` check precisely because the
        live data has such rows, and deleting an appointment in the admin
        leaves one. No unique constraint can refuse that; this does.
        """
        slot = self.make_slot()
        form = AppointmentForm(self.booking_payload(), slot=slot)
        self.assertTrue(form.is_valid())
        AvailableSlot.objects.filter(pk=slot.pk).update(is_booked=True)

        with self.assertRaises(SlotUnavailable):
            claim_slot(slot, form, AppointmentSettings.load())
        self.assertEqual(Appointment.objects.count(), 0)

    def test_the_row_lock_also_catches_a_slot_that_became_too_close(self):
        """The same gap on the other rule: a form validated with three hours to
        go, submitted with ninety minutes left."""
        slot = self.make_slot()
        form = AppointmentForm(self.booking_payload(), slot=slot)
        self.assertTrue(form.is_valid())

        with mock.patch(
            "appointments.models.AppointmentSettings.minimum_booking_notice",
            new_callable=mock.PropertyMock, return_value=timedelta(days=30),
        ):
            with self.assertRaises(SlotUnavailable):
                claim_slot(slot, form, AppointmentSettings.load())
        self.assertEqual(Appointment.objects.count(), 0)

    def test_the_form_refuses_a_taken_slot_on_its_own(self):
        """And the mirror of the above: `claim_slot` was hiding the form's
        checks just as thoroughly. The form layer is what produces the message
        the patient reads and keeps what they typed, so it needs its own test
        rather than being inferred from the page happening to work."""
        taken = self.make_appointment().slot
        taken.refresh_from_db()

        form = AppointmentForm(self.booking_payload(), slot=taken)

        self.assertFalse(form.is_valid())
        self.assertIn(SLOT_TAKEN, form.non_field_errors())

    def test_the_form_refuses_a_slot_inside_the_notice_on_its_own(self):
        soon = timezone.localtime() + timedelta(minutes=45)
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=soon.date(), time=soon.time(),
        )

        form = AppointmentForm(self.booking_payload(), slot=slot)

        self.assertFalse(form.is_valid())
        self.assertIn("too close", " ".join(form.non_field_errors()))

    def test_the_database_refuses_it_even_if_the_view_checks_are_skipped(self):
        """The backstop under both application checks: a future caller that
        forgets them still cannot produce two appointments on one slot."""
        first = self.make_appointment()

        with self.assertRaises(IntegrityError):
            Appointment.objects.create(
                fullName="Someone Else", birthDate=date(1985, 2, 2),
                gender=Appointment.Gender.MALE, email="else@example.com",
                phone="0799999999", visitType=Appointment.VisitType.FOLLOW_UP,
                reasonVisit=Appointment.ReasonForVisit.CONSULTATION,
                slot=first.slot,
            )

    def test_a_slot_inside_the_booking_notice_is_refused(self):
        soon = timezone.localtime() + timedelta(minutes=45)
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=soon.date(), time=soon.time(),
        )

        response = self.book(slot)

        self.assertEqual(Appointment.objects.count(), 0)
        self.assertContains(response, "too close")

    def test_the_refusal_says_how_much_notice_is_needed(self):
        """A generic error would leave the patient retrying the same slot."""
        soon = timezone.localtime() + timedelta(minutes=45)
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=soon.date(), time=soon.time(),
        )

        self.assertContains(self.book(slot), "2 hours")

    def test_an_unbookable_slot_says_so_before_the_form_is_filled_in(self):
        appointment = self.make_appointment()

        response = self.client.get(reverse("booking_form", args=[appointment.slot.id]))

        self.assertContains(response, "can no longer be booked")

    def test_the_doctor_is_notified_of_a_new_booking(self):
        """Decision 8: the doctor being booked, not a central AGKI inbox."""
        self.book(self.make_slot())

        booked = NotificationLog.objects.filter(event="appointment.booked")
        self.assertIn(self.doctor.email, [m.recipient for m in booked])

    def test_the_patient_is_told_what_happens_next_not_that_it_is_confirmed(self):
        self.book(self.make_slot())

        patient = NotificationLog.objects.get(
            event="appointment.booked", recipient="asha@example.com"
        )
        self.assertIn("not confirmed yet", patient.body.lower())

    def test_a_rejected_booking_notifies_nobody(self):
        appointment = self.make_appointment()
        NotificationLog.objects.all().delete()

        self.book(appointment.slot, fullName="Someone Else")

        self.assertEqual(NotificationLog.objects.count(), 0)

    def test_the_typed_answers_survive_a_refusal(self):
        """Otherwise a patient refused for a reason that is not their fault
        re-types the whole form."""
        appointment = self.make_appointment()

        response = self.book(appointment.slot, fullName="Juma Ally")

        self.assertContains(response, "Juma Ally")

    def test_the_form_no_longer_asks_for_information_it_throws_away(self):
        """A textarea asking about current medications posted to no model
        field. Unlike the rest of the decorative furniture, that one collected
        clinical information and told nobody."""
        response = self.client.get(reverse("booking_form", args=[self.make_slot().id]))

        self.assertNotContains(response, "currentMedications")

    def test_nothing_intercepts_the_submit(self):
        """Design §10 appointments #3: the old page's submit handler called
        preventDefault() unconditionally and faked success. Bookings reached
        the server only because an earlier line crashed first."""
        body = self.client.get(
            reverse("booking_form", args=[self.make_slot().id])
        ).content.decode()

        self.assertNotIn("Appointment booked successfully", body)
        self.assertNotIn("bookingForm.addEventListener('submit'", body)


class BookingSuccessCopyTests(FlowTestCase):
    """Plan 4.6 — the page a patient sees the moment they have booked."""

    def show(self, appointment):
        self.verify(appointment)
        return self.client.get(
            reverse("booking_success_page", args=[appointment.bookingID])
        )

    def test_it_does_not_claim_the_appointment_is_confirmed(self):
        response = self.show(self.make_appointment())

        self.assertNotContains(response, "Appointment Confirmed")
        self.assertContains(response, "Request sent")

    def test_it_explains_what_happens_next(self):
        """One answer, and the patient is told there is nothing to come back
        for - which is the whole point of the change (plan 12.1)."""
        response = self.show(self.make_appointment())

        self.assertContains(response, "The doctor confirms whether the time still works")
        self.assertContains(response, "no second round")

    def test_it_no_longer_promises_reminders_or_an_insurance_card(self):
        """There is no reminder job, and AGKI takes no insurance (§8.4)."""
        body = self.show(self.make_appointment()).content.decode()

        self.assertNotIn("reminders 24 hours", body)
        self.assertNotIn("insurance card", body)


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class BookingModeTests(FlowTestCase):
    """How to meet is asked at booking now (plan 12.1).

    This replaces `ChooseModeTests`, which covered round two: the page a
    patient came back to after the doctor had confirmed the time. AGKI
    collapsed the two confirmations into one, so the question moved on to the
    booking form and that page was deleted.
    """

    def book(self, slot, **overrides):
        return self.client.post(
            reverse("booking_form", args=[slot.id]),
            self.booking_payload(**overrides),
        )

    def test_the_mode_is_stored_from_the_booking_form(self):
        slot = self.make_slot(days=4)

        self.book(slot, appointmentMode=Appointment.Mode.ONLINE)

        self.assertEqual(
            Appointment.objects.get().appointmentMode, Appointment.Mode.ONLINE
        )

    def test_a_booking_without_one_is_refused(self):
        """It is the doctor's whole decision now. A booking that does not say
        how to meet is one they cannot answer in a single step."""
        slot = self.make_slot(days=4)

        response = self.book(slot, appointmentMode="")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Appointment.objects.exists())

    def test_only_the_modes_that_slot_offers_are_shown(self):
        slot = self.make_slot(days=4)
        AvailableSlot.objects.filter(pk=slot.pk).update(
            offers_online=False, offers_provider_travels=False
        )

        html = self.client.get(reverse("booking_form", args=[slot.id])).content.decode()
        offered = set(re.findall(r'name="appointmentMode" value="([^"]+)"', html))

        self.assertEqual(offered, {Appointment.Mode.PATIENT_TRAVELS})

    def test_a_mode_the_slot_does_not_offer_is_refused_on_post(self):
        """The form is a suggestion; this is the rule. A hand-made POST must
        not commit a doctor to something they never published."""
        slot = self.make_slot(days=4)
        AvailableSlot.objects.filter(pk=slot.pk).update(offers_provider_travels=False)

        response = self.book(slot, appointmentMode=Appointment.Mode.PROVIDER_TRAVELS)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Appointment.objects.exists())

    def test_a_mode_the_model_does_not_accept_is_refused(self):
        slot = self.make_slot(days=4)

        self.book(slot, appointmentMode="carrier-pigeon")

        self.assertFalse(Appointment.objects.exists())

    def test_the_summary_names_the_modes_this_time_offers(self):
        """The doctor's side of the choice, beside the choice itself. It read
        "Whichever you choose above" for a day - true, and no help to somebody
        deciding (AGKI, 2026-09-03)."""
        slot = self.make_slot(days=4)
        AvailableSlot.objects.filter(pk=slot.pk).update(offers_online=False)

        html = self.client.get(reverse("booking_form", args=[slot.id])).content.decode()
        summary = html.split("Preferred meeting modes", 1)[1].split("</div>", 3)[1]

        self.assertIn("Provider travels to the patient", summary)
        self.assertIn("Patient travels to the provider", summary)
        self.assertNotIn("Online video call", summary)

    def test_the_doctor_is_told_how_the_patient_wants_to_meet(self):
        """They answer in one step, so everything they need has to be in the
        message that asks them."""
        slot = self.make_slot(days=4)

        self.book(slot, appointmentMode=Appointment.Mode.ONLINE)

        to_doctor = NotificationLog.objects.get(recipient=self.doctor.email)
        self.assertIn("Online video call", to_doctor.body)


class AppointmentDetailStateTests(FlowTestCase):
    """Plan 4.8 — the real state, and what the patient must do next."""

    def show(self, appointment):
        self.verify(appointment)
        return self.client.get(
            reverse("appointment_detail", args=[appointment.bookingID])
        )

    def test_every_status_renders_its_own_label(self):
        for status in Appointment.Status.values:
            with self.subTest(status=status):
                appointment = self.make_appointment(status=status)
                # Escaped, because several labels contain an apostrophe and
                # the page is correctly autoescaping them.
                self.assertContains(
                    self.show(appointment), escape(Appointment.Status(status).label)
                )

    def test_a_pending_booking_is_not_announced_as_confirmed(self):
        """The old banner was hardcoded to "Appointment Confirmed" for every
        appointment, whatever had actually happened to it."""
        response = self.show(self.make_appointment())

        self.assertNotContains(response, "Appointment Confirmed")

    def test_the_retired_second_round_leaves_no_link_behind(self):
        """The page it pointed at is deleted (plan 12.1). A button to a 404 is
        worse than no button, and an old booking is exactly where one would
        survive."""
        body = self.show(self.make_appointment()).content.decode()

        self.assertNotIn("choose-mode", body)

    def test_the_buttons_with_no_view_behind_them_are_gone(self):
        """Design §10 appointments #8. A working Cancel beats five fake
        buttons."""
        body = self.show(self.make_appointment()).content.decode()

        for fake in ("Contact Doctor", "Reschedule", "Live Chat", "Add to Calendar"):
            with self.subTest(control=fake):
                self.assertNotIn(fake, body)

    def test_no_demo_data_survives(self):
        body = self.show(self.make_appointment()).content.decode()

        for leftover in ("Sarah Johnson", "2025-10-11T09:00:00", "$25 fee"):
            with self.subTest(leftover=leftover):
                self.assertNotIn(leftover, body)

    def test_the_meeting_link_is_shown_once_it_exists_and_not_before(self):
        appointment = self.make_appointment(
            status=Appointment.Status.PENDING_TIME,
            appointmentMode=Appointment.Mode.ONLINE,
        )
        self.assertNotContains(self.show(appointment), "meet.example")

        Appointment.objects.filter(pk=appointment.pk).update(
            status=Appointment.Status.CONFIRMED, meeting_url="https://meet.example/x",
        )
        self.assertContains(self.show(appointment), "https://meet.example/x")

    def test_the_progress_timeline_comes_from_the_audit_trail(self):
        appointment = self.make_appointment()
        appointment.transition_to(Appointment.Status.CONFIRMED, actor=self.doctor)

        self.assertContains(
            self.show(appointment),
            escape(Appointment.Status(Appointment.Status.CONFIRMED).label),
        )


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class CancelActionTests(FlowTestCase):
    """Plan 4.9 — the one Manage-Appointment control that is real."""

    def url(self, appointment):
        return reverse("appointment_cancel", args=[appointment.bookingID])

    def test_cancelling_writes_the_state_and_frees_the_slot(self):
        appointment = self.make_appointment()
        self.verify(appointment)

        self.client.post(self.url(appointment), {"reason": "Travelling that week"})

        appointment.refresh_from_db()
        appointment.slot.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CANCELLED)
        self.assertFalse(appointment.slot.is_booked)
        self.assertEqual(appointment.cancellation_reason, "Travelling that week")
        self.assertIsNotNone(appointment.cancelled_at)

    def test_the_freed_slot_can_be_booked_by_somebody_else(self):
        """The point of freeing it, and the thing a status change alone would
        not achieve."""
        appointment = self.make_appointment()
        self.verify(appointment)
        self.client.post(self.url(appointment))

        # Read from the doctor's page, which is where times are offered since
        # plan 11.15. The rule is the same one: a freed slot is bookable again.
        response = self.client.get(
            reverse("doctor_detail", args=[appointment.slot.user_id])
        )

        self.assertContains(
            response, reverse("booking_form", args=[appointment.slot.id])
        )

    def test_both_parties_are_told(self):
        appointment = self.make_appointment()
        self.verify(appointment)
        NotificationLog.objects.all().delete()

        self.client.post(self.url(appointment))

        recipients = set(
            NotificationLog.objects.filter(event="appointment.cancelled")
            .values_list("recipient", flat=True)
        )
        self.assertEqual(recipients, {"asha@example.com", self.doctor.email})

    def test_a_stranger_cannot_cancel_somebody_elses_appointment(self):
        appointment = self.make_appointment()

        response = self.client.post(self.url(appointment))

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)
        self.assertRedirects(response, reverse("booking_id_lookup"))

    def test_a_get_cannot_cancel(self):
        """`delete_item` is the cautionary tale: a destructive GET is
        triggerable by anything that follows a link, a crawler included."""
        appointment = self.make_appointment()
        self.verify(appointment)

        response = self.client.get(self.url(appointment))

        appointment.refresh_from_db()
        self.assertEqual(response.status_code, 405)
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)

    def test_cancelling_inside_the_cutoff_is_refused(self):
        """A cut-off wider than the slot's distance, so the window is shut.

        This used to patch **three** days of cut-off against a slot three days
        out, described as "the same arithmetic as a real too late, without
        waiting for the clock". It was the same arithmetic and it did wait for
        the clock: `can_be_cancelled` is `now <= starts_at - cutoff`, and with
        the two equal that threshold lands at the slot's own time of day,
        today. The fixture publishes at 10:00, so the test failed every run
        before 10:00 in the morning and passed every run after it.

        Five days against three moves the threshold two days into the past,
        where no hour of the day can reach it. The test still asserts what it
        always meant to — a cancellation inside the window is refused — and it
        now asserts it at every hour. Deliberately *not* four days: three days
        plus a late slot is nearly four, and this test has already been on the
        wrong side of a boundary once.
        """
        appointment = self.make_appointment()
        self.verify(appointment)
        with mock.patch(
            "appointments.models.AppointmentSettings.cancellation_cutoff",
            new_callable=mock.PropertyMock,
            return_value=timedelta(days=5),
        ):
            self.client.post(self.url(appointment))

        appointment.refresh_from_db()
        appointment.slot.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)
        self.assertTrue(appointment.slot.is_booked)

    def test_the_cancel_control_is_hidden_once_it_would_be_refused(self):
        appointment = self.make_appointment(status=Appointment.Status.CANCELLED)
        self.verify(appointment)

        response = self.client.get(
            reverse("appointment_detail", args=[appointment.bookingID])
        )

        self.assertNotContains(response, "Cancel Appointment")


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class BookingFlowEndToEndTests(FlowTestCase):
    """**The Phase 4 gate**, rewritten for the single-confirmation flow (12.1).

    A booking made with the mode on it, one confirmation, and the whole thing
    cancelled - with the right message to the right person at every step. It
    used to walk four rounds; the two in the middle no longer exist.
    """

    def test_the_whole_negotiation(self):
        slot = self.make_slot(days=5)

        # 1. The patient books.
        self.client.post(
            reverse("booking_form", args=[slot.id]), self.booking_payload()
        )
        appointment = Appointment.objects.get()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)
        self.assertEqual(
            {m.recipient for m in NotificationLog.objects.all()},
            {self.doctor.email, "asha@example.com"},
        )

        # The doctor was told the mode as well as the time, because they are
        # about to answer both in one action.
        to_doctor = NotificationLog.objects.get(recipient=self.doctor.email)
        self.assertIn("Patient travels to the provider", to_doctor.body)

        # 2. The one confirmation, with the meeting link decision B requires
        #    for an online appointment.
        NotificationLog.objects.all().delete()
        appointment.appointmentMode = Appointment.Mode.ONLINE
        appointment.meeting_url = "https://meet.example/agki-1"
        appointment.transition_to(Appointment.Status.CONFIRMED, actor=self.doctor)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)
        told = NotificationLog.objects.get()
        self.assertEqual(told.event, "appointment.confirmed")
        self.assertEqual(told.recipient, "asha@example.com")
        self.assertIn("https://meet.example/agki-1", told.body)

        # Both halves of the settlement are stamped by the one move.
        self.assertIsNotNone(appointment.time_confirmed_at)
        self.assertIsNotNone(appointment.mode_confirmed_at)

        # 3. The patient cancels.
        NotificationLog.objects.all().delete()
        self.client.post(
            reverse("appointment_cancel", args=[appointment.bookingID]),
            {"reason": "Recovered"},
        )
        appointment.refresh_from_db()
        appointment.slot.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CANCELLED)
        self.assertFalse(appointment.slot.is_booked)
        self.assertEqual(
            set(NotificationLog.objects.values_list("recipient", flat=True)),
            {"asha@example.com", self.doctor.email},
        )

        # 6. The audit trail can answer "who did what, when" months later.
        self.assertEqual(
            [row.to_status for row in appointment.transitions.all()],
            [
                Appointment.Status.CONFIRMED,
                Appointment.Status.CANCELLED,
            ],
        )
        # The doctor confirmed; the patient cancelled. `actor=None` is the
        # patient or the system - nobody with an account (design §6.1).
        self.assertEqual(
            [row.actor for row in appointment.transitions.all()],
            [self.doctor, None],
        )


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class WithdrawnDoctorTests(FlowTestCase):
    """Plan 10.3 — withdrawing approval must remove the listing, not just the login.

    **This class is design §4.5's mitigation, written down.** Decision F opened
    registration to anyone who can receive email, and deferred the question of
    whether a registrant is really a doctor. What AGKI has in the meantime is
    one lever: an administrator switches `is_approved` off. These tests are the
    difference between that lever working and merely appearing to.

    The failure they exist to prevent is specific and quiet: a doctor is
    reported, an administrator withdraws approval, the account can no longer
    sign in — and the card stays on the public site, taking bookings that will
    never be confirmed, because nothing connected the two.
    """

    def withdraw(self, doctor=None):
        """What the console's "Approved for login" toggle does (plan 5.11)."""
        CustomUser.objects.filter(pk=(doctor or self.doctor).pk).update(
            is_approved=False
        )

    def test_a_withdrawn_doctor_leaves_the_public_search(self):
        self.make_slot()
        listed_before = self.client.get(reverse("search_results"))
        self.assertContains(listed_before, "Anna Magembe")

        self.withdraw()

        self.assertNotContains(
            self.client.get(reverse("search_results")), "Anna Magembe"
        )

    def test_an_approved_doctor_still_appears(self):
        """The other half, and not a formality: a filter that removes everyone
        would pass the test above and take the whole site down with it."""
        self.make_slot()

        self.assertContains(
            self.client.get(reverse("search_results")), "Anna Magembe"
        )

    def test_a_withdrawn_doctors_slots_stop_counting_towards_a_specialty(self):
        """Otherwise the browse grid offers a specialty that leads nowhere.

        4.2 made these counts count *bookable* doctors rather than doctors,
        so a specialty whose doctors are all booked reads as unavailable
        instead of as a link to an empty page. A switched-off doctor reaches
        the same dead end by a different route, and would have kept the
        specialty looking staffed on the way.
        """
        cardiology = Specialty.objects.create(name="Test Cardiology", slug="test-cardiology")
        self.doctor.profile.specialties.add(cardiology)
        self.make_slot()

        counted = self.client.get(reverse("search_results")).context["specialties"]
        self.assertEqual(
            [s.bookable_doctors for s in counted if s.slug == "test-cardiology"], [1]
        )

        self.withdraw()

        counted = self.client.get(reverse("search_results")).context["specialties"]
        self.assertEqual(
            [s.bookable_doctors for s in counted if s.slug == "test-cardiology"], [0]
        )

    def test_the_booking_page_refuses_a_withdrawn_doctors_slot(self):
        """The half that makes this a control rather than a cosmetic change.

        A slot id is a small integer. Hiding the card while `/booking/<id>/`
        still renders would leave the listing bookable by anyone with the URL
        — including the patient who bookmarked it before the withdrawal, who
        is the likeliest person to try.
        """
        slot = self.make_slot()
        self.withdraw()

        response = self.client.get(reverse("booking_form", args=[slot.id]))

        self.assertRedirects(response, reverse("search_results"))
        self.assertIn(
            DOCTOR_UNAVAILABLE,
            [str(message) for message in get_messages(response.wsgi_request)],
        )

    def test_posting_to_a_withdrawn_doctors_slot_books_nothing(self):
        """A hidden control that still accepts a POST is `delete_item` again
        (plan 1.4): the page is not the enforcement, the view is."""
        slot = self.make_slot()
        self.withdraw()

        self.client.post(
            reverse("booking_form", args=[slot.id]),
            {
                "fullName": "Asha Mbwana", "birthDate": "1990-05-01",
                "gender": Appointment.Gender.FEMALE, "email": "asha@example.com",
                "phone": "0712345678",
                "visitType": Appointment.VisitType.NEW_PATIENT,
                "reasonVisit": Appointment.ReasonForVisit.LAB_TEST,
            },
        )

        slot.refresh_from_db()
        self.assertFalse(Appointment.objects.filter(slot=slot).exists())
        self.assertFalse(slot.is_booked)

    def test_an_inactive_doctor_is_refused_by_the_same_rule(self):
        """`is_active` and `is_approved` are independent gates (design §4.2
        point 5), and both belong in `PUBLICLY_BOOKABLE`. Tested because the
        rule is a dict now — dropping a key from it is a one-character change
        that no other test would notice."""
        slot = self.make_slot()
        CustomUser.objects.filter(pk=self.doctor.pk).update(is_active=False)

        self.assertNotContains(
            self.client.get(reverse("search_results")), "Anna Magembe"
        )
        self.assertRedirects(
            self.client.get(reverse("booking_form", args=[slot.id])),
            reverse("search_results"),
        )


class JourneyTests(TestCase):
    """The four rounds, computed once (`appointments.journey`).

    The booking form used to draw a three-step bar - Select Time, Patient Info,
    Confirmation - for a flow with four rounds, ending two rounds early. A
    patient who reached the next page therefore read "Request sent" as a
    confirmation.
    """

    def states(self, status):
        appointment = Appointment(status=status)
        return [stage["state"] for stage in journey_for(appointment)["stages"]]

    def test_the_strip_follows_the_state_machine(self):
        """Two rounds since plan 12.1, not four: the patient asks with
        everything on it, the doctor answers once. The legacy pair reads as
        "waiting for the doctor", which is what they mean to a patient
        whichever flow their booking started under."""
        expected = {
            Appointment.Status.PENDING_TIME: ["done", "now"],
            Appointment.Status.PENDING_MODE: ["done", "now"],
            Appointment.Status.MODE_PROPOSED: ["done", "now"],
            Appointment.Status.CONFIRMED: ["done", "done"],
            Appointment.Status.COMPLETED: ["done", "done"],
        }
        for status, states in expected.items():
            with self.subTest(status=status):
                self.assertEqual(self.states(status), states)

    def test_every_status_is_accounted_for(self):
        """A status added to the machine and forgotten here must not render a
        half-drawn strip that looks deliberate."""
        for status in Appointment.Status.values:
            with self.subTest(status=status):
                journey = journey_for(Appointment(status=status))
                self.assertEqual(len(journey["stages"]), 2)

    def test_an_ended_appointment_gets_a_sentence_not_a_strip(self):
        for status in (
            Appointment.Status.CANCELLED,
            Appointment.Status.EXPIRED,
            Appointment.Status.TIME_DECLINED,
            Appointment.Status.NO_SHOW,
        ):
            with self.subTest(status=status):
                self.assertIsNotNone(journey_for(Appointment(status=status))["ended"])

    def test_it_is_never_the_patients_turn_once_they_have_booked(self):
        """The point of the single confirmation (plan 12.1). The patient's one
        action is the booking form; after that there is nothing to come back
        for, only a wait or a cancellation. The key stays and answers "no",
        because four templates read it and a key that disappeared would be a
        silent `False` in every one of them."""
        turns = {
            status: journey_for(Appointment(status=status))["waiting_on_you"]
            for status in Appointment.Status.values
        }

        self.assertEqual({status for status, mine in turns.items() if mine}, set())

    def test_the_form_shows_round_one_in_progress(self):
        """There is no appointment yet on the booking form: the patient is
        filling round 1 in, so it is neither done nor ahead."""
        stages = journey_before_booking()["stages"]

        self.assertEqual(stages[0]["state"], "now")
        self.assertEqual([stage["state"] for stage in stages[1:]], ["ahead"])


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class PatientChromeTests(FlowTestCase):
    """One chrome for the seven patient pages (plan 11.11).

    Before `patient_base.html` there were seven standalone documents, each with
    its own <head>, its own ~500-line stylesheet, its own navbar and its own
    footer - roughly 3,000 of the flow's 5,197 lines being the same CSS pasted
    six times. It had already drifted: the choose-mode page never got a navbar
    at all.
    """

    def pages(self):
        """(name, url, expected title) for every page in the flow."""
        appointment = self.make_appointment(status=Appointment.Status.PENDING_TIME)
        self.verify(appointment)
        slot = self.make_slot(days=4)
        return (
            ("home", reverse("appointment_home"), "Book an appointment"),
            ("search", reverse("search_results"), "Find a doctor and a time"),
            ("form", reverse("booking_form", args=[slot.id]), "Complete your booking"),
            ("lookup", reverse("booking_id_lookup"), "Find my appointment"),
            (
                "detail",
                reverse("appointment_detail", args=[appointment.bookingID]),
                "My appointment",
            ),
            (
                "success",
                reverse("booking_success_page", args=[appointment.bookingID]),
                "Request sent",
            ),
            (
                "doctor",
                reverse("doctor_detail", args=[self.doctor.id]),
                "Dr. %s" % self.doctor.get_full_name(),
            ),
        )

    def test_every_page_wears_the_same_chrome(self):
        for name, url, _ in self.pages():
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()

                self.assertIn('class="pt-header"', html)
                self.assertIn('class="pt-footer"', html)
                self.assertIn(reverse("booking_id_lookup"), html)

    def test_every_page_titles_itself(self):
        for name, url, title in self.pages():
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()

                self.assertIn(
                    "<title>%s · AGKI Medical Laboratory</title>" % escape(title), html
                )

    def test_no_page_still_says_medibook(self):
        """Six of the seven titles, six navbars and two footer copyright lines
        named a product AGKI has nothing to do with."""
        for name, url, _ in self.pages():
            with self.subTest(page=name):
                self.assertNotIn("MediBook", self.client.get(url).content.decode())

    def test_the_header_does_not_send_a_patient_out_of_the_flow(self):
        """A "Contact" item pointed at the main site's contact section, so a
        patient halfway through booking could click it and land on a marketing
        page with the browser's back button as the only way in again (AGKI,
        2026-09-02).

        Scoped to the header on purpose: the footer links to the main site and
        should, and the brand goes to the flow's own front door.

        Asserted on each `href` rather than on `reverse("home")`: that is "/",
        a substring of every URL on the site, so searching for it marks the
        booking links themselves as escapes.
        """
        for name, url, _ in self.pages():
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()
                nav = html.split("</header>", 1)[0].split("<nav", 1)[1]

                for href in re.findall(r'href="([^"]*)"', nav):
                    with self.subTest(href=href):
                        self.assertTrue(
                            href.startswith("/appointments/"),
                            "%s leaves the booking flow" % href,
                        )

    def test_the_header_still_offers_what_this_section_does(self):
        html = self.client.get(reverse("appointment_home")).content.decode()
        header = html.split("</header>", 1)[0]

        self.assertIn(reverse("search_results"), header)
        self.assertIn(reverse("booking_id_lookup"), header)

    def test_the_footer_carries_a_real_phone_number(self):
        """Nine of its links were `#` and the tenth pointed patients at the
        staff console."""
        site = SiteSettings.load()
        site.phone = "0700 123 456"
        site.save()

        html = self.client.get(reverse("appointment_home")).content.decode()

        self.assertIn("0700 123 456", html)
        self.assertNotIn('href="#"', html)

    def test_keyboard_users_can_get_past_the_navigation(self):
        html = self.client.get(reverse("appointment_home")).content.decode()

        self.assertIn('class="pt-skip" href="#pt-content"', html)
        self.assertIn('id="pt-content"', html)

    def test_every_class_the_flow_uses_is_styled(self):
        """The discipline each console page is held to (plan 11.1 onward): a
        class in the markup that no rule defines is either a typo or a rule
        that was deleted from under it, and both look like working code.

        Font Awesome's own classes are excluded - they are defined in the CDN
        stylesheet, which is the one thing on these pages that is not local.
        """
        for name, url, _ in self.pages():
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()
                style = chr(10).join(re.findall(r"<style>(.*?)</style>", html, re.S))
                defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", style))

                used = set()
                for attr in re.findall(r'class="([^"]*)"', html):
                    used.update(attr.split())
                used = {name for name in used if not name.startswith(("fa-", "fas", "far", "fab"))}

                self.assertEqual(sorted(used - defined), [])

    def test_times_read_the_same_way_everywhere(self):
        """The detail page printed 24-hour times while the search and booking
        pages printed AM/PM - about the same appointment. (Choose-mode was the
        third page in this list until plan 12.1 deleted it.)"""
        appointment = self.make_appointment(
            slot=self.make_slot(days=3, hour=14, minute=30),
            status=Appointment.Status.PENDING_TIME,
        )
        self.verify(appointment)

        for name, url in (
            ("detail", reverse("appointment_detail", args=[appointment.bookingID])),
            ("success", reverse("booking_success_page", args=[appointment.bookingID])),
        ):
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()
                self.assertIn("2:30 PM", html)
                self.assertNotIn("14:30", html)


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class TrackerOnThePagesTests(FlowTestCase):
    """The strip itself, where a patient meets it."""

    def show(self, appointment):
        self.verify(appointment)
        return self.client.get(
            reverse("appointment_detail", args=[appointment.bookingID])
        ).content.decode()

    def test_it_says_who_is_being_waited_on(self):
        """Always the doctor, once a booking exists (plan 12.1). There is no
        state left in which the patient owes an answer."""
        waiting = self.show(self.make_appointment())

        self.assertIn("waiting for the doctor", waiting)
        self.assertNotIn("your turn", waiting)

    def test_a_cancelled_booking_gets_a_sentence_instead(self):
        html = self.show(self.make_appointment(status=Appointment.Status.CANCELLED))

        self.assertIn("pt-tracker-ended", html)
        self.assertNotIn('<ol class="pt-tracker"', html)

    def test_the_booking_form_shows_the_same_two_rounds(self):
        html = self.client.get(
            reverse("booking_form", args=[self.make_slot(days=4).id])
        ).content.decode()

        self.assertIn("pt-tracker", html)
        self.assertIn("The doctor confirms", html)
        # The bar this replaced said "Select Time / Patient Info /
        # Confirmation" - three steps for a flow that had four rounds, and now
        # has two.
        self.assertNotIn("Patient Info", html)


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class BookingFormHonestyTests(FlowTestCase):
    """What the form stopped asking for, at AGKI's decision (plan 11.11)."""

    def rendered(self):
        return self.client.get(
            reverse("booking_form", args=[self.make_slot(days=4).id])
        ).content.decode()

    def test_it_no_longer_asks_for_insurance_it_cannot_store(self):
        """Provider, member ID, group number and the card upload had no `name`
        attribute at all, so the browser never sent them; `hasInsurance` posted
        and was discarded. Nothing in `Appointment` stores any of it."""
        html = self.rendered()

        for field in ("hasInsurance", "insuranceProvider", "memberId", "groupNumber"):
            with self.subTest(field=field):
                self.assertNotIn(field, html)

    def test_it_does_not_demand_agreement_to_documents_that_do_not_exist(self):
        html = self.rendered()

        self.assertNotIn("agreeTerms", html)
        self.assertNotIn("Terms of Service", html)

    def test_every_link_on_it_goes_somewhere(self):
        self.assertNotIn('href="#"', self.rendered())

    def test_it_does_not_assert_a_visit_type_the_patient_has_not_chosen(self):
        """The summary stated "In-Person Consultation" as a fact when nobody
        had been asked. The patient is asked on this very form now (plan 12.1),
        so the summary follows the answer instead of inventing one."""
        html = self.rendered()

        self.assertNotIn("In-Person Consultation", html)
        # It said "Whichever you choose above" for a day, which was true and
        # useless. The summary shows the doctor's side: the ways this time can
        # be worked, which is what the choices above are drawn from (AGKI,
        # 2026-09-03).
        self.assertIn("Preferred meeting modes", html)
        self.assertIn('class="bf-mode"', html)

    def test_the_summary_reads_the_specialties_the_doctor_actually_picked(self):
        """It read `doctor.expertise`, the free-text field plan 11.6 stopped
        writing to - so a doctor who picked from the new list showed a blank
        line. The same bug 11.7 fixed on the search page, by another route."""
        specialty = Specialty.objects.create(name="Test Nephrology", slug="test-nephrology")
        profile, _ = Profile.objects.get_or_create(user=self.doctor)
        profile.specialties.add(specialty)
        profile.expertise = "SHOULD NOT BE SHOWN"
        profile.save()

        html = self.rendered()

        self.assertIn("Test Nephrology", html)
        self.assertNotIn("SHOULD NOT BE SHOWN", html)


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class BookingSuccessAddressTests(FlowTestCase):
    """The worst bug on the patient side (plan 11.11)."""

    def test_it_names_the_patients_own_email_not_the_doctors(self):
        """Under "Confirmation Sent To" this printed
        `appointment.slot.user.email` - the doctor's address. It told the
        patient to watch the wrong inbox, and published a staff email address
        to anybody who booked."""
        appointment = self.make_appointment(email="patient@example.com")
        self.verify(appointment)

        html = self.client.get(
            reverse("booking_success_page", args=[appointment.bookingID])
        ).content.decode()

        self.assertIn("patient@example.com", html)
        self.assertNotIn(self.doctor.email, html)


class AppointmentHomeTests(FlowTestCase):
    """The front door, rebuilt from the database (plan 11.11).

    What it replaced was 737 lines of static marketing for a product called
    MediBook, rendered by a view that passed **no context at all** - so nothing
    on it came from AGKI's database and nothing on it could be true by
    accident.
    """

    def get(self):
        return self.client.get(reverse("appointment_home"))

    def test_it_no_longer_advertises_what_agki_does_not_do(self):
        html = self.get().content.decode()

        for promise in (
            "Digital Prescriptions",
            "Secure Payments",
            "Easy Rescheduling",
            "24/7 Booking",
            "Smart Reminders",
            "Video Consultations",
        ):
            with self.subTest(promise=promise):
                self.assertNotIn(promise, html)

    def test_the_specialties_are_the_real_ones(self):
        """Eight hardcoded emoji tiles - Dentist, Ophthalmologist,
        Cardiologist - linking nowhere and matching nothing in the database."""
        specialty = Specialty.objects.create(name="Test Virology", slug="test-virology")
        Profile.objects.get_or_create(user=self.doctor)[0].specialties.add(specialty)
        self.make_slot(days=3)

        html = self.get().content.decode()

        self.assertIn("Test Virology", html)
        self.assertNotIn("Ophthalmologist", html)

    def test_a_specialty_nobody_can_be_booked_in_is_not_a_link(self):
        """The same rule the results page follows: a specialty with nobody
        bookable behind it is shown as unavailable rather than as a link to an
        empty page (design §5.5)."""
        empty = Specialty.objects.create(name="Test Oncology", slug="test-oncology")

        html = self.get().content.decode()

        self.assertIn("Test Oncology", html)
        self.assertNotIn("?specialty=%s" % empty.slug, html)

    def test_it_counts_only_doctors_who_can_actually_be_booked(self):
        specialty = Specialty.objects.create(name="Test Urology", slug="test-urology")
        Profile.objects.get_or_create(user=self.doctor)[0].specialties.add(specialty)
        self.make_slot(days=3)

        self.assertContains(self.get(), "1 available")

    def test_its_search_submits_to_the_list(self):
        """The search box on the page this replaces was commented out; the one
        visible control was a link with a magnifying glass drawn on it."""
        self.assertContains(
            self.get(), 'action="%s"' % reverse("search_results")
        )

    def test_it_says_what_the_two_steps_are(self):
        """A patient who is told they are on the last step reads the next
        page's "Request sent" as a confirmation. Two steps since plan 12.1,
        and the page has to say which."""
        html = self.get().content.decode()

        self.assertIn("You ask", html)
        self.assertIn("The doctor answers once", html)
        self.assertNotIn("You choose how to meet", html)

    def test_the_site_header_sends_patients_to_booking_not_to_the_staff_login(self):
        """The one persistent button on the public site said "Explore
        platform" and pointed at `signin`."""
        html = self.client.get(reverse("home")).content.decode()
        header = html.split("</nav>", 1)[0]

        self.assertIn('href="%s" class="nav-appointment"' % reverse("appointment_home"), header)
        self.assertNotIn(reverse("signin"), header)

    def test_clinicians_keep_their_way_in_from_the_hero(self):
        """AGKI, on 11.11's own work: the header button had to become the
        patient's, but Explore platform is how a doctor reaches their console
        and dropping it to a nav item buried it. It sits with the hero CTAs."""
        html = self.client.get(reverse("home")).content.decode()
        hero = html.split('class="hero-actions"', 1)[1].split("</div>", 1)[0]

        self.assertIn("Explore platform", hero)
        self.assertIn(reverse("signin"), hero)


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class SelectControlTests(FlowTestCase):
    """The dropdowns are the page's, not the operating system's (plan 11.14).

    Every other control in this flow is drawn by the flow's own stylesheet.
    The selects were not: they rendered the native widget, grey arrow and all,
    and the specialty picker is the first control a patient touches.
    """

    def stylesheet(self, url):
        """The page's CSS, **with comments stripped**.

        Both halves of that matter. An earlier version of these tests read the
        raw stylesheet and asserted on `%2364706a`, which appears in the
        comment above the rule explaining what it is - so deleting the colour
        from the rule left the test passing on the strength of the prose
        describing it. The same trap as the console's dead-button test in plan
        5.x, arriving by a different route.
        """
        html = self.client.get(url).content.decode()
        css = chr(10).join(re.findall(r"<style>(.*?)</style>", html, re.S))
        return re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    def declarations(self, css, selector):
        """One rule's declarations, as {property: value}.

        Parsed rather than substring-matched because `appearance` is a
        substring of `-webkit-appearance`: removing the real declaration and
        leaving the two vendor-prefixed ones behind passed a naive `assertIn`,
        while a browser drew the native widget again.
        """
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        if not match:
            return {}
        found = {}
        for piece in match.group(1).split(";"):
            if ":" not in piece:
                continue
            name, _, value = piece.partition(":")
            found[name.strip()] = value.strip()
        return found

    def test_the_native_widget_is_replaced_rather_than_left_showing(self):
        rule = self.declarations(self.stylesheet(reverse("appointment_home")), ".pt-select")

        self.assertEqual(rule.get("appearance"), "none")
        # Taking the widget away takes its arrow with it, so the page must
        # draw one or the control loses the only sign that it opens.
        self.assertIn("background-image", rule)

    def test_the_chevron_is_drawn_in_the_flow_s_own_colours(self):
        """A data URI, so it costs no request and takes its stroke from this
        palette. `%2364706a` is --dim, URL-encoded because `#` ends a data
        URI - the kind of thing that silently renders nothing if it is
        wrong."""
        rule = self.declarations(self.stylesheet(reverse("appointment_home")), ".pt-select")
        chevron = rule.get("background-image", "")

        self.assertIn("data:image/svg+xml", chevron)
        self.assertIn("%2364706a", chevron)

    def script(self, url):
        html = self.client.get(url).content.decode()
        return chr(10).join(re.findall(r"<script>(.*?)</script>", html, re.S))

    def test_the_list_is_the_page_s_own_and_not_the_browser_s(self):
        """11.14 styled the closed control, which looks the same everywhere.
        The open list is drawn by the browser: Firefox honours
        `option { background; color }` and Chrome ignores them, so one browser
        looked right and the other did not (AGKI, 2026-09-03)."""
        code = self.script(reverse("appointment_home"))

        self.assertIn("role', 'listbox'", code)
        self.assertIn("pt-combo-list", code)

    def test_every_class_the_script_builds_is_styled(self):
        """The class-discipline rule this flow is held to, extended to markup
        that does not exist until the script runs - which is exactly the markup
        no page-level scan would catch."""
        html = self.client.get(reverse("booking_form", args=[self.make_slot(days=4).id])).content.decode()
        css = chr(10).join(re.findall(r"<style>(.*?)</style>", html, re.S))
        code = chr(10).join(re.findall(r"<script>(.*?)</script>", html, re.S))
        defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))

        built = set(re.findall(r"className = '([^']+)'", code))
        built |= set(re.findall(r"className \+= ' ([^']+)'", code))
        built |= set(re.findall(r"classList\.(?:add|toggle|remove)\('([^']+)'", code))
        names = {name for entry in built for name in entry.split()}

        self.assertTrue(names, "the script builds no classes at all")
        self.assertEqual(sorted(names - defined), [])

    def test_the_select_itself_survives_the_enhancement(self):
        """It is still the element that posts, still carries the model's
        choices, and is still what a browser with JavaScript off renders. The
        listbox is built on top of it, never instead of it."""
        html = self.client.get(
            reverse("booking_form", args=[self.make_slot(days=4).id])
        ).content.decode()

        for field in ("gender", "reasonVisit"):
            with self.subTest(field=field):
                self.assertIn('name="%s"' % field, html)
                self.assertRegex(html, r'<select[^>]*name="%s"' % field)

    def test_the_hidden_select_stays_focusable(self):
        """`display: none` on a `required` select makes Chrome refuse to submit
        the form - "an invalid form control is not focusable" - because it
        cannot show the problem on a field that is not there. Zero opacity
        keeps the box, so the validation bubble points at the right control."""
        html = self.client.get(reverse("appointment_home")).content.decode()
        css = chr(10).join(re.findall(r"<style>(.*?)</style>", html, re.S))
        rule = re.search(r"\.pt-combo-native \{([^}]*)\}", css).group(1)

        self.assertIn("opacity: 0", rule)
        self.assertNotIn("display: none", rule)

    def test_the_choice_is_written_back_to_the_select(self):
        """Everything downstream - the form, Django, any listener - reads the
        select. A picker that only updated its own button would look right and
        post the wrong thing."""
        code = self.script(reverse("appointment_home"))

        self.assertIn("select.selectedIndex = index", code)
        self.assertIn("new Event('change'", code)

    def test_the_specialty_picker_uses_it(self):
        """Both pages that offer the choice, not just the one that was looked
        at."""
        for name, url in (
            ("front door", reverse("appointment_home")),
            ("doctor list", reverse("search_results")),
        ):
            with self.subTest(page=name):
                html = self.client.get(url).content.decode()
                picker = re.search(
                    r'<select[^>]*name="specialty"[^>]*>', html
                ).group(0)

                self.assertIn("pt-select", picker)

    def test_a_long_name_cannot_run_under_the_chevron(self):
        rule = self.declarations(self.stylesheet(reverse("appointment_home")), ".pt-select")

        self.assertIn("padding-right", rule)


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class DoctorPageTests(FlowTestCase):
    """The page the times moved to (plan 11.15).

    The four "is this slot offered" rules below were asserted against the
    results page until this phase. They are the same rules; this is now where
    they show.
    """

    def page(self, doctor=None, **params):
        return self.client.get(
            reverse("doctor_detail", args=[(doctor or self.doctor).id]), params
        )

    def shown(self, doctor=None, **params):
        """The page with every `<template>` removed - what a browser shows
        before any script runs.

        Since plan 11.17 each day carries its own times in a `<template>` so
        the drawer can open without a round trip, which means every day's
        booking links are in the HTML. Asserting on the raw body would say a
        day is offered when it is only pre-loaded, so these tests read what is
        actually rendered.
        """
        html = self.page(doctor, **params).content.decode()
        return re.sub(r"<template.*?</template>", "", html, flags=re.S)

    # ---- the offering rules, moved from SearchResultsTests ----------------

    def test_slots_beyond_the_old_five_day_window_are_offered(self):
        """The horizon was hard-coded to five days, so nothing past the day
        after tomorrow could be booked however far ahead doctors published."""
        slot = self.make_slot(days=20)

        self.assertContains(self.page(), reverse("booking_form", args=[slot.id]))

    def test_slots_beyond_the_configured_horizon_are_not(self):
        beyond = self.make_slot(days=self.settings_row.booking_horizon_days + 5)

        self.assertNotContains(self.page(), reverse("booking_form", args=[beyond.id]))

    def test_a_slot_inside_the_booking_notice_is_not_offered(self):
        """A slot the system will not accept must not be presented as bookable
        (design 6.1) - the patient should never pick one only to be refused."""
        soon = timezone.localtime() + timedelta(minutes=45)
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=soon.date(), time=soon.time(),
        )

        self.assertNotContains(self.page(), reverse("booking_form", args=[slot.id]))

    def test_a_booked_slot_is_not_offered(self):
        appointment = self.make_appointment()

        self.assertNotContains(
            self.page(), reverse("booking_form", args=[appointment.slot.id])
        )

    # ---- a day at a time --------------------------------------------------

    def test_arriving_opens_nothing(self):
        """A modal over a calendar the patient has not looked at yet is the
        pop-up nobody asked for. The panel this replaced could default to the
        first day because a fold-out under the calendar is just more page
        (plan 11.18)."""
        self.make_slot(days=3, hour=9)

        response = self.page()

        self.assertIsNone(response.context["selected_date"])
        self.assertNotContains(response, 'class="dd-overlay show"')

    def test_choosing_a_day_opens_it(self):
        slot = self.make_slot(days=3, hour=9)

        response = self.page(date=slot.date.strftime("%Y-%m-%d"))

        self.assertEqual(response.context["selected_date"], slot.date)
        self.assertContains(response, 'class="dd-overlay show"')

    def test_choosing_a_day_shows_that_day_and_not_another(self):
        first = self.make_slot(days=3, hour=9)
        second = self.make_slot(days=8, hour=9)

        shown = self.shown(date=second.date.strftime("%Y-%m-%d"))

        self.assertIn(reverse("booking_form", args=[second.id]), shown)
        self.assertNotIn(reverse("booking_form", args=[first.id]), shown)

    def test_every_day_carries_its_own_times_for_the_modal(self):
        """The arrangement the console uses: the day's detail travels with the
        day, so opening it needs no second query and the modal cannot show
        something the calendar does not."""
        first = self.make_slot(days=3, hour=9)
        later = self.make_slot(days=8, hour=9)

        body = self.page().content.decode()

        # Both days are pre-loaded in the markup...
        self.assertIn(reverse("booking_form", args=[first.id]), body)
        self.assertIn(reverse("booking_form", args=[later.id]), body)
        # ...and neither is rendered until one is opened.
        self.assertNotIn(reverse("booking_form", args=[first.id]), self.shown())
        self.assertIn(
            reverse("booking_form", args=[first.id]),
            self.shown(date=first.date.strftime("%Y-%m-%d")),
        )

    def test_the_calendar_links_every_day_that_has_something(self):
        wanted_days = [3, 5, 9]
        for day in wanted_days:
            self.make_slot(days=day)

        response = self.page()

        for day in wanted_days:
            wanted = timezone.localdate() + timedelta(days=day)
            with self.subTest(day=wanted):
                # `date=`, not `?date=`: the link carries the period it was
                # drawn in, so the day is the third parameter rather than the
                # first (plan 11.17).
                self.assertContains(response, "date=%s" % wanted.strftime("%Y-%m-%d"))

    def test_a_day_with_nothing_on_it_is_not_a_link(self):
        """Thirty clickable days, twenty-nine of which go nowhere, is what the
        old front door's specialty tiles were (plan 11.11)."""
        self.make_slot(days=3)
        empty = timezone.localdate() + timedelta(days=4)

        self.assertNotContains(
            self.page(), "date=%s" % empty.strftime("%Y-%m-%d")
        )

    def test_the_grid_is_padded_to_whole_weeks(self):
        """A month drawn as a ragged list is not a month. The console's
        availability page pads the same way (plan 11.1), which is the layout
        AGKI asked this page to share."""
        weeks = self.page().context["weeks"]

        self.assertTrue(weeks)
        for index, week in enumerate(weeks):
            with self.subTest(week=index):
                self.assertEqual(len(week), 7)
        # Monday first, so the day-of-week headings line up with the cells.
        self.assertEqual(weeks[0][0]["date"].weekday(), 0)

    def test_the_grid_covers_the_whole_horizon_with_nothing_to_navigate(self):
        """Thirty days is five or six rows. Drawing a month at a time would
        need a second parameter saying which month is on screen, separate from
        the day whose times are listed below it."""
        weeks = self.page().context["weeks"]
        dates = [day["date"] for week in weeks for day in week]
        today = timezone.localdate()
        horizon = today + timedelta(days=self.settings_row.booking_horizon_days)

        self.assertLessEqual(min(dates), today)
        self.assertGreaterEqual(max(dates), horizon)

    def test_days_outside_the_horizon_are_marked_as_such(self):
        """They are drawn because a week has seven days, not because they are
        offers."""
        weeks = self.page().context["weeks"]
        today = timezone.localdate()

        outside = [
            day for week in weeks for day in week
            if not day["in_period"]
        ]

        self.assertTrue(outside)
        for day in outside:
            with self.subTest(date=day["date"]):
                self.assertEqual(day["count"], 0)

    def test_a_nonsense_date_leaves_it_shut_rather_than_500ing(self):
        """It arrives from history, from a bookmark taken weeks ago, and from
        anybody who types one."""
        self.make_slot(days=3)

        for bad in ("yesterday", "2026-13-45", "", "2026-09-04T10:00"):
            with self.subTest(date=bad):
                response = self.page(date=bad)

                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context["selected_date"])

    def test_a_real_date_with_nothing_on_it_opens_nothing(self):
        self.make_slot(days=3)
        empty = timezone.localdate() + timedelta(days=4)

        response = self.page(date=empty.strftime("%Y-%m-%d"))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["selected_date"])

    def test_a_time_is_offered_as_a_row_with_an_action(self):
        """The console's modal format, at AGKI's instruction (11.18). It
        replaces morning/afternoon/evening chip groups listed under the
        calendar - one day's times, rarely more than a handful of rows, which
        do not need dividing into thirds."""
        slot = self.make_slot(days=3, hour=9)

        shown = self.shown(date=slot.date.strftime("%Y-%m-%d"))

        self.assertIn('class="dd-row"', shown)
        self.assertIn("Book this time", shown)
        self.assertNotIn("Morning", shown)

    def test_the_times_open_over_the_page_rather_than_below_it(self):
        """A fold-out under a month view put the times below five rows of
        calendar, so choosing a day scrolled the thing you had just clicked off
        the top of the screen."""
        slot = self.make_slot(days=3, hour=9)

        shown = self.shown(date=slot.date.strftime("%Y-%m-%d"))

        self.assertIn('class="dd-sheet"', shown)
        self.assertIn('role="dialog"', shown)
        self.assertIn('aria-modal="true"', shown)

    def test_a_period_with_nothing_in_it_says_so(self):
        """A patient who steps forward to an empty month should be told, rather
        than left looking at a grid of grey cells.

        `period_has_times` is a separate question from whether a day is open:
        arriving opens nothing, so keying this off the selection would put
        "nothing to book here" on every arrival."""
        self.make_slot(days=3)
        far = (timezone.localdate() + timedelta(days=120)).strftime("%Y-%m-%d")

        empty = self.page(view="month", start=far)
        here = self.page()

        self.assertFalse(empty.context["period_has_times"])
        self.assertContains(empty, "Nothing to book in")
        self.assertTrue(here.context["period_has_times"])
        self.assertNotContains(here, "Nothing to book in")

    def test_a_doctor_with_nothing_free_gets_a_page_that_says_so(self):
        response = self.page()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No times published")

    # ---- who they are -----------------------------------------------------

    def test_the_bio_finally_has_somewhere_to_be_read(self):
        """A doctor writes it on their own profile page, and until this page
        existed nobody outside the console ever saw it."""
        Profile.objects.filter(user=self.doctor).update(
            bio="Twenty years in tropical medicine."
        )

        self.assertContains(self.page(), "Twenty years in tropical medicine.")

    def test_a_withdrawn_doctor_has_no_page(self):
        """The same rule, from the same place, as the booking form's guard
        (10.3): a page describing a withdrawn doctor and offering their times
        would undo the one lever AGKI has against a fraudulent listing."""
        self.make_slot(days=3)
        CustomUser.objects.filter(pk=self.doctor.pk).update(is_approved=False)

        response = self.page()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("search_results"))

    def test_somebody_who_is_not_a_doctor_has_no_page_either(self):
        ledger = make_staff_user(username="ledger-2", email="ledger-2@example.com")

        self.assertEqual(self.page(doctor=ledger).status_code, 404)


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class DoctorPagePeriodTests(FlowTestCase):
    """Week and month, navigable — the console's arrangement (plan 11.17).

    The switch and the arrows are ordinary links, so the browser's back button
    works and a week can be bookmarked or sent to somebody. The console's page
    learned that in plan 11.1, where prev/next were `onclick` handlers calling
    functions that did not exist.
    """

    def page(self, **params):
        return self.client.get(
            reverse("doctor_detail", args=[self.doctor.id]), params
        )

    def test_it_opens_on_the_month(self):
        """The console defaults to the week: a doctor publishing times works a
        week at a time. A patient is choosing among the next thirty days and
        wants to see them at once."""
        self.assertEqual(self.page().context["view_mode"], "month")

    def test_the_week_view_covers_seven_days(self):
        """Asserted on the days marked `in_period`, not on the row length:
        `weeks_between` pads every period out to whole Monday-Sunday weeks, so
        a row is seven cells long whatever the period is. Reading the row was
        testing the padding - a mutation shortening the week to Monday-Friday
        passed."""
        response = self.page(view="week")
        weeks = response.context["weeks"]

        inside = [day for week in weeks for day in week if day["in_period"]]

        self.assertEqual(response.context["view_mode"], "week")
        self.assertEqual(len(weeks), 1)
        self.assertEqual(len(inside), 7)
        self.assertEqual(inside[0]["date"].weekday(), 0)
        self.assertEqual(inside[-1]["date"].weekday(), 6)

    def test_the_month_view_covers_the_whole_month(self):
        today = timezone.localdate()

        weeks = self.page(view="month").context["weeks"]
        inside = [day for week in weeks for day in week if day["in_period"]]

        self.assertEqual(inside[0]["date"].day, 1)
        self.assertEqual(inside[0]["date"].month, today.month)
        self.assertEqual(inside[-1]["date"].month, today.month)

    def test_the_arrows_move_the_period(self):
        response = self.page(view="week")
        start = response.context["period_start"]

        self.assertEqual(
            response.context["next_start"] - response.context["prev_start"],
            timedelta(days=14),
        )
        forward = self.page(view="week", start=response.context["next_start"])
        self.assertEqual(forward.context["period_start"], start + timedelta(days=7))

    def test_month_navigation_crosses_the_year_boundary(self):
        """December's `next` is January of the following year, which naive
        month arithmetic gets wrong."""
        response = self.page(view="month", start="2026-12-14")

        self.assertEqual(response.context["next_start"].year, 2027)
        self.assertEqual(response.context["next_start"].month, 1)

    def test_a_nonsense_start_falls_back_to_today(self):
        for bad in ("last-week", "2026-13-01", "", "not a date"):
            with self.subTest(start=bad):
                response = self.page(view="month", start=bad)

                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["is_current_period"])

    def test_an_unknown_view_falls_back_rather_than_rendering_nothing(self):
        self.assertEqual(self.page(view="decade").context["view_mode"], "month")

    def test_today_is_offered_only_when_you_have_left_it(self):
        # A doctor with nothing published gets the empty state rather than the
        # navigation, so this needs a real time to navigate around.
        self.make_slot(days=3)

        here = self.page().content.decode()
        away = self.page(view="month", start="2027-03-01").content.decode()

        self.assertNotIn(">Today<", here)
        self.assertIn(">Today<", away)

    def test_both_views_count_the_times_rather_than_listing_them(self):
        """The week view listed the times themselves, copied from the console -
        where it is right, because a doctor scanning their own week is reading
        which times they published. A patient is not: the times open in the
        modal, so printing them in the cell as well said the same thing twice
        (AGKI, 2026-09-02)."""
        self.make_slot(days=1, hour=9)
        self.make_slot(days=1, hour=11)

        week = self.shown_body(view="week", start=timezone.localdate())
        month = self.shown_body(view="month")

        for name, body in (("week", week), ("month", month)):
            with self.subTest(view=name):
                self.assertIn('class="dd-count"', body)
                self.assertNotIn('class="dd-chip"', body)

        # The count is the number of times, in both.
        self.assertIn('class="dd-count">2<', " ".join(week.split()))

    def shown_body(self, **params):
        return re.sub(
            r"<template.*?</template>", "", self.page(**params).content.decode(),
            flags=re.S,
        )


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class DoctorPageModalOpeningTests(FlowTestCase):
    """A modal is the result of a click, and only of a click (plan 11.19)."""

    def page(self, **params):
        return self.client.get(
            reverse("doctor_detail", args=[self.doctor.id]), params
        )

    def script(self):
        html = self.page().content.decode()
        return chr(10).join(re.findall(r"<script>(.*?)</script>", html, re.S))

    def test_opening_a_day_does_not_put_it_in_the_address_bar(self):
        """It used to, so `?date=` stayed in the URL and a refresh - or coming
        back through history - reopened the modal over a calendar nobody had
        asked to open. Comments are stripped first: the note explaining why
        this was removed names the method it removed."""
        self.make_slot(days=3)
        code = re.sub(r"//[^\n]*", "", self.script())

        self.assertNotIn("replaceState", code)

    def test_the_server_still_honours_a_day_in_the_url(self):
        """Which is what keeps the page working with JavaScript switched off:
        without the script, choosing a day *is* a navigation."""
        slot = self.make_slot(days=3)

        response = self.page(date=slot.date.strftime("%Y-%m-%d"))

        self.assertContains(response, 'class="dd-overlay show"')

    def test_the_calendar_still_links_the_day(self):
        """The link is the no-JavaScript path and the script's own source for
        which day was clicked. Removing it would take both."""
        slot = self.make_slot(days=3)

        self.assertContains(
            self.page(), "date=%s" % slot.date.strftime("%Y-%m-%d")
        )


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class BookingReferenceTests(FlowTestCase):
    """The reference is five digits, and five digits run out (plan 11.24).

    100,000 values means a 50% chance of at least one collision at about 370
    bookings in a year, by the birthday bound. There was nothing between the
    generator and `unique=True`, so the second patient to draw a number got an
    `IntegrityError` out of `save()` - a 500 on the booking form. It surfaced
    in `TransitionTests`, which creates enough appointments in one run to hit
    it reliably.
    """

    def test_a_taken_reference_is_retried_rather_than_500ing(self):
        first = self.make_appointment()

        with mock.patch(
            "appointments.models.new_booking_reference",
            side_effect=[first.bookingID, first.bookingID, "AGKI-2026-00042"],
        ):
            second = self.make_appointment(slot=self.make_slot(days=4))

        self.assertEqual(second.bookingID, "AGKI-2026-00042")
        self.assertEqual(Appointment.objects.count(), 2)

    def test_it_gives_up_rather_than_looping_for_ever(self):
        first = self.make_appointment()

        with mock.patch(
            "appointments.models.new_booking_reference",
            return_value=first.bookingID,
        ):
            with self.assertRaises(IntegrityError):
                self.make_appointment(slot=self.make_slot(days=4))

    def test_a_double_booked_slot_is_still_refused_immediately(self):
        """The retry must not swallow the other constraint that raises this
        exception. `Appointment.slot` is a OneToOne, and it is the last guard
        against two appointments on one time (design §10 #6) - retrying it
        would turn a refusal into a loop and then into a confusing error about
        references."""
        first = self.make_appointment()

        with mock.patch(
            "appointments.models.new_booking_reference",
            wraps=appointment_models.new_booking_reference,
        ) as reference:
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    self.make_appointment(slot=first.slot)

        # One attempt, not twelve. Asserting only that *an* IntegrityError
        # escapes is not enough: with the guard removed the loop exhausts its
        # attempts and raises one anyway - twelve wasted inserts, and a message
        # about booking references for a problem that is nothing of the kind.
        self.assertEqual(reference.call_count, 1)

    def test_references_are_not_predictable(self):
        """Half of what opens a patient's record is this string (design §8.1).
        `random` is a Mersenne Twister: enough outputs give away the next one.
        """
        source = inspect.getsource(appointment_models)

        self.assertIn("secrets.choice", source)
        self.assertNotIn("random.choices", source)

    def test_the_shape_of_a_reference_is_unchanged(self):
        """Patients read these over the phone and the lookup form shows the
        shape as a placeholder. Fixing the collision was not a licence to
        change what a reference looks like."""
        reference = new_booking_reference()

        self.assertRegex(reference, r"^AGKI-\d{4}-\d{5}$")


class SlotModeTests(TestCase):
    """A published time says how the doctor will work it (plan 12.1).

    The patient picks one of these when they book, so the offer has to be true
    before it is made: a doctor who never travels should not be asked, and a
    patient should not be shown something that was always going to be refused.
    """

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="mode-doctor", email="mode-doctor@example.com",
            role=CustomUser.Role.DOCTOR,
        )

    def slot(self, **flags):
        return AvailableSlot(
            user=self.doctor,
            date=timezone.localdate() + timedelta(days=3),
            time=time(9, 0),
            **flags,
        )

    def test_a_slot_offers_everything_by_default(self):
        """Which is what every time published before this change meant: any of
        the three could be asked for."""
        offered = [value for value, _ in self.slot().offered_modes]

        self.assertEqual(offered, list(Appointment.Mode.values))

    def test_the_offer_follows_the_flags(self):
        slot = self.slot(offers_online=False, offers_provider_travels=False)

        self.assertEqual(
            [value for value, _ in slot.offered_modes],
            [Appointment.Mode.PATIENT_TRAVELS],
        )
        self.assertFalse(slot.offers(Appointment.Mode.ONLINE))
        self.assertTrue(slot.offers(Appointment.Mode.PATIENT_TRAVELS))

    def test_an_unknown_mode_is_never_offered(self):
        self.assertFalse(self.slot().offers("carrier-pigeon"))

    def test_the_labels_come_from_the_model(self):
        """The bulk form has no model to take them from and builds its own
        fields; reading `verbose_name` is what keeps the two forms saying the
        same words."""
        for form in (AvailableSlotForm(), BulkAvailabilityForm()):
            with self.subTest(form=type(form).__name__):
                self.assertEqual(
                    str(form.fields["offers_provider_travels"].label),
                    "I travel to the patient",
                )

    def test_publishing_with_no_way_to_meet_is_refused(self):
        """A time nobody can be seen at is not availability."""
        form = AvailableSlotForm(
            data={
                "date": (timezone.localdate() + timedelta(days=3)).isoformat(),
                "time": "09:00",
            },
            user=self.doctor,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("at least one way you can meet", str(form.errors).lower())

    def test_the_bulk_form_carries_the_modes_onto_every_slot(self):
        monday = timezone.localdate() + timedelta(days=(7 - timezone.localdate().weekday()))
        form = BulkAvailabilityForm(data={
            "start_date": monday.isoformat(),
            "end_date": monday.isoformat(),
            "weekdays": [str(monday.weekday())],
            "times": "09:00,09:30",
            "offers_patient_travels": "1",
        })

        self.assertTrue(form.is_valid(), form.errors)
        form.save_for(self.doctor)

        published = AvailableSlot.objects.filter(user=self.doctor)
        self.assertEqual(published.count(), 2)
        for slot in published:
            with self.subTest(slot=slot):
                self.assertEqual(
                    [value for value, _ in slot.offered_modes],
                    [Appointment.Mode.PATIENT_TRAVELS],
                )

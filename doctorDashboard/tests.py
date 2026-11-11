"""Access control on the staff console (implementation plan Phase 1).

Every test here corresponds to something that was reproducibly exploitable by
an **anonymous** visitor before this phase:

* four dashboard URLs returned a clean 200 to anyone, and the rest returned
  500 — because `AnonymousUser` crashed inside a queryset filter, not because
  anything refused them;
* `delete_item` destroyed an ecommerce product on a plain GET;
* `activate_deactivate_doctor` flipped `is_approved` on any account via an
  unauthenticated POST;
* the shared base template rendered `{{ user.image.url }}` unguarded, so an
  account without a photo 500'd on every page of the console.

The access tests enumerate the URLconf rather than a hand-written list. That
is the point: a view added without an access decorator has to fail something.
"""

from datetime import date, time, timedelta
from unittest import mock

from base64 import b64decode

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.templatetags.static import static
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import escape

import doctorDashboard.urls as dashboard_urls
from appointments.models import (
    Appointment,
    AppointmentTransition,
    AvailableSlot,
    InvalidTransition,
    NotificationLog,
)
from cms.models import SiteSettings
from cms.test_utils import make_staff_user
from ecommerce.models import Category, Item

User = get_user_model()

PASSWORD = "test-pass-12345"

#: A 1x1 PNG. Base64 rather than a byte string of escapes, which do not
#: survive being written through a shell heredoc.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# Stand-in values for captured URL parameters. A new view whose URL captures
# something not listed here raises KeyError in `dashboard_routes()` and fails
# every test in this module — deliberately. Adding a route to the staff
# console should require a decision about who may reach it, and this is where
# that decision gets made.
SAMPLE_URL_ARGS = {
    "appointment_id": 1,
    "doctor_id": 1,
    "item_id": 1,
    "slot_id": 1,
}


def dashboard_routes():
    """(url name, concrete path) for every route in doctorDashboard.urls."""
    routes = []
    for pattern in dashboard_urls.urlpatterns:
        captured = pattern.pattern.regex.groupindex
        kwargs = {name: SAMPLE_URL_ARGS[name] for name in captured}
        routes.append((pattern.name, reverse(pattern.name, kwargs=kwargs)))
    return routes


# Views only a role=admin account may open (design §4.2 point 2).
ADMIN_ONLY = {"doctors", "set_doctor_status", "add_doctor"}
# Stricter than any role (design §4.4).
SUPERUSER_ONLY = {"ecommerce_items"}


class DashboardAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="dash-doctor", email="dash-doctor@example.com",
            password=PASSWORD, role=User.Role.DOCTOR,
        )
        cls.admin = make_staff_user(
            username="dash-admin", email="dash-admin@example.com",
            password=PASSWORD, role=User.Role.ADMIN,
        )
        cls.patient = make_staff_user(
            username="dash-patient", email="dash-patient@example.com",
            password=PASSWORD,
        )
        cls.superuser = User.objects.create_superuser(
            username="dash-root", email="dash-root@example.com", password=PASSWORD,
        )

    def test_the_console_has_routes_to_check(self):
        """Guards the enumeration itself: a `dashboard_routes()` that silently
        returned nothing would make every test below vacuously pass."""
        self.assertGreaterEqual(len(dashboard_routes()), 10)

    def test_every_url_bounces_an_anonymous_visitor_to_sign_in(self):
        for name, path in dashboard_routes():
            with self.subTest(url=name):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302, f"{name} did not redirect")
                self.assertIn("/useraccount/signin/", response["Location"])

    def test_an_account_with_no_role_is_refused_everywhere(self):
        """A signed-in patient gets 403, not a login redirect — no amount of
        signing in will grant this, so bouncing them to a form would loop."""
        self.client.force_login(self.patient)
        for name, path in dashboard_routes():
            with self.subTest(url=name):
                self.assertEqual(self.client.get(path).status_code, 403)

    def test_a_doctor_reaches_the_clinical_pages_but_not_the_admin_ones(self):
        self.client.force_login(self.doctor)
        for name, path in dashboard_routes():
            with self.subTest(url=name):
                status = self.client.get(path).status_code
                if name in ADMIN_ONLY or name in SUPERUSER_ONLY:
                    self.assertEqual(status, 403, f"{name} should be closed to doctors")
                else:
                    self.assertNotEqual(status, 403, f"{name} should be open to doctors")

    def test_an_admin_reaches_the_admin_pages(self):
        self.client.force_login(self.admin)
        for name in ADMIN_ONLY:
            with self.subTest(url=name):
                path = dict(dashboard_routes())[name]
                self.assertNotEqual(self.client.get(path).status_code, 403)

    def test_ecommerce_is_superuser_only_not_admin_only(self):
        """§4.4 is explicit that this is stricter than any role."""
        path = reverse("ecommerce_items")
        for label, user in (("admin", self.admin), ("doctor", self.doctor)):
            with self.subTest(account=label):
                self.client.force_login(user)
                self.assertEqual(self.client.get(path).status_code, 403)

        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(path).status_code, 200)

    def test_a_superuser_is_not_locked_out_by_having_no_role(self):
        """Resolved towards design §4.1 ("is_superuser grants everything").
        A superuser already reads every appointment through /admin/, so
        refusing them the console would cost the accounts that maintain this
        system their access to it and protect nothing."""
        self.client.force_login(self.superuser)
        for name, path in dashboard_routes():
            with self.subTest(url=name):
                self.assertNotEqual(self.client.get(path).status_code, 403)

    def test_the_status_toggle_cannot_be_posted_anonymously(self):
        """Reproduced before Phase 1: is_approved False -> True with no
        session at all. The endpoint was renamed in plan 5.11 when the one
        mislabelled button became two honest ones; the hole it used to have is
        the same hole, so this test follows the rename rather than being
        replaced."""
        target = make_staff_user(username="victim", email="victim@example.com")
        User.objects.filter(pk=target.pk).update(is_approved=False)

        response = self.client.post(
            reverse("set_doctor_status", kwargs={"doctor_id": target.pk}),
            {"flag": "approved", "value": "on"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/useraccount/signin/", response["Location"])
        target.refresh_from_db()
        self.assertFalse(target.is_approved)


class NoDeleteItemRouteTests(TestCase):
    """`delete_item` deleted a product on an anonymous GET (design §10
    dashboard #17). It was removed rather than guarded, so these tests are
    about it staying gone."""

    @classmethod
    def setUpTestData(cls):
        category = Category.objects.create(
            name="Test Kits", slug="test-kits", description="",
        )
        cls.item = Item.objects.create(
            category=category, title="Malaria RDT", slug="malaria-rdt",
            price=1000, status="in stock", description="",
        )

    def test_the_route_name_no_longer_reverses(self):
        with self.assertRaises(NoReverseMatch):
            reverse("delete_item")

    def test_the_old_path_is_gone(self):
        response = self.client.get(f"/doctorDashboard/products/delete/{self.item.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_an_anonymous_get_deletes_nothing(self):
        """The exact reproduction from the audit: one unauthenticated URL
        fetch took the product count from 1 to 0."""
        self.client.get(f"/doctorDashboard/products/delete/{self.item.pk}/")
        self.assertTrue(Item.objects.filter(pk=self.item.pk).exists())

    def test_a_superuser_cannot_delete_through_it_either(self):
        """Deleted, not merely locked down — deletion moved to the Django
        admin, which confirms, checks permissions and logs."""
        superuser = User.objects.create_superuser(
            username="del-root", email="del-root@example.com", password=PASSWORD,
        )
        self.client.force_login(superuser)
        self.client.get(f"/doctorDashboard/products/delete/{self.item.pk}/")
        self.assertTrue(Item.objects.filter(pk=self.item.pk).exists())


class MissingProfileImageTests(TestCase):
    """Plan 1.1. Half the accounts in the real database have no photo, and
    the shared base template rendered `{{ user.image.url }}` unguarded — so
    those accounts 500'd on every page of the console."""

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="no-photo", email="no-photo@example.com",
            password=PASSWORD, role=User.Role.DOCTOR,
        )

    def test_the_dashboard_renders_for_an_account_with_no_photo(self):
        self.assertFalse(self.doctor.image)
        self.client.force_login(self.doctor)

        response = self.client.get(reverse("doctor_admin_dashboard"))

        self.assertEqual(response.status_code, 200)

    def test_the_fallback_icon_is_rendered_instead_of_a_broken_image(self):
        # Was `doctor-avatar-sidebar`. The console shows one avatar now, in the
        # top bar beside the account menu (plan 11.9); the sidebar copy said
        # the same thing and could do nothing about it.
        self.client.force_login(self.doctor)
        response = self.client.get(reverse("doctor_admin_dashboard"))
        self.assertContains(response, 'class="user-avatar-top"')
        self.assertNotContains(response, "<img src=\"\"")


class EcommerceSidebarTests(TestCase):
    """§4.4 point 3: a doctor should never see a control they cannot use."""

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="sidebar-doctor", email="sidebar-doctor@example.com",
            password=PASSWORD, role=User.Role.DOCTOR,
        )
        cls.superuser = User.objects.create_superuser(
            username="sidebar-root", email="sidebar-root@example.com", password=PASSWORD,
        )

    def test_the_products_link_is_hidden_from_a_doctor(self):
        self.client.force_login(self.doctor)
        response = self.client.get(reverse("doctor_admin_dashboard"))
        self.assertNotContains(response, reverse("ecommerce_items"))

    def test_the_products_link_is_shown_to_a_superuser(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("doctor_admin_dashboard"))
        self.assertContains(response, reverse("ecommerce_items"))


# ---------------------------------------------------------------------------
# Phase 5 — the doctor's console
# ---------------------------------------------------------------------------
#
# The gate: "a doctor can run both confirmation rounds and manage a real
# timetable; an admin can see across all doctors; neither can touch ecommerce."
# The last clause is already covered by `DashboardAccessTests` above.


class ConsoleTestCase(TestCase):
    """One doctor, one administrator, one other doctor to be kept out of."""

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="console-doctor", email="console-doctor@example.com",
            password=PASSWORD, role=User.Role.DOCTOR,
            first_name="Anna", last_name="Magembe",
        )
        cls.other_doctor = make_staff_user(
            username="other-doctor", email="other-doctor@example.com",
            password=PASSWORD, role=User.Role.DOCTOR,
            first_name="Juma", last_name="Ally",
        )
        cls.admin = make_staff_user(
            username="console-admin", email="console-admin@example.com",
            password=PASSWORD, role=User.Role.ADMIN, first_name="Kulwa",
        )

    def setUp(self):
        mail.outbox = []
        self._minute = 0
        self.client.force_login(self.doctor)

    def make_slot(self, doctor=None, days=3, hour=10):
        self._minute += 1
        hours, minute = divmod(self._minute, 60)
        return AvailableSlot.objects.create(
            user=doctor or self.doctor,
            date=timezone.localdate() + timedelta(days=days),
            time=time(hour + hours, minute),
        )

    def make_appointment(self, slot=None, status=None, **extra):
        fields = dict(
            fullName="Asha Mbwana", birthDate=date(1990, 5, 1),
            gender="female", email="asha@example.com", phone="0712345678",
            visitType="new_patient", reasonVisit="lab_test",
            slot=slot or self.make_slot(),
        )
        # Every appointment carries a mode now: the patient chooses one on the
        # booking form (plan 12.1), and the doctor confirms the time and the
        # mode in one action.
        fields.setdefault("appointmentMode", Appointment.Mode.PATIENT_TRAVELS)
        fields.update(extra)
        appointment = Appointment.objects.create(**fields)
        if status is not None:
            Appointment.objects.filter(pk=appointment.pk).update(status=status)
            appointment.refresh_from_db()
        return appointment

    def decide(self, appointment, action, **data):
        return self.client.post(
            reverse("appointment_decision", args=[appointment.id]),
            {"action": action, **data},
        )

    def details(self, appointment):
        return self.client.get(
            reverse("doctor_appointment_details", args=[appointment.id])
        )


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class ConfirmationRoundsTests(ConsoleTestCase):
    """Plan 5.1, 5.2, 5.4 and **12.1** — the doctor's decision.

    Singular since 2026-09-03. It was two gates with a patient choice between
    them; the patient now says how they want to meet when they book, so the
    doctor sees the time and the mode together and answers once. The class
    keeps its name because the state machine still has rounds - there is one
    of them.
    """

    def test_confirming_settles_the_appointment_in_one_move(self):
        appointment = self.make_appointment()

        self.decide(appointment, "confirm")

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)
        # Both, from the one decision: the time and the mode are agreed in the
        # same act now.
        self.assertIsNotNone(appointment.time_confirmed_at)
        self.assertIsNotNone(appointment.mode_confirmed_at)

    def test_confirming_tells_the_patient_it_is_settled(self):
        appointment = self.make_appointment()

        self.decide(appointment, "confirm")

        told = NotificationLog.objects.get(event="appointment.confirmed")
        self.assertEqual(told.recipient, "asha@example.com")
        self.assertNotIn("your turn", told.body.lower())

    def test_the_doctor_may_confirm_a_different_way_of_meeting(self):
        """AGKI's decision, and the half that keeps a booking alive: a patient
        who asks a doctor to travel to them and gets a doctor who does not
        would otherwise have to be declined outright."""
        appointment = self.make_appointment(
            appointmentMode=Appointment.Mode.PROVIDER_TRAVELS
        )

        self.decide(
            appointment, "confirm", appointmentMode=Appointment.Mode.PATIENT_TRAVELS
        )

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)
        self.assertEqual(
            appointment.appointmentMode, Appointment.Mode.PATIENT_TRAVELS
        )

    def test_a_changed_mode_gets_its_own_message_and_audit_note(self):
        """"Confirmed" and "confirmed, but not the way you asked" are
        different facts. A patient now expected to travel should be told, not
        left to notice it in a table."""
        appointment = self.make_appointment(
            appointmentMode=Appointment.Mode.PROVIDER_TRAVELS
        )

        self.decide(
            appointment, "confirm", appointmentMode=Appointment.Mode.PATIENT_TRAVELS
        )

        told = NotificationLog.objects.get(
            event="appointment.confirmed_other_mode"
        )
        self.assertIn("Provider travels to the patient", told.body)
        self.assertIn("Changed how they will meet", appointment.transitions.get().note)

    def test_a_mode_the_slot_does_not_offer_cannot_be_confirmed(self):
        """The doctor publishes what they can do; this is that promise read
        back. It also stops a hand-made POST committing them to something they
        never offered."""
        # Deliberately **not** the online mode: `transition_to` refuses to
        # confirm that without a meeting link whatever the slot offers, so a
        # test using it would pass even with this rule deleted - which is
        # exactly what the mutation check found.
        appointment = self.make_appointment()
        AvailableSlot.objects.filter(pk=appointment.slot.pk).update(
            offers_provider_travels=False
        )

        self.decide(
            appointment, "confirm", appointmentMode=Appointment.Mode.PROVIDER_TRAVELS
        )

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)
        self.assertEqual(
            appointment.appointmentMode, Appointment.Mode.PATIENT_TRAVELS
        )

    def test_declining_the_time_frees_the_slot_and_passes_on_the_reason(self):
        appointment = self.make_appointment()

        self.decide(appointment, "decline_time", reason="Theatre list overran")

        appointment.refresh_from_db()
        appointment.slot.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.TIME_DECLINED)
        self.assertFalse(appointment.slot.is_booked)
        told = NotificationLog.objects.get(event="appointment.time_declined")
        self.assertIn("Theatre list overran", told.body)

    def test_completed_and_no_show_are_reachable_from_confirmed(self):
        for action, expected in (
            ("mark_completed", Appointment.Status.COMPLETED),
            ("mark_no_show", Appointment.Status.NO_SHOW),
        ):
            with self.subTest(action=action):
                appointment = self.make_appointment(status=Appointment.Status.CONFIRMED)

                self.decide(appointment, action)

                appointment.refresh_from_db()
                self.assertEqual(appointment.status, expected)

    def test_the_audit_row_names_the_doctor_who_decided(self):
        """"Who confirmed this and when" has to have an answer months later
        (design §5)."""
        appointment = self.make_appointment()

        self.decide(appointment, "confirm")

        row = appointment.transitions.get()
        self.assertEqual(row.actor, self.doctor)
        self.assertIn("confirmed the appointment", row.note)

    def test_a_decision_that_is_not_legal_from_here_is_refused(self):
        """The page offers only legal moves, but a POST need not come from the
        page. Marking a booking completed before anybody has confirmed it is
        the move to try: it is a real action, from the wrong state."""
        appointment = self.make_appointment()

        self.decide(appointment, "mark_completed")

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)

    def test_a_stale_button_cannot_be_replayed(self):
        """**This found a real bug when there were two rounds**: two decisions
        ended at `PENDING_MODE` - confirming the time and rejecting the mode -
        and the state machine allowed that destination from both places, so a
        stale "Confirm this time" POST could be accepted as a rejection,
        emailing the patient the wrong copy under the wrong audit note. The
        decision table is keyed on the *move* rather than the destination, and
        that is what this still guards.

        With one confirmation the collision is gone, but the replay is not: a
        doctor double-clicking, or two people on one account, posts the same
        button twice.
        """
        appointment = self.make_appointment(status=Appointment.Status.CONFIRMED)

        self.decide(appointment, "confirm")

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)
        self.assertFalse(NotificationLog.objects.exists())
        self.assertEqual(AppointmentTransition.objects.count(), 0)

    def test_an_unknown_action_changes_nothing(self):
        appointment = self.make_appointment()

        self.decide(appointment, "delete_everything")

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)
        self.assertEqual(AppointmentTransition.objects.count(), 0)

    def test_a_get_decides_nothing(self):
        appointment = self.make_appointment()

        response = self.client.get(
            reverse("appointment_decision", args=[appointment.id])
        )

        self.assertEqual(response.status_code, 405)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)

    def test_a_doctor_cannot_decide_on_another_doctors_appointment(self):
        appointment = self.make_appointment(slot=self.make_slot(doctor=self.other_doctor))

        response = self.decide(appointment, "confirm")

        self.assertEqual(response.status_code, 404)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)

    def test_the_page_offers_exactly_the_moves_that_are_legal(self):
        """Derived from the state machine, so a button is never shown for a
        move the model would refuse."""
        cases = {
            Appointment.Status.PENDING_TIME: {"confirm", "decline_time"},
            # The legacy pair, answerable rather than stranded (plan 12.1).
            Appointment.Status.PENDING_MODE: set(),
            Appointment.Status.MODE_PROPOSED: {
                "confirm_legacy_mode", "decline_legacy_mode",
            },
            Appointment.Status.CONFIRMED: {"mark_completed", "mark_no_show"},
            Appointment.Status.CANCELLED: set(),
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                appointment = self.make_appointment(status=status)
                offered = {
                    action for action, _ in
                    self.details(appointment).context["decisions"]
                }
                self.assertEqual(offered, expected)

    def test_a_settled_appointment_says_there_is_nothing_to_decide(self):
        appointment = self.make_appointment(status=Appointment.Status.COMPLETED)

        self.assertContains(self.details(appointment), "nothing left to decide")

    def test_the_page_no_longer_offers_actions_with_no_view_behind_them(self):
        body = self.details(self.make_appointment()).content.decode()

        for fake in ("Schedule Appointment", "Send Message", "Print Profile",
                     "Lab Results - Oct 2025.pdf", "Share note with patient"):
            with self.subTest(control=fake):
                self.assertNotIn(fake, body)


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class MeetingLinkRequiredTests(ConsoleTestCase):
    """Plan 5.3 / decision B. An "online appointment" with no link is worse
    than an unconfirmed one, because both sides think it is settled."""

    def online(self):
        return self.make_appointment(
            status=Appointment.Status.PENDING_TIME,
            appointmentMode=Appointment.Mode.ONLINE,
        )

    def test_confirming_online_without_a_link_is_refused_by_the_view(self):
        appointment = self.online()

        self.decide(appointment, "confirm", meeting_url="")

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)

    def test_the_refusal_says_why(self):
        appointment = self.online()

        response = self.client.post(
            reverse("appointment_decision", args=[appointment.id]),
            {"action": "confirm", "meeting_url": ""},
            follow=True,
        )

        self.assertContains(response, "needs a meeting link")

    def test_the_model_refuses_it_too(self):
        """Tested on its own rather than through the view, because the view's
        check would otherwise be the only thing standing between an empty link
        and a confirmed appointment — and the admin, a shell session and any
        future API do not go through the view. (Phase 4's mutation check found
        exactly this shape of gap and it is not repeated here.)"""
        appointment = self.online()

        with self.assertRaises(InvalidTransition):
            appointment.transition_to(
                Appointment.Status.CONFIRMED, actor=self.doctor
            )

    def test_confirming_with_a_link_stores_it_and_sends_it(self):
        appointment = self.online()

        self.decide(
            appointment, "confirm", meeting_url="https://meet.example/agki-9",
        )

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)
        self.assertEqual(appointment.meeting_url, "https://meet.example/agki-9")
        told = NotificationLog.objects.get(event="appointment.confirmed")
        self.assertIn("https://meet.example/agki-9", told.body)

    def test_the_other_two_modes_need_no_link(self):
        for mode in (Appointment.Mode.PATIENT_TRAVELS, Appointment.Mode.PROVIDER_TRAVELS):
            with self.subTest(mode=mode):
                appointment = self.make_appointment(
                    status=Appointment.Status.PENDING_TIME, appointmentMode=mode,
                )

                self.decide(appointment, "confirm")

                appointment.refresh_from_db()
                self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)

    def test_an_appointment_with_no_mode_at_all_cannot_be_confirmed(self):
        """Reachable only by skipping round 2 — which is what the check is
        there to stop."""
        appointment = self.make_appointment(status=Appointment.Status.PENDING_TIME)
        Appointment.objects.filter(pk=appointment.pk).update(appointmentMode="")
        appointment.refresh_from_db()

        with self.assertRaises(InvalidTransition):
            appointment.transition_to(Appointment.Status.CONFIRMED, actor=self.doctor)

    def test_the_link_field_is_only_asked_for_when_the_mode_is_online(self):
        online = self.details(self.online()).content.decode()
        in_person = self.details(
            self.make_appointment(status=Appointment.Status.PENDING_TIME)
        ).content.decode()

        self.assertIn('name="meeting_url"', online)
        self.assertNotIn('name="meeting_url"', in_person)


@override_settings(NOTIFICATIONS_DELIVERY="queued")
class ConfirmationCutoffTests(ConsoleTestCase):
    """Plan 6.3, which ships here because it is a guard *in the confirm views*.

    Belt and braces against the expiry cron having silently stopped — a
    classic failure, and one where the damage is telling a patient their
    appointment is on when the slot may already have been re-offered.
    """

    def too_late(self):
        soon = timezone.localtime() + timedelta(minutes=20)
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=soon.date(), time=soon.time(),
        )
        return self.make_appointment(slot=slot)

    def test_a_doctor_cannot_confirm_the_time_past_the_cutoff(self):
        appointment = self.too_late()

        self.decide(appointment, "confirm")

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)

    def test_a_doctor_cannot_confirm_past_the_cutoff(self):
        """One confirmation now (plan 12.1), so there is one thing the cut-off
        has to refuse - and it is the one that would tell a patient an
        appointment is on when the slot may already have been released."""
        appointment = self.too_late()

        self.decide(appointment, "confirm")

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.PENDING_TIME)

    def test_declining_past_the_cutoff_is_still_allowed(self):
        """The cut-off protects the patient from a confirmation that cannot be
        honoured. Saying no late is still better than saying nothing, and it
        frees the slot."""
        appointment = self.too_late()

        self.decide(appointment, "decline_time", reason="Sorry")

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.TIME_DECLINED)

    def test_the_page_says_why_the_buttons_will_not_work(self):
        self.assertContains(self.details(self.too_late()), "confirmation window closed")

    def test_a_booking_comfortably_ahead_is_unaffected(self):
        appointment = self.make_appointment(slot=self.make_slot(days=5))

        self.decide(appointment, "confirm")

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)


class AvailabilityTimetableTests(ConsoleTestCase):
    """Plan 5.5 / decision 5 — a real week and month, navigable.

    Every control on the old page was an `onclick` calling a function that was
    never defined, so it always rendered the current week whatever you pressed.
    """

    def page(self, **params):
        return self.client.get(reverse("doctor_availability"), params)

    def test_the_default_view_is_this_week(self):
        response = self.page()

        monday = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
        self.assertEqual(response.context["view_mode"], "week")
        self.assertEqual(response.context["period_start"], monday)

    def test_next_and_previous_are_real_links_that_move_the_period(self):
        first = self.page()
        following = self.page(start=first.context["next_start"].isoformat())

        self.assertEqual(
            following.context["period_start"],
            first.context["period_start"] + timedelta(days=7),
        )
        back = self.page(start=following.context["prev_start"].isoformat())
        self.assertEqual(back.context["period_start"], first.context["period_start"])

    def test_the_month_view_covers_the_whole_month(self):
        response = self.page(view="month", start="2026-02-11")

        self.assertEqual(response.context["period_start"], date(2026, 2, 1))
        self.assertEqual(response.context["period_end"], date(2026, 2, 28))
        self.assertEqual(response.context["period_label"], "February 2026")

    def test_month_navigation_crosses_the_year_boundary(self):
        december = self.page(view="month", start="2026-12-03")

        january = self.page(view="month", start=december.context["next_start"].isoformat())

        self.assertEqual(january.context["period_start"], date(2027, 1, 1))

    def test_a_nonsense_start_falls_back_to_today_rather_than_500ing(self):
        response = self.page(start="not-a-date")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_current_period"])

    def test_only_slots_inside_the_period_are_listed(self):
        inside = self.make_slot(days=1)
        outside = self.make_slot(days=40)

        listed = {
            slot.id
            for _, slots in self.page(start=inside.date.isoformat())
            .context["slots_by_date"]
            for slot in slots
        }

        self.assertIn(inside.id, listed)
        self.assertNotIn(outside.id, listed)

    def test_the_grid_is_padded_to_whole_weeks(self):
        """So a month renders as a month rather than as a ragged list."""
        weeks = self.page(view="month", start="2026-02-11").context["weeks"]

        self.assertTrue(all(len(week) == 7 for week in weeks))
        self.assertEqual(weeks[0][0]["date"].weekday(), 0)

    def test_a_slot_too_close_to_book_is_shown_but_marked(self):
        """It is a real published time that simply can no longer be booked;
        hiding it from the doctor who published it would look like data loss
        (design §6.1)."""
        soon = timezone.localtime() + timedelta(minutes=30)
        AvailableSlot.objects.create(
            user=self.doctor, date=soon.date(), time=soon.time(),
        )

        response = self.page(start=soon.date().isoformat())

        shown = [slot for _, slots in response.context["slots_by_date"] for slot in slots]
        self.assertTrue(shown)
        self.assertFalse(shown[0].is_bookable_now)
        self.assertContains(response, "Too close to book")

    def test_a_doctor_sees_only_their_own_slots(self):
        """Anchored on the slot's own week, not on the default view.

        `self.page()` renders **this** week and `make_slot()` publishes three
        days out, so from Thursday onwards the fixture landed in next week's
        grid and this asserted against an empty page. It failed every Thursday
        to Sunday and nobody noticed, because it is a scoping test and the day
        of the week has nothing to do with scoping. Two other tests in this
        class already navigate to the date they are about to assert on; this
        one now does the same.
        """
        mine = self.make_slot()
        theirs = self.make_slot(doctor=self.other_doctor)

        page = self.page(start=mine.date.isoformat())
        listed = {
            slot.id for _, slots in page.context["slots_by_date"] for slot in slots
        }

        self.assertEqual(listed, {mine.id})
        self.assertNotIn(theirs.id, listed)

    def test_the_administrator_sees_every_doctors_slots(self):
        """Plan 1.9 shipped her a route to the console and said plainly that
        she would land on an empty dashboard until this widened (§4.2 point 3)."""
        mine = self.make_slot()
        theirs = self.make_slot(doctor=self.other_doctor)
        self.client.force_login(self.admin)

        # Anchored on the fixture's week rather than the default one, for the
        # reason given on the test above.
        page = self.page(start=mine.date.isoformat())
        listed = {
            slot.id for _, slots in page.context["slots_by_date"] for slot in slots
        }

        self.assertEqual(listed, {mine.id, theirs.id})

    def test_the_hardcoded_hour_matrix_is_gone(self):
        """It ran 08:00-17:00 and showed one slot per hour, so a doctor
        publishing 08:00 and 08:30 saw one of them and anything at 19:00
        appeared nowhere."""
        early = AvailableSlot.objects.create(
            user=self.doctor, date=timezone.localdate() + timedelta(days=2), time=time(19, 45),
        )
        half_past = AvailableSlot.objects.create(
            user=self.doctor, date=early.date, time=time(8, 30),
        )
        on_the_hour = AvailableSlot.objects.create(
            user=self.doctor, date=early.date, time=time(8, 0),
        )

        # Anchored on the fixture's week: these are published two days out, so
        # on a Friday or later the default view is the wrong week entirely and
        # the assertion was about times this page was never showing.
        page = self.page(start=early.date.isoformat())
        listed = {
            slot.id for _, slots in page.context["slots_by_date"] for slot in slots
        }

        self.assertEqual(listed, {early.id, half_past.id, on_the_hour.id})


class PublishAvailabilityTests(ConsoleTestCase):
    """Design §5.2, which had no owner until the availability page was rebuilt."""

    def publish(self, on_date, at, **overrides):
        payload = {
            "date": on_date.isoformat(),
            "time": at,
            # A published time says what you will do at it as well as when
            # (plan 12.1), and at least one way is required - a time nobody can
            # be seen at is not availability.
            "offers_online": "1",
            "offers_provider_travels": "1",
            "offers_patient_travels": "1",
        }
        payload.update(overrides)
        return self.client.post(
            reverse("add_doctor_availability"), payload, follow=True,
        )

    def test_a_future_slot_is_published(self):
        tomorrow = timezone.localdate() + timedelta(days=1)

        self.publish(tomorrow, "09:00")

        self.assertTrue(
            AvailableSlot.objects.filter(user=self.doctor, date=tomorrow).exists()
        )

    def test_a_slot_in_the_past_is_refused(self):
        """A patient could book it straight through the URL — the five-day
        window was only ever a display filter (design §10 appointments #19)."""
        yesterday = timezone.localdate() - timedelta(days=1)

        response = self.publish(yesterday, "09:00")

        self.assertFalse(AvailableSlot.objects.filter(date=yesterday).exists())
        self.assertContains(response, "already passed")

    def test_publishing_the_same_time_twice_is_refused_with_a_message(self):
        """`unique_together` (plan 2.8) would otherwise raise an IntegrityError
        from the view — a 500 rather than an answer — because `user` is not a
        form field, so Django's own uniqueness check never runs."""
        tomorrow = timezone.localdate() + timedelta(days=1)
        self.publish(tomorrow, "09:00")

        response = self.publish(tomorrow, "09:00")

        self.assertEqual(AvailableSlot.objects.filter(date=tomorrow).count(), 1)
        self.assertContains(response, "already published")

    def test_two_doctors_may_publish_the_same_time(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        AvailableSlot.objects.create(user=self.other_doctor, date=tomorrow, time=time(9, 0))

        self.publish(tomorrow, "09:00")

        self.assertEqual(AvailableSlot.objects.filter(date=tomorrow).count(), 2)

    def test_a_get_publishes_nothing(self):
        response = self.client.get(reverse("add_doctor_availability"))

        self.assertEqual(response.status_code, 405)


class DeleteAvailabilityTests(ConsoleTestCase):
    """Plan 5.6. The Delete Slot button has always called this endpoint; the
    endpoint has never existed, so the button 404'd (design §10 dashboard #5).
    Worse, it called `/delete-availability/<id>/` at the **root** of the site,
    which is not where this app is mounted."""

    def delete(self, slot):
        return self.client.post(reverse("delete_availability", args=[slot.id]), follow=True)

    def test_a_free_slot_is_deleted(self):
        slot = self.make_slot()

        self.delete(slot)

        self.assertFalse(AvailableSlot.objects.filter(pk=slot.pk).exists())

    def test_a_slot_with_an_appointment_is_refused(self):
        """Deleting it would take the patient's record with it — which is what
        `on_delete=CASCADE` used to do, and why plan 2.9 made it PROTECT."""
        appointment = self.make_appointment()

        response = self.delete(appointment.slot)

        self.assertTrue(AvailableSlot.objects.filter(pk=appointment.slot.pk).exists())
        self.assertTrue(Appointment.objects.filter(pk=appointment.pk).exists())
        self.assertContains(response, "Cancel or decline the appointment first")

    def test_a_cancelled_appointment_still_protects_its_slot(self):
        """The case where the check and the database constraint disagree:
        cancelling frees `is_booked`, but the appointment row still points at
        the slot, so deleting it would still destroy a patient's record."""
        appointment = self.make_appointment()
        appointment.transition_to(Appointment.Status.CANCELLED, note="Patient cancelled.")
        appointment.slot.refresh_from_db()
        self.assertFalse(appointment.slot.is_booked)

        self.delete(appointment.slot)

        self.assertTrue(AvailableSlot.objects.filter(pk=appointment.slot.pk).exists())

    def test_a_get_deletes_nothing(self):
        """`delete_item` is the cautionary tale (design §10 dashboard #17): a
        destructive GET is triggerable by anything that follows a link."""
        slot = self.make_slot()

        response = self.client.get(reverse("delete_availability", args=[slot.id]))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(AvailableSlot.objects.filter(pk=slot.pk).exists())

    def test_a_doctor_cannot_delete_another_doctors_slot(self):
        slot = self.make_slot(doctor=self.other_doctor)

        response = self.client.post(reverse("delete_availability", args=[slot.id]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(AvailableSlot.objects.filter(pk=slot.pk).exists())

    def test_the_delete_control_is_hidden_on_a_booked_slot(self):
        appointment = self.make_appointment()

        response = self.client.get(reverse("doctor_availability"))

        self.assertNotContains(
            response, reverse("delete_availability", args=[appointment.slot.id])
        )

    def test_the_delete_control_is_hidden_once_cancelled_too(self):
        """**A real bug the mutation check surfaced.** The control was hidden
        on *live* appointments only, so a cancelled one left a Delete button
        that `PROTECT` could only ever refuse — the patient's record still
        points at the slot whatever the status says. The page now offers the
        button exactly when deleting would succeed."""
        appointment = self.make_appointment()
        appointment.transition_to(Appointment.Status.CANCELLED, note="Patient cancelled.")

        response = self.client.get(reverse("doctor_availability"))

        self.assertNotContains(
            response, reverse("delete_availability", args=[appointment.slot.id])
        )


class AdminScopingTests(ConsoleTestCase):
    """Design §4.2 point 3, and the other half of plan 1.9.

    The lab administrator was routed to this console in Phase 1 with an
    explicit note that she would land on an empty dashboard until the
    querysets widened. This is that.
    """

    def setUp(self):
        super().setUp()
        self.mine = self.make_appointment()
        self.theirs = self.make_appointment(
            slot=self.make_slot(doctor=self.other_doctor), fullName="Someone Else",
            email="else@example.com",
        )

    def test_a_doctor_sees_only_their_own_appointments(self):
        # `show=all` so the assertion does not depend on which tab the
        # fixture's status happens to fall in - scoping is the subject here,
        # not grouping.
        response = self.client.get(
            reverse("available_appointments"), {"show": "all"}
        )

        self.assertContains(response, self.mine.bookingID)
        self.assertNotContains(response, self.theirs.bookingID)

    def test_the_administrator_sees_every_doctors(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("available_appointments"))

        self.assertContains(response, self.mine.bookingID)
        self.assertContains(response, self.theirs.bookingID)

    def test_the_administrator_lands_on_a_dashboard_with_real_numbers(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("doctor_admin_dashboard"))

        self.assertEqual(response.context["pending_count"], 2)

    def test_the_administrator_can_open_any_doctors_appointment(self):
        self.client.force_login(self.admin)

        response = self.details(self.theirs)

        self.assertEqual(response.status_code, 200)

    def test_a_doctor_still_cannot_open_another_doctors_appointment(self):
        self.assertEqual(self.details(self.theirs).status_code, 404)

    def test_a_superuser_who_is_a_doctor_keeps_their_own_dashboard(self):
        """§4.1.2: "a doctor is a doctor first". Widening their view to every
        doctor would take away the page they use to give them one they already
        have in /admin/. One live account (`mccoln`) is exactly this."""
        self.doctor.is_superuser = True
        self.doctor.save(update_fields=["is_superuser"])

        response = self.client.get(reverse("available_appointments"))

        self.assertContains(response, self.mine.bookingID)
        self.assertNotContains(response, self.theirs.bookingID)


class AppointmentsListTests(ConsoleTestCase):
    """Plan 5.14."""

    def test_the_date_column_is_there(self):
        """The page lists every appointment ever made, so a row saying "09:00"
        could be this morning or eight months ago (design §10 dashboard #12).

        Still asserted with the year, which is why the redesign renders
        `D d M Y` rather than the shorter `D j M` the mockup showed: this page
        spans everything ever booked, and dropping the year would be the same
        failure in a new shape.
        """
        appointment = self.make_appointment()

        response = self.client.get(
            reverse("available_appointments"), {"show": "all"}
        )

        self.assertContains(response, appointment.slot.date.strftime("%d %b %Y"))

    def test_the_empty_state_does_not_claim_to_be_about_today(self):
        response = self.client.get(reverse("available_appointments"))

        self.assertContains(response, "No appointments yet")
        self.assertNotContains(response, "scheduled for today")

    def test_the_status_column_reads_the_real_state(self):
        """It branched on the superseded `AppointmentApproval` row, so six
        distinct outcomes all rendered as "Pending".

        Asks for `show=all` since plan 11.4: the page now opens on "Needs
        you", and a cancelled appointment is over, so it is not there. The
        guarantee under test has not moved - the status still comes from
        `status` via `get_status_display` - only the tab it is read in.
        """
        cancelled = self.make_appointment(status=Appointment.Status.CANCELLED)

        response = self.client.get(
            reverse("available_appointments"), {"show": "all"}
        )

        self.assertContains(response, escape(cancelled.get_status_display()))

    def test_every_status_lands_in_exactly_one_tab(self):
        """The filters partition the list, so nothing can vanish from it.

        Five hand-written `Q` objects are exactly the shape that leaves a
        status in none of them - or in two - and the symptom is an appointment
        a doctor simply cannot find. Asserted against `Status.values` rather
        than a list written here, so a status added later fails this until
        somebody decides where it belongs.
        """
        for value in Appointment.Status.values:
            appointment = self.make_appointment(status=value)
            found = [
                name
                for name, _, _ in self.client.get(
                    reverse("available_appointments")
                ).context["list_views"]
                if name != "all"
                and appointment.id in [
                    a.id
                    for a in self.client.get(
                        reverse("available_appointments"), {"show": name}
                    ).context["appointments"]
                ]
            ]
            self.assertEqual(
                len(found), 1,
                "%s appears in %r, expected exactly one tab" % (value, found),
            )
            appointment.delete()


class SidebarBadgeTests(ConsoleTestCase):
    """Plan 5.13. Hardcoded `12` and `8`, on every page, for every account."""

    def test_the_badge_counts_this_doctors_waiting_appointments(self):
        self.make_appointment()
        self.make_appointment(status=Appointment.Status.PENDING_TIME)
        self.make_appointment(status=Appointment.Status.COMPLETED)

        response = self.client.get(reverse("doctor_admin_dashboard"))

        self.assertEqual(response.context["sidebar_waiting_count"], 2)

    def test_the_bell_counts_what_this_doctor_must_answer(self):
        """It used to exclude `PENDING_MODE`, the patient's turn, so the badge
        did not nag a doctor about somebody else's inaction (design §6.1).
        Since plan 12.1 there is no such state: every open booking is theirs to
        answer, and the badge says so."""
        self.make_appointment()
        self.make_appointment(status=Appointment.Status.PENDING_TIME)

        response = self.client.get(reverse("doctor_admin_dashboard"))

        self.assertEqual(response.context["sidebar_action_count"], 2)

    def test_another_doctors_backlog_is_not_counted(self):
        self.make_appointment(slot=self.make_slot(doctor=self.other_doctor))

        response = self.client.get(reverse("doctor_admin_dashboard"))

        self.assertEqual(response.context["sidebar_waiting_count"], 0)

    def test_the_hardcoded_numbers_are_gone(self):
        response = self.client.get(reverse("doctor_admin_dashboard"))

        self.assertNotContains(response, '<span class="menu-badge">12</span>')
        self.assertNotContains(response, '<span class="notification-badge">8</span>')

    def test_a_badge_of_zero_is_not_rendered_at_all(self):
        response = self.client.get(reverse("doctor_admin_dashboard"))

        self.assertNotContains(response, 'class="notification-badge"')

    def test_the_processor_is_silent_for_accounts_with_no_console_role(self):
        """Content Editors and the developer superusers go to /admin/; there is
        no meaningful count for them, and querying one on every CMS page would
        be pure waste."""
        editor = make_staff_user(username="badge-editor", email="badge-editor@example.com")
        self.client.force_login(editor)

        response = self.client.get("/")

        self.assertNotIn("sidebar_waiting_count", response.context or {})


class StaffListTests(ConsoleTestCase):
    """Plan 5.10 and 5.11."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_only_console_roles_are_listed(self):
        """Was `CustomUser.objects.all()` under a heading reading "Registered
        Doctors" (design §10 dashboard #3)."""
        patient = make_staff_user(username="not-staff", email="not-staff@example.com")

        response = self.client.get(reverse("doctors"))

        listed = {account.pk for account in response.context["doctors"]}
        self.assertEqual(listed, {self.doctor.pk, self.other_doctor.pk, self.admin.pk})
        self.assertNotIn(patient.pk, listed)

    def test_the_stat_cards_have_values_behind_them(self):
        """All three were bound to context this view has never provided."""
        User.objects.filter(pk=self.other_doctor.pk).update(is_approved=False)

        response = self.client.get(reverse("doctors"))

        self.assertEqual(response.context["doctors_count"], 3)
        self.assertEqual(response.context["active_count"], 3)
        self.assertEqual(response.context["withdrawn_count"], 1)

    def test_approval_and_activity_are_two_separate_switches(self):
        """The single button showed `is_active` and flipped `is_approved`
        (design §10 dashboard #4)."""
        target = self.other_doctor

        self.client.post(
            reverse("set_doctor_status", args=[target.pk]),
            {"flag": "approved", "value": "off"},
        )
        target.refresh_from_db()
        self.assertFalse(target.is_approved)
        self.assertTrue(target.is_active, "approval must not touch is_active")

        self.client.post(
            reverse("set_doctor_status", args=[target.pk]),
            {"flag": "active", "value": "off"},
        )
        target.refresh_from_db()
        self.assertFalse(target.is_active)

    def test_an_unknown_flag_changes_nothing(self):
        self.client.post(
            reverse("set_doctor_status", args=[self.other_doctor.pk]),
            {"flag": "is_superuser", "value": "on"},
        )

        self.other_doctor.refresh_from_db()
        self.assertFalse(self.other_doctor.is_superuser)

    def test_an_administrator_cannot_switch_off_their_own_account(self):
        """Withdrawing your own approval ends your session and locks you out
        of signing back in (plan 1.10) — from a button whose obvious reading is
        "toggle"."""
        self.client.post(
            reverse("set_doctor_status", args=[self.admin.pk]),
            {"flag": "approved", "value": "off"},
        )

        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_approved)

    def test_a_doctor_cannot_reach_the_staff_list_at_all(self):
        self.client.force_login(self.doctor)

        self.assertEqual(self.client.get(reverse("doctors")).status_code, 403)

    def test_the_removed_fake_controls_are_gone(self):
        body = self.client.get(reverse("doctors")).content.decode()

        for fake in ("exportDoctors", "showDoctorDetails", "General Practitioner"):
            with self.subTest(control=fake):
                self.assertNotIn(fake, body)


class AddDoctorTests(ConsoleTestCase):
    """Plan 5.12 — the modal that could never have worked."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def payload(self, **overrides):
        data = {
            "firstname": "Neema", "lastname": "Joseph",
            "username": "neema", "email": "neema@example.com",
            "phone": "0715000000",
            "password1": "a-strong-pass-1", "password2": "a-strong-pass-1",
        }
        data.update(overrides)
        return data

    def add(self, **overrides):
        return self.client.post(reverse("add_doctor"), self.payload(**overrides), follow=True)

    def test_a_doctor_is_created_with_the_doctor_role(self):
        self.add()

        account = User.objects.get(username="neema")
        self.assertEqual(account.role, User.Role.DOCTOR)
        self.assertEqual(account.get_full_name(), "Neema Joseph")
        self.assertTrue(account.is_active)

    def test_the_new_account_can_sign_in_immediately(self):
        """Inverted by decision F (plan 10.1).

        This asserted the account started unapproved, reasoning that creating
        one does not prove the person exists. The reasoning survives as design
        §4.5's open problem, but it stopped belonging on this button: a
        stranger self-registering is admitted on the spot, so an administrator
        adding a colleague they have met cannot be the stricter path."""
        self.add()

        account = User.objects.get(username="neema")
        self.assertTrue(account.is_approved)
        self.assertTrue(account.is_active)
        self.assertFalse(account.check_password("wrong-password"))

    def test_mismatched_passwords_create_nothing(self):
        response = self.add(password2="something-else")

        self.assertFalse(User.objects.filter(username="neema").exists())
        self.assertContains(response, "do not match")

    def test_a_duplicate_email_is_refused(self):
        response = self.add(email=self.doctor.email)

        self.assertFalse(User.objects.filter(username="neema").exists())
        self.assertContains(response, "already exists")

    def test_a_duplicate_username_is_refused(self):
        response = self.add(username=self.doctor.username)

        self.assertContains(response, "already taken")

    def test_missing_fields_are_refused_rather_than_500ing(self):
        """**The bug this row was written for.** The modal posted to the public
        `signup` view, which reads `request.POST['phone']` and
        `request.POST['role']` with bare subscripts and got neither — so every
        submission was a `MultiValueDictKeyError`."""
        response = self.client.post(reverse("add_doctor"), {"username": "half"}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="half").exists())

    def test_a_doctor_cannot_add_accounts(self):
        self.client.force_login(self.doctor)

        self.assertEqual(
            self.client.post(reverse("add_doctor"), self.payload()).status_code, 403
        )

    def test_the_form_no_longer_posts_to_the_public_signup_view(self):
        body = self.client.get(reverse("doctors")).content.decode()

        self.assertIn(reverse("add_doctor"), body)
        self.assertNotIn(reverse("signup"), body)
class AvailabilityPageTests(ConsoleTestCase):
    """The redesign of 2026-08-31 (plan 11.1).

    Most of this page's problems were **invisible to a test that only asked
    for a 200**, which is how they lasted: the "Add Availability" overlay used
    `.modal-overlay` and `.modal-container`, classes styled inside two other
    pages' own <style> blocks and defined nowhere this page could see. So the
    form sat permanently open at the foot of the page and the button that was
    supposed to open it did nothing - while every existing test passed, because
    the markup was all present and correct.

    These assert the things that were wrong, at the layer where each can be
    checked: that the page carries its own styles, that a day offers its own
    date, and that a booked slot leads somewhere.
    """

    def page(self, **params):
        return self.client.get(reverse("doctor_availability"), params)

    def test_the_page_defines_the_classes_it_uses(self):
        """The finding that mattered most, stated as a rule rather than a list.

        Every `av-` class the markup uses must be defined in this template. The
        prefix is what makes the check possible: the old names were generic
        enough (`.modal-overlay`, `.slot-card`) that a reader could reasonably
        assume the base supplied them, and nothing would say otherwise until
        the page rendered wrong.
        """
        import re
        from pathlib import Path

        from django.conf import settings

        source = (
            Path(settings.BASE_DIR) / "doctorDashboard" / "templates"
            / "doctorDashboard" / "doctor_availability.html"
        ).read_text(encoding="utf-8")

        used = set()
        for attr in re.findall(r'class="([^"{}]*)"', source):
            used.update(c for c in attr.split() if c.startswith("av-"))
        defined = set(re.findall(r"\.(av-[a-zA-Z0-9_-]+)", source))

        self.assertTrue(used, "no av- classes found; the check has stopped working")
        self.assertEqual(
            sorted(used - defined), [],
            "these classes are used but never styled in this template",
        )

    def test_the_add_form_is_hidden_until_the_button_is_pressed(self):
        """The overlay must start closed. It could not before: with no CSS for
        `.modal-overlay`, the form was a plain block in the page flow."""
        import re
        from pathlib import Path

        from django.conf import settings

        source = (
            Path(settings.BASE_DIR) / "doctorDashboard" / "templates"
            / "doctorDashboard" / "doctor_availability.html"
        ).read_text(encoding="utf-8")

        self.assertRegex(source, r"\.av-overlay\s*\{[^}]*display:\s*none")
        self.assertRegex(source, r"\.av-overlay\.show\s*\{[^}]*display:\s*flex")

    def test_every_day_offers_its_own_date(self):
        """`openAddAvailabilityModal(date)` always took a date and prefilled
        the field with it; both call sites passed nothing, so the parameter was
        dead and adding a time meant retyping the day you were looking at.

        The day's readable label goes with it, because the field it fills is
        now read-only - having chosen the day by clicking it, you are not asked
        to choose it again, and a locked `2026-09-03` box needs the words
        beside it to be worth locking.
        """
        from django.template.defaultfilters import date as date_filter

        response = self.page()

        monday = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
        # Built with Django's own formatter rather than `strftime`, because the
        # template uses `date:'l, j F Y'` and `%-d` is not portable - it raises
        # on Windows, which is where this suite runs.
        self.assertContains(
            response,
            "openAddAvailabilityModal('%s', '%s')"
            % (monday.isoformat(), date_filter(monday, "l, j F Y")),
        )

    def test_a_booked_slot_links_to_its_appointment(self):
        """It said "Booked" and stopped. The one thing a doctor wants from a
        booked slot is on a page this one never linked to."""
        slot = self.make_slot()
        appointment = self.make_appointment(slot=slot)

        response = self.page(start=slot.date.isoformat())

        self.assertContains(
            response, reverse("doctor_appointment_details", args=[appointment.id])
        )
        self.assertContains(response, appointment.fullName)

    def test_a_free_slot_offers_removal_and_a_booked_one_does_not(self):
        """`PROTECT` refuses to delete a slot an appointment points at, whatever
        the appointment's status - so offering the button there is offering one
        that can only produce an error (plan 5.x found the same thing)."""
        free = self.make_slot()
        taken = self.make_slot()
        self.make_appointment(slot=taken)

        response = self.page(start=free.date.isoformat())

        self.assertContains(response, reverse("delete_availability", args=[free.id]))
        self.assertNotContains(response, reverse("delete_availability", args=[taken.id]))

    def test_the_month_view_shows_counts_instead_of_chips(self):
        """A month cell is a third the height of a week cell. Seven chips do
        not fit, and "+3 more" makes a month impossible to scan."""
        slot = self.make_slot(days=2)

        response = self.page(view="month", start=slot.date.isoformat())

        # Matched on the class **attribute**, not the bare name: the stylesheet
        # in this template defines rules for both, so searching the whole
        # document finds them whichever view rendered.
        self.assertContains(response, 'class="av-counts"')
        self.assertNotContains(response, 'class="av-chip')

    def test_the_week_view_shows_chips_instead_of_counts(self):
        slot = self.make_slot()

        response = self.page(start=slot.date.isoformat())

        self.assertContains(response, 'class="av-chip')
        self.assertNotContains(response, 'class="av-counts"')

    def test_the_metrics_count_what_they_say(self):
        """"Utilisation 25%" is replaced by a count of published times that can
        no longer be booked - a number a doctor can clear."""
        bookable = self.make_slot()
        booked = self.make_slot()
        booked.is_booked = True
        booked.save(update_fields=["is_booked"])
        soon = timezone.localtime() + timedelta(minutes=20)
        AvailableSlot.objects.create(user=self.doctor, date=soon.date(), time=soon.time())

        stats = self.page(start=bookable.date.isoformat()).context["stats"]

        self.assertEqual(stats["booked_slots"], 1)
        self.assertEqual(stats["bookable_slots"], 1)


class PublishTimesTests(ConsoleTestCase):
    """Publishing a pattern rather than a slot (plan 11.1).

    Replaces 75 form submissions with one for a doctor offering 09:00-16:00 on
    weekdays for a month. It is the honest version of a control this project
    deleted: the old form had a repeat pattern with a weekday picker and a
    "repeat until" date, and saved none of it.
    """

    def publish(self, **overrides):
        monday = timezone.localdate() + timedelta(days=(7 - timezone.localdate().weekday()))
        payload = {
            "start_date": monday.isoformat(),
            "end_date": (monday + timedelta(days=4)).isoformat(),
            "weekdays": ["0", "1", "2", "3", "4"],
            "times": "09:00,09:30",
            # See the note in the single-slot helper above.
            "offers_online": "1",
            "offers_provider_travels": "1",
            "offers_patient_travels": "1",
        }
        payload.update(overrides)
        return self.client.post(reverse("publish_availability"), payload, follow=True)

    def test_a_pattern_publishes_every_matching_slot(self):
        self.publish()

        self.assertEqual(AvailableSlot.objects.filter(user=self.doctor).count(), 10)

    def test_only_the_chosen_weekdays_are_published(self):
        self.publish(weekdays=["0"])

        days = {s.date.weekday() for s in AvailableSlot.objects.filter(user=self.doctor)}
        self.assertEqual(days, {0})

    def test_republishing_skips_what_is_already_there(self):
        """`unique_together` would otherwise make a second publish over the
        same week an `IntegrityError` from the view - a 500 rather than an
        answer. Publishing a second block of times over a week that already
        has some is a normal thing to do."""
        self.publish()

        response = self.publish(times="09:00,09:30,10:00")

        self.assertEqual(AvailableSlot.objects.filter(user=self.doctor).count(), 15)
        self.assertContains(response, "were already there")

    def test_times_in_the_past_are_skipped_not_refused(self):
        """The single-slot form refuses a past date, because publishing one is
        a mistake. A range starting today legitimately contains this morning,
        and refusing the whole publish over 09:00 would be obstructive."""
        yesterday = timezone.localdate() - timedelta(days=1)
        tomorrow = timezone.localdate() + timedelta(days=1)

        self.publish(
            start_date=yesterday.isoformat(),
            end_date=tomorrow.isoformat(),
            weekdays=[str(d) for d in range(7)],
            times="09:00",
        )

        published = AvailableSlot.objects.filter(user=self.doctor)
        self.assertTrue(published.exists())
        self.assertFalse(published.filter(date=yesterday).exists())

    def test_a_reversed_range_is_refused(self):
        monday = timezone.localdate() + timedelta(days=7)
        response = self.publish(
            start_date=monday.isoformat(),
            end_date=(monday - timedelta(days=3)).isoformat(),
        )

        self.assertContains(response, "before the first one")
        self.assertFalse(AvailableSlot.objects.filter(user=self.doctor).exists())

    def test_a_publish_that_would_create_nothing_says_so(self):
        """"Published 0" reads like a silent failure. Naming the reason does
        not."""
        monday = timezone.localdate() + timedelta(days=(7 - timezone.localdate().weekday()))
        response = self.publish(
            start_date=monday.isoformat(),
            end_date=(monday + timedelta(days=1)).isoformat(),
            weekdays=["5", "6"],
        )

        self.assertContains(response, "no future times")

    def test_an_oversized_publish_is_refused_with_its_size(self):
        """The inputs multiply, so a slipped year in "until" turns a
        reasonable-looking form into tens of thousands of rows. Refusing with a
        number beats inserting them and asking somebody to clean up."""
        start = timezone.localdate() + timedelta(days=1)
        response = self.publish(
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=364)).isoformat(),
            weekdays=[str(d) for d in range(7)],
            times="09:00,09:30,10:00",
        )

        self.assertContains(response, "The most in one go")
        self.assertFalse(AvailableSlot.objects.filter(user=self.doctor).exists())

    def test_a_bad_time_names_the_value(self):
        response = self.publish(times="09:00,half nine")

        self.assertContains(response, "is not a time")
        self.assertFalse(AvailableSlot.objects.filter(user=self.doctor).exists())

    def test_it_publishes_for_the_signed_in_doctor_only(self):
        """`user` is not a form field, exactly as on the single-slot form -
        so there is nothing in the request body that could name somebody
        else's calendar."""
        self.publish()

        self.assertFalse(AvailableSlot.objects.filter(user=self.other_doctor).exists())

    def test_a_get_publishes_nothing(self):
        response = self.client.get(reverse("publish_availability"))

        self.assertEqual(response.status_code, 405)
        self.assertFalse(AvailableSlot.objects.filter(user=self.doctor).exists())
class AvailabilityFormControlTests(ConsoleTestCase):
    """The three things AGKI found on the rebuilt page, 2026-08-31.

    Two of them were not visible to any test that asks for a 200, which is the
    recurring lesson on this page: the markup was right and the page was wrong.
    """

    def source(self):
        from pathlib import Path

        from django.conf import settings

        return (
            Path(settings.BASE_DIR) / "doctorDashboard" / "templates"
            / "doctorDashboard" / "doctor_availability.html"
        ).read_text(encoding="utf-8")

    # -------------------------------------------------- the invisible button

    def test_the_save_button_is_in_the_add_form(self):
        response = self.client.get(reverse("doctor_availability"))

        self.assertContains(response, 'action="%s"' % reverse("add_doctor_availability"))
        self.assertContains(response, "Add time</button>")

    def test_the_tokens_reach_inside_the_overlays(self):
        """Why the Save button was invisible, and why no rendered-HTML test
        could have caught it.

        The `--av-*` custom properties were declared on `.av`. The overlays are
        **siblings** of that element, not children - a modal inside the
        scrolling dashboard content would scroll with it - so inside a modal
        `background: var(--av-brand)` resolved to nothing, the declaration was
        dropped, and `.av-btn`'s `color: #fff` put white text on a white sheet.
        The button was present, focusable and submittable, and invisible.

        Django renders HTML; it does not compute the cascade. So this asserts
        the source: whatever selector declares the tokens must cover
        `.av-overlay`. Weaker than seeing it, and the only thing available.
        """
        import re

        source = self.source()
        hosts = [
            selector
            for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", source)
            if "--av-brand:" in body
        ]

        self.assertTrue(hosts, "nothing declares the --av- tokens any more")
        self.assertTrue(
            any(".av-overlay" in host for host in hosts),
            "the tokens do not reach the modals: %r" % hosts,
        )

    # -------------------------------------------------- the settled date

    def test_a_day_chosen_by_clicking_is_not_asked_for_again(self):
        """`readonly`, not `disabled`. A disabled input is not submitted, so
        disabling the date would post no date at all - the form would fail
        validation on a field the doctor was never given a way to fill."""
        source = self.source()

        self.assertIn("field.readOnly = true;", source)
        self.assertNotIn("field.disabled = true;", source)
        self.assertRegex(source, r"\.av-field input\[readonly\]\s*\{")

    def test_the_lock_is_cleared_when_no_day_is_given(self):
        """Kept after the caller that reached it was removed.

        A toolbar "Add one time" button opened this form with nothing decided;
        AGKI had it removed on 2026-08-31, since adding a single time already
        belongs to a day and every day carries its own `+`. So no UI path
        reaches the unlock branch today.

        It stays, and is still asserted, because the overlay is **one element
        reused across every open**. A future caller that omitted the date would
        inherit the previous day's lock - a stale value in a field nobody can
        edit, which is a worse failure than the three lines that prevent it.
        Asserting it is what stops the branch being tidied away by someone who
        notices it has no caller.
        """
        self.assertIn("field.readOnly = false;", self.source())

    def test_the_only_way_into_the_add_form_supplies_a_day(self):
        """The other half of removing the toolbar button: if some control
        opened this form without a date, the lock above would be the only thing
        standing between a doctor and a slot published on the wrong day."""
        import re

        source = self.source()
        calls = re.findall(r"onclick=\"openAddAvailabilityModal\(([^)]*)\)", source)

        self.assertTrue(calls, "nothing opens the add form any more")
        self.assertEqual(
            [c for c in calls if not c.strip()], [],
            "a control opens the add form without naming a day",
        )

    # -------------------------------------------------- 24-hour confusion

    def test_times_are_offered_as_a_list_in_am_pm(self):
        """`<input type="time">` renders in the **browser's** locale, not the
        site's, so the same page asked one doctor for `14:30` and another for
        `2:30 PM` with nothing on screen saying which. A doctor who typed
        `2:30` meaning the afternoon published half past two in the morning."""
        response = self.client.get(reverse("doctor_availability"))

        self.assertContains(response, '<select name="time" id="startTime" required>')
        self.assertContains(response, '<option value="14:30">2:30 PM</option>')
        self.assertContains(response, '<option value="09:00">9:00 AM</option>')
        self.assertNotContains(response, 'type="time"')

    def test_noon_and_midday_are_not_confused(self):
        """The classic off-by-twelve: `%I` gives 12 for noon and for midnight,
        and a naive hour-12 conversion gives `0:00 PM`."""
        from appointments.forms import TIME_CHOICES

        labels = dict(TIME_CHOICES)
        self.assertEqual(labels["12:00"], "12:00 PM")
        self.assertEqual(labels["11:45"], "11:45 AM")
        self.assertEqual(labels["13:00"], "1:00 PM")

    def test_the_page_shows_times_in_am_pm_too(self):
        """Entering "2:30 PM" and then reading "14:30" back off the calendar
        would be the same confusion one step later."""
        slot = self.make_slot(hour=14)

        response = self.client.get(
            reverse("doctor_availability"), {"start": slot.date.isoformat()}
        )

        self.assertContains(response, "PM")
        self.assertNotContains(response, ">14:")

    def test_the_server_still_receives_24_hour_values(self):
        """The split that makes the rest safe: AM/PM is a label, and the value
        that travels is what `TimeField` already parses. Changing the display
        must not change the wire format."""
        from appointments.forms import TIME_CHOICES

        for value, _ in TIME_CHOICES:
            self.assertRegex(value, r"^\d{2}:\d{2}$")

        self.client.post(
            reverse("add_doctor_availability"),
            {"date": (timezone.localdate() + timedelta(days=3)).isoformat(),
             "time": "14:30", "next": reverse("doctor_availability"),
             "offers_patient_travels": "1"},
        )

        self.assertTrue(
            AvailableSlot.objects.filter(user=self.doctor, time=time(14, 30)).exists()
        )
class AppointmentsPageTests(ConsoleTestCase):
    """The redesign of 2026-09-01 (plan 11.4).

    Same system as the availability page. Two things on the old page were not
    design problems but wrong answers, and both are asserted here.
    """

    def page(self, **params):
        return self.client.get(reverse("available_appointments"), params)

    def test_the_page_defines_the_classes_it_uses(self):
        """Same rule as the availability page. The prefix is what makes it
        checkable: a generic name can be supplied - or not - by another page's
        stylesheet, which is how that page shipped a button nobody could see."""
        import re
        from pathlib import Path

        from django.conf import settings

        source = (
            Path(settings.BASE_DIR) / "doctorDashboard" / "templates"
            / "doctorDashboard" / "available_appointments.html"
        ).read_text(encoding="utf-8")

        used = set()
        for attr in re.findall(r'class="([^"{}]*)"', source):
            used.update(c for c in attr.split() if c.startswith("ap-"))
        defined = set(re.findall(r"\.(ap-[a-zA-Z0-9_-]+)", source))

        self.assertTrue(used, "no ap- classes found; the check has stopped working")
        self.assertEqual(sorted(used - defined), [])

    # ------------------------------------------------ the two wrong answers

    def test_the_appointments_figure_counts_appointments(self):
        """It was fed by `total_patients` - distinct email addresses - under a
        label reading "Total Appointments", so a patient seen four times
        counted once and the number was smaller than the table below it."""
        for _ in range(3):
            self.make_appointment(email="repeat@example.com", fullName="Repeat Patient")

        context = self.page(show="all").context

        self.assertEqual(context["total_appointments"], 3)
        self.assertEqual(context["total_patients"], 1)

    def test_the_soonest_appointment_is_first(self):
        """Ordering was `-slot__date`, so a booking for December sat above one
        starting tomorrow. Sorting by when it happens was an earlier fix; the
        direction was never revisited."""
        far = self.make_appointment(slot=self.make_slot(days=60))
        soon = self.make_appointment(slot=self.make_slot(days=2))

        listed = [a.id for a in self.page(show="all").context["appointments"]]

        self.assertLess(listed.index(soon.id), listed.index(far.id))

    def test_the_past_tab_puts_the_most_recent_first(self):
        """The one place descending is right: what just happened is nearer to
        now than what happened in March."""
        # Negative offsets: `make_slot(days=N)` counts **forward**, so the
        # first version of this test put "old" forty days in the future and
        # asserted the wrong thing about it.
        old = self.make_appointment(
            slot=self.make_slot(days=-40), status=Appointment.Status.COMPLETED
        )
        recent = self.make_appointment(
            slot=self.make_slot(days=-1), status=Appointment.Status.COMPLETED
        )

        listed = [a.id for a in self.page(show="past").context["appointments"]]

        self.assertLess(listed.index(recent.id), listed.index(old.id))

    # ------------------------------------------------------- whose turn

    def test_the_page_opens_on_what_needs_the_doctor(self):
        self.assertEqual(self.page().context["show"], "needs_you")

    def test_every_open_appointment_is_the_doctors_to_answer(self):
        """The "Waiting on patient" tab is gone with the round it held (plan
        12.1). `PENDING_MODE` was the patient choosing how to meet; they choose
        when they book now, so nothing waits on them and a tab for it would
        always be empty - which teaches a doctor to stop reading tabs."""
        waiting = self.make_appointment(status=Appointment.Status.PENDING_TIME)

        needs_you = [a.id for a in self.page(show="needs_you").context["appointments"]]
        tabs = [name for name, _, _ in self.page().context["list_views"]]

        self.assertIn(waiting.id, needs_you)
        self.assertNotIn("waiting_patient", tabs)

    def test_a_confirmed_appointment_that_has_passed_still_needs_the_doctor(self):
        """It is waiting to be marked completed or no-show, and only the doctor
        can decide which. Filing it under Past by the clock would file work
        away where nobody looks for it."""
        gone = timezone.localtime() - timedelta(hours=2)
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=gone.date(), time=gone.time(), is_booked=True
        )
        appointment = self.make_appointment(
            slot=slot, status=Appointment.Status.CONFIRMED
        )

        needs_you = [a.id for a in self.page(show="needs_you").context["appointments"]]
        upcoming = [a.id for a in self.page(show="upcoming").context["appointments"]]

        self.assertIn(appointment.id, needs_you)
        self.assertNotIn(appointment.id, upcoming)

    # ------------------------------------------------------- search + pages

    def test_search_matches_a_name_or_a_booking_reference(self):
        wanted = self.make_appointment(fullName="Halima Juma")
        other = self.make_appointment(fullName="Baraka Mushi")

        by_name = [a.id for a in self.page(show="all", q="halima").context["appointments"]]
        by_ref = [a.id for a in self.page(show="all", q=wanted.bookingID).context["appointments"]]

        self.assertEqual(by_name, [wanted.id])
        self.assertEqual(by_ref, [wanted.id])
        self.assertNotIn(other.id, by_name)

    def test_the_tab_counts_follow_the_search(self):
        """Otherwise the tabs say how big the system is, when what the doctor
        wants to know is where their matches are."""
        self.make_appointment(fullName="Halima Juma")
        self.make_appointment(fullName="Baraka Mushi")

        counts = dict(
            (name, count)
            for name, _, count in self.page(q="halima").context["list_views"]
        )

        self.assertEqual(counts["all"], 1)

    def test_the_list_is_paginated(self):
        """There is no pagination anywhere else in this app, and this table
        grows every week."""
        for _ in range(30):
            self.make_appointment()

        page = self.page(show="all").context["page"]

        self.assertEqual(page.paginator.per_page, 25)
        self.assertEqual(len(page.object_list), 25)
        self.assertTrue(page.has_next())

    def test_an_unknown_tab_falls_back_rather_than_500ing(self):
        self.assertEqual(self.page(show="nonsense").context["show"], "needs_you")

    # ------------------------------------------------------- inline actions

    def test_a_row_offers_the_decision_it_can_take(self):
        appointment = self.make_appointment(status=Appointment.Status.PENDING_TIME)

        row = self.page().context["appointments"][0]

        self.assertEqual(
            [action for action, _ in row.inline_actions], ["confirm"]
        )
        self.assertEqual(
            [action for action, _ in row.page_actions], ["decline_time"]
        )

    def test_declining_is_never_offered_inline(self):
        """It carries a reason to the patient. That is not a one-click
        action, so it opens the page that can ask for one."""
        self.make_appointment(status=Appointment.Status.PENDING_TIME)

        response = self.page()

        self.assertContains(response, "Open to decide")
        self.assertNotContains(response, 'value="decline_time"')

    def test_confirming_an_online_mode_is_not_offered_inline(self):
        """`transition_to` refuses to confirm an ONLINE mode without a meeting
        link (plan 5.3), so an inline Confirm there could only ever fail."""
        appointment = self.make_appointment(
            status=Appointment.Status.PENDING_TIME,
            appointmentMode=Appointment.Mode.ONLINE,
        )

        row = self.page().context["appointments"][0]

        self.assertEqual(row.inline_actions, [])
        self.assertEqual([a for a, _ in row.page_actions], ["confirm", "decline_time"])

    def test_confirming_a_mode_that_needs_no_link_is_offered_inline(self):
        """The other side: nothing extra is needed, so nothing extra is asked.

        There is no "in person" mode - decision B splits it by **who travels**,
        which is the distinction that matters to a patient in a rural area.
        Neither travelling mode needs a meeting link.
        """
        self.make_appointment(
            status=Appointment.Status.PENDING_TIME,
            appointmentMode=Appointment.Mode.PROVIDER_TRAVELS,
        )

        row = self.page().context["appointments"][0]

        self.assertIn("confirm", [a for a, _ in row.inline_actions])

    def test_an_outcome_is_not_offered_before_the_appointment_happens(self):
        """`transition_to` allows CONFIRMED -> COMPLETED whenever, so the
        decision table offers it on an appointment six days away. Legal is not
        the same as sensible: recording an outcome for something that has not
        happened is not a thing to put one click from a scrolling list."""
        self.make_appointment(
            slot=self.make_slot(days=6), status=Appointment.Status.CONFIRMED
        )

        row = self.page(show="upcoming").context["appointments"][0]

        self.assertEqual(row.inline_actions, [])
        self.assertEqual(
            sorted(a for a, _ in row.page_actions),
            ["mark_completed", "mark_no_show"],
        )

    def test_an_outcome_is_offered_once_it_has(self):
        """The other side, and the reason a passed confirmed appointment is
        filed under "Needs you" at all."""
        gone = timezone.localtime() - timedelta(hours=3)
        slot = AvailableSlot.objects.create(
            user=self.doctor, date=gone.date(), time=gone.time(), is_booked=True
        )
        self.make_appointment(slot=slot, status=Appointment.Status.CONFIRMED)

        row = self.page(show="needs_you").context["appointments"][0]

        self.assertEqual(
            sorted(a for a, _ in row.inline_actions),
            ["mark_completed", "mark_no_show"],
        )

    def test_confirming_from_the_list_moves_the_appointment(self):
        appointment = self.make_appointment(status=Appointment.Status.PENDING_TIME)

        self.client.post(
            reverse("appointment_decision", args=[appointment.id]),
            {"action": "confirm", "next": reverse("available_appointments")},
        )

        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CONFIRMED)

    def test_confirming_from_the_list_comes_back_to_the_list(self):
        """Sending the doctor to the detail page after each decision would undo
        the point of putting the button on the row."""
        appointment = self.make_appointment(status=Appointment.Status.PENDING_TIME)

        response = self.client.post(
            reverse("appointment_decision", args=[appointment.id]),
            {"action": "confirm", "next": reverse("available_appointments")},
        )

        self.assertEqual(response["Location"], reverse("available_appointments"))

    def test_a_next_pointing_off_site_is_refused(self):
        """`next` comes out of the request body. Handing it to `redirect()`
        unchecked makes every view that takes one an open redirect - a crafted
        form could send a signed-in doctor to a site dressed as this one."""
        appointment = self.make_appointment(status=Appointment.Status.PENDING_TIME)

        response = self.client.post(
            reverse("appointment_decision", args=[appointment.id]),
            {"action": "confirm", "next": "https://evil.example/steal"},
        )

        self.assertNotIn("evil.example", response["Location"])
        self.assertEqual(
            response["Location"],
            reverse("doctor_appointment_details", args=[appointment.id]),
        )
class DashboardTests(ConsoleTestCase):
    """The rebuild of 2026-09-01 (plan 11.5).

    The first test here is the reason this page was done before the detail
    page: it did not merely look dated, it **told every doctor the same thing
    about every appointment, and that thing was wrong**.
    """

    def page(self, **params):
        return self.client.get(reverse("doctor_admin_dashboard"), params)

    def today_slot(self, hour=9, minute=0):
        return AvailableSlot.objects.create(
            user=self.doctor, date=timezone.localdate(),
            time=time(hour, minute), is_booked=True,
        )

    # ---------------------------------------------------- the page was lying

    def test_the_status_is_read_from_status(self):
        """It read `i.appointmentapproval.is_approved` - the superseded
        `AppointmentApproval`, which nothing has written to since Phase 5. A
        missing related object is falsy in a template, so **every** row took
        the else branch and rendered "Pending": confirmed, completed,
        cancelled and waiting alike. Plan 5.14's bug, in a page it never
        reached.
        """
        states = [
            Appointment.Status.CONFIRMED,
            Appointment.Status.COMPLETED,
            Appointment.Status.CANCELLED,
            Appointment.Status.PENDING_TIME,
        ]
        for offset, status in enumerate(states):
            self.make_appointment(slot=self.today_slot(9, offset), status=status)

        body = self.page().content.decode()

        for status in states:
            label = dict(Appointment.Status.choices)[status]
            self.assertIn(
                escape(label), body,
                "%s renders as something else - the old page showed 'Pending' "
                "for all four" % status,
            )

    def test_the_reason_shows_its_label_not_the_stored_value(self):
        """`{{ i.reasonVisit }}` rendered `lab_test`. 2.10 closed the choice
        lists and this page was never updated (plan 5.15, third occurrence)."""
        self.make_appointment(
            slot=self.today_slot(10), reasonVisit=Appointment.ReasonForVisit.LAB_TEST
        )

        response = self.page()

        self.assertContains(response, "Lab test")
        self.assertNotContains(response, "lab_test")

    def test_no_control_on_the_page_is_a_dead_button(self):
        """"View All", "Full Calendar" and the week's prev/next were bare
        `<button>` elements on a page with **no script block and zero onclick
        attributes** - so unlike the availability page's dead navigation they
        were not even calling functions that were missing.

        Every `<button>` here must therefore submit something; everything else
        is a link. Asserted on the source because a button that does nothing
        renders exactly like one that does.
        """
        import re
        from pathlib import Path

        from django.conf import settings

        source = (
            Path(settings.BASE_DIR) / "doctorDashboard" / "templates"
            / "doctorDashboard" / "doctor_admin_dashboard.html"
        ).read_text(encoding="utf-8")

        # Template comments are stripped first. The block at the top of that
        # file *describes* the dead buttons - "were bare `<button>` elements" -
        # so scanning the raw source finds the prose about the bug and reports
        # it as the bug. Same lesson as the availability page's JS guard, whose
        # first source assertion failed on the comment naming it.
        prose = re.compile(r"{% comment %}.*?{% endcomment %}", re.S)
        buttons = re.findall(r"<button\b[^>]*>", prose.sub("", source))
        self.assertTrue(buttons, "no buttons found; the check has stopped working")
        self.assertEqual(
            [b for b in buttons if 'type="submit"' not in b], [],
            "a button that submits nothing is a button that does nothing",
        )

    def test_the_page_defines_the_classes_it_uses(self):
        import re
        from pathlib import Path

        from django.conf import settings

        source = (
            Path(settings.BASE_DIR) / "doctorDashboard" / "templates"
            / "doctorDashboard" / "doctor_admin_dashboard.html"
        ).read_text(encoding="utf-8")

        used = set()
        for attr in re.findall(r'class="([^"{}]*)"', source):
            used.update(c for c in attr.split() if c.startswith("db-"))
        defined = set(re.findall(r"\.(db-[a-zA-Z0-9_-]+)", source))

        self.assertTrue(used)
        self.assertEqual(sorted(used - defined), [])

    # ---------------------------------------------------- whose turn, again

    def test_needs_you_counts_every_open_appointment(self):
        """It used to exclude `PENDING_MODE`, the patient's turn to choose a
        mode, so that the figure did not nag a doctor about somebody else's
        inaction. There is no such state now (plan 12.1): every open booking is
        waiting on the doctor, and the two figures agree."""
        self.make_appointment(status=Appointment.Status.PENDING_TIME)
        self.make_appointment(status=Appointment.Status.PENDING_TIME)

        context = self.page().context

        self.assertEqual(context["needs_you_count"], 2)
        self.assertEqual(context["pending_count"], 2)

    def test_every_number_links_to_the_page_that_explains_it(self):
        """A dashboard figure that cannot be clicked makes the reader go and
        find the same thing again by hand."""
        response = self.page()

        self.assertContains(response, "%s?show=needs_you" % reverse("available_appointments"))
        self.assertContains(response, reverse("doctor_availability"))

    # ---------------------------------------------------- today

    def test_today_is_ordered_by_time(self):
        late = self.make_appointment(slot=self.today_slot(15))
        early = self.make_appointment(slot=self.today_slot(8))

        listed = [a.id for a in self.page().context["today_appointments"]]

        self.assertEqual(listed, [early.id, late.id])

    def test_today_offers_the_same_decisions_as_the_list(self):
        """`_row_actions` is reused rather than re-derived, so a decision
        cannot be offered here that the appointments page would refuse.

        The cut-off is patched off rather than a time chosen for it. An
        appointment at 11:00 today cannot be confirmed once 10:30 has passed,
        which is correct - and would make this test pass or fail on the hour
        the suite runs, which is the trap plan 10.x was written to close.
        """
        appointment = self.make_appointment(
            slot=self.today_slot(11), status=Appointment.Status.PENDING_TIME
        )

        with mock.patch(
            "doctorDashboard.views.confirmation_cutoff_passed", return_value=False
        ):
            row = self.page().context["today_appointments"][0]
            listed = [
                a for a in self.client.get(
                    reverse("available_appointments"), {"show": "needs_you"}
                ).context["appointments"] if a.id == appointment.id
            ][0]

        self.assertEqual([a for a, _ in row.inline_actions], ["confirm"])
        self.assertEqual([a for a, _ in row.page_actions], ["decline_time"])
        # The point of the test: both pages ask the same function.
        self.assertEqual(
            [a for a, _ in row.inline_actions], [a for a, _ in listed.inline_actions]
        )

    def test_an_administrator_sees_whose_appointment_it_is(self):
        """`sees_every_doctor` merges every doctor's day into one list, and
        the old page had no column saying whose."""
        self.make_appointment(
            slot=AvailableSlot.objects.create(
                user=self.other_doctor, date=timezone.localdate(),
                time=time(12, 0), is_booked=True,
            )
        )
        self.client.force_login(self.admin)

        response = self.page()

        self.assertContains(response, self.other_doctor.get_full_name())

    # ---------------------------------------------------- the week

    def test_the_week_can_be_moved(self):
        """Prev and next were `<button>` elements with no handler, so the strip
        showed this week and only ever would. Links now, as plan 5.5 made the
        availability page's - which also means a week can be bookmarked."""
        first = self.page()
        following = self.page(start=first.context["next_start"].isoformat())

        self.assertEqual(
            following.context["week_start"],
            first.context["week_start"] + timedelta(days=7),
        )
        back = self.page(start=following.context["prev_start"].isoformat())
        self.assertEqual(back.context["week_start"], first.context["week_start"])

    def test_the_week_counts_booked_of_published(self):
        monday = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
        for minute in range(3):
            AvailableSlot.objects.create(
                user=self.doctor, date=monday, time=time(9, minute)
            )
        booked = AvailableSlot.objects.get(user=self.doctor, date=monday, time=time(9, 0))
        self.make_appointment(slot=booked)

        day = self.page().context["week"][0]

        self.assertEqual(day["published"], 3)
        self.assertEqual(day["booked"], 1)

    def test_a_day_with_nothing_published_says_so(self):
        """It said "Available" - a claim about published availability, made
        while counting appointments, and false for exactly the day a doctor
        would want to catch on a Monday morning."""
        response = self.page()

        self.assertTrue(all(d["nothing_published"] for d in response.context["week"]))
        self.assertContains(response, "Nothing")
        self.assertNotContains(response, "Available")

    def test_a_cancelled_appointment_does_not_count_as_booked(self):
        """Cancelling frees the slot, so counting the row would overstate the
        week and contradict the availability page."""
        monday = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
        slot = AvailableSlot.objects.create(user=self.doctor, date=monday, time=time(9, 0))
        self.make_appointment(slot=slot, status=Appointment.Status.CANCELLED)

        day = self.page().context["week"][0]

        self.assertEqual(day["published"], 1)
        self.assertEqual(day["booked"], 0)

    def test_patients_booking_this_week_is_not_called_new(self):
        """It counted distinct emails among appointments **created** this week,
        so a patient of three years who booked on Monday was "new". It never
        was a count of new patients."""
        self.make_appointment(email="regular@example.com")
        self.make_appointment(email="regular@example.com", slot=self.make_slot(days=5))

        context = self.page().context

        self.assertEqual(context["patients_booking_this_week"], 1)
        self.assertNotIn("new_patients_week", context)


class ConsoleChromeTests(TestCase):
    """The sidebar and top bar, rebuilt (plan 11.9).

    Four things this covers were live defects rather than matters of taste:
    every tab in the console read "Doctor Dashboard - MediBook", every `<h1>`
    said "Dashboard", the account menu looked clickable and did nothing, and
    one undefined function call killed every script statement after it.
    """

    @classmethod
    def setUpTestData(cls):
        cls.doctor = make_staff_user(
            username="chrome-doctor", email="chrome-doctor@example.com",
            password=PASSWORD, role=User.Role.DOCTOR, first_name="Ada", last_name="Lowe",
        )
        cls.admin = make_staff_user(
            username="chrome-admin", email="chrome-admin@example.com",
            password=PASSWORD, role=User.Role.ADMIN,
        )

    def page(self, name, user=None):
        self.client.force_login(user or self.doctor)
        return self.client.get(reverse(name))

    def body(self, name, user=None):
        return self.page(name, user).content.decode()

    #: (url name, what the tab and the heading should both say)
    PAGES = (
        ("doctor_admin_dashboard", "Dashboard"),
        ("available_appointments", "Appointments"),
        ("doctor_availability", "Availability"),
        ("profile", "My profile"),
        ("profile_edit", "Edit profile"),
    )

    def test_every_page_titles_itself(self):
        """One hardcoded title served twelve pages, so twelve history entries
        and twelve tabs were indistinguishable."""
        for name, title in self.PAGES:
            with self.subTest(page=name):
                html = self.body(name)
                self.assertIn(
                    "<title>%s · AGKI Doctor Console</title>" % title, html
                )
                self.assertNotIn("MediBook", html)

    def test_the_tab_and_the_heading_read_the_same_words(self):
        """The reason the title is a context variable and not two blocks: two
        blocks are two copies, and copies drift."""
        for name, title in self.PAGES:
            with self.subTest(page=name):
                html = self.body(name)
                self.assertIn('<h1 class="page-title">%s</h1>' % title, html)

    def test_a_page_says_it_once(self):
        """Availability, Appointments and Edit profile each printed their name
        twice: once in the bar and once as an `<h2>` a scroll below it."""
        for name, title in self.PAGES:
            with self.subTest(page=name):
                self.assertNotIn("<h2>%s</h2>" % title, self.body(name))

    def test_the_all_doctors_pages_say_so_in_the_title_too(self):
        for name in ("available_appointments", "doctor_availability"):
            with self.subTest(page=name):
                html = self.body(name, user=self.admin)
                self.assertIn("all doctors · AGKI Doctor Console", html)

    def test_signing_out_lives_in_the_account_menu_now(self):
        """It was a sidebar item whose label disappeared with the rail, so a
        doctor who collapsed the sidebar was signing out by icon."""
        html = self.body("doctor_admin_dashboard")
        sidebar = html.split("<aside", 1)[1].split("</aside>", 1)[0]
        menu = html.split('id="userMenu"', 1)[1].split("</nav>", 1)[0]

        self.assertNotIn(reverse("logout"), sidebar)
        self.assertIn(reverse("logout"), menu)
        self.assertEqual(html.count('href="%s"' % reverse("logout")), 1)

    def test_the_account_control_is_a_real_menu(self):
        """It was a `<div>` with an avatar, a name, a chevron-down and a hover
        style - every signal of a menu - with nothing behind it."""
        html = self.body("doctor_admin_dashboard")

        self.assertIn('id="userMenuBtn"', html)
        self.assertIn('aria-haspopup="true"', html)
        self.assertIn('aria-controls="userMenu"', html)
        for target in ("profile", "profile_edit", "logout"):
            self.assertIn(reverse(target), html)

    def test_the_doctor_is_named_once_in_the_chrome(self):
        """AGKI's instruction: the sidebar and the bar both carried an avatar,
        a name and a role. Only the bar's copy sits beside a menu that can act
        on it."""
        html = self.body("doctor_admin_dashboard")

        self.assertNotIn("doctor-profile-sidebar", html)
        self.assertNotIn("doctor-avatar-sidebar", html)
        # The class also appears once in the stylesheet, which is why this
        # counts the attribute rather than the name.
        self.assertEqual(html.count('class="user-avatar-top"'), 1)

    def test_an_account_with_no_name_does_not_render_a_blank(self):
        """The block that was removed printed `get_full_name` with no
        fallback; what replaces it falls back to the username."""
        nameless = make_staff_user(
            username="chrome-nameless", email="chrome-nameless@example.com",
            password=PASSWORD, role=User.Role.DOCTOR,
        )

        html = self.body("doctor_admin_dashboard", user=nameless)

        # Both places the chrome names them, checked separately: the fallback
        # was missing from one of the two and the other covered for it.
        self.assertIn('<span class="user-menu-name">chrome-nameless</span>', html)
        self.assertIn("<strong>chrome-nameless</strong>", html)

    def test_the_role_is_shown_by_its_label_not_its_stored_value(self):
        html = self.body("doctor_admin_dashboard")

        self.assertIn(self.doctor.get_role_display(), html)

    def test_the_script_calls_nothing_that_does_not_exist(self):
        """`setInterval(updateStats, 60000)` referenced a function defined
        nowhere. Evaluating the name threw, and every statement after it - the
        resize handler, every tooltip, both keyboard shortcuts - never ran."""
        # Comments are stripped first: the block that replaced this code names
        # every function it removed, and a test that reads prose passes on the
        # explanation rather than on the code.
        code = chr(10).join(
            line for line in self.body("doctor_admin_dashboard").splitlines()
            if not line.strip().startswith(("//", "*", "/*", "{#"))
        )

        for gone in ("updateStats", "printAppointments", "checkPendingAppointments"):
            with self.subTest(function=gone):
                self.assertNotIn(gone, code)

    def test_the_sidebar_state_is_remembered(self):
        html = self.body("doctor_admin_dashboard")

        self.assertIn("agki-sidebar-collapsed", html)

    def test_the_layout_has_a_step_between_desktop_and_phone(self):
        """Two breakpoints meant a 1024px laptop ran a 260px sidebar beside a
        squeezed table, and a tablet got the desktop layout."""
        html = self.body("doctor_admin_dashboard")

        self.assertIn("@media (max-width: 1199px) and (min-width: 992px)", html)
        self.assertIn("@media (max-width: 991px)", html)
        self.assertIn("@media (max-width: 576px)", html)
        self.assertIn("@media (prefers-reduced-motion: reduce)", html)

    def test_keyboard_users_can_get_past_the_navigation(self):
        html = self.body("doctor_admin_dashboard")

        self.assertIn('class="skip-link" href="#main-content"', html)
        self.assertIn('id="main-content"', html)

    def test_the_sidebar_carries_the_agki_mark_not_a_stock_icon(self):
        """`fa-heartbeat` is a Font Awesome icon belonging to no particular
        clinic. The console shows the same mark as the public navbar."""
        html = self.body("doctor_admin_dashboard")

        self.assertNotIn("fa-heartbeat", html)
        self.assertIn('class="sidebar-logo-mark"', html)
        self.assertIn(static("app/logo.png"), html)

    def test_the_mark_follows_the_logo_an_administrator_uploaded(self):
        """Same resolution as the public site: the uploaded file wins, and the
        bundled one is only the fallback."""
        site = SiteSettings.load()
        site.logo = SimpleUploadedFile(
            "uploaded-logo.png", b64decode(PNG_B64), content_type="image/png"
        )
        site.save()
        self.addCleanup(site.logo.delete, save=True)

        html = self.body("doctor_admin_dashboard")

        self.assertIn("uploaded-logo", html)
        self.assertNotIn(static("app/logo.png"), html)

    def test_the_collapse_arrow_turns_round_when_it_collapses(self):
        """It kept pointing left after collapsing, which reads as "collapse
        further" on a sidebar that has nowhere further to go."""
        html = self.body("doctor_admin_dashboard")
        # The declaration read out of its own rule: `rotate(180deg)` appears a
        # second time in the icon-rail breakpoint, and asserting on the two
        # strings separately let the rule keep the selector and lose the turn.
        rule = html.split(".sidebar.collapsed .collapse-toggle i {", 1)[1]
        rule = rule.split("}", 1)[0]

        self.assertIn("transform: rotate(180deg)", rule)
        self.assertIn("transition: transform", html)

    def test_the_drawer_opens_full_width_whatever_was_remembered(self):
        """The collapsed state persists across pages now, and a remembered
        70px rail would open the phone drawer as unlabelled icons."""
        html = self.body("doctor_admin_dashboard")
        phone = html.split("@media (max-width: 991px)", 1)[1].split("@media", 1)[0]

        self.assertIn("width: var(--sidebar-width)", phone)


"""Tests for account routing after sign-in.

These exist because of a reported bug: a Content Editor signing in landed on
the patient dashboard instead of the admin. The cause was three sign-in
entry points each deciding the destination separately, and all three getting
it wrong — `signin` and `otp_view` sent *every* account to the doctor
dashboard, while `LOGIN_REDIRECT_URL` sent social logins to the patient one.
None of them looked at `is_staff`.

`post_login_redirect` is now the single place that decides, so these tests
cover the matrix it is responsible for.
"""

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from cms.test_utils import make_staff_user

User = get_user_model()

PASSWORD = "test-pass-12345"


class PostLoginRedirectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = make_staff_user(username="editor", email="editor@example.com",
                                     password=PASSWORD, is_staff=True)
        cls.editor.groups.add(Group.objects.get(name="Content Editors"))

        cls.doctor = make_staff_user(username="doctor", email="doctor@example.com",
                                     password=PASSWORD, role=User.Role.DOCTOR)
        # Mirrors the real `kulwa_cherehani`: the lab administrator, staff but
        # not a superuser. She used to land on /admin/ and never reach the
        # console she administers (design §4.1.2, plan 1.9).
        cls.lab_admin = make_staff_user(username="labadmin", email="labadmin@example.com",
                                        password=PASSWORD, role=User.Role.ADMIN,
                                        is_staff=True)
        cls.receptionist = make_staff_user(username="recept", email="recept@example.com",
                                           password=PASSWORD, role=User.Role.RECEPTIONIST)
        cls.patient = make_staff_user(username="patient", email="patient@example.com",
                                      password=PASSWORD)
        cls.doctor_who_is_staff = make_staff_user(username="docstaff", email="docstaff@example.com",
                                                  password=PASSWORD, role=User.Role.DOCTOR,
                                                  is_staff=True)

        cls.superuser = User.objects.create_superuser(
            username="root", email="root@example.com", password=PASSWORD
        )
        cls.superuser.is_active = True
        cls.superuser.save()

        cls.expected = (
            ("content editor", cls.editor, "/admin/"),
            ("superuser", cls.superuser, "/admin/"),
            ("doctor", cls.doctor, "/doctorDashboard/"),
            ("lab administrator", cls.lab_admin, "/doctorDashboard/"),
            ("doctor who is also staff", cls.doctor_who_is_staff, "/doctorDashboard/"),
            ("receptionist", cls.receptionist, "/useraccount/dashboard/"),
            ("patient", cls.patient, "/useraccount/dashboard/"),
        )

    def test_each_account_type_lands_in_the_right_place(self):
        for label, user, destination in self.expected:
            with self.subTest(account=label):
                self.client.force_login(user)
                response = self.client.get(reverse("post_login_redirect"))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], destination)

    def test_the_sign_in_form_routes_through_the_same_decision(self):
        """Regression guard: `signin` used to send everyone, including
        content editors and superusers, to the doctor dashboard."""
        for label, user, destination in self.expected:
            with self.subTest(account=label):
                client = self.client_class()
                response = client.post("/useraccount/signin/",
                                       {"username": user.email, "password1": PASSWORD})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse("post_login_redirect"))
                self.assertEqual(client.get(response["Location"])["Location"], destination)

    def test_login_redirect_url_setting_points_at_the_decision(self):
        """Allauth/Google and any `@login_required` bounce use this. It used
        to name the patient dashboard directly.

        Note this reads the *effective* setting, so it also catches an `.env`
        override putting the old value back — which is exactly how the
        original fix was found to be inert.
        """
        from django.conf import settings

        self.assertEqual(reverse(settings.LOGIN_REDIRECT_URL), reverse("post_login_redirect"))

    def test_doctors_are_routed_by_role_not_by_staff_flag(self):
        """A clinician who also carries a staff flag belongs on their own
        dashboard, so `role` is checked before `is_staff`."""
        self.assertTrue(self.doctor_who_is_staff.is_staff)
        self.client.force_login(self.doctor_who_is_staff)
        self.assertEqual(
            self.client.get(reverse("post_login_redirect"))["Location"],
            "/doctorDashboard/",
        )

    def test_anonymous_visitors_are_sent_to_sign_in(self):
        response = self.client.get(reverse("post_login_redirect"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/useraccount/signin/", response["Location"])

    def test_staff_can_actually_open_the_admin_they_are_sent_to(self):
        """Routing someone somewhere they'd be bounced from would only move
        the bug."""
        for label, user in (("content editor", self.editor), ("superuser", self.superuser)):
            with self.subTest(account=label):
                self.client.force_login(user)
                self.assertEqual(self.client.get("/admin/").status_code, 200)


class UrlNameCollisionTests(TestCase):
    """Three apps once registered the URL name `home`, and Django resolves a
    duplicate to the last one included — so `{% url 'home' %}` site-wide
    pointed at the shop rather than the marketing homepage. That broke the
    "Home" links in useraccount's own pages and the admin's "View live site".
    """

    def test_home_is_the_marketing_homepage(self):
        self.assertEqual(reverse("home"), "/")

    def test_the_other_two_keep_their_own_names(self):
        self.assertEqual(reverse("ecommerce_home"), "/ecommerce/home")
        self.assertEqual(reverse("useraccount_home"), "/useraccount/")

    def test_signin_resolves_to_the_working_one(self):
        self.assertEqual(reverse("signin"), "/useraccount/signin/")


class ApprovalGateTests(TestCase):
    """`is_approved` actually gates sign-in now (plan 1.10, design §4.2 #4).

    The flag has existed since a migration whose own comment says it was
    "added so as to add admin approval to login", and the staff console has
    offered a button to toggle it — but `signin` only ever checked
    `is_active`, so the button controlled nothing. An admin could
    "unapprove" someone and watch them keep signing in.
    """

    @classmethod
    def setUpTestData(cls):
        cls.approved = make_staff_user(username="approved", email="approved@example.com",
                                       password=PASSWORD, role=User.Role.DOCTOR)
        cls.pending = make_staff_user(username="pending", email="pending@example.com",
                                      password=PASSWORD, role=User.Role.DOCTOR)
        User.objects.filter(pk=cls.pending.pk).update(is_approved=False)
        cls.pending.refresh_from_db()

    def sign_in(self, user):
        return self.client.post("/useraccount/signin/",
                                {"username": user.email, "password1": PASSWORD})

    def test_an_approved_account_still_signs_in(self):
        response = self.sign_in(self.approved)

        self.assertEqual(response["Location"], reverse("post_login_redirect"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.approved.pk)

    def test_an_unapproved_account_cannot_sign_in(self):
        response = self.sign_in(self.pending)

        self.assertEqual(response["Location"], "/useraccount/signin/")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_message_says_what_is_wrong(self):
        """"Incorrect password" would send someone to reset a password that
        was never the problem."""
        response = self.sign_in(self.pending)
        messages = [str(m) for m in get_messages(response.wsgi_request)]

        self.assertTrue(any("pending admin approval" in m for m in messages), messages)

    def test_a_wrong_password_is_still_reported_as_a_wrong_password(self):
        """The approval state must not leak to someone who cannot even
        authenticate."""
        response = self.client.post("/useraccount/signin/",
                                    {"username": self.pending.email, "password1": "wrong"})
        messages = [str(m) for m in get_messages(response.wsgi_request)]

        self.assertTrue(any("Incorrect password" in m for m in messages), messages)
        self.assertFalse(any("approval" in m for m in messages), messages)

    def test_the_gate_lives_in_the_backend_not_only_in_the_view(self):
        """`signin` was the only caller that checked anything, so any second
        entry point inherited a gate that was not there."""
        self.assertIsNone(authenticate(username=self.pending.email, password=PASSWORD))
        self.assertIsNotNone(authenticate(username=self.approved.email, password=PASSWORD))

    def test_an_inactive_account_is_still_refused_and_told_why(self):
        User.objects.filter(pk=self.approved.pk).update(is_active=False)

        response = self.sign_in(self.approved)
        messages = [str(m) for m in get_messages(response.wsgi_request)]

        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(any("not verified" in m for m in messages), messages)

    def test_unapproving_an_account_ends_its_existing_session(self):
        """Otherwise "revoke access" would mean "revoke access at their next
        login", which is not what an admin clicking it expects."""
        self.client.force_login(self.approved)
        self.assertEqual(self.client.get(reverse("post_login_redirect")).status_code, 302)

        User.objects.filter(pk=self.approved.pk).update(is_approved=False)

        bounced = self.client.get(reverse("post_login_redirect"))
        self.assertIn("/useraccount/signin/", bounced["Location"])

    def test_a_new_superuser_is_approved_automatically(self):
        """`is_approved` defaults to False, so without this `createsuperuser`
        would produce an account that cannot use the site's own login form —
        a footgun with no upside, since is_superuser already means
        everything (design §4.1)."""
        root = User.objects.create_superuser(
            username="fresh-root", email="fresh-root@example.com", password=PASSWORD,
        )

        self.assertTrue(root.is_approved)
        self.assertEqual(self.sign_in(root)["Location"], reverse("post_login_redirect"))

    def test_an_ordinary_new_account_is_not_approved_automatically(self):
        """Approval is a decision a human makes; self-signup must not grant
        it."""
        signee = User.objects.create_user(
            username="self-signup", email="self-signup@example.com", password=PASSWORD,
        )

        signee.refresh_from_db()
        self.assertFalse(signee.is_approved)

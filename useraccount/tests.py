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

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
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

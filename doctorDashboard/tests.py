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

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

import doctorDashboard.urls as dashboard_urls
from cms.test_utils import make_staff_user
from ecommerce.models import Category, Item

User = get_user_model()

PASSWORD = "test-pass-12345"

# Stand-in values for captured URL parameters. A new view whose URL captures
# something not listed here raises KeyError in `dashboard_routes()` and fails
# every test in this module — deliberately. Adding a route to the staff
# console should require a decision about who may reach it, and this is where
# that decision gets made.
SAMPLE_URL_ARGS = {
    "appointment_id": 1,
    "doctor_id": 1,
    "item_id": 1,
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
ADMIN_ONLY = {"doctors", "activate_deactivate_doctor"}
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

    def test_activate_deactivate_cannot_be_posted_anonymously(self):
        """Reproduced before this phase: is_approved False -> True with no
        session at all."""
        target = make_staff_user(username="victim", email="victim@example.com")
        User.objects.filter(pk=target.pk).update(is_approved=False)

        response = self.client.post(
            reverse("activate_deactivate_doctor", kwargs={"doctor_id": target.pk})
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
        self.client.force_login(self.doctor)
        response = self.client.get(reverse("doctor_admin_dashboard"))
        self.assertContains(response, 'class="doctor-avatar-sidebar"')
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

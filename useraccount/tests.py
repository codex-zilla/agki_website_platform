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
from datetime import date, time, timedelta

from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import caches
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cms.test_utils import make_staff_user
from useraccount import otp as verification
from useraccount.notices import ACCESS_WITHDRAWN
from useraccount.models import (
    OtpToken,
    Profile,
    Qualification,
    Specialty,
    generate_otp,
)

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
        cls.withdrawn = make_staff_user(username="withdrawn", email="withdrawn@example.com",
                                      password=PASSWORD, role=User.Role.DOCTOR)
        User.objects.filter(pk=cls.withdrawn.pk).update(is_approved=False)
        cls.withdrawn.refresh_from_db()

    def sign_in(self, user):
        return self.client.post("/useraccount/signin/",
                                {"username": user.email, "password1": PASSWORD})

    def test_an_approved_account_still_signs_in(self):
        response = self.sign_in(self.approved)

        self.assertEqual(response["Location"], reverse("post_login_redirect"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.approved.pk)

    def test_an_unapproved_account_cannot_sign_in(self):
        response = self.sign_in(self.withdrawn)

        self.assertEqual(response["Location"], "/useraccount/signin/")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_message_says_what_is_wrong(self):
        """"Incorrect password" would send someone to reset a password that
        was never the problem.

        Asserted against the constant rather than a phrase, since plan 10.5:
        the sentence itself changed — it used to say "pending admin approval",
        which under decision F told a switched-off account to wait for
        something that will never happen — and a test spelling the wording out
        a second time is a test that has to be edited every time the copy is
        improved. What is worth pinning is that *this* refusal is explained,
        not that it is explained in these words.
        """
        response = self.sign_in(self.withdrawn)
        messages = [str(m) for m in get_messages(response.wsgi_request)]

        self.assertIn(ACCESS_WITHDRAWN, messages)

    def test_a_wrong_password_is_still_reported_as_a_wrong_password(self):
        """The approval state must not leak to someone who cannot even
        authenticate."""
        response = self.client.post("/useraccount/signin/",
                                    {"username": self.withdrawn.email, "password1": "wrong"})
        messages = [str(m) for m in get_messages(response.wsgi_request)]

        self.assertTrue(any("Incorrect password" in m for m in messages), messages)
        self.assertNotIn(ACCESS_WITHDRAWN, messages)

    def test_the_gate_lives_in_the_backend_not_only_in_the_view(self):
        """`signin` was the only caller that checked anything, so any second
        entry point inherited a gate that was not there."""
        self.assertIsNone(authenticate(username=self.withdrawn.email, password=PASSWORD))
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
        """Kept after the signal that used to guarantee this was deleted.

        Plan 1.10 added a `createsuperuser` special case to
        `useraccount.signals` because `is_approved` defaulted to False and a
        fresh superuser would otherwise have been unable to use the site's own
        login form. Decision F flips the default, so plan 10.1 removed the
        special case — but the *outcome* is what is worth guarding, and it is
        now guaranteed by a default instead of by a handler. Deleting this
        test alongside the code would have left nothing watching either."""
        root = User.objects.create_superuser(
            username="fresh-root", email="fresh-root@example.com", password=PASSWORD,
        )

        self.assertTrue(root.is_approved)
        self.assertEqual(self.sign_in(root)["Location"], reverse("post_login_redirect"))

    def test_an_ordinary_new_account_is_approved_automatically(self):
        """Inverted by decision F (plan 10.1).

        This read "approval is a decision a human makes; self-signup must not
        grant it", which was right while AGKI created every account. Doctors
        register themselves now (design §0.1), so approval is granted at
        registration and `is_approved` becomes the switch that takes access
        away again.

        The gate that proves something about the registrant is untouched: the
        same signal still deactivates a public self-signup and emails a
        one-time code, so this account is approved **and inactive** until the
        address is verified. Approved is not the same as able to sign in, and
        a test implying otherwise would be false reassurance — hence the
        second assertion."""
        signee = User.objects.create_user(
            username="self-signup", email="self-signup@example.com", password=PASSWORD,
        )

        signee.refresh_from_db()
        self.assertTrue(signee.is_approved)
        self.assertFalse(
            signee.is_active,
            "email verification is a separate gate and must survive 10.1",
        )


class FixtureLoadingTests(TestCase):
    """Restoring a backup must not treat the accounts in it as new signups.

    Found by rehearsing the SQLite-to-MySQL move rather than by any test:
    `loaddata` saves rows, and `useraccount.signals` treated every newly saved
    account as a public self-signup — forcing `is_active=False` and generating
    a verification code. The import came out with five of six real accounts
    deactivated, and with production SMTP configured would have emailed five
    people a code for an account they already had.

    Django sets `raw=True` on a fixture save so handlers can tell the
    difference. These tests are here because nothing else would notice: the
    import reports success either way.
    """

    def load_fixture(self, payload):
        import json
        import tempfile
        from pathlib import Path

        directory = tempfile.mkdtemp(prefix="agki-fixture-")
        path = Path(directory) / "accounts.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        call_command("loaddata", str(path), verbosity=0)
        return path

    def account_fixture(self, **overrides):
        from django.contrib.auth.hashers import make_password

        fields = {
            "username": "restored",
            "email": "restored@example.com",
            # A **usable** hash, as every real account in the backup has. This
            # detail is the test: an unusable password sends the signal down
            # its social-login branch, which activates the account instead of
            # deactivating it — so a fixture with `!unusable` here passes
            # whether the guard exists or not. Mutation-checking caught that;
            # the first version of this test proved nothing.
            "password": make_password("irrelevant-but-usable"),
            "is_active": True,
            "is_staff": False,
            "is_superuser": False,
            "is_approved": True,
            "role": "doctor",
            "date_joined": "2026-01-01T00:00:00Z",
            "first_name": "",
            "last_name": "",
        }
        fields.update(overrides)
        return [{"model": "useraccount.customuser", "pk": 4242, "fields": fields}]

    def test_a_restored_account_keeps_its_active_flag(self):
        self.load_fixture(self.account_fixture())

        restored = User.objects.get(username="restored")
        self.assertTrue(restored.is_active, "the import deactivated a real account")

    def test_a_restored_account_keeps_its_approval(self):
        self.load_fixture(self.account_fixture())

        self.assertTrue(User.objects.get(username="restored").is_approved)

    def test_restoring_an_account_sends_nobody_a_verification_code(self):
        from django.core import mail

        mail.outbox = []

        self.load_fixture(self.account_fixture())

        self.assertEqual(mail.outbox, [])
        self.assertEqual(
            OtpToken.objects.filter(user__username="restored").count(), 0
        )

    def test_a_backup_carrying_its_own_profiles_loads(self):
        """A real dump contains Profile rows as well as accounts.

        Without the guard the signal creates a profile the moment the account
        row lands, and the fixture's own profile — a different primary key for
        the same user — then collides with it on the OneToOne. The import
        fails outright, which is at least loud; this keeps it from happening.
        """
        payload = self.account_fixture()
        payload.append(
            {
                "model": "useraccount.profile",
                "pk": 9999,
                "fields": {"user": 4242, "expertise": "Cardiology"},
            }
        )

        self.load_fixture(payload)

        profiles = Profile.objects.filter(user__username="restored")
        self.assertEqual(profiles.count(), 1)
        # The restored one, not a blank one the signal invented.
        self.assertEqual(profiles.get().expertise, "Cardiology")

    def test_an_ordinary_signup_still_gets_the_verification_it_needs(self):
        """The other half: the guard must not switch the real flow off."""
        User.objects.create_user(
            username="real-signup", email="real-signup@example.com",
            password="test-pass-12345",
        )

        signee = User.objects.get(username="real-signup")
        self.assertFalse(signee.is_active)
        self.assertEqual(OtpToken.objects.filter(user=signee).count(), 1)

    def test_an_ordinary_new_account_still_gets_a_profile(self):
        """And the other half of the profile guard."""
        User.objects.create_user(
            username="profile-signup", email="profile-signup@example.com",
            password="test-pass-12345",
        )

        self.assertTrue(Profile.objects.filter(user__username="profile-signup").exists())
class GoogleRegistrationTests(TestCase):
    """The social signup path — plan 10.4, design §0.1.

    **Nothing in this suite touched `SocialAccountAdapter` before now**, which
    is why the bug this phase fixes was invisible to it: a Google registration
    produced an account with `role=''`, and a role-less account is answered
    403 by every dashboard route (plan 1.3). The registrant arrived signed in,
    approved, and locked out — a state no test was watching for and no error
    message described.

    These drive the adapter directly rather than the whole OAuth round-trip.
    The provider hand-off is Google's code and allauth's; what belongs to this
    project is the two hooks it calls, and those are what is asserted here.
    """

    def request(self):
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        return request

    def social_signup(self, email="new-doctor@example.com", username="new-doctor"):
        """What allauth does when a Google account has no user behind it."""
        from allauth.socialaccount.models import SocialAccount, SocialLogin

        from useraccount.adapters import SocialAccountAdapter

        sociallogin = SocialLogin(
            user=User(username=username, email=email),
            account=SocialAccount(
                provider="google", uid="google-uid-" + username,
                extra_data={"email": email},
            ),
        )
        return SocialAccountAdapter().save_user(self.request(), sociallogin)

    def test_a_google_registration_is_a_doctor(self):
        """Only doctors self-register (decision F), so there is one meaning to
        read off a new Google account and the server reads it — rather than
        asking, which would put `role` back in a request body."""
        user = self.social_signup()

        self.assertEqual(User.objects.get(pk=user.pk).role, User.Role.DOCTOR)

    def test_a_google_registration_reaches_the_doctor_console(self):
        """The failure 10.4 exists to fix, stated as its outcome rather than
        as the field that caused it. With `role` empty this redirect sent the
        new doctor to `/admin/` (or the patient dashboard), and every console
        route answered them 403."""
        user = self.social_signup()

        self.client.force_login(user)
        landing = self.client.get(reverse("post_login_redirect"))

        self.assertEqual(landing["Location"], reverse("doctor_admin_dashboard"))
        self.assertEqual(
            self.client.get(reverse("doctor_admin_dashboard")).status_code, 200
        )

    def test_a_google_registration_can_sign_in_at_once(self):
        """Both gates clear together on this route: Google has verified the
        address, so there is no one-time code to wait for, and decision F
        grants approval. Asserted together because either alone would leave
        the account unable to sign in and the test still passing.

        Being precise about what each half is worth, since the mutation
        check measured it: `is_approved` follows from decision F and this
        fails if that is reverted. `is_active` follows from the field
        default — no code activates a social signup, the form branch just
        never deactivates one — so that half is a characterisation rather
        than a guard on anything written. It earns its place by catching a
        future decision to hold social registrations for review, which would
        otherwise strand every Google doctor silently."""
        user = User.objects.get(pk=self.social_signup().pk)

        self.assertTrue(user.is_active)
        self.assertTrue(user.is_approved)

    def test_an_existing_account_keeps_its_own_role(self):
        """The assumption `save_user` rests on, tested rather than trusted.

        `pre_social_login` is the hook that runs when the Google address
        already belongs to somebody — the lab administrator, or a content
        editor. If a role were assigned there too, connecting Google would
        silently demote her to a doctor and hand her a different console.
        """
        from allauth.socialaccount.models import SocialAccount, SocialLogin

        from useraccount.adapters import SocialAccountAdapter

        administrator = User.objects.create_user(
            username="lab-admin", email="lab-admin@example.com", password=PASSWORD,
        )
        User.objects.filter(pk=administrator.pk).update(role=User.Role.ADMIN)
        administrator.refresh_from_db()

        account = SocialAccount.objects.create(
            provider="google", uid="google-uid-lab-admin", user=administrator,
            extra_data={"email": administrator.email},
        )
        sociallogin = SocialLogin(user=administrator, account=account)
        SocialAccountAdapter().pre_social_login(self.request(), sociallogin)

        administrator.refresh_from_db()
        self.assertEqual(administrator.role, User.Role.ADMIN)

    def test_a_google_account_does_not_need_an_emailed_code(self):
        """The form path deactivates a self-signup and emails an OTP; Google
        has already done that job. A code generated here would be a second
        verification of an address that is verified, and the account would sit
        inactive waiting for an email nobody told the registrant to expect."""
        user = self.social_signup()

        self.assertEqual(OtpToken.objects.filter(user=user).count(), 0)
class ApprovalCopyTests(TestCase):
    """Plan 10.5 — no page may tell anyone to wait for approval.

    Decision F removed the waiting. What it could not remove is the copy
    describing it, which was spread across a login message said in two places,
    a whole post-signup page, an FAQ, a dashboard stat card and three
    JavaScript functions that *simulated* an administrator approving the
    account on a timer.

    The scanning test below is modelled on `CopyDisciplineTests` in
    `appointments`, which stops the removed SMS promise coming back. Same
    reasoning: a promise nobody keeps costs nothing to re-add and is invisible
    until somebody acts on it, and the person acting on it here is a doctor
    sitting still, waiting to be let in, on an account that has been usable the
    whole time.
    """

    #: Phrases that only make sense if somebody is working an approval queue.
    #: Deliberately not the bare word "approval" — `pre_approve_notice` is
    #: allowed to say access *can be withdrawn*, and the console's "Approved
    #: for login" toggle is the control that does it. What is banned is the
    #: promise of a pending review.
    WAITING_PHRASES = (
        "pending admin approval",
        "awaiting approval",
        "await approval",
        "once it has been reviewed",
        "will review your registration",
        "approval email",
        "upon approval",
        "after approval",
        "how long does approval take",
        "check approval status",
    )

    def test_no_page_tells_anyone_to_wait_for_approval(self):
        from pathlib import Path

        from django.conf import settings

        offenders = []
        for app in ("useraccount", "doctorDashboard"):
            root = Path(settings.BASE_DIR) / app / "templates"
            for path in root.rglob("*.html"):
                text = path.read_text(encoding="utf-8")
                for number, line in enumerate(text.splitlines(), start=1):
                    lowered = line.lower()
                    for phrase in self.WAITING_PHRASES:
                        if phrase in lowered:
                            offenders.append(
                                f"{path.name}:{number}: {line.strip()[:70]}"
                            )

        self.assertEqual(
            offenders,
            [],
            "These pages promise an approval step that no longer exists: "
            + " | ".join(offenders),
        )

    def test_the_post_signup_page_says_the_account_is_usable(self):
        """The page a verified registrant is actually sent to.

        `verify_email` redirected here saying "you can now log in" and the page
        then said the opposite — an admin review, an approval email, a 48-hour
        wait. The contradiction predates decision F; decision F settled which
        half was true.
        """
        response = self.client.get(reverse("pre_approve_notice"))

        self.assertContains(response, "Your account is ready")
        self.assertContains(response, reverse("signin"))

    def test_the_page_does_not_simulate_being_approved(self):
        """Three demo functions faked the approval that never comes.

        They were reachable code in a served template — one bound to a button
        that rendered "Approved! You can now log in" after two seconds. Plan
        4.2a is the precedent: commented-out demo content is still sent to the
        browser, and this suite has caught it putting removed copy back on a
        page once already.
        """
        from pathlib import Path

        from django.conf import settings

        source = (
            Path(settings.BASE_DIR)
            / "useraccount" / "templates" / "useraccount" / "pre_approve_notice.html"
        ).read_text(encoding="utf-8")

        for simulation in ("updateTimelineStatus", "simulateEmailCheck", "startCountdown"):
            self.assertNotIn(simulation, source)


class WithdrawnAccountMessageTests(TestCase):
    """One sentence, said by both login paths (plan 10.5).

    `signin` and `AccountAdapter.pre_login` explain the same refusal, and held
    two copies of it — which is how both were wrong at once when decision F
    changed what the flag means. `ApprovalGateTests` covers the form; this
    covers allauth's path, which is the one a doctor who registered with Google
    arrives on and which nothing tested before.
    """

    def allauth_kwargs(self):
        """What allauth itself passes. `pre_login` takes five keyword-only
        arguments, so calling it with two would test a signature this code is
        never invoked with — and would keep passing if allauth changed it."""
        from allauth.account import app_settings

        return {
            "email_verification": app_settings.EMAIL_VERIFICATION,
            "signal_kwargs": None,
            "email": None,
            "signup": False,
            "redirect_url": None,
        }

    def test_the_allauth_path_says_access_was_withdrawn(self):
        from django.contrib.messages.middleware import MessageMiddleware
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from useraccount.adapters import AccountAdapter

        user = User.objects.create_user(
            username="switched-off", email="switched-off@example.com",
            password=PASSWORD,
        )
        # `is_active` is set explicitly because the signup signal switches it
        # off to hold a form registration for its emailed code, and allauth
        # checks `is_active` *before* this adapter gets a say — an inactive
        # fixture is answered `/accounts/inactive/` and never reaches the line
        # under test. The account this test is about is active and withdrawn,
        # which is a different account from one that is merely unverified.
        User.objects.filter(pk=user.pk).update(is_active=True, is_approved=False)
        user.refresh_from_db()

        request = RequestFactory().get("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)

        response = AccountAdapter().pre_login(request, user, **self.allauth_kwargs())

        self.assertIsNotNone(response, "an unapproved account was allowed through")
        self.assertIn(reverse("signin"), response["Location"])
        self.assertIn(ACCESS_WITHDRAWN, [str(m) for m in get_messages(request)])

    def test_an_approved_account_is_not_stopped(self):
        """The other side, so the gate cannot be "refuse everybody"."""
        from django.test import RequestFactory

        from useraccount.adapters import AccountAdapter

        user = User.objects.create_user(
            username="allowed", email="allowed@example.com", password=PASSWORD,
        )
        # Explicit for the same reason, and because relying on the in-memory
        # instance still reading `is_active=True` — the signal updates the row,
        # not the object — would make this pass for a reason that has nothing
        # to do with what it claims to check.
        User.objects.filter(pk=user.pk).update(is_active=True)
        user.refresh_from_db()

        self.assertIsNone(
            AccountAdapter().pre_login(
                RequestFactory().get("/"), user, **self.allauth_kwargs()
            )
        )

    def test_neither_login_path_holds_its_own_copy_of_the_sentence(self):
        """The duplication that let the wording rot in two places at once.

        **Commented-out lines are excluded, and that is a concession rather
        than a principle.** `views.py` still carries a large commented-out
        alternative `signin` from the OTP experiment, and it contains the old
        sentence. Nothing serves it to anybody, so it is not 10.5's business
        — but plan 4.2a is the reminder that "it is only a comment" has been
        wrong in this codebase before, in a template. The dead block is
        recorded as `useraccount` debris in the plan's open risks.
        """
        from pathlib import Path

        from django.conf import settings

        for module in ("views.py", "adapters.py"):
            source = (
                Path(settings.BASE_DIR) / "useraccount" / module
            ).read_text(encoding="utf-8")
            live = [
                line for line in source.splitlines()
                if not line.strip().startswith("#")
            ]
            self.assertNotIn(
                "pending admin approval", chr(10).join(live).lower(),
                f"{module} still spells out an approval-wait message",
            )
class DecisionFEndToEndTests(TestCase):
    """The Phase 10 gate sentence, executed.

    "A doctor who has never spoken to AGKI registers, verifies their email,
    signs in, publishes availability and appears on the public search — and an
    administrator can take all of it away with one toggle, ending the session
    and removing the listing at the same moment."

    Every step of that has its own unit test somewhere in this suite. This runs
    them as one sequence, through the real views, because the phase's whole
    claim is that the steps join up — and the bug 10.4 fixed was precisely a
    join that did not: each part worked and the doctor still ended up signed
    in, approved and locked out. Following the pattern of Phase 4's
    `test_the_whole_negotiation`.
    """

    def setUp(self):
        from appointments.models import AppointmentSettings

        AppointmentSettings.load()
        self.administrator = make_staff_user(
            username="lab-boss", email="lab-boss@example.com",
            role=User.Role.ADMIN,
        )

    def register(self):
        self.client.post(reverse("signup"), {
            "username": "newcomer", "firstname": "Zawadi", "lastname": "Kimaro",
            "email": "zawadi@example.com", "phone": "0715111222",
            "password1": PASSWORD, "password2": PASSWORD,
        })
        return User.objects.get(username="newcomer")

    def test_the_whole_of_decision_f(self):
        from appointments.models import AvailableSlot

        # 1. A stranger registers. Approved on creation (decision F), and
        #    inactive until the emailed code is used — the gate that survived.
        doctor = self.register()
        self.assertEqual(doctor.role, User.Role.DOCTOR)
        self.assertTrue(doctor.is_approved)
        self.assertFalse(doctor.is_active, "email verification stopped being a gate")

        # 2. They verify their email. Nothing else stands between them and the
        #    console — which is the sentence the old post-signup page denied.
        code = OtpToken.objects.filter(user=doctor).last().otp_code
        self.client.post(reverse("verify-email", args=[doctor.username]),
                         {"otp_code": code})
        doctor.refresh_from_db()
        self.assertTrue(doctor.is_active)

        # 3. They sign in, and land on the console rather than on /admin/ or
        #    the patient dashboard.
        self.client.post(reverse("signin"),
                         {"username": doctor.email, "password1": PASSWORD})
        landing = self.client.get(reverse("post_login_redirect"))
        self.assertEqual(landing["Location"], reverse("doctor_admin_dashboard"))

        # 4. They publish a time.
        when = timezone.localdate() + timedelta(days=3)
        self.client.post(reverse("add_doctor_availability"),
                         {"date": when.isoformat(), "time": "09:00",
                          # A published time now says how the doctor will work
                          # it as well as when (plan 12.1), and at least one
                          # way is required.
                          "offers_online": "1",
                          "offers_patient_travels": "1"})
        slot = AvailableSlot.objects.get(user=doctor, date=when)

        # 5. A patient can now find and book them. The list names the doctor
        #    and says they are bookable; the times themselves live on the
        #    doctor's own page since plan 11.15, so the booking link is read
        #    from there.
        listed = self.client.get(reverse("search_results"))
        self.assertContains(listed, "Zawadi Kimaro")
        self.assertContains(listed, "Bookable")
        self.assertContains(
            self.client.get(reverse("doctor_detail", args=[doctor.id])),
            reverse("booking_form", args=[slot.id]),
        )

        # 6. An administrator withdraws access with one toggle.
        console = Client()
        console.force_login(self.administrator)
        console.post(reverse("set_doctor_status", args=[doctor.pk]),
                     {"flag": "approved", "value": "off"})
        doctor.refresh_from_db()
        self.assertFalse(doctor.is_approved)

        # 7. The doctor's **existing session** ends — not just their next
        #    login. An administrator clicking "withdraw" expects the person to
        #    be out now, and `EmailBackend.get_user` is what makes that true.
        self.assertIn("/useraccount/signin/",
                      self.client.get(reverse("doctor_admin_dashboard"))["Location"])

        # 8. And the listing goes with it, in the same moment — the half that
        #    makes this a control rather than a login change (10.3).
        public = Client()
        self.assertNotContains(public.get(reverse("search_results")), "Zawadi Kimaro")
        self.assertRedirects(
            public.get(reverse("booking_form", args=[slot.id])),
            reverse("search_results"),
        )
class SignupVerificationGateTests(TestCase):
    """The emailed code has to gate something (found by the Phase 10 gate).

    `create_user` fires the signal in `useraccount.signals`, which holds a
    public signup by writing `is_active=False` to the row. `signup` then set
    the name, phone and role on the same in-memory instance and called a plain
    `.save()` — writing `is_active=True` back over it, because the signal
    updated the row and not the object. The account came out **active**, so the
    OTP was generated, emailed, and gated nothing: a registrant could sign in
    without ever opening the message.

    **Nothing in this suite could see it.** The existing signal test calls
    `create_user` directly and stops one line before the call that undid its
    work; every other test that needed a user made one directly too. It took a
    test that went through the actual form to reach the bug — which is the
    argument for end-to-end tests that this project otherwise makes about
    booking (plan 4.x).

    It matters more under decision F than it would have before: approval is
    granted at registration now, so controlling the email address is the only
    thing a registrant has to prove (design §4.5), and this was the line that
    stopped them having to prove even that.
    """

    PAYLOAD = {
        "username": "unverified", "firstname": "Neema", "lastname": "Said",
        "email": "unverified@example.com", "phone": "0715000111",
        "password1": PASSWORD, "password2": PASSWORD,
    }

    def sign_up(self):
        self.client.post(reverse("signup"), self.PAYLOAD)
        return User.objects.get(username="unverified")

    def test_the_form_leaves_the_account_inactive(self):
        self.assertFalse(self.sign_up().is_active)

    def test_the_form_still_records_what_it_was_given(self):
        """The fix narrows the save to named fields, so the obvious way to get
        it wrong is to drop one and silently stop storing it."""
        account = self.sign_up()

        self.assertEqual(account.get_full_name(), "Neema Said")
        self.assertEqual(account.phone, "0715000111")
        self.assertEqual(account.role, User.Role.DOCTOR)

    def test_an_unverified_registrant_cannot_sign_in(self):
        """The consequence, stated as a consequence. `authenticate` is the
        gate `EmailBackend` owns, and it is the one a real attempt meets."""
        account = self.sign_up()

        self.assertIsNone(
            authenticate(username=account.email, password=PASSWORD),
            "a registrant signed in without using the emailed code",
        )

    def test_using_the_code_lets_them_in(self):
        """And the other side, so the fix cannot be "nobody may ever sign in"."""
        account = self.sign_up()
        code = OtpToken.objects.filter(user=account).last().otp_code

        self.client.post(
            reverse("verify-email", args=[account.username]), {"otp_code": code}
        )

        self.assertIsNotNone(authenticate(username=account.email, password=PASSWORD))
class SignupRoleTests(TestCase):
    """Registration assigns the role; it does not ask for it.

    The form carried a `<select>` offering "Doctor" or "Receptionist", and the
    view read `request.POST['role']` and checked it against those two values.
    Two things were wrong with that. **Decision 4 records the receptionist role
    as one that does not exist** — nobody holds it and nothing assumes it — so
    the form asked every registrant a question with one real answer and one
    imaginary one. And only doctors self-register at all (design §0.1), so
    there was never a question to ask.

    Assigning it server-side is **stricter** than validating it, not looser:
    the old code took `role` from an attacker-controlled body and compared it
    against a closed set; this never reads it, so there is nothing to compare
    and nothing to get wrong. It also brings the form into line with
    `SocialAccountAdapter.save_user`, which has assigned `doctor` since 10.4 —
    the two registration routes now agree instead of one asking and one not.
    """

    PAYLOAD = {
        "username": "selfmade", "firstname": "Baraka", "lastname": "Mushi",
        "email": "baraka@example.com", "phone": "0715222333",
        "password1": PASSWORD, "password2": PASSWORD,
    }

    def sign_up(self, **extra):
        self.client.post(reverse("signup"), dict(self.PAYLOAD, **extra))
        return User.objects.filter(username="selfmade").first()

    def test_the_form_does_not_ask_for_a_role(self):
        """A `name="role"` input is what would put the field back in the body.
        The page may still *say* "Doctor" — it should — so this looks for the
        input rather than for the word."""
        body = self.client.get(reverse("signup")).content.decode()

        self.assertNotIn('name="role"', body)
        self.assertIn("Account type", body)
        self.assertNotIn("Receptionist", body)

    def test_registering_without_a_role_produces_a_doctor(self):
        """The old view read `request.POST['role']` with a bare subscript, so
        a post without the field raised `MultiValueDictKeyError` — an
        unconditional 500, the same way the "Add New Doctor" modal was broken
        (plan 5.12). Now the field is simply not part of the request."""
        account = self.sign_up()

        self.assertIsNotNone(account, "signup failed without a role field")
        self.assertEqual(account.role, User.Role.DOCTOR)

    def test_a_role_in_the_request_body_is_ignored(self):
        """The assertion that makes this stricter than what it replaced.

        `admin` was already refused by the old validation; `receptionist` was
        accepted, and would have created an account in a role design decision 4
        says does not exist. Neither reaches the model now, because the view
        never looks.
        """
        for smuggled in ("admin", "receptionist", "4491"):
            with self.subTest(role=smuggled):
                User.objects.filter(username="selfmade").delete()
                account = self.sign_up(role=smuggled)

                self.assertEqual(account.role, User.Role.DOCTOR)

    def test_the_account_is_still_held_for_email_verification(self):
        """Assigning the role must not disturb the gate that does the work.
        `update_fields` names the columns this view writes, and `role` is one
        of them — get that list wrong and either the role is silently dropped
        or `is_active` is clobbered again (plan 10.y)."""
        account = self.sign_up()

        self.assertFalse(account.is_active)
        self.assertTrue(account.is_approved)
        self.assertEqual(account.get_full_name(), "Baraka Mushi")
        self.assertEqual(account.phone, "0715222333")
class VerificationFlowTests(TestCase):
    """Signup -> code -> dashboard, and the guards that flow needs.

    **The flow used to dead-end.** `signup` said "Account created
    successfully!" and redirected to the login form; the account was
    deliberately inactive, so signing in was refused with "Your account is not
    verified yet"; and nothing on any page linked to where the code goes. The
    only route in was the URL inside the email. AGKI reported it on
    2026-08-31.

    Verifying now signs the account in and lands it on its dashboard, which
    makes this page an **authentication endpoint**. Most of what follows tests
    that rather than the happy path: a correct code is a credential, so the
    interesting questions are who may use one, how often it may be guessed,
    and whether it survives being used.
    """

    def setUp(self):
        caches["ratelimit"].clear()
        mail.outbox = []

    def register(self, username="newdoc"):
        self.client.post(reverse("signup"), {
            "username": username, "firstname": "Zawadi", "lastname": "Kimaro",
            "email": username + "@example.com", "phone": "0715111222",
            "password1": PASSWORD, "password2": PASSWORD,
        })
        return User.objects.get(username=username)

    def code_for(self, user):
        return OtpToken.objects.filter(user=user).latest("otp_created_at").otp_code

    def submit(self, user, code):
        return self.client.post(
            reverse("verify-email", args=[user.username]), {"otp_code": code}
        )

    def is_signed_in(self):
        return self.client.session.get("_auth_user_id") is not None

    # ---------------------------------------------------------------- flow

    def test_signup_sends_the_registrant_to_the_code_page(self):
        """The bug AGKI reported. Landing on `signin` was landing on a door
        locked on purpose, with no sign saying where the key was."""
        response = self.client.post(reverse("signup"), {
            "username": "zawadi", "firstname": "Zawadi", "lastname": "Kimaro",
            "email": "zawadi@example.com", "phone": "0715111222",
            "password1": PASSWORD, "password2": PASSWORD,
        })

        self.assertRedirects(
            response, reverse("verify-email", args=["zawadi"]),
            fetch_redirect_response=False,
        )

    def test_the_page_says_where_the_code_went(self):
        """"Check your email" is no help to somebody with two of them. Masked,
        because this page is reachable by anyone who knows the username."""
        user = self.register()

        page = self.client.get(reverse("verify-email", args=[user.username]))

        self.assertContains(page, "@example.com")
        self.assertNotContains(page, user.email)
        self.assertContains(page, reverse("resend-verification", args=[user.username]))

    def test_the_boxes_are_hidden_until_a_code_is_on_its_way(self):
        """AGKI, 2026-08-31: the six boxes were on screen before anyone had
        asked for a code.

        Rendering them there invites somebody to sit and type into a form whose
        only possible outcome is "that code is not right" - the same shape of
        wrong as a button that does nothing, and this project's recurring one.
        There is nothing to enter yet, so there is nothing to enter it into.
        """
        user = self.register()
        OtpToken.objects.filter(user=user).update(
            otp_expires_at=timezone.now() - timedelta(seconds=1),
            otp_created_at=timezone.now() - verification.RESEND_COOLDOWN,
        )

        page = self.client.get(reverse("verify-email", args=[user.username]))

        self.assertNotContains(page, 'class="otp-input"')
        self.assertNotContains(page, 'id="otpForm"')
        self.assertContains(page, "Send me a code")

    def test_the_boxes_appear_once_the_code_has_been_sent(self):
        """The other half. A screen that never shows the boxes is not a fix."""
        user = self.register()
        OtpToken.objects.filter(user=user).update(
            otp_expires_at=timezone.now() - timedelta(seconds=1),
            otp_created_at=timezone.now() - verification.RESEND_COOLDOWN,
        )

        self.client.post(reverse("resend-verification", args=[user.username]))
        page = self.client.get(reverse("verify-email", args=[user.username]))

        self.assertContains(page, 'class="otp-input"', count=6)
        self.assertContains(page, 'id="otpForm"')
        self.assertContains(page, "Check your email")

    def test_the_request_screen_still_carries_its_countdown(self):
        """The countdown lives above the guard that skips the box handling, so
        a screen with no boxes keeps its timer. Reading the seconds off the
        button is what proves the server, not the browser, decides them."""
        user = self.register()

        page = self.client.get(reverse("verify-email", args=[user.username]))

        self.assertContains(page, 'id="resendBtn"')
        self.assertContains(page, 'data-wait=')

    def test_the_box_handling_script_is_guarded_against_the_boxes_being_absent(self):
        """A source assertion, because nothing here can execute JavaScript.

        The script does `otpInputs[0].focus()` and binds a submit handler to
        `#otpForm`. On the request screen neither element exists, so without
        the guard both throw and **every line after them stops running** -
        which on that screen is the resend countdown, the one control there is.

        Django's test client does not run scripts, so no behavioural test can
        catch this; mutation-checking confirmed as much by deleting the guard
        and watching the suite stay green. Asserting the source is the weaker
        thing that is actually available, and this project already does it for
        template hazards it cannot otherwise reach (`CopyDisciplineTests`, and
        `test_the_page_does_not_simulate_being_approved`). The alternative was
        to leave a known footgun with nothing watching it at all.
        """
        from pathlib import Path

        from django.conf import settings

        source = (
            Path(settings.BASE_DIR)
            / "useraccount" / "templates" / "useraccount" / "verify_token.html"
        ).read_text(encoding="utf-8")

        guard = "if (!otpInputs.length) { return; }"
        # The *statement*, matched with its indentation and semicolon. Searching
        # for the bare expression finds the comment above the guard, which
        # names it - and the first version of this test duly failed on its own
        # prose. Small, but the same shape as the `{#...#}` notes that rendered
        # to the page in Phase 2: text about code is still text in the file.
        first_use = chr(10) + "    otpInputs[0].focus();"

        self.assertIn(guard, source)
        self.assertLess(
            source.index(guard),
            source.index(first_use),
            "the guard must come before the first use of the boxes",
        )

    def test_the_right_code_signs_them_in_and_lands_them_on_the_dashboard(self):
        user = self.register()

        response = self.submit(user, self.code_for(user))

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(self.is_signed_in(), "verifying did not sign the account in")
        self.assertRedirects(
            response, reverse("post_login_redirect"), fetch_redirect_response=False,
        )
        self.assertEqual(
            self.client.get(reverse("post_login_redirect"))["Location"],
            reverse("doctor_admin_dashboard"),
        )

    # ------------------------------------------------------------- refusals

    def test_a_wrong_code_activates_nothing(self):
        user = self.register()

        self.submit(user, "000000")

        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse(self.is_signed_in())

    def test_an_expired_code_is_refused(self):
        user = self.register()
        code = self.code_for(user)
        OtpToken.objects.filter(user=user).update(
            otp_expires_at=timezone.now() - timedelta(seconds=1)
        )

        self.submit(user, code)

        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse(self.is_signed_in())

    def test_a_code_cannot_be_used_twice(self):
        """It is a credential; a spent one must not still open the door. The
        old view left the row in place after verifying."""
        user = self.register()
        code = self.code_for(user)
        self.submit(user, code)
        self.client.logout()

        self.submit(user, code)

        self.assertFalse(self.is_signed_in())

    def test_a_spent_code_is_deleted_not_merely_bypassed(self):
        """Proving the delete, which the replay test above does **not**.

        Mutation-checking caught that: removing the delete broke nothing,
        because a verified account is turned away by the `is_active` guard
        before its code is ever looked at. So the replay test was measuring
        the guard, not the cleanup, and the cleanup was untested.

        It is worth keeping rather than deleting as dead code, because it
        removes a live credential from the database the moment it is spent.
        The narrow case is real: an administrator switching the account off
        inside the code's two-minute life would otherwise leave a working code
        lying in the table.
        """
        user = self.register()
        code = self.code_for(user)
        self.submit(user, code)
        self.client.logout()
        User.objects.filter(pk=user.pk).update(is_active=False)

        self.submit(user, code)

        self.assertFalse(self.is_signed_in(), "a spent code still opened the door")
        self.assertFalse(OtpToken.objects.filter(user=user).exists())

    def test_an_already_verified_account_cannot_be_signed_in_here(self):
        """Without this guard the page is a way into **any** account whose six
        digits you can guess, not only one waiting to be verified."""
        user = self.register()
        code = self.code_for(user)
        User.objects.filter(pk=user.pk).update(is_active=True)

        response = self.client.post(
            reverse("verify-email", args=[user.username]), {"otp_code": code}
        )

        self.assertFalse(self.is_signed_in())
        self.assertRedirects(response, reverse("signin"), fetch_redirect_response=False)

    def test_guesses_are_rate_limited(self):
        """Six digits is a million guesses, which is not many. The old view
        would answer as many as it was asked."""
        user = self.register()
        code = self.code_for(user)

        for _ in range(verification.ATTEMPT_LIMIT + 1):
            self.submit(user, "000000")

        self.submit(user, code)  # the real code, but the window is spent

        user.refresh_from_db()
        self.assertFalse(user.is_active, "the limiter let a guessing run through")

    def test_a_missing_code_does_not_crash_the_page(self):
        """`user_otp.otp_code` was read off a `.last()` that returns None for
        anyone with no outstanding code - an unconditional 500."""
        user = self.register()
        OtpToken.objects.filter(user=user).delete()

        self.assertEqual(self.submit(user, "123456").status_code, 302)
        self.assertEqual(
            self.client.get(reverse("verify-email", args=[user.username])).status_code,
            200,
        )

    # --------------------------------------------------------------- resend

    def test_resending_replaces_the_old_code(self):
        """A resend supersedes rather than adds.

        **Two separate things make that true, and mutation-checking sorted out
        which is which.** The old code stops working because `latest_code`
        consults only the newest row — that holds with or without the delete,
        and the first version of this test proved only that. The delete is
        what stops superseded codes *accumulating in the table*, so the row
        count is asserted too; without it, removing the delete changed nothing
        any test could see.

        Worth having both. A spent or superseded verification code is a live
        credential sitting in a database, and "nothing currently reads it" is
        a weaker guarantee than "it is not there".
        """
        user = self.register()
        first = self.code_for(user)
        OtpToken.objects.filter(user=user).update(
            otp_created_at=timezone.now() - verification.RESEND_COOLDOWN
        )

        self.client.post(reverse("resend-verification", args=[user.username]))
        second = self.code_for(user)

        self.assertNotEqual(first, second)
        self.assertEqual(
            OtpToken.objects.filter(user=user).count(), 1,
            "the superseded code is still sitting in the table",
        )
        self.submit(user, first)
        user.refresh_from_db()
        self.assertFalse(user.is_active, "the superseded code still worked")

    def test_the_cooldown_is_enforced_on_the_server(self):
        """The countdown on the page is a courtesy. This endpoint is one curl
        away from being an email sender pointed at somebody else's inbox."""
        user = self.register()
        mail.outbox = []

        self.client.post(reverse("resend-verification", args=[user.username]))

        self.assertEqual(mail.outbox, [])

    def test_resend_is_post_only(self):
        """A GET-triggered send is followable by anything that follows links -
        a link checker, a mail client prefetching, a crawler."""
        user = self.register()

        self.assertEqual(
            self.client.get(
                reverse("resend-verification", args=[user.username])
            ).status_code,
            405,
        )
class StalledSignupTests(TestCase):
    """Signing in with an unfinished registration finishes it.

    Someone who closed the tab at the code page — or whose code expired while
    they were reading the email — had nowhere to go. `signin` told them "Your
    account is not verified yet. Please check your email for OTP
    verification", which was a dead end twice over: nothing on the page linked
    to the code page, and the code in that email had expired two minutes after
    it was sent, which is by definition before they read this.

    They now land on the code page with a fresh code. AGKI asked for this on
    2026-08-31, right after the same dead end was fixed at the signup door.

    **The interesting half is the account this must NOT do it for.**
    `is_active=False` means two unrelated things — a signup nobody finished,
    and an account an administrator switched off with the console's "Account
    active" toggle. Treating them alike would let a switched-off account turn
    itself back on by reading its own email, which is a privilege escalation
    dressed as a convenience.
    """

    def setUp(self):
        caches["ratelimit"].clear()
        mail.outbox = []

    def register(self, username="stalled"):
        self.client.post(reverse("signup"), {
            "username": username, "firstname": "Halima", "lastname": "Juma",
            "email": username + "@example.com", "phone": "0715333444",
            "password1": PASSWORD, "password2": PASSWORD,
        })
        return User.objects.get(username=username)

    def sign_in(self, user, password=PASSWORD):
        return self.client.post(
            reverse("signin"), {"username": user.email, "password1": password}
        )

    def messages_from(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    # ------------------------------------------------- the stalled signup

    def test_signing_in_sends_an_unfinished_signup_to_the_code_page(self):
        user = self.register()
        mail.outbox = []

        response = self.sign_in(user)

        self.assertRedirects(
            response, reverse("verify-email", args=[user.username]),
            fetch_redirect_response=False,
        )

    def test_signing_in_does_not_send_a_code_by_itself(self):
        """Inverted on AGKI's instruction of 2026-08-31, and they were right.

        This first shipped issuing a code here, on the reasoning that the old
        one had surely expired. That reasoning holds and the conclusion did
        not: signing in emails somebody who may only be checking whether they
        have an account, starts a two-minute clock they did not ask to start,
        and sends again on every attempt. Asking is one click, and leaves the
        person in charge of when their inbox is used.

        What it must **not** go back to being is a dead end - hence the
        redirect above and the button asserted below.
        """
        user = self.register()
        before = OtpToken.objects.get(user=user).otp_code
        OtpToken.objects.filter(user=user).update(
            otp_created_at=timezone.now() - verification.RESEND_COOLDOWN
        )
        mail.outbox = []

        self.sign_in(user)

        self.assertEqual(mail.outbox, [], "signing in sent a code nobody asked for")
        self.assertEqual(OtpToken.objects.get(user=user).otp_code, before)

    def test_the_page_they_land_on_offers_the_button(self):
        """The half that keeps "do not send" from becoming "no way forward"."""
        user = self.register()
        OtpToken.objects.filter(user=user).update(
            otp_expires_at=timezone.now() - timedelta(seconds=1),
            otp_created_at=timezone.now() - verification.RESEND_COOLDOWN,
        )

        page = self.client.get(self.sign_in(user)["Location"])

        self.assertContains(page, "Send me a code")
        self.assertContains(page, reverse("resend-verification", args=[user.username]))

    def test_asking_is_what_sends_it(self):
        user = self.register()
        OtpToken.objects.filter(user=user).update(
            otp_created_at=timezone.now() - verification.RESEND_COOLDOWN
        )
        stale = OtpToken.objects.get(user=user).otp_code
        self.sign_in(user)
        mail.outbox = []

        self.client.post(reverse("resend-verification", args=[user.username]))

        self.assertEqual(len(mail.outbox), 1)
        self.assertNotEqual(OtpToken.objects.get(user=user).otp_code, stale)

    def test_the_message_names_the_address_without_printing_it(self):
        user = self.register()

        said = " ".join(self.messages_from(self.sign_in(user)))

        self.assertIn("not verified yet", said)
        self.assertIn("@example.com", said)
        self.assertNotIn(user.email, said)

    def test_the_whole_way_through_from_the_login_form(self):
        """The end AGKI asked for: stuck at sign-in, finished on the
        dashboard."""
        user = self.register()
        self.sign_in(user)

        self.client.post(
            reverse("verify-email", args=[user.username]),
            {"otp_code": OtpToken.objects.get(user=user).otp_code},
        )

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(
            self.client.get(reverse("post_login_redirect"))["Location"],
            reverse("doctor_admin_dashboard"),
        )

    # ------------------------------------------- the account it must refuse

    def deactivated_after_verifying(self):
        """A real account: verified once, then switched off by an admin.

        Verification deletes the code on success, so "no outstanding code"
        is what separates this from a signup nobody finished — and it is state
        that already exists, needing no new column and no backfill.
        """
        user = self.register("switched-off")
        self.client.post(
            reverse("verify-email", args=[user.username]),
            {"otp_code": OtpToken.objects.get(user=user).otp_code},
        )
        self.client.logout()
        User.objects.filter(pk=user.pk).update(is_active=False)
        return User.objects.get(pk=user.pk)

    def test_a_switched_off_account_is_not_offered_verification(self):
        user = self.deactivated_after_verifying()
        mail.outbox = []

        response = self.sign_in(user)

        self.assertRedirects(response, reverse("signin"), fetch_redirect_response=False)
        self.assertIn(ACCESS_WITHDRAWN, self.messages_from(response))

    def test_a_switched_off_account_cannot_make_the_site_email_it(self):
        """Otherwise the login form is a way to have codes sent to an account
        an administrator has already closed."""
        user = self.deactivated_after_verifying()
        mail.outbox = []

        self.sign_in(user)

        self.assertEqual(mail.outbox, [])
        self.assertFalse(OtpToken.objects.filter(user=user).exists())

    def test_a_switched_off_account_stays_switched_off(self):
        user = self.deactivated_after_verifying()

        self.sign_in(user)

        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertIsNone(self.client.session.get("_auth_user_id"))

    # ------------------------------------------------------------ no oracle

    def test_a_wrong_password_sends_nothing_and_says_nothing(self):
        """The password check is what makes issuing a code safe here. Without
        it, anyone could point the login form at somebody else's address and
        have the site email them."""
        user = self.register()
        mail.outbox = []

        response = self.sign_in(user, password="not-the-password")

        self.assertEqual(mail.outbox, [])
        said = " ".join(self.messages_from(response))
        self.assertIn("Incorrect password", said)
        self.assertNotIn("not verified", said)
class CodeShapeTests(TestCase):
    """Six digits, and all six of them significant.

    `generate_otp` was `str(secrets.randbelow(10**6)).zfill(6)` - six
    characters every time, which looks correct and is. About one code in ten
    came out as `068227`: six characters, five *significant* digits. That is
    the shape of value people and tools quietly shorten - a spreadsheet makes
    it a number, `parseInt` drops the zero, and somebody reading it off a
    terminal types five digits into six boxes and is told their code is wrong.

    AGKI reported it on 2026-08-31 as "the code is 5 digits long", which was a
    precise description of what they were looking at.
    """

    def test_every_code_is_six_digits(self):
        codes = [generate_otp() for _ in range(2000)]

        self.assertEqual({len(c) for c in codes}, {6})
        self.assertTrue(all(c.isdigit() for c in codes))

    def test_no_code_starts_with_a_zero(self):
        """The actual complaint. 2000 draws is enough to be sure: with the old
        generator this fails with near-certainty, since one draw in ten
        offends."""
        offenders = [c for c in (generate_otp() for _ in range(2000)) if c[0] == "0"]

        self.assertEqual(offenders[:5], [])

    def test_the_field_still_fits_the_code(self):
        """`otp_code` is `max_length=6`. Widening the draw without checking
        this would truncate silently on some backends."""
        self.assertEqual(OtpToken._meta.get_field("otp_code").max_length, 6)

    def test_a_real_registration_gets_a_usable_code(self):
        """End of the argument: the code in the email is the code the six
        boxes accept, character for character."""
        self.client.post(reverse("signup"), {
            "username": "shaped", "firstname": "A", "lastname": "B",
            "email": "shaped@example.com", "phone": "0715000000",
            "password1": PASSWORD, "password2": PASSWORD,
        })
        user = User.objects.get(username="shaped")
        code = OtpToken.objects.get(user=user).otp_code

        self.assertEqual(len(code), 6)
        self.assertNotEqual(code[0], "0")
        self.assertIn(code, mail.outbox[-1].body)

        self.client.post(reverse("verify-email", args=[user.username]), {"otp_code": code})

        user.refresh_from_db()
        self.assertTrue(user.is_active)
class ProfilePageTests(TestCase):
    """The rebuild of 2026-09-01 (plan 11.6).

    Two of these are the reason the page was rebuilt rather than restyled: a
    doctor could not set their specialties, and an anonymous request raised a
    500.
    """

    def setUp(self):
        self.doctor = make_staff_user(
            username="prof-doc", email="prof-doc@example.com",
            role=User.Role.DOCTOR, first_name="Anna", last_name="Magembe",
        )
        self.client.force_login(self.doctor)

    # ---------------------------------------------------- the 500

    def test_an_anonymous_request_is_sent_to_sign_in_not_a_stack_trace(self):
        """`get_or_create(user=request.user)` cannot prepare `AnonymousUser`,
        so without `@login_required` this view answered a signed-out visitor
        with a 500. Plan 1.2 put the decorator on every `doctorDashboard` view;
        this one lives in `useraccount` and was outside that sweep, which left
        it the last console page an anonymous request could still reach."""
        self.client.logout()

        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/useraccount/signin/", response["Location"])

    # ---------------------------------------------------- the unfindable doctor

    def test_a_doctor_can_choose_specialties_at_all(self):
        """The whole point. `ProfileEditForm` offered three of `Profile`'s ten
        fields, and `specialties` - which `search_results` filters on - was not
        one of them. So the browse grid AGKI asked for was fed by a field
        nobody could fill in."""
        from useraccount.forms import ProfileEditForm

        offered = set(ProfileEditForm().fields)

        self.assertIn("specialties", offered)
        self.assertIn("qualifications", offered)
        for field in ("region", "languages_spoken", "years_of_experience"):
            self.assertIn(field, offered, "%s is still editable nowhere" % field)

    def test_saving_the_form_records_the_picks(self):
        general = Specialty.objects.get(name="General Practice")
        md = Qualification.objects.get(name="MD - Doctor of Medicine")

        self.client.post(reverse("profile_edit"), {
            "first_name": "Anna", "last_name": "Magembe",
            "email": "prof-doc@example.com", "phone": "0715000111",
            "specialties": [general.pk], "qualifications": [md.pk],
            "region": "Pwani", "location": "Kibaha",
            "languages_spoken": "Swahili, English", "years_of_experience": 10,
            "bio": "Ten years in general practice.",
        })

        profile = Profile.objects.get(user=self.doctor)
        self.assertEqual([s.name for s in profile.specialties.all()], ["General Practice"])
        self.assertEqual(
            [q.name for q in profile.qualifications.all()], ["MD - Doctor of Medicine"]
        )
        self.assertEqual(profile.region, "Pwani")

    def test_the_page_says_a_doctor_without_a_specialty_cannot_be_found(self):
        """Named as a consequence rather than a percentage. "Profile 40%
        complete" tells a doctor nothing; this is both true and fixable in one
        click, and every doctor in the database would see it today."""
        response = self.client.get(reverse("profile"))

        self.assertFalse(response.context["findable"])
        self.assertContains(response, "Patients cannot find you yet")

    def test_the_warning_goes_away_once_it_stops_being_true(self):
        profile = Profile.objects.get(user=self.doctor)
        profile.specialties.add(Specialty.objects.get(name="Cardiology"))

        response = self.client.get(reverse("profile"))

        self.assertTrue(response.context["findable"])
        self.assertNotContains(response, "Patients cannot find you yet")

    def test_the_preview_shows_the_doctors_real_bookable_times(self):
        """The honest version of a completeness meter: the gap is visible
        rather than described. It uses the same definition of "bookable" the
        public search does, because a preview that disagreed with the real page
        would be worse than none."""
        from appointments.models import AppointmentSettings, AvailableSlot

        AppointmentSettings.load()
        soon = timezone.localdate() + timedelta(days=3)
        AvailableSlot.objects.create(user=self.doctor, date=soon, time=time(9, 0))

        response = self.client.get(reverse("profile"))

        self.assertEqual(len(response.context["upcoming_slots"]), 1)
        self.assertContains(response, "9:00 AM")

    def test_a_booked_time_is_not_previewed_as_bookable(self):
        from appointments.models import AppointmentSettings, AvailableSlot

        AppointmentSettings.load()
        soon = timezone.localdate() + timedelta(days=3)
        AvailableSlot.objects.create(
            user=self.doctor, date=soon, time=time(9, 0), is_booked=True
        )

        response = self.client.get(reverse("profile"))

        self.assertEqual(response.context["upcoming_slots"], [])

    # ---------------------------------------------------- picked, not typed

    def test_neither_list_is_free_text(self):
        """`Profile.expertise` is the one free-text professional field anybody
        ever filled in and its single live value is "Cardiograph" - an ECG
        machine. `CustomUser.qualifications` is empty for every doctor. Both
        are replaced by picks, and neither form offers the old fields."""
        from useraccount.forms import ProfileEditForm, UserEditForm

        self.assertNotIn("qualifications", UserEditForm().fields)
        self.assertNotIn("expertise", ProfileEditForm().fields)

    def test_a_retired_entry_is_not_offered_but_stays_on_whoever_holds_it(self):
        """That is what `is_active` is for: retiring a specialty must not
        silently strip it from the doctors already linked to it."""
        from useraccount.forms import ProfileEditForm

        retired = Specialty.objects.get(name="Neurology")
        profile = Profile.objects.get(user=self.doctor)
        profile.specialties.add(retired)
        retired.is_active = False
        retired.save(update_fields=["is_active"])

        offered = list(ProfileEditForm(instance=profile).fields["specialties"].queryset)

        self.assertNotIn(retired, offered)
        self.assertIn(retired, profile.specialties.all())

    # ---------------------------------------------------- the seed

    def test_both_lists_are_seeded(self):
        self.assertEqual(Qualification.objects.count(), 14)
        self.assertEqual(Specialty.objects.count(), 18)

    def test_the_seed_did_not_duplicate_cardiology(self):
        """It was already there, put in by `map_expertise_to_specialties` when
        AGKI decided "Cardiograph" meant Cardiology (plan 2.3). A second row
        would split that doctor's link across two entries reading identically
        in the admin."""
        self.assertEqual(Specialty.objects.filter(name="Cardiology").count(), 1)

    # ---------------------------------------------------- the shadowed API

    def test_the_api_profile_view_is_reachable_again(self):
        """`profile/` was registered twice with the name `profile` - the page
        and the DRF view - so the page won and the API was dead code behind a
        live URL."""
        from django.urls import resolve

        page = reverse("profile")
        api = reverse("api-profile")

        # Asserted on the paths and the callables rather than on
        # `func.__name__`: `get_user_profile` is wrapped by DRF's `@api_view`,
        # so its resolved name is the decorator's, not its own. What matters is
        # that the two are no longer the same URL answered by one view.
        self.assertNotEqual(page, api)
        self.assertEqual(resolve(page).func.__name__, "profile")
        self.assertNotEqual(resolve(page).func, resolve(api).func)
        self.assertEqual(resolve(api).func.__module__, "useraccount.views")

    # ---------------------------------------------------- the templates

    def test_the_preview_matches_the_card_it_previews(self):
        """A preview that flatters the real page is worse than none.

        The public card read `doctor.qualifications` - the **free-text** field
        plan 11.6 stopped writing to - so a doctor who picked "MD" from the new
        list saw it here and patients saw nothing. Both now read the picked
        list, and both carry the same "Dr." prefix (plan 11.7).
        """
        from appointments.models import AppointmentSettings, AvailableSlot

        AppointmentSettings.load()
        profile = Profile.objects.get(user=self.doctor)
        profile.specialties.add(Specialty.objects.get(name="Cardiology"))
        profile.qualifications.add(Qualification.objects.get(name="MBBS"))
        AvailableSlot.objects.create(
            user=self.doctor, date=timezone.localdate() + timedelta(days=3), time=time(9, 0)
        )

        mine = self.client.get(reverse("profile")).content.decode()
        theirs = self.client.get(reverse("search_results")).content.decode()

        for page, where in ((mine, "the preview"), (theirs, "the booking page")):
            # Whitespace-normalised: both templates put the specialty, the
            # separator and the qualification on their own source lines, so the
            # rendered text carries the indentation between them. Collapsing
            # runs of whitespace asserts what a reader sees rather than how the
            # template happens to be laid out.
            flat = " ".join(page.split())
            self.assertIn("Dr. Anna Magembe", flat, where)
            # The same separator on both. The preview used the `&bull;` entity
            # while the card used a literal bullet - identical on screen, and
            # the kind of near-miss that lets two things drift apart later
            # because nobody notices they were never the same.
            self.assertIn("Cardiology • MBBS", flat, where)

    def test_the_public_card_no_longer_reads_the_retired_free_text_field(self):
        """Mutation-proof for the finding above: setting only the old field
        must put nothing on the card."""
        User.objects.filter(pk=self.doctor.pk).update(qualifications="Professor of Everything")
        profile = Profile.objects.get(user=self.doctor)
        profile.specialties.add(Specialty.objects.get(name="Cardiology"))

        body = self.client.get(reverse("search_results")).content.decode()

        self.assertNotIn("Professor of Everything", body)

    #: A 1x1 PNG, base64 rather than a byte string of \x escapes.
    #: Those escapes do not survive being written by a script through a
    #: shell heredoc: they arrive as real NUL bytes, and Python refuses to
    #: import a source file containing one. That is how this landed the
    #: first time, and base64 has nothing in it for a layer of quoting to
    #: eat.
    PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    def give_a_photo(self):
        from base64 import b64decode

        from django.core.files.uploadedfile import SimpleUploadedFile

        self.doctor.image = SimpleUploadedFile("bird.png", b64decode(self.PNG_B64), content_type="image/png")
        self.doctor.save()

    def test_django_s_own_file_widget_markup_is_gone(self):
        """An `ImageField` uses `ClearableFileInput`, whose markup is
        "Currently:", a **link to the file**, a "Clear" checkbox and "Change:".
        None of it can be styled or suppressed from CSS, and AGKI saw all of it
        on the page. The two controls are written out directly instead."""
        self.give_a_photo()

        body = self.client.get(reverse("profile_edit")).content.decode()

        self.assertNotIn("Currently", body)
        self.assertNotIn("Change:", body)
        self.assertNotIn("bird.png", body)

    def test_the_photo_shows_as_a_picture_with_two_buttons(self):
        self.give_a_photo()

        body = self.client.get(reverse("profile_edit")).content.decode()

        self.assertIn('id="pe-thumb"', body)
        self.assertIn('class="pe-file"', body)
        self.assertIn('id="pe-clear-btn"', body)

    def test_there_is_nothing_to_remove_before_there_is_a_photo(self):
        """Offering Remove on an empty avatar is a button that can only do
        nothing - the same rule the availability page applies to Delete."""
        body = self.client.get(reverse("profile_edit")).content.decode()

        self.assertIn("pe-thumb-empty", body)
        self.assertNotIn('id="pe-clear-btn"', body)

    def test_removing_a_photo_still_works(self):
        """The visible buttons drive Django's own inputs, so what is posted is
        what `ClearableFileInput` parses - `-clear` is its suffix, not one
        invented here."""
        self.give_a_photo()

        self.client.post(reverse("profile_edit"), {
            "first_name": "Anna", "last_name": "Magembe",
            "email": "prof-doc@example.com", "phone": "0715000111",
            "image-clear": "on",
        })

        self.doctor.refresh_from_db()
        self.assertFalse(self.doctor.image)

    def test_the_date_of_birth_is_a_date_control_that_shows_its_value(self):
        """Django's `DateInput` renders `type="text"`, so the field was a plain
        box with no calendar - and 11.7's styling for `input[type="date"]`
        matched nothing at all.

        The format matters as much as the type: a browser date control only
        displays a value it can parse, and only `YYYY-MM-DD` qualifies. Without
        it a stored date renders as "1 May 1990" and the field comes up
        **empty**, which reads as data loss.
        """
        Profile.objects.filter(user=self.doctor).update(birth_date=date(1990, 5, 1))

        body = self.client.get(reverse("profile_edit")).content.decode()

        self.assertIn('type="date" name="birth_date"', body)
        self.assertIn('value="1990-05-01"', body)

    @override_settings(LANGUAGE_CODE="en-gb")
    def test_the_date_survives_a_change_of_language(self):
        """Why `format='%Y-%m-%d'` is written out rather than left to Django.

        Mutation-checking found that removing it changed nothing - under
        `en-us` the first entry in `DATE_INPUT_FORMATS` is already ISO, so the
        widget renders `1990-05-01` either way. It is not redundant, though:
        `LANGUAGE_CODE` is read from the environment in this project
        (`DJANGO_LANGUAGE_CODE`), and under `en-gb` the first format is
        `%d/%m/%Y`. A browser date control shows **nothing** for a value it
        cannot parse, so the field would come up empty on a page that had one
        - which reads as data loss and is not something an env var should be
        able to cause quietly.
        """
        Profile.objects.filter(user=self.doctor).update(birth_date=date(1990, 5, 1))

        body = self.client.get(reverse("profile_edit")).content.decode()

        self.assertIn('value="1990-05-01"', body)
        self.assertNotIn('value="01/05/1990"', body)

    def test_the_edit_page_styles_its_own_photo_and_date_controls(self):
        """`<input type=file>` renders its own button and file name, neither
        of which can be styled - so it is hidden behind a real `<label for=…>`,
        which keeps the keyboard and screen-reader behaviour. The date field is
        the native control **styled**, not replaced, so typing YYYY-MM-DD still
        works alongside the picker.
        """
        response = self.client.get(reverse("profile_edit"))

        self.assertContains(response, 'class="pe-file"')
        self.assertContains(response, 'for="id_image"')
        self.assertContains(response, 'type="date"')
        self.assertContains(response, 'type="file"')

    def test_each_page_defines_the_classes_it_uses(self):
        import re
        from pathlib import Path

        from django.conf import settings

        for name, prefix in (("profile.html", "pf-"), ("profile_edit.html", "pe-")):
            source = (
                Path(settings.BASE_DIR) / "useraccount" / "templates" / "useraccount" / name
            ).read_text(encoding="utf-8")
            used = set()
            for attr in re.findall(r'class="([^"{}]*)"', source):
                used.update(c for c in attr.split() if c.startswith(prefix))
            defined = set(re.findall(r"\.(" + prefix + r"[a-zA-Z0-9_-]+)", source))
            self.assertTrue(used, "%s: no %s classes found" % (name, prefix))
            self.assertEqual(sorted(used - defined), [], name)

    def test_the_pickers_fall_back_to_real_checkboxes(self):
        """The script builds the pellets **around** the list Django rendered
        rather than instead of it, so with JavaScript off the page is two
        ordinary checkbox lists - long, but complete and submittable. This form
        is how a doctor becomes findable at all; it must not need a script."""
        response = self.client.get(reverse("profile_edit"))

        self.assertContains(response, 'name="specialties"')
        self.assertContains(response, 'name="qualifications"')
        self.assertContains(response, 'type="checkbox"')

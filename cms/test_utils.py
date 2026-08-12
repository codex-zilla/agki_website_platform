"""Shared helpers for the cms and app test suites."""

import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

User = get_user_model()


class TempMediaRootMixin:
    """Point MEDIA_ROOT at a throwaway directory for the whole test class.

    Two reasons this exists rather than tests just calling the seed command:

    * `seed_home_content` copies ~32 real files. Without this the suite would
      write them into the project's actual `media/` directory.
    * The three problem-card images are seeded from `media/` rather than
      `static/` (they were uploaded, not committed), so a fresh MEDIA_ROOT has
      to be pre-staged with them or the command can't find its sources.

    The override is enabled *before* `super().setUpClass()`, because that is
    what runs `setUpTestData` — where subclasses typically seed.
    """

    @classmethod
    def setUpClass(cls):
        cls.media_root = Path(tempfile.mkdtemp(prefix="agki-test-media-"))
        real_problem_images = Path(settings.MEDIA_ROOT) / "problem"
        if real_problem_images.is_dir():
            shutil.copytree(real_problem_images, cls.media_root / "problem")

        cls._media_override = override_settings(MEDIA_ROOT=str(cls.media_root))
        cls._media_override.enable()
        try:
            super().setUpClass()
        except Exception:
            # Otherwise a failure in setUpTestData leaves the override active
            # and the temp directory behind for the rest of the run.
            cls._media_override.disable()
            shutil.rmtree(cls.media_root, ignore_errors=True)
            raise

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_override.disable()
        shutil.rmtree(cls.media_root, ignore_errors=True)


def make_staff_user(username="editor", email="editor@example.com", password="test-pass-12345", **extra):
    """Create an active account, undoing the public-signup side effects.

    `useraccount.signals.create_token` treats any new account with a usable
    password as a public self-signup: it forces `is_active=False` and emails a
    one-time code. Correct for the signup form, wrong for a fixture — so this
    reverses it the same way `CustomUserAdmin.save_model` does for accounts an
    admin creates directly.
    """
    user = User.objects.create_user(username=username, email=email, password=password, **extra)
    user.is_active = True
    user.is_approved = True
    user.save()
    return user

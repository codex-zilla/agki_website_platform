"""A test runner that does not read the developer's `.env` for behaviour.

Added the day AGKI put real SMS credentials into `.env`. The
suite went from green to twenty failures with no code change, because
`site_channels()` asks whether the gateway is configured - by design, so that a
machine without credentials stays email-only - and suddenly it was. Every test
that counted notifications was then counting texts as well.

That is the suite depending on the machine it runs on, which makes "it passes
here" mean nothing. The credentials are cleared for the duration of a test run
and the tests that want SMS turn it back on explicitly with
`@override_settings`, which is also the honest way to read them: a test about
SMS says SMS in its own body rather than inheriting it from a file that is not
in the repository.

Cleared rather than pointed at a fake gateway on purpose. A wrong URL would
still let a mistake reach the network; an empty key cannot.
"""

from django.test.runner import DiscoverRunner


class HermeticTestRunner(DiscoverRunner):
    """The project's runner. Configured as `TEST_RUNNER` in settings."""

    #: Anything here is emptied before the suite runs. Add to it whenever a
    #: setting starts deciding *behaviour* from the environment rather than
    #: only *destinations* - an SMTP host that is wrong is a failed send, but a
    #: gateway that is present changes which messages exist at all.
    NEUTRALISED = ("BEEM_API_KEY", "BEEM_SECRET_KEY", "BEEM_SENDER_ID",
                   "AFYA_WHATSAPP_TOKEN", "AFYA_WHATSAPP_PHONE_ID")

    def setup_test_environment(self, **kwargs):
        from django.conf import settings

        super().setup_test_environment(**kwargs)
        for name in self.NEUTRALISED:
            setattr(settings, name, "")

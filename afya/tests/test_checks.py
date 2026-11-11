from django.test import SimpleTestCase, override_settings

from afya import checks

LIVE = dict(DEBUG=False, AFYA_WEBHOOK_TOKEN="x" * 40)


class StartupCheckTests(SimpleTestCase):
    @override_settings(DEBUG=False, AFYA_WEBHOOK_TOKEN="replace-with-a-long-random-token")
    def test_webhooks_refuse_a_placeholder_token(self):
        response = self.client.post(
            "/afya/webhooks/sms/?token=replace-with-a-long-random-token",
            {"from": "255712345678", "text": "habari"})
        self.assertEqual(response.status_code, 403)

    def ids(self, function):
        return {problem.id for problem in function(None)}

    @override_settings(DEBUG=False, AFYA_WEBHOOK_TOKEN="replace-with-a-long-random-token")
    def test_placeholder_token_blocks_production(self):
        self.assertEqual(self.ids(checks.webhook_token), {"afya.E001"})

    @override_settings(DEBUG=False, AFYA_WEBHOOK_TOKEN="short")
    def test_short_token_blocks_production(self):
        self.assertEqual(self.ids(checks.webhook_token), {"afya.E001"})

    @override_settings(**LIVE)
    def test_long_random_token_passes(self):
        self.assertEqual(self.ids(checks.webhook_token), set())

    @override_settings(DEBUG=True, AFYA_WEBHOOK_TOKEN="")
    def test_laptop_needs_no_token(self):
        self.assertEqual(self.ids(checks.webhook_token), set())

    @override_settings(**LIVE, AFYA_WHATSAPP_TOKEN="t", AFYA_WHATSAPP_APP_SECRET="",
                       AFYA_WHATSAPP_VERIFY_TOKEN="")
    def test_half_configured_whatsapp_blocks_production(self):
        self.assertEqual(self.ids(checks.whatsapp_secret), {"afya.E002", "afya.E003"})

    @override_settings(**LIVE, AFYA_PAYMENT_GATEWAY="afya.payments.SandboxGateway",
                       CACHES={"default": {
                           "BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
    def test_launch_warnings(self):
        self.assertTrue({"afya.W001", "afya.W002", "afya.W003"} <= self.ids(
            checks.launch_readiness))

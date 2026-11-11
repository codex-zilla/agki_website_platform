"""Tests for the production-hardening fixes: safety, privacy, money, channels."""

import hashlib
import hmac
import json
import logging
from datetime import timedelta
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from afya import bookings, guide, outbound, payments
from afya.ai import language as nlp
from afya.ai import safety
from afya.channels import ussd
from afya.models import (Booking, Channel, Client, Conversation, Need, Payment, Provider,
                         ReminderLog, Voucher)
from afya.tests.helpers import consented, provider, reset_outboxes

PHONE = "255712345678"
TOKEN = "test-webhook-token-" + "x" * 32


class SafetyLanguageTests(TestCase):
    def test_apostrophes_do_not_hide_danger_signs(self):
        for text, code in [("I can't breathe", "breathing"),
                           ("I can’t breathe properly", "breathing"),
                           ("the bleeding won't stop", "heavy_bleeding"),
                           ("I'm pregnant and the baby isn't moving, no movement",
                            "baby_not_moving")]:
            with self.subTest(text=text):
                result = safety.check(text)
                self.assertIsNotNone(result)
                self.assertEqual(result.code, code)

    def test_asking_for_a_pregnancy_test_does_not_mark_her_pregnant(self):
        for text in ("nataka kipimo cha mimba", "sina mimba, nataka uzazi wa mpango",
                     "am i pregnant? missed period", "I am not pregnant, I want family planning"):
            with self.subTest(text=text):
                self.assertFalse(nlp.understand(text).mentions_pregnancy)
        self.assertTrue(nlp.understand("nina mimba ya miezi mitatu").mentions_pregnancy)

    def test_pregnancy_test_request_sets_no_flag_on_client(self):
        provider(services=((Need.PREGNANCY_TEST, 2000),))
        consented(region="Pwani", district="Kibaha")
        guide.handle(PHONE, "nataka kipimo cha mimba")
        self.assertFalse(Client.objects.get(phone=PHONE).is_pregnant)


class ConversationFixTests(TestCase):
    def setUp(self):
        reset_outboxes()
        cache.clear()
        self.clinic = provider(services=((Need.FAMILY_PLANNING, 0), (Need.STI, 8000)))

    def test_booking_reply_skips_missing_opening_hours(self):
        Provider.objects.update(opening_hours="")
        consented(region="Pwani", district="Kibaha")
        guide.handle(PHONE, "nataka uzazi wa mpango")
        reply = guide.handle(PHONE, "1")
        self.assertIn("Miadi imewekwa", reply)
        self.assertNotIn("Saa:", reply)

    def test_unknown_place_asks_again_and_keeps_her_need(self):
        consented()
        self.assertIn("wilaya", guide.handle(PHONE, "nataka uzazi wa mpango"))
        self.assertIn("wilaya", guide.handle(PHONE, "huku kwetu"))
        self.assertIn("Kibaha Clinic", guide.handle(PHONE, "Kibaha"))

    def test_suspended_provider_cannot_be_booked_from_an_old_list(self):
        provider("Second Clinic", services=((Need.FAMILY_PLANNING, 0),))
        consented(region="Pwani", district="Kibaha")
        options = guide.handle(PHONE, "nataka uzazi wa mpango")
        first = options.splitlines()[1]
        name = "Kibaha Clinic" if "Kibaha Clinic" in first else "Second Clinic"
        Provider.objects.filter(name=name).update(verification=Provider.Verification.SUSPENDED)
        reply = guide.handle(PHONE, "1")
        self.assertIn("haipatikani", reply)
        self.assertFalse(Booking.objects.exists())

    def _web_to_otp(self, web="web:abc"):
        guide.handle(web, "nataka uzazi wa mpango Kibaha", Channel.WEB)
        guide.handle(web, "ndiyo", Channel.WEB)
        guide.handle(web, "1", Channel.WEB)
        guide.handle(web, "0712345678", Channel.WEB)
        return "".join(ch for ch in outbound.OUTBOX[-1][2] if ch.isdigit())

    def test_otp_expires(self):
        code = self._web_to_otp()
        conversation = Conversation.objects.get(channel=Channel.WEB)
        conversation.state = {**conversation.state, "otp_expires": 0}
        conversation.save()
        self.assertIn("imeisha muda", guide.handle("web:abc", code, Channel.WEB))
        self.assertFalse(Booking.objects.exists())

    def test_otp_sends_are_limited_per_number(self):
        web = "web:abc"
        guide.handle(web, "nataka uzazi wa mpango Kibaha", Channel.WEB)
        guide.handle(web, "ndiyo", Channel.WEB)
        guide.handle(web, "1", Channel.WEB)
        for _ in range(guide.OTP_SENDS_PER_HOUR):
            guide.handle(web, "0712345678", Channel.WEB)
            conversation = Conversation.objects.get(channel=Channel.WEB)
            conversation.state = {**conversation.state, "step": "phone"}
            conversation.save()
        self.assertIn("Subiri", guide.handle(web, "0712345678", Channel.WEB))

    def test_web_visitor_keeps_voucher_when_merged_into_existing_number(self):
        Client.objects.create(phone=PHONE, consented_at=timezone.now())
        web = "web:abc"
        guide.handle(web, "habari", Channel.WEB)
        guide.handle(web, "ndiyo", Channel.WEB)
        voucher = Voucher.issue("UNFPA", 10000, need=Need.STI)
        guide.handle(web, f"vocha {voucher.code}", Channel.WEB)
        guide.handle(web, "nataka kipimo cha zinaa Kibaha", Channel.WEB)
        guide.handle(web, "1", Channel.WEB)
        guide.handle(web, "0712345678", Channel.WEB)
        code = "".join(ch for ch in outbound.OUTBOX[-1][2] if ch.isdigit())
        self.assertIn("BURE", guide.handle(web, code, Channel.WEB))
        voucher.refresh_from_db()
        self.assertEqual(voucher.claimed_by.phone, PHONE)


class MoneyFixTests(TestCase):
    def setUp(self):
        reset_outboxes()
        self.clinic = provider(services=((Need.STI, 8000),), mobile_money_number="0755000000")
        self.client_ = consented(region="Pwani", district="Kibaha")

    def _book(self):
        guide.handle(PHONE, "nataka kipimo cha zinaa")
        guide.handle(PHONE, "1")
        return Booking.objects.order_by("-pk").first()

    def test_wallet_cannot_be_overdrawn_by_two_bookings(self):
        payments.deposit(self.client_, 8000, "test")
        first, second = self._book(), self._book()
        self.assertEqual((first.pay_with, second.pay_with),
                         (Booking.PayWith.WALLET, Booking.PayWith.WALLET))
        bookings.attended(bookings.confirm(first, timezone.now()))
        payment = bookings.attended(bookings.confirm(second, timezone.now()))
        self.assertEqual(self.client_.wallet_balance, 0)
        self.assertEqual(payment.method, Booking.PayWith.MOBILE_MONEY)
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_wallet_visit_pays_the_provider(self):
        payments.deposit(self.client_, 8000, "test")
        payment = bookings.attended(bookings.confirm(self._book(), timezone.now()))
        self.assertEqual(payments.SandboxGateway.payouts[0][:2], ("0755000000", 7600))
        self.assertIsNotNone(payment.paid_out_at)

    def test_voucher_visit_pays_the_provider_full_price(self):
        voucher = Voucher.issue("UNFPA", 10000)
        payments.claim_voucher(self.client_, voucher.code)
        bookings.attended(bookings.confirm(self._book(), timezone.now()))
        self.assertEqual(payments.SandboxGateway.payouts[0][:2], ("0755000000", 8000))

    def test_payout_waits_for_a_number_then_can_be_retried_once(self):
        self.clinic.mobile_money_number = ""
        self.clinic.save()
        payments.deposit(self.client_, 8000, "test")
        payment = bookings.attended(bookings.confirm(self._book(), timezone.now()))
        self.assertTrue(payment.payout_due)
        self.clinic.mobile_money_number = "0755000000"
        self.clinic.save()
        payment.refresh_from_db()
        self.assertTrue(payments.pay_out(payment))
        self.assertIsNone(payments.pay_out(payment))
        self.assertEqual(len(payments.SandboxGateway.payouts), 1)

    def test_payment_gateway_failure_does_not_break_attended(self):
        class Broken(payments.SandboxGateway):
            def request_payment(self, *args):
                raise ConnectionError("down")

        with override_settings(AFYA_PAYMENT_GATEWAY=f"{__name__}.BrokenGateway"):
            globals()["BrokenGateway"] = Broken
            with self.assertLogs("afya", logging.ERROR):
                payment = bookings.attended(bookings.confirm(self._book(), timezone.now()))
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_forged_wallet_callback_is_refused(self):
        self.assertIsNone(payments.confirm_deposit(f"W-{self.client_.pk}-50000-aaaa-bbbb", True))
        reference = payments.request_deposit(self.client_, 3000)
        tampered = reference.replace("-3000-", "-30000-")
        self.assertIsNone(payments.confirm_deposit(tampered, True))
        self.assertEqual(payments.confirm_deposit(reference, True).amount_tsh, 3000)

    def test_no_show_gives_the_voucher_back(self):
        voucher = Voucher.issue("UNFPA", 10000)
        payments.claim_voucher(self.client_, voucher.code)
        booking = self._book()
        bookings.no_show(bookings.confirm(booking, timezone.now()))
        self.assertEqual(self._book().voucher, voucher)


class USSDFixTests(TestCase):
    def setUp(self):
        reset_outboxes()
        provider(services=((Need.FAMILY_PLANNING, 0),))

    def test_every_screen_fits_on_a_phone(self):
        consented()
        for language in ("sw", "en"):
            Client.objects.filter(phone=PHONE).update(language=language)
            for text in ("", "1", "1*3", "1*3*Kibaha", "2", "3", "4", "5"):
                with self.subTest(language=language, text=text):
                    self.assertLessEqual(len(ussd.respond(f"s-{language}", PHONE, text)), 182)

    def test_switching_language_then_choosing(self):
        consented()
        self.assertIn("Find a service", ussd.respond("s1", PHONE, "6"))
        self.assertIn("Choose a service", ussd.respond("s1", PHONE, "6*1"))

    def test_she_books_the_line_she_was_shown(self):
        consented()
        ussd.respond("s1", PHONE, "1*3*Kibaha")  # only Kibaha Clinic is shown
        provider("Aaa Better Clinic", services=((Need.FAMILY_PLANNING, 0),),
                 rating_total=500, rating_count=100)
        final = ussd.respond("s1", PHONE, "1*3*Kibaha*1")
        self.assertIn("Kibaha Clinic", final)
        self.assertEqual(Booking.objects.get().provider.name, "Kibaha Clinic")
        # The details also arrive by SMS, because the USSD screen disappears.
        self.assertEqual(outbound.OUTBOX[-1][1], PHONE)
        self.assertIn(Booking.objects.get().reference, outbound.OUTBOX[-1][2])


@override_settings(AFYA_WEBHOOK_TOKEN=TOKEN, AFYA_WHATSAPP_APP_SECRET="app")
class WebhookFixTests(TestCase):
    def setUp(self):
        reset_outboxes()
        cache.clear()

    def _whatsapp(self, payload, sign=True):
        body = json.dumps(payload).encode()
        headers = {}
        if sign:
            headers["HTTP_X_HUB_SIGNATURE_256"] = "sha256=" + hmac.new(
                b"app", body, hashlib.sha256).hexdigest()
        return self.client.post(reverse("afya:whatsapp"), body,
                                content_type="application/json", **headers)

    def _message(self, message_id="wamid.1"):
        return {"entry": [{"changes": [{"value": {"messages": [
            {"id": message_id, "from": PHONE, "type": "text", "text": {"body": "habari"}}]}}]}]}

    def test_unsigned_whatsapp_is_refused(self):
        self.assertEqual(self._whatsapp(self._message(), sign=False).status_code, 403)

    @override_settings(AFYA_WHATSAPP_APP_SECRET="")
    def test_whatsapp_without_secret_is_refused_in_production(self):
        self.assertEqual(self._whatsapp(self._message(), sign=False).status_code, 403)

    def test_whatsapp_retry_is_answered_once(self):
        self._whatsapp(self._message())
        self._whatsapp(self._message())
        self.assertEqual(len(outbound.OUTBOX), 1)

    def test_a_crash_still_gets_her_an_answer(self):
        from unittest import mock

        with mock.patch("afya.guide.Session.respond", side_effect=RuntimeError("bug")), \
                self.assertLogs("afya", logging.ERROR):
            response = self.client.post(reverse("afya:sms_inbound") + f"?token={TOKEN}",
                                        {"from": PHONE, "text": "habari"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("112", response.json()["reply"])

    def test_web_chat_is_rate_limited(self):
        url = reverse("afya:chat_api")
        body = json.dumps({"message": "habari"})
        statuses = [self.client.post(url, body, content_type="application/json").status_code
                    for _ in range(25)]
        self.assertIn(429, statuses)

    def test_logs_never_hold_the_message(self):
        with self.assertLogs("afya", logging.INFO) as logs:
            outbound.send_sms(PHONE, "Afya Mfukoni: your confirmation code is 4821.")
        self.assertNotIn("4821", "\n".join(logs.output))
        self.assertNotIn(PHONE, "\n".join(logs.output))


class JobTests(TestCase):
    def setUp(self):
        reset_outboxes()
        self.clinic = provider()

    def test_cleanup_removes_numbers_that_never_consented(self):
        guide.handle("255700000001", "habari")
        keep = consented()
        Client.objects.update(created_at=timezone.now() - timedelta(days=2))
        call_command("afya_cleanup", stdout=StringIO())
        self.assertEqual(list(Client.objects.values_list("pk", flat=True)), [keep.pk])

    def test_visit_reminder_respects_stop(self):
        client = consented(region="Pwani", district="Kibaha", reminders_opt_out=True)
        guide.handle(PHONE, "nataka uzazi wa mpango")
        guide.handle(PHONE, "1")
        bookings.confirm(Booking.objects.get(), timezone.now() + timedelta(days=1))
        call_command("afya_send_reminders", stdout=StringIO())
        self.assertFalse(ReminderLog.objects.filter(client=client).exists())

    def test_rating_request_waits_while_she_is_mid_conversation(self):
        consented(region="Pwani", district="Kibaha")
        guide.handle(PHONE, "nataka uzazi wa mpango")
        guide.handle(PHONE, "1")
        booking = Booking.objects.get()
        bookings.attended(bookings.confirm(booking, timezone.now()))
        Booking.objects.update(updated_at=timezone.now() - timedelta(hours=3))
        guide.handle(PHONE, "nataka uzazi wa mpango")  # she is choosing again
        call_command("afya_send_reminders", stdout=StringIO())
        self.assertFalse(ReminderLog.objects.filter(kind=ReminderLog.Kind.RATING).exists())
        self.assertEqual(Conversation.objects.get(channel=Channel.SMS).state["step"], "choose")

    def test_antenatal_nudges_stop_after_the_pregnancy(self):
        consented(is_pregnant=True, pregnancy_noted_at=timezone.now() - timedelta(days=300))
        call_command("afya_send_reminders", stdout=StringIO())
        self.assertFalse(ReminderLog.objects.exists())

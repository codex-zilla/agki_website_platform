import hashlib
import hmac
import json
from datetime import date, timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from afya import bookings, guide, outbound, payments
from afya.analytics import district_report, voucher_alerts
from afya.channels import ussd
from afya.models import (Booking, Channel, Client, Need, NeedEvent, Payment, Provider, Voucher,
                         WalletEntry)
from afya.tests.helpers import consented, provider, reset_outboxes

PHONE = "255712345678"
TOKEN = "test-webhook-token-" + "x" * 32


class PaymentRuleTests(TestCase):
    def setUp(self):
        self.clinic = provider()
        self.public = provider("Dispensary", kind=Provider.Kind.PUBLIC)

    def test_five_percent_capped(self):
        mm = Booking.PayWith.MOBILE_MONEY
        self.assertEqual(payments.commission_for(self.clinic, 10000, mm), 500)
        self.assertEqual(payments.commission_for(self.clinic, 40000, mm), 2000)
        self.assertEqual(payments.commission_for(self.clinic, 100000, mm), 2000)

    def test_zero_percent_cases(self):
        mm = Booking.PayWith.MOBILE_MONEY
        self.assertEqual(payments.commission_for(self.public, 10000, mm), 0)
        self.assertEqual(payments.commission_for(self.clinic, 10000, Booking.PayWith.VOUCHER), 0)
        self.clinic.commission_free_until = date.today() + timedelta(days=90)
        self.assertEqual(payments.commission_for(self.clinic, 10000, mm), 0)

    def test_split_adds_up(self):
        commission, share = payments.split(self.clinic, 15000, Booking.PayWith.MOBILE_MONEY)
        self.assertEqual(commission + share, 15000)


class ConversationTests(TestCase):
    def setUp(self):
        reset_outboxes()
        self.clinic = provider(services=((Need.FAMILY_PLANNING, 0), (Need.STI, 8000)))

    def say(self, text, phone=PHONE, channel=Channel.SMS):
        return guide.handle(phone, text, channel)

    def test_full_booking_in_swahili(self):
        self.assertIn("NDIYO", self.say("nataka uzazi wa mpango"))
        self.assertIn("wilaya", self.say("ndiyo"))
        options = self.say("Kibaha")
        self.assertIn("Kibaha Clinic", options)
        self.assertIn("BURE", options)
        booked = self.say("1")
        self.assertIn("Miadi imewekwa", booked)
        booking = Booking.objects.get()
        self.assertEqual(booking.pay_with, Booking.PayWith.FREE)
        self.assertTrue(NeedEvent.objects.get().booked)
        # The provider is told about the booking but not her number.
        to_provider = [text for _, phone, text in outbound.OUTBOX if phone == "0712000000"]
        self.assertTrue(to_provider)
        self.assertNotIn(PHONE, to_provider[0])

    def test_english_and_place_in_first_message(self):
        self.say("I need family planning in Kibaha")
        reply = self.say("yes")
        self.assertIn("near you", reply)

    def test_danger_comes_before_consent(self):
        reply = self.say("Ninatokwa na damu, nina mimba")
        self.assertIn("SASA", reply)
        self.assertFalse(Client.objects.get(phone=PHONE).has_consented)
        self.assertEqual(NeedEvent.objects.get().danger_sign, "bleeding_pregnancy")

    def test_declining_consent_keeps_nothing(self):
        self.say("habari")
        self.say("hapana")
        self.assertFalse(Client.objects.filter(phone=PHONE).exists())

    def test_no_match_is_recorded_as_a_gap(self):
        consented(region="Pwani", district="Kibaha")
        self.assertIn("bado hatuna", self.say("nataka kupima VVU"))
        self.assertFalse(NeedEvent.objects.get().matched)

    def test_voucher_makes_it_free(self):
        consented(region="Pwani", district="Kibaha")
        voucher = Voucher.issue("UNFPA", 10000, need=Need.STI)
        self.assertIn(voucher.code, self.say(f"vocha {voucher.code.lower()}"))
        self.say("nina uchafu ukeni, nataka kipimo cha zinaa")
        self.assertIn("BURE", self.say("1"))
        booking = Booking.objects.get()
        self.assertEqual(booking.voucher, voucher)
        payment = bookings.attended(bookings.confirm(booking, timezone.now()))
        self.assertEqual((payment.status, payment.commission_tsh), (Payment.Status.PAID, 0))
        voucher.refresh_from_db()
        self.assertIsNotNone(voucher.redeemed_at)

    def test_used_voucher_is_refused(self):
        consented()
        voucher = Voucher.issue("UNFPA", 5000)
        voucher.redeemed_at = timezone.now()
        voucher.save()
        self.assertIn("si sahihi", self.say(f"VOCHA {voucher.code}"))

    def test_mobile_money_split_and_payout(self):
        self.clinic.mobile_money_number = "0755000000"
        self.clinic.save()
        consented(region="Pwani", district="Kibaha")
        self.say("nataka kipimo cha zinaa")
        self.say("1")
        booking = Booking.objects.get()
        payment = bookings.attended(bookings.confirm(booking, timezone.now()))
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(payments.SandboxGateway.requests[0][1], 8000)
        payments.confirm_payment(payment.gateway_reference, succeeded=True)
        payment.refresh_from_db()
        self.assertEqual((payment.commission_tsh, payment.provider_share_tsh), (400, 7600))
        self.assertEqual(payments.SandboxGateway.payouts[0][:2], ("0755000000", 7600))

    def test_wallet_pays_when_balance_is_enough(self):
        client = consented(region="Pwani", district="Kibaha")
        payments.deposit(client, 10000, "test")
        self.assertIn("10,000", self.say("salio"))
        self.say("nataka kipimo cha zinaa")
        self.assertIn("Afya Wallet", self.say("1"))
        bookings.attended(bookings.confirm(Booking.objects.get(), timezone.now()))
        self.assertEqual(client.wallet_balance, 2000)

    def test_stop_and_erase(self):
        consented()
        self.say("acha")
        self.assertTrue(Client.objects.get(phone=PHONE).reminders_opt_out)
        self.say("futa")
        self.assertFalse(Client.objects.filter(phone=PHONE).exists())

    def test_erase_keeps_clinic_records_but_not_her(self):
        consented(region="Pwani", district="Kibaha")
        self.say("nataka uzazi wa mpango")
        self.say("1")
        self.say("delete")
        self.assertIsNone(Booking.objects.get().client)

    def test_web_visitor_must_verify_number_before_booking(self):
        web = "web:abc"
        self.say("nataka uzazi wa mpango Kibaha", web, Channel.WEB)
        self.say("ndiyo", web, Channel.WEB)
        self.assertIn("simu", self.say("1", web, Channel.WEB))
        self.say("0712345678", web, Channel.WEB)
        code_sms = outbound.OUTBOX[-1]
        self.assertEqual(code_sms[1], PHONE)
        code = "".join(ch for ch in code_sms[2] if ch.isdigit())
        self.assertIn("Namba si sahihi", self.say("0000" if code != "0000" else "1111", web, Channel.WEB))
        reply, now_known_as = guide.converse(web, code, Channel.WEB)
        self.assertIn("Miadi imewekwa", reply)
        self.assertEqual(now_known_as, PHONE)
        self.assertEqual(Booking.objects.get().client.phone, PHONE)

    def test_rating_updates_provider(self):
        client = consented(region="Pwani", district="Kibaha")
        self.say("nataka uzazi wa mpango")
        self.say("1")
        booking = Booking.objects.get()
        bookings.attended(bookings.confirm(booking, timezone.now()))
        conversation = client.conversations.get(channel=Channel.SMS)
        conversation.state = {"step": "rating", "booking": booking.pk}
        conversation.save()
        self.assertIn("Asante", self.say("5"))
        self.clinic.refresh_from_db()
        self.assertEqual((self.clinic.rating_count, self.clinic.rating_total), (1, 5))


class USSDTests(TestCase):
    def setUp(self):
        provider(services=((Need.FAMILY_PLANNING, 0),))

    def test_consent_menu_and_booking(self):
        self.assertTrue(ussd.respond("s1", PHONE, "").startswith("CON"))
        self.assertIn("Tafuta huduma", ussd.respond("s1", PHONE, "1"))
        self.assertIn("Uzazi wa mpango", ussd.respond("s1", PHONE, "1*1"))
        self.assertIn("wilaya", ussd.respond("s1", PHONE, "1*1*3"))
        self.assertIn("Kibaha Clinic", ussd.respond("s1", PHONE, "1*1*3*Kibaha"))
        final = ussd.respond("s1", PHONE, "1*1*3*Kibaha*1")
        self.assertTrue(final.startswith("END Miadi"))
        self.assertIn("imewekwa", final)
        self.assertEqual(Booking.objects.get().channel, Channel.USSD)
        # Next session: no consent screen.
        self.assertIn("Tafuta huduma", ussd.respond("s2", PHONE, ""))

    def test_emergency_menu(self):
        consented()
        self.assertIn("112", ussd.respond("s1", PHONE, "5"))


@override_settings(AFYA_WEBHOOK_TOKEN=TOKEN)
class WebhookTests(TestCase):
    def setUp(self):
        reset_outboxes()
        provider()

    def test_sms_requires_token(self):
        url = reverse("afya:sms_inbound")
        self.assertEqual(self.client.post(url, {"from": PHONE, "text": "hi"}).status_code, 403)
        response = self.client.post(f"{url}?token={TOKEN}", {"from": "0712345678", "text": "habari"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("NDIYO", response.json()["reply"])
        self.assertEqual(outbound.OUTBOX[-1][1], PHONE)

    def test_ussd_endpoint(self):
        response = self.client.post(reverse("afya:ussd") + f"?token={TOKEN}",
                                    {"sessionId": "x", "phoneNumber": "+255712345678", "text": ""})
        self.assertTrue(response.content.decode().startswith("CON"))

    @override_settings(AFYA_WHATSAPP_VERIFY_TOKEN="v", AFYA_WHATSAPP_APP_SECRET="app")
    def test_whatsapp_verify_and_message(self):
        url = reverse("afya:whatsapp")
        ok = self.client.get(url, {"hub.mode": "subscribe", "hub.verify_token": "v",
                                   "hub.challenge": "42"})
        self.assertEqual(ok.content, b"42")
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": PHONE, "type": "text", "text": {"body": "nataka uzazi wa mpango"}}]}}]}]}
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"app", body, hashlib.sha256).hexdigest()
        self.client.post(url, body, content_type="application/json",
                         HTTP_X_HUB_SIGNATURE_256=signature)
        self.assertEqual(outbound.OUTBOX[-1][0], Channel.WHATSAPP)

    def test_web_chat(self):
        page = self.client.get(reverse("afya:chat"))
        self.assertContains(page, "Afya Mfukoni")
        reply = self.client.post(reverse("afya:chat_api"), json.dumps({"message": "habari"}),
                                 content_type="application/json")
        self.assertIn("NDIYO", reply.json()["reply"])

    def test_wallet_deposit_callback_is_idempotent(self):
        client = consented()
        reference = payments.request_deposit(client, 3000)
        url = reverse("afya:payment_callback") + f"?token={TOKEN}"
        body = json.dumps({"reference": reference, "status": "success"})
        self.client.post(url, body, content_type="application/json")
        self.client.post(url, body, content_type="application/json")
        self.assertEqual(WalletEntry.objects.count(), 1)
        self.assertEqual(client.wallet_balance, 3000)


class AnalyticsTests(TestCase):
    def test_small_counts_are_suppressed(self):
        for _ in range(6):
            NeedEvent.objects.create(region="Pwani", district="Kibaha", need=Need.HIV,
                                     channel=Channel.SMS, language="sw", matched=False)
        NeedEvent.objects.create(region="Pwani", district="Mafia", need=Need.HIV,
                                 channel=Channel.SMS, language="sw")
        rows = {r["district"]: r for r in district_report(date.today() - timedelta(days=1))}
        self.assertEqual((rows["Kibaha"]["requests"], rows["Kibaha"]["gap_rate"]), (6, "100%"))
        self.assertEqual(rows["Mafia"]["requests"], "<5")

    def test_report_has_no_personal_data(self):
        field_names = {f.name for f in NeedEvent._meta.get_fields()}
        self.assertFalse(field_names & {"phone", "client", "text"})

    def test_voucher_alert_for_one_phone_many_vouchers(self):
        for _ in range(4):
            v = Voucher.issue("Sponsor", 1000)
            v.claimed_phone_hash = "same"
            v.redeemed_at = timezone.now()
            v.save()
        self.assertTrue(any("One phone" in a for a in voucher_alerts()))


class AdminTests(TestCase):
    def test_every_admin_page_loads_and_confirm_texts_her(self):
        from django.contrib.auth import get_user_model

        reset_outboxes()
        staff = get_user_model().objects.create(username="staff", email="s@agki.tz",
                                          is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        provider()
        consented(region="Pwani", district="Kibaha")
        guide.handle(PHONE, "nataka uzazi wa mpango")
        guide.handle(PHONE, "1")
        for model in ("provider", "booking", "payment", "voucher", "client",
                      "walletentry", "needevent"):
            with self.subTest(model=model):
                self.assertEqual(self.client.get(f"/admin/afya/{model}/").status_code, 200)
        booking = Booking.objects.get()
        self.client.post("/admin/afya/booking/", {
            "action": "confirm", "_selected_action": [booking.pk], "apply": "1",
            "visit_at": "2026-12-01T10:00"})
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CONFIRMED)
        self.assertIn(booking.reference, outbound.OUTBOX[-1][2])

"""The Afya Mfukoni guide: one conversation engine behind every channel.

SMS, WhatsApp and the web chat all call `handle(phone, text, channel)` and send
back the string it returns. USSD uses the same building blocks through menus
(`afya.channels.ussd`).

Order of checks on every message, and why:

1. **Danger signs first**, even before consent. A girl who is bleeding in
   pregnancy is told to go to emergency care on her first message, not asked
   to agree to terms.
2. **Consent**, before anything about her is stored.
3. **Commands** (HELP, STOP, DELETE, VOUCHER, BALANCE, SAVE).
4. **The step she is on** (choosing a clinic, giving her district, rating).
5. Otherwise, **understand a new request** and match her to providers.
"""

import hashlib
import hmac
import re
import secrets
import time

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from afya import outbound, payments
from afya.ai import language as nlp
from afya.ai import ranking, safety
from afya.messages import REASON_WORDS, need_name, price_text, say
from afya.models import (Booking, Channel, Client, Conversation, Language, NeedEvent, Provider,
                         Service, Voucher, WalletEntry)

#: A web visitor's one-time code is good for this long.
OTP_TTL_SECONDS = 10 * 60
#: At most this many codes are texted to one number per hour, so the web chat
#: can't be used to flood someone's phone (or AGKI's SMS bill).
OTP_SENDS_PER_HOUR = 3

YES = {"ndiyo", "ndio", "yes", "y", "sawa", "ok", "okay", "1", "nakubali", "agree"}
NO = {"hapana", "no", "n", "2", "sikubali"}

COMMANDS = {
    "help": {"msaada", "help", "menu", "menyu"},
    "stop": {"acha", "stop", "simamisha"},
    "start": {"anza", "start"},
    "erase": {"futa", "delete", "erase"},
    "voucher": {"vocha", "voucher"},
    "balance": {"salio", "balance"},
    "save": {"weka", "save", "akiba"},
    "english": {"english", "kiingereza"},
    "swahili": {"swahili", "kiswahili"},
}


def parse_phone(text):
    """A Tanzanian mobile number in 255... form, or None."""
    from appointments.notifications.beem import normalise

    digits = re.sub(r"\D", "", text or "")
    if not digits:
        return None
    try:
        return normalise(digits)
    except ValueError:
        return None


def get_client(phone):
    client, _ = Client.objects.get_or_create(phone=phone)
    return client


def converse(phone, text, channel=Channel.SMS):
    """Answer one message. Returns (reply, phone the client is now known by).

    The phone changes when a web visitor gives her number to book, and is
    None if she declined consent or erased her data.
    """
    text = (text or "").strip()
    client = get_client(phone)
    conversation, _ = Conversation.objects.get_or_create(client=client, channel=channel)
    session = Session(client, conversation, channel)
    reply = session.respond(text)
    still_there = Client.objects.filter(pk=session.client.pk).values_list("phone", flat=True)
    return reply, still_there.first()


def handle(phone, text, channel=Channel.SMS):
    """Answer one incoming message. Returns the reply text."""
    return converse(phone, text, channel)[0]


class Session:
    def __init__(self, client, conversation, channel):
        self.client = client
        self.conversation = conversation
        self.channel = channel
        self.state = dict(conversation.state or {})
        #: The booking made by this message, if any.
        self.booking = None

    # -- helpers ------------------------------------------------------------

    @property
    def lang(self):
        return self.client.language

    def save_state(self, **state):
        self.state = state
        self.conversation.state = state
        self.conversation.save(update_fields=["state", "updated_at"])

    def clear_state(self):
        self.save_state()

    # -- the pipeline -------------------------------------------------------

    def respond(self, text):
        if not text:
            return say("ask_need", self.lang) if self.client.has_consented else \
                self.ask_consent("")

        danger = safety.check(text, known_pregnant=self.client.is_pregnant)
        if danger:
            return self.emergency(text, danger)

        words = nlp.normalise(text).split()
        first = words[0] if words else ""

        if first in COMMANDS["english"] | COMMANDS["swahili"] and len(words) == 1:
            self.client.language = Language.ENGLISH if first in COMMANDS["english"] \
                else Language.SWAHILI
            self.client.save(update_fields=["language"])
            if not self.client.has_consented:
                return say("consent", self.lang)
            return say("help", self.lang)

        if not self.client.has_consented:
            return self.consent_step(text, first)

        command = self.command(first, words)
        if command is not None:
            return command

        step = self.state.get("step")
        if step == "choose":
            reply = self.choose(first)
            if reply is not None:
                return reply
        elif step == "phone":
            return self.take_phone(text)
        elif step == "otp":
            return self.check_otp(text)
        elif step == "place":
            region, district = nlp.find_place(nlp.normalise(text))
            if region:
                self.set_place(region, district)
                return self.match(self.state["need"])
            # Not a place we know. Unless she has started a new request,
            # ask again rather than forgetting what she asked for.
            if not nlp.understand(text, self.lang).need:
                return say("ask_place", self.lang)
        elif step == "rating":
            reply = self.rate(first)
            if reply is not None:
                return reply

        return self.new_request(text)

    # -- danger -------------------------------------------------------------

    def emergency(self, text, danger):
        understood = nlp.understand(text, self.lang)
        # Before consent we haven't learnt her language yet: answer in the one she wrote in.
        language = self.lang if self.client.has_consented else understood.language
        region = understood.region or self.client.region
        district = understood.district or self.client.district
        NeedEvent.objects.create(
            region=region, district=district, need=understood.need, channel=self.channel,
            language=language, danger_sign=danger.code,
        )
        reply = danger.message(language)
        nearby = ranking.emergency_providers(region, district)
        if nearby:
            places = "; ".join(f"{p.name} {p.phone}" for p in nearby)
            reply += " " + say("nearest_emergency", language, places=places)
        return reply

    # -- consent ------------------------------------------------------------

    def ask_consent(self, text):
        if text:
            self.client.language = nlp.detect_language(text, self.client.language)
            self.client.save(update_fields=["language"])
        self.save_state(step="consent", pending=text[:300])
        return say("consent", self.lang)

    def consent_step(self, text, first):
        if self.state.get("step") != "consent":
            return self.ask_consent(text)
        if first in YES:
            self.client.consented_at = timezone.now()
            self.client.save(update_fields=["consented_at"])
            pending = self.state.get("pending", "")
            self.clear_state()
            return self.new_request(pending) if pending else say("ask_need", self.lang)
        if first in NO:
            language = self.lang
            if not self.client.bookings.exists():
                self.client.delete()
            return say("consent_declined", language)
        return self.ask_consent(text)

    # -- commands -----------------------------------------------------------

    def command(self, first, words):
        if first in COMMANDS["help"]:
            return say("help", self.lang)
        if first in COMMANDS["stop"]:
            self.client.reminders_opt_out = True
            self.client.save(update_fields=["reminders_opt_out"])
            return say("stopped", self.lang)
        if first in COMMANDS["start"]:
            self.client.reminders_opt_out = False
            self.client.save(update_fields=["reminders_opt_out"])
            return say("started", self.lang)
        if first in COMMANDS["erase"]:
            language = self.lang
            self.client.erase()
            return say("erased", language)
        if first in COMMANDS["voucher"] and len(words) > 1:
            voucher = payments.claim_voucher(self.client, words[1])
            if voucher is None:
                return say("voucher_bad", self.lang)
            return say("voucher_ok", self.lang, code=voucher.code, value=f"{voucher.value_tsh:,}")
        if first in COMMANDS["balance"]:
            return say("balance", self.lang, balance=f"{self.client.wallet_balance:,}")
        if first in COMMANDS["save"]:
            amount = next((int(w) for w in words[1:] if w.isdigit()), 0)
            if amount < 500 or self.client.is_web_visitor:
                return say("bad_amount", self.lang)
            if payments.request_deposit(self.client, amount) is None:
                return say("save_failed", self.lang)
            return say("save_request", self.lang, amount=f"{amount:,}")
        return None

    # -- new request and matching ------------------------------------------

    def set_place(self, region, district):
        self.client.region = region
        self.client.district = district or (
            self.client.district if self.client.region == region else "")
        self.client.save(update_fields=["region", "district"])

    def new_request(self, text):
        understood = nlp.understand(text, self.lang)
        changed = []
        if understood.language != self.client.language and len(understood.text.split()) >= 2:
            self.client.language = understood.language
            changed.append("language")
        if understood.mentions_pregnancy and not self.client.is_pregnant:
            self.client.is_pregnant = True
            self.client.pregnancy_noted_at = timezone.now()
            changed += ["is_pregnant", "pregnancy_noted_at"]
        if understood.is_adolescent and self.client.age_band != Client.AgeBand.ADOLESCENT:
            self.client.age_band = Client.AgeBand.ADOLESCENT
            changed.append("age_band")
        if changed:
            self.client.save(update_fields=changed)
        if understood.region:
            self.set_place(understood.region, understood.district)

        if not understood.need:
            self.clear_state()
            return say("ask_need", self.lang)
        if not self.client.region:
            self.save_state(step="place", need=understood.need)
            return say("ask_place", self.lang)
        return self.match(understood.need)

    def match(self, need):
        voucher_value = max(
            (v.value_tsh for v in self.client.vouchers.filter(redeemed_at__isnull=True)
             if v.is_usable() and (not v.need or v.need == need)),
            default=0,
        )
        matches = ranking.rank(
            need, self.client.region, self.client.district,
            adolescent=self.client.age_band == Client.AgeBand.ADOLESCENT,
            voucher_value=voucher_value,
        )
        event = NeedEvent.objects.create(
            region=self.client.region, district=self.client.district, need=need,
            channel=self.channel, language=self.lang, matched=bool(matches),
        )
        name = need_name(need, self.lang)
        if not matches:
            self.clear_state()
            return say("no_match", self.lang, need=name)

        lines = [say("matches_intro", self.lang, need=name[:1].upper() + name[1:])]
        for number, m in enumerate(matches, start=1):
            reasons = ", ".join(REASON_WORDS[self.lang][r] for r in m.reasons)
            line = (f"{number}. {m.provider.name} ({m.provider.district}) - "
                    f"{price_text(m.service.price_tsh, self.lang)}")
            if m.provider.opening_hours:
                line += f", {m.provider.opening_hours}"
            if reasons:
                line += f" [{reasons}]"
            lines.append(line)
        lines.append(say("matches_outro", self.lang,
                         n="1" if len(matches) == 1 else f"1-{len(matches)}"))
        self.save_state(step="choose", need=need, event=event.pk,
                        options=[m.service.pk for m in matches])
        return "\n".join(lines)

    # -- choosing and booking ----------------------------------------------

    def choose(self, first):
        options = self.state.get("options", [])
        if not first.isdigit():
            return None  # treat as a new request
        index = int(first) - 1
        if not 0 <= index < len(options):
            return say("not_understood_choice", self.lang, n=len(options))
        if self.client.is_web_visitor:
            self.save_state(**{**self.state, "step": "phone", "chosen": options[index]})
            return say("ask_phone", self.lang)
        return self.book(options[index])

    def take_phone(self, text):
        """A web visitor gives her number. We text it a code before trusting it."""
        phone = parse_phone(text)
        if not phone:
            return say("bad_phone", self.lang)
        sends_key = f"afya:otp-sends:{hashlib.sha256(phone.encode()).hexdigest()[:24]}"
        cache.add(sends_key, 0, 60 * 60)
        try:
            sends = cache.incr(sends_key)
        except ValueError:  # expired between add and incr
            cache.set(sends_key, 1, 60 * 60)
            sends = 1
        if sends > OTP_SENDS_PER_HOUR:
            return say("otp_wait", self.lang)
        code = f"{secrets.randbelow(9000) + 1000}"
        self.save_state(**{**self.state, "step": "otp", "pending_phone": phone,
                           "otp": hashlib.sha256(f"{phone}:{code}".encode()).hexdigest(),
                           "otp_tries": 0, "otp_expires": time.time() + OTP_TTL_SECONDS})
        outbound.send_sms(phone, say("otp_sms", self.lang, code=code))
        return say("ask_otp", self.lang)

    def check_otp(self, text):
        phone = self.state.get("pending_phone", "")
        code = re.sub(r"\D", "", text)
        expected = self.state.get("otp", "")
        if time.time() > self.state.get("otp_expires", 0):
            self.save_state(**{**self.state, "step": "phone"})
            return say("otp_expired", self.lang)
        if hmac.compare_digest(hashlib.sha256(f"{phone}:{code}".encode()).hexdigest(), expected):
            return self.attach_phone(phone)
        tries = self.state.get("otp_tries", 0) + 1
        if tries >= 3:
            self.clear_state()
            return say("otp_failed", self.lang)
        self.save_state(**{**self.state, "otp_tries": tries})
        return say("otp_wrong", self.lang)

    def attach_phone(self, phone):
        chosen, event = self.state.get("chosen"), self.state.get("event")
        web_client = self.client
        existing = Client.objects.filter(phone=phone).first()
        with transaction.atomic():
            if existing:
                existing.consented_at = existing.consented_at or web_client.consented_at
                existing.region = existing.region or web_client.region
                existing.district = existing.district or web_client.district
                existing.save()
                # Anything she claimed while anonymous comes with her.
                Voucher.objects.filter(claimed_by=web_client).update(claimed_by=existing)
                WalletEntry.objects.filter(client=web_client).update(client=existing)
                self.client = existing
                self.conversation.client = existing
                Conversation.objects.filter(client=existing, channel=self.channel).delete()
                self.conversation.save(update_fields=["client"])
                web_client.delete()
            else:
                web_client.phone = phone
                web_client.save(update_fields=["phone"])
        self.state = {"event": event, "need": self.state.get("need")}
        return self.book(chosen)

    def book(self, service_id):
        # The list she chose from may be minutes or hours old: the provider
        # could have been suspended or the service withdrawn since.
        service = Service.objects.select_related("provider").filter(
            pk=service_id, is_active=True,
            provider__verification=Provider.Verification.VERIFIED,
        ).first()
        if service is None:
            need = self.state.get("need")
            self.clear_state()
            if need and self.client.region:
                return say("service_gone", self.lang) + "\n" + self.match(need)
            return say("ask_need", self.lang)
        pay_with, voucher = payments.choose_payment(self.client, service)
        booking = Booking.objects.create(
            client=self.client, provider=service.provider, service=service,
            channel=self.channel, pay_with=pay_with, voucher=voucher,
        )
        self.booking = booking
        event_id = self.state.get("event")
        if event_id:
            NeedEvent.objects.filter(pk=event_id).update(booked=True)
        self.clear_state()

        provider = service.provider
        reply = say(
            "booked", self.lang, ref=booking.reference, provider=provider.name,
            district=provider.district, service=service.name(self.lang),
            price=price_text(service.price_tsh, self.lang),
            hours=say("hours", self.lang, hours=provider.opening_hours)
            if provider.opening_hours else "",
        )
        if pay_with == Booking.PayWith.VOUCHER:
            reply += say("booked_voucher", self.lang)
        elif pay_with == Booking.PayWith.WALLET:
            reply += say("booked_wallet", self.lang,
                         balance=f"{self.client.wallet_balance:,}")
        # The provider learns there is a booking, never her number or words.
        outbound.send_sms(
            provider.phone,
            f"Afya Mfukoni: new booking {booking.reference} - {service.name_en}. "
            f"Please confirm a time in your Afya Mfukoni dashboard.",
        )
        return reply

    # -- rating ---------------------------------------------------------------

    def rate(self, first):
        if not first.isdigit() or not 1 <= int(first) <= 5:
            return None
        booking = Booking.objects.filter(pk=self.state.get("booking"), client=self.client).first()
        self.clear_state()
        if booking and booking.rating is None:
            booking.rating = int(first)
            booking.save(update_fields=["rating"])
            booking.provider.add_rating(int(first))
        return say("thanks_rating", self.lang)

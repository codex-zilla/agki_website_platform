"""USSD menus for basic phones, on the short code issued by your USSD aggregator.

Written for the Africa's Talking USSD format, which most Tanzanian USSD
aggregators share: each request carries `sessionId`, `phoneNumber` and
`text`, where `text` is every answer so far joined by `*` ("1*3*Kibaha").
A reply starting "CON " keeps the session open; "END " closes it.
"""

from django.conf import settings
from django.utils import timezone

from afya import outbound, payments
from afya.ai import language as nlp
from afya.ai import ranking
from afya.guide import Session, get_client
from afya.messages import need_name, price_text, say
from afya.models import Booking, Channel, Conversation, Language, Need, NeedEvent

NEEDS = [Need.ANTENATAL, Need.PREGNANCY_TEST, Need.FAMILY_PLANNING, Need.HIV, Need.STI,
         Need.MENSTRUAL, Need.POSTNATAL, Need.CERVICAL, Need.MENTAL_HEALTH,
         Need.LAB_TEST, Need.GENERAL]

MENU = {
    "sw": ("Afya Mfukoni\n1. Tafuta huduma\n2. Miadi yangu\n3. Vocha\n"
           "4. Salio Afya Wallet\n5. Dharura\n6. English"),
    "en": ("Afya Mfukoni\n1. Find a service\n2. My bookings\n3. Voucher\n"
           "4. Afya Wallet balance\n5. Emergency\n6. Kiswahili"),
}
CONSENT = {
    "sw": "Afya Mfukoni: taarifa zako ni SIRI na hazitauzwa.\n1. Nakubali\n2. Hapana",
    "en": "Afya Mfukoni: your information is PRIVATE and never sold.\n1. I agree\n2. No",
}
EMERGENCY = {
    "sw": ("Dalili za hatari: kutokwa damu, degedege, kichwa kuuma sana, kuona giza, maji "
           "kutoka, mtoto kuacha kucheza, homa ukiwa mjamzito. Nenda hospitali SASA au piga {n}."),
    "en": ("Danger signs: bleeding, fits, severe headache, blurred vision, waters breaking, "
           "baby not moving, fever in pregnancy. Go to hospital NOW or call {n}."),
}
LABELS = {
    "choose_need": {"sw": "Chagua huduma:", "en": "Choose a service:"},
    "type_place": {"sw": "Andika wilaya au mkoa wako:", "en": "Type your district or region:"},
    "place_unknown": {"sw": "Hatukutambua eneo hilo. Jaribu tena.",
                      "en": "We didn't recognise that place. Please try again."},
    "type_voucher": {"sw": "Andika namba ya vocha:", "en": "Enter your voucher code:"},
    "no_bookings": {"sw": "Huna miadi bado.", "en": "You have no bookings yet."},
    "invalid": {"sw": "Chaguo si sahihi.", "en": "Invalid choice."},
}

#: Short names for the menu: a USSD screen holds about 182 characters, and the
#: full names in `messages.NEED_NAMES` would not fit eleven to a screen.
SHORT_NEED = {
    "sw": {Need.ANTENATAL: "Ujauzito", Need.PREGNANCY_TEST: "Kipimo mimba",
           Need.FAMILY_PLANNING: "Uzazi wa mpango", Need.HIV: "VVU/PrEP", Need.STI: "Zinaa",
           Need.MENSTRUAL: "Hedhi", Need.POSTNATAL: "Baada kujifungua",
           Need.CERVICAL: "Saratani kizazi", Need.MENTAL_HEALTH: "Afya ya akili",
           Need.LAB_TEST: "Maabara", Need.GENERAL: "Daktari"},
    "en": {Need.ANTENATAL: "Pregnancy care", Need.PREGNANCY_TEST: "Pregnancy test",
           Need.FAMILY_PLANNING: "Family planning", Need.HIV: "HIV/PrEP", Need.STI: "STI",
           Need.MENSTRUAL: "Periods", Need.POSTNATAL: "After birth",
           Need.CERVICAL: "Cervical screen", Need.MENTAL_HEALTH: "Mental health",
           Need.LAB_TEST: "Lab tests", Need.GENERAL: "Doctor"},
}

#: Most aggregators cut a screen at 182 characters, some reject it outright.
SCREEN_LIMIT = 182


def _fit(text):
    return text if len(text) <= SCREEN_LIMIT else text[:SCREEN_LIMIT - 3] + "..."


def con(text):
    return _fit(f"CON {text}")


def end(text):
    return _fit(f"END {text}")


def _save(conversation, state):
    conversation.state = state
    conversation.save(update_fields=["state", "updated_at"])


def respond(session_id, phone, text):
    """Answer one USSD request. `text` is every answer this session, joined by `*`."""
    client = get_client(phone)
    conversation, _ = Conversation.objects.get_or_create(client=client, channel=Channel.USSD)
    state = dict(conversation.state or {})
    if state.get("session") != session_id:
        # A new dial: forget the last session's menus and lists.
        state = {"session": session_id, "offset": 0}
    parts = text.split("*") if text else []
    # Answers that were not menu choices (consent, switching language) are
    # counted in `offset` and skipped.
    parts = parts[state["offset"]:]
    lang = client.language

    if not client.has_consented:
        if not parts:
            return con(CONSENT[lang])
        if parts[0] != "1":
            if not client.bookings.exists():
                client.delete()
            return end(say("consent_declined", lang))
        client.consented_at = timezone.now()
        client.save(update_fields=["consented_at"])
        state["offset"] += 1
        _save(conversation, state)
        parts = parts[1:]

    if not parts:
        return con(MENU[lang])

    choice, rest = parts[0], parts[1:]
    if choice == "1":
        return find_service(client, conversation, state, rest)
    if choice == "2":
        bookings = Booking.objects.filter(client=client).select_related("provider")[:3]
        if not bookings:
            return end(LABELS["no_bookings"][lang])
        return end("\n".join(f"{b.reference} {b.provider.name[:30]}: {b.get_status_display()}"
                             for b in bookings))
    if choice == "3":
        if not rest:
            return con(LABELS["type_voucher"][lang])
        voucher = payments.claim_voucher(client, rest[0])
        if voucher is None:
            return end(say("voucher_bad", lang))
        return end(say("voucher_ok", lang, code=voucher.code, value=f"{voucher.value_tsh:,}"))
    if choice == "4":
        return end(say("balance", lang, balance=f"{client.wallet_balance:,}"))
    if choice == "5":
        return end(EMERGENCY[lang].format(n=getattr(settings, "AFYA_EMERGENCY_NUMBER", "112")))
    if choice == "6":
        client.language = Language.ENGLISH if lang == Language.SWAHILI else Language.SWAHILI
        client.save(update_fields=["language"])
        # "6" was not a menu choice: her next answer is a choice on the new menu.
        state["offset"] += 1
        _save(conversation, state)
        return con(MENU[client.language])
    return end(LABELS["invalid"][lang])


def find_service(client, conversation, state, rest):
    lang = client.language
    if not rest:
        lines = [LABELS["choose_need"][lang]]
        lines += [f"{i}. {SHORT_NEED[lang][n]}" for i, n in enumerate(NEEDS, start=1)]
        return con("\n".join(lines))
    if not rest[0].isdigit() or not 1 <= int(rest[0]) <= len(NEEDS):
        return end(LABELS["invalid"][lang])
    need = NEEDS[int(rest[0]) - 1]
    rest = rest[1:]

    # Ask for the place every session: she may be travelling.
    if not rest:
        return con(LABELS["type_place"][lang])
    region, district = nlp.find_place(nlp.normalise(rest[0]))
    if not region:
        return end(LABELS["place_unknown"][lang])
    rest = rest[1:]

    if not rest:
        client.region, client.district = region, district
        client.save(update_fields=["region", "district"])
        matches = ranking.rank(need, region, district,
                               adolescent=client.age_band == client.AgeBand.ADOLESCENT)
        event = NeedEvent.objects.create(region=region, district=district, need=need,
                                         channel=Channel.USSD, language=lang,
                                         matched=bool(matches))
        if not matches:
            return end(say("no_match", lang, need=need_name(need, lang)))
        # Remember exactly what she was shown, so "2" books the second line
        # even if the ranking changes before she answers.
        _save(conversation, {**state, "options": [m.service.pk for m in matches],
                             "event": event.pk, "need": need})
        lines = [f"{i}. {m.provider.name[:40]} - {price_text(m.service.price_tsh, lang)}"
                 for i, m in enumerate(matches, start=1)]
        return con("\n".join(lines))

    options = state.get("options", [])
    if not rest[0].isdigit() or not 1 <= int(rest[0]) <= len(options):
        return end(LABELS["invalid"][lang])
    session = Session(client, conversation, Channel.USSD)
    session.state = {"event": state.get("event"), "need": need}
    full = session.book(options[int(rest[0]) - 1])
    booking = session.booking
    if booking is None:
        return end(full)  # the service was withdrawn; `full` says so
    # The USSD screen vanishes when she dismisses it, so the details go by SMS too.
    outbound.send_sms(client.phone, full)
    return end(say("ussd_booked", lang, ref=booking.reference,
                   provider=booking.provider.name[:40]))

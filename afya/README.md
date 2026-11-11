# Afya Mfukoni — "health in your pocket"

An AI health guide in Kiswahili and English that helps girls and women in
Tanzania find trusted, verified health providers on **any phone**: SMS, USSD,
WhatsApp or the web. Built by AGKI Medical Laboratory as a Django app inside the
AGKI platform.

> The AI **guides, it never diagnoses**. Danger signs always send her to
> emergency care first.

## What it does

| Feature | Where in the code |
|---|---|
| Understands everyday Swahili/English, SMS shorthand, typos and local terms | `ai/language.py` |
| Clinician-reviewable danger-sign rules, checked before anything else | `ai/safety.py` |
| Ranks verified providers by distance, cost, rating, youth-friendliness | `ai/ranking.py` |
| One conversation engine for every channel | `guide.py` |
| SMS (two-way), USSD menus, WhatsApp Cloud API, web chat | `views.py`, `channels/ussd.py` |
| Private booking; providers never see her messages | `guide.py`, `bookings.py` |
| Mobile-money split: 5% capped at TSh 2,000; 0% for public facilities and vouchers | `payments.py` |
| Partner-sponsored SMS vouchers and the Afya Wallet (savings ledger) | `payments.py`, `models.py` |
| Visit reminders, antenatal-care nudges, rating requests | `management/commands/afya_send_reminders.py` |
| Anonymised district service-gap report; voucher misuse alerts | `analytics.py`, `afya_gap_report` |
| Staff admin: verify providers, confirm bookings, issue vouchers | `admin.py` |

## A conversation by SMS

Recorded from this code with the real public-hospital list loaded and Tumbi
Regional Referral Hospital verified (`afya/data/providers_tz_public.csv`):

```
Her:  habari, nina mimba nataka kliniki
AF:   Karibu Afya Mfukoni! Tunakusaidia kupata huduma za afya zinazoaminika. Taarifa zako ni SIRI na hazitauzwa. Jibu NDIYO kukubali, au HAPANA.
Her:  ndiyo
AF:   Uko wilaya au mkoa gani? Andika jina la wilaya yako.
Her:  niko Kibaha
AF:   Huduma ya Kliniki ya ujauzito karibu nawe:
      1. Tumbi Regional Referral Hospital (Kibaha) - BURE [karibu, bure]
      Jibu namba (1) kuweka miadi kwa siri.
Her:  1
AF:   Miadi imewekwa! Namba: AF613644. Tumbi Regional Referral Hospital, Kibaha. Kliniki ya ujauzito: BURE. Kliniki itakuthibitishia muda kwa SMS.
```

If her first message is *"Ninatokwa na damu, nina mimba"*, she is told
immediately to go to emergency care (and given the nearest emergency-capable
facility), before any consent question.

**SMS commands:** `MSAADA/HELP`, `VOCHA/VOUCHER <code>`, `SALIO/BALANCE`,
`WEKA/SAVE <amount>`, `ACHA/STOP`, `ANZA/START`, `FUTA/DELETE`, `ENGLISH/KISWAHILI`.

## Privacy by design

- Consent before anything about her is stored; "HAPANA" keeps nothing.
- Only a phone number, no name, no ID. Staff see masked numbers.
- Providers get "new booking AF123456", never her number or her words.
- `FUTA` erases her record; clinic payment rows stay but lose the link to her.
- Planners' data (`NeedEvent`) has no link to any person, and counts under 5
  are suppressed in reports.
- Web visitors verify their number with a one-time SMS code before booking.
- Numbers that write once but never agree are deleted after 24 hours
  (`afya_cleanup`).
- Asking for a pregnancy test, or saying "sina mimba", never marks her as
  pregnant, so no antenatal texts arrive on a phone she may share.
- Logs never hold message text or full numbers (messages can carry one-time
  codes and what care she asked for).
- Data is never sold. Webhooks require a shared secret; WhatsApp requests must
  carry Meta's signature (`AFYA_WHATSAPP_APP_SECRET` is required with DEBUG off).

## Running it

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py afya_import_providers afya/data/providers_tz_public.csv  # real facilities, pending
python manage.py runserver
# Web chat: http://127.0.0.1:8000/afya/
python manage.py test afya
```

### Gateways (all optional while developing)

Set in `.env` (see `.env.example`):

| Setting | Purpose |
|---|---|
| `AFYA_WEBHOOK_TOKEN` | Secret every SMS/USSD/payment webhook must send (`?token=`) |
| `BEEM_*` | Outgoing SMS (already used by the platform) |
| `AFYA_WHATSAPP_TOKEN`, `AFYA_WHATSAPP_PHONE_ID`, `AFYA_WHATSAPP_VERIFY_TOKEN`, `AFYA_WHATSAPP_APP_SECRET` | WhatsApp Cloud API |
| `AFYA_PAYMENT_GATEWAY` | Python path of a `MobileMoneyGateway`; default sandbox moves no money |
| `AFYA_COMMISSION_RATE`, `AFYA_COMMISSION_CAP_TSH` | 0.05 and 2000 |
| `AFYA_EMERGENCY_NUMBER` | 112 by default; confirm before launch |

Webhook URLs to give your providers:

- SMS: `POST /afya/webhooks/sms/?token=...` (fields `from`, `text`)
- USSD: `POST /afya/webhooks/ussd/?token=...` (Africa's Talking format)
- WhatsApp: `GET/POST /afya/webhooks/whatsapp/`
- Payments: `POST /afya/webhooks/payments/?token=...` (`{"reference", "status"}`)

### Loading real providers

```bash
python manage.py afya_import_providers afya/data/providers_tz_public.csv
```

`afya/data/providers_tz_public.csv` lists real public hospitals in the pilot
districts; `providers_template.csv` is for AGKI's partner clinics. Everything
imported is *pending* and never shown to women until staff fill in the phone,
licence (HFR code) and prices, then verify it. See `afya/data/README.md`.

### Daily jobs (cron)

```
0 9 * * *  python manage.py afya_send_reminders
30 2 * * * python manage.py afya_cleanup
0 6 * * 1  python manage.py afya_gap_report --days 90 --out /path/gaps.csv --alerts
```

### Shared cache (required with more than one worker)

Web-chat rate limits, the one-time-code limit (3 codes per number per hour)
and WhatsApp de-duplication use Django's cache. The default in-memory cache is
per process, so with several gunicorn workers set `CACHES` to Redis or the
database cache, or the limits can be bypassed.

### Payments and payouts

- The way a visit is paid is re-checked when it is marked attended. If her
  Afya Wallet no longer covers it, or the voucher was used elsewhere, it
  falls back to a mobile-money request she approves on her phone.
- The provider's share is paid out for mobile-money, wallet **and** voucher
  visits (the money sits with AGKI in all three). A provider without a payout
  number is recorded as owed: filter Payments by "Still owed to provider" in
  the admin and use **Pay out provider share now** once the number is added.
  Payouts are claimed before sending, so retrying never pays twice.
- Wallet-deposit references are signed with `SECRET_KEY`. Rotating
  `SECRET_KEY` invalidates deposit requests still waiting for approval.
- Booking status and payment method can only change through the admin
  actions (which text her and settle payment), not by editing the record.

## Before going live

Run `python manage.py check --deploy` on the live server. It reports an error
while `AFYA_WEBHOOK_TOKEN` is missing, a placeholder or under 32 characters
(afya.E001; the webhooks also refuse every request until it is fixed), or
while WhatsApp is half-configured (afya.E002/E003). It warns (afya.W001-W004)
until the clinical sign-off, a real payment gateway, a shared cache and Beem
SMS credentials are in place.

1. **Clinical sign-off.** A clinician must review `ai/safety.py` and fill in
   `CLINICAL_REVIEW`. The admin shows a warning until then. Version
   `2026.11-draft.2` adds English contractions ("can't breathe", "bleeding won't
   stop") that the first draft missed.
2. **Real mobile-money gateway.** Write a `MobileMoneyGateway` subclass for
   your aggregator (M-Pesa, Airtel Money, Mixx by Yas or a payment
   aggregator) and set `AFYA_PAYMENT_GATEWAY`.
3. **USSD short code and two-way SMS** from an aggregator, pointed at the
   webhook URLs above.
4. **Data protection.** Register with Tanzania's Personal Data Protection
   Commission and review this design with them.
5. **Verify providers** against council registers before marking them verified.

## Improving the AI

The language model is a reviewed vocabulary (`NEED_PHRASES`) plus fuzzy
matching, so it runs free on any server and never sends her words to a third
party. To improve it, review anonymised "ask_need" misses with the clinical
team, add the phrases women actually use, and add a test in
`tests/test_ai.py` for each one. A hosted language model can be added later
behind the same `understand()` function without changing any channel.

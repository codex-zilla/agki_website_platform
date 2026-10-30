"""Notifications: emit an event, and the right people hear about it.

    from appointments.notifications import notify
    notify("appointment.booked", appointment)

That is the whole public surface. Views and `transition_to()` never construct a
message, never name a channel and never call `send_mail` — that coupling is
exactly what would force a rewrite the day SMS arrives (decision 3), and it is
what the old code did: `send_mail` inline, in a view, with the copy in a
Python string.

**Delivery is queued by default.** `notify()` writes `NotificationLog` rows and
returns; `manage.py send_pending_notifications` (cron) delivers them. A patient
submitting a booking form should not be waiting on an SMTP handshake, and a
mail server having a bad afternoon should not be able to fail a booking that
has already been recorded. Set `NOTIFICATIONS_DELIVERY = "immediate"` to send
inline instead — the default outside production, so a developer sees the
message in their console without running cron.
"""

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse

from . import channels as channel_registry
from . import events as event_registry
from .events import PATIENT, PROVIDER

__all__ = ["notify", "deliver", "render_message", "delivery_mode"]


def _site_url():
    return getattr(settings, "SITE_URL", "").rstrip("/")


def message_context(event, appointment, audience, extra=None):
    """What every message template can rely on.

    `appointment_url` points at the **lookup form with the booking ID already
    filled in**, not straight at the appointment. The detail page needs a
    verification step the patient has not done yet when they open an email, so
    a direct link would bounce them to a form and lose the reference they
    arrived with. This way the link carries the booking ID and asks only for
    the one thing that proves the record is theirs (design §8.1).
    """
    lookup = f"{_site_url()}{reverse('booking_id_lookup')}?bookingID={appointment.bookingID}"
    # The same destination, 29 characters shorter, for the channel that is
    # charged by the character (plan 13.3).
    short = f"{_site_url()}{reverse('appointment_short_link', args=[appointment.bookingID])}"
    dashboard = f"{_site_url()}{reverse('doctor_appointment_details', args=[appointment.id])}"
    context = {
        "appointment": appointment,
        "booking_id": appointment.bookingID,
        "slot": appointment.slot,
        "slot_date": appointment.slot.date,
        "slot_time": appointment.slot.time,
        "provider": appointment.slot.user,
        "audience": audience,
        "is_patient": audience == PATIENT,
        "is_provider": audience == PROVIDER,
        "appointment_url": lookup,
        "short_url": short,
        "dashboard_url": dashboard,
        "site_url": _site_url(),
    }
    context.update(extra or {})
    return context


def render_message(event, appointment, audience, extra=None, channel="email"):
    """The subject and body for one audience on one channel.

    The subject is rendered for both channels and stored on both, though only
    email shows it: `NotificationLog` is the audit trail as well as the queue,
    and "what was this message about" should not depend on how it went out.
    """
    context = message_context(event, appointment, audience, extra)
    subject = event.subject_for(audience).format(**context)
    body = render_to_string(event.template_for(channel), context)
    return subject, body.strip() + "\n"


def notify(event_name, appointment, to=None, channels=None, **extra):
    """Emit `event_name` for `appointment`. Returns the queued rows.

    `to` overrides the event's declared audience with one literal address —
    the escape hatch for a genuine one-off. It is not the normal path: who
    hears an event is a property of the event (see events.py).
    """
    from appointments.models import NotificationLog

    event = event_registry.get(event_name)
    order = tuple(channels or event.channels or site_channels())
    queued = []

    # Every channel that can reach them, not the first one (AGKI, 2026-09-04).
    # The text arrives; the email is the copy that still has the reference in
    # it next week. A person with only one of the two gets that one, and only
    # somebody with neither is recorded as skipped.
    for audience in event.audience:
        reached = False
        for channel_name in order:
            channel = channel_registry.get(channel_name)
            address = to or channel.address_for(audience, appointment)
            if not address:
                continue

            reached = True
            subject, body = render_message(
                event, appointment, audience, extra, channel=channel_name
            )
            queued.append(
                NotificationLog.objects.create(
                    appointment=appointment,
                    event=event.name,
                    recipient=address,
                    channel=channel_name,
                    status=NotificationLog.Status.PENDING,
                    audience=audience,
                    subject=subject,
                    body=body,
                )
            )

        if not reached:
            # Recorded rather than dropped: "we never had a way to reach them"
            # is an answer, and silence is not.
            NotificationLog.objects.create(
                appointment=appointment, event=event.name, recipient="",
                channel=order[-1], status=NotificationLog.Status.SKIPPED,
                audience=audience,
                error=f"No way to reach the {audience} on {' or '.join(order)}.",
            )

    if delivery_mode() == "immediate":
        # After the surrounding transaction commits, so a booking that rolls
        # back cannot email somebody about an appointment that does not exist.
        for message in queued:
            transaction.on_commit(lambda m=message: deliver(m))

    return queued


def delivery_mode():
    return getattr(settings, "NOTIFICATIONS_DELIVERY", "queued")


def sms_is_configured():
    """Has anybody actually set the gateway up?

    Queueing SMS on a deployment with no credentials would fill the outbox
    with rows that can only fail, three attempts each. The absence of a key is
    a legitimate state - a developer's laptop, a staging box - not a
    misconfiguration to shout about.
    """
    return bool(
        getattr(settings, "BEEM_API_KEY", "")
        and getattr(settings, "BEEM_SECRET_KEY", "")
        and getattr(settings, "BEEM_SENDER_ID", "")
    )


def site_channels():
    """Every channel a message goes out on, in order.

    **SMS and email, both, whenever both can reach the person** (AGKI,
    2026-09-04). 13.1 sent one or the other and used the second only as a
    fallback; AGKI corrected it, and the reason is the deployment rather than
    the design: most patients here do not read email often, so the text is the
    message that arrives - and the email is the copy that still has the
    booking reference in it next week when the text has scrolled away.

    "First" survives as **order**, which is what the SMS templates are written
    against: the text is the one that has to stand alone.
    """
    order = getattr(settings, "NOTIFICATION_CHANNELS", None)
    if order:
        return tuple(order)
    return ("sms", "email") if sms_is_configured() else ("email",)


def deliver(message):
    """Attempt one queued message. Returns True if it went out.

    **Claims the row first, atomically.** The `PENDING -> SENDING` update below
    is the lock: only one caller can win it, so two overlapping delivery runs
    cannot both send the same message. Counting attempts was not enough — a run
    starting while another was mid-send would see the row still pending and
    deliver it a second time, which is precisely the situation a queue exists
    to survive (a slow mail host is what makes runs overlap in the first
    place).

    Never raises. The caller is a cron job draining a queue, and one bad
    address must not stop everybody else's mail.
    """
    from django.db.models import F
    from django.utils import timezone

    from appointments.models import NotificationLog

    claimed = NotificationLog.objects.filter(
        pk=message.pk, status=NotificationLog.Status.PENDING
    ).update(status=NotificationLog.Status.SENDING, attempts=F("attempts") + 1)
    if not claimed:
        return False

    # Re-read rather than trusting the caller's copy: `attempts` was just
    # incremented by the database, and the retry decision below depends on it.
    message.refresh_from_db()

    try:
        channel_registry.get(message.channel).send(message)
    except Exception as error:  # noqa: BLE001 - see docstring
        message.error = f"{type(error).__name__}: {error}"
        message.status = (
            NotificationLog.Status.FAILED
            if message.attempts >= NotificationLog.MAX_ATTEMPTS
            else NotificationLog.Status.PENDING
        )
        message.save(update_fields=["error", "status"])
        return False

    message.status = NotificationLog.Status.SENT
    message.sent_at = timezone.now()
    message.error = ""
    message.save(update_fields=["status", "sent_at", "error"])
    return True


# `_fall_back()` stood here: it queued the next channel when one gave up for
# good, which is what "SMS first, email second" needed while only one channel
# was used at a time. Sending both up front (AGKI, 2026-09-04) makes it dead
# code - a failed SMS has nothing left to chase, because the email was queued
# beside it - and dead code that looks like a safety net is worse than none.


def release_stale_claims():
    """Return abandoned `SENDING` rows to the queue.

    Passenger starts and kills application processes as it pleases, so a
    delivery run can die between claiming a row and resolving it. Without this
    those rows would sit in SENDING forever and nobody would ever be told —
    the silent failure the whole outbox exists to avoid. Returns how many were
    released.
    """
    from django.utils import timezone

    from appointments.models import NotificationLog

    return NotificationLog.objects.filter(
        status=NotificationLog.Status.SENDING,
        created_at__lt=timezone.now() - NotificationLog.STALE_CLAIM,
    ).update(status=NotificationLog.Status.PENDING)

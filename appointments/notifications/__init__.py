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
        "dashboard_url": dashboard,
        "site_url": _site_url(),
    }
    context.update(extra or {})
    return context


def render_message(event, appointment, audience, extra=None):
    context = message_context(event, appointment, audience, extra)
    subject = event.subject_for(audience).format(**context)
    body = render_to_string(event.body_template, context)
    return subject, body.strip() + "\n"


def notify(event_name, appointment, to=None, channels=None, **extra):
    """Emit `event_name` for `appointment`. Returns the queued rows.

    `to` overrides the event's declared audience with one literal address —
    the escape hatch for a genuine one-off. It is not the normal path: who
    hears an event is a property of the event (see events.py).
    """
    from appointments.models import NotificationLog

    event = event_registry.get(event_name)
    queued = []

    for channel_name in channels or event.channels:
        channel = channel_registry.get(channel_name)
        for audience in event.audience:
            address = to or channel.address_for(audience, appointment)
            if not address:
                # Nothing to send to — a provider with no phone number, say.
                # Recorded rather than dropped: "we never had a way to reach
                # them" is an answer, and silence is not.
                NotificationLog.objects.create(
                    appointment=appointment, event=event.name, recipient="",
                    channel=channel_name, status=NotificationLog.Status.SKIPPED,
                    error=f"No {channel_name} address for the {audience}.",
                )
                continue

            subject, body = render_message(event, appointment, audience, extra)
            queued.append(
                NotificationLog.objects.create(
                    appointment=appointment,
                    event=event.name,
                    recipient=address,
                    channel=channel_name,
                    status=NotificationLog.Status.PENDING,
                    subject=subject,
                    body=body,
                )
            )

    if delivery_mode() == "immediate":
        # After the surrounding transaction commits, so a booking that rolls
        # back cannot email somebody about an appointment that does not exist.
        for message in queued:
            transaction.on_commit(lambda m=message: deliver(m))

    return queued


def delivery_mode():
    return getattr(settings, "NOTIFICATIONS_DELIVERY", "queued")


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

"""Delivery channels behind one interface (design §9.1 point 2).

`EmailChannel` ships. `SmsChannel` is a stub that raises — and it is here on
purpose rather than as a comment saying "SMS goes here later". A seam nobody
has ever run is a guess about a seam. This one is exercised by a test that
registers SMS for an event and asserts the failure is recorded on the message
rather than crashing the caller, so the day an SMS provider is chosen the only
unknown is the provider's API.

**Recipient addresses are the channel's business, not the event's.** An event
says "tell the patient"; email reads `Appointment.email` and SMS reads
`Appointment.phone`. Both columns are already collected, which is why decision
3 could say "email now, architected for SMS" and mean it — SMS needs an
integration, not a new data-capture form.
"""

from django.conf import settings
from django.core.mail import send_mail

from .events import PATIENT, PROVIDER


class Channel:
    name = ""

    def address_for(self, audience, appointment):
        """Where this channel reaches `audience`, or None if it cannot."""
        raise NotImplementedError

    def send(self, message):
        """Deliver a NotificationLog row. Raise to mark it failed."""
        raise NotImplementedError


class EmailChannel(Channel):
    name = "email"

    def address_for(self, audience, appointment):
        if audience == PATIENT:
            return appointment.email
        if audience == PROVIDER:
            return appointment.slot.user.email
        return None

    def send(self, message):
        send_mail(
            subject=message.subject,
            message=message.body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[message.recipient],
            # Deliberately loud. The queue is what makes a failure survivable:
            # the row stays PENDING with the error recorded and is retried,
            # which is only possible if the exception reaches the drain.
            fail_silently=False,
        )


class SmsChannel(Channel):
    """Not implemented. Registered so the seam is real (decision 3).

    What is missing is an account with a provider and a sender ID, not code:
    see the notification services table in the implementation plan (Phase 3).
    Until then, no template promises an SMS — a message telling a patient to
    watch for a text that will never arrive is worse than sending nothing.
    """

    name = "sms"

    def address_for(self, audience, appointment):
        if audience == PATIENT:
            return appointment.phone
        if audience == PROVIDER:
            return appointment.slot.user.phone
        return None

    def send(self, message):
        raise NotImplementedError(
            "SmsChannel has no provider integration yet. Choose a provider, "
            "add its credentials, and implement send() here; nothing else in "
            "the notification layer needs to change."
        )


REGISTRY = {channel.name: channel for channel in (EmailChannel(), SmsChannel())}


def get(name):
    try:
        return REGISTRY[name]
    except KeyError:
        raise LookupError(f"Unknown notification channel {name!r}") from None

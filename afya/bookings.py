"""What a provider does with a booking, and what she is told each time."""

from django.utils import timezone

from afya import outbound, payments
from afya.messages import say
from afya.models import Booking


class BookingError(ValueError):
    pass


ALLOWED = {
    Booking.Status.REQUESTED: {Booking.Status.CONFIRMED, Booking.Status.CANCELLED},
    Booking.Status.CONFIRMED: {Booking.Status.ATTENDED, Booking.Status.NO_SHOW,
                               Booking.Status.CANCELLED},
}


def _move(booking, status):
    if status not in ALLOWED.get(booking.status, set()):
        raise BookingError(f"{booking.reference}: cannot go from {booking.status} to {status}.")
    booking.status = status
    booking.save(update_fields=["status", "visit_at", "updated_at"])


def _channel(booking):
    # Confirmations go back on the channel she booked on; USSD and web can't
    # receive pushed messages, so those fall back to SMS.
    return booking.channel if booking.channel == "whatsapp" else "sms"


def confirm(booking, visit_at):
    if visit_at is None:
        raise BookingError("A confirmed booking needs a visit time.")
    booking.visit_at = visit_at
    _move(booking, Booking.Status.CONFIRMED)
    client = booking.client
    if client:
        when = timezone.localtime(visit_at).strftime("%d/%m %H:%M")
        outbound.send(client, say("confirmed", client.language, ref=booking.reference,
                                  when=when, provider=booking.provider.name), _channel(booking))
    return booking


def attended(booking):
    """Mark the visit done and collect payment through the 5% capped split."""
    _move(booking, Booking.Status.ATTENDED)
    payment = payments.settle(booking)
    client = booking.client
    if client and payment.status == payment.Status.PENDING:
        outbound.send(client, say("pay_request", client.language, amount=f"{payment.amount_tsh:,}",
                                  provider=booking.provider.name, ref=booking.reference),
                      _channel(booking))
    return payment


def _release_voucher(booking):
    # The voucher was never redeemed, so give it back for another booking.
    if booking.voucher_id:
        booking.voucher = None
        booking.save(update_fields=["voucher", "updated_at"])


def no_show(booking):
    _move(booking, Booking.Status.NO_SHOW)
    _release_voucher(booking)
    return booking


def cancel(booking):
    _move(booking, Booking.Status.CANCELLED)
    _release_voucher(booking)
    if booking.client:
        outbound.send(booking.client, say("cancelled", booking.client.language,
                                          ref=booking.reference), _channel(booking))
    return booking

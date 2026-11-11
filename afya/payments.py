"""Payments: the capped 5% split, vouchers and the Afya Wallet.

Free for her. When a visit is paid by mobile money, the payment is split
automatically: the provider receives the price minus a commission of 5%,
capped at TSh 2,000 per visit. The commission is 0% for public facilities,
for sponsored-voucher visits, and during a provider's optional
commission-free period. Clinics may not add the fee to the patient's price.

`MobileMoneyGateway` is the boundary to M-Pesa, Airtel Money, Mixx by Yas or
an aggregator. `SandboxGateway` is used until a real one is configured, so the
whole flow can be tested end to end without moving money.
"""

import hashlib
import hmac
import logging
import secrets
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from afya.models import Booking, Payment, Voucher, WalletEntry

log = logging.getLogger("afya")


def commission_rate():
    return Decimal(str(getattr(settings, "AFYA_COMMISSION_RATE", "0.05")))


def commission_cap():
    return int(getattr(settings, "AFYA_COMMISSION_CAP_TSH", 2000))


def commission_for(provider, amount_tsh, method, on_date=None):
    """AGKI's commission in whole shillings for one paid visit."""
    on_date = on_date or timezone.localdate()
    if amount_tsh <= 0 or provider.is_public:
        return 0
    if method in (Booking.PayWith.VOUCHER, Booking.PayWith.FREE):
        return 0
    if provider.commission_free_until and on_date <= provider.commission_free_until:
        return 0
    raw = (Decimal(amount_tsh) * commission_rate()).quantize(Decimal("1"), ROUND_HALF_UP)
    return min(int(raw), commission_cap())


def split(provider, amount_tsh, method, on_date=None):
    commission = commission_for(provider, amount_tsh, method, on_date)
    return commission, amount_tsh - commission


def phone_hash(phone):
    """A one-way fingerprint of a number, for misuse checks after erasure."""
    salt = getattr(settings, "SECRET_KEY", "")
    return hashlib.sha256(f"{salt}:{phone}".encode()).hexdigest()


# --------------------------------------------------------------------------
# Gateways
# --------------------------------------------------------------------------


class MobileMoneyGateway:
    """Ask a phone to approve a payment (a USSD push / STK push)."""

    def request_payment(self, phone, amount_tsh, reference, description):
        """Return the gateway's transaction id. Result arrives by callback."""
        raise NotImplementedError

    def pay_out(self, phone, amount_tsh, reference):
        """Send a provider their share."""
        raise NotImplementedError


class SandboxGateway(MobileMoneyGateway):
    """Records requests instead of moving money. Used in tests and before a real
    gateway is configured; `manage.py check --deploy` warns if it is live."""

    requests = []
    payouts = []

    def request_payment(self, phone, amount_tsh, reference, description):
        transaction_id = f"SBX-{secrets.token_hex(6)}"
        self.requests.append((phone, amount_tsh, reference, transaction_id))
        return transaction_id

    def pay_out(self, phone, amount_tsh, reference):
        self.payouts.append((phone, amount_tsh, reference))
        return f"SBX-OUT-{secrets.token_hex(4)}"


def gateway():
    from django.utils.module_loading import import_string

    path = getattr(settings, "AFYA_PAYMENT_GATEWAY", "afya.payments.SandboxGateway")
    return import_string(path)()


# --------------------------------------------------------------------------
# Vouchers and wallet
# --------------------------------------------------------------------------


def claim_voucher(client, code):
    """Attach a voucher to a client. Returns the voucher or None."""
    code = code.strip().upper()
    with transaction.atomic():
        voucher = Voucher.objects.select_for_update().filter(code=code).first()
        if not voucher or not voucher.is_usable():
            return None
        if voucher.claimed_by_id and voucher.claimed_by_id != client.pk:
            return None
        voucher.claimed_by = client
        voucher.claimed_phone_hash = phone_hash(client.phone)
        voucher.save(update_fields=["claimed_by", "claimed_phone_hash"])
        return voucher


def usable_voucher_for(client, service):
    # A voucher already held by an open booking is spoken for.
    free = client.vouchers.filter(redeemed_at__isnull=True, booking__isnull=True)
    for voucher in free.order_by("expires_on"):
        if voucher.is_usable() and voucher.covers(service, service.provider):
            return voucher
    return None


def deposit(client, amount_tsh, reference, kind=WalletEntry.Kind.DEPOSIT):
    return WalletEntry.objects.create(
        client=client, amount_tsh=abs(amount_tsh), kind=kind, reference=reference
    )


def _deposit_signature(client_id, amount_tsh, nonce):
    key = getattr(settings, "SECRET_KEY", "").encode()
    message = f"wallet:{client_id}:{amount_tsh}:{nonce}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:12]


def request_deposit(client, amount_tsh):
    """Ask her phone to approve saving `amount_tsh` into the Afya Wallet.

    The reference carries who and how much, signed with the server's secret,
    so a forged callback can't invent a deposit for any client or amount.
    """
    nonce = secrets.token_hex(4)
    signature = _deposit_signature(client.pk, amount_tsh, nonce)
    reference = f"W-{client.pk}-{amount_tsh}-{nonce}-{signature}"
    try:
        gateway().request_payment(client.phone, amount_tsh, reference, "Afya Wallet")
    except Exception:  # noqa: BLE001 - any gateway failure is reported the same way
        log.exception("Wallet deposit request failed for client %s", client.pk)
        return None
    return reference


def confirm_deposit(reference, succeeded):
    """Gateway callback for a wallet deposit. Idempotent. Returns the entry or None."""
    from afya.models import Client

    if not succeeded or not reference.startswith("W-"):
        return None
    try:
        _, client_id, amount, nonce, signature = reference.split("-")
        client_id, amount = int(client_id), int(amount)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _deposit_signature(client_id, amount, nonce)):
        log.warning("Rejected wallet callback with a bad signature")
        return None
    with transaction.atomic():
        client = Client.objects.select_for_update().filter(pk=client_id).first()
        if client is None:
            return None
        existing = WalletEntry.objects.filter(reference=reference).first()
        if existing:
            return existing
        return deposit(client, amount, reference)


def choose_payment(client, service):
    """Decide how a new booking will be paid: free, voucher, wallet or mobile money."""
    if service.price_tsh == 0:
        return Booking.PayWith.FREE, None
    voucher = usable_voucher_for(client, service)
    if voucher and voucher.value_tsh >= service.price_tsh:
        return Booking.PayWith.VOUCHER, voucher
    if client.wallet_balance >= service.price_tsh:
        return Booking.PayWith.WALLET, None
    return Booking.PayWith.MOBILE_MONEY, None


# --------------------------------------------------------------------------
# Settling a visit
# --------------------------------------------------------------------------


def _still_payable_by(booking, method):
    """Re-check the way chosen at booking time, now that the visit happened.

    The wallet may have been spent on another visit since, and a voucher may
    have been redeemed elsewhere. Falling back to mobile money is safe: she
    approves it on her phone, so nothing is taken without her say.
    """
    from afya.models import Client

    if method == Booking.PayWith.WALLET:
        if not booking.client_id:
            return Booking.PayWith.MOBILE_MONEY
        # Lock her row so two visits settling at once can't both spend the same balance.
        client = Client.objects.select_for_update().get(pk=booking.client_id)
        if client.wallet_balance < booking.price_tsh:
            return Booking.PayWith.MOBILE_MONEY
    if method == Booking.PayWith.VOUCHER:
        voucher = Voucher.objects.select_for_update().filter(pk=booking.voucher_id).first()
        if voucher is None or voucher.redeemed_at:
            return Booking.PayWith.MOBILE_MONEY
    return method


def settle(booking):
    """Create the payment for an attended visit and start collecting it.

    Free, voucher and wallet visits settle immediately. Mobile money sends a
    payment request to her phone; `confirm_payment` finishes it when the
    gateway calls back. The provider's share is paid out once the payment is
    in, outside any database transaction so a slow gateway can't hold locks.
    """
    existing = Payment.objects.filter(booking=booking).first()
    if existing:
        return existing

    amount = booking.price_tsh
    phone = booking.client.phone if booking.client else ""

    with transaction.atomic():
        method = _still_payable_by(booking, booking.pay_with)
        if method != booking.pay_with:
            booking.pay_with = method
            booking.save(update_fields=["pay_with", "updated_at"])
        commission, share = split(booking.provider, amount, method)
        payment = Payment.objects.create(
            booking=booking, method=method, amount_tsh=amount,
            commission_tsh=commission, provider_share_tsh=share,
            payer_phone="" if method == Booking.PayWith.VOUCHER else phone[:15],
        )
        if method == Booking.PayWith.FREE:
            _mark_paid(payment, "free")
        elif method == Booking.PayWith.VOUCHER:
            voucher = booking.voucher
            voucher.redeemed_at = timezone.now()
            voucher.save(update_fields=["redeemed_at"])
            _mark_paid(payment, f"voucher:{voucher.code}")
        elif method == Booking.PayWith.WALLET:
            WalletEntry.objects.create(
                client=booking.client, amount_tsh=-amount,
                kind=WalletEntry.Kind.VISIT, reference=booking.reference,
            )
            _mark_paid(payment, f"wallet:{booking.reference}")

    if payment.status == Payment.Status.PENDING and phone and not phone.startswith("web:"):
        try:
            payment.gateway_reference = gateway().request_payment(
                phone, amount, booking.reference, f"Afya Mfukoni {booking.reference}"
            ) or ""
            payment.save(update_fields=["gateway_reference"])
        except Exception:  # noqa: BLE001 - staff retry from the admin
            log.exception("Payment request failed for %s", booking.reference)
    pay_out(payment)
    return payment


def _mark_paid(payment, reference):
    payment.status = Payment.Status.PAID
    payment.paid_at = timezone.now()
    if not payment.gateway_reference:
        payment.gateway_reference = reference
    payment.save(update_fields=["status", "paid_at", "gateway_reference"])


def pay_out(payment):
    """Send the provider their share, once. Safe to call again to retry.

    Mobile money, wallet and voucher visits all leave money with AGKI, so the
    clinic is paid its share for each. Returns the payout reference or None.
    """
    if not payment.payout_due:
        return None
    provider = payment.booking.provider
    if not provider.mobile_money_number:
        log.warning("Provider %s has no payout number; %s still owed",
                    provider.pk, payment.booking.reference)
        return None
    # Claim the payout first, so two workers can't both send it.
    claimed = Payment.objects.filter(pk=payment.pk, paid_out_at__isnull=True,
                                     payout_reference="").update(payout_reference="sending")
    if not claimed:
        return None
    try:
        reference = gateway().pay_out(provider.mobile_money_number,
                                      payment.provider_share_tsh, payment.booking.reference)
    except Exception:  # noqa: BLE001
        log.exception("Payout failed for %s", payment.booking.reference)
        Payment.objects.filter(pk=payment.pk).update(payout_reference="")
        payment.payout_reference = ""
        return None
    payment.payout_reference = (reference or "sent")[:80]
    payment.paid_out_at = timezone.now()
    payment.save(update_fields=["payout_reference", "paid_out_at"])
    return payment.payout_reference


def confirm_payment(gateway_reference, succeeded):
    """Called from the gateway callback. Idempotent."""
    if not gateway_reference:
        return None
    with transaction.atomic():
        payment = Payment.objects.select_for_update().select_related(
            "booking__provider").filter(gateway_reference=gateway_reference).first()
        if payment is None or payment.status != Payment.Status.PENDING:
            return payment
        if succeeded:
            _mark_paid(payment, gateway_reference)
        else:
            payment.status = Payment.Status.FAILED
            payment.save(update_fields=["status"])
    pay_out(payment)
    return payment

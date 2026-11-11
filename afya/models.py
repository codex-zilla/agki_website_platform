"""Afya Mfukoni data model.

Two kinds of record live here, kept apart on purpose:

* **Operational** records (`Client`, `Booking`, `Payment`, ...) are what the
  service needs to work: who to text, which clinic to send her to. They hold
  the minimum needed and can be erased on request (`Client.erase`).
* **Anonymised** records (`NeedEvent`) are what health planners see. They have
  no link to a person - no phone, no client id - only district, need, channel
  and outcome. Deleting a client never needs to touch them, and reading them
  can never identify anyone.
"""

import secrets
import string

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import Sum
from django.utils import timezone


class Need(models.TextChoices):
    """What a girl or woman is looking for. The AI maps free text onto these."""

    ANTENATAL = "antenatal", "Antenatal care"
    PREGNANCY_TEST = "pregnancy_test", "Pregnancy test"
    FAMILY_PLANNING = "family_planning", "Family planning"
    STI = "sti", "STI testing and treatment"
    HIV = "hiv", "HIV testing, PrEP and PEP"
    MENSTRUAL = "menstrual", "Menstrual health"
    POSTNATAL = "postnatal", "Postnatal and newborn care"
    CERVICAL = "cervical", "Cervical cancer screening"
    LAB_TEST = "lab_test", "Laboratory tests"
    MENTAL_HEALTH = "mental_health", "Mental health support"
    GENERAL = "general", "General consultation"


class Channel(models.TextChoices):
    SMS = "sms", "SMS"
    USSD = "ussd", "USSD"
    WHATSAPP = "whatsapp", "WhatsApp"
    WEB = "web", "Web"


class Language(models.TextChoices):
    SWAHILI = "sw", "Kiswahili"
    ENGLISH = "en", "English"


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class Provider(models.Model):
    """A clinic, facility or individual licensed provider women can be sent to.

    Only `VERIFIED` providers are ever matched. Verification means AGKI staff
    checked the licence against the relevant professional council register;
    `verified_by` and `verified_at` record who did it and when.
    """

    class Kind(models.TextChoices):
        CLINIC = "clinic", "Private clinic / polyclinic"
        PUBLIC = "public", "Public facility (dispensary, health centre, hospital)"
        LAB = "lab", "Diagnostic laboratory"
        INDIVIDUAL = "individual", "Individual licensed provider"

    class Council(models.TextChoices):
        MCT = "mct", "Medical Council of Tanganyika"
        TNMC = "tnmc", "Tanzania Nursing and Midwifery Council"
        PC = "pc", "Pharmacy Council"
        HLPC = "hlpc", "Health Laboratory Practitioners Council"
        MOH = "moh", "Ministry of Health facility registry"
        OTHER = "other", "Other"

    class Verification(models.TextChoices):
        PENDING = "pending", "Pending verification"
        VERIFIED = "verified", "Verified"
        SUSPENDED = "suspended", "Suspended"

    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # An optional link to a doctor account on the existing AGKI platform.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="afya_provider",
    )

    licence_number = models.CharField(max_length=60)
    council = models.CharField(max_length=10, choices=Council.choices)
    verification = models.CharField(
        max_length=12, choices=Verification.choices, default=Verification.PENDING
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )

    region = models.CharField(max_length=60)
    district = models.CharField(max_length=60)
    address = models.CharField(max_length=200, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    phone = models.CharField(max_length=15)
    mobile_money_number = models.CharField(
        max_length=15, blank=True,
        help_text="Where the provider's share of each payment is sent.",
    )
    opening_hours = models.CharField(
        max_length=80, blank=True, help_text="Short, for SMS, in the form Mon-Sat 8-18. Leave blank if unknown."
    )

    is_youth_friendly = models.BooleanField(
        default=False, help_text="Offers adolescent- and youth-friendly services."
    )
    handles_emergencies = models.BooleanField(
        default=False, help_text="Can receive obstetric and other emergencies."
    )
    commission_free_until = models.DateField(
        null=True, blank=True,
        help_text="Optional 0% commission period, such as the first 3 months of a pilot.",
    )

    rating_total = models.PositiveIntegerField(default=0)
    rating_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["verification", "region", "district"])]

    def __str__(self):
        return f"{self.name} ({self.district})"

    @property
    def is_public(self):
        return self.kind == self.Kind.PUBLIC

    @property
    def is_verified(self):
        return self.verification == self.Verification.VERIFIED

    def verify(self, by):
        self.verification = self.Verification.VERIFIED
        self.verified_at = timezone.now()
        self.verified_by = by
        self.save(update_fields=["verification", "verified_at", "verified_by", "updated_at"])

    def add_rating(self, score):
        self.rating_total = models.F("rating_total") + score
        self.rating_count = models.F("rating_count") + 1
        self.save(update_fields=["rating_total", "rating_count"])
        self.refresh_from_db(fields=["rating_total", "rating_count"])


class Service(models.Model):
    """One thing a provider offers, with its price, in both languages."""

    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="services")
    need = models.CharField(max_length=20, choices=Need.choices)
    name_en = models.CharField(max_length=80)
    name_sw = models.CharField(max_length=80)
    price_tsh = models.PositiveIntegerField(default=0, help_text="0 means free.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["provider", "need"]

    def __str__(self):
        return f"{self.name_en} at {self.provider.name}"

    def name(self, language):
        return self.name_sw if language == Language.SWAHILI else self.name_en


# --------------------------------------------------------------------------
# Clients and conversations
# --------------------------------------------------------------------------


class Client(models.Model):
    """A girl or woman using the service, known only by her phone number.

    No name, no birth date, no national ID. `age_band` is optional and only
    used to prefer youth-friendly providers.
    """

    class AgeBand(models.TextChoices):
        UNKNOWN = "", "Not given"
        ADOLESCENT = "10-19", "10-19"
        YOUNG_ADULT = "20-24", "20-24"
        ADULT = "25+", "25 and over"

    # A Tanzanian number in 255... form, or "web:<token>" for a web visitor
    # who has not given a number yet.
    phone = models.CharField(max_length=40, unique=True)
    language = models.CharField(max_length=2, choices=Language.choices, default=Language.SWAHILI)
    consented_at = models.DateTimeField(null=True, blank=True)
    reminders_opt_out = models.BooleanField(default=False)
    region = models.CharField(max_length=60, blank=True)
    district = models.CharField(max_length=60, blank=True)
    age_band = models.CharField(max_length=6, choices=AgeBand.choices, blank=True, default="")
    is_pregnant = models.BooleanField(default=False)
    pregnancy_noted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.masked_phone

    @property
    def has_consented(self):
        return self.consented_at is not None

    @property
    def is_web_visitor(self):
        return self.phone.startswith("web:")

    @property
    def masked_phone(self):
        """Enough to recognise a number, not enough to call it."""
        if self.is_web_visitor:
            return "web visitor"
        return f"{self.phone[:5]}****{self.phone[-2:]}"

    @property
    def wallet_balance(self):
        return self.wallet_entries.aggregate(total=Sum("amount_tsh"))["total"] or 0

    def erase(self):
        """Delete everything that identifies her, on request ("FUTA").

        Bookings already paid keep their financial rows (the clinic was paid
        and that must reconcile) but lose the link to her.
        """
        with transaction.atomic():
            Payment.objects.filter(booking__client=self).update(payer_phone="")
            Booking.objects.filter(client=self).update(client=None, note="")
            self.conversations.all().delete()
            self.delete()


class Conversation(models.Model):
    """Where a client is in a conversation, so replies like "2" make sense."""

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="conversations")
    channel = models.CharField(max_length=10, choices=Channel.choices)
    state = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("client", "channel")]


# --------------------------------------------------------------------------
# Bookings, payments, vouchers, wallet
# --------------------------------------------------------------------------


def new_reference(prefix):
    digits = "".join(secrets.choice(string.digits) for _ in range(6))
    return f"{prefix}{digits}"


class Booking(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested - awaiting provider"
        CONFIRMED = "confirmed", "Confirmed"
        ATTENDED = "attended", "Attended"
        NO_SHOW = "no_show", "Did not attend"
        CANCELLED = "cancelled", "Cancelled"

    class PayWith(models.TextChoices):
        MOBILE_MONEY = "mobile_money", "Mobile money"
        VOUCHER = "voucher", "Sponsored voucher"
        WALLET = "wallet", "Afya Wallet"
        FREE = "free", "Free service"

    reference = models.CharField(max_length=12, unique=True)
    # Nullable so a client can be erased without breaking clinic records.
    client = models.ForeignKey(
        Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="bookings"
    )
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, related_name="bookings")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="bookings")
    channel = models.CharField(max_length=10, choices=Channel.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REQUESTED)
    pay_with = models.CharField(max_length=14, choices=PayWith.choices, default=PayWith.MOBILE_MONEY)
    voucher = models.OneToOneField(
        "Voucher", null=True, blank=True, on_delete=models.SET_NULL, related_name="booking"
    )
    visit_at = models.DateTimeField(
        null=True, blank=True, help_text="Set by the provider when confirming."
    )
    note = models.CharField(max_length=200, blank=True)
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.reference

    @property
    def price_tsh(self):
        return self.service.price_tsh

    def save(self, *args, **kwargs):
        if self.reference:
            return super().save(*args, **kwargs)
        for _ in range(10):
            self.reference = new_reference("AF")
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if not Booking.objects.filter(reference=self.reference).exists():
                    raise
        raise IntegrityError("Could not find a free booking reference.")


class Payment(models.Model):
    """One payment for one visit, already split between clinic and AGKI."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    booking = models.OneToOneField(Booking, on_delete=models.PROTECT, related_name="payment")
    method = models.CharField(max_length=14, choices=Booking.PayWith.choices)
    amount_tsh = models.PositiveIntegerField()
    commission_tsh = models.PositiveIntegerField()
    provider_share_tsh = models.PositiveIntegerField()
    payer_phone = models.CharField(max_length=15, blank=True)
    gateway_reference = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    # The provider's share leaving AGKI. Blank while it is still owed, so an
    # unpaid clinic is visible in the admin and can be retried.
    payout_reference = models.CharField(max_length=80, blank=True)
    paid_out_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.booking.reference} {self.get_status_display()}"

    @property
    def payout_due(self):
        return (self.status == self.Status.PAID and self.provider_share_tsh > 0
                and not self.paid_out_at)


class Voucher(models.Model):
    """A partner-sponsored credit a girl redeems by SMS code."""

    code = models.CharField(max_length=12, unique=True, blank=True)
    sponsor = models.CharField(max_length=100)
    value_tsh = models.PositiveIntegerField()
    need = models.CharField(
        max_length=20, choices=Need.choices, blank=True,
        help_text="Leave blank to allow any service.",
    )
    region = models.CharField(max_length=60, blank=True, help_text="Blank means any region.")
    expires_on = models.DateField(null=True, blank=True)
    # Set when a client claims the code by SMS, before she books.
    claimed_by = models.ForeignKey(
        Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="vouchers"
    )
    claimed_phone_hash = models.CharField(
        max_length=64, blank=True,
        help_text="Kept after erasure so misuse checks still work without the number.",
    )
    redeemed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} ({self.sponsor})"

    @classmethod
    def new_code(cls):
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: easy to type on a keypad
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            if not cls.objects.filter(code=code).exists():
                return code

    @classmethod
    def issue(cls, sponsor, value_tsh, **fields):
        return cls.objects.create(code=cls.new_code(), sponsor=sponsor,
                                  value_tsh=value_tsh, **fields)

    def is_usable(self, today=None):
        today = today or timezone.localdate()
        if self.redeemed_at:
            return False
        return not (self.expires_on and self.expires_on < today)

    def covers(self, service, provider):
        if self.need and self.need != service.need:
            return False
        if self.region and self.region.lower() != provider.region.lower():
            return False
        return True


class WalletEntry(models.Model):
    """The Afya Wallet as an append-only ledger. Balance = sum of entries."""

    class Kind(models.TextChoices):
        DEPOSIT = "deposit", "Saving (deposit)"
        VISIT = "visit", "Paid for a visit"
        TOP_UP = "top_up", "Sponsor top-up"

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="wallet_entries")
    amount_tsh = models.IntegerField(help_text="Positive adds, negative spends.")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    reference = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "wallet entries"
        ordering = ["-created_at"]


# --------------------------------------------------------------------------
# Anonymised analytics and reminder bookkeeping
# --------------------------------------------------------------------------


class NeedEvent(models.Model):
    """One anonymous record each time someone asks for care.

    Deliberately has no foreign key to `Client` and no phone number.
    """

    created_on = models.DateField(default=timezone.localdate)
    region = models.CharField(max_length=60, blank=True)
    district = models.CharField(max_length=60, blank=True)
    need = models.CharField(max_length=20, choices=Need.choices, blank=True)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    language = models.CharField(max_length=2, choices=Language.choices)
    matched = models.BooleanField(default=False, help_text="At least one verified provider found.")
    booked = models.BooleanField(default=False)
    danger_sign = models.CharField(max_length=30, blank=True)

    class Meta:
        indexes = [models.Index(fields=["created_on", "region", "district"])]


class ReminderLog(models.Model):
    class Kind(models.TextChoices):
        VISIT = "visit", "Visit tomorrow"
        ANTENATAL = "antenatal", "Antenatal care nudge"
        RATING = "rating", "Rate your visit"

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="reminders")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    booking = models.ForeignKey(Booking, null=True, blank=True, on_delete=models.CASCADE)
    sent_at = models.DateTimeField(default=timezone.now)

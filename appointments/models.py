import random
import string

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from cms.models import SingletonModel
from useraccount.models import CustomUser


class AvailableSlot(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    date = models.DateField()
    time = models.TimeField()
    is_booked = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)  # ← auto updated field
    created_at = models.DateTimeField(auto_now_add=True)  # ← auto created field

    class Meta:
        # Nothing stopped a doctor publishing the same slot twice, and the
        # database had duplicates to prove it (design §10 dashboard #7). Two
        # identical slots mean two bookable rows for one real appointment.
        unique_together = [("user", "date", "time")]

    def __str__(self):
        status = "Booked" if self.is_booked else "Available"
        return f"{self.date} at {self.time} - {status}"


class Appointment(models.Model):
    """A booking, and the two-round negotiation it goes through.

    AGKI's real flow is: the doctor publishes availability, the patient books
    a time, **the doctor confirms the time**, the patient then chooses how to
    meet, and **the doctor confirms the mode** (design §0.1). That is two
    provider gates with a patient decision between them, which is why `status`
    below replaces `AppointmentApproval`'s single `is_approved` boolean — one
    bit cannot express two independent approvals, let alone cancelled,
    completed, no-show or expired.
    """

    class Status(models.TextChoices):
        # --- round 1: the provider confirms the time ---
        PENDING_TIME = "pending_time", "Awaiting provider's confirmation of the time"
        TIME_DECLINED = "time_declined", "Time declined by provider"
        # --- round 2: the patient picks a mode, the provider confirms it ---
        PENDING_MODE = "pending_mode", "Time confirmed - awaiting patient's choice of mode"
        MODE_PROPOSED = "mode_proposed", "Awaiting provider's confirmation of the mode"
        # --- settled ---
        CONFIRMED = "confirmed", "Confirmed"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No-show"
        # --- nobody responded in time (design §6.1) ---
        EXPIRED = "expired", "Expired - not confirmed in time"

    class Mode(models.TextChoices):
        ONLINE = "online", "Online video call"
        PROVIDER_TRAVELS = "provider_travels", "Provider travels to the patient"
        PATIENT_TRAVELS = "patient_travels", "Patient travels to the provider"

    class VisitType(models.TextChoices):
        NEW_PATIENT = "new_patient", "New patient"
        FOLLOW_UP = "follow_up", "Follow-up"

    class ReasonForVisit(models.TextChoices):
        LAB_TEST = "lab_test", "Lab test / panel"
        REFERRED_TEST = "referred_test", "Doctor-referred test"
        RESULT_FOLLOWUP = "result_followup", "Result collection / follow-up"
        CONSULTATION = "consultation", "Consultation"
        OTHER = "other", "Other"
        # Deliberately absent: "home sample collection". It is *how* the
        # appointment happens, not *why* the patient booked — so it is the
        # PROVIDER_TRAVELS mode above (design §5.1).

    class Gender(models.TextChoices):
        MALE = "male", "Male"
        FEMALE = "female", "Female"
        OTHER = "other", "Other"
        PREFER_NOT_TO_SAY = "prefer_not_to_say", "Prefer not to say"

    fullName = models.CharField(max_length=100)
    birthDate = models.DateField()
    # The three fields below were bare CharFields. `CustomUser.role` was too,
    # and production accumulated doctors with role='4491' who were silently
    # excluded from the booking page — a closed set has to exist on the model,
    # not only as <option> tags a POST can bypass (design §5.1).
    gender = models.CharField(max_length=20, choices=Gender.choices)
    email = models.EmailField()
    phone = models.CharField(max_length=15)
    visitType = models.CharField(max_length=20, choices=VisitType.choices)
    reasonVisit = models.CharField(max_length=30, choices=ReasonForVisit.choices)
    # Matches the UI, which never marked this required while the model did.
    symptoms = models.TextField(blank=True)
    # OneToOne, not ForeignKey: two Appointments could point at one slot, and
    # did — `booking_form` never read `is_booked`, so two *sequential* bookings
    # both succeeded (design §10 #6). PROTECT because CASCADE meant deleting an
    # availability slot silently deleted the patient's appointment with it.
    slot = models.OneToOneField(AvailableSlot, on_delete=models.PROTECT)
    bookingID = models.CharField(max_length=20, unique=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING_TIME
    )
    # Not collected at booking. The patient chooses it in round 2, once the
    # provider has confirmed the time, so it is blank until then — mandatory
    # before CONFIRMED, but enforced by the flow rather than by the column.
    appointmentMode = models.CharField(max_length=20, choices=Mode.choices, blank=True)

    # Online mode only, supplied by the doctor when confirming the mode
    # (decision B). An "online appointment" with no link is worse than an
    # unconfirmed one, because both sides think it is settled.
    meeting_url = models.URLField(blank=True)
    time_confirmed_at = models.DateTimeField(null=True, blank=True)
    mode_confirmed_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # `age` was dropped in migration 0021: never collected, always null.

    def save(self, *args, **kwargs):
        is_new = self.pk is None  # check if appointment is being created

        # Generate booking ID if not already set
        if not self.bookingID:
            current_year = timezone.now().year
            random_digits = ''.join(random.choices(string.digits, k=5))
            self.bookingID = f"AGKI-{current_year}-{random_digits}"

        super().save(*args, **kwargs)

        # If new appointment created, mark slot as booked
        if is_new:
            self.slot.is_booked = True
            self.slot.save(update_fields=['is_booked'])

    def __str__(self):
        return f"Appointment for {self.fullName} on {self.created_at}"


class AppointmentTransition(models.Model):
    """Append-only audit trail: who moved this appointment to which state, when.

    Health-adjacent data with a multi-party approval flow needs to answer "who
    confirmed this and when" months later. `Appointment.status` holds the
    current state; this holds the history.

    `actor=None` means nobody with an account did it — either the patient, or
    the system expiring a booking nobody answered. Which of the two is in
    `note`, and that distinction matters: expiry can be waiting on the doctor
    *or* on the patient, and an audit that blames a doctor for a patient's
    silence is worse than no audit (design §6.1).
    """

    appointment = models.ForeignKey(
        Appointment, on_delete=models.CASCADE, related_name="transitions"
    )
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    actor = models.ForeignKey(
        CustomUser, null=True, blank=True, on_delete=models.SET_NULL
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.appointment.bookingID}: {self.from_status or '-'} -> {self.to_status}"


class AppointmentApproval(models.Model):
    """**Superseded by `Appointment.status`. Do not write to this.**

    Kept read-only for one release after the backfill in migration 0018, as
    insurance against an unrecoverable mistake in it; step 2.7 drops the table
    once the new flow has run in production for a while. Its single
    `is_approved` bit cannot express a flow with two independent provider
    gates, which is why it is going.
    """

    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE)
    is_approved = models.BooleanField(default=False)
    approved_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    approved_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        status = "Approved" if self.is_approved else "Pending"
        return f"Appointment {self.appointment.bookingID} - {status}"


class AppointmentNote(models.Model):
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name='notes')
    note_text = models.TextField()
    added_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Note for {self.appointment.fullName} at {self.created_at}"


class Rating(models.Model):
    """Reshaped now, built later (decision 7).

    Nothing in the codebase has ever created a `Rating`, so every average the
    old templates computed was permanently "No ratings yet". The read sites are
    gone; the table stays, in the shape the feature would need, so that adding
    it later is one form, one view and one notification handler rather than a
    migration of live appointment data.

    The `appointment` OneToOne is the whole trick: it makes "one rating per
    completed visit" a database rule, and gives a future feedback form a
    naturally authenticated entry point — the patient reaches it through the
    same signed session marker that already protects their appointment.
    """

    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE)
    provider = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    rating_value = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    feedback = models.TextField(blank=True)
    recommended = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Rating for {self.provider.username}: {self.rating_value}"


class NotificationLog(models.Model):
    """One row per attempted send (design §9.1).

    "Did the patient ever get told?" is an operational question that gets asked
    about real appointments, and "check the mail server logs" is not an answer
    anyone at AGKI can act on. Also makes a retry possible without guessing
    what was already sent.
    """

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        SMS = "sms", "SMS"

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    appointment = models.ForeignKey(
        Appointment, on_delete=models.CASCADE, related_name="notifications",
        null=True, blank=True,
    )
    event = models.CharField(max_length=50)
    recipient = models.CharField(max_length=255)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    status = models.CharField(max_length=10, choices=Status.choices)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event} -> {self.recipient} ({self.status})"


class AppointmentSettings(SingletonModel):
    """Superuser-only configuration for the booking flow (design §4.3).

    Deliberately separate from `cms.SiteSettings`, which the Content Editors
    group may edit. The boundary is **identity vs. configuration**: a phone
    number in the footer is shared company identity, whereas how long a doctor
    has to confirm an appointment is clinical configuration, and the CMS group
    must not reach it.

    Stored in minutes rather than as `DurationField`s because the people who
    set these read "120", not "2:00:00", and the admin widget for a duration is
    a well-known source of silently wrong values.
    """

    minimum_booking_notice_minutes = models.PositiveIntegerField(
        default=120,
        help_text=(
            "A slot cannot be booked once it is closer than this. Must be "
            "greater than the confirmation cut-off. AGKI: 2 hours."
        ),
    )
    confirmation_cutoff_minutes = models.PositiveIntegerField(
        default=30,
        help_text=(
            "An appointment still unanswered this close to its slot expires "
            "and the slot is released. AGKI: 30 minutes."
        ),
    )
    cancellation_cutoff_minutes = models.PositiveIntegerField(
        default=120,
        help_text=(
            "A patient may cancel up to this long before the slot. Decision 9 "
            "left the value open - confirm it with AGKI."
        ),
    )

    class Meta:
        verbose_name = "Appointment settings"
        verbose_name_plural = "Appointment settings"

    def clean(self):
        """Booking notice must exceed the confirmation cut-off.

        Set the notice to 15 minutes while the cut-off stays at 30 and every
        booking is created *already past its own expiry deadline*: the site
        accepts bookings and immediately expires them, which reads as "the
        site is broken" rather than "the setting is wrong" (design §6.1).
        """
        if self.minimum_booking_notice_minutes <= self.confirmation_cutoff_minutes:
            raise ValidationError(
                {
                    "minimum_booking_notice_minutes": (
                        "Minimum booking notice must be greater than the "
                        "confirmation cut-off, otherwise every booking would "
                        "expire the moment it is made."
                    )
                }
            )

    def save(self, *args, **kwargs):
        # A singleton edited in the admin goes through full_clean(), but a
        # `.save()` from code or a shell does not — and this invariant is one
        # nobody should be able to write past.
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def minimum_booking_notice(self):
        return timezone.timedelta(minutes=self.minimum_booking_notice_minutes)

    @property
    def confirmation_cutoff(self):
        return timezone.timedelta(minutes=self.confirmation_cutoff_minutes)

    @property
    def cancellation_cutoff(self):
        return timezone.timedelta(minutes=self.cancellation_cutoff_minutes)

    def __str__(self):
        return "Appointment settings"

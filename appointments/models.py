import secrets
import string
from datetime import datetime

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from cms.models import SingletonModel
from useraccount.models import CustomUser


class AvailableSlot(models.Model):
    """A time a doctor will work, and the ways they will work it.

    The three `offers_` flags arrived with the single-confirmation flow (AGKI,
    2026-09-03). Before it, a patient could ask for any of the three modes and
    the doctor accepted or rejected the choice in a second round; now the
    patient picks at booking, so the offer has to be true before it is made.
    A doctor who never travels should not be asked, and a patient should not
    pick something that was always going to be refused.

    Three booleans rather than a list in one column because SQLite has no array
    type, and a comma-separated string is a parser plus a validator plus a
    migration the day somebody writes to it by hand. Each flag is a column the
    database can index and the admin can show as a checkbox.
    """

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    date = models.DateField()
    time = models.TimeField()
    is_booked = models.BooleanField(default=False)

    #: Default `True` on all three, which is what every existing slot meant:
    #: before this change any mode could be asked for on any slot.
    offers_online = models.BooleanField(
        default=True, verbose_name="Online video call"
    )
    offers_provider_travels = models.BooleanField(
        default=True, verbose_name="I travel to the patient"
    )
    offers_patient_travels = models.BooleanField(
        default=True, verbose_name="The patient comes to me"
    )

    updated_at = models.DateTimeField(auto_now=True)  # ← auto updated field
    created_at = models.DateTimeField(auto_now_add=True)  # ← auto created field

    class Meta:
        # Nothing stopped a doctor publishing the same slot twice, and the
        # database had duplicates to prove it (design §10 dashboard #7). Two
        # identical slots mean two bookable rows for one real appointment.
        unique_together = [("user", "date", "time")]

    #: Which flag carries which mode. A dict rather than three `if`s so that
    #: adding a mode means adding one line here and one column, and every
    #: reader - the booking form, the console, the admin - follows.
    MODE_FLAGS = {
        "online": "offers_online",
        "provider_travels": "offers_provider_travels",
        "patient_travels": "offers_patient_travels",
    }

    @property
    def offered_modes(self):
        """The modes this slot may be booked for, as `(value, label)`.

        Ordered by `Appointment.Mode` rather than by this dict, so the patient
        always sees them in the same order as everywhere else.
        """
        return [
            (value, label)
            for value, label in Appointment.Mode.choices
            if getattr(self, self.MODE_FLAGS[value], False)
        ]

    def offers(self, mode):
        """Is `mode` on offer here? Unknown modes are not."""
        flag = self.MODE_FLAGS.get(mode)
        return bool(flag) and getattr(self, flag, False)

    @property
    def starts_at(self):
        """The slot's start as a single moment.

        `date` and `time` are stored separately, so every "is this slot still
        bookable / has it expired" question had to recombine them by hand.
        Doing it once here is what stops one caller comparing an aware
        datetime with a naive one and quietly getting the wrong answer.
        """
        moment = datetime.combine(self.date, self.time)
        if django_settings.USE_TZ:
            moment = timezone.make_aware(moment, timezone.get_current_timezone())
        return moment

    def is_bookable(self, settings=None, now=None):
        """Can a patient still book this slot?

        Not merely "is it free": a slot closer than `MINIMUM_BOOKING_NOTICE`
        is refused, because a booking made minutes before its own slot would
        be created already past its confirmation deadline (design §6.1).
        """
        settings = settings or AppointmentSettings.load()
        return not self.is_booked and self.starts_at >= settings.earliest_bookable_start(now)

    def __str__(self):
        status = "Booked" if self.is_booked else "Available"
        return f"{self.date} at {self.time} - {status}"


class InvalidTransition(Exception):
    """A move the state machine refuses (design §5).

    Deliberately not a `ValidationError`: this is not a user typing something
    wrong, it is code asking for a state change the flow does not have. Views
    catch it to render "this appointment has already been answered"; a bare
    escape is a 500, which is the right outcome for a genuine programming
    mistake.
    """


#: How many references to try before giving up. Each attempt is one INSERT,
#: and at any realistic number of bookings a second attempt is already
#: unlikely - this is a bound on a pathological case, not an expected cost.
BOOKING_REFERENCE_ATTEMPTS = 12


def new_booking_reference(when=None):
    """`AGKI-<year>-<five digits>`.

    `secrets` rather than `random`: a booking reference is half of what opens a
    patient's record (design §8.1), and `random` is a Mersenne Twister whose
    next output can be derived from enough previous ones. Nobody needs to
    predict the next reference AGKI will issue.
    """
    year = (when or timezone.now()).year
    digits = "".join(secrets.choice(string.digits) for _ in range(5))
    return f"AGKI-{year}-{digits}"


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

    #: The same three modes in the fewest words that still say which is which.
    #: A text message is charged by the character, and "Provider travels to the
    #: patient" is 31 of them - a third of a segment spent on a phrase written
    #: for a form label (plan 13.3). Neutral between the two readers on
    #: purpose: the doctor's "you travel" and the patient's "the doctor
    #: travels" would be two strings to keep in step for no gain.
    SHORT_MODES = {
        "online": "online call",
        "provider_travels": "doctor travels",
        "patient_travels": "at the lab",
    }

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

    # --- the state machine (design §5, plan 4.1) ----------------------------
    #
    # Everything below is data rather than a pile of `if` statements in views,
    # for one reason: "confirm" has to mean the same thing in the dashboard,
    # the admin, the expiry cron and any future API. A view that assigns
    # `status` directly is a view that can skip a gate, forget the audit row
    # or forget to tell the patient — and all three fail silently.

    #: Which moves are legal. A state absent from a set is refused, so the
    #: illegal ones (PENDING_TIME straight to CONFIRMED, skipping both gates)
    #: are refused by omission rather than by a check somebody has to remember.
    #: One confirmation, not two (AGKI, 2026-09-03). The patient now chooses
    #: the mode on the booking form - from the modes that slot actually offers
    #: - so the doctor sees the time and the mode together and answers once.
    #:
    #: `PENDING_MODE` and `MODE_PROPOSED` stay in `Status` because
    #: `AppointmentTransition` rows recorded them and history is append-only;
    #: they keep a way forward here so that any row still sitting in one - a
    #: migration that missed it, a fixture, a shell session - can be settled
    #: rather than stranded.
    TRANSITIONS = {
        Status.PENDING_TIME: {
            Status.CONFIRMED,         # doctor confirms the time and the mode
            Status.TIME_DECLINED,     # doctor declines it
            Status.CANCELLED,
            Status.EXPIRED,
        },
        Status.PENDING_MODE: {
            Status.CONFIRMED,
            Status.TIME_DECLINED,
            Status.CANCELLED,
            Status.EXPIRED,
        },
        Status.MODE_PROPOSED: {
            Status.CONFIRMED,
            Status.TIME_DECLINED,
            Status.CANCELLED,
            Status.EXPIRED,
        },
        Status.CONFIRMED: {
            Status.COMPLETED,
            Status.NO_SHOW,
            Status.CANCELLED,
        },
        # Terminal. TIME_DECLINED is an ending, not a pause: the slot is freed
        # and offered to somebody else, so there is nothing left to revive.
        Status.TIME_DECLINED: set(),
        Status.COMPLETED: set(),
        Status.CANCELLED: set(),
        Status.NO_SHOW: set(),
        Status.EXPIRED: set(),
    }

    #: States in which the slot is no longer spoken for. COMPLETED and NO_SHOW
    #: are absent on purpose — that time was used (or held) and re-offering it
    #: after the fact would be inventing availability that never existed.
    FREES_SLOT = {Status.TIME_DECLINED, Status.CANCELLED, Status.EXPIRED}

    #: Non-terminal states, i.e. the ones a patient may still cancel from.
    CANCELLABLE = {
        Status.PENDING_TIME, Status.PENDING_MODE,
        Status.MODE_PROPOSED, Status.CONFIRMED,
    }

    #: Events keyed by the *move*, not the destination, because arriving at
    #: PENDING_MODE means two different things: "your time is confirmed, choose
    #: how to meet" the first time, and "the doctor could not do that mode,
    #: please pick another" the second. One event for both would send the
    #: wrong message on the branch the patient is most likely to be confused by.
    TRANSITION_EVENTS = {
        (Status.PENDING_TIME, Status.CONFIRMED): "appointment.confirmed",
        (Status.PENDING_TIME, Status.TIME_DECLINED): "appointment.time_declined",
        # The legacy states, for a row that escaped migration 0027. They send
        # the same message as the live path rather than a special one: what the
        # patient needs to hear does not depend on which flow their booking
        # started under.
        (Status.PENDING_MODE, Status.CONFIRMED): "appointment.confirmed",
        (Status.PENDING_MODE, Status.TIME_DECLINED): "appointment.time_declined",
        (Status.MODE_PROPOSED, Status.CONFIRMED): "appointment.confirmed",
        (Status.MODE_PROPOSED, Status.TIME_DECLINED): "appointment.time_declined",
    }
    #: Events that depend only on where the appointment ended up. NO_SHOW is
    #: absent deliberately: there is nobody useful to tell. The patient did not
    #: turn up, and the doctor is the one who recorded it.
    STATUS_EVENTS = {
        Status.CANCELLED: "appointment.cancelled",
        Status.EXPIRED: "appointment.expired",
        Status.COMPLETED: "appointment.completed",
    }

    #: One confirmation stamps both columns: the time and the mode are settled
    #: in the same act now, and two columns that always hold the same value are
    #: better than one column that half the reporting still looks for.
    TRANSITION_TIMESTAMPS = {
        (Status.PENDING_TIME, Status.CONFIRMED): ("time_confirmed_at", "mode_confirmed_at"),
        (Status.PENDING_MODE, Status.CONFIRMED): ("time_confirmed_at", "mode_confirmed_at"),
        (Status.MODE_PROPOSED, Status.CONFIRMED): ("mode_confirmed_at",),
    }
    STATUS_TIMESTAMPS = {
        Status.CANCELLED: "cancelled_at",
        Status.EXPIRED: "expired_at",
    }

    #: What must be true before an appointment may be CONFIRMED, as messages
    #: rather than booleans, so the dashboard can show the doctor which one
    #: they have not satisfied.
    MISSING_MODE = "This appointment has no meeting mode yet, so there is nothing to confirm."
    MISSING_MEETING_URL = (
        "An online appointment needs a meeting link. Confirming without one "
        "leaves both sides thinking it is settled with no way to meet."
    )

    @property
    def short_mode(self):
        """The meeting mode in a handful of characters, for SMS."""
        return self.SHORT_MODES.get(self.appointmentMode, "")

    def confirmation_problem(self):
        """Why this appointment cannot be confirmed yet, or None.

        Decision B, and the reason it is here rather than only in the
        dashboard form: an "online appointment" with no link is worse than an
        unconfirmed one, because both sides think it is settled. A rule that
        only the form enforces is one the admin, a shell session and any
        future API walk straight past — the same argument that put
        `full_clean()` inside `AppointmentSettings.save()`.
        """
        if not self.appointmentMode:
            return self.MISSING_MODE
        if self.appointmentMode == self.Mode.ONLINE and not self.meeting_url:
            return self.MISSING_MEETING_URL
        return None

    def check_confirmable(self):
        problem = self.confirmation_problem()
        if problem:
            raise InvalidTransition(f"{self.bookingID}: {problem}")

    def transition_to(self, new_status, actor=None, note="", event=None, **event_extra):
        """Move to `new_status`, or refuse. The only supported way to do it.

        Validates the move, stamps the matching timestamp, frees the slot where
        that is what the new state means, writes the append-only audit row and
        emits the notification event — in one transaction, so a booking cannot
        end up half-moved or, worse, emailed about a state it never reached.

        `actor=None` means nobody with an account did it: the patient, or the
        expiry job. Say which in `note` — an audit that cannot tell a doctor's
        silence from a patient's is worse than no audit (design §6.1).

        Returns the `AppointmentTransition` it wrote.
        """
        # Imported here rather than at module level: `notifications` reads
        # `NotificationLog` from this module, so a top-level import would be
        # circular.
        from .notifications import notify

        now = timezone.now()
        with transaction.atomic():
            # Re-read the status under a lock instead of trusting the copy in
            # memory. Two doctors on the same appointment, or a doctor racing
            # the expiry cron, would otherwise both pass a check against a
            # value that stopped being true while the page was open.
            current = (
                type(self).objects.select_for_update()
                .filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )
            if current is None:
                raise InvalidTransition(
                    f"Appointment {self.bookingID} no longer exists."
                )
            self.status = current

            if new_status not in self.TRANSITIONS.get(current, set()):
                raise InvalidTransition(
                    f"{self.bookingID}: cannot move from {current!r} to "
                    f"{new_status!r}. Legal moves from {current!r}: "
                    f"{', '.join(sorted(self.TRANSITIONS.get(current, set()))) or 'none (terminal)'}."
                )

            if new_status == self.Status.CONFIRMED:
                self.check_confirmable()

            from_status = current
            self.status = new_status
            stamp = (
                self.TRANSITION_TIMESTAMPS.get((from_status, new_status))
                or self.STATUS_TIMESTAMPS.get(new_status)
            )
            if stamp:
                for field in (stamp,) if isinstance(stamp, str) else stamp:
                    setattr(self, field, now)
            self.save()

            if new_status in self.FREES_SLOT and self.slot.is_booked:
                self.slot.is_booked = False
                self.slot.save(update_fields=["is_booked"])

            record = AppointmentTransition.objects.create(
                appointment=self,
                from_status=from_status,
                to_status=new_status,
                actor=actor,
                note=note[:255],
            )

            # `event` overrides the map, for the one case where the same
            # move means two different things to the patient: a confirmation
            # the doctor changed the meeting mode on is still
            # PENDING_TIME -> CONFIRMED, but "confirmed" and "confirmed, though
            # not the way you asked" are not the same message (plan 12.1). The
            # map stays the default so no caller has to know an event name to
            # do the ordinary thing.
            event = event or (
                self.TRANSITION_EVENTS.get((from_status, new_status))
                or self.STATUS_EVENTS.get(new_status)
            )
            if event:
                notify(event, self, **event_extra)

        return record

    def can_be_cancelled(self, settings=None, now=None):
        """May the patient still cancel? Status *and* the cut-off (decision 9).

        Both halves matter: cancelling something already cancelled is
        meaningless, and cancelling twenty minutes before the slot leaves the
        doctor holding an hour nobody can rebook.
        """
        if self.status not in self.CANCELLABLE:
            return False
        settings = settings or AppointmentSettings.load()
        return (now or timezone.now()) <= self.slot.starts_at - settings.cancellation_cutoff

    def save(self, *args, **kwargs):
        is_new = self.pk is None  # check if appointment is being created

        if self.bookingID:
            super().save(*args, **kwargs)
        else:
            self._save_with_a_new_reference(*args, **kwargs)

        # If new appointment created, mark slot as booked
        if is_new:
            self.slot.is_booked = True
            self.slot.save(update_fields=['is_booked'])

    def _save_with_a_new_reference(self, *args, **kwargs):
        """Save under a reference nobody else holds.

        The reference used to be five digits generated once, with `unique=True`
        underneath and nothing in between: two patients drawing the same number
        meant an `IntegrityError` out of `save()`, which is a 500 on the
        booking form for the second of them. Five digits is 100,000 values, so
        by the birthday bound a **50% chance of at least one collision arrives
        at about 370 bookings in a year** - not a remote possibility, a
        scheduled one. It surfaced in `TransitionTests`, which creates enough
        appointments in one run to hit it.

        Retried rather than pre-checked alone, because a pre-check is a race:
        two requests can both find a number free and then both insert it. The
        `IntegrityError` is caught, the number is checked, and only a genuine
        clash is retried - `Appointment.slot`'s OneToOne raises the same
        exception class, and that one is the last guard against double-booking
        a slot (design §10 #6). Retrying it would turn a refusal into a loop.
        """
        for _ in range(BOOKING_REFERENCE_ATTEMPTS):
            self.bookingID = new_booking_reference()
            try:
                # Its own atomic block: without one, a failed INSERT marks the
                # surrounding transaction as broken and the retry cannot run.
                with transaction.atomic():
                    super().save(*args, **kwargs)
            except IntegrityError:
                if Appointment.objects.filter(bookingID=self.bookingID).exists():
                    continue
                raise
            return

        raise IntegrityError(
            "Could not find a free booking reference after %d attempts."
            % BOOKING_REFERENCE_ATTEMPTS
        )

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

    @property
    def to_status_label(self):
        """The human label for `to_status`.

        `to_status` is a plain CharField rather than one with `choices`, so
        that a row written years ago still reads back as whatever it said even
        if the state machine is later renamed — an audit trail that rewrites
        itself is not one. That costs the automatic `get_..._display`, hence
        this, and the fallback for a value no longer in the enum.
        """
        try:
            return Appointment.Status(self.to_status).label
        except ValueError:
            return self.to_status

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
    """One row per message: the outbox **and** the audit trail (design §9.1).

    "Did the patient ever get told?" is an operational question that gets asked
    about real appointments, and "check the mail server logs" is not an answer
    anyone at AGKI can act on. So the message itself is stored, not just the
    fact of it — `subject` and `body` are what was actually sent, which is a
    different and more useful question than what a template would render today.

    It doubles as the queue. A row starts `PENDING`; `send_pending_notifications`
    (cron, every minute) delivers it and moves it to `SENT` or `FAILED`. That
    indirection exists because the alternative is talking to an SMTP server
    inside the request that a patient is waiting on: a slow mail host makes
    booking feel broken, and a mail host that is down makes booking *be*
    broken, for something that is not part of booking at all.
    """

    MAX_ATTEMPTS = 3

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        SMS = "sms", "SMS"

    class Status(models.TextChoices):
        PENDING = "pending", "Queued"
        # Claimed by a delivery run and not yet resolved. Exists so two
        # overlapping cron runs cannot both send the same message: the claim
        # is an atomic PENDING -> SENDING transition, and only one caller can
        # win it. Counting attempts alone was not enough — a run starting
        # while another was mid-send would read the row, see it still pending,
        # and deliver it a second time.
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    #: A row left in SENDING for longer than this was almost certainly
    #: abandoned — Passenger starts and kills application processes as it
    #: pleases, so a run can die mid-send. Such rows go back to PENDING.
    STALE_CLAIM = timezone.timedelta(minutes=15)

    appointment = models.ForeignKey(
        Appointment, on_delete=models.CASCADE, related_name="notifications",
        null=True, blank=True,
    )
    event = models.CharField(max_length=50)
    #: Who this was for. Needed since plan 13.1 so that a message which
    #: fails on one channel can be re-rendered for the next one: the row
    #: knows the address it tried, and an address is not a person - two
    #: audiences can share one, and a phone number cannot be turned back
    #: into "the patient" without guessing.
    audience = models.CharField(max_length=20, blank=True)
    recipient = models.CharField(max_length=255)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    subject = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The drain's only query: pending work, oldest first.
            models.Index(fields=["status", "created_at"], name="notif_status_created_idx"),
        ]

    @property
    def is_deliverable(self):
        return self.status == self.Status.PENDING and self.attempts < self.MAX_ATTEMPTS

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
            "A patient may cancel up to this long before the slot. AGKI: "
            "2 hours, matching the booking notice - the window in which a "
            "booking can be made is the window in which it can be undone."
        ),
    )
    booking_horizon_days = models.PositiveIntegerField(
        default=30,
        help_text=(
            "How far ahead the public booking page offers slots. The old "
            "hard-coded 5 days meant a patient could not book past the day "
            "after tomorrow, however far ahead doctors published."
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

    def earliest_bookable_start(self, now=None):
        """The earliest slot start a patient may book right now (decision D).

        One place, so the page that *offers* slots and the view that *accepts*
        one cannot disagree — offering a slot the server will then refuse is
        the failure this exists to prevent (design §6.1).
        """
        return (now or timezone.now()) + self.minimum_booking_notice

    def __str__(self):
        return "Appointment settings"

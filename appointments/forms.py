from datetime import time, timedelta

from django import forms
from django.utils import timezone

from .models import (
    Appointment,
    AppointmentApproval,
    AppointmentNote,
    AppointmentSettings,
    AvailableSlot,
)

SLOT_TAKEN = "That time has just been taken. Please pick another slot."
SLOT_TOO_SOON = (
    "That time is too close now — appointments must be booked at least "
    "%(notice)s in advance so the doctor has a realistic chance to confirm. "
    "Please pick a later slot."
)


def humanise(delta):
    """"2 hours" / "90 minutes" — for a message a patient reads, not a log."""
    minutes = int(delta.total_seconds() // 60)
    if minutes % 60 == 0 and minutes >= 60:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minutes"


#: The three flags, offered as checkboxes wherever a doctor publishes times.
#: Named for the doctor reading them - "I travel to the patient", not
#: "provider_travels" - and sourced from the model so the two cannot drift.
MODE_FIELDS = tuple(AvailableSlot.MODE_FLAGS.values())

NO_MODES = (
    "Choose at least one way you can meet. A time nobody can be seen at is not "
    "availability."
)


def mode_flag_field(name):
    """One `offers_` checkbox, labelled from the model.

    `AvailableSlotForm` gets these from `AvailableSlot` through `Meta.fields`;
    `BulkAvailabilityForm` describes a *plan* rather than a row, so it has no
    model to take them from. Reading `verbose_name` here rather than retyping
    the labels keeps the two forms saying the same words.

    They are declared in the class body and not on the mixin below, which is
    where they started: Django's form metaclass collects declared fields from
    the class body and from bases that are themselves forms, so fields on a
    plain mixin are dropped - the bulk form rendered without them and nothing
    complained.
    """
    return forms.BooleanField(
        required=False,
        initial=True,
        label=AvailableSlot._meta.get_field(name).verbose_name,
    )


class ModeOfferMixin:
    """The rule shared by both forms that publish times: tick at least one.

    A mixin rather than a base class because one of the two is a `ModelForm`
    over `AvailableSlot` and the other is a plain `Form` describing a plan.
    """

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(field) for field in MODE_FIELDS):
            raise forms.ValidationError(NO_MODES, code="no_modes")
        return cleaned

    def offered(self):
        """The flags as a dict, ready to splat into `AvailableSlot(...)`."""
        return {field: bool(self.cleaned_data.get(field)) for field in MODE_FIELDS}


class AppointmentForm(forms.ModelForm):
    """The public booking form.

    Takes the slot it is booking, because the two rules that can refuse a
    booking are both about the slot rather than about anything the patient
    typed: it may have been taken since the page was rendered, and it may be
    inside the minimum booking notice (design §5.2, §6.1). Validating them
    here means the patient gets a message that says which; the view re-checks
    both under a row lock, because the slot id arrives in the URL and a POST
    can reach it without this form ever having been rendered.
    """

    class Meta:
        model = Appointment
        fields = [
                    'fullName','birthDate','gender','email','phone',
                    'visitType','reasonVisit','appointmentMode','symptoms'
                  ]

    def __init__(self, *args, slot=None, settings=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.slot = slot
        self.settings = settings or AppointmentSettings.load()

        # How they will meet is asked here now, not in a second round after the
        # doctor has confirmed the time (AGKI, 2026-09-03). The choices are the
        # ones **this slot** offers, so a patient is never shown an option that
        # was always going to be refused - and the same list is re-checked on
        # POST, because a select is a suggestion and this is the rule.
        mode = self.fields["appointmentMode"]
        mode.required = True
        mode.label = "How would you like to meet?"
        #
        # Setting `choices` **is** the enforcement, not just the display: a
        # `ChoiceField` validates membership, so a hand-made POST naming a mode
        # this slot does not offer is refused here. A `clean_appointmentMode`
        # repeating the rule stood below this line until a mutation check
        # showed it was dead - deleting it changed nothing, because the field
        # had already refused. Two copies of a rule is one copy nobody tests.
        #
        # `slot is None` yields an empty list, so a caller who forgets the slot
        # gets a form that accepts nothing rather than one that accepts
        # everything.
        mode.choices = slot.offered_modes if slot is not None else []

    def clean(self):
        cleaned = super().clean()
        if self.slot is None:
            return cleaned

        if self.slot.is_booked or Appointment.objects.filter(slot=self.slot).exists():
            raise forms.ValidationError(SLOT_TAKEN, code="slot_taken")

        if self.slot.starts_at < self.settings.earliest_bookable_start():
            raise forms.ValidationError(
                SLOT_TOO_SOON % {"notice": humanise(self.settings.minimum_booking_notice)},
                code="too_soon",
            )
        return cleaned


class AppointmentNoteForm(forms.ModelForm):
    class Meta:
        model = AppointmentNote
        fields = ['note_text',]


class AppointmentApprovalForm(forms.ModelForm):
    class Meta:
        model = AppointmentApproval
        fields = ['is_approved',]


class AvailableSlotForm(ModeOfferMixin, forms.ModelForm):
    """Publishing availability (design §5.2).

    Two rules, neither of which existed: a slot in the past could be published
    and then booked straight through the URL, and the same slot could be
    published twice — which it was, in the live database, until migration 0019
    de-duplicated it. The `unique_together` added there would now raise an
    `IntegrityError` from the view instead, which is a 500 rather than an
    answer, because `user` is not a form field and Django's own uniqueness
    check therefore never runs.
    """

    class Meta:
        model = AvailableSlot
        fields = ['date', 'time'] + list(MODE_FIELDS)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        cleaned = super().clean()
        date, at = cleaned.get("date"), cleaned.get("time")
        if not date or not at:
            return cleaned

        if AvailableSlot(date=date, time=at).starts_at < timezone.now():
            raise forms.ValidationError(
                "That time has already passed. Availability can only be "
                "published for the future.",
                code="in_the_past",
            )

        if self.user is not None and AvailableSlot.objects.filter(
            user=self.user, date=date, time=at
        ).exists():
            raise forms.ValidationError(
                "You have already published that time.", code="duplicate",
            )
        return cleaned
#: The most a single publish may create. Not a business rule - a blast radius.
#: The inputs multiply (days x times), so a slipped year in "until" turns a
#: reasonable-looking form into tens of thousands of rows. Refusing with a
#: number the doctor can see beats inserting them and asking someone to clean
#: it up.
MAX_BULK_SLOTS = 500

#: The times a doctor can pick, as `(value, label)` - 24-hour to the server,
#: AM/PM to the person.
#:
#: This replaces `<input type="time">` on both availability forms. That input
#: renders in the **browser's** locale, not the site's, so the same page asked
#: one doctor for `14:30` and another for `2:30 PM` with nothing on screen
#: saying which - and a doctor who typed `2:30` meaning the afternoon
#: published a slot at half past two in the morning. AGKI reported it as "the
#: user cannot know if the time has to be entered in 24 hour format".
#:
#: A closed list also removes a class of input that was never wanted: 08:07 is
#: a valid `TimeField` and not a valid appointment.
TIME_STEP_MINUTES = 15
TIME_CHOICES = [
    (f"{hour:02d}:{minute:02d}", time(hour, minute).strftime("%I:%M %p").lstrip("0"))
    for hour in range(6, 22)
    for minute in range(0, 60, TIME_STEP_MINUTES)
]

WEEKDAY_CHOICES = [
    ("0", "Mon"), ("1", "Tue"), ("2", "Wed"), ("3", "Thu"),
    ("4", "Fri"), ("5", "Sat"), ("6", "Sun"),
]


class BulkAvailabilityForm(ModeOfferMixin, forms.Form):
    offers_online = mode_flag_field("offers_online")
    offers_provider_travels = mode_flag_field("offers_provider_travels")
    offers_patient_travels = mode_flag_field("offers_patient_travels")

    """Publish the same times across a range of days (plan 11.1).

    **The honest version of a control this project deleted.** The old
    availability form had a repeat pattern - daily/weekly/monthly, a weekday
    picker, a "repeat until" date - and saved none of it: every field was
    collected and discarded (plan 5.x). It was removed rather than left as a
    convincing fake, and recorded as "a real gap, not a removed feature",
    because publishing 09:00-16:00 on weekdays for a month is 75 trips through
    a form that takes one slot at a time.

    This form is a **plan**, not a row. It produces the list of
    `(date, time)` pairs that should exist; `save_for` creates the ones that
    do not. That split is what lets the page show a count before anything is
    written.
    """

    start_date = forms.DateField()
    end_date = forms.DateField()
    weekdays = forms.MultipleChoiceField(choices=WEEKDAY_CHOICES)
    #: Comma-separated `HH:MM`. A text field rather than a formset because the
    #: page builds it from chips, and because it degrades to something a person
    #: can type - "09:00,09:30" works with JavaScript switched off.
    times = forms.CharField()

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_times(self):
        raw = self.cleaned_data["times"]
        parsed, seen = [], set()
        for chunk in raw.replace(" ", "").split(","):
            if not chunk:
                continue
            try:
                hour, _, minute = chunk.partition(":")
                at = time(int(hour), int(minute))
            except (ValueError, TypeError):
                raise forms.ValidationError(
                    f"'{chunk}' is not a time. Use 24-hour HH:MM, like 09:30.",
                    code="bad_time",
                )
            if at not in seen:
                seen.add(at)
                parsed.append(at)
        if not parsed:
            raise forms.ValidationError(
                "Add at least one time.", code="no_times",
            )
        return sorted(parsed)

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        times_ = cleaned.get("times")
        days = cleaned.get("weekdays")
        if not (start and end and times_ and days):
            return cleaned

        if end < start:
            raise forms.ValidationError(
                "The last day is before the first one.", code="reversed",
            )

        planned = self.planned_slots(cleaned)
        if not planned:
            # Every combination is in the past, or none of the chosen weekdays
            # falls in the range. Saying so beats reporting "0 published",
            # which reads like the form silently failed.
            raise forms.ValidationError(
                "That gives no future times. Check the dates and the days of "
                "the week.",
                code="empty_plan",
            )
        if len(planned) > MAX_BULK_SLOTS:
            raise forms.ValidationError(
                f"That would publish {len(planned)} times. The most in one go "
                f"is {MAX_BULK_SLOTS} - narrow the dates or the times.",
                code="too_many",
            )
        return cleaned

    def planned_slots(self, cleaned=None):
        """Every `(date, time)` the pattern asks for, past ones dropped.

        Past times are **skipped rather than refused**, unlike
        `AvailableSlotForm`. The single-slot form is answering "publish this
        one time", where a past date is a mistake worth stopping on; a range
        starting today legitimately contains this morning, and refusing the
        whole publish because 09:00 has been and gone would be obstructive.
        """
        cleaned = cleaned or self.cleaned_data
        start, end = cleaned["start_date"], cleaned["end_date"]
        wanted = {int(d) for d in cleaned["weekdays"]}
        now = timezone.now()

        planned, day = [], start
        while day <= end:
            if day.weekday() in wanted:
                for at in cleaned["times"]:
                    if AvailableSlot(date=day, time=at).starts_at >= now:
                        planned.append((day, at))
            day += timedelta(days=1)
        return planned

    def save_for(self, user):
        """Create what is missing. Returns `(created, skipped)`.

        A duplicate is skipped, not an error. Publishing a second block of
        times over a week that already has some is a normal thing to do, and
        `unique_together` would otherwise turn it into an `IntegrityError`
        from the view - a 500 rather than an answer, which is exactly what
        plan 5.x found the single-slot form doing.
        """
        planned = self.planned_slots()
        existing = set(
            AvailableSlot.objects.filter(
                user=user,
                date__range=(self.cleaned_data["start_date"], self.cleaned_data["end_date"]),
            ).values_list("date", "time")
        )
        offered = self.offered()
        fresh = [
            AvailableSlot(user=user, date=day, time=at, **offered)
            for day, at in planned
            if (day, at) not in existing
        ]
        AvailableSlot.objects.bulk_create(fresh, ignore_conflicts=True)
        return len(fresh), len(planned) - len(fresh)

"""The events the booking flow emits, and who hears each one.

Call sites emit **events, never messages** (design §9.1). That is the whole
point of this module: `transition_to()` says "the provider confirmed the time"
and nothing about email, subject lines or phone numbers. Adding SMS later is
then a change to `CHANNELS` below, not a rewrite of every view that ever tells
somebody something.

**Who gets told is declared here, not at the call site.** The design sketches
`notify(event, appointment, to=...)`, and `to=` still works for the odd
one-off — but a call site that has to remember the audience is a call site that
can get it wrong, and this one is easy to get wrong in a way nobody notices:
per decision 8 a new booking notifies **the doctor being booked**, not a
central AGKI inbox, because the platform hosts doctors from other
organisations who cannot be expected to watch someone else's dashboard.
"""

from dataclasses import dataclass, field

PATIENT = "patient"
PROVIDER = "provider"


@dataclass(frozen=True)
class Event:
    name: str
    audience: tuple
    subject: str
    #: Per-audience subject overrides. Most events read the same to both
    #: parties ("Appointment cancelled: AGKI-2026-12345"), but some do not:
    #: "New appointment request: Asha Mbwana" is what the *doctor* needs in
    #: their inbox and is nonsense in the patient's, since they are the one who
    #: made the request. The body templates already branch on `is_patient`;
    #: without this the subject line was the one part that could not.
    subjects: dict = field(default_factory=dict)
    #: Template under appointments/notifications/, without the .txt
    template: str = ""
    #: Channels to deliver on. Email only today; adding "sms" here is the
    #: entire change needed once SmsChannel is implemented.
    channels: tuple = ("email",)

    def subject_for(self, audience):
        return self.subjects.get(audience, self.subject)

    @property
    def body_template(self):
        return f"appointments/notifications/{self.template or self.name.split('.')[-1]}.txt"


# --- the eight events (plan 3.5) -------------------------------------------
#
# Every patient-facing message carries a link back to their own appointment.
# That is not a courtesy: the flow needs the patient to come back twice - once
# to choose a mode, once to see it confirmed - and there are no patient
# accounts to log in to (design §0.1). The link is what makes that work.

BOOKED = Event(
    name="appointment.booked",
    # Both, and the doctor is the half decision 8 is about. The patient half
    # was added with the booking flow (plan 4.6): design §6 step 4 requires
    # them to be told their reference *and what happens next*, because what
    # happens next is a wait for someone else — the old page promised an
    # instant confirmation this flow cannot deliver.
    audience=(PROVIDER, PATIENT),
    subject="New appointment request: {appointment.fullName} on {slot_date}",
    subjects={PATIENT: "We have your booking request - reference {booking_id}"},
)
TIME_DECLINED = Event(
    name="appointment.time_declined",
    audience=(PATIENT,),
    subject="Your requested appointment time is not available",
)
# One confirmation, so one message (AGKI, 2026-09-03). This replaces
# `time_confirmed` (*"please choose how to meet"*), `mode_proposed` (*"the
# patient chose, please confirm"*) and `mode_confirmed` — three messages that
# only existed to carry a negotiation between two rounds that no longer
# happens. The patient hears once, and what they hear is that it is settled.
CONFIRMED = Event(
    name="appointment.confirmed",
    audience=(PATIENT,),
    subject="Your appointment is confirmed - {booking_id}",
)
# Sent instead of the above when the doctor confirmed a **different** way of
# meeting from the one asked for. It is still one decision and one message, but
# it is not the same message: "confirmed" and "confirmed, though not the way
# you asked" are different facts, and a patient who is now expected to travel
# needs to be told plainly rather than left to spot it in a details table.
CONFIRMED_OTHER_MODE = Event(
    name="appointment.confirmed_other_mode",
    audience=(PATIENT,),
    subject="Your appointment is confirmed, with a change - {booking_id}",
)
CANCELLED = Event(
    name="appointment.cancelled",
    # Both: a doctor whose slot has just been freed needs to know as much as
    # the patient who freed it.
    audience=(PATIENT, PROVIDER),
    subject="Appointment cancelled: {booking_id}",
)
EXPIRED = Event(
    name="appointment.expired",
    audience=(PATIENT, PROVIDER),
    subject="Appointment {booking_id} has expired",
)
COMPLETED = Event(
    name="appointment.completed",
    audience=(PATIENT,),
    subject="Thank you for visiting AGKI",
)

REGISTRY = {
    event.name: event
    for event in (
        BOOKED,
        TIME_DECLINED,
        CONFIRMED,
        CONFIRMED_OTHER_MODE,
        CANCELLED,
        EXPIRED,
        COMPLETED,
    )
}


def get(name):
    try:
        return REGISTRY[name]
    except KeyError:
        raise LookupError(
            f"Unknown notification event {name!r}. Known events: "
            f"{', '.join(sorted(REGISTRY))}. Events are declared in "
            f"appointments/notifications/events.py — a typo here would "
            f"otherwise mean a message that is simply never sent."
        ) from None

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
    #: Template under appointments/notifications/, without the .txt
    template: str = ""
    #: Channels to deliver on. Email only today; adding "sms" here is the
    #: entire change needed once SmsChannel is implemented.
    channels: tuple = ("email",)

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
    audience=(PROVIDER,),
    subject="New appointment request: {appointment.fullName} on {slot_date}",
)
TIME_CONFIRMED = Event(
    name="appointment.time_confirmed",
    audience=(PATIENT,),
    subject="Your appointment time is confirmed - please choose how to meet",
)
TIME_DECLINED = Event(
    name="appointment.time_declined",
    audience=(PATIENT,),
    subject="Your requested appointment time is not available",
)
MODE_PROPOSED = Event(
    name="appointment.mode_proposed",
    audience=(PROVIDER,),
    subject="{appointment.fullName} chose how to meet - please confirm",
)
MODE_CONFIRMED = Event(
    name="appointment.mode_confirmed",
    audience=(PATIENT,),
    subject="Your appointment is confirmed",
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
        TIME_CONFIRMED,
        TIME_DECLINED,
        MODE_PROPOSED,
        MODE_CONFIRMED,
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

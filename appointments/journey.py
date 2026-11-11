"""Where an appointment is in the flow, for the patient's benefit.

**Two rounds since 2026-09-03 (plan 12.1), not four.** The patient books with
everything the doctor needs — the time *and* how they would like to meet — and
the doctor answers once. AGKI asked for the collapse: the old flow confirmed
the time, sent the patient back to choose a mode, then confirmed that too, and
the two waits in the middle were the part patients felt.

What was here before described those four rounds. The mapping below is what is
left of it: `STAGES` shrank, and the states that only existed between the two
confirmations (`PENDING_MODE`, `MODE_PROPOSED`) are now read as "waiting for
the doctor" — which is what they mean to a patient either way.

Computed here rather than as an `{% if %}` ladder in the template for the same
reason `BANNER_CLASS` is a dict in `views`: a status added to the machine and
forgotten here falls out of `_ENDED` and `_REACHED` into the first stage,
which is visibly wrong, rather than rendering a half-drawn strip that looks
deliberate.
"""

from .models import Appointment

Status = Appointment.Status

#: The two rounds, in order. `mine` marks the one the patient acts on — which
#: is only the first, and only while they are filling the form in. After that
#: the wait is always the doctor's, which is the whole point of the change.
STAGES = (
    {"label": "You ask for a time and a way to meet", "mine": True},
    {"label": "The doctor confirms — settled", "mine": False},
)

#: How many rounds are behind this status, and which round is in progress.
#: `None` for a current stage means there is nothing in progress: the
#: appointment is settled.
#:
#: Round 1 is behind every one of these, because a status only exists once the
#: patient has asked.
_REACHED = {
    Status.PENDING_TIME: (1, 2),
    # Both legacy states mean the same thing now: the doctor has it.
    Status.PENDING_MODE: (1, 2),
    Status.MODE_PROPOSED: (1, 2),
    Status.CONFIRMED: (2, None),
    Status.COMPLETED: (2, None),
}

#: The endings. Each says what happened and what the patient can do about it,
#: because a strip of grey dots does neither.
_ENDED = {
    Status.CANCELLED: ("cancelled", "This appointment was cancelled."),
    Status.EXPIRED: (
        "expired",
        "This request expired before it could be settled. The time has been "
        "released.",
    ),
    Status.TIME_DECLINED: (
        "declined",
        "The doctor could not take that appointment.",
    ),
    Status.NO_SHOW: ("missed", "This appointment was recorded as missed."),
}


def _build(done, current, ended):
    stages = []
    for index, stage in enumerate(STAGES, start=1):
        if ended is not None:
            state = "done" if index <= done else "stopped"
        elif index <= done and index != current:
            state = "done"
        elif index == current:
            state = "now"
        else:
            state = "ahead"
        stages.append({"label": stage["label"], "state": state, "mine": stage["mine"]})
    return stages


def journey_before_booking():
    """The strip on the booking form, where there is no appointment yet.

    Round 1 is in progress rather than done: the patient is filling it in. It
    is the same component the other pages use, so the form cannot drift into
    describing a different flow from the pages after it.
    """
    return {
        "stages": _build(done=0, current=1, ended=None),
        "ended": None,
        "waiting_on_you": False,
        "settled": False,
    }


def journey_for(appointment):
    """The tracker's state for one appointment.

    Returns a dict with `stages` (each carrying `state` — done, now or
    ahead — and whether the wait is the patient's), `ended` for a booking that
    stopped, and `waiting_on_you`, which is now always False once a booking
    exists: with one confirmation there is nothing left for the patient to do
    but wait, or cancel. It is kept because four templates read it, and because
    a key that answers "no" is clearer than a key that disappeared.
    """
    status = appointment.status
    ended = _ENDED.get(status)
    done, current = _REACHED.get(status, (1, 2))

    return {
        "stages": _build(done, current, ended),
        "ended": {"kind": ended[0], "detail": ended[1]} if ended else None,
        "waiting_on_you": False,
        "settled": status in (Status.CONFIRMED, Status.COMPLETED),
    }

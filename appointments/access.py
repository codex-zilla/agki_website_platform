"""Who may read a patient's appointment record.

AGKI's patients have no accounts (design §2), so "is this your appointment?"
cannot be answered by asking who is logged in. It is answered by a **signed,
appointment-scoped marker in the session**, granted in exactly two places
(design §8.1):

1. ``booking_form``, to the person who just created the appointment.
2. ``booking_id_lookup``, after a POST matching the booking ID **and** one
   piece of personal information.

Before this, ``/appointments/detail/<pk>/`` took a sequential integer and
checked nothing at all: incrementing a number in the URL returned somebody
else's name, date of birth, phone, email and symptoms.

**Why sign a value that already lives in a server-side session.** The session
backend is a deployment detail — switch to ``signed_cookies`` and every marker
moves into the browser. Signing here means that change cannot silently turn a
server-side fact into a client-editable one. It also buys the expiry below for
free.
"""

from django.core import signing

SESSION_KEY = "appointment_access"
SIGNING_SALT = "appointments.detail-access"

# How long a marker stays valid. Long enough that a patient coming back to
# choose a meeting mode (round 2, design §0.1) is usually not asked to
# re-verify, short enough that a shared or public computer does not keep
# somebody's medical record reachable indefinitely. When `AppointmentSettings`
# exists (plan 2.1) this is a candidate to move there.
ACCESS_MAX_AGE = 7 * 24 * 60 * 60  # seconds


def grant_appointment_access(request, appointment):
    """Record that this session has proved its claim to `appointment`."""
    granted = dict(request.session.get(SESSION_KEY) or {})
    granted[appointment.bookingID] = signing.dumps(
        appointment.bookingID, salt=SIGNING_SALT
    )
    request.session[SESSION_KEY] = granted


def has_appointment_access(request, appointment):
    """True if this session may read `appointment`.

    Scoped per appointment on purpose: proving you own one booking says
    nothing about any other, so the marker is a map keyed by booking ID rather
    than a single "verified" flag.
    """
    token = (request.session.get(SESSION_KEY) or {}).get(appointment.bookingID)
    if not token:
        return False
    try:
        unsigned = signing.loads(token, salt=SIGNING_SALT, max_age=ACCESS_MAX_AGE)
    except signing.BadSignature:  # also covers SignatureExpired
        return False
    return unsigned == appointment.bookingID


def revoke_appointment_access(request, appointment):
    """Drop one appointment's marker — used when a session should stop
    counting as verified (e.g. after a patient cancels)."""
    granted = dict(request.session.get(SESSION_KEY) or {})
    if granted.pop(appointment.bookingID, None) is not None:
        request.session[SESSION_KEY] = granted

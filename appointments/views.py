from collections import defaultdict
from datetime import datetime, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from urllib.parse import quote

from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from appointments.forms import SLOT_TAKEN, SLOT_TOO_SOON, AppointmentForm, humanise
from useraccount.models import CustomUser, Profile, Specialty
from .access import grant_appointment_access, has_appointment_access
# `Rating` is deliberately not imported: its three read sites are gone
# (decision 7, design §5.4). The model stays, reshaped, for when feedback is
# built — reading an empty table was what produced the permanent "No ratings
# yet" the old pages showed as though it were a measurement.
from .models import (
    Appointment,
    AppointmentNote,
    AppointmentSettings,
    AvailableSlot,
    InvalidTransition,
)
from .journey import journey_before_booking, journey_for
from .periods import MONTH, WEEK, anchor_date, mode_from, period, step, weeks_between
from .notifications import notify
from .ratelimit import lookup_is_rate_limited

# One message for every way a lookup can fail — wrong booking ID, wrong
# verification value, or an appointment that does not exist. Saying which part
# was wrong would confirm that a booking ID is real, which is the one thing an
# attacker with a list of guesses needs to know (design §8.1).
LOOKUP_FAILED = "We couldn't find an appointment matching those details. Please check and try again."
LOOKUP_THROTTLED = "Too many attempts. Please wait a few minutes and try again."
NEEDS_VERIFICATION = "Please confirm your details to view your appointment."

NOT_YOUR_TURN = "There is nothing for you to do on this appointment right now."
CANCELLED_OK = "Your appointment has been cancelled and the time released."
CANCEL_TOO_LATE = (
    "This appointment can no longer be cancelled online — it is too close to "
    "the appointment time. Please call us instead."
)
# Deliberately says nothing about *why*. "Not currently taking appointments"
# is the truth for a doctor on leave and for one an administrator has just
# switched off, and a patient has no business being told which — nor should a
# page confirm that a withdrawal happened to whoever prompted it (10.3).
DOCTOR_UNAVAILABLE = (
    "That doctor is not currently taking appointments. Please choose another "
    "from the list below."
)

# Nine states, four banner styles. Here rather than as an `{% if %}` ladder in
# the template so that a state added to the machine and forgotten here falls
# back to "pending" instead of rendering an unstyled banner.
BANNER_CLASS = {
    Appointment.Status.PENDING_TIME: "pending",
    Appointment.Status.PENDING_MODE: "pending",
    Appointment.Status.MODE_PROPOSED: "pending",
    Appointment.Status.CONFIRMED: "confirmed",
    Appointment.Status.COMPLETED: "completed",
    Appointment.Status.CANCELLED: "cancelled",
    Appointment.Status.EXPIRED: "cancelled",
    Appointment.Status.TIME_DECLINED: "cancelled",
    Appointment.Status.NO_SHOW: "cancelled",
}


def _offerable_slots(settings, now):
    """Free, inside the horizon, and far enough away to be bookable.

    The date bounds are a cheap narrowing; `starts_at` is what actually
    enforces the notice, because "later today" cannot be expressed as a date
    comparison — so callers filter on it themselves.

    Lifted out of `search_results` so that `appointment_home` counts
    specialties by exactly the same rule the list it links to uses. Two
    queries with the same intent and different code is how a front door comes
    to advertise a specialty whose page is empty.
    """
    horizon = now.date() + timedelta(days=settings.booking_horizon_days)
    return (
        AvailableSlot.objects.filter(
            is_booked=False,
            date__gte=settings.earliest_bookable_start(now).date(),
            date__lte=horizon,
            # Slots belonging to somebody a patient may not book (10.3). This
            # narrows the specialty counts as well as the cards, which is the
            # half that would otherwise have been missed: a specialty counted
            # from a switched-off doctor's slots renders as available and
            # links to an empty page - the exact bug 4.2 fixed for doctors on
            # leave, arriving again by a different route.
            **CustomUser.publicly_bookable_filter("user__"),
        )
        .select_related("user")
        .order_by("date", "time")
    )


def _bookable_specialties(bookable_doctor_ids):
    """Active specialties, each counting only doctors who can be booked now.

    A specialty whose doctors have all gone on leave is shown as unavailable
    rather than as a link to an empty page (design §5.5).
    """
    return list(
        Specialty.objects.filter(is_active=True)
        .annotate(
            bookable_doctors=Count(
                "doctors",
                filter=Q(doctors__user_id__in=bookable_doctor_ids),
                distinct=True,
            )
        )
        .order_by("name")
    )


def appointment_home(request):
    """The front door of the booking flow (plan 11.11).

    What was here rendered a 737-line static page for a product called
    MediBook: a hero, six feature cards promising digital prescriptions,
    online payments, one-click rescheduling and 24/7 booking, eight hardcoded
    specialty tiles that linked nowhere, and a footer of `#`s. The view passed
    **no context at all**, so nothing on the page came from AGKI's database and
    nothing on it could ever be true by accident.

    What replaces it does two things a patient needs before they book: it
    offers the same search the list uses, over the real specialties with real
    counts, and it says plainly that booking is a four-round negotiation
    rather than a checkout. The second is what stops the "why is it still not
    confirmed?" phone call.
    """
    settings = AppointmentSettings.load()
    now = timezone.now()
    earliest = settings.earliest_bookable_start(now)

    offerable = _offerable_slots(settings, now)
    bookable_doctor_ids = {
        slot.user_id for slot in offerable if slot.starts_at >= earliest
    }

    context = {
        'specialties': _bookable_specialties(bookable_doctor_ids),
        'bookable_doctors': len(bookable_doctor_ids),
        'booking_notice': humanise(settings.minimum_booking_notice),
        'horizon_days': settings.booking_horizon_days,
        'page_title': 'Book an appointment',
        'page_section': 'Appointments',
    }
    return render(request, 'appointments/appointment_home.html', context)


def search_results(request):
    """The public doctor list: who is bookable, and when (design §6 step 2).

    Three things changed here, and each was a bug rather than a preference:

    * It iterated `Profile`, so a doctor who had never opened their own
      profile page was invisible to patients (design §5.3). It now lists
      accounts by `role`, and a missing profile costs some card detail rather
      than the whole doctor.
    * The horizon was hard-coded to five days, so nothing past the day after
      tomorrow could be booked however far ahead doctors published (decision 5).
    * It offered every slot, including ones the booking view would then refuse
      for being inside the minimum booking notice (decision D). A slot the
      system will not accept must not be presented as bookable.
    * It listed doctors on `role` and `is_active` alone, so an account an
      administrator had switched off stayed on the public site (10.3). Under
      decision F that switch is the only thing standing between AGKI and a
      fraudulent listing, and a switch that removes somebody's login while
      leaving their card up is not one.

    Filtering is server-side. The old page shipped a client-side filter engine
    over gender, rating and experience — which filtered only the cards already
    on the page, and two of whose three axes were fake (`Rating` is empty and
    "Accepting New Patients" was hardcoded `true`).
    """
    settings = AppointmentSettings.load()
    now = timezone.now()
    earliest = settings.earliest_bookable_start(now)

    offerable = _offerable_slots(settings, now)
    bookable_doctor_ids = {
        slot.user_id for slot in offerable if slot.starts_at >= earliest
    }
    specialties = _bookable_specialties(bookable_doctor_ids)

    selected_specialty = request.GET.get("specialty", "").strip()
    location_query = request.GET.get("location", "").strip()

    doctors = (
        CustomUser.objects.filter(**CustomUser.publicly_bookable_filter())
        .order_by("first_name", "last_name", "username")
    )
    if selected_specialty:
        doctors = doctors.filter(profile__specialties__slug=selected_specialty)
    if location_query:
        doctors = doctors.filter(
            Q(profile__region__icontains=location_query)
            | Q(profile__location__icontains=location_query)
        )
    doctors = doctors.distinct()

    # Profiles are fetched separately rather than through `select_related`, so
    # that a doctor without one still appears — with a thinner card, which is
    # the honest outcome, instead of vanishing from the site.
    profiles = {
        profile.user_id: profile
        # `qualifications` is prefetched too since plan 11.7. The card below
        # read `doctor.qualifications` - the **free-text** field on
        # `CustomUser` that 11.6 stopped writing to - so a doctor who picked
        # "MD" from the new list saw it in their own profile preview and
        # patients saw nothing. A preview that flatters the real page is worse
        # than no preview.
        for profile in Profile.objects.filter(user__in=doctors).prefetch_related(
            "specialties", "qualifications"
        )
    }

    # `offerable` is ordered by date then time, so the first entry for a
    # doctor is their soonest bookable slot.
    slots_by_doctor = defaultdict(list)
    for slot in offerable:
        if slot.starts_at >= earliest:
            slots_by_doctor[slot.user_id].append(slot)

    doctors_data = [
        {
            "doctor": doctor,
            "profile": profiles.get(doctor.id),
            "specialties": list(profiles[doctor.id].specialties.all()) if doctor.id in profiles else [],
            # A summary, not the times themselves (plan 11.15). This card used
            # to render every bookable slot inside the 30-day horizon, grouped
            # by day: for the one doctor in the current database that is 59
            # chips across 20 day-groups, about 2,400px, six to a page. The
            # list could not be scanned, which is the one thing a list is for.
            #
            # Three facts replace them, because they are what a patient
            # chooses on: whether the doctor can be booked at all, the soonest
            # time, and how much there is. The times live on the doctor's own
            # page now, where there is room to lay them out.
            "next_slot": (slots_by_doctor.get(doctor.id) or [None])[0],
            "slot_count": len(slots_by_doctor.get(doctor.id, ())),
            "day_count": len({slot.date for slot in slots_by_doctor.get(doctor.id, ())}),
        }
        for doctor in doctors
    ]

    # Doctors with something bookable first: a page whose first screen is
    # doctors you cannot book is a page that looks broken.
    doctors_data.sort(key=lambda entry: (entry["slot_count"] == 0,))

    page = Paginator(doctors_data, 6).get_page(request.GET.get("page"))

    # Ratings are not computed here any more (decision 7, design §5.4).
    # Nothing has ever written a `Rating`, so every average this produced was
    # a fixed "No ratings yet" dressed up as a measurement. The model is kept
    # and reshaped for when feedback is actually built; until then, showing
    # nothing is more honest than showing a placeholder that looks like data.

    context = {
        'page': page,
        'doctors_data': page.object_list,
        'result_count': len(doctors_data),
        'specialties': specialties,
        'selected_specialty': selected_specialty,
        'location_query': location_query,
        'booking_notice': humanise(settings.minimum_booking_notice),
        'today': now.date(),
        # The card says what a doctor has *inside the horizon*, so the card
        # has to be able to name it.
        'horizon_days': settings.booking_horizon_days,
        'page_title': 'Find a doctor and a time',
        'page_section': 'Appointments',
    }

    return render(request, 'appointments/search_results.html', context)


# `DAY_PARTS` grouped a day's times into morning, afternoon and evening while
# they were listed under the calendar. The times open in a day drawer now, in
# the row format the console uses (plan 11.17), and a drawer holds one day -
# rarely more than a handful of rows, which do not need dividing into thirds.


def doctor_detail(request, doctor_id):
    """One doctor: who they are, and when they can be seen (plan 11.15).

    The flow has never had this page. Availability was listed inside the
    results card, so the list of doctors was mostly a list of times, and the
    doctor's own qualifications, region and description had nowhere to live —
    the bio a doctor writes on their profile was shown to nobody.

    Availability is offered a day at a time. The chosen day is a query
    parameter rather than a fragment or a fetch, so the view stays a plain GET
    that works without JavaScript, can be sent to somebody, and survives a
    refresh.
    """
    doctor = get_object_or_404(CustomUser, id=doctor_id, role=CustomUser.Role.DOCTOR)

    # The same rule, read from the same place, as `booking_form`'s guard
    # (10.3): a page that describes a withdrawn doctor and offers their times
    # would undo the one lever AGKI has against a fraudulent listing.
    if not doctor.is_publicly_bookable:
        messages.error(request, DOCTOR_UNAVAILABLE)
        return redirect('search_results')

    settings = AppointmentSettings.load()
    now = timezone.now()
    earliest = settings.earliest_bookable_start(now)

    slots = [
        slot
        for slot in _offerable_slots(settings, now).filter(user=doctor)
        if slot.starts_at >= earliest
    ]

    by_date = defaultdict(list)
    for slot in slots:
        by_date[slot.date].append(slot)
    days = [{"date": date, "count": len(day)} for date, day in sorted(by_date.items())]

    # A calendar in the shape the console's availability page draws (plan
    # 11.1), navigable by week or month, at AGKI's instruction. What it
    # replaces was a strip of the days that had something, scrolling sideways:
    # with a 30-day horizon that is twenty pills wide, which traded the page's
    # height for its width.
    #
    # The default view is the **month**, where the console defaults to the
    # week. The two pages are answering different questions: a doctor
    # publishing times works a week at a time, while a patient is choosing
    # among the next thirty days and wants to see them at once.
    today = timezone.localdate()
    horizon_end = today + timedelta(days=settings.booking_horizon_days)
    mode = mode_from(request, default=MONTH)
    anchor = anchor_date(request)
    period_start, period_end, period_label = period(anchor, mode)

    weeks = weeks_between(period_start, period_end, lambda day: {
        "date": day,
        "slots": by_date.get(day, []),
        # Only bookable slots are counted. This page never shows the doctor's
        # booked or too-late times: those are real to the doctor and
        # meaningless to a patient, which is exactly why the console shows
        # three counts on a day and this shows one.
        "count": len(by_date.get(day, ())),
        "is_today": day == today,
        "is_past": day < today,
        # Two different things, and the cell styles them differently: a day can
        # be inside the month on screen but outside the window a patient may
        # book in.
        "in_period": period_start <= day <= period_end,
        "bookable_window": today <= day <= horizon_end,
    })

    # The day whose times are open, and **only** when the URL asks for one.
    #
    # It used to fall back to the first day that had something, which suited
    # the panel this replaced: a fold-out under the calendar is just more page.
    # A modal is not. Opening one over a calendar the patient has not looked at
    # yet is the pop-up nobody asked for, so an absent, unparseable or empty
    # `date` simply leaves it shut. The parameter arrives from a link this page
    # drew, but also from browser history, from a bookmark taken weeks ago, and
    # from anybody who types one — none of which should 404.
    selected = None
    asked = request.GET.get("date", "").strip()
    if asked:
        try:
            selected = datetime.strptime(asked, "%Y-%m-%d").date()
        except ValueError:
            selected = None
    if selected not in by_date:
        selected = None

    profile = Profile.objects.filter(user=doctor).first()
    context = {
        'doctor': doctor,
        'profile': profile,
        'specialties': list(profile.specialties.all()) if profile else [],
        'qualifications': list(profile.qualifications.all()) if profile else [],
        'days': days,
        'weeks': weeks,
        'selected_date': selected,
        # The day the drawer opens on. Rendered by the server so the page
        # works with JavaScript switched off; the script below the calendar
        # then opens other days without a round trip, from markup Django
        # already wrote into each cell.
        'selected_slots': by_date.get(selected, []),
        'view_mode': mode,
        # Whether *this* period has anything, which is not the same question as
        # whether a day is open. Without it the "nothing to book here" line
        # would show on every arrival, since arriving opens nothing.
        'period_has_times': any(
            period_start <= entry["date"] <= period_end for entry in days
        ),
        'period_label': period_label,
        'period_start': period_start,
        'prev_start': step(anchor, mode, -1),
        'next_start': step(anchor, mode, +1),
        'is_current_period': period_start <= today <= period_end,
        'next_slot': slots[0] if slots else None,
        'slot_count': len(slots),
        'day_count': len(days),
        'booking_notice': humanise(settings.minimum_booking_notice),
        'horizon_days': settings.booking_horizon_days,
        'page_title': 'Dr. %s' % (doctor.get_full_name() or doctor.username),
        'page_section': 'Appointments',
    }
    return render(request, 'appointments/doctor_detail.html', context)


def booking_form(request, id):
    slot = get_object_or_404(AvailableSlot, id=id)
    settings = AppointmentSettings.load()

    # The doctor may have been switched off since this link was made — or the
    # link may never have come from the search page at all, since a slot id is
    # a small integer anybody can type.
    #
    # **This is what makes 10.3 a control rather than a cosmetic change.**
    # Design §4.5 leaves AGKI exactly one lever against a fraudulent listing,
    # withdrawing `is_approved`, and a lever that removes a doctor's card
    # while a direct URL still books them has not removed anything. Filtering
    # the list and guarding the view are the two halves of one rule, which is
    # why they read it from the same place (`CustomUser.PUBLICLY_BOOKABLE`).
    #
    # Placed above the method check on purpose, so it refuses the POST as well
    # as hiding the GET. It is deliberately *not* pushed down into
    # `claim_slot`'s row lock, unlike the slot-taken and too-soon rules: those
    # guard a race between two patients that happens in milliseconds, while
    # this guards an administrator's decision that is minutes old by the time
    # anyone acts on it. A lock there would buy nothing and would imply the
    # race matters.
    if not slot.user.is_publicly_bookable:
        messages.error(request, DOCTOR_UNAVAILABLE)
        return redirect('search_results')

    # Get the doctor's profile. A missing Profile must not 404 the booking
    # page: profiles are created lazily, so three of the doctors in the
    # current database have none (design §5.3, and see plan 1.1's note). The
    # signal and backfill that stop this happening are plan 2.13; this is the
    # guard that stops it being a crash in the meantime.
    doctor = Profile.objects.filter(user=slot.user).first()

    form = AppointmentForm(slot=slot, settings=settings)
    if request.method == 'POST':
        form = AppointmentForm(request.POST, slot=slot, settings=settings)
        if form.is_valid():
            try:
                appointment = claim_slot(slot, form, settings)
            except SlotUnavailable as unavailable:
                form.add_error(None, str(unavailable))
            else:
                # The patient who just filled this in is the one person who
                # provably owns this record, so this is where the session marker
                # is granted (design §8.1). The success page checks it like any
                # other page; it does not hand out access of its own, which is
                # what the old `/booking-success/<pk>/` effectively did.
                grant_appointment_access(request, appointment)
                return redirect('booking_success_page', booking_id=appointment.bookingID)

    context = {
        'slot': slot,
        'doctor': doctor,
        'form': form,
        # The same four-round strip the pages after this one show. It replaces
        # a three-step bar that ended two rounds early (plan 11.11).
        'journey': journey_before_booking(),
        'page_title': 'Complete your booking',
        'page_section': 'Appointments',
        # The page is rendered for a slot chosen from a list that may be
        # minutes stale, so it says up front when the slot can no longer be
        # booked rather than accepting the form and refusing it afterwards.
        'slot_is_bookable': slot.is_bookable(settings),
        'booking_notice': humanise(settings.minimum_booking_notice),
    }

    return render(request, 'appointments/booking_form.html', context)


class SlotUnavailable(Exception):
    """The slot could not be claimed — taken, or now too close."""


def claim_slot(slot, form, settings):
    """Create the appointment, or refuse because the slot is gone.

    The form has already checked both rules; this checks them again **under a
    row lock**, because between the two checks somebody else can book the same
    slot. That is not theoretical here: `booking_form` never read `is_booked`
    at all, so two *sequential* bookings on one slot both succeeded and the
    database held two appointments for one real time (design §10 #6).

    Three layers, deliberately: the form for the message, this lock for the
    race, and `Appointment.slot`'s OneToOne constraint underneath both, in case
    a future caller skips the first two.
    """
    with transaction.atomic():
        locked = AvailableSlot.objects.select_for_update().get(pk=slot.pk)
        if locked.is_booked or Appointment.objects.filter(slot=locked).exists():
            raise SlotUnavailable(SLOT_TAKEN)
        if locked.starts_at < settings.earliest_bookable_start():
            raise SlotUnavailable(
                SLOT_TOO_SOON % {"notice": humanise(settings.minimum_booking_notice)}
            )

        appointment = form.save(commit=False)
        appointment.slot = locked
        try:
            appointment.save()
        except IntegrityError as clash:
            raise SlotUnavailable(SLOT_TAKEN) from clash

        # Queued inside the transaction, so a booking that rolls back cannot
        # email a doctor about an appointment that does not exist.
        notify("appointment.booked", appointment)

    return appointment


def _appointment_for_reader(request, booking_id):
    """Return the appointment this session is allowed to read, or None.

    One helper for both patient-facing record pages, so they cannot drift
    apart — and one uniform answer for "no such booking" and "not yours".
    Distinguishing them would let an attacker use the response itself to
    confirm which booking IDs are real (design §8.1).
    """
    appointment = Appointment.objects.filter(bookingID=booking_id).select_related(
        "slot", "slot__user"
    ).first()
    if appointment is None or not has_appointment_access(request, appointment):
        return None
    return appointment


def booking_success_page(request, booking_id):
    appointment = _appointment_for_reader(request, booking_id)
    if appointment is None:
        messages.info(request, NEEDS_VERIFICATION)
        return redirect('booking_id_lookup')

    profile = Profile.objects.filter(user=appointment.slot.user).first()
    context = {
        'appointment': appointment,
        'profile': profile,
        'journey': journey_for(appointment),
        'page_title': 'Request sent',
        'page_section': 'Appointments',
    }
    return render(request, 'appointments/booking_success_page.html', context)


def _digits(value):
    return "".join(character for character in value if character.isdigit())


def _matches_verification(appointment, answer):
    """Does `answer` match this appointment's phone number or date of birth?

    One field accepting either, because asking a walk-in patient to remember
    *which* of the two the form wants is friction with no security value —
    both are things only they and AGKI know.

    Phone numbers are compared on their last nine digits, so `+255 700 000 000`,
    `0700 000 000` and `0700000000` all match the same stored value. Numbers
    are stored inconsistently here, and a patient typing their own number in a
    different-but-correct format should not be told their details are wrong.
    """
    answer = (answer or "").strip()
    if not answer:
        return False

    typed_digits = _digits(answer)
    stored_digits = _digits(appointment.phone or "")
    if len(typed_digits) >= 9 and len(stored_digits) >= 9:
        if typed_digits[-9:] == stored_digits[-9:]:
            return True

    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(answer, date_format).date()
        except ValueError:
            continue
        if parsed == appointment.birthDate:
            return True
    return False


def short_link(request, booking_id):
    """`/a/AGKI-2026-58614` -> the lookup form with the reference filled in.

    Exists for SMS and nothing else (plan 13.3). The full link is
    `/appointments/lookup/?bookingID=AGKI-2026-58614`, which is 75 characters
    against a real domain; this is 46. **29 characters is the difference
    between one segment and two** on most of the messages that carry it, and a
    segment is a second charge for the same message - so the short URL is a
    cost decision as much as a tidiness one.

    It redirects rather than rendering, so there is exactly one lookup page to
    maintain and exactly one place the verification gate lives (design §8.1).
    An unknown or malformed reference is not checked here: the lookup form
    already answers every failure with the same message, and confirming that a
    booking ID exists is precisely what that sameness is there to prevent.
    """
    return redirect(f"{reverse('booking_id_lookup')}?bookingID={quote(booking_id)}")


def booking_id_lookup(request):
    """Trace an appointment: booking ID **plus** one verification field.

    The booking ID alone was the whole gate before, and it is not a secret —
    `AGKI-<year>-<5 digits>` is 100,000 possibilities a year, enumerable in an
    afternoon. §8.1 requires a second field; §8.2 requires that guessing at it
    be slow.
    """
    # Notification emails link here with the reference already in the URL, so
    # the patient only has to supply the one thing that proves the record is
    # theirs. Linking straight to the detail page would bounce them to this
    # form anyway — and lose the reference they arrived with (design §8.1).
    context = {
        "bookingID": request.GET.get("bookingID", "").strip(),
        "page_title": "Find my appointment",
        "page_section": "Appointments",
    }
    if request.method == 'POST':
        if lookup_is_rate_limited(request):
            context['error'] = LOOKUP_THROTTLED
            # 429 rather than a 200 carrying an error: this is the one failure
            # a client is meant to detect and back off from.
            return render(request, 'appointments/booking_id_lookup.html', context, status=429)

        booking_id = request.POST.get('bookingID', '').strip()
        verification = request.POST.get('verification', '')

        appointment = Appointment.objects.filter(bookingID=booking_id).first()
        if appointment is not None and _matches_verification(appointment, verification):
            grant_appointment_access(request, appointment)
            return redirect('appointment_detail', booking_id=appointment.bookingID)

        context['error'] = LOOKUP_FAILED
        context['bookingID'] = booking_id

    return render(request, 'appointments/booking_id_lookup.html', context)


def appointment_detail(request, booking_id):
    """The patient's view of their own appointment — the real state of it.

    The old page rendered a hardcoded "Confirmed" banner (and a countdown to a
    date in a template literal), then offered Reschedule, Contact Doctor and
    Live Chat, none of which had a view behind them. What a patient needs from
    this page is two things: what state their booking is actually in, and
    whether it is their turn to do something.
    """
    appointment = _appointment_for_reader(request, booking_id)
    if appointment is None:
        messages.info(request, NEEDS_VERIFICATION)
        return redirect('booking_id_lookup')

    settings = AppointmentSettings.load()
    profile = Profile.objects.filter(user=appointment.slot.user).first()
    appointment_notes = AppointmentNote.objects.filter(appointment=appointment).order_by('-created_at')
    context = {
        'appointment': appointment,
        'profile': profile,
        'appointment_notes': appointment_notes,
        'status': appointment.status,
        'Status': Appointment.Status,
        'banner_class': BANNER_CLASS.get(appointment.status, 'pending'),
        # The patient's turn is exactly one state (design §0.1), and saying so
        # here keeps the template from re-deriving it slightly differently.
        # Kept as a key that is now always False rather than deleted: the
        # legacy state can still exist on a row that escaped migration 0027,
        # and the template branch it guards has become the explanation of what
        # happened to those bookings.
        'patient_must_choose_mode': appointment.status == Appointment.Status.PENDING_MODE,
        'can_cancel': appointment.can_be_cancelled(settings),
        'cancellation_notice': humanise(settings.cancellation_cutoff),
        'transitions': appointment.transitions.select_related("actor"),
        'journey': journey_for(appointment),
        'page_title': 'My appointment',
        'page_section': 'Your appointment',
    }
    return render(request, 'appointments/appointment_detail.html', context)


# `appointment_choose_mode` stood here: round two, where the patient came back
# to say how they would like to meet after the doctor had confirmed the time.
# Retired on 2026-09-03 (plan 12.1). The question is asked on the booking form
# now, from the modes the slot actually offers, so there is nothing left to
# come back for - and a page that exists only to be reached from an email link
# is a page that has to be deleted deliberately, or it lingers as a URL that
# half-works.
#
# The route is gone too. A patient following an old link from an email lands on
# their appointment instead of a 404, which is where the same information now
# lives (see `appointment_detail`).


@require_POST
def appointment_cancel(request, booking_id):
    """The one Manage-Appointment button that is real (design §6 step 9).

    POST only: this frees a slot and emails two people, which is not something
    a link preview or a crawler should be able to do.
    """
    appointment = _appointment_for_reader(request, booking_id)
    if appointment is None:
        messages.info(request, NEEDS_VERIFICATION)
        return redirect('booking_id_lookup')

    settings = AppointmentSettings.load()
    if not appointment.can_be_cancelled(settings):
        messages.error(request, CANCEL_TOO_LATE)
        return redirect('appointment_detail', booking_id=appointment.bookingID)

    reason = request.POST.get('reason', '').strip()[:255]
    appointment.cancellation_reason = reason
    try:
        appointment.transition_to(
            Appointment.Status.CANCELLED,
            actor=None,
            note="Cancelled by the patient." + (f" Reason: {reason}" if reason else ""),
        )
    except InvalidTransition:
        messages.error(request, CANCEL_TOO_LATE)
    else:
        messages.success(request, CANCELLED_OK)
    return redirect('appointment_detail', booking_id=appointment.bookingID)

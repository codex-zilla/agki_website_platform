import calendar
from datetime import date as date_cls, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import ProtectedError, Q
from django.utils.http import url_has_allowed_host_and_scheme
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from appointments.forms import (
    TIME_CHOICES,
    AppointmentNoteForm,
    AvailableSlotForm,
    BulkAvailabilityForm,
)
from appointments.models import (
    Appointment,
    AppointmentNote,
    AppointmentSettings,
    AvailableSlot,
    InvalidTransition,
)
from ecommerce.models import Category, Item
from useraccount.decorators import role_required, superuser_required
from useraccount.models import CustomUser

from .scoping import scope_appointments, scope_slots, sees_every_doctor

# Every view in this module is behind an access decorator, and that is a
# deliberate all-or-nothing rule rather than a judgement call per view: until
# now the console had no auth wall at all (design §10 dashboard #1). If a new
# view is added here without one, `DashboardAccessTests` fails — it enumerates
# the URLconf rather than a hand-written list, so "I forgot" is not a
# reachable state.
STAFF_CONSOLE = role_required("admin", "doctor")
ADMIN_ONLY = role_required("admin")

#: Statuses that are waiting on somebody. The sidebar badge and the "pending"
#: stat both count these, and they replace a `AppointmentApproval.is_approved`
#: query that could not tell "waiting for the doctor" from "waiting for the
#: patient" — one boolean for a flow with two gates (design §5).
WAITING_STATUSES = (
    Appointment.Status.PENDING_TIME,
    Appointment.Status.PENDING_MODE,
    Appointment.Status.MODE_PROPOSED,
)
#: The subset the *doctor* is holding up - which, since the single
#: confirmation (plan 12.1), is **all** of them. The distinction this set
#: existed to draw was `PENDING_MODE`, the patient's turn to choose a mode; the
#: patient chooses when they book now, so nothing waits on them. It stays a
#: separate name because the two questions ("is this open?" and "is it mine?")
#: are still different questions, and a flow that grows a patient-side wait
#: again will need somewhere to say so.
DOCTOR_ACTION_STATUSES = WAITING_STATUSES
#: Over, one way or another. Four endings the old `is_approved` boolean could
#: not tell apart, and they mean genuinely different things: the patient
#: changed their mind, the doctor said no, nobody answered in time, the patient
#: did not turn up.
ENDED_STATUSES = (
    Appointment.Status.CANCELLED,
    Appointment.Status.EXPIRED,
    Appointment.Status.TIME_DECLINED,
    Appointment.Status.NO_SHOW,
)


def _safe_next(request, fallback):
    """Where to go after a POST, when the page asked to come back to itself.

    Four views take a `next` out of the request body. Passing it straight to
    `redirect()` makes each one an **open redirect**: a crafted form could send
    a signed-in doctor to another site that looks like this one. CSRF makes it
    awkward to exploit and does not make it safe, and it costs one call to rule
    out.
    """
    candidate = request.POST.get("next", "")
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return fallback


def _appointments(user):
    return scope_appointments(
        Appointment.objects.select_related("slot", "slot__user"), user
    )


# --- held for review, not kept on merit (plan 5.7, 5.9) ---------------------
#
# Both of these are scheduled for deletion and neither is linked from the
# sidebar any more. They stay reachable by URL **only** so AGKI can compare
# them against their replacements in a browser before the deletion is made
# final — decision 5 made deleting `doctor_schedule` conditional on
# `doctor_availability` really providing the week/month timetable it was
# imitating, and that condition is a judgement somebody should make by looking.
#
# `doctor_schedule` renders 100% hardcoded fake data — invented patients,
# invented times — from a view that passes no context at all. `doctor_base`
# renders the *base template* as a standalone page (design §10 dashboard #19),
# which is why it comes out with no content and no navigation state.

@STAFF_CONSOLE
def doctor_base(request):
    return render(request, 'doctorDashboard/doctor_base.html')


@STAFF_CONSOLE
def doctor_schedule(request):
    return render(request, 'doctorDashboard/doctor_schedule.html')


@STAFF_CONSOLE
def doctor_admin_dashboard(request):
    """What needs the doctor now, and how the week looks (plan 11.5).

    **The page this replaces told every doctor the same thing about every
    appointment, and it was wrong.** The status column read
    `i.appointmentapproval.is_approved` - the superseded `AppointmentApproval`
    model, which nothing has written to since Phase 5 and which the plan
    records as "written by nothing at all". A missing related object resolves
    to empty in a template, which is falsy, so **every row took the else
    branch**: confirmed, completed, cancelled and waiting all rendered
    "Pending". That is plan 5.14's bug, fixed on the appointments list and
    never brought here.

    Four controls did nothing either - "View All", "Full Calendar" and the
    week's prev/next were bare `<button>` elements on a page with no script
    block at all, so they were not even calling a function that was missing.
    """
    user = request.user
    today = timezone.localdate()
    settings = AppointmentSettings.load()

    appointments = _appointments(user)

    # Ordered, and annotated with what each row may offer - the same rules the
    # appointments list uses (11.4), so a decision cannot be offered here that
    # would be refused there.
    today_appointments = list(
        appointments.filter(slot__date=today).order_by("slot__time")
    )
    now = timezone.now()
    for appointment in today_appointments:
        appointment.inline_actions, appointment.page_actions = _row_actions(
            appointment, settings
        )
        appointment.is_now = (
            appointment.slot.starts_at <= now
            < appointment.slot.starts_at + timedelta(hours=1)
        )

    # The week strip, navigable. `_anchor_date` / `_period` / `_step` are the
    # availability page's, reused rather than re-derived: prev and next here
    # were `<button>` elements with no handler, so this page showed this week
    # and only ever would.
    anchor = _anchor_date(request)
    week_start, week_end, week_label = _period(anchor, WEEK)

    week_appointments = appointments.filter(
        slot__date__range=(week_start, week_end)
    ).exclude(status__in=ENDED_STATUSES)
    week_slots = scope_slots(AvailableSlot.objects.all(), user).filter(
        date__range=(week_start, week_end)
    )

    week = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        published = week_slots.filter(date=day).count()
        booked = week_appointments.filter(slot__date=day).count()
        week.append({
            "date": day,
            "published": published,
            "booked": booked,
            # A day with nothing published is not "Available", which is what
            # the old strip said. That was a claim about availability made
            # while counting appointments, and false for exactly the day a
            # doctor would want to catch on a Monday morning.
            "nothing_published": published == 0,
            "fill": round(booked / published * 100) if published else 0,
            "is_today": day == today,
        })

    context = {
        'today_appointments': today_appointments,
        'today_count': len(today_appointments),
        # `DOCTOR_ACTION_STATUSES`, not `WAITING_STATUSES`. The difference is
        # `PENDING_MODE`, which is the patient choosing how to meet - the
        # sidebar bell has excluded it since plan 5.13, with a comment saying a
        # count that included it "would nag a doctor about somebody else's
        # inaction". The largest number on this page did exactly that.
        'needs_you_count': appointments.filter(
            status__in=DOCTOR_ACTION_STATUSES
        ).count(),
        'pending_count': appointments.filter(status__in=WAITING_STATUSES).count(),
        'week_count': week_appointments.count(),
        'total_patients': appointments.values('email').distinct().count(),
        # Renamed from `new_patients_week`, which counted distinct emails among
        # appointments **created** this week - so a patient of three years who
        # booked on Monday was "new". It never was a count of new patients; it
        # is a count of who booked, and now says so.
        'patients_booking_this_week': appointments.filter(
            created_at__date__range=(week_start, week_end)
        ).values('email').distinct().count(),
        'week': week,
        'week_label': week_label,
        'week_start': week_start,
        'prev_start': _step(anchor, WEEK, -1),
        'next_start': _step(anchor, WEEK, +1),
        'is_this_week': week_start <= today <= week_end,
        'today': today,
        'sees_every_doctor': sees_every_doctor(user),
        'Status': Appointment.Status,
        'ended_statuses': ENDED_STATUSES,
        # Named for the browser tab and the `<h1>` in the top bar, which read
        # the same string (doctor_base.html).
        'page_section': 'Overview',
        'page_title': 'Dashboard',
    }
    return render(request, 'doctorDashboard/doctor_admin_dashboard.html', context)


#: The tabs, in the order they appear. `needs_you` is first and is the
#: default: the question a doctor opens this page with is "what is waiting on
#: me", and the page used to answer "here is everything that has ever
#: happened, furthest away first".
# "Waiting on patient" was the fifth tab, holding `PENDING_MODE` - the
# patient choosing how to meet. The patient chooses when they book now (plan
# 12.1), so nothing can land in it: a tab that is always empty is a tab that
# teaches a doctor to stop looking at tabs.
LIST_VIEWS = ("needs_you", "upcoming", "past", "all")

LIST_LABELS = {
    "needs_you": "Needs you",
    "upcoming": "Upcoming",
    "past": "Past",
    "all": "All",
}

#: Over and done with, whichever way it ended.
FINISHED_STATUSES = ENDED_STATUSES + (Appointment.Status.COMPLETED,)

APPOINTMENTS_PER_PAGE = 25


def _slot_has_passed(now=None):
    """`Q` for "the slot's start is behind us".

    Two clauses because `date` and `time` are separate columns: a single
    comparison would need the database to recombine them, and doing it in
    Python would mean pulling every row. `AvailableSlot.starts_at` is the
    in-Python equivalent and is what the row-level code uses.
    """
    now = now or timezone.localtime()
    return Q(slot__date__lt=now.date()) | Q(
        slot__date=now.date(), slot__time__lt=now.time()
    )


def _list_filters(now=None):
    """What belongs in each tab.

    The split was **whose turn it is**, and since plan 12.1 the answer is
    always the doctor: the patient's only action is the booking form. What is
    left is *what kind of attention* an appointment wants - a decision, a
    diary entry, or nothing.

    A **confirmed appointment whose time has passed** is filed under "needs
    you", not "past": it is waiting to be marked completed or no-show, and
    that is a decision only the doctor can make. Filing it by the clock would
    file work away where nobody looks for it.

    Between them the four branches cover every status, so nothing can fall out
    of the list entirely — which is the failure a hand-written set of filters
    invites. That includes the two legacy statuses: they wait on the doctor
    like everything else, so they belong under "needs you".
    """
    passed = _slot_has_passed(now)
    needs_you = Q(status__in=DOCTOR_ACTION_STATUSES) | (
        Q(status=Appointment.Status.CONFIRMED) & passed
    )
    return {
        "needs_you": needs_you,
        "upcoming": Q(status=Appointment.Status.CONFIRMED) & ~passed,
        "past": Q(status__in=FINISHED_STATUSES),
        "all": Q(),
    }


def _row_actions(appointment, settings):
    """`(inline, on_the_page)` — what this row may offer, and where.

    Derived from `available_decisions`, so the list can never offer a move the
    state machine would refuse. Three things push a decision off the row and
    onto the detail page, and each is a rule that already exists:

    * **it needs a reason.** Declining a time or a mode carries a message to
      the patient. That is not a one-click action;
    * **it needs a meeting link.** `transition_to` refuses to confirm an
      `ONLINE` mode without one (plan 5.3), so an inline Confirm there would
      be a button that can only fail;
    * **the confirmation window has closed.** `appointment_decision` refuses
      it (plan 6.3), so the button is not offered rather than shown and then
      rejected — the distinction plan 4.9 draws about the cancel control.
    """
    inline, on_the_page = [], []
    too_late = confirmation_cutoff_passed(appointment, settings)
    has_happened = appointment.slot.starts_at < timezone.now()
    for action, decision in available_decisions(appointment):
        if decision.confirms and too_late:
            continue
        needs_link = (
            decision.confirms
            and appointment.appointmentMode == Appointment.Mode.ONLINE
        )
        # Recording an outcome for an appointment that has not happened yet is
        # legal - `transition_to` allows CONFIRMED -> COMPLETED whenever - and
        # it is not something to put one click away on a list. The detail page
        # still offers it, where the doctor is looking at the appointment
        # rather than scrolling past it.
        too_early = (
            action in ("mark_completed", "mark_no_show") and not has_happened
        )
        keep_on_page = decision.reason_prompt or needs_link or too_early
        (on_the_page if keep_on_page else inline).append((action, decision))
    return inline, on_the_page


@STAFF_CONSOLE
def available_appointments(request):
    """Every appointment, grouped by who is holding it up (plan 11.4).

    What this replaces was one flat table of every appointment ever made, with
    no search, no filter and no pagination anywhere in the app — ordered
    `-slot__date`, so a booking for December sat above one starting tomorrow.
    That descending order was half of an earlier fix: the ordering used to be
    by *creation* date, so a booking made this morning for next month sorted
    above one starting in an hour. Sorting by when it happens was right; the
    direction was never revisited.
    """
    user = request.user
    today = timezone.localdate()
    now = timezone.localtime()
    settings = AppointmentSettings.load()

    appointments = _appointments(user)

    query = request.GET.get("q", "").strip()
    if query:
        # Name or booking reference: the two things a doctor has in front of
        # them when somebody is on the phone.
        appointments = appointments.filter(
            Q(fullName__icontains=query) | Q(bookingID__icontains=query)
        )

    filters = _list_filters(now)
    show = request.GET.get("show", "needs_you")
    if show not in filters:
        show = "needs_you"

    # Counted against the searched set, so the tabs say where the matches are
    # rather than how big the system is.
    counts = {
        name: appointments.filter(rule).count() for name, rule in filters.items()
    }

    selected = appointments.filter(filters[show])
    # Soonest first, except for what is over: there, most recent first. Both
    # answer "what is nearest to now", which is the only ordering a working
    # list wants.
    if show == "past":
        selected = selected.order_by("-slot__date", "-slot__time")
    else:
        selected = selected.order_by("slot__date", "slot__time")

    page = Paginator(selected, APPOINTMENTS_PER_PAGE).get_page(request.GET.get("page"))
    for appointment in page.object_list:
        appointment.inline_actions, appointment.page_actions = _row_actions(
            appointment, settings
        )
        appointment.slot_has_passed = appointment.slot.starts_at < timezone.now()

    context = {
        'appointments': page.object_list,
        'page': page,
        'show': show,
        'query': query,
        'counts': counts,
        # Name, label and count together: a template cannot look a
        # dictionary up by a variable key without a custom filter, and adding
        # one to read a number the view already has would be a tag written to
        # work around the shape of its own context.
        'list_views': [
            (name, LIST_LABELS[name], counts[name]) for name in LIST_VIEWS
        ],
        'today_count': appointments.filter(slot__date=today).count(),
        # Was `total_patients` under a label reading "Total Appointments" -
        # that figure counts **distinct email addresses**, so a patient seen
        # four times counted once and the number was smaller than the number
        # of rows in the table below it. Both are provided now, each under its
        # own name.
        'total_appointments': appointments.count(),
        'total_patients': appointments.values('email').distinct().count(),
        'pending_count': appointments.filter(status__in=WAITING_STATUSES).count(),
        'needs_you_count': counts["needs_you"],
        'upcoming_count': counts["upcoming"],
        'has_any': _appointments(user).exists(),
        'sees_every_doctor': sees_every_doctor(user),
        'Status': Appointment.Status,
        'ended_statuses': ENDED_STATUSES,
        # Named for the browser tab and the `<h1>` in the top bar, which read
        # the same string (doctor_base.html).
        'page_section': 'Clinical',
        'page_title': (
            'Appointments, all doctors' if sees_every_doctor(user) else 'Appointments'
        ),
    }
    return render(request, 'doctorDashboard/available_appointments.html', context)


def _appointment_for_staff(request, appointment_id):
    """404 rather than the bare `.get()` that was here: an id belonging to
    another doctor raised DoesNotExist and returned a 500, which is both a
    worse answer and a noisier one than "no such appointment for you"."""
    return get_object_or_404(
        _appointments(request.user).prefetch_related("transitions__actor"),
        id=appointment_id,
    )


@STAFF_CONSOLE
def doctor_appointment_details(request, appointment_id):
    appointment = _appointment_for_staff(request, appointment_id)
    settings = AppointmentSettings.load()
    context = {
        'appointment': appointment,
        'appointment_notes': appointment.notes.order_by('-created_at'),
        'transitions': appointment.transitions.all(),
        'Status': Appointment.Status,
        'Mode': Appointment.Mode,
        'waiting_statuses': WAITING_STATUSES,
        'ended_statuses': ENDED_STATUSES,
        # Which of the two rounds, if either, is this doctor's to answer. The
        # page used to offer one "Approve Patient" checkbox for a flow with
        # two independent gates and four other endings.
        'decisions': available_decisions(appointment),
        'confirmation_problem': appointment.confirmation_problem(),
        'needs_meeting_url': appointment.appointmentMode == Appointment.Mode.ONLINE,
        'cutoff_passed': confirmation_cutoff_passed(appointment, settings),
        'cutoff_minutes': settings.confirmation_cutoff_minutes,
        # Named for the browser tab and the `<h1>` in the top bar, which read
        # the same string (doctor_base.html).
        'page_section': 'Clinical',
        'page_title': 'Appointment %s' % appointment.bookingID,
    }
    return render(request, 'doctorDashboard/doctor_appointment_details.html', context)


@STAFF_CONSOLE
@require_POST
def add_appointment_note(request, appointment_id):
    appointment = _appointment_for_staff(request, appointment_id)
    form = AppointmentNoteForm(request.POST)
    if form.is_valid():
        note = form.save(commit=False)
        note.appointment = appointment
        note.added_by = request.user
        note.save()
    else:
        messages.error(request, "A note cannot be empty.")
    return redirect('doctor_appointment_details', appointment_id=appointment.id)


# --- the two confirmation rounds (plan 5.1-5.4) ----------------------------
#
# One view, and a table of what each button means, for the same reason
# `Appointment.TRANSITIONS` is a table: six near-identical views would be six
# places to forget the cut-off check, the audit note or the scoping.

class Decision:
    #: Keyed on the **move**, not the destination. Two of the six below end at
    #: `PENDING_MODE` and mean opposite things — "your time is confirmed,
    #: choose how to meet" and "I cannot do it that way, choose again". Without
    #: `from_status` the console would offer "Confirm this time" on an
    #: appointment whose time was confirmed days ago, and taking it would send
    #: the patient the rejection copy. `Appointment.TRANSITION_EVENTS` is keyed
    #: the same way, for the same reason.
    def __init__(self, label, from_status, to_status, note, reason_prompt="", confirms=False):
        self.label = label
        self.from_status = from_status
        self.to_status = to_status
        self.note = note
        #: When set, the form offers a reason box and the text is passed to the
        #: patient. Declining a time or rejecting a mode without saying why
        #: leaves the patient guessing at what to do differently.
        self.reason_prompt = reason_prompt
        #: True for the moves that must satisfy the confirmation cut-off.
        self.confirms = confirms


#: One decision where there were two (AGKI, 2026-09-03). The patient chooses
#: the mode when they book, from the ones that slot offers, so the doctor sees
#: the time and the mode together and answers once.
#:
#: The legacy pair below exists for rows that were mid-negotiation when the
#: flow changed. Migration 0027 moves them to `PENDING_TIME`, so in a migrated
#: database these entries match nothing - they are here so that a row which
#: escaped it (a fixture, a restore from an old dump, a shell session) is still
#: answerable rather than stuck with no buttons at all.
DECISIONS = {
    "confirm": Decision(
        "Confirm this appointment",
        Appointment.Status.PENDING_TIME, Appointment.Status.CONFIRMED,
        "Doctor confirmed the appointment.", confirms=True,
    ),
    "decline_time": Decision(
        "Decline this time",
        Appointment.Status.PENDING_TIME, Appointment.Status.TIME_DECLINED,
        "Doctor declined the appointment.",
        reason_prompt="Why can you not take this appointment?",
    ),
    "confirm_legacy_mode": Decision(
        "Confirm this appointment",
        Appointment.Status.MODE_PROPOSED, Appointment.Status.CONFIRMED,
        "Doctor confirmed the appointment.", confirms=True,
    ),
    "decline_legacy_mode": Decision(
        "Decline this time",
        Appointment.Status.MODE_PROPOSED, Appointment.Status.TIME_DECLINED,
        "Doctor declined the appointment.",
        reason_prompt="Why can you not take this appointment?",
    ),
    "mark_completed": Decision(
        "Mark as completed",
        Appointment.Status.CONFIRMED, Appointment.Status.COMPLETED,
        "Doctor marked the appointment completed.",
    ),
    "mark_no_show": Decision(
        "Mark as no-show",
        Appointment.Status.CONFIRMED, Appointment.Status.NO_SHOW,
        "Doctor recorded that the patient did not attend.",
    ),
}


def available_decisions(appointment):
    """The buttons this appointment should actually offer right now.

    Derived from the state machine rather than hand-listed per template
    branch, so a button can never be shown for a move `transition_to` would
    refuse — the class of bug where a page offers Cancel on a cancelled
    appointment and 500s when you press it.
    """
    legal = Appointment.TRANSITIONS.get(appointment.status, set())
    return [
        (action, decision)
        for action, decision in DECISIONS.items()
        if decision.from_status == appointment.status and decision.to_status in legal
    ]


def confirmation_cutoff_passed(appointment, settings=None):
    """Is it too late for the doctor to confirm this (design §6.1)?

    Checked here as well as by the expiry job, per plan 6.3: a cron that has
    silently stopped is a classic failure, and without this the doctor would
    confirm an appointment that should already have been released — telling
    the patient it is on when the slot may have been re-offered.
    """
    settings = settings or AppointmentSettings.load()
    return timezone.now() > appointment.slot.starts_at - settings.confirmation_cutoff


CUTOFF_PASSED = (
    "It is too late to confirm this appointment — the confirmation window "
    "closed %(minutes)s minutes before the slot. The booking is being released."
)
DECISION_TAKEN = "Done. The patient has been told."
MODE_NOT_OFFERED = (
    "You have not offered that way of meeting on this time, so it cannot be "
    "confirmed that way. Change what this slot offers first, or decline."
)


@STAFF_CONSOLE
@require_POST
def appointment_decision(request, appointment_id):
    """Every dashboard action that moves an appointment's state.

    Never assigns `status`: it calls `transition_to`, which is what keeps
    "confirm" meaning the same thing here, in the expiry job and in anything
    built later (design §5).
    """
    appointment = _appointment_for_staff(request, appointment_id)
    action = request.POST.get("action", "")
    decision = DECISIONS.get(action)

    # Back to whichever page pressed the button. The appointments list offers
    # the decisions that need nothing typed (plan 11.4), and sending a doctor
    # to the detail page after each one would undo the point of putting them
    # on the row. Errors follow the same route, so the message is read where
    # the action was taken rather than on a page nobody asked for.
    destination = _safe_next(
        request, reverse('doctor_appointment_details', args=[appointment.id])
    )

    if decision is None:
        messages.error(request, "That is not something you can do to an appointment.")
        return redirect(destination)

    if decision.from_status != appointment.status:
        # `transition_to` would let this through where two actions share a
        # destination — "confirm the time" and "reject the mode" both end at
        # PENDING_MODE — and the patient would get the wrong message with the
        # wrong audit note attached. The move has to be the one the button
        # meant, not merely a legal one.
        messages.error(
            request,
            f"This appointment is {appointment.get_status_display().lower()}, "
            f"so that is no longer the decision in front of you.",
        )
        return redirect(destination)

    settings = AppointmentSettings.load()
    if decision.confirms and confirmation_cutoff_passed(appointment, settings):
        messages.error(
            request, CUTOFF_PASSED % {"minutes": settings.confirmation_cutoff_minutes}
        )
        return redirect(destination)

    reason = request.POST.get("reason", "").strip()[:255]
    mode_changed_from = ""

    if decision.confirms:
        # The doctor may confirm the appointment **as a different mode** than
        # the patient asked for (AGKI, 2026-09-03). It is the half of the
        # single-confirmation flow that keeps a booking alive: without it, a
        # patient asking a doctor to travel to them who cannot would have to be
        # declined outright over something a sentence settles.
        #
        # Restricted to what the slot offers, and to a real mode, because this
        # arrives in a POST: a doctor cannot be talked into agreeing to
        # something they never published by a hand-made request.
        chosen = request.POST.get("appointmentMode", "").strip()
        if chosen and chosen != appointment.appointmentMode:
            if not appointment.slot.offers(chosen):
                messages.error(request, MODE_NOT_OFFERED)
                return redirect(destination)
            mode_changed_from = appointment.get_appointmentMode_display()
            appointment.appointmentMode = chosen

        # Decision B. Set before the transition so `check_confirmable()` sees
        # it, and so the link reaches the patient in the confirmation email.
        #
        # There is deliberately **no** meeting-link check here. There was one,
        # and the mutation check showed it was dead code: deleting it changed
        # nothing, because `transition_to` refuses the move and the `except`
        # below shows the model's own message. Keeping a second copy of the
        # rule would mean two places to change it and only one of them tested.
        meeting_url = request.POST.get("meeting_url", "").strip()
        if meeting_url:
            appointment.meeting_url = meeting_url

    if decision.reason_prompt:
        appointment.cancellation_reason = reason

    note = decision.note + (f" Reason: {reason}" if reason else "")
    if mode_changed_from:
        # In the audit trail as well as the email: "confirmed" and "confirmed,
        # but not the way you asked" are different events to the patient, and
        # the record should say which happened.
        note += (
            f" Changed how they will meet: {mode_changed_from} \u2192 "
            f"{appointment.get_appointmentMode_display()}."
        )
    try:
        appointment.transition_to(
            decision.to_status,
            actor=request.user,
            note=note,
            reason=reason,
            # Only when the doctor changed it: the ordinary confirmation keeps
            # the mapped event, and the patient is told what they asked for
            # alongside what they got.
            event="appointment.confirmed_other_mode" if mode_changed_from else None,
            requested_mode=mode_changed_from,
        )
    except InvalidTransition as refused:
        # Somebody else answered it first, or the expiry job did.
        messages.error(request, str(refused))
    else:
        messages.success(request, DECISION_TAKEN)
    return redirect(destination)


# --- availability (plan 5.5, 5.6) ------------------------------------------

WEEK = "week"
MONTH = "month"


def _anchor_date(request):
    raw = request.GET.get("start", "")
    try:
        return date_cls.fromisoformat(raw)
    except ValueError:
        return timezone.localdate()


def _period(anchor, mode):
    """First and last day shown, and the label above the navigation."""
    if mode == MONTH:
        start = anchor.replace(day=1)
        end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
        return start, end, start.strftime("%B %Y")
    start = anchor - timedelta(days=anchor.weekday())
    end = start + timedelta(days=6)
    return start, end, f"{start:%d %b} - {end:%d %b %Y}"


def _step(anchor, mode, direction):
    """The anchor date one period earlier or later."""
    if mode == MONTH:
        first = anchor.replace(day=1)
        if direction < 0:
            return (first - timedelta(days=1)).replace(day=1)
        return (first.replace(day=calendar.monthrange(first.year, first.month)[1])
                + timedelta(days=1))
    return anchor + timedelta(days=7 * direction)


@STAFF_CONSOLE
def doctor_availability(request):
    """A real timetable, navigable by week or month (decision 5).

    The prev/next/Today buttons were `onclick` handlers calling functions that
    did not exist, and the page always rendered the current week whatever you
    pressed. They are ordinary links now, so the browser's back button works
    and a week can be bookmarked or sent to somebody.

    AGKI asked for this horizon because booking is unrealistic without it, and
    plan 5.7 depends on it: `doctor_schedule.html` is only safe to delete once
    this page really covers a week *and* a month.
    """
    user = request.user
    mode = MONTH if request.GET.get("view") == MONTH else WEEK
    anchor = _anchor_date(request)
    start, end, label = _period(anchor, mode)
    today = timezone.localdate()
    booking_settings = AppointmentSettings.load()
    earliest_bookable = booking_settings.earliest_bookable_start()

    slots = list(
        scope_slots(AvailableSlot.objects.select_related("user"), user)
        .filter(date__range=(start, end))
        .order_by("date", "time")
    )
    # **Any** appointment row, whatever its status — not just the live ones.
    # This drives whether the Delete control is offered, and `PROTECT` refuses
    # the delete for a cancelled appointment exactly as firmly as for a live
    # one: the patient's record still points at the slot. Filtering to active
    # statuses here (which is what it did first) offered a button that could
    # only ever produce an error.
    # Keyed by slot, and carrying the row rather than only its existence: the
    # redesigned page links a booked slot to the appointment behind it, which
    # was previously a dead end reading "Booked" and stopping. `slot` is a
    # OneToOneField since plan 2.9, so there is at most one per slot and this
    # dict cannot lose anything.
    appointment_by_slot = {
        appointment.slot_id: appointment
        for appointment in Appointment.objects.filter(slot__in=slots)
    }

    by_date = {}
    for slot in slots:
        # The doctor's own page still shows slots inside the booking notice —
        # they are real, published times that simply can no longer be booked,
        # and hiding them here would look like data loss (design §6.1).
        slot.is_bookable_now = (
            not slot.is_booked and slot.starts_at >= earliest_bookable
        )
        # Deliberately **not** `slot.appointment`: that name belongs to the
        # reverse side of the OneToOneField, whose descriptor raises
        # `RelatedObjectDoesNotExist` on read rather than returning None -
        # so assigning None to it and reading it back is an exception, not a
        # falsy value. Caught immediately by the existing tests.
        slot.linked_appointment = appointment_by_slot.get(slot.id)
        slot.has_appointment = slot.linked_appointment is not None
        by_date.setdefault(slot.date, []).append(slot)

    # A calendar grid padded to whole Monday-Sunday weeks, so a month renders
    # as a month rather than as a ragged list.
    grid_start = start - timedelta(days=start.weekday())
    grid_end = end + timedelta(days=6 - end.weekday())
    weeks, row = [], []
    day = grid_start
    while day <= grid_end:
        day_slots = by_date.get(day, [])
        row.append({
            "date": day,
            "slots": day_slots,
            "is_today": day == today,
            "in_period": start <= day <= end,
            # Counts as well as the slots themselves, because the month view
            # renders numbers where the week view renders chips. A month cell
            # is a third the height of a week cell and seven chips do not fit
            # in it; truncating to "+3 more" makes a month impossible to scan.
            "free_count": sum(1 for s in day_slots if s.is_bookable_now),
            "booked_count": sum(1 for s in day_slots if s.is_booked),
            "late_count": sum(
                1 for s in day_slots if not s.is_booked and not s.is_bookable_now
            ),
            "is_past": day < today,
        })
        if len(row) == 7:
            weeks.append(row)
            row = []
        day += timedelta(days=1)

    total = len(slots)
    booked = sum(1 for slot in slots if slot.is_booked)
    bookable = sum(1 for slot in slots if slot.is_bookable_now)
    context = {
        'form': AvailableSlotForm(user=user),
        'view_mode': mode,
        'period_label': label,
        'period_start': start,
        'period_end': end,
        'weeks': weeks,
        'slots_by_date': sorted(by_date.items()),
        'today': today,
        'prev_start': _step(anchor, mode, -1),
        'next_start': _step(anchor, mode, +1),
        'is_current_period': start <= today <= end,
        'bulk_form': BulkAvailabilityForm(user=user),
        # Rendered as a <select> on both forms rather than an <input
        # type="time">, whose display format follows the browser's locale and
        # so could not be relied on to be one thing or the other.
        'time_choices': TIME_CHOICES,
        'stats': {
            'total_slots': total,
            'available_slots': total - booked,
            'booked_slots': booked,
            # What the redesign shows instead of a utilisation percentage: a
            # count of published times that can no longer be booked. A ratio
            # tells a doctor nothing they can act on; this number is one they
            # can clear.
            'bookable_slots': bookable,
            'too_late_slots': total - booked - bookable,
            'utilization_rate': round(booked / total * 100) if total else 0,
        },
        'sees_every_doctor': sees_every_doctor(user),
        # Named for the browser tab and the `<h1>` in the top bar, which read
        # the same string (doctor_base.html).
        'page_section': 'Clinical',
        'page_title': (
            'Availability, all doctors' if sees_every_doctor(user) else 'Availability'
        ),
    }
    return render(request, 'doctorDashboard/doctor_availability.html', context)


@STAFF_CONSOLE
@require_POST
def add_doctor_availability(request):
    form = AvailableSlotForm(request.POST, user=request.user)
    if form.is_valid():
        slot = form.save(commit=False)
        slot.user = request.user
        slot.save()
        messages.success(request, "Availability added.")
    else:
        # The form refuses a past date and a duplicate (design §5.2). Both
        # used to be accepted: the first produced a slot a patient could book
        # through the URL, the second an IntegrityError from the view.
        for error in form.errors.values():
            messages.error(request, " ".join(error))
    return redirect(_safe_next(request, 'doctor_availability'))


@STAFF_CONSOLE
@require_POST
def publish_availability(request):
    """Publish a pattern of times across a range of days (plan 11.1).

    A separate endpoint from `add_doctor_availability` rather than a mode of
    it. They answer different questions - "add this time" and "add these times
    for the next month" - and they disagree about the past on purpose: the
    single-slot form **refuses** a past date, because publishing one is a
    mistake, while this one **skips** past times, because a range starting
    today legitimately contains this morning and refusing the whole publish
    over 09:00 would be obstructive.

    The form does the arithmetic and the creating; this view only reports. It
    says how many were skipped as well as how many were created, because
    "published 0" on a re-run is alarming and "already there" is not.
    """
    form = BulkAvailabilityForm(request.POST, user=request.user)
    if form.is_valid():
        created, skipped = form.save_for(request.user)
        if created and skipped:
            messages.success(
                request,
                f"Published {created} time{'' if created == 1 else 's'}. "
                f"{skipped} {'was' if skipped == 1 else 'were'} already there.",
            )
        elif created:
            messages.success(
                request,
                f"Published {created} time{'' if created == 1 else 's'}.",
            )
        else:
            messages.info(request, "Those times were already published.")
    else:
        for error in form.errors.values():
            messages.error(request, " ".join(error))
    return redirect(_safe_next(request, 'doctor_availability'))


SLOT_HAS_APPOINTMENT = (
    "That time is booked. Cancel or decline the appointment first — deleting "
    "the slot would take the patient's record with it."
)


@STAFF_CONSOLE
@require_POST
def delete_availability(request, slot_id):
    """The endpoint the Delete Slot button has always called and that has
    never existed (design §10 dashboard #5) — the button 404'd. Worse, it
    called `/delete-availability/<id>/` at the **root** of the site, which is
    not where this app is mounted.

    Refusing a slot that has an appointment is `on_delete=PROTECT` doing its
    job (plan 2.9), not an `if` above it. There *was* an `if` above it; the
    mutation check showed that deleting it changed nothing, because PROTECT
    covers every appointment row pointing at the slot — including a cancelled
    one, whose `is_booked` is false but whose record would still be destroyed.
    So the constraint is the check, and this turns its exception into a
    sentence a doctor can act on.
    """
    slot = get_object_or_404(scope_slots(AvailableSlot.objects.all(), request.user), id=slot_id)
    try:
        slot.delete()
    except ProtectedError:
        messages.error(request, SLOT_HAS_APPOINTMENT)
    else:
        messages.success(request, "Availability removed.")
    return redirect(_safe_next(request, 'doctor_availability'))


# --- staff administration (plan 5.10-5.12) ---------------------------------

@ADMIN_ONLY
def doctors(request):
    """The staff list. Was `CustomUser.objects.all()` under a heading reading
    "Registered Doctors" — every patient-less account, every Content Editor and
    both developer superusers, listed as doctors."""
    staff = CustomUser.objects.filter(
        role__in=(CustomUser.Role.DOCTOR, CustomUser.Role.ADMIN)
    ).order_by("first_name", "last_name", "username")

    context = {
        'doctors': staff,
        'doctors_count': staff.count(),
        # The three stat cards were bound to `today_comparison`, `pending_count`
        # and `total_patients` — none of which this view has ever provided, so
        # all three rendered blank. These are the numbers the page is about.
        'active_count': staff.filter(is_active=True).count(),
        # Not "awaiting approval" any more (plan 10.5). Nothing is waiting:
        # decision F grants approval at registration, so a False here is an
        # administrator having switched the account off. The old label was the
        # more dangerous kind of wrong — 10.2 makes this render 0 on every
        # existing install, so it would have looked correct while counting the
        # opposite of what it said.
        'withdrawn_count': staff.filter(is_approved=False).count(),
        'roles': CustomUser.Role.choices,
        # Named for the browser tab and the `<h1>` in the top bar, which read
        # the same string (doctor_base.html).
        'page_section': 'Administration',
        'page_title': 'Staff',
    }
    return render(request, 'doctorDashboard/doctors.html', context)


ACCOUNT_FLAGS = {
    # Two independent switches, deliberately not one button. The old control
    # showed `is_active` and toggled `is_approved` (design §10 dashboard #4):
    # the label said one thing and the POST did another, and one of the two
    # fields was unreachable from the UI entirely.
    "approved": ("is_approved", "Approved for login"),
    "active": ("is_active", "Account active"),
}


@ADMIN_ONLY
@require_POST
def set_doctor_status(request, doctor_id):
    account = get_object_or_404(CustomUser, id=doctor_id)
    flag = ACCOUNT_FLAGS.get(request.POST.get("flag", ""))
    if flag is None:
        messages.error(request, "Unknown account setting.")
        return redirect('doctors')

    field, label = flag
    value = request.POST.get("value") == "on"

    if account == request.user and not value:
        # Turning either flag off on your own account signs you out and, for
        # `is_approved`, leaves you unable to sign back in (plan 1.10).
        messages.error(request, "You cannot switch off your own account.")
        return redirect('doctors')

    setattr(account, field, value)
    account.save(update_fields=[field])
    messages.success(
        request,
        f"{label} is now {'on' if value else 'off'} for "
        f"{account.get_full_name() or account.username}.",
    )
    return redirect('doctors')


ADD_DOCTOR_REQUIRED = "First name, last name, username, email and both passwords are required."
ADD_DOCTOR_MISMATCH = "The two passwords do not match."


@ADMIN_ONLY
@require_POST
def add_doctor(request):
    """Create a doctor's account from the console (plan 5.12).

    **The modal posted to the public `signup` view and could not have worked.**
    That view reads `request.POST['phone']` and `request.POST['role']` with
    bare subscripts; the modal sends neither, so every submission was a
    `MultiValueDictKeyError` — an unconditional 500. It also redirects to the
    sign-in page on success, which is the wrong place to land an administrator
    who is already signed in.

    So this is a separate endpoint rather than a corrected form action. The
    account is created **able to sign in**. That inverts what this docstring
    said at 5.12 — "an administrator creating an account does not prove the
    person behind the address exists" — and the reasoning is relocated rather
    than abandoned: it is design §4.5's open problem now, and it applies to
    every registration route rather than to this one. Under decision F a
    stranger self-registering is admitted on the spot, so leaving the
    administrator's own button as the stricter path would be backwards.
    Access is taken away again from this same page's "Approved for login"
    toggle, which is what `is_approved` means now.
    """
    fields = {
        name: request.POST.get(name, "").strip()
        for name in ("firstname", "lastname", "username", "email", "phone")
    }
    password1 = request.POST.get("password1", "")
    password2 = request.POST.get("password2", "")

    required = (fields["firstname"], fields["lastname"], fields["username"],
                fields["email"], password1, password2)
    if not all(required):
        messages.error(request, ADD_DOCTOR_REQUIRED)
    elif password1 != password2:
        messages.error(request, ADD_DOCTOR_MISMATCH)
    elif CustomUser.objects.filter(email__iexact=fields["email"]).exists():
        messages.error(request, "An account with that email already exists.")
    elif CustomUser.objects.filter(username__iexact=fields["username"]).exists():
        messages.error(request, "That username is already taken.")
    else:
        account = CustomUser.objects.create_user(
            username=fields["username"], email=fields["email"], password=password1,
        )
        account.first_name = fields["firstname"]
        account.last_name = fields["lastname"]
        account.phone = fields["phone"] or None
        account.role = CustomUser.Role.DOCTOR
        # `create_user` with a usable password is treated as a public
        # self-signup by `useraccount.signals`, which deactivates the account
        # and emails a one-time code. That is right for the public form and
        # wrong here, so it is undone the same way `CustomUserAdmin` does.
        #
        # `is_approved` needs no line at all now: the model default grants it
        # (plan 10.1). Re-stating it here would be a fourth place to keep in
        # step with a decision that belongs on the field.
        account.is_active = True
        account.save()
        messages.success(
            request,
            f"{account.get_full_name()} added, and able to sign in now.",
        )
    return redirect('doctors')


@superuser_required
def ecommerce_items(request):
    """Read-only catalogue view, superuser-only (design §4.4).

    Ecommerce is frozen pending its own redesign, and this is the only window
    onto it from the staff console. Nothing here mutates a product: the delete
    endpoint that used to live below this function is gone, and anything that
    changes a product happens in the Django admin, which has a confirmation
    step, object permissions and a LogEntry trail.
    """
    items = Item.objects.all()
    categories = Category.objects.all()

    context = {
        'items': items,
        'categories': categories,
        'available_count': items.filter(available=True).count(),
        'featured_count': items.filter(is_featured=True).count(),
        # Named for the browser tab and the `<h1>` in the top bar, which read
        # the same string (doctor_base.html).
        'page_section': 'Administration',
        'page_title': 'Products',
    }
    return render(request, 'doctorDashboard/ecommerce_items.html', context)


# `delete_item` was deleted here, not ported (design §4.4, plan 1.4).
#
# It destroyed an `ecommerce.Item` on a plain **GET** with no authentication
# and no CSRF token — reproduced against a test database, product count 1 -> 0
# from a single unauthenticated URL fetch. Adding @login_required would not
# have been enough: a destructive GET stays CSRF-triggerable by any signed-in
# staff member who follows a crafted link, because no token is involved.
#
# Product deletion now happens in the Django admin. `NoDeleteItemRouteTests`
# asserts the name no longer reverses, so it cannot quietly come back.
#
# Three more views were removed in plan 5.7-5.9, for less alarming reasons:
#
# * `doctor_schedule` rendered a page of hardcoded fake data — invented
#   patients, invented times — from a view that passed no context at all.
#   Deleted once `doctor_availability` gained the real week/month timetable it
#   was pretending to be (decision 5 made that the condition).
# * `doctor_base` rendered the *base template* as a standalone page and
#   returned 200 to anyone who asked (design §10 dashboard #19). A base
#   template is not a page; it rendered with no content block and no context.
# * `appointment_approval` wrote the superseded `AppointmentApproval` row.
#   Its replacement is `appointment_decision` above, which runs both rounds
#   through the state machine.

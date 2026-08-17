from datetime import datetime

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from appointments.forms import AppointmentForm
from useraccount.models import CustomUser, Profile
from .access import grant_appointment_access, has_appointment_access
# `Rating` is deliberately not imported: its three read sites are gone
# (decision 7, design §5.4). The model stays, reshaped, for when feedback is
# built — reading an empty table was what produced the permanent "No ratings
# yet" the old pages showed as though it were a measurement.
from .models import Appointment, AppointmentApproval, AppointmentNote, AvailableSlot
from .ratelimit import lookup_is_rate_limited

# One message for every way a lookup can fail — wrong booking ID, wrong
# verification value, or an appointment that does not exist. Saying which part
# was wrong would confirm that a booking ID is real, which is the one thing an
# attacker with a list of guesses needs to know (design §8.1).
LOOKUP_FAILED = "We couldn't find an appointment matching those details. Please check and try again."
LOOKUP_THROTTLED = "Too many attempts. Please wait a few minutes and try again."
NEEDS_VERIFICATION = "Please confirm your details to view your appointment."

# Create your views here.
def appointment_home(request):
    return render(request, 'appointments/appointment_home.html')

from datetime import date, timedelta
from collections import defaultdict
from datetime import datetime, timedelta
def search_results(request):
    doctors = Profile.objects.filter(user__role=CustomUser.Role.DOCTOR).select_related('user')
    
    # Get the next 5 days starting from today
    today = datetime.now().date()
    date_range = [today + timedelta(days=i) for i in range(5)]
    
    # Prepare doctor data with their available slots
    doctors_data = []
    for doctor in doctors:
        # Get all available slots for this doctor for the next 5 days
        slots = AvailableSlot.objects.filter(
            user=doctor.user,
            date__in=date_range
        ).order_by('date', 'time')
        
        # Group slots by date
        slots_by_date = defaultdict(list)
        for slot in slots:
            slots_by_date[slot.date].append(slot)
        
        doctors_data.append({
            'doctor': doctor,
            'slots_by_date': dict(slots_by_date)
        })

    # Ratings are not computed here any more (decision 7, design §5.4).
    # Nothing has ever written a `Rating`, so every average this produced was
    # a fixed "No ratings yet" dressed up as a measurement. The model is kept
    # and reshaped for when feedback is actually built; until then, showing
    # nothing is more honest than showing a placeholder that looks like data.

    context = {
        'doctors_data': doctors_data,
        'date_range': date_range,
        'today': today,
    }
    
    return render(request, 'appointments/search_results.html', context)

def booking_form(request, id):
    # Get the selected slot
    slot = get_object_or_404(AvailableSlot, id=id)

    # Get the doctor's profile. A missing Profile must not 404 the booking
    # page: profiles are created lazily, so three of the doctors in the
    # current database have none (design §5.3, and see plan 1.1's note). The
    # signal and backfill that stop this happening are plan 2.13; this is the
    # guard that stops it being a crash in the meantime.
    doctor = Profile.objects.filter(user=slot.user).first()

    form = AppointmentForm()
    if request.method == 'POST':
        form = AppointmentForm(request.POST)
        if form.is_valid():
            pre = form.save(commit=False)
            pre.slot = slot
            pre.save()
            # The patient who just filled this in is the one person who
            # provably owns this record, so this is where the session marker
            # is granted (design §8.1). The success page checks it like any
            # other page; it does not hand out access of its own, which is
            # what the old `/booking-success/<pk>/` effectively did.
            grant_appointment_access(request, pre)
            return redirect('booking_success_page', booking_id=pre.bookingID)
    else:
        form = AppointmentForm()
    
    context = {
        'slot': slot,
        'doctor': doctor,
        'form':form,
    }

    return render(request, 'appointments/booking_form.html', context)

def _appointment_for_reader(request, booking_id):
    """Return the appointment this session is allowed to read, or None.

    One helper for both patient-facing record pages, so they cannot drift
    apart — and one uniform answer for "no such booking" and "not yours".
    Distinguishing them would let an attacker use the response itself to
    confirm which booking IDs are real (design §8.1).
    """
    appointment = Appointment.objects.filter(bookingID=booking_id).first()
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


def booking_id_lookup(request):
    """Trace an appointment: booking ID **plus** one verification field.

    The booking ID alone was the whole gate before, and it is not a secret —
    `AGKI-<year>-<5 digits>` is 100,000 possibilities a year, enumerable in an
    afternoon. §8.1 requires a second field; §8.2 requires that guessing at it
    be slow.
    """
    context = {}
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
    appointment = _appointment_for_reader(request, booking_id)
    if appointment is None:
        messages.info(request, NEEDS_VERIFICATION)
        return redirect('booking_id_lookup')

    profile = Profile.objects.filter(user=appointment.slot.user).first()
    appointment_approval = AppointmentApproval.objects.filter(appointment=appointment).first()
    appointment_notes = AppointmentNote.objects.filter(appointment=appointment).order_by('-created_at')
    context = {
        'appointment': appointment,
        'profile': profile,
        'appointment_approval': appointment_approval,
        'appointment_notes': appointment_notes,
    }
    return render(request, 'appointments/appointment_detail.html', context)


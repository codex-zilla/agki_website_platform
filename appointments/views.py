from django.shortcuts import get_object_or_404, redirect, render
from appointments.forms import AppointmentForm
from useraccount.models import CustomUser, Profile
from .models import Appointment, AppointmentApproval, AppointmentNote, AvailableSlot, Rating

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

    # Doctor rating and recommended percentage calculation
    for doctor_data in doctors_data:
        ratings = Rating.objects.filter(doctor=doctor_data['doctor'].user)
        rating_count = ratings.count()

        if rating_count > 0:
            # Calculate average rating
            total_rating = sum(r.rating_value for r in ratings)
            average_rating = total_rating / rating_count

            # Calculate recommendation percentage
            recommended_count = ratings.filter(recommended=True).count()
            recommended_percentage = (recommended_count / rating_count) * 100

            doctor_data['rating'] = {
                'average_rating': round(average_rating, 1),
                'total_reviews': rating_count,
                'recommended_percentage': round(recommended_percentage, 1)
            }
        else:
            doctor_data['rating'] = {
                'average_rating': 'No ratings yet',
                'total_reviews': 0,
                'recommended_percentage': 0
            }

    
    context = {
        'doctors_data': doctors_data,
        'date_range': date_range,
        'today': today,
    }
    
    return render(request, 'appointments/search_results.html', context)

def booking_form(request, id):
    # Get the selected slot
    slot = get_object_or_404(AvailableSlot, id=id)
    rating = Rating.objects.filter(doctor=slot.user)
    rating_count = rating.count()
    if rating.exists():
        average_rating = sum(r.rating_value for r in rating) / rating_count
        rating = {
            'average_rating': round(average_rating, 1),
            'total_reviews': rating.count()
        }
    else:
        rating = {
            'average_rating': 'No ratings yet',
            'total_reviews': 0
        }
    
    # Get the doctor's profile
    doctor = get_object_or_404(Profile, user=slot.user)

    form = AppointmentForm()
    if request.method == 'POST':
        form = AppointmentForm(request.POST)
        if form.is_valid():
            pre = form.save(commit=False)
            pre.slot = slot
            pre.save()
            return redirect('booking_success_page', appointment_id=pre.id)
    else:
        form = AppointmentForm()
    
    context = {
        'slot': slot,
        'doctor': doctor,
        'form':form,
        'rating': rating,
        'rating_count': rating_count,
    }
    
    return render(request, 'appointments/booking_form.html', context)

def booking_success_page(request, appointment_id):
    appointment = get_object_or_404(Appointment, id=appointment_id)
    profile = Profile.objects.get(user=appointment.slot.user)
    context = {
        'appointment': appointment,
        'profile': profile,
    }
    return render(request, 'appointments/booking_success_page.html', context)

def booking_id_lookup(request):
    if request.method == 'POST':
        booking_id = request.POST.get('bookingID', '').strip()

        # Check if the booking ID exists
        try:
            appointment = Appointment.objects.get(bookingID=booking_id)
            # Redirect to detail page
            return redirect('appointment_detail', appointment_id=appointment.id)
        except Appointment.DoesNotExist:
            # Return with an error message
            return render(request, 'appointments/booking_id_lookup.html', {
                'error': 'Booking ID not found. Please check and try again.'
            })

    return render(request, 'appointments/booking_id_lookup.html')


def appointment_detail(request, appointment_id):
    appointment = get_object_or_404(Appointment, id=appointment_id)
    profile = Profile.objects.get(user=appointment.slot.user)
    appointment_approval = AppointmentApproval.objects.filter(appointment=appointment).first()
    appointment_notes = AppointmentNote.objects.filter(appointment=appointment).order_by('-created_at')
    rating = Rating.objects.filter(doctor=appointment.slot.user)
    rating_count = rating.count()
    if rating.exists():
        average_rating = sum(r.rating_value for r in rating) / rating_count
        rating = {
            'average_rating': round(average_rating, 1),
            'total_reviews': rating.count()
        }
    else:
        rating = {
            'average_rating': 'No ratings yet',
            'total_reviews': 0
        }
    context = {
        'appointment': appointment,
        'profile': profile,
        'appointment_approval': appointment_approval,
        'appointment_notes': appointment_notes,
        'rating': rating,
        'rating_count': rating_count,
    }
    return render(request, 'appointments/appointment_detail.html', context)


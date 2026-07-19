from django.shortcuts import get_object_or_404, redirect, render
from appointments.forms import AppointmentApprovalForm, AppointmentNoteForm, AvailableSlotForm
from appointments.models import *
from django.utils import timezone
from datetime import timedelta
from django.contrib import messages

from ecommerce.models import Category, Item

# Create your views here.
def doctor_base(request):
    return render(request, 'doctorDashboard/doctor_base.html')

def doctor_admin_dashboard(request):
    doctor = request.user
    today = timezone.now().date()
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())  # Monday of this week
    week_end = week_start + timedelta(days=6)  # Sunday

    # --- Existing Logic (KEEPED) ---
    today_appointments = Appointment.objects.filter(
        slot__user=doctor,
        slot__date=today
    )
    today_count = today_appointments.count()

    yesterday_count = Appointment.objects.filter(
        slot__user=doctor,
        slot__date=yesterday
    ).count()

    today_comparison = today_count - yesterday_count

    total_patients = Appointment.objects.filter(slot__user=doctor).values('email').distinct().count()
    new_patients_week = Appointment.objects.filter(
        slot__user=doctor,
        created_at__date__gte=week_start
    ).values('email').distinct().count()

    total_comparison = new_patients_week

    pending_count = AppointmentApproval.objects.filter(
        appointment__slot__user=doctor,
        is_approved=False
    ).count()

    # --- 🆕 Weekly Calendar Logic ---
    weekly_appointments = Appointment.objects.filter(
        slot__user=doctor,
        slot__date__range=(week_start, week_end)
    )

    weekly_data = []
    for i in range(7):
        day = week_start + timedelta(days=i)
        count = weekly_appointments.filter(slot__date=day).count()
        weekly_data.append({
            'day_name': day.strftime('%a'),  # Mon, Tue, etc.
            'day_number': day.day,
            'count': count,
            'is_today': (day == today),
        })

    # --- Combine All Context ---
    context = {
        # old data (still usable elsewhere)
        'today_appointments': today_appointments,
        'today_count': today_count,
        'today_comparison': today_comparison,
        'total_patients': total_patients,
        'new_patients_week': new_patients_week,
        'total_comparison': total_comparison,
        'pending_count': pending_count,

        # new data for calendar
        'weekly_data': weekly_data,
        'current_month': today.strftime('%B %Y'),
    }

    return render(request, 'doctorDashboard/doctor_admin_dashboard.html', context)

def  doctor_schedule(request):
    return render(request, 'doctorDashboard/doctor_schedule.html')

def  available_appointments(request):
    doctor = request.user
    today = timezone.now().date()
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())  # Monday of this week
    week_end = week_start + timedelta(days=6)  # Sunday

    # --- Existing Logic (KEEPED) ---
    all_appointments = Appointment.objects.filter(slot__user=doctor).order_by('-created_at')
    today_appointments = Appointment.objects.filter(
        slot__user=doctor,
        slot__date=today
    )
    today_count = today_appointments.count()

    yesterday_count = Appointment.objects.filter(
        slot__user=doctor,
        slot__date=yesterday
    ).count()

    today_comparison = today_count - yesterday_count

    total_patients = Appointment.objects.filter(slot__user=doctor).values('email').distinct().count()
    new_patients_week = Appointment.objects.filter(
        slot__user=doctor,
        created_at__date__gte=week_start
    ).values('email').distinct().count()

    total_comparison = new_patients_week

    pending_count = AppointmentApproval.objects.filter(
        appointment__slot__user=doctor,
        is_approved=False
    ).count()


    # --- Combine All Context ---
    context = {
        # old data (still usable elsewhere)
        'all_appointments': all_appointments,
        'today_appointments': today_appointments,
        'today_count': today_count,
        'today_comparison': today_comparison,
        'total_patients': total_patients,
        'new_patients_week': new_patients_week,
        'total_comparison': total_comparison,
        'pending_count': pending_count,

    }
    return render(request, 'doctorDashboard/available_appointments.html', context)

def doctor_appointment_details(request, appointment_id):
    doctor = request.user
    appointment = Appointment.objects.get(id=appointment_id, slot__user=doctor)
    approval_status = AppointmentApproval.objects.filter(appointment=appointment).first()
    appointment_notes = AppointmentNote.objects.filter(appointment=appointment).order_by('-id')
    context = {
        'appointment': appointment,
        'approval_status':approval_status,
        'appointment_notes':appointment_notes,
    }
    return render(request, 'doctorDashboard/doctor_appointment_details.html', context)

def add_appointment_note(request, appointment_id):
    doctor = request.user
    appointment = get_object_or_404(Appointment, id=appointment_id, slot__user=doctor)

    if request.method == 'POST':
        form = AppointmentNoteForm(request.POST)
        if form.is_valid():
            note = form.save(commit=False)
            note.appointment = appointment
            note.added_by = request.user
            note.save()
            return redirect('doctor_appointment_details', appointment_id=appointment.id)
    else:
        form = AppointmentNoteForm()

    context = {
        'form': form,
        'appointment': appointment
    }
    return render(request, 'doctorDashboard/doctor_appointment_details.html', context)

def appointment_approval(request, appointment_id):
    doctor = request.user
    appointment = get_object_or_404(Appointment, id=appointment_id, slot__user=doctor)
    
    # Get existing approval if exists
    approval, created = AppointmentApproval.objects.get_or_create(
        appointment=appointment,
        defaults={'approved_by': doctor}
    )
    
    if request.method == 'POST':
        form = AppointmentApprovalForm(request.POST, instance=approval)  # <-- use instance
        if form.is_valid():
            note = form.save(commit=False)
            note.approved_by = doctor  # ensure approved_by is set
            note.save()
            return redirect('doctor_appointment_details', appointment_id=appointment.id)
    else:
        form = AppointmentApprovalForm(instance=approval)  # <-- prefill existing data

    context = {
        'form': form,
        'appointment': appointment
    }
    return render(request, 'doctorDashboard/doctor_appointment_details.html', context)

def doctor_availability(request):
    doctor = request.user
    # Get all available slots for the doctor, not just one
    available_slots = AvailableSlot.objects.filter(user=doctor)
    
    # Calculate date range for current week (Monday to Sunday)
    today = timezone.now().date()
    start_of_week = today - timedelta(days=today.weekday())  # Monday
    end_of_week = start_of_week + timedelta(days=6)  # Sunday
    
    # Get slots for current week
    current_week_slots = available_slots.filter(
        date__range=[start_of_week, end_of_week]
    ).order_by('date', 'time')
    
    # Calculate statistics
    total_slots_week = current_week_slots.count()
    available_slots_count = current_week_slots.filter(is_booked=False).count()
    booked_slots_count = current_week_slots.filter(is_booked=True).count()
    utilization_rate = (booked_slots_count / total_slots_week * 100) if total_slots_week > 0 else 0
    
    # Organize slots by day for template
    slots_by_day = {}
    for slot in current_week_slots:
        day_name = slot.date.strftime('%A')
        if day_name not in slots_by_day:
            slots_by_day[day_name] = []
        slots_by_day[day_name].append(slot)
    
    context = {
        'available_slots': current_week_slots,
        'slots_by_day': slots_by_day,
        'start_of_week': start_of_week,
        'end_of_week': end_of_week,
        'today': today,
        'stats': {
            'total_slots': total_slots_week,
            'available_slots': available_slots_count,
            'booked_slots': booked_slots_count,
            'utilization_rate': round(utilization_rate),
        }
    }
    return render(request, 'doctorDashboard/doctor_availability.html', context)

def add_doctor_availability(request):
    form = AvailableSlotForm()
    if request.method == 'POST':
        form = AvailableSlotForm(request.POST)
        if form.is_valid():
            slot = form.save(commit=False)
            slot.user = request.user
            slot.save()
            return redirect('doctor_availability')
    context = {
        'form':form,
    }
    return render(request, 'doctorDashboard/doctor_availability.html', context)

def doctors(request):
    doctors = CustomUser.objects.all()
    doctors_count = doctors.count()
    context = {
        'doctors': doctors,
        'doctors_count': doctors_count,
    }
    return render(request, 'doctorDashboard/doctors.html', context)

def activate_deactivate_doctor(request, doctor_id):
    doctor = get_object_or_404(CustomUser, id=doctor_id)
    if request.method == 'POST':
        # Toggle the is_active status
        doctor.is_approved = not doctor.is_approved
        doctor.save()
        return redirect('doctors')
    context = {
        'doctor': doctor,
    }
    return render(request, 'doctorDashboard/doctors.html', context)

def ecommerce_items(request):
    items = Item.objects.all()
    categories = Category.objects.all()  # Add this line
    
    context = {
        'items': items,
        'categories': categories,  # Add this
        'available_count': items.filter(available=True).count(),  # Add these counts
        'featured_count': items.filter(is_featured=True).count(),
    }
    return render(request, 'doctorDashboard/ecommerce_items.html', context)

def delete_item(request, item_id):
    try:
        item = Item.objects.get(id=item_id)
        item_name = item.title
        item.delete()
        messages.success(request, f'Product "{item_name}" deleted successfully!')
    except Item.DoesNotExist:
        messages.error(request, 'Product not found!')
    return redirect('ecommerce_items')
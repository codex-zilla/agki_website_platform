
from django.shortcuts import render, redirect
from django.contrib import messages
from .forms import AppointmentForm

def home(request):
    return render(request, 'app/home.html')

def book_appointment(request):
    form = AppointmentForm()
    if request.method == 'POST':
        form = AppointmentForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Appointment booked successfully!')
            return redirect('appointment_success') 
    else:
        form = AppointmentForm()
    
    return render(request, 'app/book_appointment.html', {'form': form})

def appointment_success(request):
    return render(request, 'app/appointment_success.html')

def signin(request):
    return render(request, 'app/signin.html')
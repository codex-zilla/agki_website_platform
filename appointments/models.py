from django.db import models
from useraccount.models import CustomUser

class AvailableSlot(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    date = models.DateField()
    time = models.TimeField()
    is_booked = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)  # ← auto updated field
    created_at = models.DateTimeField(auto_now_add=True)  # ← auto created field

    def __str__(self):
        status = "Booked" if self.is_booked else "Available"
        return f"{self.date} at {self.time} - {status}"
    
# Create an appointment.
import random
import string
from django.utils import timezone

class Appointment(models.Model):
    fullName = models.CharField(max_length=100)
    birthDate = models.DateField()
    gender = models.CharField(max_length=20)
    age = models.IntegerField(null=True, blank=True)
    email = models.EmailField()
    phone = models.CharField(max_length=15)
    visitType = models.CharField(max_length=20) # e.g., New Patient, Follow-up
    reasonVisit = models.CharField(max_length=100)
    appointmentMode = models.CharField(max_length=20, null=True, blank=True) # e.g., In-person, Virtual
    symptoms = models.TextField() # symptoms or concerns
    slot = models.ForeignKey(AvailableSlot, on_delete=models.CASCADE)
    bookingID = models.CharField(max_length=20, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        is_new = self.pk is None  # check if appointment is being created
        
        # Generate booking ID if not already set
        if not self.bookingID:
            current_year = timezone.now().year
            random_digits = ''.join(random.choices(string.digits, k=5))
            self.bookingID = f"AGKI-{current_year}-{random_digits}"

        super().save(*args, **kwargs)

        # If new appointment created, mark slot as booked
        if is_new:
            self.slot.is_booked = True
            self.slot.save(update_fields=['is_booked'])

    def __str__(self):
        return f"Appointment for {self.fullName} on {self.created_at}"

class AppointmentApproval(models.Model):
    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE)
    is_approved = models.BooleanField(default=False)
    approved_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    approved_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        status = "Approved" if self.is_approved else "Pending"
        return f"Appointment {self.appointment.bookingID} - {status}"
    

class AppointmentNote(models.Model):
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name='notes')
    note_text = models.TextField()
    added_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Note for {self.appointment.fullName} at {self.created_at}"  
    

class Rating(models.Model):
    doctor = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    rating_value = models.IntegerField()  # e.g., 1 to 5
    feedback = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    recommended = models.BooleanField(default=True)

    def __str__(self):
        return f"Rating for {self.doctor.username}: {self.rating_value}"

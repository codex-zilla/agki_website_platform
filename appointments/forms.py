from django import forms
from .models import *

class AppointmentForm(forms.ModelForm):
    class Meta:
        model = Appointment
        fields = [
                    'fullName','birthDate','gender','email','phone',
                    'visitType','reasonVisit','symptoms'
                  ]

class AppointmentNoteForm(forms.ModelForm):
    class Meta:
        model = AppointmentNote
        fields = ['note_text',]
        
class AppointmentApprovalForm(forms.ModelForm):
    class Meta:
        model = AppointmentApproval
        fields = ['is_approved',]
        
class AvailableSlotForm(forms.ModelForm):
    class Meta:
        model = AvailableSlot
        fields = ['date','time']
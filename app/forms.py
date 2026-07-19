from django import forms
from .models import *

class AppointmentForm(forms.ModelForm):
    class Meta:
        model = Appointment
        fields = [
            'fullName', 'phone', 'street', 'ward', 
            'district', 'region', 'department', 
            'notes'
        ]
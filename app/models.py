from django.db import models

class Appointment(models.Model):
    fullName = models.CharField(max_length=100)
    phone = models.CharField(max_length=100)
    street = models.CharField(max_length=100)
    ward = models.CharField(max_length=100)
    district = models.CharField(max_length=100)
    region = models.CharField(max_length=100)
    department = models.CharField(max_length=100)
    appointment_time = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.fullName}  - {self.appointment_time}"

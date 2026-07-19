from django.urls import path
from . import views
urlpatterns = [
    path('',views.doctor_admin_dashboard,name='doctor_admin_dashboard'),
    path('base/',views.doctor_base,name='doctor_base'),
    path('schedule/',views.doctor_schedule,name='doctor_schedule'),
    path('available-appointments/',views.available_appointments,name='available_appointments'),
    path('appointment-details/<int:appointment_id>/', views.doctor_appointment_details, name='doctor_appointment_details'),
    path('add_appointment_note/<int:appointment_id>/', views.add_appointment_note, name='add_appointment_note'),
    path('appointment_approval/<int:appointment_id>/', views.appointment_approval, name='appointment_approval'),
    path('doctor_availability/', views.doctor_availability, name='doctor_availability'),
    path('add_doctor_availability/', views.add_doctor_availability, name='add_doctor_availability'),
    
    path('ecommerce_items/', views.ecommerce_items, name='ecommerce_items'),
    path('products/delete/<int:item_id>/', views.delete_item, name='delete_item'),
    
    path('doctors/', views.doctors, name='doctors'),
    path('activate_deactivate_doctor/<int:doctor_id>/', views.activate_deactivate_doctor, name='activate_deactivate_doctor'),
    
]
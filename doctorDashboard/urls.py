from django.urls import path
from . import views
urlpatterns = [
    path('',views.doctor_admin_dashboard,name='doctor_admin_dashboard'),
    # Both of these are scheduled for deletion (plan 5.7, 5.9) and neither is
    # linked from the sidebar any more. They stay reachable by URL **only** so
    # AGKI can compare them against their replacements in a browser first —
    # decision 5 made deleting `doctor_schedule` conditional on
    # `doctor_availability` really providing the timetable it imitated, and
    # that condition is a judgement somebody should make by looking.
    path('base/',views.doctor_base,name='doctor_base'),
    path('schedule/',views.doctor_schedule,name='doctor_schedule'),
    path('available-appointments/',views.available_appointments,name='available_appointments'),
    path('appointment-details/<int:appointment_id>/', views.doctor_appointment_details, name='doctor_appointment_details'),
    path('add_appointment_note/<int:appointment_id>/', views.add_appointment_note, name='add_appointment_note'),
    # Replaces `appointment_approval`, which wrote the superseded
    # `AppointmentApproval` row: one boolean cannot express a flow with two
    # provider gates (design §5). Every state change goes through here.
    path('appointment-decision/<int:appointment_id>/', views.appointment_decision, name='appointment_decision'),

    path('doctor_availability/', views.doctor_availability, name='doctor_availability'),
    path('add_doctor_availability/', views.add_doctor_availability, name='add_doctor_availability'),
    path('publish-availability/', views.publish_availability, name='publish_availability'),
    path('delete-availability/<int:slot_id>/', views.delete_availability, name='delete_availability'),

    path('ecommerce_items/', views.ecommerce_items, name='ecommerce_items'),
    # No `delete_item` route. It deleted a product on an anonymous GET; see the
    # note where the view used to be in views.py (design §4.4, plan 1.4).

    path('doctors/', views.doctors, name='doctors'),
    path('doctors/add/', views.add_doctor, name='add_doctor'),
    # Replaces `activate_deactivate_doctor`, whose label showed `is_active`
    # while its POST flipped `is_approved` (design §4.2 point 5). Two
    # independent flags now, each named in the request.
    path('doctors/<int:doctor_id>/status/', views.set_doctor_status, name='set_doctor_status'),
]

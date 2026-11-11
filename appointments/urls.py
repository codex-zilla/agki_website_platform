from django.urls import path
from . import views
urlpatterns = [
    path('',views.appointment_home,name='appointment_home'),
    path('search/',views.search_results,name='search_results'),
    # One doctor: who they are, and when they can be seen. The results page
    # used to carry both, so a list of doctors was mostly a list of times
    # (plan 11.15).
    path('doctor/<int:doctor_id>/', views.doctor_detail, name='doctor_detail'),
    path('booking/<int:id>/', views.booking_form, name='booking_form'),
    # Both patient-record URLs key off `bookingID`, not the sequential primary
    # key. The old `<int:appointment_id>` routes are gone rather than
    # redirected: incrementing that integer *was* the vulnerability (design
    # §8.1), so there is nothing worth preserving a link to.
    path('booking-success/<str:booking_id>/', views.booking_success_page, name='booking_success_page'),
    path('lookup/',views.booking_id_lookup,name='booking_id_lookup'),
    path('detail/<str:booking_id>/',views.appointment_detail,name='appointment_detail'),
    # Round 2 and the one real Manage-Appointment action. Both hang off the
    # detail URL because they are the same record behind the same gate — a
    # separate top-level path would be a second place to get that gate wrong.
    path('detail/<str:booking_id>/cancel/', views.appointment_cancel, name='appointment_cancel'),
]
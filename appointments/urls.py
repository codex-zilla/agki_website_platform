from django.urls import path
from . import views
urlpatterns = [
    path('',views.appointment_home,name='appointment_home'),
    path('search/',views.search_results,name='search_results'),
    path('booking/<int:id>/', views.booking_form, name='booking_form'),
    # Both patient-record URLs key off `bookingID`, not the sequential primary
    # key. The old `<int:appointment_id>` routes are gone rather than
    # redirected: incrementing that integer *was* the vulnerability (design
    # §8.1), so there is nothing worth preserving a link to.
    path('booking-success/<str:booking_id>/', views.booking_success_page, name='booking_success_page'),
    path('lookup/',views.booking_id_lookup,name='booking_id_lookup'),
    path('detail/<str:booking_id>/',views.appointment_detail,name='appointment_detail'),
]
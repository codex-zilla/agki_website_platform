from django.urls import path
from . import views
urlpatterns = [
    path('',views.appointment_home,name='appointment_home'),
    path('search/',views.search_results,name='search_results'),
    path('booking/<int:id>/', views.booking_form, name='booking_form'),
    path('booking-success/<int:appointment_id>/', views.booking_success_page, name='booking_success_page'),
    path('lookup/',views.booking_id_lookup,name='booking_id_lookup'),
    path('detail/<int:appointment_id>/',views.appointment_detail,name='appointment_detail'),
]

from django.urls import path,include
from . import views


urlpatterns = [
    path('', views.home, name='home'),
    path('book-appointment/', views.book_appointment, name='book_appointment'),
    path('appointment-success/', views.appointment_success, name='appointment_success'),
    path('signin/', views.signin, name='signin'),
]
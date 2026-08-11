
from django.urls import path,include
from . import views


urlpatterns = [
    path('', views.home, name='home'),
    # Separate POST endpoint so `home` stays a pure, cacheable GET (design §9).
    path('contact/', views.contact_submit, name='contact_submit'),
]
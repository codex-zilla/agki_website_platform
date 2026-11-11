"""
URL configuration for project project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin

from appointments import views as appointment_views
from django.urls import path,include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('app.urls')),
    path('appointments/', include('appointments.urls')),
    # Deliberately at the root and deliberately two characters: this is the
    # link that goes in a text message, where every character is charged
    # (plan 13.3). It redirects into the appointments app.
    path('a/<str:booking_id>/', appointment_views.short_link,
         name='appointment_short_link'),
    path('useraccount/', include('useraccount.urls')),
    path('doctorDashboard/', include('doctorDashboard.urls')),
    path('ecommerce/', include('ecommerce.urls')),
    path('afya/', include('afya.urls')),  # Afya Mfukoni
    path('accounts/', include('allauth.urls')),  # google login ===============
    path('ckeditor5/', include('django_ckeditor_5.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL,document_root=settings.MEDIA_ROOT)
    
urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)



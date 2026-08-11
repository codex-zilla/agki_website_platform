
from django.urls import path
from . import views

from .views import custom_logout_view
from rest_framework_simplejwt.views import TokenRefreshView

# Import the custom password reset views
from .views import (
    CustomPasswordResetView,
    CustomPasswordResetDoneView,
    CustomPasswordResetConfirmView,
    CustomPasswordResetCompleteView
)


urlpatterns = [
    # Named 'useraccount_home', not 'home' - see the note in ecommerce/urls.py.
    # Three apps registered the name 'home' (app, useraccount, ecommerce) and
    # Django resolves duplicates to the last one included, so the name never
    # pointed at the actual marketing homepage. It belongs to app.urls now.
    # The view behind this route renders a 13-line developer scratch page
    # ("QR Code Generator" / "McColn Basement"); nothing links to it. Left in
    # place rather than deleted - flagged as cleanup, not silently removed.
    path('', views.home, name='useraccount_home'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('profile/', views.profile, name='profile'),
    path('profile/edit/', views.profile_edit, name='profile_edit'),
    path('account/delete/', views.delete_account, name='delete_account'),

    path('logout/', custom_logout_view, name='logout'),
    path('signup/', views.signup, name='signup'),
    path('signin/', views.signin, name='signin'),
    path('verify-email/<str:username>/', views.verify_email, name='verify-email'),
    path('resend-otp/', views.resend_otp, name='resend-otp'),
    path('otp/', views.otp_view, name='otp'), # otp for login verification

    # Password reset URLs
    path('password-reset/', 
         CustomPasswordResetView.as_view(), 
         name='password_reset'),
    
    path('password-reset/done/', 
         CustomPasswordResetDoneView.as_view(), 
         name='password_reset_done'),
    
    path('reset/<uidb64>/<token>/', 
         CustomPasswordResetConfirmView.as_view(), 
         name='password_reset_confirm'),
    
    path('reset/done/', 
         CustomPasswordResetCompleteView.as_view(), 
         name='password_reset_complete'),

    # change password
    path('change-password/', views.change_password, name='change_password'),
    path('password_change_done/', views.password_change_done, name='password_change_done'),

     #    restframework API
    path('register/', views.register_user, name='register'),
    path('login/', views.login_user, name='login'),
    path('logout/', views.logout_user, name='api-logout'),
    path('profile/', views.get_user_profile, name='profile'),
    path('profile/update/', views.update_user_profile, name='update-profile'),
    path('change-password/', views.change_password, name='change-password'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('pre-approve-notice/', views.pre_approve_notice, name='pre_approve_notice'),

]


from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.views import (
    PasswordResetView, 
    PasswordResetDoneView, 
    PasswordResetConfirmView,
    PasswordResetCompleteView
)
from django.contrib import messages
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from .forms import *
from .models import *
from .utils import send_otp
import pyotp


class CustomPasswordResetView(PasswordResetView):
    form_class = CustomPasswordResetForm
    template_name = 'useraccount/password_reset_form.html'
    email_template_name = 'useraccount/password_reset_email.txt'  # Plain text primary
    html_email_template_name = 'useraccount/password_reset_email.html'  # HTML fallback
    subject_template_name = 'useraccount/password_reset_subject.txt'
    success_url = reverse_lazy('password_reset_done')
    
    def form_valid(self, form):
        messages.success(
            self.request, 
            'Password reset email has been sent to your email address.'
        )
        return super().form_valid(form)

class CustomPasswordResetDoneView(PasswordResetDoneView):
    template_name = 'useraccount/password_reset_done.html'

@method_decorator(sensitive_post_parameters(), name='dispatch')
@method_decorator(never_cache, name='dispatch')
class CustomPasswordResetConfirmView(PasswordResetConfirmView):
    form_class = CustomSetPasswordForm
    template_name = 'useraccount/password_reset_confirm.html'
    success_url = reverse_lazy('password_reset_complete')
    
    def form_valid(self, form):
        messages.success(
            self.request, 
            'Your password has been reset successfully!'
        )
        return super().form_valid(form)

class CustomPasswordResetCompleteView(PasswordResetCompleteView):
    template_name = 'useraccount/password_reset_complete.html'



# ==========================================================================
def signup(request):
    if request.method == 'POST':
        username = request.POST['username']
        firstname = request.POST['firstname']
        lastname = request.POST['lastname']
        email = request.POST['email']
        # added this
        phone = request.POST['phone']
        role = request.POST['role']
        image = request.FILES.get('image')
        # end added
        password1 = request.POST['password1']
        password2 = request.POST['password2']

        # Check if email already exists
        if CustomUser.objects.filter(email=email).exists():
            messages.error(request, 'An account with this email already exists. Please use another email or log in.')
            return redirect('signup')

        # Check if username already exists (optional but recommended)
        if CustomUser.objects.filter(username=username).exists():
            messages.error(request, 'This username is already taken. Please choose another one.')
            return redirect('signup')

        # Check if passwords match
        if password1 != password2:
            messages.error(request, 'Passwords do not match. Please try again.')
            return redirect('signup')

        # The <select> in the template only offers doctor/receptionist, but
        # the request body is still attacker-controlled - validate server
        # side too. "admin" is deliberately never accepted here; that role
        # is only ever assigned through the staff admin UI (CustomUserAdmin).
        allowed_signup_roles = {CustomUser.Role.DOCTOR, CustomUser.Role.RECEPTIONIST}
        if role not in allowed_signup_roles:
            messages.error(request, 'Please select a valid role.')
            return redirect('signup')

        # Create user but don't activate yet
        myuser = CustomUser.objects.create_user(username=username, email=email, password=password1)
        myuser.first_name = firstname
        myuser.last_name = lastname
        myuser.phone = phone
        myuser.role = role
        myuser.image = image
        myuser.save()
        
        messages.success(request, 'Account created successfully!')
        return redirect('signin')

    return render(request, 'useraccount/signup.html')


def verify_email(request, username):
    user = get_object_or_404(CustomUser, username=username)
    user_otp = OtpToken.objects.filter(user=user).last()
    if request.method == 'POST':
        if user_otp.otp_code == request.POST.get('otp_code'):
            if user_otp.otp_expires_at >= timezone.now():
                user.is_active = True
                user.save()
                messages.success(request, 'Your email has been verified successfully! You can now log in.')
                return redirect('pre_approve_notice')
            # expired token
            else:
                messages.warning(request, 'OTP has expired. Get a new OTP.')
                return redirect('verify-email', username=user.username)
        # invalid otp code
        else:
            messages.warning(request, 'Invalid OTP. Please enter a valid OTP.')
            return redirect('verify-email', username=user.username)
    context = {}
    return render(request, 'useraccount/verify_token.html', context)

def pre_approve_notice(request):
    return render(request, 'useraccount/pre_approve_notice.html')

def resend_otp(request):
    if request.method == 'POST':
        user_email = request.POST["otp_email"]

        if CustomUser.objects.filter(email=user_email).exists():
            user = get_object_or_404(CustomUser, email=user_email)
            otp = OtpToken.objects.create(user=user, otp_expires_at=timezone.now() + timezone.timedelta(minutes=2))
         
            # email
            subject = 'Email Verification - Your OTP Code'
            verification_url = (
                f"{settings.SITE_URL}"
                f"{reverse('verify-email', kwargs={'username': user.username})}"
            )
            message = f"""
                            Dear {user.username},
                            Your OTP code is: {otp.otp_code}
                            This code will expire in 2 minutes. 
                            use the url below to verify your account:
                            {verification_url}
                            If you did not request this, please ignore this email.
                        """
            sender = settings.EMAIL_HOST_USER
            receiver = [user.email]
            send_mail(
                subject, 
                message, 
                sender, 
                receiver, 
                fail_silently=False
            )
            messages.success(request, 'A new OTP has been sent to your email address.')
            return redirect('verify-email', username=user.username)
        else:
            messages.warning(request, 'Email address not found. Please enter a registered email.')
            return redirect('resend-otp')
    
    context = {}
    return render(request, 'useraccount/resend_otp.html', context)

def custom_logout_view(request):
    logout(request)
    return redirect('signin') 


# login without otp verification ===============

def signin(request):
    if request.method == 'POST':
        username = request.POST['username']
        password1 = request.POST['password1']

        # Check if username exists
        if not CustomUser.objects.filter(email=username).exists():
            messages.error(request, 'No account found with this username.')
            return redirect('signin')

        user = authenticate(username=username, password=password1)

        if user is not None:
            if user.is_active:
                login(request, user)
                return redirect('doctor_admin_dashboard')
            else:
                messages.warning(request, 'Your account is not verified yet. Please check your email for OTP verification.')
                return redirect('signin')
            
        else:
            messages.error(request, 'Incorrect password. Please try again.')
            return redirect('signin')

    return render(request, 'useraccount/signin.html')

# login with otp verification ===============
# def signin(request):
#     if request.method == 'POST':
#         username = request.POST['username']
#         password1 = request.POST['password1']

#         # Check if username exists
#         if not CustomUser.objects.filter(email=username).exists():
#             messages.error(request, 'No account found with this email.')
#             return redirect('signin')

#         # Authenticate user
#         user = authenticate(username=username, password=password1)

#         if user is not None:
#             if user.is_active:
#                 pass
#             else:
#                 messages.warning(request, 'Your account is not verified yet. Please check your email for OTP verification.')
#                 return redirect('signin')
            
#             # see if its approved by admin
#             if not user.is_approved:
#                 messages.warning(request, 'Your account is pending admin approval. Please wait for approval before logging in.')
#                 return redirect('signin')
            

#             # ✅ send otp tied to user
#             send_otp(user)

#             request.session['username'] = username
#             request.session['backend'] = user.backend  # keep backend

#             return redirect('otp')
#         else:
#             messages.error(request, 'Incorrect password. Please try again.')
#             return redirect('signin')

#     return render(request, 'useraccount/signin.html')


def otp_view(request):
    if request.method == 'POST':
        otp = request.POST['otp']
        username = request.session.get('username')

        try:
            user = CustomUser.objects.get(email=username)
        except CustomUser.DoesNotExist:
            messages.error(request, 'User not found. Please log in again.')
            return redirect('signin')

        if user.otp_secret_key and user.otp_valid_date:
            if user.otp_valid_date > timezone.now():
                totp = pyotp.TOTP(user.otp_secret_key, interval=120)
                if totp.verify(otp):
                    backend = request.session.get('backend', 'django.contrib.auth.backends.ModelBackend')
                    login(request, user, backend=backend)

                    # clean up OTP fields
                    user.otp_secret_key = None
                    user.otp_valid_date = None
                    user.save(update_fields=['otp_secret_key', 'otp_valid_date'])

                    # clean session
                    request.session.pop('backend', None)
                    request.session.pop('username', None)

                    return redirect('doctor_admin_dashboard')
                else:
                    messages.error(request, 'Invalid OTP. Please try again.')
                    return redirect('otp')
            else:
                messages.error(request, 'OTP has expired. Please log in again.')
                return redirect('signin')
        else:
            messages.error(request, 'No OTP found. Please log in again.')
            return redirect('signin')

    return render(request, 'useraccount/otp.html')


# ==========================================================================
def home(request):
    return render(request, 'useraccount/home.html')

def dashboard(request):
    return render(request, 'useraccount/dashboard.html')

def profile(request):
    profile, created = Profile.objects.get_or_create(user=request.user)
    context = {'profile': profile}
    return render(request, 'useraccount/profile.html', context)

@login_required
def profile_edit(request):
    profile, created = Profile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        user_form = UserEditForm(request.POST, request.FILES, instance=request.user)
        profile_form = ProfileEditForm(request.POST, request.FILES, instance=profile)

        if user_form.is_valid() and profile_form.is_valid():
            user_form.save()
            profile_form.save()
            messages.success(request, 'Your profile has been updated successfully!')
            return redirect('profile')
    else:
        user_form = UserEditForm(instance=request.user)
        profile_form = ProfileEditForm(instance=profile)

    return render(request, 'useraccount/profile_edit.html', {
        'user_form': user_form,
        'profile_form': profile_form,
    })

@login_required
def delete_account(request):
    if request.method == 'POST':
        user = request.user
        logout(request)  # log out the user first
        user.delete()    # delete the user and related profile (if on_delete=CASCADE)
        messages.success(request, 'Your account has been deleted successfully.')
        return redirect('home')  # redirect to home page or landing page

    return render(request, 'useraccount/delete_account.html')

# ==============================================================================
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from .forms import CustomPasswordChangeForm

@login_required
def change_password(request):
    if request.method == 'POST':
        form = CustomPasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            # Update session to prevent logout
            update_session_auth_hash(request, user)
            messages.success(request, 'Your password was successfully updated!')
            return redirect('password_change_done')
        else:
            messages.error(request, 'Please correct the error below.')
    else:
        form = CustomPasswordChangeForm(request.user)
    
    return render(request, 'useraccount/change_password.html', {'form': form})


def password_change_done(request):
    return render(request,'useraccount/password_change_done.html')

# ===================================================================================


#API LOGIN VIEW WITH JWT TOKEN ==================================================
# pip install django djangorestframework djangorestframework-simplejwt django-cors-headers

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()

@api_view(['POST'])
@permission_classes([AllowAny])
def register_user(request):
    try:
        # Get data from request
        username = request.data.get('username')
        email = request.data.get('email')
        password = request.data.get('password')
        password2 = request.data.get('password2')
        first_name = request.data.get('first_name', '')
        last_name = request.data.get('last_name', '')

        # Validation
        if not all([username, email, password, password2]):
            return Response({
                'error': 'All fields are required: username, email, password, password2'
            }, status=status.HTTP_400_BAD_REQUEST)

        if password != password2:
            return Response({
                'error': 'Passwords do not match'
            }, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(username=username).exists():
            return Response({
                'error': 'Username already exists'
            }, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(email=email).exists():
            return Response({
                'error': 'Email already exists'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Validate password strength
        try:
            validate_password(password)
        except ValidationError as e:
            return Response({
                'error': ' '.join(e.messages)
            }, status=status.HTTP_400_BAD_REQUEST)

        # Create user
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name
        )

        # Generate tokens
        refresh = RefreshToken.for_user(user)
        
        # Prepare user data for response
        user_data = {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name
        }

        return Response({
            'user': user_data,
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'message': 'User registered successfully'
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response({
            'error': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@permission_classes([AllowAny])
def login_user(request):
    
    username = request.data.get('username')
    password = request.data.get('password')

    if not username or not password:
        return Response({
            'error': 'Both username and password are required'
        }, status=status.HTTP_400_BAD_REQUEST)

    user = authenticate(username=username, password=password)
    
    if user is not None:
        refresh = RefreshToken.for_user(user)
        
        # Prepare user data
        user_data = {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name
        }
        
        return Response({
            'user': user_data,
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'message': 'Login successful'
        })
    else:
        return Response({
            'error': 'Invalid credentials'
        }, status=status.HTTP_401_UNAUTHORIZED)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_user(request):
    try:
        refresh_token = request.data.get('refresh_token')
        if refresh_token:
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response({'message': 'Successfully logged out'}, status=status.HTTP_200_OK)
        else:
            return Response({'error': 'Refresh token is required'}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({'error': 'Invalid token'}, status=status.HTTP_400_BAD_REQUEST)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_user_profile(request):
    user = request.user
    user_data = {
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'is_active': user.is_active,
        'date_joined': user.date_joined
    }
    return Response(user_data)

@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def update_user_profile(request):
    user = request.user
    data = request.data

    try:
        # Update allowed fields
        if 'first_name' in data:
            user.first_name = data['first_name']
        if 'last_name' in data:
            user.last_name = data['last_name']
        if 'email' in data:
            # Check if email is already taken by another user
            if User.objects.filter(email=data['email']).exclude(id=user.id).exists():
                return Response({
                    'error': 'Email already taken'
                }, status=status.HTTP_400_BAD_REQUEST)
            user.email = data['email']

        user.save()

        user_data = {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name
        }

        return Response({
            'user': user_data,
            'message': 'Profile updated successfully'
        })

    except Exception as e:
        return Response({
            'error': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_password(request):
    user = request.user
    data = request.data

    current_password = data.get('current_password')
    new_password = data.get('new_password')
    confirm_password = data.get('confirm_password')

    if not all([current_password, new_password, confirm_password]):
        return Response({
            'error': 'All password fields are required'
        }, status=status.HTTP_400_BAD_REQUEST)

    if new_password != confirm_password:
        return Response({
            'error': 'New passwords do not match'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Check current password
    if not user.check_password(current_password):
        return Response({
            'error': 'Current password is incorrect'
        }, status=status.HTTP_400_BAD_REQUEST)

    # Validate new password
    try:
        validate_password(new_password, user)
    except ValidationError as e:
        return Response({
            'error': ' '.join(e.messages)
        }, status=status.HTTP_400_BAD_REQUEST)

    # Set new password
    user.set_password(new_password)
    user.save()

    return Response({
        'message': 'Password changed successfully'
    })

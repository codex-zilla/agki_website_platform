

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
from django.views.decorators.http import require_POST

from .notices import ACCESS_WITHDRAWN
from . import otp as verification
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

        # Create user but don't activate yet
        myuser = CustomUser.objects.create_user(username=username, email=email, password=password1)
        myuser.first_name = firstname
        myuser.last_name = lastname
        myuser.phone = phone
        # Assigned, never asked. Only doctors self-register (design §0.1), and
        # decision 4 records the receptionist role as one that does not exist
        # - so the <select> this replaces offered a choice between a real
        # account type and an imaginary one.
        #
        # It is **stricter** than the validation it replaces, not looser. That
        # code read `role` out of the request body and checked it against a
        # closed set; this never reads it at all, so there is nothing to
        # validate and nothing an attacker can put there. Same direction the
        # `role='4491'` lesson points, one step further, and it makes this form
        # agree with `SocialAccountAdapter.save_user`, which has assigned
        # `doctor` since plan 10.4.
        #
        # Dropping the `request.POST['role']` read also removes a bare
        # subscript that would have raised `MultiValueDictKeyError` - an
        # unconditional 500 - for any client that posted without the field.
        # That is precisely how the "Add New Doctor" modal was broken (5.12).
        myuser.role = CustomUser.Role.DOCTOR
        myuser.image = image
        # `update_fields` is load-bearing, not tidiness. `create_user` fires
        # the signal in `useraccount.signals`, which holds a public signup
        # until its emailed code is used by writing `is_active=False`
        # straight to the row. A plain `.save()` here writes this stale
        # in-memory instance back over it — `is_active` is still True on the
        # object — so the account came out **active** and the OTP gated
        # nothing: a registrant could sign in without ever opening the email.
        #
        # Found by the Phase 10 gate test, which asserted the account was
        # inactive and was right to. It matters more under decision F than it
        # did before: approval is granted at registration now, so controlling
        # the address is the only thing anybody has to prove (design §4.5).
        myuser.save(update_fields=[
            'first_name', 'last_name', 'phone', 'role', 'image',
        ])
        
        # Straight to the page that finishes the job. This used to redirect
        # to `signin` with "Account created successfully!", which was true and
        # useless: the account is deliberately inactive until the emailed code
        # is used, so the next thing every registrant saw was their own login
        # being refused as "not verified yet" — with nothing on screen linking
        # to the page where the code goes. The only route in was the URL
        # inside the email, which is a poor thing to depend on and no help at
        # all to somebody watching the tab they just submitted.
        messages.success(
            request,
            f"Account created. We have sent a 6-digit code to "
            f"{verification.mask_email(myuser.email)}.",
        )
        return redirect('verify-email', username=myuser.username)

    return render(request, 'useraccount/signup.html')


def verify_email(request, username):
    """Finish a form registration, and sign the account in.

    **This is an authentication endpoint now**, not a formality: a correct
    code logs the account in and lands it on its dashboard, which is what AGKI
    asked for on 2026-08-31 and is a real improvement — the old flow verified
    the address and then dropped the person back on a login form to type a
    password they had entered ninety seconds earlier.

    Being an authentication endpoint is what the guards below are for, and
    each replaces something that was missing rather than something that was
    wrong:

    * **Only an inactive account may be verified.** Without this, the page is
      a way to log in as any account whose six-digit code you can guess.
    * **Attempts are rate-limited** (`otp.is_rate_limited`). Six digits with
      unlimited guesses is not a gate.
    * **A missing code no longer 500s.** `user_otp.otp_code` was read straight
      off a `.last()` that returns `None` for anyone with no outstanding code
      — a verified user revisiting the page, or one whose code was cleaned up.
    * **A used code is deleted**, so it cannot be replayed.

    The username in the URL is not a secret and never was: it is the same
    value the signup form just chose, and the page reveals nothing about an
    account that does not already exist. What it must not become is an oracle
    for *codes*, which is what the rate limit is for.
    """
    user = get_object_or_404(CustomUser, username=username)

    if user.is_active:
        # Already done. Sending them on rather than showing a code box they
        # have no code for, and refusing to let this page authenticate an
        # account that is not waiting on it.
        messages.info(request, 'Your email is already verified. Please sign in.')
        return redirect('signin')

    if request.method == 'POST':
        if verification.is_rate_limited(request, user):
            messages.error(
                request,
                'Too many attempts. Please wait a few minutes and try again.',
            )
            return redirect('verify-email', username=user.username)

        token = verification.latest_code(user)
        submitted = (request.POST.get('otp_code') or '').strip()

        if token is None:
            messages.warning(request, 'That code has expired. Request a new one.')
        elif token.otp_expires_at and token.otp_expires_at < timezone.now():
            messages.warning(request, 'That code has expired. Request a new one.')
        elif token.otp_code != submitted:
            messages.warning(request, 'That code is not right. Please check and try again.')
        else:
            user.is_active = True
            user.save(update_fields=['is_active'])
            OtpToken.objects.filter(user=user).delete()

            # An explicit backend because the project configures three, and
            # `login()` refuses to guess between them. `EmailBackend` is the
            # one that owns the `is_active` + `is_approved` rules, so it is
            # the right one to have authorised this session.
            login(request, user, backend='useraccount.backends.EmailBackend')

            messages.success(request, 'Your email is verified. Welcome to AGKI.')
            # `post_login_redirect` rather than a hard-coded dashboard: it is
            # the single place that decides where an account lands (plan 1.9),
            # and hard-coding "doctor dashboard" here would be the fourth
            # entry point to disagree with it.
            return redirect('post_login_redirect')

        return redirect('verify-email', username=user.username)

    context = {
        'account': user,
        'masked_email': verification.mask_email(user.email),
        'seconds_until_expiry': verification.seconds_until_expiry(user),
        'seconds_until_resend': verification.seconds_until_resend(user),
    }
    return render(request, 'useraccount/verify_token.html', context)


@require_POST
def resend_verification(request, username):
    """Another code for an account that is already waiting for one.

    Separate from `resend_otp`, which asks for an email address and exists for
    somebody who closed the tab. This one is the button on the code page, so
    it needs no typing and cannot be pointed at a different account.

    POST-only, and the cooldown is enforced **here** rather than only in the
    countdown on the page: a disabled button is a courtesy, not a control, and
    this endpoint is one `curl` away from being an email-sending service
    pointed at somebody else's inbox.
    """
    user = get_object_or_404(CustomUser, username=username)

    if user.is_active:
        messages.info(request, 'Your email is already verified. Please sign in.')
        return redirect('signin')

    waiting = verification.seconds_until_resend(user)
    if waiting:
        messages.warning(
            request,
            f'Please wait {waiting} more second{"" if waiting == 1 else "s"} '
            f'before requesting another code.',
        )
    else:
        verification.issue_code(user)
        messages.success(
            request,
            f'A new code is on its way to {verification.mask_email(user.email)}.',
        )
    return redirect('verify-email', username=user.username)

def pre_approve_notice(request):
    return render(request, 'useraccount/pre_approve_notice.html')

def resend_otp(request):
    if request.method == 'POST':
        user_email = request.POST["otp_email"]

        if CustomUser.objects.filter(email=user_email).exists():
            user = get_object_or_404(CustomUser, email=user_email)
            # A second copy of the verification email lived here, worded
            # differently from the signal's, so a registrant who asked for
            # another code received a different-looking message than the
            # first - which is what a spam filter, and a cautious person,
            # both notice. Both come from `otp.issue_code` now.
            #
            # The same cooldown applies as on the button, because this page
            # would otherwise be the way around it: anyone could point it at
            # somebody else's address and send them a code every second.
            if not verification.seconds_until_resend(user):
                verification.issue_code(user)
            messages.success(request, 'A new code has been sent to your email address.')
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
        account = CustomUser.objects.filter(email=username).first()
        if account is None:
            messages.error(request, 'No account found with this username.')
            return redirect('signin')

        user = authenticate(request, username=username, password=password1)

        if user is not None:
            login(request, user)
            # Single source of truth for the landing page — see
            # post_login_redirect. This used to send every account,
            # including content editors and superusers, to the doctor
            # dashboard.
            return redirect('post_login_redirect')

        # `authenticate` refuses three different things and reports none of
        # them: a wrong password, an unverified account, and — since plan 1.10
        # — an unapproved one. The gate itself lives in the backend, where it
        # applies to every caller; this only re-derives which of the three it
        # was, so the person reading the message can act on it. Checking the
        # password again is what keeps "pending approval" from being said to
        # someone who does not know the password.
        # An inactive account means **two different things**, and telling them
        # apart is the whole of this branch:
        #
        #   * a signup that was never finished — the emailed code is still
        #     outstanding, because `verify_email` deletes it only on success;
        #   * an account an administrator switched off with the console's
        #     "Account active" toggle, which has no outstanding code because
        #     the account got past verification long ago.
        #
        # **An outstanding code is the marker**, and it has to be read before
        # a new one is issued or issuing would erase the distinction. Getting
        # this wrong is not cosmetic: sending every inactive account to the
        # verification page would let somebody an administrator deliberately
        # switched off turn themselves back on by reading their own email.
        #
        # Issuing a code here is safe **because `check_password` passed**. The
        # caller has proved they are the account holder, so this cannot be
        # used to make the site email an address the caller does not control.
        pending_code = None if account.is_active else verification.latest_code(account)

        if account.check_password(password1):
            if pending_code is not None:
                # Sent to the page that can finish this, **without sending a
                # code on their behalf**. Signing in used to issue one here,
                # which AGKI pushed back on and was right to: it emails
                # somebody who may only be checking whether they have an
                # account, it starts a two-minute clock they did not ask to
                # start, and repeated sign-in attempts each send another.
                # Asking is one click and leaves the person in charge of it.
                #
                # What this must still not become is a dead end, which is what
                # `NOT_VERIFIED` was: the page they land on names the address
                # and offers the button, so the next step is visible rather
                # than described.
                messages.warning(
                    request,
                    f'Your account is not verified yet. Request a code and we '
                    f'will send it to {verification.mask_email(account.email)}.',
                )
                return redirect('verify-email', username=account.username)
            messages.warning(request, ACCESS_WITHDRAWN)
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

                    # Same landing-page decision as the plain signin path.
                    return redirect('post_login_redirect')
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

@login_required
def post_login_redirect(request):
    """Decide where an account lands straight after signing in.

    Every sign-in route funnels through here — the site's own `signin` form,
    the OTP step, and social/allauth login via `LOGIN_REDIRECT_URL` — so the
    decision lives in exactly one place instead of being duplicated (and
    contradicted) at each entry point, which is how it was:

    * `signin` and `otp_view` both redirected *everyone* to
      `doctor_admin_dashboard`, regardless of who they were.
    * `LOGIN_REDIRECT_URL` pointed at `dashboard` (the patient dashboard),
      so anyone arriving through Google/allauth landed there instead.

    Neither ever sent a staff member to the admin, so a Content Editor who
    signed in to edit the website was dropped on a clinical dashboard they
    have no permission to use.

    Order matters here: `role` is checked before `is_staff` deliberately, so
    a clinician who also happens to have a staff flag still lands on their
    own dashboard rather than the admin.

    **Both operational roles** route to the dashboard (design §4.1.2). This
    used to name only `doctor`, which meant the lab administrator - staff, not
    a superuser - landed on `/admin/` and never reached the console she
    administers. `role` is "which console is this person's job", and there are
    two roles in it; `is_staff` is "may open the Django admin", which is the
    right answer only for an account with no operational role at all, such as
    a Content Editor.
    """
    user = request.user

    if user.role in (CustomUser.Role.DOCTOR, CustomUser.Role.ADMIN):
        return redirect('doctor_admin_dashboard')

    # Covers content editors, receptionists-with-admin-access and superusers:
    # is_staff is exactly "may open the Django admin", which is where the
    # website's content lives.
    if user.is_staff:
        return redirect('admin:index')

    return redirect('dashboard')


def dashboard(request):
    return render(request, 'useraccount/dashboard.html')

@login_required
def profile(request):
    """What a patient sees, shown to the doctor it describes (plan 11.6).

    **`@login_required` is the fix, not the decoration.** Without it this
    view's first line - `get_or_create(user=request.user)` - is handed
    `AnonymousUser`, which the query cannot prepare, so a signed-out visitor
    got a 500 rather than a sign-in redirect. Plan 1.2 put the decorator on
    every `doctorDashboard` view; this one lives in `useraccount` and was
    outside that sweep, which left it the last console page an anonymous
    request could still reach.
    """
    from appointments.models import AppointmentSettings, AvailableSlot

    profile, _ = Profile.objects.get_or_create(user=request.user)

    # The next few bookable times, for the "how patients see you" card. Same
    # definition the public search uses - free, in the future, and far enough
    # ahead to satisfy the booking notice - because a preview that disagreed
    # with the real page would be worse than no preview.
    booking_settings = AppointmentSettings.load()
    earliest = booking_settings.earliest_bookable_start()
    upcoming = [
        slot
        for slot in AvailableSlot.objects.filter(
            user=request.user, is_booked=False, date__gte=earliest.date()
        ).order_by("date", "time")[:12]
        if slot.starts_at >= earliest
    ][:3]

    specialties = list(profile.specialties.all())
    context = {
        'profile': profile,
        'qualifications': list(profile.qualifications.all()),
        'specialties': specialties,
        'upcoming_slots': upcoming,
        # Named as a consequence rather than a percentage. "Profile 40%
        # complete" tells a doctor nothing; `search_results` filters on
        # specialty, so a doctor without one never appears at all - which is
        # true of every doctor in the database today.
        'findable': bool(specialties),
        'missing': [
            label
            for label, value in (
                ("a specialty", specialties),
                ("your qualifications", profile.qualifications.exists()),
                ("the region you practise in", profile.region),
                ("a short description", profile.bio),
            )
            if not value
        ],
    }
    context['page_section'] = 'Account'
    context['page_title'] = 'My profile'
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
        'page_section': 'Account',
        'page_title': 'Edit profile',
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

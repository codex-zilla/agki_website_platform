from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailBackend(ModelBackend):
    """Sign in with an email address instead of a username.

    Overriding `authenticate` the way this did skipped `user_can_authenticate`
    entirely, so the backend happily authenticated deactivated accounts and
    left every caller to notice for itself. Exactly one caller did: `signin`
    checked `is_active` by hand. Anything else calling `authenticate()` — a
    future API, a management command, a second login form — inherited a gate
    that was not there.

    Both checks now live in `user_can_authenticate` below, which is the hook
    Django calls for this and the reason it exists.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        try:
            user = UserModel.objects.get(email=username)
        except UserModel.DoesNotExist:
            # Run the default hasher once anyway, so a missing account does
            # not answer measurably faster than a wrong password. Same
            # reasoning as ModelBackend's own implementation.
            UserModel().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def user_can_authenticate(self, user):
        """`is_active` (Django's rule) **and** `is_approved` (AGKI's).

        `is_approved` is the flag the model already described as "added so as
        to add admin approval to login" and that the dashboard offers a button
        for — but nothing enforced it, so the button controlled nothing
        (design §4.2 point 4).

        Returning False here makes `authenticate()` return None, which cannot
        by itself explain *why*. `signin` re-derives that for the user-facing
        message; see the comment there.
        """
        return super().user_can_authenticate(user) and user.is_approved

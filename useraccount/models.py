import secrets
from django.db import models
from django.contrib.auth.models import AbstractUser


class CustomUser(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        DOCTOR = "doctor", "Doctor"
        RECEPTIONIST = "receptionist", "Receptionist"

    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=15, null=True, blank=True, default="null")
    image = models.ImageField(blank=True, null=True)
    # Account type - drives permissions/business logic (e.g. who shows up
    # as bookable on the public site). Admin-assigned only, not
    # self-editable - see useraccount/admin.py CustomUserAdmin and the
    # (now removed) role field on the self-service profile-edit form.
    role = models.CharField(max_length=20, choices=Role.choices, blank=True)
    # Free-text professional credentials (e.g. "MD, MBBS, PhD") - split out
    # from `role` because the two were previously the same field doing two
    # unrelated jobs, which is how e.g. "4491" ended up as someone's role.
    qualifications = models.CharField(max_length=255, blank=True)
    signature = models.ImageField(blank=True, null=True)
    # login otp verification
    otp_secret_key = models.CharField(max_length=255, blank=True, null=True)
    otp_valid_date = models.DateTimeField(blank=True, null=True)
    # "This account has not been switched off." A revocation switch, not an
    # admission queue (design §0.1, decision F).
    #
    # It gates sign-in, and that has not changed: `EmailBackend
    # .user_can_authenticate` and `AccountAdapter.pre_login` both refuse an
    # account with this false, and because the backend refuses in `get_user`
    # too, withdrawing it ends a live session rather than only the next login.
    #
    # `default=True` since plan 10.1. It was False while AGKI created every
    # account by hand, when the comment here read "added so as to add admin
    # approval to login". Doctors now register themselves, and an approval
    # queue nobody has committed to staffing is a slower way of saying no.
    #
    # What this does **not** claim is that the person is really a doctor.
    # Nothing in registration checks a qualification; email verification
    # proves control of an inbox, not a licence. Design §4.5 is where that
    # gets solved, and it must not be solved by overloading this field —
    # signing in and being publicly bookable are different questions.
    is_approved = models.BooleanField(default=True)

    #: The conditions that make an account visible to patients and bookable
    #: by them (design §6 step 2). **One definition**, because the page that
    #: *offers* a doctor and the view that *accepts* a booking have to agree —
    #: the same argument that put `is_bookable()` on `AvailableSlot` rather
    #: than in the two places that ask (plan 4.5). Offering a doctor the
    #: booking view then refuses is a bad page; refusing to offer one the
    #: booking view still accepts is a hole, and 10.3 exists because of the
    #: second.
    #:
    #: `is_approved` earns its place here as of decision F. It used to mean
    #: "an administrator has not got round to this account yet", which is no
    #: reason to hide a doctor. It now means "an administrator switched this
    #: account off", which is every reason (design §4.5).
    PUBLICLY_BOOKABLE = {
        "role": Role.DOCTOR,
        "is_active": True,
        "is_approved": True,
    }

    @classmethod
    def publicly_bookable_filter(cls, prefix=""):
        """`PUBLICLY_BOOKABLE` as field lookups, optionally through a relation.

        `prefix="user__"` filters slots by their doctor rather than filtering
        doctors, which is what the specialty counts need. Derived from the one
        dict rather than written out a second time — a copy is what lets the
        two drift, and the drift would be silent in exactly the direction that
        matters: a doctor hidden from the list but still counted as available.
        """
        return {prefix + field: value for field, value in cls.PUBLICLY_BOOKABLE.items()}

    @property
    def is_publicly_bookable(self):
        """The single-object form of the same rule, for `booking_form`."""
        return all(
            getattr(self, field) == value
            for field, value in self.PUBLICLY_BOOKABLE.items()
        )

    def __str__(self):
        return f"{self.username} ({self.email})"

class Specialty(models.Model):
    """What a doctor practises, as a closed set patients can search (§5.5).

    A model rather than `TextChoices` because AGKI must be able to add one
    without a deploy — a taxonomy that grows, unlike the fixed
    reason-for-visit list. Lives here rather than in `appointments` because it
    hangs off `Profile`, and `appointments` already imports this module;
    the reverse would be a circular import.

    Superuser-managed (design §4.3): it configures how patients find doctors,
    which is configuration, not shared identity.
    """

    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(unique=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Uncheck to retire a specialty without deleting it or the links to it.",
    )
    order = models.PositiveIntegerField(default=0, help_text="Position in the browse grid.")

    class Meta:
        verbose_name_plural = "specialties"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Qualification(models.Model):
    """What a doctor is qualified as - MD, MMed, Clinical Officer.

    A model rather than the free-text `CustomUser.qualifications` it replaces,
    for the reason `Specialty` is a model rather than `Profile.expertise`: the
    one free-text professional field anybody ever filled in holds
    **"Cardiograph"**, which is an ECG machine. A closed list is the only
    version of this that stays meaningful.

    The free-text field was not merely unreliable - it was **unused**. Every
    doctor in the database has it empty, which is what a field nobody can be
    sure how to fill in looks like after a year.

    Deliberately a sibling of `Specialty` rather than a variation on it: same
    four fields, same superuser-managed admin, same `is_active` retirement.
    Two taxonomies that behave differently are two things to learn.
    """

    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(unique=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Uncheck to retire a qualification without deleting it or the links to it.",
    )
    order = models.PositiveIntegerField(default=0, help_text="Position in the picker.")

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Profile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    gender = models.CharField(max_length=20, blank=True, null=True)
    bio = models.TextField(blank=True, null=True)
    location = models.CharField(max_length=100, blank=True, null=True)
    google_map_embed = models.TextField(blank=True, null=True)
    birth_date = models.DateField(blank=True, null=True)
    # Many-to-many because a doctor can hold more than one specialty, and
    # forcing a single value would make someone unfindable under half their
    # practice.
    specialties = models.ManyToManyField(Specialty, blank=True, related_name="doctors")
    # Picked, not typed - see `Qualification`. This replaces
    # `CustomUser.qualifications`, which stays for now with nothing writing to
    # it, the same way `expertise` below is being retired: the column is kept
    # for a release so a value can still be read, and a later migration drops
    # it. Neither is offered by any form.
    qualifications = models.ManyToManyField(
        Qualification, blank=True, related_name="doctors"
    )
    # Free text, and it shows the drift that guarantees: the one live value is
    # "Cardiograph", which is an ECG machine rather than a specialty. Being
    # replaced by `specialties` above; kept until the mapping is confirmed by
    # a human (plan 2.3 - see `map_expertise_to_specialties`), then dropped.
    expertise = models.CharField(max_length=255, blank=True, null=True)
    region = models.CharField(max_length=100, blank=True, null=True)
    years_of_experience = models.IntegerField(blank=True, null=True)
    languages_spoken = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return f"Profile of {self.user.username}"

#: Codes are drawn from 100000-999999 rather than 0-999999, so **no code ever
#: starts with a zero**.
#:
#: The old draw was `randbelow(10**6)` padded with `zfill(6)`, which is six
#: characters every time and looks correct. About one code in ten came out as
#: `068227` — six characters, five significant digits — and that is the shape
#: of value people and tools quietly shorten: a spreadsheet turns it into a
#: number, `parseInt` drops the zero, and somebody reading it off a terminal
#: types five digits into six boxes and is told their code is wrong. AGKI
#: reported exactly that on 2026-08-31, as "the code is 5 digits long".
#:
#: The cost is 10% of the keyspace — 900,000 codes instead of 1,000,000 — which
#: is worth nothing next to a code that reads unambiguously, and is not what
#: makes guessing hard here anyway. The attempt limit in `useraccount.otp` is.
OTP_LOW = 100_000
OTP_HIGH = 999_999


def generate_otp():
    """A six-digit code with no leading zero."""
    return str(OTP_LOW + secrets.randbelow(OTP_HIGH - OTP_LOW + 1))


class OtpToken(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    otp_code = models.CharField(max_length=6, default=generate_otp)
    otp_created_at = models.DateTimeField(auto_now_add=True)
    otp_expires_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"OTP for {self.user.email}: {self.otp_code}"

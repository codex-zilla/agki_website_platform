"""Move `Profile.expertise` free text onto the `Specialty` taxonomy (plan 2.3).

    python manage.py map_expertise_to_specialties            # report only
    python manage.py map_expertise_to_specialties --apply    # write the links

**Why this is a command and not a data migration.** Design §11 step 5a puts the
mapping in a migration, but a migration cannot do it correctly: the mapping
needs the taxonomy to exist, and the specialties are AGKI's to name (step 9.6).
A migration that ran at deploy time would find an empty `Specialty` table and
map nothing — or, worse, invent a taxonomy nobody asked for. So the schema
ships in a migration, the names are entered in the admin, and this runs
afterwards. It is idempotent and safe to re-run as more specialties are added.

**It will not guess.** Matching is exact after folding case and spacing:
"cardiology" finds `Cardiology`. Anything else is reported and left alone. The
one live value is `expertise='Cardiograph'` — a cardiograph is an ECG machine,
the specialty is Cardiology, and the difference between those two is a
judgement only a person should make. This codebase already knows what happens
when free text is guessed at: `role='4491'` silently removed a real doctor from
the booking page.

`expertise` is not cleared and not dropped. It stays until AGKI confirms every
mapping; dropping the column is a later migration, once this reports nothing.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from useraccount.models import Profile, Specialty


def normalise(value):
    return " ".join(str(value).strip().lower().replace("-", " ").replace("_", " ").split())


class Command(BaseCommand):
    help = "Map Profile.expertise onto the Specialty taxonomy, reporting anything ambiguous."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the links. Without this the command only reports.",
        )

    def handle(self, *args, **options):
        specialties = {normalise(s.name): s for s in Specialty.objects.all()}
        specialties.update({normalise(s.slug): s for s in Specialty.objects.all()})

        if not specialties:
            self.stdout.write(
                self.style.WARNING(
                    "No Specialty rows exist yet. Add AGKI's specialties in the "
                    "admin first (Django admin -> Useraccount -> Specialties), "
                    "then re-run this. Nothing has been changed."
                )
            )
            return

        profiles = (
            Profile.objects.exclude(expertise__isnull=True)
            .exclude(expertise__exact="")
            .select_related("user")
            .order_by("user__username")
        )

        mapped, unmapped, already = [], [], []

        for profile in profiles:
            match = specialties.get(normalise(profile.expertise))
            if match is None:
                unmapped.append(profile)
            elif profile.specialties.filter(pk=match.pk).exists():
                already.append((profile, match))
            else:
                mapped.append((profile, match))

        if options["apply"] and mapped:
            with transaction.atomic():
                for profile, specialty in mapped:
                    profile.specialties.add(specialty)

        verb = "linked" if options["apply"] else "would link"
        for profile, specialty in mapped:
            self.stdout.write(
                self.style.SUCCESS(
                    f"  {verb}: {profile.user.username} "
                    f"({profile.expertise!r}) -> {specialty.name}"
                )
            )
        for profile, specialty in already:
            self.stdout.write(f"  already linked: {profile.user.username} -> {specialty.name}")
        for profile in unmapped:
            self.stdout.write(
                self.style.WARNING(
                    f"  NEEDS A DECISION: {profile.user.username} has "
                    f"expertise={profile.expertise!r}, which matches no Specialty. "
                    f"Either add that specialty, or set the link by hand if it "
                    f"means one that already exists."
                )
            )

        self.stdout.write("")
        self.stdout.write(
            f"{len(mapped)} {verb}, {len(already)} already linked, "
            f"{len(unmapped)} need a human."
        )
        if mapped and not options["apply"]:
            self.stdout.write("Re-run with --apply to write these links.")
        if unmapped:
            self.stdout.write(
                "`expertise` stays in place until nothing is left needing a "
                "decision; dropping the column is a later migration."
            )

"""Move `Profile.expertise` free text onto the `Specialty` taxonomy (plan 2.3).

    python manage.py map_expertise_to_specialties            # report only
    python manage.py map_expertise_to_specialties --apply    # write the links
    python manage.py map_expertise_to_specialties --apply \\
        --map "Cardiograph=Cardiology"                       # a decided mapping

**Why this is a command and not a data migration.** Design §11 step 5a puts the
mapping in a migration, but a migration cannot do it correctly: the mapping
needs the taxonomy to exist, and the specialties are AGKI's to name (step 9.6).
A migration that ran at deploy time would find an empty `Specialty` table and
map nothing — or, worse, invent a taxonomy nobody asked for. So the schema
ships in a migration, the names are entered in the admin, and this runs
afterwards. It is idempotent and safe to re-run as more specialties are added.

**It will not guess.** Matching is exact after folding case and spacing:
"cardiology" finds `Cardiology`. Anything else is reported and left alone. This
codebase already knows what happens when free text is guessed at: `role='4491'`
silently removed a real doctor from the booking page.

**`--map "OLD=NEW"` is how a human's decision gets in.** It is deliberately not
a fuzzy matcher with a confidence threshold: somebody types the pairing out,
and it lands in the shell history and the deploy notes as an explicit act. The
one live value needed exactly this — `expertise='Cardiograph'` is an ECG
*machine*, the specialty is *Cardiology*, and no amount of string distance
makes that inference safe. AGKI decided it on 2026-08-17; the flag is how that
decision travels to production instead of being re-derived from memory.

`expertise` is not cleared and not dropped. It stays until AGKI confirms every
mapping; dropping the column is a later migration, once this reports nothing.
"""

from django.core.management.base import BaseCommand, CommandError
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
        parser.add_argument(
            "--map",
            action="append",
            default=[],
            metavar="OLD=SPECIALTY",
            dest="decided",
            help=(
                "A mapping a person has decided, e.g. "
                '--map "Cardiograph=Cardiology". Repeatable. The named '
                "specialty must already exist."
            ),
        )

    def handle(self, *args, **options):
        specialties = {normalise(s.name): s for s in Specialty.objects.all()}
        specialties.update({normalise(s.slug): s for s in Specialty.objects.all()})

        # Decided mappings are folded into the same lookup table, so from here
        # on there is no difference between "matched by name" and "a human said
        # so" — the decision is just another entry, and the reporting below
        # treats it identically.
        for pair in options["decided"]:
            if "=" not in pair:
                raise CommandError(f"--map needs the form OLD=SPECIALTY, got {pair!r}")
            old, _, target = pair.partition("=")
            specialty = specialties.get(normalise(target))
            if specialty is None:
                raise CommandError(
                    f"--map {pair!r} names the specialty {target!r}, which does "
                    f"not exist. Create it in the admin first — this command "
                    f"links to a taxonomy, it does not invent one."
                )
            specialties[normalise(old)] = specialty

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

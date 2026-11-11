from django.db import migrations
from django.utils.text import slugify

#: A starting list, not a taxonomy. AGKI adds and retires rows from the admin;
#: `is_active` retires one without deleting the links to it.
#:
#: Weighted towards the Tanzanian clinical ladder rather than a generic one:
#: Clinical Officer and Assistant Medical Officer are real cadres here and are
#: what several AGKI doctors would pick first.
QUALIFICATIONS = [
    "MD - Doctor of Medicine",
    "MBBS",
    "MMed",
    "Assistant Medical Officer",
    "Clinical Officer",
    "Diploma in Clinical Medicine",
    "BSc Nursing",
    "Registered Nurse",
    "BPharm",
    "BDS - Dentistry",
    "BSc Medical Laboratory Sciences",
    "MSc",
    "PhD",
    "Fellowship (FCS)",
]

SPECIALTIES = [
    "General Practice",
    "Internal Medicine",
    "Paediatrics",
    "Obstetrics & Gynaecology",
    "Surgery",
    "Cardiology",
    "Dermatology",
    "Ophthalmology",
    "ENT",
    "Orthopaedics",
    "Psychiatry",
    "Neurology",
    "Radiology",
    "Laboratory Medicine",
    "Dentistry",
    "Physiotherapy",
    "Nutrition & Dietetics",
    "Public Health",
]


def seed(apps, schema_editor):
    """Create what is missing, in the order given, touching nothing that exists.

    **Matched on `name`, not created blindly.** `Cardiology` is already in the
    live database - put there by `map_expertise_to_specialties` when AGKI
    decided "Cardiograph" meant Cardiology (plan 2.3) - and it has a doctor
    linked to it. Creating a second one would split that link across two rows
    that read identically in the admin.

    `order` is only set on rows this creates, so a row AGKI has already moved
    keeps its place.
    """
    Qualification = apps.get_model("useraccount", "Qualification")
    Specialty = apps.get_model("useraccount", "Specialty")

    for model, names in ((Qualification, QUALIFICATIONS), (Specialty, SPECIALTIES)):
        for position, name in enumerate(names, start=1):
            model.objects.get_or_create(
                name=name,
                defaults={"slug": slugify(name), "order": position},
            )


def unseed(apps, schema_editor):
    """Remove only the rows this migration created, and only if unused.

    A doctor may have linked themselves to one of these between the migration
    running and being rolled back. Deleting it would silently strip that from
    their profile, so a row with doctors attached is left alone - the same
    refusal `0019` makes about duplicate slots that have appointments.

    Anything AGKI has renamed is untouched too: this matches on the exact names
    it wrote.
    """
    Qualification = apps.get_model("useraccount", "Qualification")
    Specialty = apps.get_model("useraccount", "Specialty")

    for model, names in ((Qualification, QUALIFICATIONS), (Specialty, SPECIALTIES)):
        for row in model.objects.filter(name__in=names):
            if not row.doctors.exists():
                row.delete()


class Migration(migrations.Migration):
    """Seed both picked lists - plan 11.6, at AGKI's request of 2026-09-01.

    Until that request, `SpecialtyAdmin` said in as many words that seeding
    this table "would be putting words in their mouth", and it was right to.
    The request changes that, and the admin docstring is corrected alongside
    this migration rather than left contradicting it.
    """

    dependencies = [
        ("useraccount", "0012_qualification"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]

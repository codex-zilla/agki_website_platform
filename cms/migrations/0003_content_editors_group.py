# Data migration: creates the "Content Editors" permission group described
# in docs/website-cms-design.md §5. Kept as a migration (not a one-off
# shell command) so it exists automatically in every environment this
# project is deployed to, production included — nobody has to remember to
# click it together by hand.
#
# Deliberately uses get_or_create() for ContentType/Permission lookups
# instead of .get()/.filter(). Both rows are normally created by Django's
# post_migrate signal (contenttypes' create_contenttypes, auth's
# create_permissions) — but that signal only fires once *all* migrations
# in a `migrate` run finish, not incrementally after each app. On a brand
# new environment where this migration runs in the same invocation as the
# ones that create the cms models (0001/0002), a plain .get() would find
# nothing yet and silently produce an empty permission set. get_or_create()
# sidesteps the ordering entirely: if the "official" rows already exist it
# just finds them (matched on the same content_type + codename Django's own
# post_migrate handler would have used), and if they don't exist yet it
# creates them itself.

from django.db import migrations

# Models where content editors get the full CRUD Django admin can express.
FULL_CRUD_MODELS = [
    "heroimpactstat",
    "corevalue",
    "problemcard",
    "problemstat",
    "featurerow",
    "awardlogo",
    "testimonial",
    "communityphoto",
    "newspost",
    "faqitem",
    "teammember",
]

# Models where add/delete are pointless to grant because something else
# already blocks them unconditionally, regardless of the permission bit:
#   - the four singletons: SingletonModelAdmin.has_add_permission and
#     has_delete_permission always return False (see cms/admin.py)
#   - ContactMessage: an inbox filled by the public contact form, not
#     staff-authored content (design doc §1, §6) — add is always blocked,
#     delete is restricted to superusers in ContactMessageAdmin
VIEW_CHANGE_ONLY_MODELS = [
    "sitesettings",
    "herocontent",
    "aboutcontent",
    "problemsectioncontent",
    "contactmessage",
]

GROUP_NAME = "Content Editors"


def _get_or_create_permission(Permission, ContentType, model_name, action):
    content_type, _ = ContentType.objects.get_or_create(
        app_label="cms",
        model=model_name,
    )
    codename = f"{action}_{model_name}"
    permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename=codename,
        defaults={"name": f"Can {action} {model_name}"},
    )
    return permission


def create_content_editors_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    group, _ = Group.objects.get_or_create(name=GROUP_NAME)

    permissions = []
    for model_name in FULL_CRUD_MODELS:
        for action in ("add", "change", "delete", "view"):
            permissions.append(
                _get_or_create_permission(Permission, ContentType, model_name, action)
            )
    for model_name in VIEW_CHANGE_ONLY_MODELS:
        for action in ("change", "view"):
            permissions.append(
                _get_or_create_permission(Permission, ContentType, model_name, action)
            )

    group.permissions.set(permissions)


def remove_content_editors_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=GROUP_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("cms", "0002_alter_aboutcontent_story_copy_alter_faqitem_answer_and_more"),
        ("auth", "0001_initial"),
        ("contenttypes", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_content_editors_group, remove_content_editors_group),
    ]

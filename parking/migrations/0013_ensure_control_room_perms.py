# Ensure Control Room group has decide + view access-request permissions

from django.db import migrations


CONTROL_ROOM_GROUP = "Control Room"

PERMS = (
    ("view_accessrequest", "Can view access request"),
    ("decide_accessrequest", "Can approve or deny access requests"),
)


def ensure_control_room(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    ct, _ = ContentType.objects.get_or_create(
        app_label="parking", model="accessrequest"
    )
    perms = []
    for codename, name in PERMS:
        perm, _ = Permission.objects.get_or_create(
            content_type=ct,
            codename=codename,
            defaults={"name": name},
        )
        if perm.name != name:
            perm.name = name
            perm.save(update_fields=["name"])
        perms.append(perm)

    group, _ = Group.objects.get_or_create(name=CONTROL_ROOM_GROUP)
    group.permissions.add(*perms)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("parking", "0012_sessionauditentry"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(ensure_control_room, noop),
    ]

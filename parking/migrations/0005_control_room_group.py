from django.db import migrations


CONTROL_ROOM_GROUP = "Control Room"

# Permissions may not exist yet during RunPython (post_migrate creates them later).
GATEHOLD_PERMS = (
    ("add_gatehold", "Can add gate hold"),
    ("change_gatehold", "Can change gate hold"),
    ("delete_gatehold", "Can delete gate hold"),
    ("view_gatehold", "Can view gate hold"),
    ("decide_gatehold", "Can approve or deny gate holds"),
)


def create_control_room_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    ct, _ = ContentType.objects.get_or_create(app_label="parking", model="gatehold")
    perms = []
    for codename, name in GATEHOLD_PERMS:
        perm, _ = Permission.objects.get_or_create(
            content_type=ct,
            codename=codename,
            defaults={"name": name},
        )
        perms.append(perm)

    group, _ = Group.objects.get_or_create(name=CONTROL_ROOM_GROUP)
    group.permissions.add(
        *[p for p in perms if p.codename in ("view_gatehold", "decide_gatehold")]
    )


def remove_control_room_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=CONTROL_ROOM_GROUP).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("parking", "0004_gatehold_and_control_room"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(create_control_room_group, remove_control_room_group),
    ]

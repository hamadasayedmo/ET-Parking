import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


CONTROL_ROOM_GROUP = "Control Room"

OLD_PERMS = (
    "add_gateapproval",
    "change_gateapproval",
    "delete_gateapproval",
    "view_gateapproval",
    "decide_gateapproval",
)

NEW_PERMS = (
    ("add_accessrequest", "Can add access request"),
    ("change_accessrequest", "Can change access request"),
    ("delete_accessrequest", "Can delete access request"),
    ("view_accessrequest", "Can view access request"),
    ("decide_accessrequest", "Can approve or deny access requests"),
)


def rename_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    ct = ContentType.objects.filter(app_label="parking", model="accessrequest").first()
    if ct is None:
        ct, _ = ContentType.objects.get_or_create(
            app_label="parking", model="accessrequest"
        )

    Permission.objects.filter(
        content_type__app_label="parking",
        codename__in=OLD_PERMS,
    ).delete()

    new_perms = []
    for codename, name in NEW_PERMS:
        perm, _ = Permission.objects.get_or_create(
            content_type=ct,
            codename=codename,
            defaults={"name": name},
        )
        if perm.name != name:
            perm.name = name
            perm.save(update_fields=["name"])
        new_perms.append(perm)

    group, _ = Group.objects.get_or_create(name=CONTROL_ROOM_GROUP)
    group.permissions.clear()
    group.permissions.add(
        *[
            p
            for p in new_perms
            if p.codename in ("view_accessrequest", "decide_accessrequest")
        ]
    )


def revert_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    ct = ContentType.objects.filter(app_label="parking", model="gateapproval").first()
    if ct is None:
        return

    old = (
        ("add_gateapproval", "Can add gate approval"),
        ("change_gateapproval", "Can change gate approval"),
        ("delete_gateapproval", "Can delete gate approval"),
        ("view_gateapproval", "Can view gate approval"),
        ("decide_gateapproval", "Can approve or deny gate approvals"),
    )
    Permission.objects.filter(
        content_type__app_label="parking",
        codename__in=[c for c, _ in NEW_PERMS],
    ).delete()

    perms = []
    for codename, name in old:
        perm, _ = Permission.objects.get_or_create(
            content_type=ct,
            codename=codename,
            defaults={"name": name},
        )
        perms.append(perm)

    group = Group.objects.filter(name=CONTROL_ROOM_GROUP).first()
    if group is not None:
        group.permissions.clear()
        group.permissions.add(
            *[
                p
                for p in perms
                if p.codename in ("view_gateapproval", "decide_gateapproval")
            ]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("parking", "0007_rename_gatehold_to_gateapproval"),
        ("sites", "0006_site_hourly_rate_additional_fee"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RenameModel(old_name="GateApproval", new_name="AccessRequest"),
        migrations.AlterModelOptions(
            name="accessrequest",
            options={
                "ordering": ["-created_at"],
                "permissions": [
                    ("decide_accessrequest", "Can approve or deny access requests")
                ],
                "verbose_name": "access request",
                "verbose_name_plural": "access requests (control room)",
            },
        ),
        migrations.RemoveConstraint(
            model_name="accessrequest",
            name="uniq_pending_gateapproval_per_device",
        ),
        migrations.AlterField(
            model_name="accessrequest",
            name="decided_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="access_request_decisions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="accessrequest",
            name="device",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="access_requests",
                to="sites.device",
            ),
        ),
        migrations.AlterField(
            model_name="accessrequest",
            name="resolution_event",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="resolved_access_request",
                to="parking.event",
            ),
        ),
        migrations.AddConstraint(
            model_name="accessrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "pending")),
                fields=("device",),
                name="uniq_pending_accessrequest_per_device",
            ),
        ),
        migrations.RunPython(rename_permissions, revert_permissions),
    ]

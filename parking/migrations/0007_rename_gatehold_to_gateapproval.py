import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


CONTROL_ROOM_GROUP = "Control Room"

OLD_PERMS = (
    "add_gatehold",
    "change_gatehold",
    "delete_gatehold",
    "view_gatehold",
    "decide_gatehold",
)

NEW_PERMS = (
    ("add_gateapproval", "Can add gate approval"),
    ("change_gateapproval", "Can change gate approval"),
    ("delete_gateapproval", "Can delete gate approval"),
    ("view_gateapproval", "Can view gate approval"),
    ("decide_gateapproval", "Can approve or deny gate approvals"),
)


def rename_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    # RenameModel updates ContentType.model to gateapproval.
    ct = ContentType.objects.filter(app_label="parking", model="gateapproval").first()
    if ct is None:
        ct, _ = ContentType.objects.get_or_create(
            app_label="parking", model="gateapproval"
        )

    # Drop stale gatehold permissions if ContentType still points elsewhere.
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
            if p.codename in ("view_gateapproval", "decide_gateapproval")
        ]
    )


def revert_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    ct = ContentType.objects.filter(app_label="parking", model="gatehold").first()
    if ct is None:
        return

    old = (
        ("add_gatehold", "Can add gate hold"),
        ("change_gatehold", "Can change gate hold"),
        ("delete_gatehold", "Can delete gate hold"),
        ("view_gatehold", "Can view gate hold"),
        ("decide_gatehold", "Can approve or deny gate holds"),
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
            *[p for p in perms if p.codename in ("view_gatehold", "decide_gatehold")]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("parking", "0006_gatehold_verbose_name"),
        ("sites", "0006_site_hourly_rate_additional_fee"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RenameModel(old_name="GateHold", new_name="GateApproval"),
        migrations.AlterModelOptions(
            name="gateapproval",
            options={
                "ordering": ["-created_at"],
                "permissions": [
                    ("decide_gateapproval", "Can approve or deny gate approvals")
                ],
                "verbose_name": "gate approval",
                "verbose_name_plural": "gate approvals (control room)",
            },
        ),
        migrations.RemoveConstraint(
            model_name="gateapproval",
            name="uniq_pending_gatehold_per_device",
        ),
        migrations.AlterField(
            model_name="gateapproval",
            name="decided_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="gate_approval_decisions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="gateapproval",
            name="device",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="gate_approvals",
                to="sites.device",
            ),
        ),
        migrations.AlterField(
            model_name="gateapproval",
            name="resolution_event",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="resolved_approval",
                to="parking.event",
            ),
        ),
        migrations.RenameIndex(
            model_name="gateapproval",
            new_name="parking_gat_status_d8c7f4_idx",
            old_name="parking_gat_status_c530c2_idx",
        ),
        migrations.RenameIndex(
            model_name="gateapproval",
            new_name="parking_gat_device__c1e9aa_idx",
            old_name="parking_gat_device__c6a492_idx",
        ),
        migrations.AddConstraint(
            model_name="gateapproval",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "pending")),
                fields=("device",),
                name="uniq_pending_gateapproval_per_device",
            ),
        ),
        migrations.RunPython(rename_permissions, revert_permissions),
    ]

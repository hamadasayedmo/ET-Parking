# Create Operations / Wallet groups and adjust_wallet permission

from django.db import migrations


OPERATIONS_GROUP = "Operations"
WALLET_GROUP = "Wallet"


def create_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    wallet_ct, _ = ContentType.objects.get_or_create(
        app_label="wallets", model="wallet"
    )
    adjust, _ = Permission.objects.get_or_create(
        content_type=wallet_ct,
        codename="adjust_wallet",
        defaults={"name": "Can top up or withdraw wallet balances"},
    )
    view_wallet, _ = Permission.objects.get_or_create(
        content_type=wallet_ct,
        codename="view_wallet",
        defaults={"name": "Can view wallet"},
    )

    tx_ct, _ = ContentType.objects.get_or_create(
        app_label="wallets", model="transaction"
    )
    view_tx, _ = Permission.objects.get_or_create(
        content_type=tx_ct,
        codename="view_transaction",
        defaults={"name": "Can view transaction"},
    )

    ops, _ = Group.objects.get_or_create(name=OPERATIONS_GROUP)
    ops.permissions.add(view_wallet, view_tx)

    wallet_group, _ = Group.objects.get_or_create(name=WALLET_GROUP)
    wallet_group.permissions.add(adjust, view_wallet, view_tx)


def remove_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=(OPERATIONS_GROUP, WALLET_GROUP)).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0006_client_is_exempted"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="wallet",
            options={
                "permissions": (
                    ("adjust_wallet", "Can top up or withdraw wallet balances"),
                ),
            },
        ),
        migrations.RunPython(create_groups, remove_groups),
    ]

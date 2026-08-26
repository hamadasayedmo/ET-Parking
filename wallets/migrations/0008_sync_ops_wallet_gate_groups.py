# Ensure Operations / Wallet / Control Room permissions stay separated.

from django.db import migrations

OPERATIONS_GROUP = "Operations"
WALLET_GROUP = "Wallet"
CONTROL_ROOM_GROUP = "Control Room"


def sync_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    wallet_ct, _ = ContentType.objects.get_or_create(
        app_label="wallets", model="wallet"
    )
    tx_ct, _ = ContentType.objects.get_or_create(
        app_label="wallets", model="transaction"
    )
    ar_ct, _ = ContentType.objects.get_or_create(
        app_label="parking", model="accessrequest"
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
    view_tx, _ = Permission.objects.get_or_create(
        content_type=tx_ct,
        codename="view_transaction",
        defaults={"name": "Can view transaction"},
    )
    view_ar, _ = Permission.objects.get_or_create(
        content_type=ar_ct,
        codename="view_accessrequest",
        defaults={"name": "Can view access request"},
    )
    decide_ar, _ = Permission.objects.get_or_create(
        content_type=ar_ct,
        codename="decide_accessrequest",
        defaults={"name": "Can approve or deny access requests"},
    )

    ops, _ = Group.objects.get_or_create(name=OPERATIONS_GROUP)
    # Operations: dashboard wallet ledger reads only — no adjust, no gate decide.
    ops.permissions.set([view_wallet, view_tx])

    wallet_group, _ = Group.objects.get_or_create(name=WALLET_GROUP)
    # Wallet: everything Operations has, plus adjust.
    wallet_group.permissions.set([view_wallet, view_tx, adjust])

    control, _ = Group.objects.get_or_create(name=CONTROL_ROOM_GROUP)
    control.permissions.set([view_ar, decide_ar])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0007_operations_wallet_groups"),
        ("parking", "0015_zone_nested_billing"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(sync_groups, noop),
    ]

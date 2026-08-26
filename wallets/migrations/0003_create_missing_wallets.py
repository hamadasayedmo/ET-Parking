from decimal import Decimal

from django.db import migrations


def create_missing_wallets(apps, schema_editor):
    Client = apps.get_model("wallets", "Client")
    Wallet = apps.get_model("wallets", "Wallet")
    existing = set(Wallet.objects.values_list("client_id", flat=True))
    for client in Client.objects.exclude(pk__in=existing):
        Wallet.objects.create(client=client, balance=Decimal("0.00"))


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0002_wallet_transaction"),
    ]

    operations = [
        migrations.RunPython(create_missing_wallets, noop),
    ]

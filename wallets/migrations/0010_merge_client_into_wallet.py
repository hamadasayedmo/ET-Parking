import django.db.models.deletion
from django.db import migrations, models


def forwards_merge(apps, schema_editor):
    Client = apps.get_model("wallets", "Client")
    Wallet = apps.get_model("wallets", "Wallet")
    Vehicle = apps.get_model("wallets", "Vehicle")

    for client in Client.objects.all().iterator():
        wallet = Wallet.objects.filter(client_id=client.pk).first()
        if wallet is None:
            Wallet.objects.create(
                client_id=client.pk,
                project_id=client.project_id,
                name=client.name,
                is_active=client.is_active,
                is_exempted=client.is_exempted,
            )
        else:
            wallet.project_id = client.project_id
            wallet.name = client.name
            wallet.is_active = client.is_active
            wallet.is_exempted = client.is_exempted
            wallet.save(
                update_fields=[
                    "project_id",
                    "name",
                    "is_active",
                    "is_exempted",
                ]
            )

    for vehicle in Vehicle.objects.all().iterator():
        wallet = Wallet.objects.filter(client_id=vehicle.client_id).first()
        if wallet is None:
            raise RuntimeError(
                f"Vehicle {vehicle.pk} client {vehicle.client_id} has no wallet"
            )
        vehicle.wallet_id = wallet.pk
        vehicle.save(update_fields=["wallet_id"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0009_project_country_seller_vat"),
        ("wallets", "0009_transaction_created_by"),
        # MobileProfile/ClientDevice still FK wallets.Client until payments.0008.
        # Without this, fresh migrate deletes Client while those models remain
        # and raises: Related model 'wallets.client' cannot be resolved.
        ("payments", "0008_remove_mobile_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="wallet",
            name="project",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="wallets",
                to="projects.project",
            ),
        ),
        migrations.AddField(
            model_name="wallet",
            name="name",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="wallet",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="wallet",
            name="is_exempted",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, all vehicles and sessions for this wallet are billing-exempt.",
            ),
        ),
        migrations.AddField(
            model_name="vehicle",
            name="wallet",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="vehicles",
                to="wallets.wallet",
            ),
        ),
        migrations.RunPython(forwards_merge, noop_reverse),
        migrations.AlterField(
            model_name="wallet",
            name="project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="wallets",
                to="projects.project",
            ),
        ),
        migrations.AlterField(
            model_name="wallet",
            name="name",
            field=models.CharField(max_length=200),
        ),
        migrations.AlterField(
            model_name="vehicle",
            name="wallet",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="vehicles",
                to="wallets.wallet",
            ),
        ),
        migrations.RemoveField(
            model_name="vehicle",
            name="client",
        ),
        migrations.RemoveField(
            model_name="wallet",
            name="client",
        ),
        migrations.DeleteModel(
            name="Client",
        ),
    ]

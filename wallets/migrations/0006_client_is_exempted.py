# Generated manually for Client.is_exempted

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0005_client_project"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="is_exempted",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, all vehicles and sessions for this client are billing-exempt.",
            ),
        ),
    ]

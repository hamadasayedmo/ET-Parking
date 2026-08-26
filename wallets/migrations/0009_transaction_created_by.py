# Generated manually for Transaction.created_by

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("wallets", "0008_sync_ops_wallet_gate_groups"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                help_text="Staff user who topped up or withdrew; blank for system parking fees.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="wallet_transactions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]

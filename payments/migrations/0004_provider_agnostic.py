# Provider-agnostic provider field (was NearPay-only choices).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0003_nearpay_mobile"),
    ]

    operations = [
        migrations.AlterField(
            model_name="paymentintent",
            name="provider",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]

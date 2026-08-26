from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "sites",
            "0005_rename_sites_devic_site_id_type_idx_sites_devic_site_id_a7dbb5_idx",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="site",
            name="hourly_rate",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("2.00"),
                help_text="Billed per started hour (ceil, minimum 1 hour).",
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name="site",
            name="additional_fee",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("1.00"),
                help_text="Flat fee added to every parking session.",
                max_digits=10,
            ),
        ),
    ]

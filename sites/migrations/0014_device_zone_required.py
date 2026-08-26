from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0013_backfill_default_zones"),
    ]

    operations = [
        migrations.AlterField(
            model_name="device",
            name="zone",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="devices",
                to="sites.zone",
            ),
        ),
    ]

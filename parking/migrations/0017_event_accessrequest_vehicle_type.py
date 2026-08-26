# Generated manually for optional ANPR vehicle_type from gateways

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("parking", "0016_session_wallet"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="vehicle_type",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Optional ANPR vehicle type from the gateway (e.g. car, truck).",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="accessrequest",
            name="vehicle_type",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Optional ANPR vehicle type from the gateway (e.g. car, truck).",
                max_length=32,
            ),
        ),
    ]

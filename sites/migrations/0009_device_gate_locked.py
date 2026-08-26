# Generated manually for gate_locked

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sites", "0008_device_ordering"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="gate_locked",
            field=models.BooleanField(
                default=False,
                help_text="When locked, edge should ignore auto-open until unlocked.",
            ),
        ),
    ]

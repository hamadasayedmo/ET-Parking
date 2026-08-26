from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sites", "0009_device_gate_locked"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Optional label (e.g. Entry North).",
                max_length=100,
            ),
        ),
    ]

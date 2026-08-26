from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0010_device_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="site",
            name="is_active",
            field=models.BooleanField(
                default=True,
                help_text="Inactive sites ignore camera events and do not open gates.",
            ),
        ),
    ]

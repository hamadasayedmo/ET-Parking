from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("parking", "0017_event_accessrequest_vehicle_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="session",
            name="paid_exit_until",
            field=models.DateTimeField(
                blank=True,
                help_text="A paid card session may auto-exit through this timestamp.",
                null=True,
            ),
        ),
    ]

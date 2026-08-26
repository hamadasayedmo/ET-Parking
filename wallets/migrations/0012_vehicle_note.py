from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0011_rename_app_subscriptions_to_wallets"),
    ]

    operations = [
        migrations.AddField(
            model_name="vehicle",
            name="note",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Optional operator note shown on the vehicle card.",
                max_length=255,
            ),
        ),
    ]

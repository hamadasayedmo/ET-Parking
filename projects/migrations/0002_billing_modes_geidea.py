# Generated manually for billing flags + Geidea credentials

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="wallet_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Prepaid wallet / subscription auto-access.",
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="pay_at_exit_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Unknown/underfunded plates enter freely; exit pays via Geidea.",
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="geidea_merchant_public_key",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Overrides GEIDEA_MERCHANT_PUBLIC_KEY env when set.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="geidea_api_password",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Overrides GEIDEA_API_PASSWORD env when set.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="geidea_currency",
            field=models.CharField(blank=True, default="EGP", max_length=3),
        ),
    ]

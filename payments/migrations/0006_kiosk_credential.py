# Generated manually for KioskCredential

import django.db.models.deletion
import payments.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "payments",
            "0005_rename_payments_pa_status_7c1c8e_idx_payments_pa_status_79eeea_idx_and_more",
        ),
        ("sites", "0016_rename_default_zones_to_main"),
    ]

    operations = [
        migrations.CreateModel(
            name="KioskCredential",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "token",
                    models.CharField(
                        default=payments.models.generate_kiosk_token,
                        editable=False,
                        help_text="Secret API token for the kiosk app (copy once into device env).",
                        max_length=64,
                        unique=True,
                    ),
                ),
                (
                    "nearpay_terminal_id",
                    models.CharField(
                        help_text="NearPay Tid for this exit (used when minting JWT for the kiosk).",
                        max_length=64,
                    ),
                ),
                ("label", models.CharField(blank=True, default="", max_length=100)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "device",
                    models.OneToOneField(
                        help_text="Must be an exit device.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="kiosk_credential",
                        to="sites.device",
                    ),
                ),
            ],
            options={
                "verbose_name": "kiosk credential",
                "verbose_name_plural": "kiosk credentials",
            },
        ),
    ]

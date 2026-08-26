# Generated manually for GeideaTerminal + PaymentIntent.terminal

import django.db.models.deletion
from django.db import migrations, models

import payments.models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0001_initial"),
        ("sites", "0010_device_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="GeideaTerminal",
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
                ("name", models.CharField(max_length=100)),
                (
                    "api_token",
                    models.CharField(
                        default=payments.models.generate_terminal_token,
                        editable=False,
                        help_text="Send as header X-Geidea-Terminal-Token from the SoftPOS app.",
                        max_length=64,
                        unique=True,
                    ),
                ),
                (
                    "external_terminal_id",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Optional Geidea / SoftPOS device id for your records.",
                        max_length=128,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "exit_device",
                    models.OneToOneField(
                        help_text="Must be an exit camera/barrier device.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="geidea_terminal",
                        to="sites.device",
                    ),
                ),
            ],
            options={
                "verbose_name": "Geidea terminal",
                "verbose_name_plural": "Geidea terminals",
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="paymentintent",
            name="terminal",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="payment_intents",
                to="payments.geideaterminal",
            ),
        ),
    ]

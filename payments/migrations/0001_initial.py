# Generated manually for PaymentIntent

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("parking", "0011_session_billing_method"),
        ("projects", "0002_billing_modes_geidea"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaymentIntent",
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
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("currency", models.CharField(default="EGP", max_length=3)),
                ("merchant_reference_id", models.CharField(max_length=64, unique=True)),
                (
                    "geidea_session_id",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "geidea_order_id",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("pending", "Pending"),
                            ("paid", "Paid"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                        ],
                        db_index=True,
                        default="created",
                        max_length=16,
                    ),
                ),
                ("raw_callback", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                (
                    "access_request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="payment_intents",
                        to="parking.accessrequest",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payment_intents",
                        to="projects.project",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payment_intents",
                        to="parking.session",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["status", "created_at"],
                        name="payments_pa_status_7c1c8e_idx",
                    ),
                    models.Index(
                        fields=["geidea_session_id"],
                        name="payments_pa_geidea__8b2e1a_idx",
                    ),
                ],
            },
        ),
    ]

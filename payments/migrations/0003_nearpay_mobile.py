# NearPay mobile: reshape PaymentIntent, drop GeideaTerminal, add mobile models

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0002_geidea_terminal"),
        ("projects", "0003_nearpay_currency"),
        ("wallets", "0005_client_project"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveField(model_name="paymentintent", name="terminal"),
        migrations.RemoveIndex(
            model_name="paymentintent",
            name="payments_pa_geidea__8b2e1a_idx",
        ),
        migrations.RemoveField(model_name="paymentintent", name="geidea_session_id"),
        migrations.RemoveField(model_name="paymentintent", name="geidea_order_id"),
        migrations.RenameField(
            model_name="paymentintent",
            old_name="raw_callback",
            new_name="raw_result",
        ),
        migrations.AddField(
            model_name="paymentintent",
            name="provider",
            field=models.CharField(
                choices=[("nearpay", "NearPay")],
                default="nearpay",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="paymentintent",
            name="provider_transaction_id",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=128
            ),
        ),
        migrations.AlterField(
            model_name="paymentintent",
            name="currency",
            field=models.CharField(default="SAR", max_length=3),
        ),
        migrations.DeleteModel(name="GeideaTerminal"),
        migrations.CreateModel(
            name="MobileProfile",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mobile_profiles",
                        to="wallets.client",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mobile_profile",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "mobile profile",
                "verbose_name_plural": "mobile profiles",
            },
        ),
        migrations.CreateModel(
            name="ClientDevice",
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
                ("fcm_token", models.CharField(max_length=512, unique=True)),
                (
                    "platform",
                    models.CharField(
                        choices=[
                            ("android", "Android"),
                            ("ios", "iOS"),
                            ("other", "Other"),
                        ],
                        default="android",
                        max_length=16,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="devices",
                        to="wallets.client",
                    ),
                ),
            ],
            options={
                "ordering": ["-updated_at"],
            },
        ),
        migrations.AddIndex(
            model_name="paymentintent",
            index=models.Index(
                fields=["provider", "provider_transaction_id"],
                name="payments_pa_provide_idx",
            ),
        ),
    ]

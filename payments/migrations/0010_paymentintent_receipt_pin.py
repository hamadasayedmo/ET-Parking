# PaymentIntent.receipt_pin for public plate+pin receipt lookup.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0009_paymentintent_receipt_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentintent",
            name="receipt_pin",
            field=models.CharField(
                blank=True,
                default="",
                help_text="PIN for public receipt lookup (plate + pin → last session only).",
                max_length=8,
            ),
        ),
    ]

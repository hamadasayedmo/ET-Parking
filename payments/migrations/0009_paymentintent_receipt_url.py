# PaymentIntent.receipt_url — external page URL for kiosk QR.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0008_remove_mobile_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentintent",
            name="receipt_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="External receipt page URL shown as QR on the kiosk (hosted elsewhere).",
                max_length=500,
            ),
        ),
    ]

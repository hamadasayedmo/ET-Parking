# Rename receipt_pin → pin; drop receipt_url (client builds URL from base + plate + pin).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0010_paymentintent_receipt_pin"),
    ]

    operations = [
        migrations.RenameField(
            model_name="paymentintent",
            old_name="receipt_pin",
            new_name="pin",
        ),
        migrations.AlterField(
            model_name="paymentintent",
            name="pin",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Last 4 digits of the card used to pay (public receipt lookup).",
                max_length=8,
            ),
        ),
        migrations.RemoveField(
            model_name="paymentintent",
            name="receipt_url",
        ),
    ]

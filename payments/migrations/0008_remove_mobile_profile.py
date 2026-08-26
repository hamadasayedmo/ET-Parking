# Generated for removing MobileProfile + ClientDevice

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0007_kioskpairingcode"),
    ]

    operations = [
        migrations.DeleteModel(name="ClientDevice"),
        migrations.DeleteModel(name="MobileProfile"),
    ]

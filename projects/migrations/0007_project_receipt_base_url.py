# Project receipt_base_url for external receipt hosts.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0006_project_powered_by_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="receipt_base_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text=(
                    "External receipt host (no trailing slash). "
                    "New intents get receipt_url = {base}/{merchant_reference_id}. "
                    "Example: https://receipts.example.com/r"
                ),
                max_length=500,
            ),
        ),
    ]

from django.db import migrations, models


def backfill_country_sa(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    Project.objects.filter(country="").update(country="SA")
    Project.objects.filter(country__isnull=True).update(country="SA")


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0008_remove_project_kiosk_branding"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="country",
            field=models.CharField(
                db_index=True,
                default="SA",
                help_text="ISO 3166-1 alpha-2 country code (default SA).",
                max_length=2,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="seller_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Legal seller name for tax receipts (e.g. ZATCA Phase-1 QR).",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="project",
            name="vat_number",
            field=models.CharField(
                blank=True,
                default="",
                help_text="VAT / tax registration number for tax receipts.",
                max_length=32,
            ),
        ),
        migrations.RunPython(backfill_country_sa, migrations.RunPython.noop),
    ]

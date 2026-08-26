# Project currency for NearPay; drop Geidea merchant fields

from django.db import migrations, models


def copy_geidea_currency_to_currency(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    for project in Project.objects.all().iterator():
        currency = getattr(project, "geidea_currency", None) or "SAR"
        Project.objects.filter(pk=project.pk).update(currency=currency or "SAR")


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0002_billing_modes_geidea"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="currency",
            field=models.CharField(
                blank=True,
                default="SAR",
                help_text="Currency for pay-at-exit / NearPay amounts.",
                max_length=3,
            ),
        ),
        migrations.RunPython(copy_geidea_currency_to_currency, migrations.RunPython.noop),
        migrations.RemoveField(model_name="project", name="geidea_api_password"),
        migrations.RemoveField(model_name="project", name="geidea_merchant_public_key"),
        migrations.RemoveField(model_name="project", name="geidea_currency"),
        migrations.AlterField(
            model_name="project",
            name="pay_at_exit_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Unknown/underfunded plates enter freely; "
                    "exit pays via mobile NearPay."
                ),
            ),
        ),
    ]

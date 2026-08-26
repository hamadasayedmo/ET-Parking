from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0005_grace_on_site_and_project"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="powered_by_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Shown on exit kiosks as “Powered by …”.",
                max_length=100,
            ),
        ),
    ]

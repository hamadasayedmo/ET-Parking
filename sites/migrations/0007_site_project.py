import django.db.models.deletion
from django.db import migrations, models


def assign_default_project(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    Site = apps.get_model("sites", "Site")

    project, _ = Project.objects.get_or_create(
        name="Default",
        defaults={"is_active": True},
    )
    Site.objects.filter(project__isnull=True).update(project=project)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("sites", "0006_site_hourly_rate_additional_fee"),
        ("projects", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="site",
            name="project",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sites",
                to="projects.project",
            ),
        ),
        migrations.RunPython(assign_default_project, noop),
        migrations.AlterField(
            model_name="site",
            name="project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sites",
                to="projects.project",
            ),
        ),
        migrations.AlterModelOptions(
            name="site",
            options={"ordering": ["name"]},
        ),
    ]

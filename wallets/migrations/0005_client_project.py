# Generated manually — scope clients to a project

import django.db.models.deletion
from django.db import migrations, models


def assign_clients_to_project(apps, schema_editor):
    Client = apps.get_model("wallets", "Client")
    Project = apps.get_model("projects", "Project")
    project = Project.objects.order_by("id").first()
    if project is None:
        project = Project.objects.create(name="Default", is_active=True)
    Client.objects.filter(project__isnull=True).update(project_id=project.id)


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0002_billing_modes_geidea"),
        ("wallets", "0004_alter_wallet_currency"),
    ]

    operations = [
        migrations.AddField(
            model_name="client",
            name="project",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="clients",
                to="projects.project",
            ),
        ),
        migrations.RunPython(assign_clients_to_project, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="client",
            name="project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="clients",
                to="projects.project",
            ),
        ),
    ]

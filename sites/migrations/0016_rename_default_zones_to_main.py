# Rename legacy "Default" zones to "Main".

from django.db import migrations


def forwards(apps, schema_editor):
    Zone = apps.get_model("sites", "Zone")
    Zone.objects.filter(name="Default").update(name="Main")


def backwards(apps, schema_editor):
    Zone = apps.get_model("sites", "Zone")
    Zone.objects.filter(name="Main", parent_id__isnull=True).update(name="Default")


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0015_grace_on_site_and_project"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

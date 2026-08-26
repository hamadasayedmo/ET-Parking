# Move powered_by / receipt base URL off Project (now Django settings).

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0007_project_receipt_base_url"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="project",
            name="powered_by_name",
        ),
        migrations.RemoveField(
            model_name="project",
            name="receipt_base_url",
        ),
    ]

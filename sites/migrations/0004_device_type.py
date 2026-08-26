from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sites", "0003_site_iot_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="type",
            field=models.CharField(
                choices=[("entry", "Entry"), ("exit", "Exit")],
                default="entry",
                max_length=8,
            ),
        ),
        migrations.AddIndex(
            model_name="device",
            index=models.Index(
                fields=["site", "type"], name="sites_devic_site_id_type_idx"
            ),
        ),
    ]

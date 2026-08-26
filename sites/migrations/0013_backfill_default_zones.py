# Generated manually — backfill default zones and attach devices/sessions.

from django.db import migrations


def forwards(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Zone = apps.get_model("sites", "Zone")
    Device = apps.get_model("sites", "Device")
    Session = apps.get_model("parking", "Session")

    for site in Site.objects.all():
        zone, _ = Zone.objects.get_or_create(
            site_id=site.pk,
            name="Default",
            defaults={
                "is_active": True,
                "hourly_rate": site.hourly_rate,
                "additional_fee": site.additional_fee,
                "grace_minutes": 0,
                "parent_id": None,
            },
        )
        Device.objects.filter(site_id=site.pk, zone_id__isnull=True).update(zone_id=zone.pk)
        Session.objects.filter(site_id=site.pk, zone_id__isnull=True).update(zone_id=zone.pk)


def backwards(apps, schema_editor):
    Zone = apps.get_model("sites", "Zone")
    Zone.objects.filter(name="Default", parent_id__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0012_zone_nested_billing"),
        ("parking", "0015_zone_nested_billing"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

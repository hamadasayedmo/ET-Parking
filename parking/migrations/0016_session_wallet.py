# Generated manually for Session.wallet billing ownership

from django.db import migrations, models
import django.db.models.deletion


def backfill_session_wallet(apps, schema_editor):
    Session = apps.get_model("parking", "Session")
    Vehicle = apps.get_model("wallets", "Vehicle")
    plate_to_wallet = {
        v.plate: v.wallet_id
        for v in Vehicle.objects.all().only("plate", "wallet_id")
    }
    batch = []
    for session in Session.objects.filter(wallet_id__isnull=True).iterator(
        chunk_size=500
    ):
        wallet_id = plate_to_wallet.get(session.plate)
        if wallet_id:
            session.wallet_id = wallet_id
            batch.append(session)
        if len(batch) >= 500:
            Session.objects.bulk_update(batch, ["wallet_id"])
            batch = []
    if batch:
        Session.objects.bulk_update(batch, ["wallet_id"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("parking", "0015_zone_nested_billing"),
        ("wallets", "0012_vehicle_note"),
    ]

    operations = [
        migrations.AddField(
            model_name="session",
            name="wallet",
            field=models.ForeignKey(
                blank=True,
                help_text="Wallet that owned this plate when the session started (billing owner).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="sessions",
                to="wallets.wallet",
            ),
        ),
        migrations.AddIndex(
            model_name="session",
            index=models.Index(
                fields=["wallet", "end_time"], name="parking_ses_wallet__6f2a1c_idx"
            ),
        ),
        migrations.RunPython(backfill_session_wallet, noop_reverse),
    ]

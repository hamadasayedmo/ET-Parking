"""Idempotent leftovers after remap_wallets_app (tables/content types/migrations)."""

from django.db import migrations

from wallets.app_rename import remap_wallets_app


def forwards(apps, schema_editor):
    # Management command should already have done this; keep migration idempotent.
    remap_wallets_app()


def backwards(apps, schema_editor):
    # Irreversible in practice after merge migrations; no-op.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("wallets", "0010_merge_client_into_wallet"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

"""
Pre-migrate fix for renaming the Django app subscriptions → wallets.

Safe on every boot: remaps migration history, renames tables, updates content types.

    python manage.py remap_wallets_app
    python manage.py migrate
"""

from django.core.management.base import BaseCommand

from wallets.app_rename import remap_wallets_app


class Command(BaseCommand):
    help = "Remap subscriptions → wallets (migrations, tables, content types)."

    def handle(self, *args, **options):
        stats = remap_wallets_app(stdout=self.stdout)
        if stats["tables_renamed"] or stats["migrations_remapped"]:
            self.stdout.write(self.style.SUCCESS("Wallets app remap complete."))
        else:
            self.stdout.write("Wallets app remap: nothing to do.")

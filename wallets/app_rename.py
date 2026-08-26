"""Idempotent rename of DB artifacts from app label subscriptions → wallets."""

from __future__ import annotations

from django.db import connection

OLD_APP = "subscriptions"
NEW_APP = "wallets"

# Include client: prod may not have run merge migration 0010 yet.
TABLE_RENAMES = (
    ("subscriptions_client", "wallets_client"),
    ("subscriptions_wallet", "wallets_wallet"),
    ("subscriptions_vehicle", "wallets_vehicle"),
    ("subscriptions_transaction", "wallets_transaction"),
)


def remap_wallets_app(*, stdout=None) -> dict:
    """
    Safe to run on every boot.

    - Remaps django_migrations.app subscriptions → wallets
    - Renames subscriptions_* tables → wallets_* when needed
    - Remaps contenttypes.app_label
    """
    log = stdout.write if stdout is not None else (lambda *_: None)
    existing = set(connection.introspection.table_names())
    quote = connection.ops.quote_name
    stats = {
        "migrations_remapped": 0,
        "tables_renamed": [],
        "content_types_updated": 0,
    }

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM django_migrations WHERE app = %s",
            [OLD_APP],
        )
        count = cursor.fetchone()[0]
        if count:
            cursor.execute(
                "UPDATE django_migrations SET app = %s WHERE app = %s",
                [NEW_APP, OLD_APP],
            )
            stats["migrations_remapped"] = count
            log(f"Remapped {count} django_migrations row(s) to app={NEW_APP}.")
        else:
            log("No subscriptions migration rows to remap.")

        for old, new in TABLE_RENAMES:
            if old in existing and new not in existing:
                cursor.execute(f"ALTER TABLE {quote(old)} RENAME TO {quote(new)}")
                stats["tables_renamed"].append(f"{old} → {new}")
                log(f"Renamed table {old} → {new}.")
                existing.discard(old)
                existing.add(new)
            elif old in existing and new in existing:
                log(f"Both {old} and {new} exist; leaving as-is.")

        cursor.execute(
            "UPDATE django_content_type SET app_label = %s WHERE app_label = %s",
            [NEW_APP, OLD_APP],
        )
        stats["content_types_updated"] = cursor.rowcount
        if cursor.rowcount:
            log(f"Updated {cursor.rowcount} content type(s) to app_label={NEW_APP}.")

    return stats

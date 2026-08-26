# Generated manually for Session.billing_method

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("parking", "0010_accessrequest_exempted_session_billing"),
    ]

    operations = [
        migrations.AddField(
            model_name="session",
            name="billing_method",
            field=models.CharField(
                choices=[("wallet", "Wallet"), ("card", "Card (pay at exit)")],
                db_index=True,
                default="wallet",
                help_text="How this session is expected to be settled.",
                max_length=16,
            ),
        ),
    ]

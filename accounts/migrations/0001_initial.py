# Generated manually for StaffProfile

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def create_profiles_for_existing_users(apps, schema_editor):
    User = apps.get_model(settings.AUTH_USER_MODEL)
    StaffProfile = apps.get_model("accounts", "StaffProfile")
    for user in User.objects.all().iterator():
        StaffProfile.objects.get_or_create(
            user_id=user.id,
            defaults={"all_projects": True},
        )


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffProfile",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "all_projects",
                    models.BooleanField(
                        default=True,
                        help_text=(
                            "If checked, this user can access every project. "
                            "Uncheck and assign projects below to restrict access."
                        ),
                    ),
                ),
                (
                    "projects",
                    models.ManyToManyField(
                        blank=True,
                        help_text='Used only when “All projects” is unchecked.',
                        related_name="staff_members",
                        to="projects.project",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="staff_profile",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "staff profile",
                "verbose_name_plural": "staff profiles",
            },
        ),
        migrations.RunPython(
            create_profiles_for_existing_users,
            migrations.RunPython.noop,
        ),
    ]

from django.conf import settings
from django.db import models


class StaffProfile(models.Model):
    """
    Ops access scope for a staff user.

    - all_projects=True  → unrestricted (default for existing staff)
    - all_projects=False → only the projects in `projects`
    Superusers always bypass scoping regardless of this profile.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="staff_profile",
    )
    all_projects = models.BooleanField(
        default=True,
        help_text="If checked, this user can access every project. "
        "Uncheck and assign projects below to restrict access.",
    )
    projects = models.ManyToManyField(
        "projects.Project",
        blank=True,
        related_name="staff_members",
        help_text="Used only when “All projects” is unchecked.",
    )

    class Meta:
        verbose_name = "staff profile"
        verbose_name_plural = "staff profiles"

    def __str__(self):
        return f"Staff profile: {self.user}"


class Cashier(models.Model):
    """
    Desk cashier assignment: one staff user ↔ one zone.

    A user may have multiple rows (multiple zones). The Cashier Hub scopes
    gate control and on-site plate search to these zones.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="cashier_assignments",
    )
    zone = models.ForeignKey(
        "sites.Zone",
        on_delete=models.CASCADE,
        related_name="cashiers",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "cashier"
        verbose_name_plural = "cashiers"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "zone"],
                name="accounts_cashier_user_zone_uniq",
            )
        ]
        indexes = [
            models.Index(fields=["user"]),
            models.Index(fields=["zone"]),
        ]

    def __str__(self):
        return f"{self.user} @ {self.zone}"

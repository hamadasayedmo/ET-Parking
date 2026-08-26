from rest_framework import serializers

from projects.models import Project


class ProjectSerializer(serializers.ModelSerializer):
    site_count = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = (
            "id",
            "name",
            "is_active",
            "wallet_enabled",
            "pay_at_exit_enabled",
            "currency",
            "country",
            "seller_name",
            "vat_number",
            "grace_minutes",
            "site_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("created_at", "updated_at", "site_count")

    def get_site_count(self, obj):
        if hasattr(obj, "site_count_agg"):
            return obj.site_count_agg
        return obj.sites.filter(is_active=True).count()

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from accounts.scoping import user_project_ids
from projects.models import Project

User = get_user_model()


class ProjectScopeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ("id", "name", "is_active")
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    groups = serializers.SlugRelatedField(
        many=True,
        read_only=True,
        slug_field="name",
    )
    permissions = serializers.SerializerMethodField()
    all_projects = serializers.SerializerMethodField()
    projects = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_staff",
            "is_superuser",
            "groups",
            "permissions",
            "all_projects",
            "projects",
        )
        read_only_fields = fields

    def get_permissions(self, obj):
        return sorted(obj.get_all_permissions())

    def get_all_projects(self, obj):
        return user_project_ids(obj) is None

    def get_projects(self, obj):
        allowed = user_project_ids(obj)
        if allowed is None:
            qs = Project.objects.filter(is_active=True).order_by("name")
        else:
            qs = Project.objects.filter(id__in=allowed).order_by("name")
        return ProjectScopeSerializer(qs, many=True).data


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError(
                {"confirm_password": "New password and confirmation do not match."}
            )
        user = self.context["request"].user
        if not user.check_password(attrs["current_password"]):
            raise serializers.ValidationError(
                {"current_password": "Current password is incorrect."}
            )
        try:
            validate_password(attrs["new_password"], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"new_password": list(exc.messages)})
        return attrs

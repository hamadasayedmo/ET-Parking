from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from parking.models import AccessRequest
from parking.services import authorize_plate
from projects.models import Project
from sites.models import Device, Site
from wallets.models import Transaction, Vehicle, Wallet


User = get_user_model()


class ApiAuthTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="staff",
            password="pass12345",
            is_staff=True,
        )

    def test_login_and_me(self):
        response = self.client.post(
            "/api/auth/login/",
            {"username": "staff", "password": "pass12345"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        token = response.data["access"]

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        me = self.client.get("/api/auth/me/")
        self.assertEqual(me.status_code, status.HTTP_200_OK)
        self.assertEqual(me.data["username"], "staff")
        self.assertTrue(me.data["all_projects"])
        self.assertIn("projects", me.data)

    def test_change_password(self):
        self.client.force_authenticate(user=self.staff)
        bad = self.client.post(
            "/api/auth/change-password/",
            {
                "current_password": "wrong",
                "new_password": "NewPass999!",
                "confirm_password": "NewPass999!",
            },
            format="json",
        )
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", bad.data)

        mismatch = self.client.post(
            "/api/auth/change-password/",
            {
                "current_password": "pass12345",
                "new_password": "NewPass999!",
                "confirm_password": "OtherPass999!",
            },
            format="json",
        )
        self.assertEqual(mismatch.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("confirm_password", mismatch.data)

        ok = self.client.post(
            "/api/auth/change-password/",
            {
                "current_password": "pass12345",
                "new_password": "NewPass999!",
                "confirm_password": "NewPass999!",
            },
            format="json",
        )
        self.assertEqual(ok.status_code, status.HTTP_200_OK)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("NewPass999!"))

        login = self.client.post(
            "/api/auth/login/",
            {"username": "staff", "password": "NewPass999!"},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)

    def test_api_requires_auth(self):
        response = self.client.get("/api/projects/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class ApiCrudTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.staff = User.objects.create_user(
            username="ops",
            password="pass12345",
            is_staff=True,
        )
        self.api.force_authenticate(user=self.staff)
        self.project = Project.objects.create(name="Portfolio")
        self.site = Site.objects.create(project=self.project, name="Lot A")
        with patch("sites.signals.publish_once", return_value=True):
            self.device = Device.objects.create(
                site=self.site,
                ip="10.0.0.1",
                username="admin",
                password="secret",
                type=Device.Type.ENTRY,
            )
        self.wallet = Wallet.objects.create(project=self.project, name="Acme")
        Vehicle.objects.create(wallet=self.wallet, plate="ABC123")

    def test_projects_list_and_create(self):
        response = self.api.get("/api/projects/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)

        create = self.api.post(
            "/api/projects/",
            {"name": "New Project", "is_active": True},
            format="json",
        )
        self.assertEqual(create.status_code, status.HTTP_201_CREATED)

    def test_sites_and_devices(self):
        sites = self.api.get("/api/sites/")
        self.assertEqual(sites.status_code, status.HTTP_200_OK)

        devices = self.api.get("/api/devices/?site=%s" % self.site.pk)
        self.assertEqual(devices.status_code, status.HTTP_200_OK)
        self.assertEqual(devices.data["count"], 1)

    def test_wallet_top_up(self):
        wallet = self.wallet
        # Staff without adjust_wallet cannot change balances.
        forbidden = self.api.post(
            f"/api/wallets/{wallet.pk}/top_up/",
            {"amount": "15.00", "reference": "test"},
            format="json",
        )
        self.assertEqual(forbidden.status_code, status.HTTP_403_FORBIDDEN)

        wallet_group = Group.objects.get(name="Wallet")
        self.staff.groups.add(wallet_group)
        # Group perms are cached on the user instance.
        self.staff = User.objects.get(pk=self.staff.pk)
        self.api.force_authenticate(user=self.staff)

        response = self.api.post(
            f"/api/wallets/{wallet.pk}/top_up/",
            {"amount": "15.00", "reference": "test"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data["balance"]), Decimal("15.00"))
        tx = Transaction.objects.get(reference="test")
        self.assertEqual(tx.created_by_id, self.staff.pk)
        self.assertEqual(tx.transaction_type, Transaction.Type.TOP_UP)

    @patch("sites.views.publish_once", return_value=True)
    def test_operations_cannot_command_gates(self, _mock_pub):
        ops = User.objects.create_user(
            username="opsrole",
            password="pass12345",
            is_staff=False,
        )
        ops.groups.add(Group.objects.get(name="Operations"))
        self.api.force_authenticate(user=ops)

        response = self.api.post(f"/api/devices/{self.device.pk}/open/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("sites.views.publish_once", return_value=True)
    def test_control_room_can_command_gates(self, _mock_pub):
        operator = User.objects.create_user(
            username="gatecmd",
            password="pass12345",
            is_staff=False,
        )
        operator.groups.add(Group.objects.get(name="Control Room"))
        self.api.force_authenticate(user=operator)

        response = self.api.post(f"/api/devices/{self.device.pk}/open/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_operations_cannot_top_up_wallet(self):
        ops = User.objects.create_user(
            username="opsnowallet",
            password="pass12345",
            is_staff=False,
        )
        ops.groups.add(Group.objects.get(name="Operations"))
        self.api.force_authenticate(user=ops)

        wallet = self.wallet
        response = self.api.post(
            f"/api/wallets/{wallet.pk}/top_up/",
            {"amount": "5.00", "reference": "nope"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("parking.services.publish_once", return_value=True)
    def test_access_request_approve(self, _mock_pub):
        perm = Permission.objects.get(codename="decide_accessrequest")
        self.staff.user_permissions.add(perm)
        group = Group.objects.get(name="Control Room")
        self.staff.groups.add(group)

        result = authorize_plate(self.device, "UNK999")
        ar = result.access_request
        self.assertIsNotNone(ar)

        response = self.api.post(
            f"/api/access-requests/{ar.pk}/approve/",
            {"note": "ok", "exempted": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], AccessRequest.Status.APPROVED)
        self.assertTrue(response.data["exempted"])

    @patch("parking.services.publish_once", return_value=True)
    def test_control_room_group_can_gate_ops_without_staff_flag(self, _mock_pub):
        operator = User.objects.create_user(
            username="gateops",
            password="pass12345",
            is_staff=False,
        )
        group = Group.objects.get(name="Control Room")
        operator.groups.add(group)
        self.api.force_authenticate(user=operator)

        devices = self.api.get("/api/devices/")
        self.assertEqual(devices.status_code, status.HTTP_200_OK)

        result = authorize_plate(self.device, "CR001")
        ar = result.access_request
        self.assertIsNotNone(ar)

        approve = self.api.post(
            f"/api/access-requests/{ar.pk}/approve/",
            {"note": "control room", "exempted": False},
            format="json",
        )
        self.assertEqual(approve.status_code, status.HTTP_200_OK)
        self.assertEqual(approve.data["status"], AccessRequest.Status.APPROVED)


class ProjectScopeTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.project_a = Project.objects.create(name="Alpha")
        self.project_b = Project.objects.create(name="Beta")
        self.site_a = Site.objects.create(project=self.project_a, name="Site A")
        self.site_b = Site.objects.create(project=self.project_b, name="Site B")

        self.scoped = User.objects.create_user(
            username="scoped",
            password="pass12345",
            is_staff=True,
        )
        profile = self.scoped.staff_profile
        profile.all_projects = False
        profile.save()
        profile.projects.set([self.project_a])

        self.api.force_authenticate(user=self.scoped)

    def test_projects_list_is_scoped(self):
        response = self.api.get("/api/projects/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in response.data["results"]}
        self.assertEqual(ids, {self.project_a.id})

    def test_sites_list_is_scoped(self):
        response = self.api.get("/api/sites/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in response.data["results"]}
        self.assertEqual(ids, {self.site_a.id})

    def test_cannot_read_other_project(self):
        response = self.api.get(f"/api/projects/{self.project_b.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_create_site_in_other_project(self):
        response = self.api.post(
            "/api/sites/",
            {
                "project": self.project_b.id,
                "name": "Hack",
                "hourly_rate": "10.00",
                "additional_fee": "0.00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_me_lists_assigned_projects_only(self):
        response = self.api.get("/api/auth/me/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["all_projects"])
        ids = {row["id"] for row in response.data["projects"]}
        self.assertEqual(ids, {self.project_a.id})

    def test_wallets_are_scoped(self):
        wallet_a = Wallet.objects.create(project=self.project_a, name="Wallet A")
        Wallet.objects.create(project=self.project_b, name="Wallet B")

        wallets = self.api.get("/api/wallets/")
        self.assertEqual(wallets.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {row["id"] for row in wallets.data["results"]},
            {wallet_a.id},
        )

        forbidden = self.api.post(
            "/api/wallets/",
            {"project": self.project_b.id, "name": "Nope", "is_active": True},
            format="json",
        )
        self.assertIn(
            forbidden.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN),
        )

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase

from parking.models import AccessRequest, Event
from parking.services import (
    approve_access_request,
    authorize_plate,
    deny_access_request,
    match_access_request_to_session,
    session_matches_for_access_request,
)
from sites.models import Device, Site
from projects.models import Project
from wallets.models import Vehicle, Wallet


User = get_user_model()


class AccessRequestFlowTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Test Project")
        self.site = Site.objects.create(
            project=self.project,
            name="Lot A",
            hourly_rate=Decimal("2.00"),
            additional_fee=Decimal("1.00"),
        )
        with patch("sites.signals.publish_once", return_value=True):
            self.entry = Device.objects.create(
                site=self.site,
                ip="192.168.0.10",
                username="admin",
                password="secret",
                type=Device.Type.ENTRY,
            )
        self.wallet = Wallet.objects.create(
            project=self.project, name="Acme", is_active=True
        )
        Vehicle.objects.create(
            wallet=self.wallet,
            plate="ABC123",
            is_active=True,
        )
        self.wallet.balance = Decimal("20.00")
        self.wallet.save(update_fields=["balance"])
        self.operator = User.objects.create_user(
            username="control",
            password="pass",
            is_staff=True,
        )

    def test_grant_clears_prior_pending_as_timeout(self):
        self.wallet.balance = Decimal("0.00")
        self.wallet.save(update_fields=["balance"])
        first = authorize_plate(self.entry, "UNK001")
        self.assertIsNotNone(first.access_request)
        self.assertEqual(first.access_request.status, AccessRequest.Status.PENDING)
        first_id = first.access_request.pk

        self.wallet.balance = Decimal("20.00")
        self.wallet.save(update_fields=["balance"])
        second = authorize_plate(self.entry, "ABC123")
        self.assertTrue(second.granted)

        self.assertFalse(
            AccessRequest.objects.filter(pk=first_id).exists(),
            "Timed-out pending AR should be deleted, not kept as timeout",
        )
        self.assertEqual(
            AccessRequest.objects.filter(
                device=self.entry,
                status=AccessRequest.Status.PENDING,
            ).count(),
            0,
        )
        self.assertFalse(
            Event.objects.filter(plate="UNK001", decision=Event.Decision.DENIED).exists(),
            "Timeouts should not write a denied Event",
        )

    def test_deny_creates_pending_request_no_event(self):
        result = authorize_plate(self.entry, "UNK999")
        self.assertFalse(result.granted)
        self.assertIsNotNone(result.access_request)
        self.assertEqual(result.access_request.plate, "UNK999")
        self.assertEqual(result.access_request.status, AccessRequest.Status.PENDING)
        self.assertFalse(
            Event.objects.filter(plate="UNK999", device=self.entry).exists()
        )

    def test_second_deny_on_same_device_times_out_first(self):
        first = authorize_plate(self.entry, "CAR001")
        first_id = first.access_request.pk
        second = authorize_plate(self.entry, "XYZ999")

        self.assertFalse(AccessRequest.objects.filter(pk=first_id).exists())
        self.assertEqual(second.access_request.status, AccessRequest.Status.PENDING)
        self.assertEqual(second.access_request.plate, "XYZ999")
        self.assertEqual(
            AccessRequest.objects.filter(
                device=self.entry,
                status=AccessRequest.Status.PENDING,
            ).count(),
            1,
        )
        self.assertFalse(
            Event.objects.filter(
                plate="CAR001",
                decision=Event.Decision.DENIED,
            ).exists()
        )
    @patch("parking.services.publish_once", return_value=True)
    def test_approve_grants_and_pulses(self, mock_pub):
        result = authorize_plate(self.entry, "UNK111")
        access_request = result.access_request
        approve_access_request(
            access_request, self.operator, note=" VIP guest "
        )

        access_request.refresh_from_db()
        self.assertEqual(access_request.status, AccessRequest.Status.APPROVED)
        self.assertEqual(access_request.decided_by, self.operator)
        self.assertEqual(access_request.decision_note, "VIP guest")
        self.assertFalse(access_request.exempted)
        self.assertEqual(
            access_request.resolution_event.decision,
            Event.Decision.GRANTED,
        )
        mock_pub.assert_called_once()
        args, _kwargs = mock_pub.call_args
        self.assertEqual(args[0], self.entry.get_command_topic())
        self.assertEqual(args[1], self.entry.get_pulse_payload())

        from parking.models import Session

        session = Session.objects.get(plate="UNK111", end_time__isnull=True)
        self.assertFalse(session.billing_exempt)
        self.assertTrue(session.allow_negative_balance)

    @patch("parking.services.publish_once", return_value=True)
    def test_exempted_entry_auto_exits_without_escalation(self, mock_pub):
        from datetime import timedelta
        from decimal import Decimal

        from django.utils import timezone

        from parking.models import Session
        from sites.models import Device

        self.wallet.balance = Decimal("0.00")
        self.wallet.save(update_fields=["balance"])
        entry = authorize_plate(self.entry, "ABC123")
        approve_access_request(
            entry.access_request,
            self.operator,
            exempted=True,
            note="Exempt entry for test",
        )

        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        self.assertTrue(session.billing_exempt)
        session.start_time = timezone.now() - timedelta(hours=1)
        session.save(update_fields=["start_time"])

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                ip="192.168.0.22",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        result = authorize_plate(exit_device, "ABC123")
        self.assertTrue(result.granted)
        self.assertIsNone(result.access_request)

        session.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(session.payment_status, Session.PaymentStatus.EXEMPTED)
        self.assertEqual(self.wallet.balance, Decimal("0.00"))

    @patch("parking.services.publish_once", return_value=True)
    def test_approve_non_exempt_allows_negative_balance(self, mock_pub):
        from datetime import timedelta
        from decimal import Decimal

        from django.utils import timezone

        from parking.models import Session
        from sites.models import Device

        authorize_plate(self.entry, "ABC123")
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        session.start_time = timezone.now() - timedelta(hours=2)
        session.save(update_fields=["start_time"])

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                ip="192.168.0.21",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )
        self.wallet.balance = Decimal("1.00")
        self.wallet.save(update_fields=["balance"])

        result = authorize_plate(exit_device, "ABC123")
        approve_access_request(result.access_request, self.operator, exempted=False)

        session.refresh_from_db()
        self.wallet.refresh_from_db()
        result.access_request.refresh_from_db()
        self.assertEqual(session.payment_status, Session.PaymentStatus.PAID)
        self.assertLess(self.wallet.balance, Decimal("0.00"))
        self.assertFalse(result.access_request.exempted)

    def test_deny_writes_denied_event_no_pulse(self):
        result = authorize_plate(self.entry, "UNK222")
        with patch("parking.services.publish_once") as mock_pub:
            deny_access_request(
                result.access_request, self.operator, note="No subscription"
            )
            mock_pub.assert_not_called()

        result.access_request.refresh_from_db()
        self.assertEqual(result.access_request.status, AccessRequest.Status.DENIED)
        self.assertEqual(result.access_request.decision_note, "No subscription")
        self.assertEqual(
            result.access_request.resolution_event.decision,
            Event.Decision.DENIED,
        )

    def test_approve_non_pending_raises(self):
        result = authorize_plate(self.entry, "UNK333")
        deny_access_request(result.access_request, self.operator)
        with self.assertRaises(ValueError):
            approve_access_request(result.access_request, self.operator)

    @patch("parking.services.publish_once", return_value=True)
    def test_approve_exit_without_session_requires_entry_time(self, mock_pub):
        from datetime import timedelta

        from django.utils import timezone

        from parking.models import Session
        from sites.models import Device

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                ip="192.168.0.30",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        result = authorize_plate(exit_device, "ABC123")
        self.assertIsNotNone(result.access_request)
        self.assertFalse(
            Session.objects.filter(plate="ABC123", end_time__isnull=True).exists()
        )

        with self.assertRaises(ValueError):
            approve_access_request(
                result.access_request, self.operator, exempted=False
            )

        entry_time = timezone.now() - timedelta(hours=2)
        approve_access_request(
            result.access_request,
            self.operator,
            exempted=False,
            entry_time=entry_time,
        )

        result.access_request.refresh_from_db()
        self.wallet.refresh_from_db()
        session = Session.objects.get(plate="ABC123")
        self.assertEqual(result.access_request.status, AccessRequest.Status.APPROVED)
        self.assertIsNotNone(session.end_time)
        self.assertEqual(session.start_time, entry_time)
        self.assertEqual(session.payment_status, Session.PaymentStatus.PAID)
        self.assertEqual(session.fee, Decimal("5.00"))  # 2h * 2 + 1
        self.assertLess(self.wallet.balance, Decimal("20.00"))
        mock_pub.assert_called()

    @patch("parking.services.publish_once", return_value=True)
    def test_operator_matches_no_entry_exit_to_closest_open_session(self, mock_pub):
        from parking.models import Session, SessionAuditEntry

        authorize_plate(self.entry, "ABC123")
        Vehicle.objects.create(
            wallet=self.wallet,
            plate="ZZZ999",
            is_active=True,
        )
        authorize_plate(self.entry, "ZZZ999")

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                zone=self.entry.zone,
                ip="192.168.0.32",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        result = authorize_plate(exit_device, "ABC12B")
        matches = session_matches_for_access_request(result.access_request)
        self.assertEqual(matches[0]["plate"], "ABC123")
        # ABC12B vs ABC123 → 1 edit on 6 chars → ~83%
        self.assertEqual(matches[0]["match_percent"], 83)
        self.assertFalse(matches[0]["weak"])
        # Far plate ZZZ999 is below the 50% floor and must not be suggested.
        self.assertEqual([row["plate"] for row in matches], ["ABC123"])

        matched = match_access_request_to_session(
            result.access_request,
            matches[0]["id"],
            self.operator,
        )
        self.assertEqual(matched.plate, "ABC123")
        self.assertIn("Exit OCR ABC12B", matched.reason)
        self.assertTrue(
            SessionAuditEntry.objects.filter(
                session_id=matches[0]["id"],
                actor=self.operator,
                field=SessionAuditEntry.EXIT_MATCH_FIELD,
            ).exists()
        )

        approve_access_request(matched, self.operator)
        session = Session.objects.get(pk=matches[0]["id"])
        self.assertIsNotNone(session.end_time)
        self.assertEqual(session.end_event.plate, "ABC123")
        self.assertIsNone(
            Session.objects.get(plate="ZZZ999", end_time__isnull=True).end_time
        )

    @patch("parking.services.publish_once", return_value=True)
    def test_unmatch_exit_keeps_match_audit(self, mock_pub):
        from parking.models import Session, SessionAuditEntry
        from parking.services import (
            match_access_request_to_session,
            session_has_active_exit_match,
            unmatch_access_request_from_session,
        )

        authorize_plate(self.entry, "ABC123")
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                zone=self.entry.zone,
                ip="192.168.0.33",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        result = authorize_plate(exit_device, "ABC12B")
        matched = match_access_request_to_session(
            result.access_request,
            session.pk,
            self.operator,
        )
        self.assertEqual(matched.plate, "ABC123")
        self.assertTrue(session_has_active_exit_match(session.pk))

        unmatched = unmatch_access_request_from_session(matched, self.operator)
        self.assertEqual(unmatched.plate, "ABC12B")
        self.assertFalse(unmatched.reason.startswith("Exit OCR"))
        self.assertFalse(session_has_active_exit_match(session.pk))
        self.assertTrue(
            SessionAuditEntry.objects.filter(
                session=session,
                field=SessionAuditEntry.EXIT_MATCH_FIELD,
            ).exists()
        )
        self.assertTrue(
            SessionAuditEntry.objects.filter(
                session=session,
                field=SessionAuditEntry.EXIT_UNMATCH_FIELD,
                actor=self.operator,
            ).exists()
        )

    @patch("parking.services.publish_once", return_value=True)
    def test_unmatch_exit_works_after_ar_decided(self, mock_pub):
        from rest_framework.test import APIClient

        from parking.models import Session, SessionAuditEntry
        from parking.services import (
            match_access_request_to_session,
            session_has_active_exit_match,
        )

        authorize_plate(self.entry, "ABC123")
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                zone=self.entry.zone,
                ip="192.168.0.34",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        result = authorize_plate(exit_device, "ABC12B")
        matched = match_access_request_to_session(
            result.access_request,
            session.pk,
            self.operator,
        )
        deny_access_request(matched, self.operator, note="Reject after match")
        matched.refresh_from_db()
        self.assertEqual(matched.status, AccessRequest.Status.DENIED)
        self.assertTrue(session_has_active_exit_match(session.pk))

        api = APIClient()
        api.force_authenticate(user=self.operator)
        self.operator.user_permissions.add(
            Permission.objects.get(codename="decide_accessrequest")
        )
        detail = api.get(f"/api/sessions/{session.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.data["can_unmatch_exit"])

        response = api.post(f"/api/sessions/{session.pk}/unmatch-exit/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["exit_matched"])
        self.assertFalse(response.data["can_unmatch_exit"])
        self.assertFalse(session_has_active_exit_match(session.pk))
        self.assertTrue(
            SessionAuditEntry.objects.filter(
                session=session,
                field=SessionAuditEntry.EXIT_UNMATCH_FIELD,
            ).exists()
        )
        matched.refresh_from_db()
        # Decided AR plate stays as matched — only the session link is undone.
        self.assertEqual(matched.plate, "ABC123")
        self.assertEqual(matched.status, AccessRequest.Status.DENIED)

    def test_sessions_list_filters_exit_matched(self):
        from rest_framework.test import APIClient

        from parking.models import Session, SessionAuditEntry

        authorize_plate(self.entry, "ABC123")
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        SessionAuditEntry.objects.create(
            session=session,
            actor=self.operator,
            action=SessionAuditEntry.Action.NOTE,
            field=SessionAuditEntry.EXIT_MATCH_FIELD,
            old_value="ABC12B",
            new_value="ABC123",
            note="Matched exit access request #1",
        )

        Vehicle.objects.create(wallet=self.wallet, plate="XYZ999", is_active=True)
        authorize_plate(self.entry, "XYZ999")

        api = APIClient()
        api.force_authenticate(user=self.operator)
        matched = api.get("/api/sessions/", {"exit_matched": "1"})
        self.assertEqual(matched.status_code, 200)
        plates = [row["plate"] for row in matched.data["results"]]
        self.assertIn("ABC123", plates)
        self.assertNotIn("XYZ999", plates)
        row = next(r for r in matched.data["results"] if r["plate"] == "ABC123")
        self.assertTrue(row["exit_matched"])
        self.assertEqual(row["exit_match_ocr"], "ABC12B")

        unmatched = api.get("/api/sessions/", {"exit_matched": "0"})
        unmatched_plates = [row["plate"] for row in unmatched.data["results"]]
        self.assertNotIn("ABC123", unmatched_plates)
        self.assertIn("XYZ999", unmatched_plates)

    @patch("parking.services.publish_once", return_value=True)
    def test_wallet_only_no_wallet_skips_entry_time(self, mock_pub):
        """Wallet-only + unknown plate: approve opens gate without entry time."""
        from parking.models import Session
        from parking.services import access_request_requires_entry_time
        from sites.models import Device

        self.assertTrue(self.project.wallet_enabled)
        self.assertFalse(self.project.pay_at_exit_enabled)

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                ip="192.168.0.31",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        result = authorize_plate(exit_device, "NOWALLET")
        self.assertIsNotNone(result.access_request)
        self.assertFalse(
            access_request_requires_entry_time(result.access_request)
        )

        approve_access_request(
            result.access_request,
            self.operator,
            exempted=False,
        )

        result.access_request.refresh_from_db()
        self.assertEqual(result.access_request.status, AccessRequest.Status.APPROVED)
        self.assertFalse(
            Session.objects.filter(plate="NOWALLET").exists()
        )
        mock_pub.assert_called()

    @patch("parking.services.publish_once", return_value=True)
    def test_wallet_only_no_wallet_session_closes_exempted(self, mock_pub):
        """Approved guest stay with no wallet closes as exempted, not failed."""
        from datetime import timedelta

        from django.utils import timezone

        from parking.models import Session
        from sites.models import Device

        entry = authorize_plate(self.entry, "GUEST01")
        self.assertIsNotNone(entry.access_request)
        approve_access_request(entry.access_request, self.operator, exempted=False)

        session = Session.objects.get(plate="GUEST01", end_time__isnull=True)
        self.assertIsNone(session.wallet_id)
        self.assertEqual(session.payment_status, Session.PaymentStatus.PENDING)
        session.start_time = timezone.now() - timedelta(hours=1)
        session.save(update_fields=["start_time"])

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                ip="192.168.0.32",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        exit_result = authorize_plate(exit_device, "GUEST01")
        self.assertIsNotNone(exit_result.access_request)
        approve_access_request(
            exit_result.access_request, self.operator, exempted=False
        )

        session.refresh_from_db()
        self.assertIsNotNone(session.end_time)
        self.assertTrue(session.billing_exempt)
        self.assertEqual(session.payment_status, Session.PaymentStatus.EXEMPTED)
        self.assertEqual(session.waiver_kind, Session.WaiverKind.GUEST)
        mock_pub.assert_called()

    @patch("parking.services.publish_once", return_value=True)
    def test_inactive_wallet_raises_ar_with_reason(self, mock_pub):
        from parking.models import Session
        from sites.models import Device

        self.wallet.is_active = False
        self.wallet.save(update_fields=["is_active"])

        entry = authorize_plate(self.entry, "ABC123")
        self.assertIsNotNone(entry.access_request)
        self.assertIn("Wallet is inactive", entry.access_request.reason)
        self.assertIn("Acme", entry.access_request.reason)

        approve_access_request(entry.access_request, self.operator, exempted=False)
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                ip="192.168.0.33",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        exit_result = authorize_plate(exit_device, "ABC123")
        self.assertIsNotNone(exit_result.access_request)
        self.assertIn("Wallet is inactive", exit_result.access_request.reason)
        approve_access_request(
            exit_result.access_request, self.operator, exempted=False
        )

        session.refresh_from_db()
        self.assertEqual(session.payment_status, Session.PaymentStatus.EXEMPTED)
        self.assertTrue(session.billing_exempt)
        self.assertEqual(session.waiver_kind, Session.WaiverKind.GUEST)
        mock_pub.assert_called()

    @patch("parking.services.publish_once", return_value=True)
    def test_wallet_charge_failure_raises_ar_not_failed(self, mock_pub):
        """Closed session that cannot be charged stays pending and raises an AR."""
        from datetime import timedelta

        from django.utils import timezone

        from parking.models import Session
        from sites.models import Device

        authorize_plate(self.entry, "ABC123")
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        session.allow_negative_balance = False
        session.start_time = timezone.now() - timedelta(hours=2)
        session.save(update_fields=["allow_negative_balance", "start_time"])

        self.wallet.balance = Decimal("0.00")
        self.wallet.save(update_fields=["balance"])

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                ip="192.168.0.34",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        # Force a granted exit (bypass balance check) to hit the charge path.
        from parking.models import Event

        Event.objects.create(
            device=exit_device,
            plate="ABC123",
            action=Event.Action.EXIT,
            decision=Event.Decision.GRANTED,
        )

        session.refresh_from_db()
        self.assertIsNotNone(session.end_time)
        self.assertEqual(session.payment_status, Session.PaymentStatus.PENDING)
        self.assertNotEqual(session.payment_status, Session.PaymentStatus.FAILED)

        ar = AccessRequest.objects.filter(
            plate="ABC123",
            status=AccessRequest.Status.PENDING,
            device=exit_device,
        ).first()
        self.assertIsNotNone(ar)
        self.assertIn("could not cover parking fee", ar.reason)

        approve_access_request(ar, self.operator, exempted=False)
        session.refresh_from_db()
        self.assertEqual(session.payment_status, Session.PaymentStatus.PAID)
        mock_pub.assert_called()

    @patch("parking.services.publish_once", return_value=True)
    def test_settle_closed_pending_session(self, mock_pub):
        """POST settle charges operator wallet (not fleet) and stamps paid grace."""
        from datetime import timedelta

        from django.utils import timezone

        from parking.models import Event, Session
        from parking.services import settle_session
        from sites.models import Device

        operator_wallet = Wallet.objects.create(
            project=self.project,
            name="Op cash",
            is_active=True,
            assigned_user=self.operator,
            balance=Decimal("0.00"),
        )

        authorize_plate(self.entry, "ABC123")
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        session.allow_negative_balance = False
        session.start_time = timezone.now() - timedelta(hours=2)
        session.save(update_fields=["allow_negative_balance", "start_time"])
        self.wallet.balance = Decimal("0.00")
        self.wallet.save(update_fields=["balance"])

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                ip="192.168.0.35",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        Event.objects.create(
            device=exit_device,
            plate="ABC123",
            action=Event.Action.EXIT,
            decision=Event.Decision.GRANTED,
        )
        session.refresh_from_db()
        self.assertEqual(session.payment_status, Session.PaymentStatus.PENDING)

        settled = settle_session(session, user=self.operator, note="desk settle")
        self.assertEqual(settled.payment_status, Session.PaymentStatus.PAID)
        self.assertIsNotNone(settled.paid_exit_until)
        operator_wallet.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertLess(operator_wallet.balance, Decimal("0.00"))
        self.assertEqual(self.wallet.balance, Decimal("0.00"))
        self.assertFalse(
            AccessRequest.objects.filter(
                plate="ABC123",
                status=AccessRequest.Status.PENDING,
            ).exists()
        )

    @patch("parking.services.publish_once", return_value=True)
    def test_approve_exit_without_session_exempted_skips_entry_time(self, mock_pub):
        from parking.models import Session
        from sites.models import Device

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                ip="192.168.0.31",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        result = authorize_plate(exit_device, "ABC123")
        approve_access_request(
            result.access_request,
            self.operator,
            exempted=True,
            note="Open gate only — lost ticket cash",
        )

        result.access_request.refresh_from_db()
        self.assertEqual(result.access_request.status, AccessRequest.Status.APPROVED)
        self.assertFalse(Session.objects.filter(plate="ABC123").exists())
        mock_pub.assert_called()

    def test_control_room_group_has_decide_permission(self):
        group = Group.objects.get(name="Control Room")
        codenames = set(group.permissions.values_list("codename", flat=True))
        self.assertIn("decide_accessrequest", codenames)
        self.assertIn("view_accessrequest", codenames)
        self.assertTrue(
            Permission.objects.filter(
                content_type__app_label="parking",
                codename="decide_accessrequest",
            ).exists()
        )

    @patch("parking.services.publish_once", return_value=True)
    def test_session_bill_html_includes_zatca_qr(self, mock_pub):
        from datetime import timedelta

        from django.utils import timezone
        from rest_framework.test import APIClient

        from parking.models import Session
        from parking.session_bill import session_bill_payload
        from payments.models import PaymentIntent

        self.project.seller_name = "Example Parking Co"
        self.project.vat_number = "310175397400003"
        self.project.country = "SA"
        self.project.save(update_fields=["seller_name", "vat_number", "country"])

        authorize_plate(self.entry, "ABC123")
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        session.start_time = timezone.now() - timedelta(hours=2)
        session.end_time = timezone.now()
        session.fee = Decimal("5.00")
        session.payment_status = Session.PaymentStatus.PAID
        session.billing_method = Session.BillingMethod.CARD
        session.save(
            update_fields=[
                "start_time",
                "end_time",
                "fee",
                "payment_status",
                "billing_method",
            ]
        )
        PaymentIntent.objects.create(
            project=self.project,
            session=session,
            amount=Decimal("5.00"),
            currency="SAR",
            merchant_reference_id=f"bill-test-{session.pk}",
            status=PaymentIntent.Status.PAID,
            paid_at=timezone.now(),
            pin="4242",
        )

        payload = session_bill_payload(session)
        self.assertEqual(payload["seller_name"], "Example Parking Co")
        self.assertEqual(payload["vat_number"], "310175397400003")
        self.assertEqual(payload["vat_rate"], "15.00")
        self.assertEqual(payload["invoice_total_with_vat"], "5.00")
        self.assertTrue(payload["qr_base64"])

        api = APIClient()
        api.force_authenticate(user=self.operator)
        response = api.get(f"/api/sessions/{session.pk}/bill/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response["Content-Type"])
        body = response.content.decode("utf-8")
        self.assertIn("Simplified tax invoice", body)
        self.assertIn("فاتورة ضريبية مبسطة", body)
        self.assertIn("<span>Card ···· 4242</span>", body)
        self.assertIn("Example Parking Co", body)
        self.assertIn("310175397400003", body)
        self.assertIn("ABC123", body)
        self.assertIn("<svg", body)

    def test_session_bill_rejects_non_pay_at_exit_session(self):
        from rest_framework.test import APIClient

        from parking.models import Session

        authorize_plate(self.entry, "ABC123")
        session = Session.objects.get(plate="ABC123", end_time__isnull=True)
        session.payment_status = Session.PaymentStatus.PAID
        session.billing_method = Session.BillingMethod.WALLET
        session.save(update_fields=["payment_status", "billing_method"])

        api = APIClient()
        api.force_authenticate(user=self.operator)
        response = api.get(f"/api/sessions/{session.pk}/bill/")

        self.assertEqual(response.status_code, 400)
        self.assertIn("successful pay-at-exit card payment", response.data["detail"])

    def test_exempt_client_exits_without_open_session(self):
        from sites.models import Device

        self.wallet.is_exempted = True
        self.wallet.save(update_fields=["is_exempted"])

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                ip="192.168.0.44",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        result = authorize_plate(exit_device, "ABC123")
        self.assertTrue(result.granted)
        self.assertIsNone(result.access_request)
        self.assertEqual(result.event.decision, Event.Decision.GRANTED)

    @patch("parking.services.publish_once", return_value=True)
    def test_exit_without_session_reason_is_clear(self, _mock_pub):
        from sites.models import Device

        with patch("sites.signals.publish_once", return_value=True):
            exit_device = Device.objects.create(
                site=self.site,
                ip="192.168.0.45",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )

        result = authorize_plate(exit_device, "ABC123")
        self.assertFalse(result.granted)
        self.assertIsNotNone(result.access_request)
        self.assertIn("No open entry session", result.access_request.reason)

    def test_entry_insufficient_balance_reason_is_clear(self):
        self.wallet.balance = Decimal("0.00")
        self.wallet.save(update_fields=["balance"])
        result = authorize_plate(self.entry, "ABC123")
        self.assertFalse(result.granted)
        self.assertIsNotNone(result.access_request)
        self.assertIn("Insufficient wallet balance", result.access_request.reason)

    def test_unknown_plate_entry_reason_is_clear(self):
        result = authorize_plate(self.entry, "UNK999")
        self.assertFalse(result.granted)
        self.assertIsNotNone(result.access_request)
        self.assertIn("No active subscription", result.access_request.reason)

    def test_authorize_stores_vehicle_type_on_granted_event(self):
        result = authorize_plate(self.entry, "ABC123", vehicle_type="truck")
        self.assertTrue(result.granted)
        self.assertEqual(result.event.vehicle_type, "truck")

    def test_authorize_without_vehicle_type_stays_empty(self):
        """Old gateways omit vehicle_type; ingest must not require it."""
        result = authorize_plate(self.entry, "ABC123")
        self.assertTrue(result.granted)
        self.assertEqual(result.event.vehicle_type, "")

    def test_authorize_stores_vehicle_type_on_pending_access_request(self):
        result = authorize_plate(self.entry, "UNK999", vehicle_type="bus")
        self.assertFalse(result.granted)
        self.assertEqual(result.access_request.vehicle_type, "bus")

        approved = approve_access_request(result.access_request, self.operator)
        self.assertEqual(approved.resolution_event.vehicle_type, "bus")


class StaffProcessPlateTests(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient

        self.api = APIClient()
        self.project = Project.objects.create(
            name="Manual Lot",
            wallet_enabled=False,
            pay_at_exit_enabled=False,
        )
        self.site = Site.objects.create(
            project=self.project,
            name="Lot A",
            hourly_rate=Decimal("2.00"),
            additional_fee=Decimal("1.00"),
        )
        with patch("sites.signals.publish_once", return_value=True):
            self.entry = Device.objects.create(
                site=self.site,
                ip="10.0.0.1",
                username="admin",
                password="secret",
                type=Device.Type.ENTRY,
            )
        self.operator = User.objects.create_user(
            username="gate_op",
            password="pass",
            is_staff=True,
        )
        perm = Permission.objects.get(codename="decide_accessrequest")
        self.operator.user_permissions.add(perm)
        self.api.force_authenticate(user=self.operator)

    @patch("parking.services.publish_once", return_value=True)
    def test_manual_creates_and_auto_approves(self, mock_pub):
        from parking.services import staff_process_plate

        access_request = staff_process_plate(
            self.entry,
            "XYZ999",
            self.operator,
            note="VIP visitor",
            exempted=True,
        )
        self.assertEqual(access_request.status, AccessRequest.Status.APPROVED)
        self.assertEqual(access_request.plate, "XYZ999")
        self.assertEqual(access_request.decided_by_id, self.operator.pk)
        self.assertTrue(access_request.exempted)
        self.assertEqual(access_request.decision_note, "VIP visitor")
        self.assertIsNotNone(access_request.resolution_event)
        self.assertEqual(
            access_request.resolution_event.decision, Event.Decision.GRANTED
        )
        mock_pub.assert_called()

    def test_manual_rejects_when_gate_busy(self):
        from parking.services import staff_process_plate

        AccessRequest.objects.create(
            device=self.entry,
            plate="BUSY1",
            action=Event.Action.ENTRY,
            status=AccessRequest.Status.PENDING,
            reason="waiting",
        )
        with self.assertRaises(ValueError):
            staff_process_plate(self.entry, "NEW1", self.operator)

    @patch("parking.services.publish_once", return_value=True)
    def test_manual_api_endpoint(self, mock_pub):
        response = self.api.post(
            "/api/access-requests/manual/",
            {
                "device_id": self.entry.pk,
                "plate": "MAN001",
                "note": "Opened by desk",
                "exempted": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["granted"])
        self.assertEqual(response.data["access_request"]["plate"], "MAN001")
        self.assertEqual(response.data["access_request"]["status"], "approved")
        self.assertTrue(response.data["access_request"]["exempted"])
        self.assertEqual(
            response.data["access_request"]["decision_note"], "Opened by desk"
        )
        mock_pub.assert_called()


class OperatorWalletValidateTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Op Wallet Project",
            wallet_enabled=True,
            pay_at_exit_enabled=False,
        )
        self.site = Site.objects.create(
            project=self.project,
            name="Lot Op",
            hourly_rate=Decimal("2.00"),
            additional_fee=Decimal("1.00"),
            grace_minutes=15,
        )
        with patch("sites.signals.publish_once", return_value=True):
            self.entry = Device.objects.create(
                site=self.site,
                ip="192.168.1.10",
                username="admin",
                password="secret",
                type=Device.Type.ENTRY,
            )
            self.exit = Device.objects.create(
                site=self.site,
                ip="192.168.1.11",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )
        self.fleet = Wallet.objects.create(
            project=self.project,
            name="Fleet",
            is_active=True,
            balance=Decimal("20.00"),
        )
        Vehicle.objects.create(wallet=self.fleet, plate="OPV001", is_active=True)
        self.operator = User.objects.create_user(
            username="cashier",
            password="pass",
            is_staff=True,
        )
        self.operator_wallet = Wallet.objects.create(
            project=self.project,
            name="Cashier wallet",
            is_active=True,
            assigned_user=self.operator,
            balance=Decimal("0.00"),
        )

    def test_assign_operator_wallet_api(self):
        """Ops API cannot create/assign operator wallets — Django admin only."""
        from rest_framework.test import APIClient

        other = User.objects.create_user(
            username="otherstaff", password="pass", is_staff=True
        )
        api = APIClient()
        api.force_authenticate(user=self.operator)
        created = api.post(
            "/api/wallets/",
            {
                "project": self.project.pk,
                "name": "Desk B",
                "is_active": True,
                "assigned_user": other.pk,
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertFalse(created.data["is_operator_wallet"])
        self.assertIsNone(created.data["assigned_user"])

        patch = api.patch(
            f"/api/wallets/{self.operator_wallet.pk}/",
            {"assigned_user": other.pk, "name": "Cashier wallet renamed"},
            format="json",
        )
        self.assertEqual(patch.status_code, 200, patch.data)
        self.assertEqual(patch.data["assigned_user"], self.operator.pk)
        self.assertEqual(patch.data["name"], "Cashier wallet renamed")

        staff = api.get("/api/auth/staff/")
        self.assertEqual(staff.status_code, 200)

        blocked = api.post(
            "/api/vehicles/",
            {
                "wallet": self.operator_wallet.pk,
                "plate": "NOPE1",
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(blocked.status_code, 400)

    @patch("parking.services.publish_once", return_value=True)
    def test_validate_payment_charges_operator_and_sets_grace(self, mock_pub):
        from datetime import timedelta

        from django.utils import timezone

        from parking.models import Session
        from parking.services import validate_payment_access_request

        authorize_plate(self.entry, "OPV001")
        session = Session.objects.get(plate="OPV001", end_time__isnull=True)
        session.start_time = timezone.now() - timedelta(hours=2)
        session.allow_negative_balance = False
        session.save(update_fields=["start_time", "allow_negative_balance"])
        self.fleet.balance = Decimal("0.00")
        self.fleet.save(update_fields=["balance"])

        denied = authorize_plate(self.exit, "OPV001")
        self.assertFalse(denied.granted)
        ar = denied.access_request
        self.assertIsNotNone(ar)
        self.assertIn("insufficient", (ar.reason or "").lower())

        fleet_before = self.fleet.balance
        validated = validate_payment_access_request(ar, self.operator)
        self.assertEqual(validated.status, AccessRequest.Status.APPROVED)
        session.refresh_from_db()
        self.assertEqual(session.payment_status, Session.PaymentStatus.PAID)
        self.assertIsNotNone(session.paid_exit_until)
        self.assertIsNotNone(session.end_time)
        self.operator_wallet.refresh_from_db()
        self.fleet.refresh_from_db()
        self.assertLess(self.operator_wallet.balance, Decimal("0.00"))
        self.assertEqual(self.fleet.balance, fleet_before)
        mock_pub.assert_called()

        again = authorize_plate(self.exit, "OPV001")
        self.assertTrue(again.granted)

    @patch("parking.services.publish_once", return_value=True)
    def test_residual_after_grace_requires_validate_again(self, mock_pub):
        from datetime import timedelta

        from django.utils import timezone

        from parking.models import Session
        from parking.services import validate_payment_access_request
        from wallets.models import Transaction

        authorize_plate(self.entry, "OPV001")
        session = Session.objects.get(plate="OPV001", end_time__isnull=True)
        session.start_time = timezone.now() - timedelta(hours=2)
        session.allow_negative_balance = False
        session.save(update_fields=["start_time", "allow_negative_balance"])
        self.fleet.balance = Decimal("0.00")
        self.fleet.save(update_fields=["balance"])

        denied = authorize_plate(self.exit, "OPV001")
        validate_payment_access_request(denied.access_request, self.operator)
        session.refresh_from_db()
        session.paid_exit_until = timezone.now() - timedelta(minutes=1)
        session.save(update_fields=["paid_exit_until"])

        residual = authorize_plate(self.exit, "OPV001")
        self.assertFalse(residual.granted)
        self.assertIsNotNone(residual.access_request)
        self.assertIn(
            "Wallet / fleet payment",
            residual.access_request.reason or "",
        )

        before_tx = Transaction.objects.filter(
            wallet=self.operator_wallet,
            transaction_type=Transaction.Type.OPERATOR_VALIDATE,
        ).count()
        validate_payment_access_request(residual.access_request, self.operator)
        after_tx = Transaction.objects.filter(
            wallet=self.operator_wallet,
            transaction_type=Transaction.Type.OPERATOR_VALIDATE,
        ).count()
        self.assertGreater(after_tx, before_tx)
        again = authorize_plate(self.exit, "OPV001")
        self.assertTrue(again.granted)


class CashierHubTests(TestCase):
    def setUp(self):
        from accounts.models import Cashier

        self.project = Project.objects.create(
            name="Cash Hub",
            wallet_enabled=True,
            pay_at_exit_enabled=False,
        )
        self.site = Site.objects.create(
            project=self.project,
            name="Lot C",
            hourly_rate=Decimal("2.00"),
            additional_fee=Decimal("1.00"),
            grace_minutes=15,
        )
        with patch("sites.signals.publish_once", return_value=True):
            self.entry = Device.objects.create(
                site=self.site,
                ip="10.0.0.10",
                username="admin",
                password="secret",
                type=Device.Type.ENTRY,
            )
            self.exit = Device.objects.create(
                site=self.site,
                ip="10.0.0.11",
                username="admin",
                password="secret",
                type=Device.Type.EXIT,
            )
        self.zone = self.entry.zone
        self.fleet = Wallet.objects.create(
            project=self.project,
            name="Fleet",
            is_active=True,
            balance=Decimal("50.00"),
        )
        Vehicle.objects.create(wallet=self.fleet, plate="CSH100", is_active=True)
        self.cashier_user = User.objects.create_user(
            username="desk",
            password="pass",
            is_staff=True,
        )
        self.cashier_user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="parking",
                codename="decide_accessrequest",
            )
        )
        Wallet.objects.create(
            project=self.project,
            name="Desk cash",
            is_active=True,
            assigned_user=self.cashier_user,
            balance=Decimal("0.00"),
        )
        Cashier.objects.create(user=self.cashier_user, zone=self.zone)

    @patch("parking.services.publish_once", return_value=True)
    def test_cashier_validate_keeps_session_open_for_grace_exit(self, mock_pub):
        from datetime import timedelta

        from django.utils import timezone
        from rest_framework.test import APIClient

        from parking.cashier import validate_open_session_for_exit
        from parking.models import Session
        from parking.services import verify_wallet_access

        authorize_plate(self.entry, "CSH100")
        session = Session.objects.get(plate="CSH100", end_time__isnull=True)
        session.start_time = timezone.now() - timedelta(hours=2)
        session.save(update_fields=["start_time"])

        updated = validate_open_session_for_exit(
            session, self.cashier_user, note="desk cash"
        )
        self.assertIsNone(updated.end_time)
        self.assertEqual(updated.payment_status, Session.PaymentStatus.PAID)
        self.assertIsNotNone(updated.paid_exit_until)
        self.assertTrue(verify_wallet_access("CSH100", device=self.exit))

        api = APIClient()
        api.force_authenticate(user=self.cashier_user)
        search = api.get("/api/cashier/search/", {"q": "CSH10"})
        self.assertEqual(search.status_code, 200)
        plates = [row["plate"] for row in search.data["results"]]
        self.assertIn("CSH100", plates)
        hit = next(row for row in search.data["results"] if row["plate"] == "CSH100")
        self.assertFalse(hit["can_validate"])
        self.assertTrue(hit["within_paid_exit_grace"])

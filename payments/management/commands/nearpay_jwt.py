from django.core.management.base import BaseCommand, CommandError

from payments.nearpay import NearPayConfigError, build_nearpay_payload, mint_nearpay_jwt


class Command(BaseCommand):
    help = (
        "Mint a NearPay Terminal SDK JWT (RS256) using pos_key.pem. "
        "Paste the token into parking-payment EXPO_PUBLIC_NEARPAY_AUTH_VALUE for sandbox."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--terminal-id",
            required=True,
            help="NearPay Tid (same value as KioskCredential.nearpay_terminal_id).",
        )
        parser.add_argument(
            "--client-uuid",
            default="",
            help="Override NEARPAY_CLIENT_UUID (Client dashboard).",
        )
        parser.add_argument(
            "--merchant-uuid",
            default="",
            help="Override NEARPAY_MERCHANT_UUID (Merchant dashboard).",
        )
        parser.add_argument(
            "--show-payload",
            action="store_true",
            help="Print the JWT payload JSON before the token.",
        )

    def handle(self, *args, **options):
        terminal_id = (options["terminal_id"] or "").strip()
        if not terminal_id:
            raise CommandError("--terminal-id is required.")
        client_uuid = options["client_uuid"] or None
        merchant_uuid = options["merchant_uuid"] or None
        try:
            if options["show_payload"]:
                payload = build_nearpay_payload(
                    terminal_id=terminal_id,
                    client_uuid=client_uuid,
                    merchant_uuid=merchant_uuid,
                )
                self.stdout.write(str(payload))
            token = mint_nearpay_jwt(
                terminal_id=terminal_id,
                client_uuid=client_uuid,
                merchant_uuid=merchant_uuid,
            )
        except NearPayConfigError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(token)

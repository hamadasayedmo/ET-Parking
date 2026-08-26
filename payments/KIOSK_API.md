# Exit kiosk pay-at-exit API

Platform flow:

1. Exit camera/MQTT → platform holds gate (`AccessRequest` pending).
2. Platform calculates open-session fee → creates `PaymentIntent` (no PSP call).
3. Platform pushes the bill over **SSE** to the exit kiosk.
4. Kiosk charges NearPay (or mock) and posts the result.
5. On paid, session is marked paid and the gate pulses.

### Local test (simulate session + exit hold)

```bash
# from parking-platform/
./scripts/sim_pay_at_exit.sh --plate TEST001 --hours 2
# or PowerShell:
.\scripts\sim_pay_at_exit.ps1 --plate TEST001 --hours 2
# bind to your kiosk exit:
./scripts/sim_pay_at_exit.sh --plate TEST001 --exit-device-id <exit_id>
```

Base: `/api/payments/`

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET/POST | `nearpay/jwt/` | Kiosk token | Mint NearPay JWT (Tid from credential) |
| POST | `intents/` | Staff JWT | Manually create intent `{access_request_id}` |
| POST | `kiosk/pairing-codes/` | Staff JWT | Mint one-time pairing code |
| POST | `kiosk/pair/` | — | Redeem pairing code → kiosk token |
| GET | `kiosk/me/` | Kiosk token | Bound exit device, branding, NearPay Tid |
| GET | `kiosk/pending/` | Kiosk token or staff | Snapshot of open bills |
| GET | `kiosk/events/` | Kiosk token or staff JWT | **SSE** live bills (kiosk online = SSE connected) |
| POST | `kiosk/payments/{id}/complete/` | Kiosk token or staff | Report paid/failed |

### Branding & receipts (app config)

```env
KIOSK_POWERED_BY_NAME=Your Brand
KIOSK_RECEIPT_BASE_URL=https://receipts.example.com
```

After a successful pay, the kiosk sends `card_last4`. The platform stores it as `pin` (no `receipt_url` field). The kiosk builds:

```text
{receipt_base_url}/receipts?plate={plate}&pin={pin}
```

`receipt_base_url` comes from `GET /kiosk/me/`. Full public receipt API (lookup rules, flat JSON including ZATCA Phase-1 fields, Project `country` / `seller_name` / `vat_number`): see **[RECEIPTS.md](RECEIPTS.md)**.

```http
GET /api/payments/receipts/?plate=ABC123&pin=4242
```

Complete payload:

```json
{
  "status": "paid",
  "provider": "nearpay",
  "provider_transaction_id": "...",
  "card_last4": "4242",
  "raw_result": {}
}
```

`GET /api/payments/kiosk/me/` returns `powered_by_name` and `receipt_base_url`.

### Pairing

Admin → Payments → Kiosk credentials → **Mint pairing code**, or:

```http
POST /api/payments/kiosk/pairing-codes/
Authorization: Bearer <staff_jwt>
{"credential_id": 1}
```

```http
POST /api/payments/kiosk/pair/
{"code": "AB7K2Q"}
```

Revoke from admin: **Revoke pairing (rotate token)** or deactivate the credential.

### NearPay JWT

```
NEARPAY_PRIVATE_KEY_PATH=.../pos_key.pem
NEARPAY_CLIENT_UUID=...   # or NEARPAY_MERCHANT_UUID
```

Tid is per exit on `KioskCredential.nearpay_terminal_id`.

```http
GET /api/payments/nearpay/jwt/
Authorization: Bearer <kiosk_token>
```

### Complete

```http
POST /api/payments/kiosk/payments/42/complete/
Authorization: Bearer <kiosk_token>
Content-Type: application/json

{
  "status": "paid",
  "provider": "nearpay",
  "provider_transaction_id": "psp-tx-id",
  "raw_result": {}
}
```

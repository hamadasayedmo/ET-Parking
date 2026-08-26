## Base URL

```
https://api.parking.etech.sa.com
```
## Endpoint

```http
GET /api/payments/receipts/?plate={plate}&pin={pin}
```

| Item      | Detail                                    |
| --------- | ----------------------------------------- |
| Auth      | None (`AllowAny`)                         |
| Success   | `200` JSON body                           |
| Not found | `404` `{"detail": "Receipt not found."}`  |
### Query parameters
| Param   | Required  | Description                                                       |
| ------- | --------- | ----------------------------------------------------------------- |
| `plate` | yes       | Vehicle plate (normalized the same way as sessions)               |
| `pin`   | yes       | Last 4 digits of the paying card (stored on the intent as `pin`)  |
### Lookup rules
1. Find the **latest paid** `PaymentIntent` for that plate (`status=paid`, `order_by -created_at`).
2. Match only if `pin` equals that intent’s stored card last-4.
3. Otherwise return 404 (wrong pin, unpaid only, no sessions, or empty params).
## Response body (flat object)
```json
{
  "plate": "ABC123",
  "amount": "3.00",
  "currency": "SAR",
  "site_name": "Lot A",
  "session_start": "2026-08-03T04:00:00+00:00",
  "session_end": "2026-08-03T06:00:00+00:00",
  "paid_at": "2026-08-03T06:55:48+00:00",
  "powered_by_name": "Your Brand",
  "country": "SA",
  "seller_name": "Example Parking Co",
  "vat_number": "310175397400003",
  "vat_rate": "15.00",
  "invoice_timestamp": "2026-08-03T06:55:48+00:00",
  "invoice_total_with_vat": "3.00",
  "vat_amount": "0.39",
  "net_amount": "2.61",
  "invoice_number": "sess-1-abc123def456",
  "qr_base64": "<Base64 TLV tags 1–5>"
}
```
### Field notes
| Field                               | Notes                                                                                                          |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `amount` / `invoice_total_with_vat` | Intent amount; treated as **VAT-inclusive** for SA                                                             |
| `vat_rate`                          | `15.00` when `country=SA` (may vary by country later)                                                          |
| `vat_amount` / `net_amount`         | Split from inclusive total; empty when not SA                                                                  |
| `invoice_timestamp`                 | `paid_at` if set, else intent `created_at`                                                                     |
| `invoice_number`                    | Payment intent merchant reference (unique bill id)                                                             |
| `qr_base64`                         | ZATCA Phase-1 TLV Base64 when `country=SA` **and** both `seller_name` and `vat_number` are set; otherwise `""` |
## Example

```bash
curl -s "https://api.parking.etech.sa.com/api/payments/receipts/?plate=ABC123&pin=4242"
```

404 when the latest intent’s pin does not match:
```json
{"detail": "Receipt not found."}
```

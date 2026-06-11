# Spec: chat4000 pairing QR-image endpoint

**For:** an agent/engineer implementing this on the chat4000 web infra.
**Why:** the chat4000 installer (agent mode) hands the Telegram agent a QR so a
*second* device can scan it to pair. ASCII QRs render unscannably in Telegram
(its font distorts the half-block characters), so the installer instead posts a
QR **image** URL as image-markdown — `![](<this endpoint>)` — which Telegram
renders as a real, scannable image. This endpoint serves that image.

The installer already points at this exact URL
(`QR_IMAGE_URL_TEMPLATE` in `scripts/installer.py`):

```
https://pair.chat4000.com/qr?code=<CODE>
```

Implement it to match, or tell me the final URL and I'll update the installer.

## Endpoint

`GET https://pair.chat4000.com/qr?code=<CODE>`

### Request
- `code` (required) — the 6-digit pairing code, raw digits, e.g. `673697`.
  Validate: exactly 6 ASCII digits (`^[0-9]{6}$`). Reject anything else with
  `400` (do not render a QR for malformed input).
- Optional `size` (px, default 512, clamp 128–1024) and `ecc`
  (`L|M|Q|H`, default `M`) — nice-to-have, not required.

### Response (success)
- `200 OK`
- `Content-Type: image/png` (a PNG; SVG is acceptable only if Telegram renders
  it inline — PNG is the safe choice).
- Body: a QR code image encoding **exactly** the canonical pairing payload (see
  below). Quiet zone (border) ≥ 4 modules. Black on white, high contrast.

### What the QR must ENCODE
The QR must encode the **app pairing deep link**, byte-for-byte the same string
the plugin's `chat4000 pair` command already prints as its QR payload:

```
chat4000://pair?code=<CODE>
```

(Confirm against the iOS/Mac app's QR scanner — whatever payload the app accepts
when scanning to pair is the one to encode. If the app expects the web link
`https://pair.chat4000.com/?code=<CODE>` instead, encode that — but it must be a
single canonical choice, and it must match what the app scans.)

> The endpoint takes only `code` and builds the payload server-side, so the
> encoded format can change here later with **zero installer changes**.

### Response (errors)
- `400 Bad Request` — missing/malformed `code` (not 6 digits). Plain text body.
- `404`/`410` are NOT required (the endpoint doesn't know if a code is live; it
  just renders). Rendering a QR for an expired code is harmless — it just won't
  redeem.

## Hard requirements
1. **First-party only.** This MUST be served by chat4000 infra. Do NOT proxy to
   or redirect to a third-party QR service (e.g. `api.qrserver.com`,
   `chart.googleapis.com`): the pairing code is a live, redeemable credential for
   ~5 minutes — handing it to a third party lets them redeem it. Generate the QR
   in-process (any server-side QR lib).
2. **No caching of the code.** Send `Cache-Control: no-store` (codes are
   short-lived and single-use; never cache or log the full code longer than the
   request). Don't put the code in access logs if avoidable.
3. **Cheap + fast.** Pure render, no DB, no auth. Should respond in well under a
   second; Telegram fetches it server-side when the agent posts the image.
4. **CORS not needed** (Telegram fetches server-side), but harmless to allow.
5. **HTTPS, valid cert** (Telegram won't fetch an image from a bad-cert host).

## Acceptance test
```
curl -sS -o /tmp/qr.png -D - "https://pair.chat4000.com/qr?code=673697"
# → 200, Content-Type: image/png, Cache-Control: no-store
file /tmp/qr.png            # → PNG image data
# Scan /tmp/qr.png → decodes to exactly: chat4000://pair?code=673697
curl -s -o /dev/null -w '%{http_code}\n' "https://pair.chat4000.com/qr?code=abc"   # → 400
```

When it's live, the installer's QR works with no further changes. If you deploy
it at a different path/host, tell me the URL and I'll update
`QR_IMAGE_URL_TEMPLATE`.

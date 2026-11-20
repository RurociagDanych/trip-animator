# Security

Controls implemented in the code and configuration. This is not an independent audit; deployment-specific risks remain the operator's responsibility. See also [THREAT_MODEL.md](THREAT_MODEL.md) and the reporting policy in [../SECURITY.md](../SECURITY.md).

## Operator checklist

- Use strong, unique secrets; restrict ingress and egress.
- Protect uploaded location data and share links.
- Keep dependencies and base images current.
- Set up backups, monitoring, retention and incident response.
- Use synthetic GPS data for demos and screenshots. Publishing real route imagery needs the route owner's permission; keep source activity files and private photos out of the repository.

## Implemented controls

- **Authentication & Sessions (`A07`, `A01`)**: Passwords hashed with **Argon2id** (`argon2-cffi`); constant-time login dummy verification prevents user enumeration. 256-bit opaque session tokens stored strictly as `SHA-256` HMAC digests in PostgreSQL `sessions`; cookies set `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` in production.
- **CSRF & Rate Limiting (`A01`, `A04`)**: State-changing routes validate `X-CSRF-Token` using `hmac.compare_digest`. Login, GPX/Strava import, and render creation enforce persistent PostgreSQL-backed rate limits (`rate_limits`).
- **Fail-Closed Production Guards (`A05`)**: When `APP_ENV=production`, startup refuses `DEV_AUTH_ENABLED=true`, default/short `SESSION_SECRET` (< 32 chars), placeholder `TOKEN_ENCRYPTION_KEY`, or missing S3 credentials. `/api/docs` and `/api/openapi.json` are disabled in production, and `GET /metrics` fails closed unless `METRICS_TOKEN` is configured.
- **Strava OAuth2 PKCE & Token Encryption (`A02`)**: Import-only OAuth2 **PKCE (`S256`)** with single-use, session-bound `state`. Access and refresh tokens are encrypted at rest with versioned **AEAD (`AES-256-GCM`)** bound to user/provider AAD (`oauth:{user_id}:strava`). Supports explicit disconnect + Strava deauthorization webhook purge.
- **SSRF, XXE & Command Injection Prevention (`A03`, `A10`)**:
  - `defusedxml` blocks DTDs, external entities, and billion-laughs expansion in an isolated subprocess.
  - Outbound `httpx` calls and Playwright `page.route()` enforce explicit host allowlists (`www.strava.com`, `MAP_TILE_ALLOWLIST`, `RENDER_BASE_URL`), block `file://`/non-HTTP schemes, block HTTP redirects, and reject RFC1918/loopback/link-local/ULA IPs.
  - `ffmpeg` and `ffprobe` execute with fixed `argv` lists (`shell=False`) and `-` path guards; `LocalObjectStore` rejects path traversal.
- **Container & Edge Hardening (`A05`, `A09`)**: Caddy enforces CSP, HSTS, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `25MB` max upload body, and blocks external `/metrics` access (`403`). Application containers run as non-root (`10001`/`101`) with `read_only: true`, `cap_drop: ALL`, and `no-new-privileges:true`.

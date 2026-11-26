# Security and publication safety

## Reporting an issue

Do not put credentials, private activity coordinates, photos, session cookies, or
share links in public issues or pull requests. Use a private GitHub security
advisory if private vulnerability reporting is enabled for this repository.
Otherwise, ask the maintainer to arrange a private reporting channel without
including sensitive details in the public request.

## Before publishing or sharing a demo

- Use synthetic activities and generated images. Review screenshots manually for
  private routes, names, email addresses, browser URLs, share links, and tokens;
  inspect image metadata as well as the visible pixels. A secret scanner does
  not establish that location data or photographs are safe to publish.
- Keep real `.env` files, credentials, object-store data, databases, raw activity
  files, and Terraform state and saved plans outside version control. Ignore
  rules are preventive only: they do not remove previously tracked content or
  make force-added files safe. Do not distribute the working directory or `.git`
  directory as a substitute for a reviewed source export.
- Treat `.env.example`, development Compose defaults, and Terraform example
  values as public development placeholders, never as production credentials.
  Replace them and restrict network exposure before deploying a public service.
  Terraform state can contain generated passwords even when outputs are marked
  `sensitive`; do not attach state or plan files to issues or build artifacts.
- Scan both the complete locally available Git history and the exact publication
  candidate, including intended untracked files. Use a full clone in CI; a
  shallow checkout cannot validate earlier history. Review the candidate file
  list rather than scanning an entire developer directory containing real data.
- Use Gitleaks 8.30.1 or a reviewed newer release with `.gitleaks.toml`, full
  redaction (`--redact=100`), and `--ignore-gitleaks-allow`. Keep reports only in a
  private temporary location outside the repository. The checked-in configuration
  extends all default rules and limits exceptions to reviewed, exact non-secret
  lines in specific files. Do not bypass findings by excluding source directories
  or adding broad allowlists. Review scanner warnings and omissions as well as
  its exit status; a clean scan is not proof that no secrets exist.
- If an actual credential was committed, stop publication, revoke or rotate it,
  and coordinate history cleanup with the repository owner. Removing a file or
  adding an ignore rule does not revoke a secret or erase it from history. Do not
  rewrite shared history without explicit authorization.

## Audit scope limits

A local review covers only the refs, reflogs, and files available at review time.
Recheck the final candidate after other changes. Hosting-provider secrets,
remote-only refs, Git LFS payloads, external object storage, and future uploads
require their own access and review. Ignored local data is intentionally not a
publication candidate and should not be uploaded to a scanning service.

## Security controls

Controls implemented in the code and configuration. This is not an independent audit; deployment-specific risks remain the operator's responsibility. See also [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) and the reporting policy above.

### Operator checklist

- Use strong, unique secrets; restrict ingress and egress.
- Protect uploaded location data and share links.
- Keep dependencies and base images current.
- Set up backups, monitoring, retention and incident response.
- Use synthetic GPS data for demos and screenshots. Publishing real route imagery needs the route owner's permission; keep source activity files and private photos out of the repository.

### Implemented controls

- **Authentication & Sessions (`A07`, `A01`)**: Passwords hashed with **Argon2id** (`argon2-cffi`); constant-time login dummy verification prevents user enumeration. 256-bit opaque session tokens stored strictly as `SHA-256` HMAC digests in PostgreSQL `sessions`; cookies set `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` in production.
- **CSRF & Rate Limiting (`A01`, `A04`)**: State-changing routes validate `X-CSRF-Token` using `hmac.compare_digest`. Login, GPX/Strava import, and render creation enforce persistent PostgreSQL-backed rate limits (`rate_limits`).
- **Fail-Closed Production Guards (`A05`)**: When `APP_ENV=production`, startup refuses `DEV_AUTH_ENABLED=true`, default/short `SESSION_SECRET` (< 32 chars), placeholder `TOKEN_ENCRYPTION_KEY`, missing `SMTP_HOST` when `REGISTRATION_ENABLED=true`, half-configured Turnstile credentials, or missing S3 credentials. `/api/docs` and `/api/openapi.json` are disabled in production, and `GET /metrics` fails closed unless `METRICS_TOKEN` is configured.
- **Strava OAuth2 PKCE & Token Encryption (`A02`)**: Import-only OAuth2 **PKCE (`S256`)** with single-use, session-bound `state`. Access and refresh tokens are encrypted at rest with versioned **AEAD (`AES-256-GCM`)** bound to user/provider AAD (`oauth:{user_id}:strava`). Supports explicit disconnect + Strava deauthorization webhook purge.
- **SSRF, XXE & Command Injection Prevention (`A03`, `A10`)**:
  - `defusedxml` blocks DTDs, external entities, and billion-laughs expansion in an isolated subprocess.
  - Outbound `httpx` calls and Playwright `page.route()` enforce explicit host allowlists (`www.strava.com`, `challenges.cloudflare.com`, `MAP_TILE_ALLOWLIST`, `RENDER_BASE_URL`), block `file://`/non-HTTP schemes, block HTTP redirects, and reject RFC1918/loopback/link-local/ULA IPs.
  - `ffmpeg` and `ffprobe` execute with fixed `argv` lists (`shell=False`) and `-` path guards; `LocalObjectStore` rejects path traversal.
- **Container & Edge Hardening (`A05`, `A09`)**: Caddy enforces CSP, HSTS, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `25MB` max upload body, and blocks external `/metrics` access (`403`). Application containers run as non-root (`10001`/`101`) with `read_only: true`, `cap_drop: ALL`, and `no-new-privileges:true`.

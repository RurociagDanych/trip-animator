# Trip Animator — OWASP Threat Model & Security Control Matrix

This document establishes the security architecture for **Trip Animator**, aligned with the **OWASP Top 10 (2021)** and **OWASP ASVS Level 2** baselines.

---

## 1. Trust Boundaries & Crown-Jewel Assets

| Asset / Boundary | Sensitivity | Exposure | Primary Protection |
| :--- | :--- | :--- | :--- |
| **Session Cookies & Passwords** | Critical | Browser <-> Caddy <-> API | `Argon2id` hashes, SHA-256 hashed 256-bit opaque cookies (`HttpOnly`, `SameSite=Lax`, `Secure`), constant-time CSRF |
| **Strava OAuth2 Access/Refresh Tokens** | Critical | Postgres `oauth_connections` | Versioned AEAD (`AES-256-GCM`) with `TOKEN_ENCRYPTION_KEY` + PKCE `S256` + single-use `state` |
| **Raw GPX Uploads & GPS Coordinates** | High (PII / Location) | Upload -> API -> Bronze/Silver | Streaming 25 MiB limit, `defusedxml` isolated subprocess, private coordinate redaction in logs |
| **PostgreSQL 17 & MinIO S3** | High | Internal Docker network only | Loopback-bound dev ports, non-public credentials, least-privilege bucket/schema access |
| **Playwright Render Worker** | High (Untrusted input + network egress) | `worker` container | `page.route()` URL allowlist, private IP/DNS-rebinding filter, `read_only: true`, `cap_drop: ALL`, fixed-argv FFmpeg |

---

## 2. Threat Model & Control Matrix

| Threat ID | Threat Scenario (STRIDE / OWASP) | Attack Vector | Required Mitigation & Verification Control |
| :--- | :--- | :--- | :--- |
| **TM-01** | **Credential Stuffing / Weak Password Storage** (`A07: Identification & Authentication Failures`) | Brute-force login, leaked DB hashes | Hash passwords with **Argon2id** (`argon2-cffi`). Enforce persistent PostgreSQL-backed rate limiting on `/api/v1/auth/login`. Refuse API startup if `DEV_AUTH_ENABLED=true` when `APP_ENV=production` or if `SESSION_SECRET` < 32 bytes. |
| **TM-02** | **Session Hijacking & CSRF** (`A01: Broken Access Control`, `A07`) | XSS cookie theft, cross-site state-changing form/fetch | Issue 256-bit cryptographically random opaque session tokens; store only `SHA-256(token)` in `sessions`. Set `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` (production). Require `X-CSRF-Token` header on all non-safe `/api/v1/*` routes verified via `hmac.compare_digest`. |
| **TM-03** | **OAuth2 Authorization Code Interception / Token Leak** (`A02: Cryptographic Failures`, `A07`) | Redirect URI hijacking, CSRF on OAuth callback, DB dump exposing Strava tokens | Strava integration is **import-only** (requires active authenticated session). Enforce **PKCE (`S256`)** and single-use session-bound `state` stored server-side with short TTL. Encrypt Strava access/refresh tokens at rest with versioned **AEAD (`AES-256-GCM`)** bound to user/provider AAD. Support Strava deauthorization webhook + explicit disconnect purge. |
| **TM-04** | **GPX XXE, Billion Laughs & Parser CPU/Memory DoS** (`A03: Injection`, `A05: Security Misconfiguration`) | Malicious `.gpx` containing `<!DOCTYPE>`, `<!ENTITY>`, deep XML nesting (10k levels), or 25 MiB stream stalls | Enforce `25MB` request limit at Caddy edge and streaming 25 MiB cap in FastAPI before buffering. Parse strictly with **`defusedxml`** (forbids DTDs, external entities, and entity expansion) inside an **isolated subprocess with hard 15s wall-clock timeout** and strict caps (depth <= 64, tracks <= 20, segments <= 1,000, points <= 500k/track). Sanitize all multipart/XML parse errors into generic JSON responses (`safeError`) with zero stack/path leaks. |
| **TM-05** | **SSRF & DNS Rebinding via Tile URLs or Browser Rendering** (`A10: Server-Side Request Forgery`) | Crafted style/tile URLs, redirects to cloud metadata (`169.254.169.254`), or internal service probing (`postgres`, `minio`, `api`) from headless Chromium | Enforce explicit outbound allowlists (`https://www.strava.com` for OAuth/API; `MAP_TILE_ALLOWLIST` + internal `RENDER_BASE_URL` for worker). Resolve hostnames and block RFC1918, loopback, link-local, and ULA CIDRs (`10.0.0.0/8`, `127.0.0.0/8`, `169.254.0.0/16`, `172.16.0.0/12`, `192.168.0.0/16`, `::1`, `fc00::/7`, `fe80::/10`) before outbound connections (`httpx` transport hook) and inside Playwright **`page.route()`** (rejecting `file://`, `ws://`, non-allowlisted hosts, and unsafe redirects). |
| **TM-06** | **Command Injection in Video Encoding** (`A03: Injection`) | Crafted project titles, stage labels, or filenames passed to `ffmpeg` / `ffprobe` | Invoke `ffmpeg` and `ffprobe` exclusively via **fixed `argv` lists** (`subprocess.run(..., shell=False)`), reading frames over stdin pipe and writing to UUID-named temp files under `/tmp`. Project/stage titles are never interpolated into shell or `drawtext` filter graphs (HUD rendering happens via escaped React DOM in Chromium). |
| **TM-07** | **Broken Object-Level Auth & Concurrency Races** (`A01: Broken Access Control`, `A04: Insecure Design`) | Accessing another user's project/GPX/MP4, injecting foreign stage UUIDs, duplicate render billing via `Idempotency-Key` | Scope every project, stage, dataset, and render query by `owner_user_id = current_user.id`. Validate `stage_colors` and `stage_transitions` keys strictly against the project's persisted stage IDs. Enforce optimistic locking via `revision` and verify `(owner_user_id, project_id, request_hash)` consistency on `Idempotency-Key` reuse. |
| **TM-08** | **XSS, Clickjacking & Header Downgrade** (`A05: Security Misconfiguration`) | Inline script injection, framing editor in attacker iframe, MIME sniffing | Serve UI and API on the **same origin** via Caddy (`infra/Caddyfile`). Apply strict **CSP** (`default-src 'self'`, explicit `connect-src`/`img-src`/`worker-src` for MapLibre/Deck.gl/OpenFreeMap, `object-src 'none'`, `frame-ancestors 'none'`, `base-uri 'self'`, `form-action 'self'`), `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, and `-Server`. |
| **TM-09** | **Container Escape & Root Filesystem Tampering** (`A05`, `A09`) | Exploited Chromium renderer or Python dependency writing to `/usr` or escalating privileges | Run `api`, `worker`, and `web` as **non-root UIDs** (`10001` / `101`) with `read_only: true`, `cap_drop: ["ALL"]`, `no-new-privileges:true`, and bounded `tmpfs` (`/tmp:size=256M` for `api`, `/tmp:size=2G` + `shm_size: 1gb` for `worker`). Pre-install Playwright Chromium and DuckDB `spatial` extension at image build time. |
| **TM-10** | **Data Retention & PII Exhaustion** (`A04`, `A09`) | Orphaned MP4 outputs, expired sessions, unpurged Strava tokens after revocation, raw GPS logs | Run scheduled retention reaper pruning expired `sessions`, expired `render_jobs` output artifacts (`outputs/{jobId}.mp4` & `.webp`), orphaned Bronze/Silver/LOD artifacts after project/stage deletion, and revoked Strava tokens. Redact raw coordinates, secrets, and `Authorization`/cookie headers from structured logs. |

---

## 3. Security Verification Checklist (CI & Pre-Release)

- [x] **Unit & Security Tests (`pytest`)**:
  - XXE payload (`<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>`) rejected cleanly with 4xx.
  - Billion-laughs entity expansion rejected immediately by `defusedxml`.
  - Adversarial nested/huge XML stalls terminated by 15s subprocess timeout without blocking API worker loop.
  - Malformed multipart boundary returns sanitized JSON error (`safeError`) without stack traces.
  - SSRF suite verifies `127.0.0.1`, `169.254.169.254`, `10.0.0.1`, `file:///etc/passwd`, and unapproved domains are blocked in both `httpx` and Playwright `page.route()`.
  - `APP_ENV=production` startup fails closed when `DEV_AUTH_ENABLED=true`, `SESSION_SECRET` < 32 bytes, `TOKEN_ENCRYPTION_KEY` is invalid, default DB/S3 credentials are used, or `ALLOWED_ORIGINS` contains `*`.
- [x] **Static & Quality Gates (CI)**:
  - `ruff check` (including Bandit `S` security rules), `mypy --strict`, `tsc --noEmit`, `eslint`, `dbt parse`, `docker compose config`, and Airflow DAG integrity checks (including AST secret scan).
  - Recommended pre-release supply-chain scans: `pip-audit`, `pnpm audit`, container image scan (`trivy`), and CycloneDX SBOM generation.

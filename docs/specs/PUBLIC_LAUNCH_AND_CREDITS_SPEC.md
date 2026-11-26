# Public Launch, Identity, and Server Render Credits Specification

> Purpose: Specify open user registration, Google OIDC sign-in, voucher-gated Render Credits for AWS Lambda Server Renders, and operational hardening required for a public medium-scale launch.
> Audience: contributors, operators, reviewers.
> Last reviewed: 2026-11-26.

Sections:

- Problem Statement
- Solution Overview
- Domain Vocabulary
- User Stories
- Architecture & Implementation Decisions
- Delivery Slices
- Testing Decisions & Seams
- Operator Launch Checklist
- Out of Scope

## Problem Statement

Trip Animator currently gates account creation behind static `REGISTRATION_INVITE_CODE` values in `.env`, offers only email-and-password authentication, and allows any signed-in user to submit unbounded Server Renders on the AWS Step Functions + Lambda backend (`RENDER_BACKEND=aws_sfn_lambda`) without cost controls, per-user storage quotas, or global concurrency ceilings. Opening the platform to 50–500 external users without closing these gaps exposes the deployment to bot sign-ups, synchronous SMTP blocking, email enumeration, unbounded AWS Lambda bills, PostgreSQL connection exhaustion on SSE streams, and missing privacy/account-deletion controls required for public Google OAuth verification.

## Solution Overview

Open self-service registration to everyone (email+password with mandatory email verification, plus optional Google OIDC sign-in) while keeping **Local Export** free and unlimited in the browser and gating **Server Render** behind prepaid **Render Credits**. During beta/testing—before real payment processing exists—operators distribute **Credit Vouchers** via CLI (`trip-api`) that users redeem into an append-only **Credit Ledger**. Each Server Render is priced proportionally to rendered pixel-frames (`width * height * fps * film_seconds`), debited atomically on submission, and protected by per-user quotas and a global `Remote Execution` concurrency cap.

## Domain Vocabulary

Canonical definitions live in [GLOSSARY.md](../../GLOSSARY.md):

- **Local Export**: Browser-rendered MP4 export; always free and never consumes Render Credits.
- **Server Render**: Backend-generated MP4 export (local worker or `aws_sfn_lambda`) billed in Render Credits.
- **Render Credit**: Prepaid usage unit spent on Server Renders, scaled proportionally to output resolution, frame rate, film duration, and 3D terrain overhead.
- **Credit Voucher**: Database-backed redeemable code granting Render Credits to a user's balance (supports single-use and capped multi-use codes; replaces the retired registration invite code).
- **Credit Ledger**: Append-only ledger of all credit grants, render debits, render refunds, and operator adjustments from which a user's current balance is derived.
- **Render Backend**, **Render Bundle**, **Remote Execution**: Defined in [GLOSSARY.md](../../GLOSSARY.md#render-execution) and [REMOTE_RENDER_BACKEND_SPEC.md](REMOTE_RENDER_BACKEND_SPEC.md).

## User Stories

1. As a new visitor, I want to create an account with my email and password without an invite code, so that I can start building trip animations immediately after verifying my email.
2. As a new or returning user, I want to sign in with my Google account, so that I do not have to create or remember a separate password.
3. As a user with an existing verified email+password account, I want signing in with Google using the same verified email to attach to my existing workspace, so that my projects are not fragmented across duplicate accounts.
4. As a Google-only user, I want to optionally set a password from my Account page, so that I can also sign in with email and password if needed.
5. As a visitor reviewing the sign-in or sign-up screen, I want clear links to the Privacy Policy (`/privacy`) and Terms of Service (`/terms`), so that I understand how GPS tracks, photos, and Strava data are handled.
6. As a user, I want to export MP4s via **Local Export** with zero Render Credits, so that I can use the editor freely without a voucher.
7. As a beta tester given a **Credit Voucher**, I want to redeem the code in my Account page or directly in the Editor's Export panel, so that my balance is credited immediately and **Server Render** unlocks.
8. As a user configuring a **Server Render**, I want to see the exact Render Credit cost before starting the export, so that there are no surprises when I change resolution, FPS, duration, photo stops, or 3D terrain.
9. As a user whose **Server Render** fails on the backend, I want my debited Render Credits automatically refunded, so that infrastructure failures never consume my balance.
10. As a user who cancels a queued **Server Render** before AWS execution starts, I want my Render Credits refunded; once remote execution has started, cancellation stops further work without a refund.
11. As a user re-exporting an unchanged project that hits the render artifact cache, I want the cached export to cost `0` Render Credits, so that I am not double-charged for identical output.
12. As a user, I want to inspect my current credit balance and recent **Credit Ledger** history on my Account page, so that I can audit grants, debits, and refunds.
13. As a user, I want to permanently delete my account and all associated projects, tracks, photos, and OAuth tokens from the Account page, so that I control my personal data.
14. As an operator, I want to create, list, and revoke **Credit Vouchers** and grant manual adjustments from `trip-api` CLI without building an admin UI.
15. As an operator, I want registration protected by optional Cloudflare Turnstile, anti-enumeration responses, async SMTP delivery, fail-closed production SMTP validation, per-user resource quotas, global AWS Lambda concurrency caps, scalable SSE, automatic retention reaping, and a VPS backup script, so that the single-VPS + AWS Lambda stack stays predictable and cost-bounded for 50–500 accounts.

## Architecture & Implementation Decisions

### 1. Open Registration, Async SMTP, Anti-Bot & Legal Pages (PR 1)

- **Open Registration**:
  - Replace invite-code-derived `registration_enabled` with an explicit boolean setting `REGISTRATION_ENABLED` (default `true`). Remove `REGISTRATION_INVITE_CODE` enforcement from `POST /api/v1/auth/register`.
  - Keep `POST /api/v1/auth/register` requiring `email`, `display_name`, and `password` (>= 12 chars), plus optional `turnstile_token`.
  - **Anti-enumeration**: Return uniform `202 Accepted` with `{status: "verification_required", email, message}` whether the email is brand new, already pending verification, or already registered.
    - New email: create inactive/unverified user (`email_verified_at = NULL`), issue verification token, schedule verification email.
    - Existing unverified active user: issue fresh verification token (do not overwrite password), schedule verification email.
    - Existing verified or inactive user: run constant-time dummy Argon2id work to normalize timing, write an audit event (`auth.register_existing_ignored`), do not create a duplicate user or leak account existence.
  - **Async Email Delivery**: Dispatch SMTP sends for `/register`, `/resend-verification`, and `/forgot-password` via FastAPI `BackgroundTasks` (never blocking the request handler on a 15 s SMTP timeout), logging delivery failures safely without secrets or token URLs.
  - **Production Fail-Closed**: When `APP_ENV=production` and `REGISTRATION_ENABLED=true`, require non-empty `SMTP_HOST`. When either `TURNSTILE_SITE_KEY` or `TURNSTILE_SECRET_KEY` is set, require both.
- **Optional Cloudflare Turnstile**:
  - Controlled by `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY` (disabled when both are empty).
  - Exposed via `GET /api/v1/public-config` as `turnstile_site_key: str | None` (never exposing the secret).
  - When enabled, both `POST /api/v1/auth/register` and `POST /api/v1/auth/forgot-password` require a valid `turnstile_token`, verified server-side against `https://challenges.cloudflare.com/turnstile/v0/siteverify` via `httpx` with outbound host allowlist (`challenges.cloudflare.com`) and short timeout.
  - Update Caddy CSP (`infra/Caddyfile`) to optionally allow `https://challenges.cloudflare.com` in `script-src`, `frame-src`/`child-src`, and `connect-src` when configured.
- **Legal Pages**:
  - Add public SPA routes `/privacy` and `/terms` rendered with consistent dark-canvas typography and accessible landmarks, linked from the sign-in/sign-up screen and footer.

### 2. Google OIDC Sign-In & Self-Service Account Deletion (PR 2)

- **Google OIDC (`GOOGLE_AUTH_CLIENT_ID`, `GOOGLE_AUTH_CLIENT_SECRET`, `GOOGLE_AUTH_REDIRECT_URI`)**:
  - Keep credentials strictly separate from the legacy `GOOGLE_CLIENT_*` Google Photos placeholders.
  - Use standard OAuth2 Authorization Code + PKCE (`S256`) + `nonce` + opaque `state` bound to a short-lived HttpOnly SameSite=Lax cookie (since login occurs before a user session exists).
  - Verify Google's token response and `id_token` (`iss`, `aud`, `exp`, `nonce`, `sub`, `email`, `email_verified`) via Google JWKS / tokeninfo over allowlisted `oauth2.googleapis.com` / `www.googleapis.com`.
  - Persist external login identities in a new `user_identities` table (`user_id`, `provider`, `provider_subject`, `email`, `created_at`, unique `(provider, provider_subject)`).
  - Make `users.password_hash` nullable (`TEXT NULL`), exposing `has_password: bool` on `UserOut`.
  - **Auto-linking rule**: Match first by `(provider="google", provider_subject=sub)`. If not found, look up by normalized email:
    - If no user exists: create active user with `password_hash=NULL` and `email_verified_at=now()`, insert identity, issue session.
    - If user exists and `email_verified_at IS NOT NULL` and Google `email_verified` is `true`: attach `user_identities` row and issue session.
    - If user exists and `email_verified_at IS NULL`: reject safely with a clear message instructing the user to verify or reset the existing email account (prevents unverified pre-hijacking).
  - `AccountPage` renders "Set a password" for users with `has_password === false` and "Change password" otherwise.
- **Self-Service Account Deletion**:
  - `DELETE /api/v1/auth/me` (CSRF-protected): requires `current_password` when `has_password=true`, or `confirmation == "DELETE"` when `has_password=false`.
  - Collects all user-owned object keys (stages Bronze/Silver/caches, photos/thumbs, render MP4s/thumbnails), revokes Strava token best-effort, deletes the `User` row (cascading all owned PostgreSQL records), clears the session cookie, and deletes `ObjectStore` objects.

### 3. Render Credits, Credit Ledger & Credit Vouchers (PR 3)

- **Deterministic Pricing Function** (shared canonical formula in `packages/telemetry` and mirrored in `apps/web`):
  - Baseline: `1920 × 1080 @ 30 fps` for `60 s` = `3,732,480,000` pixel-frames = `100` Render Credits.
  - `film_seconds = duration_seconds + intro_hold_seconds + outro_hold_seconds + cinematic_photo_stop_seconds` (exact stop duration computed from enabled matched photos when `photo_card_style == "cinematic_stop"` and `show_photos` is true).
  - `raw_credits = 100.0 * (width * height * fps * film_seconds) / (1920 * 1080 * 30 * 60)`
  - Multiply by `1.5` when `terrain_3d` is true.
  - Final cost: `max(10, math.ceil(raw_credits))`.
  - Expose `POST /api/v1/projects/{project_id}/renders/quote` (or embed quote calculation in frontend + server-authoritative check on `POST /renders`) so the UI shows exact credit cost before submission.
- **Credit Ledger & Balances**:
  - New `credit_ledger_entries` table: `id`, `user_id`, `delta` (non-zero integer), `entry_type` (`voucher_grant`, `render_debit`, `render_refund`, `admin_adjustment`), `render_job_id` (nullable FK `SET NULL`), `voucher_id` (nullable FK `SET NULL`), `idempotency_key` (unique nullable string), `note`, `created_at`.
  - New users start with balance `0`.
  - On `POST /api/v1/projects/{project_id}/renders`:
    - If `Idempotency-Key` matches an existing job, return it without re-debiting.
    - If `content_hash` hits an unexpired succeeded render and artifacts copy cleanly, cost is `0` credits (no ledger debit).
    - Otherwise, acquire `SELECT ... FROM users WHERE id = :uid FOR UPDATE` inside the commit transaction (never across S3/network I/O), verify `SUM(delta) >= cost`, insert `render_debit` entry (`delta = -cost`, `idempotency_key = f"render_debit:{job.id}"`), persist `RenderJob.credits_charged = cost`, and commit. Reject with `402 Payment Required` when balance is insufficient.
  - **Idempotent Refunds**:
    - Any transition to `failed` (local worker failure, remote `ReportFailure` callback, dispatch retry exhaustion, or reconciler failure) inserts `render_refund` with unique `idempotency_key = f"render_refund:{job.id}"` if `credits_charged > 0`.
    - User cancellation (`POST /api/v1/renders/{job_id}/cancel`):
      - For `render_backend == "aws_sfn_lambda"`: refund only when `remote_dispatched_at IS NULL` (before Step Functions `StartExecution` has been invoked); after dispatch, call `StopExecution` with no refund.
      - For `render_backend == "local"`: refund only when `status == "queued"` (before a worker starts rendering); once `running`, cancel with no refund.
- **Credit Vouchers**:
  - New `credit_vouchers` table: `id`, `code_hash` (unique SHA-256 of normalized code), `code_prefix` (for operator listing), `credits_amount`, `max_redemptions`, `redemption_count`, `expires_at`, `revoked_at`, `note`, `created_at`.
  - New `credit_voucher_redemptions` table: `id`, `voucher_id`, `user_id`, `redeemed_at`, unique `(voucher_id, user_id)`.
  - Case-insensitive normalized code matching (strip whitespace, uppercase), strict rate limit (`10` attempts / `15 min` per user and IP).
  - Endpoints: `GET /api/v1/credits` (balance + recent ledger entries), `POST /api/v1/credits/redeem` (CSRF-protected).
  - CLI commands in `trip-api`: `create-voucher`, `list-vouchers`, `revoke-voucher`, `grant-credits`.
  - UI: balance, redeem input, and ledger history on `AccountPage`; compact balance, live render credit quote, and inline voucher redeem box in `ExportPanel` while keeping `Local Export` always available.

### 4. Go-Live Hardening & Operational Guardrails (PR 4)

- **Per-User Quotas** (configurable via `.env`):
  - `MAX_PROJECTS_PER_USER = 25`
  - `MAX_STAGES_PER_PROJECT = 30` (alongside existing `MAX_PHOTOS_PER_PROJECT = 100`)
  - `MAX_ACTIVE_RENDERS_PER_USER = 1` (`queued` or `running`)
- **Server Render Parameter Caps** (configurable via `.env`):
  - `SERVER_RENDER_MAX_DURATION_S = 120.0` (Local Export retains `180.0`)
  - `SERVER_RENDER_MAX_FPS_LOCAL = 30`
  - `SERVER_RENDER_MAX_FPS_REMOTE = 60`
- **AWS Lambda Global Concurrency Cap**:
  - `RENDER_REMOTE_MAX_INFLIGHT = 3` in the outbox dispatcher: do not dispatch new `Remote Executions` while `COUNT(render_jobs WHERE render_backend = 'aws_sfn_lambda' AND remote_dispatched_at IS NOT NULL AND status IN ('queued', 'running')) >= RENDER_REMOTE_MAX_INFLIGHT`.
  - Document and set recommended `reserved_concurrent_executions = 24` (`3 × 8`) in `infra/terraform/standalone-remote-render/terraform.tfvars.example`.
- **Runtime Scalability & Ops**:
  - Shared in-process PostgreSQL `LISTEN render_job_events` fan-out hub in FastAPI so multiple open SSE tabs share one async PG connection per API worker instead of opening one connection per tab.
  - Configure `UVICORN_WORKERS` (default `2` in Compose production profile) while ensuring the remote-render outbox loop coordinates safely via `FOR UPDATE SKIP LOCKED` across workers.
  - Enable automatic retention reaping (`reap`) in the default Compose operational cadence and add `scripts/backup-vps.sh` (`pg_dump` + optional offsite S3/R2/B2 sync) documented in `docs/OPERATIONS.md`.

## Delivery Slices

1. **PR 1 (`feat/open-registration-and-legal`)**: This spec, `GLOSSARY.md` updates, open registration without invite code, async SMTP + production fail-closed `SMTP_HOST`, anti-enumeration registration, optional Cloudflare Turnstile + Caddy CSP, `/privacy` and `/terms` pages.
2. **PR 2 (`feat/google-oidc-and-account-delete`)**: Google OIDC sign-in (`user_identities`, nullable `password_hash`, verified-only auto-link, "Set password") and self-service `DELETE /api/v1/auth/me` with `ObjectStore` purge.
3. **PR 3 (`feat/render-credits-and-vouchers`)**: Proportional Render Credit pricing, Credit Ledger, atomic debit and idempotent refunds across `renders.py` and `remote_renders.py`, Credit Vouchers API/CLI, and Account/Export UI.
4. **PR 4 (`chore/go-live-hardening`)**: Per-user quotas, Server Render caps, `RENDER_REMOTE_MAX_INFLIGHT`, shared SSE PG listener hub, multi-worker `uvicorn`, automatic `reap`, and `scripts/backup-vps.sh`.

## Testing Decisions & Seams

All tests operate at the highest stable public seams and verify observable behavior rather than private internals:

1. **Settings Validation Seam (`Settings`)**: Verify fail-closed production invariants (`REGISTRATION_ENABLED=true` requires `SMTP_HOST`; half-configured Turnstile/Google OIDC keys raise `ValueError`; defaults allow open registration).
2. **HTTP Auth & Registration Seam (`TestClient` / `httpx.AsyncClient` against FastAPI routes)**:
   - Registration without invite code, uniform anti-enumeration responses for new/unverified/existing emails, background verification email dispatch, Turnstile verification (valid, invalid, missing, upstream error) via injected `http_client_factory`.
   - Google OIDC callback state/PKCE/nonce validation, new user creation, verified-email auto-linking, unverified-email rejection, and passwordless "Set password" + `DELETE /api/v1/auth/me` cascade + `ObjectStore` deletion.
3. **Credit Pricing & Ledger Seam (`POST /renders`, `POST /renders/{id}/cancel`, `POST /credits/redeem`, `process_remote_renders_once`, `trip-api` CLI)**:
   - Worked-example pricing assertions (e.g. `1920x1080 @ 30fps 60s == 100`, `min 10`, `terrain_3d ×1.5`, `cinematic_stop` extra seconds).
   - Insufficient credits (`402`), idempotent submit, `0`-credit cache hit, automatic idempotent refund on failure, cancel refund before vs after `remote_dispatched_at`, single-use and multi-use voucher limits, and per-user concurrency races.
4. **Frontend Route & Form Seam (Vitest + Testing Library on `LoginPage`, `ForgotPasswordPage`, `PrivacyPage`, `TermsPage`, `AccountPage`, `ExportPanel`)**:
   - Open sign-up without invite code, Turnstile token flow when configured, legal page rendering and navigation links, credit balance/quote display, and voucher redemption.
5. **Quota, SSE & Hardening Seam**: Per-user project/stage/render limits, `RENDER_REMOTE_MAX_INFLIGHT` throttling, shared SSE fan-out behavior, and Compose/Terraform validation.

## Operator Launch Checklist

Non-code operational prerequisites before public announcement:

- Configure transactional SMTP provider and verify domain SPF, DKIM, and DMARC records.
- Configure Cloudflare Turnstile site/secret keys for the production domain (and add `https://challenges.cloudflare.com` to Caddy CSP if enabled).
- Publish the Google Cloud OAuth Consent Screen to production with `/privacy` and `/terms` URLs and authorized redirect URI `https://<domain>/auth/google/callback`.
- Apply to Strava API Support to raise the athlete and daily rate-limit cap above default single-player limits.
- Set `reserved_concurrent_executions = 24` in `infra/terraform/standalone-remote-render` and configure an AWS Budgets email alert.
- Schedule `scripts/backup-vps.sh` via host cron/systemd and verify a test restore.

## Out of Scope

- Payment gateway integration (Stripe/Paddle/tax invoicing) — deferred until post-beta; Credit Vouchers provide the manual mock.
- Web-based Admin Panel UI for voucher or user management — CLI (`trip-api`) is sufficient for beta.
- External APM/error tracking (Sentry/GlitchTip) and automated alerting webhooks (covered separately from the existing private Grafana/Loki/Prometheus stack).
- Architecture Decision Records under `docs/adr/`.

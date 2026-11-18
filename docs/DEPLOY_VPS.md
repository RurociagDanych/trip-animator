# Manual VPS Deployment (GitHub Actions)

Trip Animator v2 includes a manual GitHub Actions workflow ([`.github/workflows/deploy-vps.yml`](../.github/workflows/deploy-vps.yml)) triggered via `workflow_dispatch`. It connects to a VPS over SSH, checks out the requested Git ref, builds Docker images sequentially (with optional BuildKit cache pruning for low-disk VPS hosts such as Mikr.us), starts the Compose stack with `infra/compose.yaml`, and verifies the health endpoint.

## VPS Prerequisites

Before running the workflow for the first time, prepare the target VPS:

1. Install Docker Engine and Docker Compose v2, and grant the deploy SSH user permission to run `docker` (for example, add the user to the `docker` group).
2. Clone this repository to the application directory (default `/opt/trip-animator`).
3. Create `/opt/trip-animator/.env` from `.env.example` with production secrets and domain settings. The workflow refuses to deploy if `.env` is missing.

## SSH Deploy Key Setup

Generate a dedicated Ed25519 key pair for GitHub Actions (without a passphrase) on your workstation or directly on the VPS:

```bash
ssh-keygen -t ed25519 -a 64 -N "" -C "trip-animator-vps-deploy" -f ~/.ssh/trip_animator_deploy
```

1. Append the public key (`~/.ssh/trip_animator_deploy.pub`) to `~/.ssh/authorized_keys` for the deploy user on the VPS:

   ```bash
   chmod 700 ~/.ssh
   cat ~/.ssh/trip_animator_deploy.pub >> ~/.ssh/authorized_keys
   chmod 600 ~/.ssh/authorized_keys
   ```

2. Store the full contents of the private key (`~/.ssh/trip_animator_deploy`) in the repository secret `VPS_SSH_KEY`, then delete any temporary local copy you no longer need.
3. Recommended: collect the VPS SSH host key for strict host key verification (`VPS_SSH_KNOWN_HOSTS`):

   ```bash
   ssh-keyscan -p <ssh-port> -H <vps-host>
   ```

   Store the resulting line(s) in the `VPS_SSH_KNOWN_HOSTS` secret. If omitted, the workflow logs a warning and falls back to `StrictHostKeyChecking=accept-new`.

## GitHub Secrets and Variables

Configure these in **Repository Settings -> Secrets and variables -> Actions**. Connection values (`VPS_HOST`, `VPS_USER`, `VPS_PORT`) can be set as either Secrets or Variables (Secrets take precedence if both are defined).

### Secrets

| Name | Required | Description |
| --- | --- | --- |
| `VPS_SSH_KEY` | Yes | Private Ed25519 SSH key authorized for the deploy user on the VPS. |
| `VPS_SSH_KNOWN_HOSTS` | No | Output of `ssh-keyscan` for the VPS host; enables `StrictHostKeyChecking=yes`. |
| `VPS_HOST` | Yes (or Variable) | VPS hostname or IP address. |
| `VPS_USER` | Yes (or Variable) | SSH username on the VPS. |
| `VPS_PORT` | No (or Variable) | SSH port (defaults to `22`). |

### Variables

| Name | Default | Description |
| --- | --- | --- |
| `VPS_HOST` | — | VPS hostname or IP address (used when `secrets.VPS_HOST` is unset). |
| `VPS_USER` | — | SSH username on the VPS (used when `secrets.VPS_USER` is unset). |
| `VPS_PORT` | `22` | SSH port (used when `secrets.VPS_PORT` is unset). |
| `VPS_APP_DIR` | `/opt/trip-animator` | Absolute path to the repository checkout on the VPS. |
| `VPS_HEALTH_URL` | `http://127.0.0.1:8080/health` | Local health endpoint checked via `curl` on the VPS after startup. |

> Note: Set `VPS_HEALTH_URL` to match `CADDY_HTTP_PUBLISH` in `.env` (for example `http://127.0.0.1:8080/health` behind host Nginx, or `http://127.0.0.1/health` when Caddy binds port `80` directly).

## Workflow Inputs

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `ref` | string | `main` | Git branch, tag, or commit SHA to deploy (`[A-Za-z0-9._/-]+`). |
| `build_worker` | boolean | `true` | Rebuild the Playwright MP4 render worker image (`worker`). Set `false` to skip rebuilding it on quick API/Web deploys. |
| `prune_builder_cache` | boolean | `true` | Run `docker builder prune -f` before and between sequential image builds to reduce peak VPS disk usage. |
| `with_airflow` | boolean | `false` | Build and start the optional `--profile airflow` services. |

## Triggering a Deployment

### GitHub Actions UI

1. Open the repository on GitHub and go to **Actions -> Deploy VPS**.
2. Click **Run workflow**.
3. Choose the target `ref` and toggle `build_worker`, `prune_builder_cache`, and `with_airflow` as needed, then click **Run workflow**.

### GitHub CLI (`gh`)

Deploy `main` with default options:

```bash
gh workflow run deploy-vps.yml
```

Fast API and Web deploy of a specific release tag without rebuilding the Playwright worker:

```bash
gh workflow run deploy-vps.yml \
  -f ref=v1.0.0 \
  -f build_worker=false \
  -f prune_builder_cache=true \
  -f with_airflow=false
```

Watch the latest run:

```bash
gh run watch
```

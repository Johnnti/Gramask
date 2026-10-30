# Deployment

For DigitalOcean, use the complete [Droplet instructions](DIGITALOCEAN.md) and independent `compose.production.yaml`. The production stack includes Caddy, HTTPS, persistent volumes, private Ollama, and an app that trusts only the proxy's fixed private address. The generic configurations below remain available for other hosts.

## Supported topology

Run one app process with one Uvicorn worker and persistent SQLite storage. Gemma 3 runs in Ollama on the same network or at a separate endpoint. Keep the Ollama port private and put the app behind an HTTPS reverse proxy in production.

The image runs as UID/GID 10001. A bind-mounted data directory must be writable by that user; the Compose named volume inherits the image's directory ownership. Database migrations run at application startup, never at image-build time.

## Compose on a server

1. Install Docker and Compose v2.
2. Copy `.env.example` to `.env`. Set `APP_ENV=production`, `APP_ORIGIN` to the exact HTTPS origin, and `SECURE_COOKIES=1`. Keep demo and fallback disabled.
3. Configure your HTTPS reverse proxy to forward to `127.0.0.1:8000`. Compose binds to localhost by default. The configured origin must match the browser origin, including any nonstandard port.
4. Run `docker compose up --build -d`; inspect `docker compose logs -f pull-model app`. The initial model download can take several minutes.
5. Check `/readyz`, register an account, ask a known fact, and complete a real three-contributor crowd question.

The model-pull job finishes before the app starts using Docker's [dependency conditions](https://docs.docker.com/compose/how-tos/startup-order/). Storage follows [Ollama's Docker layout](https://docs.ollama.com/docker). The configuration supports CPU inference; follow the Ollama instructions for your host's GPU setup. The default model is documented in the [Gemma 3 library](https://ollama.com/library/gemma3).

The container disables forwarded proxy headers. Authentication limits may therefore group users behind a reverse proxy by its address; choose limits appropriate for your audience. If enabling Uvicorn proxy headers, trust only your actual proxy with `--forwarded-allow-ips`. Never trust arbitrary forwarded headers on a publicly reachable port.

## Render

`render.yaml` defines one native Python web service with a 1 GB persistent disk at `/var/data`. The native runtime uses Render's service account and avoids relying on a Docker image user's ownership of a mounted disk. Render disks require a paid service and support only one instance. The configuration follows the [disk documentation](https://render.com/docs/disks) and [Blueprint reference](https://render.com/docs/blueprint-spec). `.python-version` selects the latest Python 3.12 patch release using Render's [documented version selection](https://render.com/docs/python-version).

1. Push the repository to your Git host and create a Render Blueprint from `render.yaml`.
2. Enter the prompted `APP_ORIGIN` as the final HTTPS site origin, such as the assigned `https://...onrender.com` address or custom domain.
3. Enter `LLM_BASE_URL` for a reachable OpenAI-compatible endpoint, including `/v1`, its Gemma 3 `LLM_MODEL` identifier, and API key. The blueprint does not run Ollama inside the app's small web instance.
4. Retain the production, secure-cookie, and demo-disabled settings. Render supplies `PORT`, which the startup command reads. The build installs `requirements.txt`; database initialization happens when the service starts and its disk is mounted.
5. Wait for `/readyz` to pass. An unavailable model, invalid key, or unreachable provider prevents readiness. Inspect logs and correct the configuration if needed.

For a separate private Ollama host, change `LLM_PROVIDER` to `ollama`, use its native API base URL without `/v1`, and select `gemma3:4b`. Download the model on that host first. Use a trusted private network or authenticated TLS gateway.

This native blueprint does not use the Dockerfile. For a separately configured Docker deployment, provision data-directory permissions for UID/GID 10001 before mounting the disk; image-build ownership does not guarantee ownership on an existing disk.

## Health and operations

- `/healthz`: process and database availability.
- `/readyz`: database and configured Gemma 3 endpoint/model availability.
- `/api/config`: non-secret engine status and workflow settings.

Readiness calls `/api/tags` for Ollama or `/models` for the compatible API and requires an exact model identifier match. Your provider must support that listing endpoint as well as inference. Availability checks do not prove inference quality or latency. Run a real question after model/provider changes. Gemma uses [structured outputs](https://docs.ollama.com/capabilities/structured-outputs), with application validation of fact identifiers and supporting reports. Monitor readiness failures, model errors, latency, disk use, and unresolved questions.

The background worker sweeps expired questions and resumes from persisted records after restart. Browser notifications require permission and an open app; there is no background push service.

## Backups

Use SQLite's backup API for a consistent copy while running. Set `DB_PATH` to the active persistent database and provision a protected backup destination:

```bash
python - <<'PY'
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

backup_dir = Path('/var/backups/gramask')
backup_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
destination = backup_dir / f'gramask-{stamp}.db'
with sqlite3.connect(os.environ['DB_PATH']) as source:
    with sqlite3.connect(destination) as target:
        source.backup(target)
print(destination)
PY
```

For Docker, execute inside the app container with that backup directory mounted and writable. Copy backups to protected off-host storage and test recovery. Backups contain account and question data. To restore, stop the app, preserve the current database and WAL/SHM sidecars together, restore the verified backup as `DB_PATH`, check ownership, and restart. Copying only an active WAL-mode `.db` can omit recent transactions.

## Release checks

Run `python -m unittest discover -s tests -v`, validate with `docker compose config --quiet`, build the image, and exercise readiness and the real student workflow on the target host. Offline tests do not verify production inference, hardware capacity, TLS, provider credentials, or platform disk permissions.

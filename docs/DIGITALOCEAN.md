# Deploy GramAsk on a DigitalOcean Droplet

This release uses a dedicated Docker Compose project, `gramask-production`. It runs the real Gemma 3 model, the app, and Caddy for HTTPS. It does not reuse the local demo's container, image tag, database, or ports. Run the deployment commands on the Droplet, not on the presentation computer.

Use a **Droplet**, since this version stores accounts and questions in SQLite. DigitalOcean App Platform has an ephemeral filesystem and does not support persistent volumes; deploying this database there would lose data on replacement. See [App Platform storage limits](https://docs.digitalocean.com/products/app-platform/details/limits/).

## 1. Prepare the server and domain

1. Create an Ubuntu Droplet with Docker and Compose v2, using DigitalOcean's [Docker image](https://docs.digitalocean.com/products/marketplace/catalog/docker/) if convenient. A starting estimate for the app plus `gemma3:4b` is **4 vCPUs, 16 GB RAM, and at least 40 GB free disk**. This is a capacity estimate, not a load benchmark: CPU inference is slow, and production traffic may need faster model hardware. Start with one app instance and one model request at a time.
2. Add your SSH public key and verify SSH access. Follow DigitalOcean's [recommended Droplet setup](https://docs.digitalocean.com/products/droplets/getting-started/recommended-droplet-setup/) for the account, monitoring, and server backups. Creating a Droplet incurs DigitalOcean charges; this package does not create one automatically.
3. Create a Cloud Firewall: allow TCP **22 from your administrator IP**, and TCP **80 and 443 from the public Internet**. Allow outbound traffic for DNS, model/image downloads, and certificates. Do not expose ports 8000 or 11434. [Firewall instructions](https://docs.digitalocean.com/products/networking/firewalls/how-to/configure-rules/).
4. Point your domain's A record to the Droplet's public IPv4 address. Only add an AAAA record when the server's IPv6 path works. Caddy obtains and renews certificates once DNS and ports 80/443 are reachable. [Caddy HTTPS requirements](https://caddyserver.com/docs/automatic-https).

The app accepts traffic only through Caddy. Uvicorn trusts forwarding headers from Caddy's fixed private IP, so users are rate-limited individually. Both `PROXY_SUBNET` and `PROXY_IP` can be changed together if the default network overlaps a VPC or an existing Docker network. Do not replace the trusted address with `*` or publish the app port.

## 2. Upload this release

The supplied `artifacts/gramask-digitalocean.tar.gz` contains application code, deployment files, tests, and instructions. It excludes local accounts, demo databases, credentials, virtual environments, and screenshots.

From your computer, replacing the SSH user and IP:

```bash
scp artifacts/gramask-digitalocean.tar.gz YOUR_SSH_USER@YOUR_DROPLET_IP:/tmp/
ssh YOUR_SSH_USER@YOUR_DROPLET_IP
```

On the Droplet:

```bash
tar -xzf /tmp/gramask-digitalocean.tar.gz -C "$HOME"
cd "$HOME/gramask-production"
cp deploy/production.env.example .env.production
chmod 600 .env.production
nano .env.production
```

Set `DOMAIN` to your actual hostname, without `https://` or a path, and `ACME_EMAIL` to your certificate contact email. Keep `LLM_MODEL=gemma3:4b`. No model API key is needed for the included private Ollama service.

`QUESTION_ROUTING=knowledge_first` is the default: Gemma checks the SQLite knowledge index and returns a grounded answer when available. `LIVE_FRESHNESS_MIN=5` expires changing information after five minutes; stable curated facts remain reusable. Missing or stale answers go to students. `QUESTION_ROUTING=community` is an optional override for presentations that require every question to open a fresh crowd request. In both modes, unfamiliar topics reach students instead of being rejected. Existing `.env.production` files must be updated to `QUESTION_ROUTING=knowledge_first`; changing a Compose default does not override an explicit old value.

Production forces demo mode, simulated accounts, and heuristic model fallback off. Students create real accounts; there is no automatic three-account demonstration helper on the public site.

## 3. Start and verify

```bash
bash scripts/production.sh check
bash scripts/production.sh deploy
bash scripts/production.sh status
```

The first deploy pulls the model before starting the app; it can take several minutes. If status is not ready, use `bash scripts/production.sh logs` to inspect the download and startup. Caddy creates the HTTPS certificate once the application starts and the domain reaches this server.

Open `https://YOUR_DOMAIN`, then verify:

```bash
curl --fail https://YOUR_DOMAIN/healthz
curl --fail https://YOUR_DOMAIN/readyz
bash scripts/production.sh smoke
```

The smoke command exercises real Gemma with synthetic accounts in a **temporary database**. It does not insert test users into the public site's database. It verifies routing, three reports, text agreement, notifications, and repeat-question behavior. It may take several minutes on CPU.

Finally, use four real test accounts in separate browser profiles/devices: ask a question, answer from the three other accounts, and confirm the result appears for the asker. Text answers are saved immediately; the background worker then asks Gemma to compare them. A pending question is not an AI-generated answer. Confirm your HTTPS URL works from another device before inviting users.

## Storage, backups, and updates

The project's named volumes persist the database, model weights, and TLS state. Keep one app replica and one worker. Do not run `docker compose down -v`, remove those volumes, or attach the presentation demo's volume to production.

Create a consistent database backup while the app is running:

```bash
bash scripts/production.sh backup
```

This uses SQLite's backup API, verifies the copy, and saves it with owner-only permissions in `backups/`. Copy backups to protected off-server storage and verify recovery on a separate database. Droplet backups complement application backups; a copy on the same Droplet alone does not protect against losing that Droplet. Backups contain password hashes, session records, and private questions.

For restoration: stop the production app, keep the current database **and its `-wal` / `-shm` files together** in a separate recovery directory, place a verified backup at `/app/data/gramask.db` with UID/GID 10001 ownership, then start the app. Restore only while the app is stopped; never overwrite a live database or leave its previous WAL beside the restored copy.

Before an update, take a backup and retain the previous release directory and image tag. Upload the new source without overwriting `.env.production`, change `RELEASE_TAG`, and run `deploy` again. The production project/volume names remain stable. To roll back code, restore the previous source and tag, and redeploy; database rollback may also be needed for an incompatible future migration. No destructive reset is part of the deployment script.

## Operational limits

Accounts verify a password, not university enrollment. SSO, password recovery, moderation tools, and closed-browser Web Push are not included. SQLite and the in-process rate limiter support a single app process. Review these limits before a larger public rollout. Never open the unauthenticated Ollama port to the Internet.

The release was tested locally in an isolated production image. Droplet SSH access, DNS, public TLS issuance, server capacity, and public reachability still need verification on your actual DigitalOcean account.

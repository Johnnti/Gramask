# GramAsk

A campus Q&A web app for Grambling State University. **Gemma 3 is the main reasoning engine**: it routes questions, selects trusted campus facts, prepares crowd questions, and groups free-text student reports. The server enforces evidence, quorum, expiry, and account permissions before publishing answers.

The app includes password-protected accounts, a responsive dashboard, personal question history, a community inbox, contributions, source-linked campus knowledge, expiring live answers, and in-app notifications. Students can opt into browser notifications while the app is open. It is an independent project, not an official university service.

## Start with Docker and Gemma 3

Requires Docker Engine or Docker Desktop with Docker Compose v2 and enough memory/storage for `gemma3:4b`. The first start downloads the model; CPU inference is supported but can be slow. On Apple silicon, native Ollama usually performs better than inference inside Docker.

```bash
cp .env.example .env
docker compose up --build -d
docker compose logs -f pull-model app
```

Open [http://localhost:8000](http://localhost:8000). Compose waits for Ollama and the model download before starting the app. Readiness also checks the configured model. Accounts and answers persist in `gramask-data`; model weights persist in `ollama-models`. The Ollama API is private to the Compose network. Stop with `docker compose down`; adding `-v` deletes those volumes.

This runs the real Gemma 3 engine. Demo mode and silent model fallbacks are disabled.

## Run locally with native Ollama

Use Python 3.12 or newer and install [Ollama](https://ollama.com/download). Start the Ollama app, or run `ollama serve` in a separate terminal, then:

```bash
ollama pull gemma3:4b
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
set -a
source .env
set +a
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Python does not automatically read `.env`; the export commands above load it. Docker Compose reads `.env` itself. Use `--reload` only during development. The default endpoint is Ollama's native API at `http://127.0.0.1:11434` with `gemma3:4b`.

For a remote Gemma 3 deployment with an OpenAI-compatible API, set `LLM_PROVIDER=openai`, a base URL including the provider's `/v1` path, its exact Gemma 3 model identifier, and `LLM_API_KEY`. This selects an API format; the model remains Gemma 3. The model must be available on your provider account.

## Try the complete student flow

1. Create an account and ask “When was Grambling State University founded?” to see a sourced knowledge answer.
2. Ask “Is the library open right now?” It appears in other accounts' community inboxes.
3. Sign in as three different contributors using separate browser profiles or devices. Submit their observations.
4. Once at least three eligible reports meet the agreement threshold, the asker receives an answer with an expiry time. Other students asking the identical question receive the same fresh evidence.

The asker and other subscribers cannot vote on their own question. Each account gets one report per question. “Not sure” does not count toward quorum. Questions without sufficient agreement close without a claimed answer. Live information is attributed to student reports, not university verification.

## Explicit one-device demo

For an offline demonstration only, use an isolated database:

```bash
DEMO_MODE=1 APP_ENV=development DB_PATH=/tmp/gramask-demo.db \
  python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Create an account and use the guided demo controls. This mode uses deterministic classification and simulated voters, is visibly labeled, and scopes simulated records to your account. Reset removes only that account's demo data. Demo mode is off by default and rejected in production configuration.

To answer manually from three accounts in the **same browser**, click **Set up 3 student accounts**, then open **Student 1**, **Student 2**, and **Student 3**. Each link opens its own signed-in session without replacing the asker. Type a question in the asker's tab or use **1. Create question**, then answer from each student's Community inbox. After quorum and agreement are met, the answer and notification appear in the asker's original tab. Students cannot read the asker's private history or vote on another demo owner's questions. **Reconnect student accounts** restores their sessions without creating duplicate voters. The automatic simulation remains optional.

## Deploy

For a presentation where **every typed question must reach every other student**, set `DEMO_MODE=1 DEMO_BROADCAST_ALL=1`. This bypasses topic rejection, instant knowledge answers, cached answers, and shared-question reuse in the demo. Each submission opens a fresh crowd request; all other student accounts can respond, including accounts created by another demo asker. The requester cannot vote. In this broadcast demo, all responses count, including “Not sure.” Questions close after three students respond or when their deadline expires, and the asker receives the count and percentage for each response. Written answers are grouped by matching text; uncertain answers are grouped as “Not sure.” At the deadline, partial responses are returned; if nobody responded, the result says “No answers received.” Set `TIMEOUT_MIN=1` for a one-minute window. These rules are enabled in the local submission demo on port 8009 and have no effect outside demo mode.

**DigitalOcean:** use the separate [Droplet deployment guide](docs/DIGITALOCEAN.md), `compose.production.yaml`, and `bash scripts/production.sh deploy`. This setup includes automatic HTTPS, persistent storage, private Ollama, trusted proxy handling, and verified database backups. Its own image tags and volumes keep the local submission demo independent. The production default in this stack sends every question to students using real Gemma 3; unfamiliar topics are not rejected.

The repository includes a non-root `Dockerfile`, an app + Ollama `compose.yaml`, and a disk-backed `render.yaml` for hosting against an external Gemma 3 endpoint. See [deployment instructions](docs/DEPLOYMENT.md) for HTTPS, Render, health checks, and backups.

For production, set `APP_ENV=production`, `APP_ORIGIN=https://your-domain.example`, `SECURE_COOKIES=1`, and keep `DEMO_MODE=0`. Run **one application instance and one Uvicorn worker** with persistent SQLite storage behind HTTPS. Keep credentials out of the repository.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | Native `ollama` or `openai` compatible API |
| `LLM_BASE_URL` | `http://127.0.0.1:11434` | Server-side model endpoint |
| `LLM_MODEL` | `gemma3:4b` | Gemma 3 model identifier |
| `LLM_API_KEY` | empty | Optional provider credential |
| `LLM_TIMEOUT` | `45` | Timeout per inference call; example config uses 120 for CPU. Static questions use two calls. |
| `LLM_MAX_CONCURRENT` | `2` | Concurrent model request limit |
| `ALLOW_FALLBACK` | `0` | Explicit degraded heuristic mode; leave disabled in production |
| `DB_PATH` | `gramask.db` beside app | SQLite location |
| `QUORUM` | `3` | Minimum eligible reports |
| `AGREEMENT` | `0.6` | Minimum fraction supporting one answer |
| `TIMEOUT_MIN` | `15` | Crowd response window |
| `FACT_TTL_HOURS` | `2` | Fact lifetime; menus capped at three hours |
| `DEMO_MODE` | `0` | Explicit, labeled offline demo |
| `QUESTION_ROUTING` | `knowledge_first` | `community` sends each submission to students; `knowledge_first` returns trusted facts/fresh evidence first. Both production modes route unfamiliar topics to students. DigitalOcean defaults to `community`. |
| `APP_ENV` | `development` | `development`, `test`, or `production` |
| `APP_ORIGIN` | empty | Exact browser origin; required HTTPS origin in production |
| `SECURE_COOKIES` | true in production | HTTPS-only account cookies |
| `SESSION_DAYS` | `14` | Login lifetime |
| `ASK_RATE_LIMIT` | `20` | Questions per account per minute |
| `AUTH_RATE_LIMIT` | `15` | Authentication requests per client IP per minute |

Configuration is validated at startup. `.env.example` supplies a local origin and a longer CPU inference timeout. Model errors are surfaced; normal mode does not invent successful answers when Gemma is unavailable.

## Tests and health

```bash
python -m unittest discover -s tests -v
curl --fail http://localhost:8000/healthz
curl --fail http://localhost:8000/readyz
```

Tests use temporary databases and explicit offline fixtures. They do not modify `gramask.db` or prove live model quality. `/healthz` checks the process/database; `/readyz` additionally checks that the configured model is listed by the provider's `/api/tags` (Ollama) or `/models` (compatible API). Your endpoint must support the relevant listing API. Exercise the real Gemma flow after configuring your deployment endpoint.

Run `python scripts/smoke_live.py --timeout 120` against a configured model for an isolated check of sourced answers, scope, current conditions, text consensus, notifications, and fresh-answer reuse. See [live verification](docs/LIVE_VERIFICATION.md) for Docker commands and the bounded coverage, and [verification results](docs/VERIFICATION.md) for checks completed on this build.

## Boundaries and code map

Accounts authenticate a username/password, not campus enrollment. University SSO, email verification, password recovery, moderation administration, and Web Push while the app is closed are not implemented. Duplicate prevention is account-based, not proof of different humans. SQLite and in-process rate limits target one instance; migrate persistence and coordination before scaling horizontally.

- `app.py`, `database.py`, `security.py` — authenticated API, persistence, crowd workflow, expiry, notifications
- `config.py` — validated settings
- `llm.py` — Gemma 3 integration, structured-output validation, fact grounding
- `index.html`, `static/` — responsive dashboard and app shell
- `seed_facts.json` — curated facts with source links
- `tests/` — offline API and model-boundary regression tests
- [Architecture](docs/ARCHITECTURE.md), [deployment](docs/DEPLOYMENT.md), [original MVP README](docs/MVP_README.md)

The original anonymous MVP database is preserved in legacy tables during migration. Legacy display names do not grant access to new accounts; users must register.

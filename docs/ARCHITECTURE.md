# Architecture

FastAPI serves a same-origin browser app. SQLite stores accounts, sessions, personal queries, shared crowd questions, reports, expiring facts, and notifications. A background worker resolves expired questions. One process is the supported deployment topology.

## Question lifecycle

1. Authenticate the account, verify the mutation's CSRF token and origin, validate input, and check rate limits.
2. Gemma first classifies only the question, without reference facts. With `QUESTION_ROUTING=knowledge_first`, search the existing SQLite FTS5 index for curated facts and fresh community evidence. Filter private and expired records before sending candidates to Gemma; current-condition questions cannot retrieve stable descriptions. A short structured selection call chooses a supplied fact ID or null; the server returns exact source text and attribution. Related keywords alone never authorize a knowledge answer.
3. A miss opens a shared or new community request using the original question and its classification. No topic is rejected based on the model's scope flag. With explicit `community` routing, skip retrieval and open a fresh request for every submission.
4. Other accounts receive it in their inbox. Askers/subscribers cannot vote. Each contributor has one report. “Not sure” is stored but excluded from quorum and agreement.
5. Count yes/no reports deterministically. Production free-text submissions return after persistence; the durable background sweep asks Gemma 3 to group reports with validated supporting indices. Recompute agreement and enforce both quorum and the agreement threshold. The sweep resumes pending work after restart.
6. Sufficient evidence creates a temporary fact and completes every subscribed query with a notification. A timeout without sufficient evidence produces an unanswered result. Changing facts expire five minutes after the oldest contributing observation. Both retrieval and publication enforce this cap, including old rows with longer TTLs. Reads do not renew freshness. Stable curated facts have no automatic five-minute expiry. Uncertain or tied demo summaries are not reusable knowledge.

Question identity includes wording/polarity: an answer to “Is the library open?” cannot automatically answer “Is the library closed?” The app favors a new question over unsafe reuse.

## Trust boundaries

Model output is untrusted data. The model cannot modify storage, select account identity, reduce quorum, extend fact lifetime, or invent verified sources. Normal operation surfaces model failures. Offline demo mode uses visibly labeled deterministic fixtures. `DEMO_LIVE_LLM=1` enables real Gemma inference while retaining the demo controls.

Ollama requests explicitly allocate an 8,192-token context. A conservative UTF-8 byte limit reserves room for the chat template and output. Oversized requests fail closed; reports are never silently dropped to fit, since that could distort agreement. Large sets of long reports can therefore remain unresolved. This is a bounded single-campus deployment, not a high-volume aggregation service.

Sessions use HttpOnly, SameSite cookies; production requires Secure cookies and an HTTPS origin. The server controls identity and private-record access. Simulated demo records and voters stay scoped to their creator; reset cannot delete real records.

Independent student tabs select distinct HttpOnly session cookies with a validated `X-GramAsk-Session` namespace. The URL contains only the namespace, never a login token; possession of a link alone does not authenticate an account. Browser auth synchronization is scoped to the same namespace. In demo mode, an authenticated asker can prepare three separate student accounts and open their inboxes. Those students can answer only that asker's questions, including the isolated sample question, and cannot read the asker's private history. The helper and these student sessions are disabled outside demo mode.

The explicit `DEMO_BROADCAST_ALL` presentation option allows demo students to answer other askers' public questions too. It remains demo-only and also enables all-response summaries at quorum/deadline. It does not bypass knowledge-first routing; only `QUESTION_ROUTING=community` does that. The running local demo is an immutable container independent of source edits and of the DigitalOcean production project.

The service worker caches app-shell assets only. Authenticated API responses are not cached for offline access. Notifications are fetched while the app is open; closing the browser stops delivery.

## Persistence and evolution

Migration retains anonymous MVP data in legacy tables. New accounts do not inherit an old display name's history. Curated facts live in `seed_facts.json` with official source links; review them as part of releases. The FTS5 index is rebuilt at startup for seed edits and legacy databases, then kept current by fact insert/update/delete triggers. It introduces no external database service.

Accounts establish password identity only. Campus email verification, institutional SSO, password recovery, moderation tools, reputation, closed-app push, and multi-instance infrastructure are future product work. Horizontal scaling requires shared persistence, rate limits, expiry coordination, and background jobs.

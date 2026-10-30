# Architecture

FastAPI serves a same-origin browser app. SQLite stores accounts, sessions, personal queries, shared crowd questions, reports, expiring facts, and notifications. A background worker resolves expired questions. One process is the supported deployment topology.

## Question lifecycle

1. Authenticate the account, verify the mutation's CSRF token and origin, validate input, and check rate limits.
2. Gemma 3 first classifies only the question's scope, time sensitivity, subject, and response format. For an in-scope static question, a separate Gemma call selects a supplied fact ID. This keeps reference facts from influencing intent classification. The app preserves the original crowd question verbatim; generated campus claims are not accepted as knowledge.
3. With `QUESTION_ROUTING=knowledge_first`, return a trusted knowledge fact or fresh matching crowd evidence, otherwise subscribe to an existing matching question or create one. With `community`, skip knowledge selection and evidence reuse and create a fresh crowd request for every submission. In production, unfamiliar topics reach contributors regardless of the model's scope classification; scope is not an access-control decision.
4. Other accounts receive it in their inbox. Askers/subscribers cannot vote. Each contributor has one report. “Not sure” is stored but excluded from quorum and agreement.
5. Count yes/no reports deterministically. Production free-text submissions return after persistence; the durable background sweep asks Gemma 3 to group reports with validated supporting indices. Recompute agreement and enforce both quorum and the agreement threshold. The sweep resumes pending work after restart.
6. Sufficient evidence creates a temporary fact and completes every subscribed query with a notification. A timeout without sufficient evidence produces an unanswered result. Expired facts cannot answer new questions.

Question identity includes wording/polarity: an answer to “Is the library open?” cannot automatically answer “Is the library closed?” The app favors a new question over unsafe reuse.

## Trust boundaries

Model output is untrusted data. The model cannot modify storage, select account identity, reduce quorum, extend fact lifetime, or invent verified sources. Normal operation surfaces model failures. Explicit demo mode uses visibly labeled deterministic fixtures.

Ollama requests explicitly allocate an 8,192-token context. A conservative UTF-8 byte limit reserves room for the chat template and output. Oversized requests fail closed; reports are never silently dropped to fit, since that could distort agreement. Large sets of long reports can therefore remain unresolved. This is a bounded single-campus deployment, not a high-volume aggregation service.

Sessions use HttpOnly, SameSite cookies; production requires Secure cookies and an HTTPS origin. The server controls identity and private-record access. Simulated demo records and voters stay scoped to their creator; reset cannot delete real records.

Independent student tabs select distinct HttpOnly session cookies with a validated `X-GramAsk-Session` namespace. The URL contains only the namespace, never a login token; possession of a link alone does not authenticate an account. Browser auth synchronization is scoped to the same namespace. In demo mode, an authenticated asker can prepare three separate student accounts and open their inboxes. Those students can answer only that asker's questions, including the isolated sample question, and cannot read the asker's private history. The helper and these student sessions are disabled outside demo mode.

The explicit `DEMO_BROADCAST_ALL` presentation option allows demo students to answer other askers' public questions too. It remains demo-only. The running local demo is an immutable container independent of source edits and of the DigitalOcean production project.

The service worker caches app-shell assets only. Authenticated API responses are not cached for offline access. Notifications are fetched while the app is open; closing the browser stops delivery.

## Persistence and evolution

Migration retains anonymous MVP data in legacy tables. New accounts do not inherit an old display name's history. Curated facts live in `seed_facts.json` with official source links; review their freshness as part of releases.

Accounts establish password identity only. Campus email verification, institutional SSO, password recovery, moderation tools, reputation, closed-app push, and multi-instance infrastructure are future product work. Horizontal scaling requires shared persistence, rate limits, expiry coordination, and background jobs.

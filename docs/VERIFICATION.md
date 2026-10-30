# Verification — 2026-10-30

## DigitalOcean production release

- 55 tests passed on the host and inside the separate `gramask-production:2026-10-30` image. Production-specific coverage checks HTTPS session cookies, private histories, host/origin rejection, disabled demo routes, unknown-topic routing, background text consensus, trusted-proxy rate limits, and a verified SQLite WAL backup with owner-only permissions.
- `compose.production.yaml` passed Compose configuration validation. Caddy 2.11.7 validated the final configuration without errors or warnings. Only Caddy publishes ports; the app and Gemma have no host port mappings. The production image runs independently of the demo image/tag.
- Six real Gemma 3 checks passed with `QUESTION_ROUTING=community`, demo/fallback disabled, a temporary database, and no published ports: inventory/accounts (0.30 s), founding-date routing (32.81 s), library-name routing (7.50 s), unfamiliar-topic routing (6.39 s), preserved-negation route (7.15 s), and three-student text agreement/notification/fresh-repeat behavior (23.80 s).
- The DigitalOcean guide, dedicated Compose stack, HTTPS proxy configuration, deployment/status/smoke/backup helper, and source-only release archive are included. No DigitalOcean resources have been created and no public site has been deployed: SSH target, domain/DNS, public TLS issuance, and target-server performance remain to be verified.
- The running demo container was not restarted, rebuilt, reconfigured, or connected to the production tests; its database/volume was never mounted by the tests. Source changes do not affect its existing image.

The earlier checks below record the local-demo and initial implementation milestones.

## Passed

- 48 automated tests across API integration, backend safety, model validation, configuration, and the complete Gemma-adapter/API workflow. Tests use temporary databases and controlled model transports, not live inference.
- Password account creation/login/logout, session cookies, CSRF/origin enforcement, private query access, and cross-account demo isolation.
- Sourced static facts, pending crowd requests, distinct-account voting, agreement thresholds, uncertainty exclusions, question polarity isolation, expiry, cached answers, and notifications for all subscribers.
- Concurrent finalization creates one fact and one completion notification. Model calls do not hold SQLite write transactions. Failed text consensus is retried. Legacy records survive an idempotent migration.
- Ollama and hosted OpenAI-compatible request serialization, Gemma 3 defaults, strict output validation, evidence index validation, Unicode/sign-preserving fallback comparison, and explicit model failure behavior.
- JavaScript and service worker syntax, static asset references, dependency consistency (`pip check`), and Docker Compose configuration validation.
- Docker image built successfully with Python 3.12. All 47 automated tests also passed inside the built image, with networking disabled and temporary test databases.
- Regression coverage for the previously rejected “Is JTS open right now?” question: all three independent student inboxes receive it, their reports resolve it, and the original asker receives the completion notification. JTS spelling variants remain unanswered until student evidence arrives.

## Real Gemma 3 checks

Ollama `0.40.13` and `gemma3:4b` ran inside Docker. `scripts/smoke_live.py` passed all six cases using the real model, the application API, and a temporary database. Demo mode and fallback were disabled. These API checks use FastAPI's test client; the deployed HTTP service was separately checked for readiness and public-page rendering.

| Case | Result | Elapsed |
|---|---|---|
| Model availability and account creation | Passed | 0.29 s |
| Exact sourced founding-date answer | Passed | 34.91 s |
| Exact sourced library-name answer | Passed | 24.93 s |
| Unrelated question rejected | Passed | 9.46 s |
| Current library conditions remain pending, with original negation and yes/no input | Passed | 10.14 s |
| Two agreeing text reports plus one conflict: 2/3 agreement, notification, and cached repeat | Passed | 34.12 s |

The first live runs exposed classification errors when reference facts and intent shared one prompt. The final implementation separates intent classification from fact selection. Explicit report boundaries and comparison rules fixed over-inclusive text agreement. The same behavioral assertions then passed without relaxing expectations. Oversized native-model inputs now fail closed instead of silently truncating evidence.

These are bounded functional checks, not a general accuracy or load benchmark. The model can still misclassify unfamiliar wording. Static questions make two inference calls; the browser timeout accommodates both.

The real app was started at `http://localhost:8008` with persistent Docker volumes. A separate requested submission demo runs at `http://localhost:8009`, visibly labeled as simulation and using its own database. Both remain running; no public deployment was made.

## Browser checks

The all-questions presentation patch passed 48 automated tests, including arbitrary topics, known knowledge questions, fresh repeats, voting by another asker's student accounts, and return to the requester. The running demo was rebuilt with `DEMO_BROADCAST_ALL=1`. The previously blocked yard question retained its history ID and appeared with text-answer controls in all three student inboxes; screenshot: `artifacts/all-questions-demo.jpg`. The earlier 47-test image validation below predates this presentation-only patch.

After the JTS routing correction, the existing rejected question was restored to pending without changing its original history ID. All three signed-in student tabs visibly showed “Is JTS open right now?” with Yes, No, and Not sure controls. The asker showed “Asking the community.” The new student inbox link was verified from both Live answers and Ask views. The question was left unanswered for the user to demonstrate; evidence is `artifacts/jts-inbox-fixed.jpg`.

The multi-account update was also exercised against the running Docker demo: one asker and three independently signed-in student tabs shared the same browser. Each student manually submitted Yes to an isolated sample question. The original asker automatically displayed three reports, 100% agreement, and an answer-ready notification without losing its login. No browser errors were observed. Automated checks also cover typed questions, one cookie jar with four identities, per-session CSRF, independent logout, reconnect without duplicate voters, demo-owner isolation, and rejection of demo student sessions outside demo mode. The screenshot is `artifacts/multi-account-demo.jpg`.

The finished app was served on localhost with `DEMO_MODE=1` and a separate temporary database. The following were exercised through the actual interface:

- Registration and returning-user login; a sourced university-history answer.
- Guided simulation from pending question to three reports, consensus, expiry metadata, and an answer-ready notification; marking notifications read.
- A second account's community inbox, a Yes submission, and contribution history.
- Logout and account changes propagating across two tabs without retaining the earlier account's private question history.
- Knowledge search, navigation, and live fact presentation.
- Layout at 1440, 390, and 320 CSS pixels; no horizontal page overflow, and closed mobile navigation is inert.
- No JavaScript errors or warnings observed in the checked browser tabs.

The captured desktop preview is in `artifacts/gramask-desktop.jpg`. It shows explicitly labeled demonstration data. The temporary preview server and test tabs were stopped after verification.

## Not verified here

- Hosted deployment, production TLS, disk permissions, and real multi-device browser alerts were not exercised. No site was published.
- Hosted OpenAI-compatible Gemma endpoints were tested only with controlled transports, not live credentials. Re-run live checks against your chosen production endpoint and hardware.

Follow `README.md` and `docs/DEPLOYMENT.md` to start the app, provision Gemma 3, and run the deployment smoke checks. Accounts do not verify campus enrollment, and browser alerts require an open app.

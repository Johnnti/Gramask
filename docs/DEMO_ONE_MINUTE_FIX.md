# Local demo: all responses and one-minute deadline

Applied 2026-10-30 to `gramask-submission-demo` on port 8009 using image `gramask-demo:one-minute`, with `DEMO_MODE=1`, `DEMO_BROADCAST_ALL=1`, and `TIMEOUT_MIN=1`.

- Every response counts toward the three-student completion threshold, including No and Not sure.
- Mixed answers return a response summary rather than waiting for consensus.
- At 60 seconds the background worker closes the question with available responses or “No answers received.” This also works without an open browser.
- Written responses follow the same rule. Matching text is grouped; uncertain responses are grouped as Not sure. Percentages use the number of responses received.
- Both asker history and live answers show the distribution. Pending questions show a seconds countdown; expired inputs are disabled.
- Existing open questions receive the shorter deadline at startup. Previously unanswered demo questions were recomputed from their original saved responses. No account or answer records were removed.

Verification: all 58 regression tests passed locally and in the exact Docker image, including production behavior checks. An isolated real-clock test confirmed both choice and text questions with zero reports closed automatically after 60.43 seconds. Browser verification confirmed the asker's JTS result (No 2/3, Not sure 1/3) and homecoming result (Not sure 2/3, Nov 3rd 1/3). All four demo tabs were refreshed; no browser errors were observed.

A consistent pre-update backup is retained in the `gramask-demo-data` volume at `/app/data/before-one-minute-demo.db`. Production image and release archive were not replaced.

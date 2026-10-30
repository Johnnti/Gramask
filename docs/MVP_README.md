# GramAsk — crowdsourced campus Q&A agent (MVP)

Ask a question about Grambling State. If the agent knows (static facts, or a fresh crowd answer), it replies right away.
If not, it asks other students in the app, waits for a quorum, saves the answer with an expiry time, and replies to you.

## Run (2 minutes)

Create a virtual environment, activate it, and install the dependencies. Using
`python3 -m pip` avoids relying on a separate `pip` command being available in
zsh.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload
```

If you already created and activated `.venv`, run only the last command when
starting the app again:

```bash
source .venv/bin/activate
python -m uvicorn app:app --reload
```
Open http://localhost:8000 in two browser windows (one normal, one incognito), sign in with different names.
Ask in one window, answer in the other. Default quorum is 3, so open 3 "crowd" windows or run `QUORUM=1 python -m uvicorn app:app` for a demo.

### One-device end-to-end demo

The app starts in local demo mode, so no LLM key or push service is required.
Open the app on one device, enter any name, then use the **End-to-end demo**
buttons:

1. **Create demo question** asks “Is the library open right now?” as a
   simulated student.
2. The mock LLM classifies the question and creates a crowd question.
3. **Simulate student answers** submits three “Yes” answers as simulated
   students.
4. The quorum closes the question, creates a temporary live fact, and returns
   the consensus answer to the simulated asker.
5. The **Notifications** card shows the simulated crowd and answer-ready push
   events. Browser notifications are also used when permission is granted.

Use **Reset demo** to clear the demo database and run the flow again. To use
the normal external LLM path instead, start with `DEMO_MODE=0` and configure
`LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`.

## Plug in a small LLM (optional — works without one using keyword fallback)

Any OpenAI-compatible endpoint:

```bash
# Hosted (e.g. Groq)
export LLM_API_KEY=your_key
export LLM_MODEL=llama-3.1-8b-instant      # use any small model the provider currently serves

# Or local with Ollama
export LLM_BASE_URL=http://localhost:11434/v1
export LLM_MODEL=qwen3:4b
```

## Settings (env vars)

| Var | Default | Meaning |
|---|---|---|
| `QUORUM` | 3 | answers needed to close a crowd question |
| `AGREEMENT` | 0.6 | share needed on the top yes/no answer |
| `TIMEOUT_MIN` | 15 | close question after this many minutes |
| `FACT_TTL_HOURS` | 2 | how long a crowd answer stays valid (menus max 3h) |
| `DEMO_MODE` | 1 | use the deterministic mock LLM and demo controls (`0` disables them) |

## Files

- `app.py` — API + agent loop (classify → lookup → crowd → aggregate → write back)
- `index.html` — the whole frontend (polls every 4s, browser notification when tab is hidden)
- `seed_facts.json` — static Grambling facts; add more here

## Suggested 2-hour split

| Time | Person A | Person B | Person C |
|---|---|---|---|
| 0:00–0:20 | Run it, get an LLM key working | Add 20–30 real facts to `seed_facts.json` | Write 15 demo questions + expected behavior |
| 0:20–1:15 | Tune the classify prompt on demo questions | Style/branding of `index.html` | Deploy (Render/Railway: `uvicorn app:app --host 0.0.0.0 --port $PORT`) |
| 1:15–1:45 | Bug fixes from rehearsal | Bug fixes | Rehearse the demo with 3–4 phones |
| 1:45–2:00 | Freeze. Demo script ready. | | |

## Known MVP shortcuts (say these in the pitch as "next steps")
- Display names, no auth → campus email sign-in
- Every user sees every crowd question → targeted sampling + daily ping cap
- In-app polling notifications → Web Push via a PWA service worker
- No reputation weighting → weight votes by past accuracy

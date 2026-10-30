# Live Gemma 3 verification

`scripts/smoke_live.py` runs the real application API in a test client, uses the
configured Gemma endpoint for every routing and text-consensus decision, and
creates all accounts, questions, and reports in a temporary SQLite database.
It does not read or change the deployment's account database. The database and
synthetic accounts are removed when the run ends.

With native Ollama running and `gemma3:4b` installed:

```bash
source .venv/bin/activate
LLM_BASE_URL=http://127.0.0.1:11434 python scripts/smoke_live.py --timeout 120
```

With the Compose app and model already running:

```bash
docker compose cp scripts/smoke_live.py app:/tmp/smoke_live.py
docker compose exec -T -e PYTHONPATH=/app app python /tmp/smoke_live.py --timeout 120
```

Copying the script also works when the running image was built before the script
was added. The process inherits the model connection from the app container.
For a hosted Gemma 3 endpoint, supply `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`,
and `LLM_API_KEY` through the environment as documented in the main README.
Do not put API keys into command-line arguments or commit them to the repository.

The six cases verify:

1. The configured model is available, live mode is active, and accounts can register.
2. A founding-date question returns the exact trusted fact and its source link.
3. A library-name question returns the corresponding trusted fact and source link.
4. An unrelated geography question is rejected without asking contributors.
5. A question about current library conditions stays pending and preserves its negation.
6. Two agreeing free-text menu reports and one conflicting report resolve with
   exactly two-thirds agreement, preserve a contributor's wording, notify the
   asker, and supply the same fresh evidence to an identical repeat question.

The final case also checks that the asker cannot vote on their own question.
Every answer must identify Gemma 3, and no demonstration or fallback output is
accepted. There are nine inference calls: intent routing, separate trusted-fact
selection for the two static questions, and free-text evidence grouping. Output includes case names, pass/fail
reasons, elapsed seconds, and a final count. Routing failures include only the
structured kind, scope, fact ID, entity, attribute, and answer type for diagnosis;
question text, report text, and credentials are excluded. A failure returns a
nonzero exit code.

CPU inference may take longer during the first model load. `--timeout` adjusts
the timeout for each model call, up to 300 seconds. A successful model inventory
check alone does not prove inference works. This smoke test verifies a bounded
set of real decisions; it is not a model accuracy benchmark, load test, or browser
test. Check the deployed HTTP app and browser separately before release.

#!/usr/bin/env python3
"""Exercise the complete API with real Gemma inference and disposable records.

Run with the application's dependencies installed, for example:
    LLM_BASE_URL=http://127.0.0.1:11434 python scripts/smoke_live.py

The model configuration comes from LLM_* environment variables or the arguments
below. Authentication and crowd records are created only in a temporary SQLite
database. No running app database is opened. Demo mode, fallback, and mocked
model transports are never used. The report contains case names and durations,
not credentials or model response bodies. Model call count depends on routing mode.
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Gemma server URL (otherwise LLM_BASE_URL)")
    parser.add_argument("--model", help="Gemma 3 model identifier (otherwise LLM_MODEL)")
    parser.add_argument("--provider", choices=("ollama", "openai"))
    parser.add_argument("--timeout", type=float, help="Timeout per model call, 1–300 seconds")
    args = parser.parse_args()
    if args.timeout is not None and not 1 <= args.timeout <= 300:
        parser.error("--timeout must be between 1 and 300 seconds")
    for name, value in (("LLM_BASE_URL", args.base_url), ("LLM_MODEL", args.model),
                        ("LLM_PROVIDER", args.provider), ("LLM_TIMEOUT", args.timeout)):
        if value is not None:
            os.environ[name] = str(value)

    # Import after selecting a disposable database, including when run from a
    # production container whose environment names a persistent database.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix="gramask-live-smoke-") as folder:
        os.environ.update({
            "APP_ENV": "test", "APP_ORIGIN": "http://testserver", "SECURE_COOKIES": "0",
            "DB_PATH": str(Path(folder) / "smoke.db"), "DEMO_MODE": "0", "ALLOW_FALLBACK": "0",
            "QUORUM": "3", "AGREEMENT": "0.6", "TIMEOUT_MIN": "60", "AUTH_RATE_LIMIT": "1000",
        })
        from contextlib import ExitStack
        from fastapi.testclient import TestClient
        import app as application
        from config import settings
        from llm import FACTS_BY_ID

        outcomes = []

        def check(condition, reason):
            if not condition:
                raise AssertionError(reason)

        def case(name, work):
            started = time.monotonic()
            try:
                work()
            except AssertionError as exc:
                outcome = {"case": name, "passed": False, "error": str(exc)}
            except Exception as exc:
                # Exceptions from dependencies may contain request data; keep
                # only the class in the console/report.
                outcome = {"case": name, "passed": False, "error": type(exc).__name__}
            else:
                outcome = {"case": name, "passed": True}
            outcome["seconds"] = round(time.monotonic() - started, 2)
            outcomes.append(outcome)
            print(json.dumps(outcome), flush=True)
            return outcome["passed"]

        with ExitStack() as stack:
            owner = stack.enter_context(TestClient(application.app))
            contributors = [TestClient(application.app) for _ in range(3)]
            for client in contributors:
                stack.callback(client.close)

            def setup():
                response = owner.get("/readyz")
                check(response.status_code == 200, "Configured Gemma model is not ready")
                check(response.json()["engine"]["mode"] == "live", "Expected live inference mode")
                for index, client in enumerate([owner, *contributors]):
                    response = client.post("/api/auth/register", json={
                        "username": f"smoke_user_{index}", "display_name": f"Synthetic tester {index}",
                        "password": secrets.token_urlsafe(24),
                    })
                    check(response.status_code == 201, f"Registration returned HTTP {response.status_code}")
                    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]

            def ask(text):
                response = owner.post("/api/ask", json={"text": text})
                check(response.status_code == 200, f"Question returned HTTP {response.status_code}")
                result = response.json()
                check(result["engine"] == "gemma3", "Response did not use Gemma 3")
                check(not result["is_demo"], "Response unexpectedly used demonstration records")
                return result

            def route_details(result):
                # These schema-constrained routing fields contain no question
                # text, student reports, cookies, or provider credentials.
                info = result.get("classification") or {}
                fields = ("kind", "in_scope", "fact_id", "entity", "attribute", "answer_type")
                return " (route=" + json.dumps({key: info.get(key) for key in fields}, sort_keys=True) + ")"

            def grounded(question, fact_id):
                result = ask(question)
                fact = FACTS_BY_ID[fact_id]
                details = route_details(result)
                check(result["status"] == "answered", "Stable question was not answered" + details)
                check(result["source"] == "knowledge", "Stable answer did not use trusted knowledge" + details)
                check(result["answer"] == fact["value"], "Answer was not the exact trusted source text" + details)
                check(result["source_url"] == fact["source_url"], "Trusted source link was not preserved")

            def unfamiliar_topic():
                result = ask("What is the capital of France?")
                check(result["source"] == "pending", "Unfamiliar topic did not reach students" + route_details(result))
                check(any(q['id'] == result['crowd_question_id'] for q in contributors[0].get('/api/inbox').json()),
                      "Student inbox is missing the question")

            def dynamic_negation():
                question = "Is the campus library closed right now?"
                result = ask(question)
                check(result["status"] == "pending", "Current conditions were answered without student reports" + route_details(result))
                info = result["classification"]
                check(info["kind"] == "dynamic" and info["fact_id"] is None,
                      "Current conditions used a historical fact" + route_details(result))
                check(info["answer_type"] == "yes_no", "Yes/no question used the wrong contribution type" + route_details(result))
                inbox = contributors[0].get("/api/inbox").json()
                target = next(item for item in inbox if item["id"] == result["crowd_question_id"])
                check(target["prompt"] == question, "Original question polarity was not preserved")

            def crowd_consensus():
                question = "What is on the campus dining hall menu today?"
                result = ask(question)
                check(result["status"] == "pending", "Menu was answered before fresh student reports" + route_details(result))
                check(result["classification"]["answer_type"] == "text", "Menu question did not permit text reports" + route_details(result))
                question_id = result["crowd_question_id"]
                response = owner.post("/api/answer", json={"question_id": question_id, "value": "Pizza and salad"})
                check(response.status_code == 403, "Asker was allowed to vote on their own question")
                reports = ["Pizza and salad", "Salad with pizza", "Chicken and rice"]
                for client, report in zip(contributors, reports):
                    response = client.post("/api/answer", json={"question_id": question_id, "value": report})
                    check(response.status_code == 200, f"Student report returned HTTP {response.status_code}")
                deadline = time.monotonic() + settings.llm_timeout + 30
                resolved = owner.get(f"/api/queries/{result['id']}").json()
                while resolved['status'] == 'pending' and time.monotonic() < deadline:
                    time.sleep(1)
                    resolved = owner.get(f"/api/queries/{result['id']}").json()
                check(resolved["status"] == "answered", "Agreeing reports did not resolve the question")
                check(resolved["source"] == "crowd" and resolved["engine"] == "gemma3",
                      "Answer did not identify live Gemma crowd consensus")
                check(resolved["n_votes"] == 3, "Eligible report count was incorrect")
                check(abs(resolved["confidence"] - 2 / 3) < 1e-6,
                      "Gemma did not group precisely the two agreeing reports")
                check(any(report in resolved["answer"] for report in reports[:2]),
                      "Consensus did not preserve an agreeing student report")
                check(resolved["valid_until"] > time.time(), "Fresh crowd answer has no future expiry")
                notifications = owner.get("/api/notifications").json()
                check(any(item["kind"] == "question_answered" and item["query_id"] == result["id"]
                          for item in notifications), "Asker did not receive an answer notification")
                cached = ask(question)
                if settings.question_routing == 'community':
                    check(cached['status'] == 'pending' and cached['crowd_question_id'] != question_id,
                          'Community mode must collect fresh reports for every submission')
                else:
                    check(cached["source"] == "crowd" and cached["status"] == "answered",
                          "Identical question did not reuse fresh evidence" + route_details(cached))
                    check(cached["answer"] == resolved["answer"], "Cached answer changed the underlying evidence")

            if case("live_model_inventory_and_accounts", setup):
                def stable_question(text, fact_id):
                    if settings.question_routing == 'community':
                        check(ask(text)['status'] == 'pending', 'Stable question did not reach students')
                    else:
                        grounded(text, fact_id)
                case("founding_date_routing", lambda: stable_question(
                    "When was Grambling State University founded?", "gsu_founded"))
                case("library_name_routing", lambda: stable_question(
                    "What is the name of Grambling State University's library?", "library_name"))
                case("unfamiliar_topic_reaches_students", unfamiliar_topic)
                case("dynamic_route_preserves_negation", dynamic_negation)
                case("text_consensus_notification_and_cache", crowd_consensus)
        summary = {"model": settings.llm_model, "provider": settings.llm_provider,
                   "passed": sum(item["passed"] for item in outcomes), "total": len(outcomes),
                   "isolated_database": True, "demo": False, "fallback": False}
        print(json.dumps({"summary": summary}), flush=True)
        return 0 if all(item["passed"] for item in outcomes) and len(outcomes) == 6 else 1


if __name__ == "__main__":
    raise SystemExit(main())

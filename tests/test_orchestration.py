"""End-to-end API workflow through the real Gemma adapter, with mock HTTP only."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import app as application
from config import settings
from llm import GemmaEngine


class OrchestrationTests(unittest.TestCase):
    def test_gemma_routes_crowd_groups_reports_and_reuses_fresh_evidence(self):
        requests = []

        def model(request):
            payload = json.loads(request.content)
            requests.append(payload)
            self.assertEqual(payload["model"], "gemma3:4b")
            fields = payload["format"]["properties"]
            if "in_scope" in fields:
                output = {"in_scope": True, "kind": "dynamic", "entity": "main_dining_hall",
                          "attribute": "menu_today", "answer_type": "text", "category": "dining"}
            elif 'fact_id' in fields:
                facts = json.loads(payload['messages'][0]['content'].split('\nINPUT:\n')[1])['facts']
                output = {'fact_id': next((fact['id'] for fact in facts if fact['kind'] == 'dynamic'), None)}
            else:
                output = {"representative_index": 0, "supporting_indices": [0, 1, 2]}
            return httpx.Response(200, json={"message": {"content": json.dumps(output)}})

        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            for key, value in {"db_path": str(Path(folder) / "test.db"), "demo_mode": False,
                               "app_origin": "", "secure_cookies": False, "auth_rate_limit": 1000,
                               "quorum": 3, "agreement": 0.6}.items():
                stack.enter_context(patch.object(settings, key, value))
            model_engine = GemmaEngine(replace(settings, llm_model="gemma3:4b", llm_provider="ollama", allow_fallback=False), httpx.MockTransport(model))
            stack.enter_context(patch.object(application, "engine", model_engine))
            owner = stack.enter_context(TestClient(application.app))
            clients = [owner] + [TestClient(application.app) for _ in range(3)]
            for client in clients[1:]:
                stack.callback(client.close)
            for index, client in enumerate(clients):
                response = client.post("/api/auth/register", json={"username": f"gemma_user_{index}",
                    "display_name": f"Gemma Tester {index}", "password": "correct-test-password"})
                self.assertEqual(response.status_code, 201, response.text)
                client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            text = "What is on the dining hall menu today?"
            response = owner.post("/api/ask", json={"text": text})
            self.assertEqual(response.status_code, 200, response.text)
            query = response.json()
            self.assertEqual(query["source"], "pending")
            self.assertEqual(query["engine"], "gemma3")
            self.assertEqual(clients[1].get("/api/inbox").json()[0]["prompt"], text)
            for client, answer in zip(clients[1:], ["Pizza and salad", "They have pizza with salad", "Pizza, salad"]):
                response = client.post("/api/answer", json={"question_id": query["crowd_question_id"], "value": answer})
                self.assertEqual(response.status_code, 200, response.text)
            application.sweep()
            result = owner.get(f"/api/queries/{query['id']}").json()
            self.assertEqual(result["status"], "answered")
            self.assertEqual(result["n_votes"], 3)
            self.assertEqual(result["confidence"], 1)
            self.assertEqual(result["engine"], "gemma3")
            self.assertIn("Pizza and salad", result["answer"])
            cached = owner.post("/api/ask", json={"text": text}).json()
            self.assertEqual(cached["source"], "crowd")
            self.assertEqual(cached["status"], "answered")
            self.assertEqual(len(requests), 4)  # two intent checks, grouping, grounded retrieval
            self.assertEqual(owner.get("/api/config").json()["engine"]["status"], "available")


if __name__ == "__main__":
    unittest.main()

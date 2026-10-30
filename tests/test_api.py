"""HTTP contract regression tests against an isolated SQLite database.

The explicit demo engine makes these tests deterministic and offline. They do
not validate live Gemma inference. Run: python -m unittest discover -s tests -v
"""

import importlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="gramask-tests-")
        cls.db_path = str(Path(cls.temporary.name) / "isolated.db")
        cls.environment = patch.dict(os.environ, {
            "DB_PATH": cls.db_path,
            "APP_ENV": "test", "APP_ORIGIN": "http://testserver",
            "SECURE_COOKIES": "0", "DEMO_MODE": "1",
            "LLM_PROVIDER": "ollama", "LLM_MODEL": "gemma3:4b",
            "LLM_BASE_URL": "http://127.0.0.1:11434", "ALLOW_FALLBACK": "0",
            "QUORUM": "3", "AGREEMENT": "0.75", "TIMEOUT_MIN": "15",
            "FACT_TTL_HOURS": "2", "AUTH_RATE_LIMIT": "1000",
            "ASK_RATE_LIMIT": "1000",
        })
        cls.environment.start()
        # Import a separate app/config instance without disturbing other suites.
        cls.module_names = ("config", "llm", "app", "database", "security")
        cls.saved_modules = {name: sys.modules.pop(name, None) for name in cls.module_names}
        cls.application = importlib.import_module("app")
        cls.lifespan = TestClient(cls.application.app)
        cls.lifespan.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.lifespan.__exit__(None, None, None)
        for name, previous in cls.saved_modules.items():
            sys.modules.pop(name, None)
            if previous is not None:
                sys.modules[name] = previous
        cls.environment.stop()
        cls.temporary.cleanup()

    def setUp(self):
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()

    def student(self):
        client = TestClient(self.application.app, headers={"Origin": "http://testserver"})
        self.clients.append(client)
        username = "student_" + uuid.uuid4().hex[:12]
        response = client.post("/api/auth/register", json={
            "username": username, "display_name": "Test student",
            "password": "A-long-password-123!",
        })
        self.assertIn(response.status_code, (200, 201), response.text)
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return client

    def ask(self, client, text=None):
        text = text or f"Is the library open right now for campus event {uuid.uuid4().hex[:8]}?"
        response = client.post("/api/ask", json={"text": text})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def vote(self, client, question_id, value="Yes", status=200):
        response = client.post("/api/answer", json={"question_id": question_id, "value": value})
        self.assertEqual(response.status_code, status, response.text)
        return response

    def expire_question(self, question_id):
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute("UPDATE crowd_questions SET closes_at=? WHERE id=?", (time.time() - 10, question_id))
        # Run the actual background sweep without a wall-clock timing race.
        self.application.sweep()

    def query(self, client, query_id):
        response = client.get(f"/api/queries/{query_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_authentication_csrf_origin_and_private_queries(self):
        anonymous = TestClient(self.application.app)
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.get("/api/queries").status_code, 401)
        self.assertEqual(anonymous.post("/api/ask", json={"text": "Hello"}).status_code, 401)
        owner, stranger = self.student(), self.student()
        query = self.ask(owner)
        self.assertEqual(stranger.get(f"/api/queries/{query['id']}").status_code, 404)
        self.assertNotIn(query["id"], [item["id"] for item in stranger.get("/api/queries").json()])
        token = owner.headers.pop("X-CSRF-Token")
        self.assertEqual(owner.post("/api/ask", json={"text": "Is the library open?"}).status_code, 403)
        owner.headers["X-CSRF-Token"] = token
        self.assertEqual(owner.post("/api/ask", json={"text": "Is the library open?"}, headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(owner.post("/api/auth/logout", json={}).status_code, 200)
        self.assertEqual(owner.get("/api/auth/me").status_code, 401)

    def test_known_facts_are_grounded(self):
        owner = self.student()
        query = self.ask(owner, "When was Grambling State University founded?")
        self.assertEqual(query["status"], "answered", query)
        self.assertEqual(query["source"], "knowledge")
        self.assertIn("1901", query["answer"])
        self.assertTrue(query.get("source_url", "").startswith("https://"), query)
        self.assertIsNone(query.get("crowd_question_id"))

    def test_broadcast_demo_sends_every_submission_to_all_other_students(self):
        owner, other_asker = self.student(), self.student()
        accounts = owner.post('/api/demo/students', json={}).json()
        with patch.object(self.application.settings, 'question_routing', 'community'), patch.object(self.application.settings, 'demo_broadcast_all', True):
            question_ids = set()
            for asker, text in ((owner, 'What going on at the yard right now'),
                                (owner, 'When was Grambling State University founded?'),
                                (other_asker, 'What is the capital of France?'),
                                (owner, 'What going on at the yard right now')):
                question = self.ask(asker, text)
                self.assertEqual(question['status'], 'pending')
                qid = question['crowd_question_id']
                self.assertNotIn(qid, question_ids)
                question_ids.add(qid)
                self.assertNotIn(qid, [q['id'] for q in asker.get('/api/inbox').json()])
                for account in accounts:
                    headers = {'X-GramAsk-Session': account['slot']}
                    auth = owner.get('/api/auth/me', headers=headers).json()
                    headers['X-CSRF-Token'] = auth['csrf_token']
                    self.assertIn(qid, [q['id'] for q in owner.get('/api/inbox', headers=headers).json()])
                    response = owner.post('/api/answer', json={'question_id': qid, 'value': 'Student report'}, headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.query(asker, question['id'])['n_votes'], 3)
            with patch.object(self.application.settings, 'demo_mode', False):
                self.assertFalse(self.application.settings.broadcast_questions)

    def test_broadcast_demo_counts_every_choice_and_returns_shares(self):
        owner = self.student()
        voters = [self.student() for _ in range(3)]
        with patch.object(self.application.settings, 'question_routing', 'community'), patch.object(self.application.settings, 'demo_broadcast_all', True), patch.object(self.application.settings, 'timeout_min', 1):
            for values in [('No', 'No', 'Not sure'), ('Yes', 'No', 'Not sure'), ('Not sure',) * 3, ('No',) * 3, ('Yes',) * 3]:
                with self.subTest(values=values):
                    question = self.ask(owner)
                    self.assertAlmostEqual(question['closes_at'] - question['created_at'], 60)
                    for index, (voter, value) in enumerate(zip(voters, values)):
                        self.vote(voter, question['crowd_question_id'], value)
                        if index < 2:
                            self.assertEqual(self.query(owner, question['id'])['status'], 'pending')
                    result = self.query(owner, question['id'])
                    self.assertEqual(result['status'], 'answered', result)
                    self.assertEqual(result['n_votes'], 3)
                    summary = result['report_summary']
                    self.assertEqual(summary['closed_by'], 'responses')
                    for item in summary['answers']:
                        self.assertEqual(item['count'], values.count(item['value']))
                        self.assertAlmostEqual(item['share'], values.count(item['value']) / 3)
                    live = next(f for f in owner.get('/api/facts').json()['live'] if f['crowd_question_id'] == question['crowd_question_id'])
                    self.assertEqual(live['report_summary'], summary)
                    notes = [n for n in owner.get('/api/notifications').json() if n['query_id'] == question['id']]
                    self.assertEqual(len(notes), 1)
                    self.assertEqual(notes[0]['kind'], 'question_answered')

    def test_broadcast_demo_returns_written_and_uncertain_responses(self):
        owner = self.student()
        voters = [self.student() for _ in range(3)]
        with patch.object(self.application.settings, 'question_routing', 'community'), patch.object(self.application.settings, 'demo_broadcast_all', True):
            for values, counts in [(('Not sure', "don't know", 'Nov 3rd'), {'Not sure': 2, 'Nov 3rd': 1}),
                                   (('Pizza', 'Salad', 'Rice'), {'Pizza': 1, 'Salad': 1, 'Rice': 1})]:
                question = self.ask(owner, 'When is homecoming?')
                for voter, value in zip(voters, values):
                    self.vote(voter, question['crowd_question_id'], value)
                result = self.query(owner, question['id'])
                self.assertEqual(result['status'], 'answered', result)
                self.assertEqual(result['report_summary']['total'], 3)
                self.assertEqual({a['value']: a['count'] for a in result['report_summary']['answers']}, counts)

    def test_broadcast_demo_deadline_returns_partial_or_no_answers_for_both_types(self):
        owner = self.student()
        voters = [self.student() for _ in range(3)]
        with patch.object(self.application.settings, 'question_routing', 'community'), patch.object(self.application.settings, 'demo_broadcast_all', True):
            for text, values in [('Is the library open?', ('No', 'Not sure')),
                                 ('What is happening at the yard?', ('Not sure', 'Music'))]:
                for count in range(3):
                    with self.subTest(text=text, count=count):
                        question = self.ask(owner, text)
                        for voter, value in zip(voters[:count], values[:count]):
                            self.vote(voter, question['crowd_question_id'], value)
                        self.expire_question(question['crowd_question_id'])
                        result = self.query(owner, question['id'])
                        self.assertEqual(result['status'], 'answered' if count else 'unanswered', result)
                        self.assertEqual(result['n_votes'], count)
                        self.assertEqual(result['report_summary']['total'], count)
                        self.assertEqual(result['report_summary']['closed_by'], 'deadline')
                        if count:
                            self.assertAlmostEqual(sum(a['share'] for a in result['report_summary']['answers']), 1)
                        else:
                            self.assertEqual(result['source'], 'no_answers')
                            self.assertIn('No answers received', result['answer'])
                            self.assertFalse(any(f['crowd_question_id'] == question['crowd_question_id'] for f in owner.get('/api/facts').json()['live']))
                        self.vote(voters[2], question['crowd_question_id'], values[0], status=409)
                        self.application.sweep()
                        notes = [n for n in owner.get('/api/notifications').json() if n['query_id'] == question['id']]
                        self.assertEqual(len(notes), 1)

    def test_written_answer_endorsements_count_once_and_allow_new_responses(self):
        owner = self.student()
        voters = [self.student() for _ in range(3)]
        with patch.object(self.application.settings, 'question_routing', 'community'), patch.object(self.application.settings, 'demo_broadcast_all', True):
            for last_value in (None, 'Music at the yard'):
                question = self.ask(owner, 'What is happening at the yard?')
                qid = question['crowd_question_id']
                self.vote(voters[0], qid, 'A student event')
                inbox = next(q for q in voters[1].get('/api/inbox').json() if q['id'] == qid)
                option = inbox['answer_options'][0]
                self.assertEqual(set(option), {'id', 'value', 'count'})
                self.assertEqual(option['value'], 'A student event')
                self.assertEqual(option['count'], 1)
                payload = {'question_id': qid, 'endorse_answer_id': option['id']}
                response = voters[1].post('/api/answer', json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(voters[1].post('/api/answer', json=payload).status_code, 409)
                self.vote(voters[1], qid, 'Another response', status=409)
                self.assertNotIn(qid, [q['id'] for q in voters[1].get('/api/inbox').json()])
                inbox = next(q for q in voters[2].get('/api/inbox').json() if q['id'] == qid)
                self.assertEqual(inbox['answer_options'], [{**option, 'count': 2}])
                self.assertEqual(inbox['n_answers'], 2)
                if last_value:
                    self.vote(voters[2], qid, last_value)
                else:
                    self.assertEqual(voters[2].post('/api/answer', json=payload).status_code, 200)
                result = self.query(owner, question['id'])
                self.assertEqual(result['status'], 'answered')
                self.assertEqual(result['n_votes'], 3)
                breakdown = {item['value']: item['count'] for item in result['report_summary']['answers']}
                self.assertEqual(breakdown, {'A student event': 2, last_value: 1} if last_value else {'A student event': 3})
                self.assertEqual(voters[1].get('/api/contributions').json()[0]['value'], 'A student event')

    def test_endorsements_reject_wrong_question_own_votes_closed_and_invalid_requests(self):
        owner, first, second = self.student(), self.student(), self.student()
        with patch.object(self.application.settings, 'question_routing', 'community'), patch.object(self.application.settings, 'demo_broadcast_all', True):
            question = self.ask(owner, 'What is happening at the yard?')
            qid = question['crowd_question_id']
            self.vote(first, qid, 'An event')
            inbox = next(q for q in second.get('/api/inbox').json() if q['id'] == qid)
            answer_id = inbox['answer_options'][0]['id']
            payload = {'question_id': qid, 'endorse_answer_id': answer_id}
            self.assertEqual(owner.post('/api/answer', json=payload).status_code, 403)
            self.assertEqual(second.post('/api/answer', json={**payload, 'value': 'Tampered text'}).status_code, 422)
            self.assertEqual(second.post('/api/answer', json={'question_id': qid}).status_code, 422)
            self.assertEqual(second.post('/api/answer', json={**payload, 'endorse_answer_id': 9999999}).status_code, 404)
            another = self.ask(owner, 'What is for lunch?')
            self.assertEqual(second.post('/api/answer', json={**payload, 'question_id': another['crowd_question_id']}).status_code, 404)
            binary = self.ask(owner, 'Is the library open?')
            self.assertEqual(second.post('/api/answer', json={**payload, 'question_id': binary['crowd_question_id']}).status_code, 422)
            self.expire_question(qid)
            self.assertEqual(second.post('/api/answer', json=payload).status_code, 409)

    def test_three_distinct_students_resolve_and_shared_subscribers_are_notified(self):
        owner, subscriber = self.student(), self.student()
        voters = [self.student() for _ in range(3)]
        query = self.ask(owner)
        subscribed = self.ask(subscriber, query["text"])
        question_id = query["crowd_question_id"]
        self.assertEqual(subscribed["crowd_question_id"], question_id)
        self.assertNotIn(question_id, [item["id"] for item in owner.get("/api/inbox").json()])
        self.assertNotIn(question_id, [item["id"] for item in subscriber.get("/api/inbox").json()])
        self.assertIn(owner.post("/api/answer", json={"question_id": question_id, "value": "Yes"}).status_code, (403, 409))
        self.assertIn(subscriber.post("/api/answer", json={"question_id": question_id, "value": "Yes"}).status_code, (403, 409))
        self.vote(voters[0], question_id)
        self.assertEqual(self.query(owner, query["id"])["status"], "pending")
        self.assertEqual(voters[0].post("/api/answer", json={"question_id": question_id, "value": "No"}).status_code, 409)
        self.vote(voters[1], question_id)
        self.assertEqual(self.query(owner, query["id"])["status"], "pending")
        self.vote(voters[2], question_id)
        for client, result in ((owner, query), (subscriber, subscribed)):
            answer = self.query(client, result["id"])
            self.assertEqual(answer["status"], "answered", answer)
            self.assertEqual(answer["source"], "crowd")
            self.assertEqual(answer["n_votes"], 3)
            self.assertEqual(answer["confidence"], 1)
            self.assertGreater(answer["valid_until"], time.time())
            notifications = client.get("/api/notifications").json()
            self.assertTrue(any(item.get("query_id") == result["id"] for item in notifications), notifications)
            self.assertEqual(client.post("/api/notifications/read", json={}).status_code, 200)
            self.assertTrue(all(item["read"] for item in client.get("/api/notifications").json()))
        self.assertEqual(len([entry for entry in voters[0].get("/api/contributions").json() if entry["question_id"] == question_id]), 1)

    def test_not_sure_is_excluded_from_quorum_and_timeout_never_publishes_partial_vote(self):
        owner = self.student()
        voters = [self.student() for _ in range(3)]
        query = self.ask(owner)
        question_id = query["crowd_question_id"]
        for client, value in zip(voters, ("Yes", "Yes", "Not sure")):
            self.vote(client, question_id, value)
        self.assertEqual(self.query(owner, query["id"])["status"], "pending")
        self.expire_question(question_id)
        result = self.query(owner, query["id"])
        self.assertEqual(result["status"], "unanswered", result)
        self.assertFalse(any(item.get("question") == query["text"] for item in owner.get("/api/facts").json()["live"]))
        late = self.student()
        self.assertIn(late.post("/api/answer", json={"question_id": question_id, "value": "Yes"}).status_code, (409, 410))

    def test_low_agreement_stays_pending_and_timeout_refuses(self):
        owner = self.student()
        query = self.ask(owner)
        for value in ("Yes", "Yes", "No"):
            self.vote(self.student(), query["crowd_question_id"], value)
        self.assertEqual(self.query(owner, query["id"])["status"], "pending")
        self.expire_question(query["crowd_question_id"])
        self.assertEqual(self.query(owner, query["id"])["status"], "unanswered")

    def test_free_text_disagreement_does_not_publish_a_model_guess(self):
        owner = self.student()
        query = self.ask(owner, f"What is the dining hall serving today for campus event {uuid.uuid4().hex[:8]}?")
        for value in ("Pizza", "Salad", "Rice"):
            self.vote(self.student(), query["crowd_question_id"], value)
        self.assertEqual(self.query(owner, query["id"])["status"], "pending")
        self.expire_question(query["crowd_question_id"])
        self.assertEqual(self.query(owner, query["id"])["status"], "unanswered")

    def test_expired_live_facts_are_not_reused(self):
        owner = self.student()
        query = self.ask(owner)
        for _ in range(3):
            self.vote(self.student(), query["crowd_question_id"])
        self.assertEqual(self.query(owner, query["id"])["status"], "answered")
        fresh = self.ask(self.student(), query["text"])
        self.assertEqual(fresh["status"], "answered")
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute("UPDATE facts SET valid_until=?", (time.time() - 10,))
        expired = self.ask(self.student(), query["text"])
        self.assertEqual(expired["status"], "pending", expired)
        self.assertNotEqual(expired["crowd_question_id"], query["crowd_question_id"])

    def test_demo_is_isolated_and_reset_preserves_real_records(self):
        owner, stranger = self.student(), self.student()
        real = self.ask(owner)
        demo_response = owner.post("/api/demo/seed-question", json={})
        self.assertEqual(demo_response.status_code, 200, demo_response.text)
        demo = demo_response.json()
        question_id = demo["crowd_question_id"]
        self.assertTrue(demo["is_demo"])
        self.assertNotIn(question_id, [item["id"] for item in stranger.get("/api/inbox").json()])
        denied = stranger.post(f"/api/demo/auto-answer?question_id={question_id}", json={})
        self.assertIn(denied.status_code, (403, 404))
        self.assertEqual(owner.post(f"/api/demo/auto-answer?question_id={question_id}", json={}).status_code, 200)
        self.assertEqual(self.query(owner, demo["id"])["status"], "answered")
        self.assertFalse(any(item.get("is_demo") for item in stranger.get("/api/facts").json()["live"]))
        real_equivalent = self.ask(stranger, demo["text"])
        self.assertEqual(real_equivalent["status"], "pending")
        self.assertNotEqual(real_equivalent["crowd_question_id"], question_id)
        strangers_demo = stranger.post("/api/demo/seed-question", json={}).json()
        self.assertEqual(owner.post("/api/demo/reset", json={}).status_code, 200)
        self.assertEqual(self.query(owner, real["id"])["status"], "pending")
        self.assertEqual(owner.get(f"/api/queries/{demo['id']}").status_code, 404)
        self.assertEqual(self.query(stranger, strangers_demo["id"])["status"], "pending")

    def test_default_configuration_disables_demo(self):
        clean_env = {key: value for key, value in os.environ.items() if key not in {
            "DEMO_MODE", "APP_ENV", "APP_ORIGIN", "SECURE_COOKIES", "LLM_PROVIDER",
            "LLM_BASE_URL", "LLM_MODEL", "ALLOW_FALLBACK",
        }}
        script = "import json; from config import settings; print(json.dumps({'demo':settings.demo_mode,'model':settings.llm_model,'fallback':settings.allow_fallback}))"
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=clean_env, text=True, capture_output=True, check=True)
        settings = json.loads(result.stdout)
        self.assertEqual(settings, {"demo": False, "model": "gemma3:4b", "fallback": False})

    def test_three_student_tabs_share_cookie_jar_but_keep_accounts_separate(self):
        owner, outsider = self.student(), self.student()
        owner_id = owner.get('/api/auth/me').json()['user']['id']
        question = owner.post('/api/demo/seed-question', json={}).json()
        prepared = owner.post('/api/demo/students', json={})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        accounts = prepared.json()
        self.assertEqual(len({a['id'] for a in accounts}), 3)
        self.assertNotIn(owner_id, [a['id'] for a in accounts])
        self.assertEqual(owner.get('/api/demo/students').json(), accounts)
        self.assertNotIn(question['crowd_question_id'], [q['id'] for q in outsider.get('/api/inbox').json()])
        other_question = self.ask(outsider)
        for index, account in enumerate(accounts):
            # All four accounts deliberately use the same browser cookie jar.
            headers = {'X-GramAsk-Session': account['slot']}
            auth = owner.get('/api/auth/me', headers=headers).json()
            self.assertEqual(auth['user']['id'], account['id'])
            self.assertTrue(auth['user']['is_demo_student'])
            inbox = owner.get('/api/inbox', headers=headers).json()
            self.assertIn(question['crowd_question_id'], [q['id'] for q in inbox])
            self.assertNotIn(other_question['crowd_question_id'], [q['id'] for q in inbox])
            self.assertEqual(owner.get(f"/api/queries/{question['id']}", headers=headers).status_code, 404)
            body = {'question_id': question['crowd_question_id'], 'value': 'Yes'}
            self.assertEqual(owner.post('/api/answer', json=body, headers=headers).status_code, 403)
            headers['X-CSRF-Token'] = auth['csrf_token']
            vote = owner.post('/api/answer', json=body, headers=headers)
            self.assertEqual(vote.status_code, 200, vote.text)
            self.assertEqual(owner.post('/api/demo/students', json={}, headers=headers).status_code, 403)
            self.assertEqual(owner.post('/api/answer', json={'question_id':other_question['crowd_question_id'], 'value':'Yes'}, headers=headers).status_code, 404)
            self.assertEqual(owner.get('/api/auth/me').json()['user']['id'], owner_id)
            self.assertEqual(self.query(owner, question['id'])['status'], 'answered' if index == 2 else 'pending')
            self.assertEqual(len(owner.get('/api/contributions', headers=headers).json()), 1)
        resolved = self.query(owner, question['id'])
        self.assertEqual(resolved['n_votes'], 3)
        self.assertEqual(resolved['confidence'], 1)
        self.assertTrue(any(n['query_id'] == question['id'] for n in owner.get('/api/notifications').json()))
        self.assertEqual(owner.post('/api/auth/logout', json={}, headers=headers).status_code, 200)
        self.assertEqual(owner.get('/api/auth/me', headers=headers).status_code, 401)
        self.assertEqual(owner.get('/api/auth/me').json()['user']['id'], owner_id)
        self.assertEqual(owner.get('/api/auth/me', headers={'X-GramAsk-Session':accounts[0]['slot']}).status_code, 200)

    def test_demo_students_can_answer_typed_questions_and_reconnect_without_duplicates(self):
        owner = self.student()
        question = self.ask(owner)
        first = owner.post('/api/demo/students', json={}).json()
        accounts = owner.post('/api/demo/students', json={}).json()
        self.assertEqual([a['id'] for a in first], [a['id'] for a in accounts])
        for account in accounts:
            headers = {'X-GramAsk-Session':account['slot']}
            auth = owner.get('/api/auth/me', headers=headers).json()
            headers['X-CSRF-Token'] = auth['csrf_token']
            self.assertEqual(owner.post('/api/answer', json={'question_id':question['crowd_question_id'], 'value':'Yes'}, headers=headers).status_code, 200)
        self.assertEqual(self.query(owner, question['id'])['status'], 'answered')
        with patch.object(self.application.settings, 'demo_mode', False):
            self.assertEqual(owner.post('/api/demo/students', json={}).status_code, 404)
            self.assertEqual(owner.get('/api/auth/me', headers=headers).status_code, 401)

    def test_session_selectors_are_not_credentials_and_never_fall_back(self):
        owner = self.student()
        account = owner.post('/api/demo/students', json={}).json()[0]
        with TestClient(self.application.app) as empty_browser:
            self.assertEqual(empty_browser.get('/api/auth/me', headers={'X-GramAsk-Session':account['slot']}).status_code, 401)
        self.assertEqual(owner.get('/api/auth/me', headers={'X-GramAsk-Session':'f' * 32}).status_code, 401)
        self.assertEqual(owner.get('/api/auth/me', headers={'X-GramAsk-Session':'bad;cookie=other'}).status_code, 400)

    def test_jts_question_reaches_all_three_students_and_returns_to_asker(self):
        owner = self.student()
        accounts = owner.post('/api/demo/students', json={}).json()
        question = self.ask(owner, 'Is JTS open right now?')
        self.assertEqual(question['status'], 'pending')
        self.assertEqual(question['classification']['entity'], 'jts')
        for account in accounts:
            headers = {'X-GramAsk-Session': account['slot']}
            auth = owner.get('/api/auth/me', headers=headers).json()
            headers['X-CSRF-Token'] = auth['csrf_token']
            inbox = owner.get('/api/inbox', headers=headers).json()
            received = next(q for q in inbox if q['id'] == question['crowd_question_id'])
            self.assertEqual(received['prompt'], 'Is JTS open right now?')
            self.assertEqual(received['answer_type'], 'yes_no')
            response = owner.post('/api/answer', json={
                'question_id': received['id'], 'value': 'Yes'}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
        result = self.query(owner, question['id'])
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(result['n_votes'], 3)
        self.assertTrue(any(n['query_id'] == question['id'] and n['kind'] == 'question_answered'
                            for n in owner.get('/api/notifications').json()))

    def test_normal_mode_disables_demo_routes_and_requires_model(self):
        isolated = {**os.environ, "DEMO_MODE": "0", "ALLOW_FALLBACK": "0",
                    "DB_PATH": str(Path(self.temporary.name) / "normal-mode.db"),
                    "LLM_BASE_URL": "http://127.0.0.1:1", "LLM_TIMEOUT": "1"}
        script = '''
import json
from fastapi.testclient import TestClient
from app import app
with TestClient(app, headers={"Origin":"http://testserver"}) as client:
    auth = client.post("/api/auth/register", json={"username":"normal_user", "display_name":"Test", "password":"A-long-password-123!"})
    assert auth.status_code == 201, auth.text
    client.headers["X-CSRF-Token"] = auth.json()["csrf_token"]
    print(json.dumps({
        "demo": client.post("/api/demo/seed-question", json={}).status_code,
        "ready": client.get("/readyz").status_code,
        "ask": client.post("/api/ask", json={"text":"When was Grambling State University founded?"}).status_code,
        "queries": client.get("/api/queries").json(),
    }))
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=isolated, text=True, capture_output=True, check=True, timeout=15)
        outcome = json.loads(result.stdout)
        self.assertEqual(outcome["demo"], 404)
        self.assertEqual(outcome["ready"], 503)
        self.assertEqual(outcome["ask"], 503)
        self.assertFalse(outcome["queries"])


if __name__ == "__main__":
    unittest.main()

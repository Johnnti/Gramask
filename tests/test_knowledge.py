"""Knowledge-first routing, evidence freshness, and privacy regressions."""
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import app as application
from config import settings
from knowledge import fresh
from llm import GemmaEngine, STATIC_FACTS
from security import RateLimiter


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(application, 'limiter', RateLimiter()))
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        for key, value in {'db_path': str(Path(folder) / 'knowledge.db'), 'demo_mode': True,
                           'demo_live_llm': True, 'demo_broadcast_all': True,
                           'question_routing': 'knowledge_first', 'live_freshness_min': 5,
                           'fact_ttl_hours': 2, 'app_origin': '', 'app_env': 'test',
                           'secure_cookies': False, 'auth_rate_limit': 1000, 'ask_rate_limit': 1000,
                           'quorum': 3, 'agreement': 0.6, 'timeout_min': 1}.items():
            self.stack.enter_context(patch.object(settings, key, value))
        self.requests = []
        self.on_selection = None
        self.forced_id = None
        self.reject_selection = False
        model = GemmaEngine(replace(settings, allow_fallback=False), httpx.MockTransport(self.model))
        self.stack.enter_context(patch.object(application, 'engine', model))
        self.owner = self.stack.enter_context(TestClient(application.app))
        self.voters = [TestClient(application.app) for _ in range(3)]
        for index, client in enumerate([self.owner, *self.voters]):
            if index:
                self.stack.callback(client.close)
            response = client.post('/api/auth/register', json={'username': f'knowledge_user_{index}',
                'display_name': f'Test student {index}', 'password': 'Disposable-test-password-123!'})
            self.assertEqual(response.status_code, 201, response.text)
            client.headers['X-CSRF-Token'] = response.json()['csrf_token']

    def model(self, request):
        payload = json.loads(request.content)
        supplied = json.loads(payload['messages'][0]['content'].split('\nINPUT:\n')[1])
        fields = payload['format']['properties']
        self.requests.append(supplied)
        if 'fact_id' in fields:
            selected = None
            for fact in supplied['facts']:
                if (fact.get('original_question') == supplied['question'] or
                    (fact['id'] == 'static:gsu_founded' and supplied['question'] == 'When was Grambling founded?')):
                    selected = fact['id']
                    break
            output = {'fact_id': self.forced_id or (None if self.reject_selection else selected)}
            if self.on_selection:
                self.on_selection()
        else:
            stable = supplied['question'] == 'When was Grambling founded?'
            output = {'in_scope': stable, 'kind': 'static' if stable else 'dynamic', 'entity': 'library',
                      'attribute': 'status', 'category': 'library',
                      'answer_type': 'yes_no' if supplied['question'].startswith('Is ') else 'text'}
        return httpx.Response(200, json={'message': {'content': json.dumps(output)}})

    def ask(self, text, client=None):
        response = (client or self.owner).post('/api/ask', json={'text': text})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def answered(self, text='Is the library open right now?', values=('Yes', 'Yes', 'Yes')):
        question = self.ask(text)
        self.assertEqual(question['status'], 'pending')
        for client, value in zip(self.voters, values):
            response = client.post('/api/answer', json={'question_id': question['crowd_question_id'], 'value': value})
            self.assertEqual(response.status_code, 200, response.text)
        result = self.owner.get(f"/api/queries/{question['id']}").json()
        self.assertEqual(result['status'], 'answered')
        return result

    def test_stable_fact_returns_from_gemma_without_opening_community_question(self):
        result = self.ask('When was Grambling founded?')
        self.assertEqual(result['source'], 'knowledge')
        self.assertIn('1901', result['answer'])
        self.assertEqual(result['engine'], 'gemma3')
        self.assertIsNone(result['valid_until'])
        self.assertIsNone(result['crowd_question_id'])
        self.assertTrue(result['source_url'].startswith('https://'))
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn('facts', self.requests[0])
        self.assertIn('facts', self.requests[1])
        self.assertEqual(self.voters[0].get('/api/inbox').json(), [])
        # Old curated verification dates do not trigger the live-data TTL.
        with patch.object(application, 'STATIC_FACTS', [{**f, 'verified_at': '2000-01-01'} for f in STATIC_FACTS]):
            self.assertEqual(self.ask('When was Grambling founded?')['source'], 'knowledge')
        config = self.owner.get('/api/config').json()
        self.assertEqual(config['engine']['mode'], 'live')
        self.assertTrue(config['demo_mode'])
        self.assertEqual(config['live_freshness_min'], 5)

    def test_unknown_topic_routes_to_all_three_students_despite_scope_rejection(self):
        result = self.ask('What is the capital of France?')
        self.assertEqual(result['status'], 'pending')
        for student in self.voters:
            self.assertIn(result['crowd_question_id'], [q['id'] for q in student.get('/api/inbox').json()])
        self.assertEqual(self.owner.get('/api/inbox').json(), [])

    def test_current_conditions_never_offer_static_descriptions_to_model(self):
        # Reproduce a model eager to pick a related static fact if offered one.
        self.forced_id = 'static:library_resources'
        result = self.ask('Is the library closed right now?')
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn('facts', self.requests[0])

    def test_fresh_answers_reused_without_extending_expiry(self):
        result = self.answered()
        with application.db() as connection:
            fact = dict(connection.execute('SELECT * FROM facts').fetchone())
        self.assertAlmostEqual(fact['valid_until'] - fact['created_at'], 300)
        for _ in range(2):
            repeated = self.ask(result['text'])
            self.assertEqual(repeated['source'], 'crowd')
            self.assertEqual(repeated['answer'], result['answer'])
            self.assertEqual(repeated['valid_until'], result['valid_until'])
        with application.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM crowd_questions').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT created_at FROM facts').fetchone()[0], fact['created_at'])

    def test_stale_legacy_two_hour_fact_never_reaches_model_or_live_list(self):
        result = self.answered()
        with application.db() as connection:
            connection.execute('UPDATE facts SET created_at=?, valid_until=?', (time.time() - 301, time.time() + 7200))
        self.requests.clear()
        repeated = self.ask(result['text'])
        self.assertEqual(repeated['status'], 'pending')
        self.assertNotEqual(repeated['crowd_question_id'], result['crowd_question_id'])
        self.assertFalse(any(f['id'].startswith('live:') for r in self.requests for f in r.get('facts', [])))
        self.assertEqual(self.owner.get('/api/facts').json()['live'], [])
        self.assertEqual(self.owner.get('/api/stats').json()['fresh_answers'], 0)

    def test_fact_expiring_during_inference_routes_to_community(self):
        result = self.answered()
        def expire():
            with application.db() as connection:
                connection.execute('UPDATE facts SET created_at=?', (time.time() - 301,))
        self.on_selection = expire
        repeated = self.ask(result['text'])
        self.assertEqual(repeated['status'], 'pending')
        self.assertNotEqual(repeated['crowd_question_id'], result['crowd_question_id'])

    def test_uncertainty_and_ties_are_returned_to_asker_but_not_reused_as_knowledge(self):
        for number, values in enumerate([('Not sure',) * 3, ('Yes', 'No', 'Not sure')]):
            result = self.answered(f'Is the library open right now for event {number}?', values)
            self.assertEqual(result['report_summary']['total'], 3)
            self.requests.clear()
            self.assertEqual(self.ask(result['text'])['status'], 'pending')
            self.assertFalse(any(f['id'].startswith('live:') for r in self.requests for f in r.get('facts', [])))

    def test_private_demo_evidence_and_opposite_yes_no_never_reach_model(self):
        result = self.answered()
        self.requests.clear()
        self.assertEqual(self.ask('Is the library closed right now?')['status'], 'pending')
        self.assertFalse(any(f['id'].startswith('live:') for r in self.requests for f in r.get('facts', [])))
        owner_id = self.owner.get('/api/auth/me').json()['user']['id']
        with application.db() as connection:
            connection.execute('UPDATE facts SET demo_owner_id=?', (owner_id,))
        self.requests.clear()
        self.assertEqual(self.ask(result['text'], self.voters[0])['status'], 'pending')
        self.assertFalse(any(f['id'].startswith('live:') for r in self.requests for f in r.get('facts', [])))

    def test_model_miss_cannot_fall_back_to_exact_cached_fact(self):
        result = self.answered()
        self.reject_selection = True
        self.assertEqual(self.ask(result['text'])['status'], 'pending')

    def test_unknown_model_id_is_rejected_without_publishing_an_answer(self):
        self.forced_id = 'static:invented'
        response = self.owner.post('/api/ask', json={'text': 'When was Grambling founded?'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.owner.get('/api/queries').json(), [])

    def test_search_index_survives_restart_and_tracks_delete(self):
        result = self.answered()
        application.init_db()
        self.assertEqual(self.ask(result['text'])['source'], 'crowd')
        with application.db() as connection:
            connection.execute('DELETE FROM facts')
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM knowledge_search WHERE doc_id LIKE 'live:%'").fetchone()[0], 0)
        self.assertEqual(self.ask(result['text'])['status'], 'pending')

    def test_five_minute_boundary_and_query_syntax(self):
        fact = {'created_at': 1000, 'valid_until': 8200}
        self.assertTrue(fresh(fact, 1299.999, settings))
        self.assertFalse(fresh(fact, 1300, settings))
        self.assertFalse(fresh(fact, 999, settings))
        self.assertEqual(self.ask('library OR " : * - (NEAR)') ['status'], 'pending')


if __name__ == '__main__':
    unittest.main()

"""Production behavior with HTTPS cookies and a controlled Gemma transport."""
from contextlib import ExitStack, closing
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts.backup_db import backup_database


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {
            'APP_ENV': 'production', 'APP_ORIGIN': 'https://gramask.test',
            'DB_PATH': str(Path(folder) / 'production.db'), 'DEMO_MODE': '0',
            'DEMO_BROADCAST_ALL': '0', 'SECURE_COOKIES': '1', 'ALLOW_FALLBACK': '0',
            'QUESTION_ROUTING': 'community', 'AUTH_RATE_LIMIT': '1000', 'ASK_RATE_LIMIT': '1000',
            'QUORUM': '3', 'AGREEMENT': '0.6',
        }))
        names = ('app', 'llm', 'database', 'config', 'security')
        previous = {name: sys.modules.pop(name, None) for name in names}
        def restore():
            for name, module in previous.items():
                sys.modules.pop(name, None)
                if module is not None:
                    sys.modules[name] = module
        self.stack.callback(restore)
        self.app = importlib.import_module('app')
        self.requests = []
        def model(request):
            payload = json.loads(request.content)
            self.requests.append(payload)
            if 'in_scope' in payload['format']['properties']:
                # Reproduce a model rejecting an unfamiliar name/topic.
                result = {'in_scope': False, 'kind': 'static', 'entity': 'university',
                          'attribute': 'status', 'answer_type': 'text', 'category': 'campus'}
            else:
                result = {'representative_index': 0, 'supporting_indices': [0, 1]}
            return httpx.Response(200, json={'message': {'content': json.dumps(result)}})
        self.app.engine.transport = httpx.MockTransport(model)
        from fastapi.testclient import TestClient
        self.client_type = TestClient
        self.owner = self.stack.enter_context(TestClient(self.app.app, base_url='https://gramask.test',
                                                        headers={'Origin': 'https://gramask.test'}))
        self.register(self.owner, 0)

    def register(self, client, number):
        response = client.post('/api/auth/register', json={
            'username': f'production_tester_{number}', 'display_name': f'Tester {number}',
            'password': 'Disposable-test-password-123!',
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIn('Secure', response.headers['set-cookie'])
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']

    def test_every_topic_reaches_students_and_text_consensus_runs_in_background(self):
        voters = []
        for number in range(1, 4):
            client = self.client_type(self.app.app, base_url='https://gramask.test',
                                      headers={'Origin': 'https://gramask.test'})
            self.stack.callback(client.close)
            self.register(client, number)
            voters.append(client)
        for text in ('What going on at the yard right now', 'When was Grambling founded?',
                     'What is the capital of France?'):
            response = self.owner.post('/api/ask', json={'text': text})
            self.assertEqual(response.status_code, 200, response.text)
            query = response.json()
            self.assertEqual(query['status'], 'pending')
            self.assertEqual(query['engine'], 'gemma3')
            self.assertFalse(query['is_demo'])
            for voter in voters:
                self.assertTrue(any(q['id'] == query['crowd_question_id'] and q['prompt'] == text
                                    for q in voter.get('/api/inbox').json()))
        model_calls = len(self.requests)
        for voter, answer in zip(voters, ['Paris', 'The answer is Paris', 'Lyon']):
            result = voter.post('/api/answer', json={'question_id': query['crowd_question_id'], 'value': answer})
            self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.requests), model_calls, 'Submitting a report must not wait for Gemma')
        self.assertEqual(self.owner.get(f"/api/queries/{query['id']}").json()['status'], 'pending')
        self.app.sweep()
        result = self.owner.get(f"/api/queries/{query['id']}").json()
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(result['n_votes'], 3)
        self.assertAlmostEqual(result['confidence'], 2 / 3)
        self.assertTrue(any(n['query_id'] == query['id'] and n['kind'] == 'question_answered'
                            for n in self.owner.get('/api/notifications').json()))
        repeated = self.owner.post('/api/ask', json={'text': text}).json()
        self.assertEqual(repeated['status'], 'pending')
        self.assertNotEqual(repeated['crowd_question_id'], query['crowd_question_id'])
        self.assertEqual(voters[0].get(f"/api/queries/{query['id']}").status_code, 404)

    def test_production_rejects_demo_routes_bad_hosts_and_cross_origin_mutations(self):
        self.assertEqual(self.owner.post('/api/demo/students', json={}).status_code, 404)
        self.assertEqual(self.owner.get('/api/config', headers={'Host': 'attacker.test'}).status_code, 400)
        response = self.owner.post('/api/ask', json={'text': 'Campus question'}, headers={'Origin': 'https://attacker.test'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.requests, [])
        config = self.owner.get('/api/config')
        self.assertFalse(config.json()['demo_mode'])
        self.assertEqual(config.headers['cache-control'], 'no-store')
        self.assertIn('max-age=', config.headers['strict-transport-security'])

    def test_knowledge_first_still_routes_unfamiliar_topics(self):
        with patch.object(self.app.settings, 'question_routing', 'knowledge_first'):
            result = self.owner.post('/api/ask', json={'text': 'What is happening at XYZ today?'}).json()
        self.assertEqual(result['source'], 'pending')
        self.assertIsNotNone(result['crowd_question_id'])

    def test_only_the_private_proxy_can_supply_client_addresses_for_rate_limits(self):
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
        proxy = ProxyHeadersMiddleware(self.app.app, trusted_hosts=['172.30.180.2'])
        with patch.object(self.app.settings, 'auth_rate_limit', 1):
            for connection_ip, expected in [('172.30.180.2', (401, 401)),
                                             ('198.51.100.9', (401, 429))]:
                with self.client_type(proxy, base_url='https://gramask.test', client=(connection_ip, 1234)) as client:
                    actual = []
                    for forwarded in ['203.0.113.1', '203.0.113.2']:
                        actual.append(client.post('/api/auth/login', json={'username': 'absent', 'password': 'invalid'},
                                                  headers={'X-Forwarded-For': forwarded}).status_code)
                    self.assertEqual(tuple(actual), expected)


class BackupTests(unittest.TestCase):
    def test_backup_includes_committed_wal_data_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'live.db', Path(folder) / 'snapshots' / 'backup.db'
            with closing(sqlite3.connect(source)) as live:
                live.execute('PRAGMA journal_mode=WAL')
                live.execute('CREATE TABLE questions(text TEXT)')
                live.execute("INSERT INTO questions VALUES ('Committed question')")
                live.commit()
                backup_database(source, target)
                with closing(sqlite3.connect(target)) as restored:
                    self.assertEqual(restored.execute('SELECT text FROM questions').fetchone()[0], 'Committed question')
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    backup_database(source, target)

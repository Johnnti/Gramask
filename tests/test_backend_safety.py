"""Storage migration, race safety and validation regressions; uses temporary data only."""
import importlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


class BackendSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='gramask-safety-')
        cls.db_path = str(Path(cls.temporary.name) / 'app.db')
        cls.environment = patch.dict(os.environ, {
            'DB_PATH': cls.db_path, 'APP_ENV': 'test', 'APP_ORIGIN': 'http://testserver',
            'SECURE_COOKIES': '0', 'DEMO_MODE': '1', 'QUORUM': '3', 'AGREEMENT': '0.75',
            'AUTH_RATE_LIMIT': '1000', 'ASK_RATE_LIMIT': '1000',
        })
        cls.environment.start()
        cls.saved_modules = {name: sys.modules.pop(name, None) for name in ('app', 'llm', 'database', 'config', 'security')}
        cls.module = importlib.import_module('app')
        cls.lifespan = TestClient(cls.module.app)
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

    def student(self):
        client = TestClient(self.module.app, headers={'Origin': 'http://testserver'})
        self.addCleanup(client.close)
        response = client.post('/api/auth/register', json={
            'username': 'safety_' + uuid.uuid4().hex[:12], 'display_name': 'Student',
            'password': 'a-long-password-1234',
        })
        self.assertEqual(response.status_code, 201, response.text)
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        return client

    def ask(self, client, text=None):
        response = client.post('/api/ask', json={'text': text or f'Is the library open now for campus project {uuid.uuid4().hex}? '})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def vote(self, client, question_id, value='Yes'):
        response = client.post('/api/answer', json={'question_id': question_id, 'value': value})
        self.assertEqual(response.status_code, 200, response.text)

    def test_chunked_body_is_bounded_without_content_length(self):
        with TestClient(self.module.app) as client:
            request = client.build_request('POST', '/api/auth/login', content=iter([b'x' * 9000, b'y' * 9000]))
            self.assertNotIn('content-length', request.headers)
            response = client.send(request)
            self.assertEqual(response.status_code, 413, response.text)

    def test_concurrent_closures_create_one_fact_and_one_notification(self):
        owner = self.student()
        voters = [self.student() for _ in range(3)]
        query = self.ask(owner)
        question_id = query['crowd_question_id']
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            for voter in voters:
                user_id = voter.get('/api/auth/me').json()['user']['id']
                connection.execute('INSERT INTO crowd_answers(question_id,user_id,value,created_at) VALUES (?,?,?,?)', (question_id, user_id, 'Yes', time.time()))
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(self.module.try_close, [question_id] * 24))
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM facts WHERE crowd_question_id=?', (question_id,)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notifications WHERE crowd_question_id=? AND kind='question_answered'", (question_id,)).fetchone()[0], 1)
        self.assertEqual(owner.get(f"/api/queries/{query['id']}").json()['status'], 'answered')

    def test_prior_contributor_becoming_subscriber_cannot_self_vote(self):
        owner, subscriber, voter = self.student(), self.student(), self.student()
        query = self.ask(owner)
        self.vote(subscriber, query['crowd_question_id'])
        self.vote(voter, query['crowd_question_id'])
        subscribed = self.ask(subscriber, query['text'])
        self.assertEqual(subscribed['crowd_question_id'], query['crowd_question_id'])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM crowd_answers WHERE question_id=?', (query['crowd_question_id'],)).fetchone()[0], 1)
        self.assertEqual(subscriber.post('/api/answer', json={'question_id': query['crowd_question_id'], 'value': 'Yes'}).status_code, 403)

    def test_gemma_aggregation_retries_and_does_not_hold_sqlite_write_lock(self):
        owner = self.student()
        query = self.ask(owner, f'What is on the campus dining menu today for {uuid.uuid4().hex}?')
        question_id = query['crowd_question_id']
        values = ['Vegetable curry'] * 3
        calls = []
        def aggregate(prompt, received):
            # A separate writer must succeed while the model call is in progress.
            with closing(sqlite3.connect(self.db_path, timeout=0.2)) as connection, connection:
                connection.execute('UPDATE crowd_questions SET next_attempt_at=0 WHERE id=?', (question_id,))
            calls.append(received)
            if len(calls) == 1:
                return None
            return {'value': values[0], 'supporting_indices': [0, 1, 2], 'agreement': 1, 'engine': 'gemma3'}
        with patch.object(self.module.engine, 'aggregate', side_effect=aggregate):
            for value in values:
                self.vote(self.student(), question_id, value)
            self.assertEqual(owner.get(f"/api/queries/{query['id']}").json()['status'], 'pending')
            self.module.sweep()
        self.assertEqual(len(calls), 2)
        answer = owner.get(f"/api/queries/{query['id']}").json()
        self.assertEqual(answer['status'], 'answered')
        self.assertEqual(answer['engine'], 'gemma3')
        cached = self.ask(self.student(), query['text'])
        self.assertEqual(cached['engine'], 'gemma3')

    def test_text_agreement_and_unknown_votes_are_enforced_at_timeout(self):
        question = {'id': 1, 'answer_type': 'text', 'prompt': 'Campus menu?', 'demo_owner_id': None, 'engine': 'demo'}
        with patch.object(self.module.engine, 'aggregate', return_value={
            'value': 'Pasta', 'agreement': 1, 'supporting_indices': [0, 1, 1, 500], 'engine': 'gemma3',
        }):
            result, count = self.module.consensus(question, [{'value': value} for value in ('Pasta', 'Pasta', 'Rice', 'Not sure')])
        self.assertEqual(count, 3)
        self.assertIsNone(result)
        owner = self.student()
        query = self.ask(owner, f'What is on the dining hall menu today for {uuid.uuid4().hex}?')
        for value in ('Pasta', 'Rice', 'Beans'):
            self.vote(self.student(), query['crowd_question_id'], value)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute('UPDATE crowd_questions SET closes_at=? WHERE id=?', (time.time() - 1, query['crowd_question_id']))
        self.module.sweep()
        answer = owner.get(f"/api/queries/{query['id']}").json()
        self.assertEqual(answer['status'], 'unanswered')
        self.assertIsNone(answer['confidence'])

    def test_uncertain_free_text_variants_never_count_toward_quorum(self):
        question = {'id': 1, 'answer_type': 'text', 'prompt': 'Campus menu?', 'demo_owner_id': None, 'engine': 'demo'}
        for value in ('Not sure.', 'I don’t know!', 'I am not sure', 'No idea…'):
            with patch.object(self.module.engine, 'aggregate') as aggregate:
                result, count = self.module.consensus(question, [{'value': item} for item in ('Pasta', 'Pasta', value)])
            self.assertIsNone(result, value)
            self.assertEqual(count, 2, value)
            aggregate.assert_not_called()

    def test_opposite_polarities_do_not_share_questions_or_facts(self):
        owner = self.student()
        suffix = uuid.uuid4().hex
        opened = self.ask(owner, f'Is the library open now for campus event {suffix}?')
        closed = self.ask(self.student(), f'Is the library closed now for campus event {suffix}?')
        self.assertNotEqual(opened['crowd_question_id'], closed['crowd_question_id'])
        for _ in range(3):
            self.vote(self.student(), opened['crowd_question_id'])
        closed_again = self.ask(self.student(), closed['text'])
        self.assertEqual(closed_again['status'], 'pending')
        self.assertEqual(closed_again['crowd_question_id'], closed['crowd_question_id'])

    def test_legacy_rows_are_preserved_and_cannot_be_claimed_by_name(self):
        legacy_path = str(Path(self.temporary.name) / 'legacy.db')
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            for table in ('queries', 'facts', 'crowd_questions', 'crowd_answers', 'notifications'):
                connection.execute(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY, user TEXT, content TEXT)')
                connection.execute(f'INSERT INTO {table} VALUES (1,?,?)', ('old_name', 'private historical content'))
        environment = dict(os.environ, DB_PATH=legacy_path)
        script = "from database import init_db; init_db(); init_db()"
        subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=environment, check=True, capture_output=True, text=True)
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            for table in ('queries', 'facts', 'crowd_questions', 'crowd_answers', 'notifications'):
                self.assertEqual(connection.execute(f'SELECT user,content FROM legacy_{table}').fetchall(), [('old_name', 'private historical content')])
                self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()

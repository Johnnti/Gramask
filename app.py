"""Authenticated, Gemma-orchestrated campus questions and crowd consensus."""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import Counter
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator
from config import settings
from database import db, init_db
from llm import EngineUnavailable, STATIC_FACTS, engine, evidence_key
from security import hash_password, limiter, token_hash, verify_password

HERE = Path(__file__).resolve().parent
COOKIE = 'gramask_session'
logger = logging.getLogger('gramask')
_question_locks = [threading.Lock() for _ in range(128)]
_dummy_password_hash = hash_password(secrets.token_urlsafe(24))


def format_fact(fact):
    if 'report_summary' in fact.keys() and fact['report_summary']:
        return format_report_summary(json.loads(fact['report_summary']))
    return (f'Students were asked “{fact["question"]}” — the crowd says: {fact["value"]} '
            f'({fact["n_votes"]} reports, {round(fact["confidence"] * 100)}% agreement). '
            'This is a student report, not an official university announcement.')


def query_object(row):
    result = dict(row)
    result['is_demo'] = result.pop('demo_owner_id', None) is not None
    result.pop('user_id', None)
    if result.get('classification'):
        result['classification'] = json.loads(result['classification'])
    if result.get('report_summary'):
        result['report_summary'] = json.loads(result['report_summary'])
    return result


def public_user(row):
    return {**{key: row[key] for key in ('id', 'display_name', 'username', 'created_at')},
            'is_demo_student': row['demo_owner_id'] is not None and not row['is_simulated']}


def session_cookie(request):
    """A tab can select its own HttpOnly cookie without receiving its token."""
    slot = request.headers.get('x-gramask-session')
    if slot is None:
        return COOKIE
    if not re.fullmatch(r'[a-f0-9]{32}', slot):
        raise HTTPException(400, 'Invalid account session')
    return f'{COOKIE}_{slot}'


def question_key(text, info):
    # Exact normalized input preserves polarity: "closed?" never reuses "open?" votes.
    normalized = ' '.join(unicodedata.normalize('NFKC', text).casefold().split()).rstrip(' ?.!')
    identity = json.dumps([info['entity'], info['attribute'], info['answer_type'], normalized])
    return hashlib.sha256(identity.encode()).hexdigest()


def is_usable(value):
    normalized = ' '.join(value.casefold().replace('’', "'").split()).strip(' \t.!?…')
    return normalized not in {
        '', 'not sure', "i'm not sure", 'i am not sure', 'unsure', 'uncertain', 'unknown',
        "i don't know", 'i do not know', "don't know", 'do not know', 'idk', 'n/a',
        'no idea', 'i have no idea', "i can't confirm", 'i cannot confirm',
    }


def demo_report_summary(question, answers):
    """Report every response, including uncertainty and disagreements."""
    total = len(answers)
    if total < settings.quorum and time.time() < question['closes_at']:
        return None, total
    groups = {}
    if question['answer_type'] == 'yes_no':
        groups = {value.casefold(): {'value': value, 'count': 0} for value in ('Yes', 'No', 'Not sure')}
    for answer in answers:
        value = answer['value'] if is_usable(answer['value']) else 'Not sure'
        key = evidence_key(value)
        groups.setdefault(key, {'value': value, 'count': 0})['count'] += 1
    breakdown = [{**item, 'share': item['count'] / total if total else 0} for item in groups.values()]
    summary = {'total': total, 'expected': settings.quorum, 'answers': breakdown,
               'closed_by': 'responses' if total >= settings.quorum else 'deadline'}
    return {'value': format_report_summary(summary), 'agreement': max((i['share'] for i in breakdown), default=0),
            'engine': 'demo', 'report_summary': summary}, total


def format_report_summary(summary):
    total = summary['total']
    if not total:
        return 'No answers received before the deadline.'
    opening = (f"All {total} students responded." if summary['closed_by'] == 'responses'
               else f"Time is up. Received {total} of {summary['expected']} responses.")
    largest = max(item['count'] for item in summary['answers'])
    leaders = [item['value'] for item in summary['answers'] if item['count'] == largest]
    leading = f"Most reported: {leaders[0]}." if len(leaders) == 1 else 'No single answer leads; responses are tied.'
    shares = '; '.join(f"{item['value']}: {item['count']}/{total} ({item['share']:.1%})"
                       for item in summary['answers'])
    return f'{opening} {leading} Share of responses: {shares}.'


def consensus(question, answers):
    if settings.broadcast_questions:
        return demo_report_summary(question, answers)
    values = [answer['value'] for answer in answers if is_usable(answer['value'])]
    if len(values) < settings.quorum:
        return None, len(values)
    if question['answer_type'] == 'yes_no':
        values = [value for value in values if value in ('Yes', 'No')]
        if len(values) < settings.quorum:
            return None, len(values)
        counts = Counter(values).most_common()
        if len(counts) > 1 and counts[0][1] == counts[1][1]:
            return None, len(values)
        value, count = counts[0]
        result = {'value': value, 'agreement': count / len(values), 'engine': question['engine']}
    elif question['demo_owner_id'] is not None:
        value, count = Counter(values).most_common(1)[0]
        result = {'value': value, 'agreement': count / len(values), 'engine': 'demo'}
    else:
        try:
            result = engine.aggregate(question['prompt'], values)
        except EngineUnavailable:
            logger.warning('Gemma aggregation unavailable for question %s', question['id'])
            return None, len(values)
        if result:
            indices = result.get('supporting_indices', [])
            valid = {i for i in indices if type(i) is int and 0 <= i < len(values)}
            result = {**result, 'agreement': len(valid) / len(values)}
    if not result or not result.get('value') or result['agreement'] < settings.agreement:
        return None, len(values)
    return result, len(values)


def close_question(connection, question, result, n_votes, now):
    """Caller holds a write transaction and has verified the answer snapshot."""
    changed = connection.execute("UPDATE crowd_questions SET status='closed' WHERE id=? AND status='open'", (question['id'],)).rowcount
    if not changed:
        return
    confidence = valid_until = None
    result_engine = question['engine']
    summary = result.get('report_summary') if result else None
    summary_json = json.dumps(summary) if summary is not None else None
    if summary is not None and not n_votes:
        answer, status, source = format_report_summary(summary), 'unanswered', 'no_answers'
    elif result:
        confidence = result['agreement']
        ttl = settings.fact_ttl_hours * 3600
        if 'menu' in question['attribute']:
            ttl = min(ttl, 3 * 3600)
        valid_until = now + ttl
        result_engine = result.get('engine', question['engine'])
        cursor = connection.execute('''INSERT INTO facts(entity,attribute,question,question_key,answer_type,category,value,
            confidence,n_votes,created_at,valid_until,crowd_question_id,demo_owner_id,engine,report_summary)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                question['entity'], question['attribute'], question['prompt'], question['question_key'],
                question['answer_type'], question['category'], result['value'], confidence,
                n_votes, now, valid_until, question['id'], question['demo_owner_id'], result_engine, summary_json))
        fact = connection.execute('SELECT * FROM facts WHERE id=?', (cursor.lastrowid,)).fetchone()
        answer, status, source = format_fact(fact), 'answered', 'crowd'
    else:
        answer = "There weren't enough agreeing student reports before this question expired. You can ask again for a new update."
        status, source = 'unanswered', 'unanswered'
    subscribers = connection.execute("SELECT user_id, MAX(id) AS query_id FROM queries WHERE crowd_question_id=? AND status='pending' GROUP BY user_id", (question['id'],)).fetchall()
    connection.execute('''UPDATE queries SET status=?,answer=?,answered_at=?,source=?,confidence=?,
        n_votes=?,valid_until=?,engine=?,report_summary=? WHERE crowd_question_id=? AND status='pending' ''',
        (status, answer, now, source, confidence, n_votes, valid_until, result_engine, summary_json, question['id']))
    for subscriber in subscribers:
        connection.execute('''INSERT INTO notifications(user_id,title,body,kind,created_at,query_id,crowd_question_id,demo_owner_id)
            VALUES (?,?,?,?,?,?,?,?)''', (subscriber['user_id'],
            ('No answers received' if source == 'no_answers' else 'Your campus answer is ready' if result else 'Your question needs more reports'), answer,
            'question_answered' if status == 'answered' else 'question_unanswered', now, subscriber['query_id'], question['id'], question['demo_owner_id']))


def try_close(question_id, force=False):
    # Bounded striped locks serialize each question. Network calls run without a DB transaction.
    with _question_locks[question_id % len(_question_locks)]:
        for _ in range(3):
            with db() as connection:
                row = connection.execute("SELECT * FROM crowd_questions WHERE id=? AND status='open'", (question_id,)).fetchone()
                if row is None:
                    return
                question = dict(row)
                answers = [dict(row) for row in connection.execute('SELECT * FROM crowd_answers WHERE question_id=? ORDER BY id', (question_id,))]
                connection.execute('UPDATE crowd_questions SET next_attempt_at=? WHERE id=?', (time.time() + 30, question_id))
            result, n_votes = consensus(question, answers)
            expired = time.time() >= question['closes_at']
            if not result and not expired:
                return
            with db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                current = connection.execute('SELECT status FROM crowd_questions WHERE id=?', (question_id,)).fetchone()
                if not current or current['status'] != 'open':
                    return
                ids = [row[0] for row in connection.execute('SELECT id FROM crowd_answers WHERE question_id=? ORDER BY id', (question_id,))]
                if ids != [answer['id'] for answer in answers]:
                    continue
                close_question(connection, question, result, n_votes, time.time())
                return


def sweep():
    with db() as connection:
        now = time.time()
        ids = [row[0] for row in connection.execute('''SELECT cq.id FROM crowd_questions cq WHERE cq.status='open'
            AND (cq.closes_at<=? OR ((cq.answer_type='text' OR ?) AND cq.next_attempt_at<=?
                AND (SELECT COUNT(*) FROM crowd_answers a WHERE a.question_id=cq.id)>=?))
            ORDER BY cq.closes_at LIMIT 100''', (now, settings.broadcast_questions, now, settings.quorum))]
        connection.execute('DELETE FROM sessions WHERE expires_at<=?', (time.time(),))
    for question_id in ids:
        try:
            try_close(question_id, force=True)
        except Exception:
            logger.exception('Failed to finalize expired question %s', question_id)


async def sweep_worker():
    while True:
        try:
            await asyncio.to_thread(sweep)
        except Exception:
            logger.exception('Expiry worker failed; retrying')
        await asyncio.sleep(1 if settings.broadcast_questions else 5)


@asynccontextmanager
async def lifespan(application):
    init_db()
    if settings.broadcast_questions:
        with db() as connection:
            connection.execute("UPDATE crowd_questions SET closes_at=MIN(closes_at,created_at+?) WHERE status='open'",
                               (settings.timeout_min * 60,))
    worker = asyncio.create_task(sweep_worker())
    yield
    worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker


app = FastAPI(title='GramAsk', version='1.0.0', lifespan=lifespan, docs_url=None, redoc_url=None)


class RequestBodyLimit:
    """Bound real body bytes, including chunked requests without Content-Length."""
    def __init__(self, app, maximum=16_384):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > self.maximum:
                response = JSONResponse({'detail': 'Request body is too large'}, status_code=413)
                return await response(scope, receive, send)
            chunks.append(chunk)
            if not message.get('more_body', False):
                break
        replayed = False
        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()
        return await self.app(scope, replay, send)


app.add_middleware(RequestBodyLimit)
if settings.app_env == 'production':
    app.add_middleware(TrustedHostMiddleware,
                      allowed_hosts=[urlsplit(settings.app_origin).hostname, '127.0.0.1', 'localhost'])


def normalize_origin(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            return None
        return f'{parsed.scheme.lower()}://{parsed.netloc.lower()}'
    except ValueError:
        return None


@app.middleware('http')
async def protect_requests(request, call_next):
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        try:
            length = int(request.headers.get('content-length', '0'))
        except ValueError:
            return JSONResponse({'detail': 'Invalid request length'}, status_code=400)
        if length > 16_384:
            return JSONResponse({'detail': 'Request body is too large'}, status_code=413)
        allowed = normalize_origin(settings.app_origin or str(request.base_url))
        origin = request.headers.get('origin')
        if (origin is not None and normalize_origin(origin) != allowed) or request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': 'This request came from an untrusted origin'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if settings.secure_cookies:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    if request.url.path.startswith('/api/') or request.url.path in ('/healthz', '/readyz'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def optional_session(request: Request):
    token = request.cookies.get(session_cookie(request))
    if not token or len(token) > 256:
        return None
    with db() as connection:
        row = connection.execute('''SELECT users.*, sessions.csrf_token FROM sessions JOIN users ON users.id=sessions.user_id
            WHERE sessions.token_hash=? AND sessions.expires_at>? AND users.is_simulated=0''', (token_hash(token), time.time())).fetchone()
    if row and row['demo_owner_id'] is not None and not settings.demo_mode:
        return None
    return dict(row) if row else None


def current_user(user=Depends(optional_session)):
    if not user:
        raise HTTPException(401, 'Sign in to continue')
    return user


def mutation_user(request: Request, user=Depends(current_user)):
    csrf = request.headers.get('x-csrf-token', '')
    if not hmac.compare_digest(csrf.encode(), user['csrf_token'].encode()):
        raise HTTPException(403, 'Your session could not be verified. Refresh and try again.')
    return user


class InputModel(BaseModel):
    model_config = ConfigDict(extra='forbid')

class RegisterIn(InputModel):
    display_name: str = Field(min_length=1, max_length=60)
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=10, max_length=256)

class LoginIn(InputModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)

class AskIn(InputModel):
    text: str = Field(min_length=3, max_length=1000)

class AnswerIn(InputModel):
    question_id: int = Field(gt=0)
    value: str | None = Field(default=None, min_length=1, max_length=500)
    endorse_answer_id: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def one_response(self):
        if (self.value is None) == (self.endorse_answer_id is None):
            raise ValueError('Enter a response or endorse an existing answer')
        return self


def start_session(connection, response, user, cookie=COOKIE):
    token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), time.time()
    connection.execute('INSERT INTO sessions(token_hash,user_id,csrf_token,created_at,expires_at) VALUES (?,?,?,?,?)',
        (token_hash(token), user['id'], csrf, now, now + settings.session_days * 86400))
    response.set_cookie(cookie, token, max_age=settings.session_days * 86400, httponly=True,
        secure=settings.secure_cookies, samesite='lax', path='/')
    return {'user': public_user(user), 'csrf_token': csrf}


def auth_rate(request):
    limiter.check(('auth', request.client.host if request.client else 'unknown'), settings.auth_rate_limit)


@app.post('/api/auth/register', status_code=201)
def register(body: RegisterIn, request: Request, response: Response):
    auth_rate(request)
    username, display_name = body.username.strip().lower(), body.display_name.strip()
    if not re.fullmatch(r'[a-z0-9_]{3,32}', username):
        raise HTTPException(422, 'Username must be 3–32 letters, numbers, or underscores')
    if not display_name or any(unicodedata.category(char).startswith('C') for char in display_name):
        raise HTTPException(422, 'Enter a valid display name')
    password_hash = hash_password(body.password)
    with db() as connection:
        try:
            cursor = connection.execute('INSERT INTO users(username,display_name,password_hash,created_at) VALUES (?,?,?,?)', (username, display_name, password_hash, time.time()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'That username is already taken')
        user = connection.execute('SELECT * FROM users WHERE id=?', (cursor.lastrowid,)).fetchone()
        return start_session(connection, response, user, session_cookie(request))


@app.post('/api/auth/login')
def login(body: LoginIn, request: Request, response: Response):
    auth_rate(request)
    with db() as connection:
        user = connection.execute('SELECT * FROM users WHERE username=? AND is_simulated=0', (body.username.strip().lower(),)).fetchone()
    valid = verify_password(body.password, user['password_hash'] if user else _dummy_password_hash)
    if not user or not valid:
        raise HTTPException(401, 'Incorrect username or password')
    with db() as connection:
        old = request.cookies.get(session_cookie(request))
        if old:
            connection.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash(old),))
        return start_session(connection, response, user, session_cookie(request))


@app.get('/api/auth/me')
def me(user=Depends(current_user)):
    return {'user': public_user(user), 'csrf_token': user['csrf_token']}


@app.post('/api/auth/logout')
def logout(request: Request, response: Response, user=Depends(mutation_user)):
    with db() as connection:
        connection.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash(request.cookies.get(session_cookie(request), '')),))
    response.delete_cookie(session_cookie(request), path='/', secure=settings.secure_cookies, httponly=True, samesite='lax')
    return {'ok': True}


def create_query(user, text, demo=False):
    broadcast = not demo and (settings.broadcast_questions or
                (not settings.demo_mode and settings.question_routing == 'community'))
    now = time.time()
    with db() as connection:
        known = [row[0] for row in connection.execute('SELECT DISTINCT entity FROM crowd_questions WHERE demo_owner_id IS NULL UNION SELECT DISTINCT entity FROM facts WHERE demo_owner_id IS NULL')]
    if demo:
        info = {'in_scope': True, 'kind': 'dynamic', 'entity': 'library', 'attribute': 'open_status',
            'answer_type': 'yes_no', 'crowd_prompt': text, 'static_answer': None, 'fact_id': None,
            'category': 'library', 'engine': 'demo'}
    else:
        try:
            info = engine.classify(text, known_entities=known)
        except EngineUnavailable as error:
            raise HTTPException(503, 'Gemma 3 is unavailable. Please try again shortly.') from error
    if broadcast:
        # Live presentation mode: use routing only to choose the input type.
        # Every submission gets its own crowd question, even a cached repeat.
        info.update(in_scope=True, kind='dynamic', static_answer=None, fact_id=None, crowd_prompt=text)
    owner = user['id'] if demo else None
    key = question_key(text, info)
    now = time.time()
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        def finish(answer, status='answered', source='knowledge', cq_id=None, fact=None, source_url=None):
            cursor = connection.execute('''INSERT INTO queries(user_id,text,status,answer,crowd_question_id,created_at,answered_at,
                source,classification,confidence,n_votes,valid_until,source_url,engine,demo_owner_id,report_summary)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (user['id'], text, status, answer, cq_id, now,
                now if status != 'pending' else None, source, json.dumps(info),
                fact['confidence'] if fact else None, fact['n_votes'] if fact else None,
                fact['valid_until'] if fact else None, source_url, fact['engine'] if fact else info['engine'], owner,
                fact['report_summary'] if fact else None))
            return query_object(connection.execute('''SELECT q.*,cq.closes_at FROM queries q
                LEFT JOIN crowd_questions cq ON cq.id=q.crowd_question_id WHERE q.id=?''', (cursor.lastrowid,)).fetchone())
        if not info['in_scope'] and settings.demo_mode:
            return finish('I can help with questions about Grambling State University and campus life. Try asking a campus question.', source='out_of_scope')
        if info['kind'] == 'static' and info.get('static_answer'):
            fact = next((fact for fact in STATIC_FACTS if fact.get('id') == info.get('fact_id')), None)
            if fact:
                return finish(info['static_answer'], source_url=fact.get('source_url'))
        fact = connection.execute('SELECT * FROM facts WHERE question_key=? AND demo_owner_id IS ? AND valid_until>? ORDER BY created_at DESC LIMIT 1', (key, owner, now)).fetchone()
        if fact and not broadcast:
            return finish(format_fact(fact), source='crowd', fact=fact)
        question = connection.execute("SELECT * FROM crowd_questions WHERE question_key=? AND demo_owner_id IS ? AND status='open' AND closes_at>? ORDER BY id LIMIT 1", (key, owner, now)).fetchone()
        if question is None or broadcast:
            cursor = connection.execute('''INSERT INTO crowd_questions(entity,attribute,prompt,answer_type,category,question_key,status,
                asked_by,created_at,closes_at,demo_owner_id,engine) VALUES (?,?,?,?,?,?,'open',?,?,?,?,?)''', (
                info['entity'], info['attribute'], info['crowd_prompt'], info['answer_type'], info['category'], key,
                user['id'], now, now + settings.timeout_min * 60, owner, info['engine']))
            cq_id = cursor.lastrowid
            if not demo:
                connection.execute('''INSERT INTO notifications(user_id,title,body,kind,created_at,crowd_question_id)
                    SELECT id,?,?,?, ?,? FROM users WHERE id!=? AND is_simulated=0''',
                    ('A student could use your help', info['crowd_prompt'], 'crowd_question', now, cq_id, user['id']))
        else:
            cq_id = question['id']
            # A former contributor now asking this question becomes a subscriber;
            # their earlier vote must not help resolve their own request.
            connection.execute('DELETE FROM crowd_answers WHERE question_id=? AND user_id=?', (cq_id, user['id']))
            connection.execute("DELETE FROM notifications WHERE user_id=? AND crowd_question_id=? AND kind='crowd_question'", (user['id'], cq_id))
        return finish("I've asked the campus community. Your answer will appear here when enough student reports agree.",
            status='pending', source='pending', cq_id=cq_id)


@app.post('/api/ask')
def ask(body: AskIn, user=Depends(mutation_user)):
    text = body.text.strip()
    if len(text) < 3:
        raise HTTPException(422, 'Please enter a question of at least three characters')
    limiter.check(('ask', user['id']), settings.ask_rate_limit)
    return create_query(user, text)


@app.get('/api/queries')
def queries(user=Depends(current_user)):
    with db() as connection:
        return [query_object(row) for row in connection.execute('''SELECT q.*,cq.closes_at
            FROM queries q LEFT JOIN crowd_questions cq ON cq.id=q.crowd_question_id
            WHERE q.user_id=? ORDER BY q.created_at DESC LIMIT 200''', (user['id'],))]


@app.get('/api/queries/{query_id}')
def query(query_id: int, user=Depends(current_user)):
    with db() as connection:
        row = connection.execute('''SELECT q.*,cq.closes_at FROM queries q
            LEFT JOIN crowd_questions cq ON cq.id=q.crowd_question_id
            WHERE q.id=? AND q.user_id=?''', (query_id, user['id'])).fetchone()
    if not row:
        raise HTTPException(404, 'Question not found')
    return query_object(row)


@app.get('/api/inbox')
def inbox(user=Depends(current_user)):
    parent = user['demo_owner_id']
    with db() as connection:
        rows = connection.execute('''SELECT cq.id,cq.prompt,cq.entity,cq.attribute,cq.category,cq.answer_type,cq.status,cq.created_at,cq.closes_at,cq.demo_owner_id,
            (SELECT json_group_array(json_object('id',a.id,'value',a.value)) FROM
                (SELECT id,value FROM crowd_answers WHERE question_id=cq.id ORDER BY id) a) AS answer_reports
            FROM crowd_questions cq WHERE cq.status='open' AND cq.closes_at>?
            AND (cq.demo_owner_id IS NULL OR cq.demo_owner_id=?)
            AND (? OR ? IS NULL OR cq.asked_by=?) AND cq.asked_by!=?
            AND NOT EXISTS(SELECT 1 FROM queries q WHERE q.crowd_question_id=cq.id AND q.user_id=?)
            AND NOT EXISTS(SELECT 1 FROM crowd_answers a WHERE a.question_id=cq.id AND a.user_id=?)
            ORDER BY cq.created_at DESC LIMIT 200''', (time.time(), parent, settings.broadcast_questions, parent, parent, user['id'], user['id'], user['id'])).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        reports = json.loads(item.pop('answer_reports'))
        values = [report['value'] for report in reports]
        item['n_answers'] = len(values) if settings.broadcast_questions else sum(is_usable(value) for value in values)
        options = {}
        if item['answer_type'] == 'text':
            for report in reports:
                key = evidence_key(report['value'])
                option = options.setdefault(key, {'id': report['id'], 'value': report['value'], 'count': 0})
                option['count'] += 1
        item['answer_options'] = list(options.values())
        is_demo = item.pop('demo_owner_id') is not None
        result.append({**item, 'quorum': settings.quorum, 'is_demo': is_demo})
    return result


@app.post('/api/answer')
def answer(body: AnswerIn, user=Depends(mutation_user)):
    limiter.check(('answer', user['id']), 60)
    value = body.value.strip() if body.value is not None else None
    if value == '':
        raise HTTPException(422, 'Please enter an answer')
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        parent = user['demo_owner_id']
        question = connection.execute('''SELECT * FROM crowd_questions WHERE id=?
            AND (demo_owner_id IS NULL OR demo_owner_id=?) AND (? OR ? IS NULL OR asked_by=?)''',
            (body.question_id, parent, settings.broadcast_questions, parent, parent)).fetchone()
        if not question:
            raise HTTPException(404, 'Question not found')
        if question['status'] != 'open' or question['closes_at'] <= time.time():
            raise HTTPException(409, 'This question has closed')
        if question['asked_by'] == user['id'] or connection.execute('SELECT 1 FROM queries WHERE crowd_question_id=? AND user_id=?', (body.question_id, user['id'])).fetchone():
            raise HTTPException(403, 'You cannot answer a question you asked or subscribed to')
        if body.endorse_answer_id is not None:
            if question['answer_type'] != 'text':
                raise HTTPException(422, 'Endorsements are available for written questions')
            original = connection.execute('SELECT value FROM crowd_answers WHERE id=? AND question_id=?',
                                          (body.endorse_answer_id, body.question_id)).fetchone()
            if not original:
                raise HTTPException(404, 'This answer is not available for this question')
            # An endorsement is one student's report of the original text. The
            # existing unique constraint and closing rules apply to both paths.
            value = original['value']
        if question['answer_type'] == 'yes_no':
            canonical = {'yes': 'Yes', 'no': 'No', 'not sure': 'Not sure'}
            if value.casefold() not in canonical:
                raise HTTPException(422, 'Choose Yes, No, or Not sure')
            value = canonical[value.casefold()]
        try:
            connection.execute('INSERT INTO crowd_answers(question_id,user_id,value,created_at) VALUES (?,?,?,?)', (body.question_id, user['id'], value, time.time()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'You already answered this question')
    # Real-model text grouping can take minutes on CPU. The durable sweep
    # retries these saved reports after a restart and never delays the voter.
    if question['answer_type'] == 'yes_no' or settings.demo_mode:
        try_close(body.question_id)
    return {'ok': True}


@app.get('/api/contributions')
def contributions(user=Depends(current_user)):
    with db() as connection:
        return [dict(row) for row in connection.execute('''SELECT a.id,a.question_id,cq.prompt,a.value,a.created_at,cq.status
            FROM crowd_answers a JOIN crowd_questions cq ON cq.id=a.question_id
            WHERE a.user_id=? AND (cq.demo_owner_id IS NULL OR cq.demo_owner_id=?)
            ORDER BY a.created_at DESC LIMIT 200''', (user['id'], user['demo_owner_id']))]


@app.get('/api/stats')
def stats(user=Depends(current_user)):
    with db() as connection:
        return {
            'open_questions': connection.execute("SELECT COUNT(*) FROM crowd_questions WHERE status='open' AND closes_at>? AND demo_owner_id IS NULL", (time.time(),)).fetchone()[0],
            'fresh_answers': connection.execute('SELECT COUNT(*) FROM facts WHERE valid_until>? AND demo_owner_id IS NULL', (time.time(),)).fetchone()[0],
            'students': connection.execute('SELECT COUNT(*) FROM users WHERE is_simulated=0').fetchone()[0],
            'my_contributions': connection.execute('SELECT COUNT(*) FROM crowd_answers a JOIN crowd_questions cq ON cq.id=a.question_id WHERE a.user_id=? AND (cq.demo_owner_id IS NULL OR cq.demo_owner_id=?)', (user['id'], user['demo_owner_id'])).fetchone()[0],
            'my_questions': connection.execute('SELECT COUNT(*) FROM queries WHERE user_id=? AND demo_owner_id IS NULL', (user['id'],)).fetchone()[0],
        }


@app.get('/api/facts')
def facts(user=Depends(optional_session)):
    live = []
    if user:
        with db() as connection:
            for row in connection.execute('SELECT * FROM facts WHERE valid_until>? AND (demo_owner_id IS NULL OR demo_owner_id=?) ORDER BY created_at DESC LIMIT 200', (time.time(), user['demo_owner_id'] or user['id'])):
                item = dict(row)
                item['is_demo'] = item.pop('demo_owner_id') is not None
                item.pop('question_key', None)
                item['text'] = format_fact(row)
                if item.get('report_summary'):
                    item['report_summary'] = json.loads(item['report_summary'])
                live.append(item)
    return {'static': STATIC_FACTS, 'live': live}


@app.get('/api/notifications')
def notifications(user=Depends(current_user)):
    with db() as connection:
        return [{**dict(row), 'read': bool(row['read'])} for row in connection.execute('SELECT id,title,body,kind,read,created_at,query_id,crowd_question_id FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100', (user['id'],))]


@app.post('/api/notifications/read')
def read_notifications(user=Depends(mutation_user)):
    with db() as connection:
        connection.execute('UPDATE notifications SET read=1 WHERE user_id=?', (user['id'],))
    return {'ok': True}


def require_demo():
    if not settings.demo_mode:
        raise HTTPException(404, 'Demo mode is disabled')


def student_accounts(connection, user):
    accounts = []
    for number in range(1, 4):
        username = f"~participant_{user['id']}_{number}"
        student = connection.execute('SELECT * FROM users WHERE username=? AND demo_owner_id=? AND is_simulated=0',
                                     (username, user['id'])).fetchone()
        if student:
            slot = hashlib.sha256(f'{settings.app_origin}:{username}'.encode()).hexdigest()[:32]
            accounts.append({'number': number, 'id': student['id'], 'name': student['display_name'],
                             'slot': slot, 'url': f'/?session={slot}#community'})
    return accounts


@app.get('/api/demo/students', dependencies=[Depends(require_demo)])
def demo_students(user=Depends(current_user)):
    with db() as connection:
        return student_accounts(connection, user) if user['demo_owner_id'] is None else []


@app.post('/api/demo/students', dependencies=[Depends(require_demo)])
def prepare_demo_students(response: Response, user=Depends(mutation_user)):
    if user['demo_owner_id'] is not None:
        raise HTTPException(403, 'Open student accounts from the account that asks the question')
    limiter.check(('demo_students', user['id']), 5)
    password_hash = hash_password(secrets.token_urlsafe(32))
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        for number in range(1, 4):
            connection.execute('''INSERT OR IGNORE INTO users(username,display_name,password_hash,created_at,demo_owner_id)
                VALUES (?,?,?,?,?)''', (f"~participant_{user['id']}_{number}", f'Student {number}', password_hash, time.time(), user['id']))
        accounts = student_accounts(connection, user)
        for account in accounts:
            student = connection.execute('SELECT * FROM users WHERE id=?', (account['id'],)).fetchone()
            connection.execute('DELETE FROM sessions WHERE user_id=?', (student['id'],))
            start_session(connection, response, student, f"{COOKIE}_{account['slot']}")
    return accounts


@app.post('/api/demo/seed-question', dependencies=[Depends(require_demo)])
def demo_seed(user=Depends(mutation_user)):
    limiter.check(('ask', user['id']), settings.ask_rate_limit)
    return create_query(user, 'Is the library open right now?', demo=True)


@app.post('/api/demo/auto-answer', dependencies=[Depends(require_demo)])
def demo_auto_answer(question_id: int, user=Depends(mutation_user)):
    submitted = 0
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        question = connection.execute('SELECT * FROM crowd_questions WHERE id=? AND demo_owner_id=?', (question_id, user['id'])).fetchone()
        if not question:
            raise HTTPException(404, 'Your demo question was not found')
        if question['status'] != 'open' or question['closes_at'] <= time.time():
            raise HTTPException(409, 'This demo question has closed')
        for index in range(settings.quorum):
            name = f"~demo_{user['id']}_{index}"
            connection.execute('INSERT OR IGNORE INTO users(username,display_name,created_at,is_simulated,demo_owner_id) VALUES (?,?,?,1,?)', (name, f'Simulated student {index + 1}', time.time(), user['id']))
            voter_id = connection.execute('SELECT id FROM users WHERE username=?', (name,)).fetchone()[0]
            submitted += connection.execute('INSERT OR IGNORE INTO crowd_answers(question_id,user_id,value,created_at) VALUES (?,?,?,?)', (question_id, voter_id, 'Yes', time.time())).rowcount
    try_close(question_id)
    return {'ok': True, 'submitted': submitted}


@app.post('/api/demo/reset', dependencies=[Depends(require_demo)])
def demo_reset(user=Depends(mutation_user)):
    with db() as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('DELETE FROM crowd_questions WHERE demo_owner_id=?', (user['id'],))
        connection.execute('DELETE FROM queries WHERE demo_owner_id=?', (user['id'],))
        connection.execute('DELETE FROM notifications WHERE demo_owner_id=?', (user['id'],))
        connection.execute('DELETE FROM users WHERE is_simulated=1 AND demo_owner_id=?', (user['id'],))
    return {'ok': True, 'message': 'Your demonstration data was reset'}


@app.get('/api/config')
def config():
    return {'quorum': settings.quorum, 'agreement': settings.agreement, 'timeout_min': settings.timeout_min,
        'fact_ttl_hours': settings.fact_ttl_hours, 'demo_mode': settings.demo_mode,
        'question_routing': settings.question_routing, 'report_all': settings.broadcast_questions,
        'server_time': time.time(),
        'engine': engine.status(), 'llm': settings.llm_model}


@app.get('/healthz')
def health():
    try:
        with db() as connection:
            connection.execute('SELECT COUNT(*) FROM users').fetchone()
    except sqlite3.Error:
        return JSONResponse({'status': 'unhealthy', 'database': 'unavailable'}, status_code=503)
    return {'status': 'ok', 'database': 'available'}


@app.get('/readyz')
def ready():
    database_status = health()
    if isinstance(database_status, JSONResponse):
        return database_status
    status = engine.check_health()
    available = status.get('status') in ('available', 'demo', 'ok', 'ready')
    return JSONResponse({'status': 'ready' if available else 'not_ready', 'database': 'available', 'engine': status}, status_code=200 if available else 503)


@app.get('/')
def index():
    return FileResponse(HERE / 'index.html', headers={'Cache-Control': 'no-cache'})


@app.get('/sw.js')
def service_worker():
    return FileResponse(HERE / 'static' / 'sw.js', media_type='application/javascript', headers={'Service-Worker-Allowed': '/', 'Cache-Control': 'no-cache'})


@app.get('/manifest.webmanifest')
def manifest():
    return FileResponse(HERE / 'static' / 'manifest.webmanifest', media_type='application/manifest+json')


app.mount('/static', StaticFiles(directory=HERE / 'static', check_dir=False), name='static')

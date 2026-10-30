"""Password hashing and bounded in-process request throttling."""
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from fastapi import HTTPException
ITERATIONS = 600_000

def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}'

def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, digest = encoded.split('$')
        if algorithm != 'pbkdf2_sha256':
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, digest)
    except (AttributeError, ValueError, TypeError):
        return False

def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()

class RateLimiter:
    def __init__(self):
        self._entries = defaultdict(deque)
        self._lock = threading.Lock()
    def check(self, key, limit, window=60):
        now = time.monotonic()
        with self._lock:
            if len(self._entries) > 10_000:
                self._entries = defaultdict(deque, {k: v for k, v in self._entries.items() if v and v[-1] > now - window})
            values = self._entries[key]
            while values and values[0] <= now - window:
                values.popleft()
            if len(values) >= limit:
                retry = max(1, int(window - (now - values[0])) + 1)
                raise HTTPException(429, 'Too many requests. Please try again shortly.', headers={'Retry-After': str(retry)})
            values.append(now)
limiter = RateLimiter()

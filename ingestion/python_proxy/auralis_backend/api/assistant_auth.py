"""Derive assistant ownership from verified identity, never a caller's scope ID."""
from hashlib import sha256
import os
import re
from threading import BoundedSemaphore, Lock
from contextlib import contextmanager
import time
from urllib.parse import urlsplit
import requests
from fastapi import HTTPException

_verification_slots = BoundedSemaphore(2)
_answer_slots = BoundedSemaphore(2)
_admission_lock = Lock()
_recent_answers = {}


@contextmanager
def assistant_work(scope):
    now = time.monotonic()
    with _admission_lock:
        for key in list(_recent_answers):
            _recent_answers[key] = [at for at in _recent_answers[key] if now-at < 60]
            if not _recent_answers[key]:
                _recent_answers.pop(key)
        recent = _recent_answers.get(scope, [])
        if (len(recent) >= 6 or sum(map(len,_recent_answers.values())) >= 24
                or scope not in _recent_answers and len(_recent_answers) >= 128):
            raise HTTPException(429, detail={'code':'assistant_rate_limited'})
        if not _answer_slots.acquire(blocking=False):
            raise HTTPException(429, detail={'code':'assistant_busy'})
        _recent_answers.setdefault(scope, []).append(now)
    try:
        yield
    finally:
        _answer_slots.release()


def assistant_scope(request, claimed: str = 'guest', *, stateless: bool = False) -> str:
    authorization = request.headers.get('authorization', '')
    if authorization:
        if not authorization.startswith('Bearer ') or len(authorization) > 8192:
            raise HTTPException(401, detail={'code':'assistant_auth_required'})
        base = os.environ.get('SUPABASE_URL', '').rstrip('/')
        key = os.environ.get('SUPABASE_ANON_KEY') or os.environ.get('SUPABASE_PUBLISHABLE_KEY')
        if not key or urlsplit(base).scheme != 'https' or not urlsplit(base).netloc:
            raise HTTPException(503, detail={'code':'assistant_auth_unavailable'})
        if not _verification_slots.acquire(blocking=False):
            raise HTTPException(429, detail={'code':'assistant_busy'})
        try:
            with requests.get(base+'/auth/v1/user', headers={'Authorization':authorization,'apikey':key},
                              timeout=(2,3), allow_redirects=False) as response:
                if response.status_code in (401,403):
                    raise HTTPException(401, detail={'code':'assistant_auth_required'})
                response.raise_for_status()
                owner = response.json().get('id')
                if not isinstance(owner,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',owner):
                    raise ValueError('invalid_user_identity')
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, detail={'code':'assistant_auth_unavailable'}) from None
        finally:
            _verification_slots.release()
        if claimed not in ('guest', owner):
            raise HTTPException(403, detail={'code':'assistant_scope_mismatch'})
        return owner
    if claimed != 'guest' and not claimed.startswith('guest:'):
        raise HTTPException(401, detail={'code':'assistant_auth_required'})
    guest = request.headers.get('x-neatie-guest-key','')
    if re.fullmatch(r'[a-f0-9]{64}', guest):
        return 'guest:'+sha256(guest.encode()).hexdigest()[:48]
    if stateless and not guest:
        # Stateless interpretation cannot read/write saved sessions or memory.
        from uuid import uuid4
        return 'guest:stateless:'+uuid4().hex
    raise HTTPException(401, detail={'code':'assistant_guest_key_required'})

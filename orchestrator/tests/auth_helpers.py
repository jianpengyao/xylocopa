"""Shared setup for auth-ON tests (test_auth_middleware, test_auth_routes).

Auth is enabled against a temp file DB: DISABLE_AUTH unset and every module
that opens its own DB session (main / database / routers.preview) pointed at
it. The password hash uses bcrypt cost 4 so wrong-password loops stay fast;
verify_password accepts any "$2b$" cost.
"""

import hashlib
import json as _json

import bcrypt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import auth
import database
import main
import routers.preview
from auth import set_config
from config import PORT
from models import Base
from tests.asgi_helpers import asgi_request

PASSWORD = "correct horse battery staple"
HOST = ("host", f"localhost:{PORT}")
LAN_IP = "100.64.0.10"


def fast_hash(password: str) -> str:
    derived = hashlib.sha256(password.encode()).hexdigest().encode()
    return bcrypt.hashpw(derived, bcrypt.gensalt(rounds=4)).decode()


def make_auth_db(tmp_path, password: str | None = PASSWORD):
    """(Session factory, engine) for a fresh DB, with a password unless None."""
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    if password is not None:
        db = Session()
        set_config(db, "password_hash", fast_hash(password))
        db.close()
    return Session, engine


def enable_auth(monkeypatch, Session):
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(database, "SessionLocal", Session)
    monkeypatch.setattr(routers.preview, "SessionLocal", Session)
    # In-memory limiter / meter singletons outlive a test; start clean.
    auth.login_limiter._state.clear()
    auth.change_password_limiter._state.clear()
    auth.global_failures.clear()


async def call(method: str, path: str, *, json=None, token: str | None = None,
               ip: str = LAN_IP, headers=(), content_type: str | None = None,
               body: bytes | None = None):
    """One request into main.app → (status, headers, parsed JSON or raw bytes)."""
    hdrs = [HOST, *headers]
    if token is not None:
        hdrs.append(("authorization", f"Bearer {token}"))
    if json is not None:
        body = _json.dumps(json).encode()
        content_type = content_type or "application/json"
    if content_type is not None:
        hdrs.append(("content-type", content_type))
    status, resp_headers, raw = await asgi_request(
        main.app, method, path, hdrs, body or b"", client=(ip, 50000))
    try:
        parsed = _json.loads(raw) if raw else None
    except ValueError:
        parsed = raw
    return status, resp_headers, parsed

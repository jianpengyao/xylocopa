"""Auth routes — login, password setup/change, auth check."""

import logging
import os
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from auth import (
    create_token,
    get_jwt_secret,
    get_password_hash,
    login_limiter,
    needs_rehash,
    rotate_jwt_secret,
    set_password_hash,
    verify_password,
    verify_token,
)
from config import AUTH_TIMEOUT_MINUTES
from database import get_db
from models import Project

logger = logging.getLogger("orchestrator")

_setup_lock = threading.Lock()

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _seed_default_project(db: Session, request: Request) -> None:
    """First-install onboarding: create a starter 'random-things' project so
    new users can dispatch right away without needing to set up a project first.
    Idempotent — skips if a project with that name already exists (active or archived)."""
    from config import PROJECTS_DIR
    name = "random-things"
    if db.get(Project, name):
        return
    projects_dir = PROJECTS_DIR or "/projects"
    proj = Project(
        name=name,
        display_name=name,
        path=os.path.join(projects_dir, name),
        description="Default workspace — rename or move tasks anytime.",
    )
    db.add(proj)
    db.commit()
    wm = getattr(request.app.state, "worker_manager", None)
    if wm:
        try:
            wm.ensure_project_dir(name)
        except Exception as e:
            logger.warning("seed default project: ensure_project_dir failed: %s", e)
    logger.info("Seeded default project '%s' on first-install", name)


@router.post("/check")
async def auth_check(request: Request, db: Session = Depends(get_db)):
    """Check auth state — returns whether password is set and if token is valid."""
    # auth_disabled lets the frontend skip its own password machinery
    # (idle lock, login redirects) when the server does no password check.
    if os.environ.get("DISABLE_AUTH", "").strip() in ("1", "true", "yes"):
        return {"authenticated": True, "needs_setup": False, "auth_disabled": True}
    pw_hash = get_password_hash(db)
    if pw_hash is None:
        return {"authenticated": False, "needs_setup": True, "auth_disabled": False}

    # Password is set — verify the bearer token if provided
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        jwt_secret = get_jwt_secret(db)
        if verify_token(token, jwt_secret):
            return {"authenticated": True, "needs_setup": False, "auth_disabled": False}

    return {"authenticated": False, "needs_setup": False, "auth_disabled": False}


@router.post("/set-password")
async def auth_set_password(request: Request, db: Session = Depends(get_db)):
    """First-time password setup. Only works if no password has been set yet."""
    with _setup_lock:
        pw_hash = get_password_hash(db)
        if pw_hash is not None:
            raise HTTPException(status_code=400, detail="Password already set")

        body = await request.json()
        password = body.get("password", "")
        if len(password) < 4:
            raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

        set_password_hash(db, password)
        try:
            _seed_default_project(db, request)
        except Exception as e:
            logger.warning("seed default project failed: %s", e)
    jwt_secret = get_jwt_secret(db)
    token = create_token(jwt_secret)
    logger.info("Initial password set")
    return {"token": token, "expires_minutes": AUTH_TIMEOUT_MINUTES}


@router.post("/login")
async def auth_login(request: Request, db: Session = Depends(get_db)):
    """Login with password. Returns JWT token. Rate-limited with exponential backoff."""
    ip = request.client.host if request.client else "unknown"

    # Check if this IP is locked out
    locked, remaining = login_limiter.check(ip)
    if locked:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {remaining}s.",
        )

    pw_hash = get_password_hash(db)
    if pw_hash is None:
        raise HTTPException(status_code=400, detail="No password set — use /api/auth/set-password")

    body = await request.json()
    password = body.get("password", "")
    if not verify_password(password, pw_hash):
        now_locked, lock_secs = login_limiter.record_failure(ip)
        detail = "Wrong password"
        if now_locked:
            detail += f". Locked out for {lock_secs}s."
            logger.warning("Login locked for %s after repeated failures (%ds)", ip, lock_secs)
        raise HTTPException(status_code=401, detail=detail)

    login_limiter.record_success(ip)

    # Transparent rehash: migrate legacy SHA-256 to bcrypt on successful login
    if needs_rehash(pw_hash):
        set_password_hash(db, password)
        logger.info("Migrated password hash from SHA-256 to bcrypt")

    jwt_secret = get_jwt_secret(db)
    token = create_token(jwt_secret)
    logger.info("LOGIN_OK from %s (token=%s…)", ip, token[:16])
    return {"token": token, "expires_minutes": AUTH_TIMEOUT_MINUTES}


@router.post("/change-password")
async def auth_change_password(request: Request, db: Session = Depends(get_db)):
    """Change password. Requires current password for verification. Rate-limited."""
    ip = request.client.host if request.client else "unknown"

    # Rate limit — same as login to prevent brute-force via this endpoint
    locked, remaining = login_limiter.check(ip)
    if locked:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {remaining}s.",
        )

    pw_hash = get_password_hash(db)
    if pw_hash is None:
        raise HTTPException(status_code=400, detail="No password set")

    body = await request.json()
    current = body.get("current_password", "")
    new_pw = body.get("new_password", "")

    if not verify_password(current, pw_hash):
        now_locked, lock_secs = login_limiter.record_failure(ip)
        detail = "Current password is wrong"
        if now_locked:
            detail += f". Locked out for {lock_secs}s."
            logger.warning("Change-password locked for %s after repeated failures (%ds)", ip, lock_secs)
        raise HTTPException(status_code=401, detail=detail)

    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")

    login_limiter.record_success(ip)
    set_password_hash(db, new_pw)
    jwt_secret = rotate_jwt_secret(db)
    token = create_token(jwt_secret)
    logger.info("Password changed")
    return {"token": token, "expires_minutes": AUTH_TIMEOUT_MINUTES}

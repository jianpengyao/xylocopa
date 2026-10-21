"""Break-glass password reset for the web UI (works with the server running).

    ./run.sh reset-password [--generate | --password-stdin] [--keep-sessions]
                            [--rotate-secret] [--idle-timeout N|off] [--yes]

Default: prompts twice for the new password (needs a terminal). It then, in
one transaction:
  - stores the new bcrypt hash;
  - unless --keep-sessions, signs every device out (sessions revoked with
    reason "reset") and refuses JWTs issued before now (auth_jwt_not_before);
  - clears login lockouts (auth_limiter_reset_at; the running server honours
    it on the next attempt);
  - optionally sets the idle lock (--idle-timeout) and rotates jwt_secret
    (--rotate-secret, which breaks port-preview cards and open previews).

The server reads all of this per request, so no restart is needed.
Exit codes: 0 ok, 2 usage / validation error, 3 database error.

Imports config, database, auth and auth_store only — never main, so it does
not start the app (or touch tmux) on import.
"""

import argparse
import getpass
import os
import secrets
import sys
import time

import auth
import auth_store
import config

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DB = 3
MIN_PASSWORD_LENGTH = 8


class _UsageError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    choices = ", ".join(str(c) for c in config.AUTH_IDLE_CHOICES if c)
    p = argparse.ArgumentParser(
        prog="./run.sh reset-password",
        description="Reset the web UI password. Signs every device out unless "
                    "--keep-sessions is given. Takes effect immediately.",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--generate", action="store_true",
                     help="generate a random password and print it once")
    src.add_argument("--password-stdin", action="store_true",
                     help="read the new password from the first line of stdin")
    p.add_argument("--keep-sessions", action="store_true",
                   help="keep signed-in devices signed in")
    p.add_argument("--rotate-secret", action="store_true",
                   help="also rotate jwt_secret (port-preview cards and open "
                        "previews must then be re-presented)")
    p.add_argument("--idle-timeout", metavar="N|off",
                   help=f"idle lock in minutes ({choices}) or off")
    p.add_argument("--yes", "-y", action="store_true",
                   help="do not ask for confirmation")
    return p


def _parse_idle(value: str | None) -> int | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v == "off":
        return 0
    try:
        minutes = int(v)
    except ValueError:
        minutes = -1
    if minutes not in config.AUTH_IDLE_CHOICES:
        raise _UsageError(
            f"--idle-timeout must be off or one of {[c for c in config.AUTH_IDLE_CHOICES if c]}")
    return minutes


def _read_password(args, stdin) -> str:
    if args.generate:
        return secrets.token_urlsafe(12)
    if args.password_stdin:
        return stdin.readline().rstrip("\r\n")
    if not stdin.isatty():
        raise _UsageError("no terminal to prompt on; use --password-stdin or --generate")
    password = getpass.getpass("New password: ")
    if getpass.getpass("Repeat new password: ") != password:
        raise _UsageError("passwords do not match")
    return password


def _confirm(db_path: str, args, stdin) -> bool:
    """Ask on an interactive terminal unless --yes / --password-stdin."""
    if args.yes or args.password_stdin or not stdin.isatty():
        return True
    answer = input(f"Reset the web UI password stored in {db_path}? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def _apply(db_path: str, pw_hash: str, args, idle: int | None, now: float) -> int:
    """Write everything in one transaction; returns the number of sessions revoked."""
    import database  # after the DB_PATH check: importing it creates the data dir

    engine = database.engine
    if os.path.realpath(engine.url.database or "") != os.path.realpath(db_path):
        raise RuntimeError(f"database engine points at {engine.url.database}, not {db_path}")
    auth_store.ensure_auth_schema(engine)
    db = database.SessionLocal()
    try:
        auth.store_password_hash(db, pw_hash, commit=False)
        revoked = 0
        if not args.keep_sessions:
            revoked = auth_store.revoke_all(db, reason="reset", now=now, commit=False)
            auth.set_config(db, "auth_jwt_not_before", repr(now), commit=False)
        auth.set_config(db, "auth_limiter_reset_at", repr(now), commit=False)
        if idle is not None:
            auth.set_config(db, "auth_idle_timeout_minutes", idle, commit=False)
        if args.rotate_secret:
            auth.rotate_jwt_secret(db, commit=False)
        db.commit()
        return revoked
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main(argv=None, stdin=None, out=None, err=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:  # --help (0) or an argparse usage error (2)
        return e.code if isinstance(e.code, int) else EXIT_USAGE

    db_path = config.DB_PATH
    print(f"Database: {db_path}", file=out)
    if not os.path.isfile(db_path):
        print(f"error: database not found at {db_path} (check DB_PATH in .env)", file=err)
        return EXIT_DB

    try:
        idle = _parse_idle(args.idle_timeout)
        if not _confirm(db_path, args, stdin):
            print("Aborted; nothing changed.", file=err)
            return EXIT_USAGE
        password = _read_password(args, stdin)
        if len(password) < MIN_PASSWORD_LENGTH:
            raise _UsageError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    except _UsageError as e:
        print(f"error: {e}", file=err)
        return EXIT_USAGE
    except (EOFError, KeyboardInterrupt):
        print("\nAborted; nothing changed.", file=err)
        return EXIT_USAGE

    pw_hash = auth.hash_password(password)
    now = time.time()
    try:
        revoked = _apply(db_path, pw_hash, args, idle, now)
    except Exception as e:  # SQLAlchemyError, sqlite3 errors, engine mismatch
        print(f"error: database update failed, nothing changed: {type(e).__name__}: {e}", file=err)
        return EXIT_DB

    if args.generate:
        print(f"New password: {password}", file=out)
    print("Password updated.", file=out)
    if args.keep_sessions:
        print("Signed-in devices kept (--keep-sessions).", file=out)
    else:
        print(f"Signed out {revoked} device session(s); older tokens are refused.", file=out)
    print("Login lockouts cleared.", file=out)
    if idle is not None:
        print(f"Idle lock: {'off' if idle == 0 else f'{idle} min'}.", file=out)
    if args.rotate_secret:
        print("jwt_secret rotated: port-preview cards and open previews must be "
              "re-presented.", file=out)
    print("Takes effect immediately; no restart needed.", file=out)
    if auth.auth_disabled():
        print("Note: DISABLE_AUTH is set (.env or environment), so the password is not "
              "checked until it is turned off.", file=out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

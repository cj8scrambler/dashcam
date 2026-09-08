"""
Optional username/password gate for the dashcam viewer.

Auth is **active only when at least one user exists** (`enabled()`); a fresh
install or local dev run is wide open, with a loud startup warning. The
deployment path (`app.create_app`) can require it - see DASHCAM_REQUIRE_AUTH.

Credentials live in a single JSON file, `<config dir>/users.json`, mapping
username -> a Werkzeug pbkdf2 hash (never a plaintext password). The file is
re-read on every login attempt, so `adduser` / `deluser` take effect without a
restart. Manage it with the CLI subcommands in app.py:

    python app.py adduser <name>     # prompts for a password
    python app.py deluser <name>
    python app.py listusers

The session cookie is signed (not encrypted) with a secret key. Priority:
$DASHCAM_SECRET_KEY, else `<config dir>/secret_key` (created with 32 random
bytes on first run and then left alone - regenerating it on every start would
invalidate everyone's session), else an ephemeral in-memory key with a warning.

The config dir is `config.CONFIG_DIR` - the same directory `config.py` uses for
the cached data-dir path (`$DASHCAM_CONFIG_DIR`, or `~/.config/dashcam-viewer`).
In the Docker image that's `/config`, which should be a bind mount from the host
so `users.json` / `secret_key` survive `docker compose down -v` and can be
backed up as ordinary files.
"""
from __future__ import annotations

import getpass
import json
import os
import secrets
import sys
import time
from datetime import timedelta

from flask import abort, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from config import CONFIG_DIR

USERS_PATH = CONFIG_DIR / "users.json"
SECRET_KEY_PATH = CONFIG_DIR / "secret_key"

MIN_PASSWORD_LENGTH = 8

# pbkdf2:sha256 rather than the scrypt default: no OpenSSL-scrypt dependency, so
# it works identically on any Python/platform. Werkzeug's default iteration
# count applies.
_HASH_METHOD = "pbkdf2:sha256"

# Endpoints reachable without a session. `static` is Flask's built-in static
# handler (the login page's CSS is inline, but keep it open regardless);
# `healthz` is the container's liveness probe.
_OPEN_ENDPOINTS = {"login", "logout", "static", "healthz"}

# Precomputed once so verify() spends roughly the same time whether the username
# exists or not - a missing user shouldn't be observably faster than a wrong
# password.
_DUMMY_HASH = generate_password_hash("dashcam-nonexistent-user", method=_HASH_METHOD)

# Effectively "never expires" - a 10-year window, slid forward on every request
# (Flask's SESSION_REFRESH_EACH_REQUEST default). The user explicitly does not
# want their own logins to time out.
_SESSION_LIFETIME = timedelta(days=3650)


# --------------------------------------------------------------------------- #
# users.json
# --------------------------------------------------------------------------- #

def load_users() -> dict[str, str]:
    """username -> password hash. Empty dict if the file is missing or unreadable."""
    try:
        data = json.loads(USERS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_users(users: dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USERS_PATH.with_name(USERS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(users, indent=2, sort_keys=True) + "\n")
    tmp.chmod(0o600)
    tmp.replace(USERS_PATH)  # atomic - a concurrent login never sees a half-written file


def enabled() -> bool:
    """Auth is enforced iff at least one user is configured."""
    return bool(load_users())


def verify(username: str, password: str) -> bool:
    users = load_users()
    hashed = users.get(username)
    if hashed is None:
        check_password_hash(_DUMMY_HASH, password)  # burn comparable time
        return False
    return check_password_hash(hashed, password)


# --------------------------------------------------------------------------- #
# CLI (called from app.py's subcommands)
# --------------------------------------------------------------------------- #

def add_user_interactive(username: str) -> None:
    username = (username or "").strip()
    if not username:
        sys.exit("username cannot be empty")

    password = getpass.getpass(f"Password for {username!r}: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if getpass.getpass("Repeat password: ") != password:
        sys.exit("passwords did not match")

    users = load_users()
    action = "Updated" if username in users else "Added"
    users[username] = generate_password_hash(password, method=_HASH_METHOD)
    _write_users(users)
    print(f"{action} user {username!r}. {len(users)} user(s) in {USERS_PATH}.")


def delete_user(username: str) -> None:
    users = load_users()
    if username not in users:
        sys.exit(f"no such user: {username!r}")
    del users[username]
    _write_users(users)
    print(f"Removed user {username!r}. {len(users)} user(s) remain.")
    if not users:
        print("No users left - auth is now DISABLED (the viewer is open to anyone who can reach it).")


def list_users() -> None:
    users = load_users()
    if not users:
        print("(no users configured - auth is disabled)")
        return
    for name in sorted(users):
        print(name)


# --------------------------------------------------------------------------- #
# secret key
# --------------------------------------------------------------------------- #

def load_or_create_secret_key() -> bytes:
    env = os.environ.get("DASHCAM_SECRET_KEY")
    if env:
        return env.encode()

    try:
        existing = SECRET_KEY_PATH.read_bytes()
        if existing:
            return existing
    except (FileNotFoundError, OSError):
        pass

    key = secrets.token_bytes(32)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SECRET_KEY_PATH.with_name(SECRET_KEY_PATH.name + ".tmp")
        tmp.write_bytes(key)
        tmp.chmod(0o600)
        tmp.replace(SECRET_KEY_PATH)
        print(f"Generated a session secret key at {SECRET_KEY_PATH} (kept across restarts).")
    except OSError as e:
        print(f"WARNING: could not persist a session secret key ({e}). Sessions will "
              f"not survive a restart. Set $DASHCAM_SECRET_KEY or make {CONFIG_DIR} writable.")
    return key


# --------------------------------------------------------------------------- #
# Flask wiring
# --------------------------------------------------------------------------- #

def init_app(flask_app, *, secure_cookie: bool) -> None:
    """
    Attach the secret key, session-cookie settings, the login gate, and the
    /login and /logout routes to `flask_app`. Safe to call whether or not any
    users exist - if none do, the gate is a no-op and a warning is printed.
    """
    flask_app.secret_key = load_or_create_secret_key()
    flask_app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookie,
        PERMANENT_SESSION_LIFETIME=_SESSION_LIFETIME,
    )

    flask_app.before_request(_require_login)
    flask_app.add_url_rule("/login", "login", _login_view, methods=["GET", "POST"])
    flask_app.add_url_rule("/logout", "logout", _logout_view)

    if enabled():
        print(f"Auth: enabled ({len(load_users())} user(s), {USERS_PATH}).")
    else:
        line = "=" * 68
        print(f"{line}\n"
              f"  WARNING: auth is DISABLED - no users configured.\n"
              f"  Anyone who can reach this server has full access to the footage.\n"
              f"  Add a login:  python app.py adduser <name>\n"
              f"{line}")


def _safe_next(target: str) -> bool:
    """A local path only - never an absolute URL or a protocol-relative //host."""
    return bool(target) and target.startswith("/") and not target.startswith("//") and "\\" not in target


def _require_login():
    if not enabled():
        return None
    if request.endpoint in _OPEN_ENDPOINTS or session.get("user"):
        return None
    # XHR / media callers can't follow an HTML redirect usefully - give them a 401.
    if request.path.startswith(("/api/", "/video/", "/original/")):
        abort(401)
    return redirect(url_for("login", next=request.path))


def _login_view():
    if not enabled() or session.get("user"):
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if verify(username, password):
            session["user"] = username
            session.permanent = True
            target = request.args.get("next", "")
            return redirect(target if _safe_next(target) else url_for("index"))
        time.sleep(0.5)  # blunt the guess rate (pbkdf2 cost + nginx limiting do the rest)
        return render_template("login.html", error="Incorrect username or password."), 401

    return render_template("login.html", error=None)


def _logout_view():
    session.pop("user", None)
    return redirect(url_for("login") if enabled() else url_for("index"))

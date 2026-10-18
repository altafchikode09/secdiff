"""secdiff website accounts — stdlib-only user accounts for the web app.

Storage is a single SQLite database (``secdiff.db`` in the site's cache
dir) with three tables:

- ``users`` — username (unique, case-insensitive), email (unique,
  case-insensitive), salted PBKDF2 password hash, plan (default
  ``free``), created_at.
- ``sessions`` — login sessions; only the SHA-256 hash of the session
  token is stored, with an expiry.
- ``reset_codes`` — 6-digit codes for password reset and username
  recovery; only the code's hash is stored, with purpose, expiry, an
  attempt counter and a used flag. Codes are single-use and expire
  after 10 minutes; at most 5 attempts per code.

Passwords are hashed with ``hashlib.pbkdf2_hmac`` (SHA-256, per-user
random salt) and compared with ``hmac.compare_digest``. Passwords are
never logged and never stored in plain text. All SQL is parameterised.

Mail: codes are delivered by :func:`send_email`, which reads SMTP
configuration from the environment (``SMTP_HOST``, ``SMTP_PORT``,
``SMTP_USER``, ``SMTP_PASS``, ``MAIL_FROM``). When SMTP is not
configured, :func:`send_email` returns False and logs a clear line —
the caller must then tell the visitor honestly that email delivery is
not switched on yet, and must never display the code on screen.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PBKDF2_ROUNDS = 200_000
_CODE_TTL = timedelta(minutes=10)
_SESSION_TTL = timedelta(days=30)
MAX_CODE_ATTEMPTS = 5

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

PURPOSE_PASSWORD = "password"
PURPOSE_USERNAME = "username"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _algo, rounds, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Mailer (SMTP via env vars; unconfigured = honest False, never fake-sent)

def mailer_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("MAIL_FROM"))


def send_email(to: str, subject: str, body: str) -> bool:
    """Send one plain-text email via SMTP using env configuration.

    Env contract: SMTP_HOST, SMTP_PORT (default 587), SMTP_USER,
    SMTP_PASS, MAIL_FROM. Returns True only when the message was handed
    to the SMTP server; False (with a clear server-side log line) when
    mail is unconfigured or delivery fails.
    """
    host = os.environ.get("SMTP_HOST")
    sender = os.environ.get("MAIL_FROM")
    if not host or not sender:
        print("secdiff mail: SMTP not configured (set SMTP_HOST, SMTP_PORT, "
              "SMTP_USER, SMTP_PASS, MAIL_FROM) — email NOT sent to "
              f"{to!r} (subject {subject!r})", file=sys.stderr)
        return False
    import smtplib
    from email.message import EmailMessage

    port = int(os.environ.get("SMTP_PORT") or 587)
    user = os.environ.get("SMTP_USER") or ""
    password = os.environ.get("SMTP_PASS") or ""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as exc:  # delivery problems must not crash the site
        print(f"secdiff mail: SMTP delivery to {to!r} failed: {exc}",
              file=sys.stderr)
        return False
    return True


def deliver_code(user: dict, code: str, purpose: str) -> bool:
    """Email a recovery code to a user. Returns True iff it was sent."""
    if purpose == PURPOSE_PASSWORD:
        subject = "Your secdiff password reset code"
        what = "reset your password"
    else:
        subject = "Your secdiff username recovery code"
        what = "recover your username"
    body = (f"Hello {user['username']},\n\n"
            f"Your code to {what} is: {code}\n\n"
            "It expires in 10 minutes and can be used once. "
            "If you didn't request this, you can ignore this email.\n\n"
            "— secdiff (not investment advice)")
    return send_email(user["email"], subject, body)


# ---------------------------------------------------------------------------
# Account store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reset_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    purpose TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0
);
"""


class AccountStore:
    """SQLite-backed users / sessions / recovery codes."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript(_SCHEMA)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # -- users -------------------------------------------------------------
    def create_user(self, username: str, email: str, password: str,
                    display_name: str = "") -> dict:
        """Insert a user; raises ValueError with a friendly message when
        the username or email is already taken."""
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM users WHERE username = ?",
                          (username,)).fetchone():
                raise ValueError("That username is already taken — "
                                 "please choose another.")
            if db.execute("SELECT 1 FROM users WHERE email = ?",
                          (email,)).fetchone():
                raise ValueError("An account with that email address "
                                 "already exists — try logging in.")
            cur = db.execute(
                "INSERT INTO users (username, email, display_name, "
                "password_hash, plan, created_at) VALUES (?, ?, ?, ?, "
                "'free', ?)",
                (username, email, display_name, _hash_password(password),
                 _now().isoformat()))
            db.commit()
            row = db.execute("SELECT * FROM users WHERE id = ?",
                             (cur.lastrowid,)).fetchone()
            return dict(row)

    def get_by_username(self, username: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE username = ?",
                             (username,)).fetchone()
            return dict(row) if row else None

    def get_by_email(self, email: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE email = ?",
                             (email,)).fetchone()
            return dict(row) if row else None

    def find_for_login(self, identifier: str) -> dict | None:
        """Look up by username OR email (both case-insensitive)."""
        return self.get_by_username(identifier) or \
            self.get_by_email(identifier)

    def find_for_recovery(self, identifier: str) -> dict | None:
        return self.find_for_login(identifier)

    def check_password(self, user: dict, password: str) -> bool:
        return _verify_password(password, user["password_hash"])

    def set_password(self, user_id: int, new_password: str) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (_hash_password(new_password), user_id))
            db.commit()

    # -- sessions ------------------------------------------------------------
    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock, self._db() as db:
            db.execute("INSERT INTO sessions (token_hash, user_id, "
                       "expires_at) VALUES (?, ?, ?)",
                       (_token_hash(token), user_id,
                        (_now() + _SESSION_TTL).isoformat()))
            db.commit()
        return token

    def user_for_session(self, token: str) -> dict | None:
        if not token:
            return None
        with self._db() as db:
            row = db.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id = "
                "s.user_id WHERE s.token_hash = ?", (_token_hash(token),)
            ).fetchone()
            if row is None:
                return None
            sess = db.execute("SELECT expires_at FROM sessions WHERE "
                              "token_hash = ?", (_token_hash(token),)
                              ).fetchone()
            if sess["expires_at"] <= _now().isoformat():
                self.delete_session(token)
                return None
            return dict(row)

    def delete_session(self, token: str) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?",
                       (_token_hash(token),))
            db.commit()

    def delete_user_sessions(self, user_id: int,
                             except_token: str | None = None) -> None:
        with self._lock, self._db() as db:
            if except_token:
                db.execute("DELETE FROM sessions WHERE user_id = ? AND "
                           "token_hash != ?",
                           (user_id, _token_hash(except_token)))
            else:
                db.execute("DELETE FROM sessions WHERE user_id = ?",
                           (user_id,))
            db.commit()

    # -- recovery codes --------------------------------------------------------
    def issue_code(self, user_id: int, purpose: str) -> str:
        """Create a fresh 6-digit code (invalidating older ones for the
        same purpose) and return the plain code for delivery."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._lock, self._db() as db:
            db.execute("UPDATE reset_codes SET used = 1 WHERE user_id = ? "
                       "AND purpose = ? AND used = 0", (user_id, purpose))
            db.execute("INSERT INTO reset_codes (user_id, purpose, "
                       "code_hash, expires_at) VALUES (?, ?, ?, ?)",
                       (user_id, purpose, _code_hash(code),
                        (_now() + _CODE_TTL).isoformat()))
            db.commit()
        return code

    def verify_code(self, user_id: int, purpose: str, code: str) -> bool:
        """Check a code: constant-time compare, single-use, 10-minute
        expiry, max MAX_CODE_ATTEMPTS attempts. A correct code is marked
        used by this call."""
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT * FROM reset_codes WHERE user_id = ? AND purpose "
                "= ? AND used = 0 ORDER BY id DESC LIMIT 1",
                (user_id, purpose)).fetchone()
            if row is None:
                return False
            if row["attempts"] >= MAX_CODE_ATTEMPTS:
                return False
            if row["expires_at"] <= _now().isoformat():
                db.execute("UPDATE reset_codes SET used = 1 WHERE id = ?",
                           (row["id"],))
                db.commit()
                return False
            if hmac.compare_digest(_code_hash(code.strip()),
                                   row["code_hash"]):
                db.execute("UPDATE reset_codes SET used = 1 WHERE id = ?",
                           (row["id"],))
                db.commit()
                return True
            db.execute("UPDATE reset_codes SET attempts = attempts + 1 "
                       "WHERE id = ?", (row["id"],))
            db.commit()
            return False

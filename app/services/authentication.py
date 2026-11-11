"""Authentication policy shared by JSON and MongoDB; no HTTP or driver access."""

import hashlib
import hmac
import secrets
import uuid

from argon2.exceptions import InvalidHashError, VerificationError

from ..auth_storage import USERNAME
from ..validation import ValidationError, is_unicode
from .results import ServiceResult, failure

SESSION_TTL = 28800
IDLE_TTL = 1800
WINDOW = 900
MAX_ATTEMPTS = 5
ATTEMPT_CAPACITY = 10000


class InvalidSession(Exception):
    """Abort, rather than commit a read/touch for a rejected session."""


def valid_password(password):
    return (
        isinstance(password, str)
        and is_unicode(password)
        and 12 <= len(password) <= 256
        and len(password.encode("utf-8")) <= 1024
    )


def valid_display(name):
    return (
        isinstance(name, str)
        and is_unicode(name)
        and 1 <= len(name) <= 120
        and name == name.strip()
    )


def normalize_username(username):
    if not isinstance(username, str):
        return None
    username = username.strip().lower()
    return username if USERNAME.fullmatch(username) else None


def public_profile(user):
    return {key: user[key] for key in ("id", "username", "display_name", "role", "enabled")}


def token_digest(token):
    if not isinstance(token, str) or not token.isascii() or not 20 <= len(token) <= 128:
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def attempt_key(key, prefix, value):
    return hmac.new(key, (prefix + value).encode("utf-8"), hashlib.sha256).hexdigest()


def session_is_current(session, user, timestamp):
    return bool(
        session
        and user
        and user["enabled"]
        and session["user_id"] == user["id"]
        and timestamp < session["expires_at"]
        and timestamp < session["last_seen"] + IDLE_TTL
        and timestamp >= session["last_seen"]
    )


def unauthorized():
    return failure(401, "unauthorized", "Authentification requise ou invalide.")


def attempt_budget(work, keys, timestamp):
    work.attempts.prune_expired(timestamp, window=WINDOW)
    existing = work.attempts.get_many(keys)
    if any(existing.get(key, {}).get("count", 0) >= MAX_ATTEMPTS for key in keys):
        return existing, failure(
            429, "rate_limited", "Trop de tentatives.", headers={"Retry-After": str(WINDOW)}
        )
    if work.attempts.count() + len(set(keys) - existing.keys()) > ATTEMPT_CAPACITY:
        return existing, failure(
            503, "auth_attempt_capacity", "Capacité de vérification temporairement atteinte."
        )
    return existing, None


def record_failed_attempts(work, existing, keys, timestamp):
    for key in keys:
        current = existing.get(key)
        work.attempts.set(
            {"key": key, "window_start": timestamp, "count": 1}
            if current is None
            else {**current, "count": current["count"] + 1}
        )


class AuthenticationService:
    def __init__(self, persistence, *, hasher, dummy_hash, throttle_key):
        self._persistence = persistence
        self._hasher = hasher
        self._dummy_hash = dummy_hash
        if not isinstance(throttle_key, bytes) or len(throttle_key) < 32:
            raise ValueError("Authentication requires a 32-byte throttle key")
        self._throttle_key = throttle_key

    def login(self, username, password, *, address, timestamp):
        normalized = normalize_username(username)
        supplied = (
            password
            if isinstance(password, str) and is_unicode(password) and len(password) <= 256
            else ""
        )
        keys = [
            attempt_key(self._throttle_key, "user:", normalized or "<invalid>"),
            attempt_key(self._throttle_key, "ip:", address),
        ]
        # Preparation is outside the callback: a transaction retry keeps one identity/token.
        token, session_id = secrets.token_urlsafe(32), str(uuid.uuid4())

        def command(work):
            existing, problem = attempt_budget(work, keys, timestamp)
            if problem is not None:
                return problem
            user = work.accounts.by_username(normalized) if normalized is not None else None
            try:
                ok = self._hasher.verify(
                    user["password_hash"] if user else self._dummy_hash, supplied
                )
            except (VerificationError, InvalidHashError):
                ok = False
            if not ok or not user or not user["enabled"]:
                record_failed_attempts(work, existing, keys, timestamp)
                return unauthorized()
            work.sessions.delete_expired(timestamp, idle_ttl=IDLE_TTL)
            work.sessions.insert(
                {
                    "id": session_id,
                    "user_id": user["id"],
                    "token_hash": token_digest(token),
                    "created_at": timestamp,
                    "last_seen": timestamp,
                    "expires_at": timestamp + SESSION_TTL,
                }
            )
            return ServiceResult(
                200, body={"token": token, "token_type": "Bearer", "expires_in": SESSION_TTL}
            )

        return self._persistence.run(command)

    def _protected(self, token, timestamp, operation):
        digest = token_digest(token)
        if digest is None:
            return unauthorized()

        def command(work):
            session = work.sessions.by_token_hash(digest)
            user = work.accounts.get(session["user_id"]) if session is not None else None
            if not session_is_current(session, user, timestamp):
                raise InvalidSession()
            work.sessions.touch(session["id"], timestamp)
            return operation(work, user, session)

        try:
            return self._persistence.run(command)
        except InvalidSession:
            return unauthorized()

    def me(self, token, *, timestamp):
        return self._protected(
            token,
            timestamp,
            lambda work, user, session: ServiceResult(200, body=public_profile(user)),
        )

    def update_me(self, token, display_name, *, timestamp):
        if not valid_display(display_name):
            raise ValidationError({"display_name": "Nom invalide."})

        def update(work, user, session):
            user["display_name"] = display_name
            work.accounts.save(user)
            return ServiceResult(200, body=public_profile(user))

        return self._protected(token, timestamp, update)

    def change_password(self, token, current_password, new_password, *, timestamp):
        if not valid_password(new_password):
            raise ValidationError({"new_password": "Mot de passe invalide."})
        if (
            not isinstance(current_password, str)
            or not is_unicode(current_password)
            or len(current_password) > 256
        ):
            return unauthorized()

        def change(work, user, session):
            keys = [attempt_key(self._throttle_key, "reauth:user:", user["id"])]
            existing, problem = attempt_budget(work, keys, timestamp)
            if problem is not None:
                return problem
            try:
                self._hasher.verify(user["password_hash"], current_password)
            except (VerificationError, InvalidHashError):
                record_failed_attempts(work, existing, keys, timestamp)
                return unauthorized()
            user["password_hash"] = self._hasher.hash(new_password)
            work.accounts.save(user)
            work.sessions.revoke_user(user["id"])
            return ServiceResult(204)

        return self._protected(token, timestamp, change)

    def logout(self, token, *, timestamp):
        def revoke(work, user, session):
            work.sessions.revoke(session["id"])
            return ServiceResult(204)

        return self._protected(token, timestamp, revoke)

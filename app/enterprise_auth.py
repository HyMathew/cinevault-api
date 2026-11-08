"""Enterprise account routes; a user identity is verified only by this service."""

import hashlib
import hmac
import json
import secrets
import time
import uuid
from pathlib import Path

import portalocker
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask import Blueprint, current_app, g, jsonify, request
from werkzeug.exceptions import BadRequest, ServiceUnavailable, UnsupportedMediaType

from .auth_storage import USERNAME
from .client_context import InvalidClientContext, client_address, install_client_context
from .registration_admission import RegistrationAdmission, RegistrationBusy
from .repository import unique_pairs
from .storage_v2 import V2Store
from .validation import ValidationError, is_unicode

SESSION_TTL = 28800
IDLE_TTL = 1800
WINDOW = 900
MAX_ATTEMPTS = 5
bp = Blueprint("enterprise_auth", __name__, url_prefix="/api/v1")
hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def error(status, code, message, fields=None):
    value = {"code": code, "message": message}
    if fields is not None:
        value["fields"] = fields
    return jsonify(error=value), status


def payload(required, optional=()):
    if not request.is_json:
        raise UnsupportedMediaType()
    try:
        value = json.loads(
            request.get_data(),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise BadRequest("JSON invalide.")
    if (
        not isinstance(value, dict)
        or not set(required).issubset(value)
        or set(value) - set(required) - set(optional)
    ):
        raise ValidationError({"body": "Champs invalides."})
    return value


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
    return {k: user[k] for k in ("id", "username", "display_name", "role", "enabled")}


def transact(operation):
    try:
        return current_app.extensions["enterprise_store"].transact(operation)
    except (OSError, ValueError, RecursionError, portalocker.exceptions.LockException) as exc:
        current_app.logger.error("Stockage Enterprise indisponible : %s", type(exc).__name__)
        raise ServiceUnavailable("Stockage indisponible.") from exc


def check_store():
    try:
        return current_app.extensions["enterprise_store"].read()
    except (OSError, ValueError, RecursionError, portalocker.exceptions.LockException) as exc:
        current_app.logger.error("Stockage Enterprise indisponible : %s", type(exc).__name__)
        raise ServiceUnavailable("Stockage indisponible.") from exc


def now():
    return int(current_app.config.get("AUTH_CLOCK", time.time)())


def token_digest(token):
    if not isinstance(token, str) or not token.isascii() or not 20 <= len(token) <= 128:
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def bearer():
    value = request.headers.get("Authorization", "")
    parts = value.split(" ")
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] else ""


def find_session(document, token, timestamp):
    digest = token_digest(token)
    if digest is None:
        return None, None
    for session in document["sessions"]:
        if hmac.compare_digest(session["token_hash"], digest):
            if (
                timestamp >= session["expires_at"]
                or timestamp >= session["last_seen"] + IDLE_TTL
                or timestamp < session["last_seen"]
            ):
                return None, None
            user = next(
                (u for u in document["users"] if u["id"] == session["user_id"] and u["enabled"]),
                None,
            )
            if user:
                session["last_seen"] = timestamp
                return user, session
    return None, None


def unauthorized():
    return error(401, "unauthorized", "Authentification requise ou invalide.")


class InvalidSession(Exception):
    """Abort a transaction before a rejected identity can cause a disk write."""


def protected(operation):
    token, timestamp = bearer(), now()
    if token_digest(token) is None:
        return unauthorized()

    def action(doc):
        user, session = find_session(doc, token, timestamp)
        if user is None:
            raise InvalidSession()
        return operation(doc, user, session)

    try:
        return transact(action)
    except InvalidSession:
        return unauthorized()


def make_user(username, display_name, password, role, timestamp):
    return {
        "id": str(uuid.uuid4()),
        "username": username,
        "display_name": display_name,
        "password_hash": hasher.hash(password),
        "role": role,
        "enabled": True,
        "created_at": timestamp,
    }


@bp.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.before_request
def authenticate_client_context():
    if request.method == "POST" and request.endpoint in (
        "enterprise_auth.register",
        "enterprise_auth.login",
    ):
        try:
            g.auth_client_address = client_address(current_app, now())
        except InvalidClientContext:
            return unauthorized()


def registration_problem(document, username):
    if any(user["username"] == username for user in document["users"]):
        return error(409, "conflict", "Identifiant indisponible.")
    if len(document["users"]) >= 10000:
        return error(503, "registration_capacity", "Inscription temporairement indisponible.")
    return None


@bp.post("/auth/register")
def register():
    data = payload(("username", "display_name", "password"))
    username = normalize_username(data["username"])
    if (
        not username
        or not valid_display(data["display_name"])
        or not valid_password(data["password"])
    ):
        raise ValidationError({"body": "Identifiant, nom ou mot de passe invalide."})
    problem = registration_problem(check_store(), username)
    if problem:
        return problem
    try:
        user = current_app.extensions["registration_admission"].compute(
            limit_key("register-ip:", g.auth_client_address),
            now(),
            lambda: make_user(username, data["display_name"], data["password"], "member", now()),
        )
    except RegistrationBusy as exc:
        response, status = error(429, "rate_limited", "Trop de tentatives d’inscription.")
        response.headers["Retry-After"] = str(exc.retry_after)
        return response, status
    except (OSError, ValueError, RecursionError, portalocker.exceptions.LockException) as exc:
        current_app.logger.error("Admission indisponible : %s", type(exc).__name__)
        raise ServiceUnavailable("Inscription temporairement indisponible.") from exc

    def add(doc):
        problem = registration_problem(doc, username)
        if problem:
            return problem
        doc["users"].append(user)
        return public_profile(user)

    profile = transact(add)
    if not isinstance(profile, dict):
        return profile
    return jsonify(profile), 201


def limit_key(prefix, value):
    key = current_app.extensions["auth_throttle_key"]
    return hmac.new(key, (prefix + value).encode("utf-8"), hashlib.sha256).hexdigest()


@bp.post("/auth/login")
def login():
    data = payload(("username", "password"))
    supplied_user = normalize_username(data["username"])
    supplied_password = (
        data["password"]
        if isinstance(data["password"], str)
        and is_unicode(data["password"])
        and len(data["password"]) <= 256
        else ""
    )
    username = supplied_user or "<invalid>"
    timestamp = now()
    keys = [limit_key("user:", username), limit_key("ip:", g.auth_client_address)]
    dummy = current_app.extensions["auth_dummy_hash"]

    def attempt(doc):
        doc["auth_attempts"][:] = [
            a
            for a in doc["auth_attempts"]
            if timestamp - a["window_start"] < WINDOW and timestamp >= a["window_start"]
        ]
        existing = {a["key"]: a for a in doc["auth_attempts"]}
        if any(existing.get(k, {}).get("count", 0) >= MAX_ATTEMPTS for k in keys):
            return "limited", None
        user = next((u for u in doc["users"] if u["username"] == username), None)
        try:
            ok = hasher.verify(user["password_hash"] if user else dummy, supplied_password)
        except (VerificationError, InvalidHashError):
            ok = False
        if not ok or not user or not user["enabled"]:
            for key in keys:
                if key in existing:
                    existing[key]["count"] += 1
                else:
                    doc["auth_attempts"].append({"key": key, "window_start": timestamp, "count": 1})
            return "invalid", None
        token = secrets.token_urlsafe(32)
        doc["sessions"][:] = [
            s
            for s in doc["sessions"]
            if s["expires_at"] > timestamp and s["last_seen"] + IDLE_TTL > timestamp
        ]
        doc["sessions"].append(
            {
                "id": str(uuid.uuid4()),
                "user_id": user["id"],
                "token_hash": token_digest(token),
                "created_at": timestamp,
                "last_seen": timestamp,
                "expires_at": timestamp + SESSION_TTL,
            }
        )
        return "ok", token

    outcome, token = transact(attempt)
    if outcome == "limited":
        response, status = error(429, "rate_limited", "Trop de tentatives.")
        response.headers["Retry-After"] = str(WINDOW)
        return response, status
    if outcome != "ok":
        return unauthorized()
    return {"token": token, "token_type": "Bearer", "expires_in": SESSION_TTL}


@bp.get("/me")
def me():
    return protected(lambda doc, user, session: (jsonify(public_profile(user)), 200))


@bp.patch("/me")
def update_me():
    data = payload(("display_name",))
    if not valid_display(data["display_name"]):
        raise ValidationError({"display_name": "Nom invalide."})

    def update(doc, user, session):
        user["display_name"] = data["display_name"]
        return jsonify(public_profile(user)), 200

    return protected(update)


@bp.put("/me/password")
def change_password():
    data = payload(("current_password", "new_password"))
    if not valid_password(data["new_password"]):
        raise ValidationError({"new_password": "Mot de passe invalide."})
    current = data["current_password"]
    if not isinstance(current, str) or not is_unicode(current) or len(current) > 256:
        return unauthorized()

    def change(doc, user, session):
        try:
            hasher.verify(user["password_hash"], current)
        except (VerificationError, InvalidHashError):
            return unauthorized()
        user["password_hash"] = hasher.hash(data["new_password"])
        doc["sessions"][:] = [s for s in doc["sessions"] if s["user_id"] != user["id"]]
        return "", 204

    return protected(change)


def forbidden():
    return error(403, "forbidden", "Accès interdit.")


@bp.get("/users")
def list_users():
    def listing(doc, user, session):
        if user["role"] != "admin":
            return forbidden()
        try:
            limit = int(request.args.get("limit", "50"))
            offset = int(request.args.get("offset", "0"))
            if (
                not 1 <= limit <= 100
                or not 0 <= offset <= 10000
                or set(request.args) - {"limit", "offset"}
            ):
                raise ValueError()
        except ValueError:
            raise ValidationError({"query": "Pagination invalide."})
        users = sorted(doc["users"], key=lambda u: u["username"])
        return jsonify(
            users=[public_profile(u) for u in users[offset : offset + limit]],
            total=len(users),
            limit=limit,
            offset=offset,
        ), 200

    return protected(listing)


@bp.get("/users/<user_id>")
def get_user(user_id):
    def get(doc, user, session):
        if user["role"] != "admin" and user["id"] != user_id:
            return forbidden()
        target = next((u for u in doc["users"] if u["id"] == user_id), None)
        if target is None:
            return error(404, "not_found", "Compte introuvable.")
        return jsonify(public_profile(target)), 200

    return protected(get)


@bp.patch("/users/<user_id>")
def update_user(user_id):
    def update(doc, user, session):
        if user["role"] != "admin":
            return forbidden()
        data = payload((), ("role", "enabled"))
        if (
            not data
            or ("role" in data and data["role"] not in ("member", "manager", "admin"))
            or ("enabled" in data and type(data["enabled"]) is not bool)
        ):
            raise ValidationError({"body": "Rôle ou statut invalide."})
        target = next((u for u in doc["users"] if u["id"] == user_id), None)
        if target is None:
            return error(404, "not_found", "Compte introuvable.")
        candidate = {**target, **data}
        if (
            target["role"] == "admin"
            and target["enabled"]
            and (candidate["role"] != "admin" or not candidate["enabled"])
        ):
            if not any(
                u["id"] != target["id"] and u["role"] == "admin" and u["enabled"]
                for u in doc["users"]
            ):
                return error(
                    409, "last_admin", "Le dernier administrateur actif doit être conservé."
                )
        changed = any(target[k] != v for k, v in data.items())
        target.update(data)
        if not target["enabled"]:
            from .reservation_service import cancel_disabled

            cancel_disabled(doc, target["id"], user["id"], now())
        if changed:
            doc["sessions"][:] = [s for s in doc["sessions"] if s["user_id"] != target["id"]]
        return jsonify(public_profile(target)), 200

    return protected(update)


@bp.post("/auth/logout")
def logout():
    def revoke(doc, user, session):
        doc["sessions"].remove(session)
        return "", 204

    return protected(revoke)


def install_enterprise(app):
    path = app.config.get("ENTERPRISE_DATA_FILE")
    key = app.config.get("AUTH_THROTTLE_KEY")
    key_file = app.config.get("AUTH_THROTTLE_KEY_FILE")
    if key is None and key_file:
        key = Path(key_file).read_bytes()
    if not path or not isinstance(key, bytes) or len(key) < 32:
        raise RuntimeError("Enterprise requires an existing v2 store and a 32-byte throttle key")
    store = V2Store(path)
    store.read()
    app.extensions["enterprise_store"] = store
    app.extensions["auth_throttle_key"] = key
    app.extensions["auth_dummy_hash"] = hasher.hash(secrets.token_urlsafe(32))
    install_client_context(app)
    admission = RegistrationAdmission(path)
    reserved = {
        Path(str(value) + suffix).resolve()
        for value in (path, app.config["DATA_FILE"])
        for suffix in ("", ".lock")
    }
    if any(Path(value).resolve() in reserved for value in (admission.path, admission.lock_path)):
        raise RuntimeError("Registration admission must be separate from business files and locks")
    app.extensions["registration_admission"] = admission
    app.register_blueprint(bp)
    from .enterprise_catalogue import bp as catalogue_bp

    app.register_blueprint(catalogue_bp)
    from .enterprise_collections import bp as collections_bp

    app.register_blueprint(collections_bp)
    from .enterprise_lending import bp as lending_bp

    app.register_blueprint(lending_bp)
    from .enterprise_reservations import bp as reservations_bp

    app.register_blueprint(reservations_bp)
    from .enterprise_transfer import bp as transfer_bp

    app.register_blueprint(transfer_bp)
    from .enterprise_insights import bp as insights_bp

    app.register_blueprint(insights_bp)
    from .enterprise_metadata import install_metadata

    install_metadata(app)

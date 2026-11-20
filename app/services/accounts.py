"""Registration and account administration over transaction-scoped ports."""

import uuid

from ..auth_storage import ACCOUNT_CAPACITY
from ..circulation_model import payload
from ..personal_model import make_event
from ..validation import ValidationError
from .authentication import (
    authenticated,
    normalize_username,
    public_profile,
    valid_display,
    valid_password,
)
from .catalogue import canonical_identity
from .circulation import cancel_disabled_in_work
from .results import ServiceResult, failure


def new_account(username, display_name, password_hash, role, timestamp):
    return {
        "id": str(uuid.uuid4()),
        "username": username,
        "display_name": display_name,
        "password_hash": password_hash,
        "role": role,
        "enabled": True,
        "created_at": timestamp,
    }


def registration_problem(accounts, username):
    if accounts.by_username(username) is not None:
        return failure(409, "conflict", "Identifiant indisponible.")
    if accounts.count() >= ACCOUNT_CAPACITY:
        return failure(503, "registration_capacity", "Inscription temporairement indisponible.")
    return None


def forbidden():
    return failure(403, "forbidden", "Accès interdit.")


def missing():
    return failure(404, "not_found", "Compte introuvable.")


class RegistrationService:
    def __init__(self, persistence):
        self._persistence = persistence

    def register(self, username, display_name, password, *, timestamp, prepare_hash):
        username = normalize_username(username)
        if not username or not valid_display(display_name) or not valid_password(password):
            raise ValidationError({"body": "Identifiant, nom ou mot de passe invalide."})
        problem = self._persistence.query(
            lambda work: registration_problem(work.accounts, username)
        )
        if problem is not None:
            return problem
        # Admission and expensive crypto happen once, without a business UoW.
        # Only the prepared account below is captured by a retryable command.
        account = new_account(username, display_name, prepare_hash(password), "member", timestamp)

        def add(work):
            problem = registration_problem(work.accounts, username)
            if problem is not None:
                return problem
            work.accounts.insert(account)
            return ServiceResult(201, body=public_profile(account))

        return self._persistence.run(add)


class OperatorAccountService:
    def __init__(self, persistence):
        self._persistence = persistence

    def bootstrap(self, username, display_name, password, *, timestamp, prepare_hash):
        username = normalize_username(username)
        if not username or not valid_display(display_name) or not valid_password(password):
            raise ValueError("Invalid account fields")

        def check(work):
            if work.accounts.has_admin():
                raise ValueError("An administrator already exists")
            if work.accounts.by_username(username) is not None:
                raise ValueError("Username unavailable")
            if work.accounts.count() >= ACCOUNT_CAPACITY:
                raise ValueError("Account storage limit exceeded")

        self._persistence.query(check)
        account = new_account(username, display_name, prepare_hash(password), "admin", timestamp)

        def add(work):
            check(work)
            work.accounts.insert(account)
            return public_profile(account)

        return self._persistence.run(add)

    def reset_password(self, operator, username, password, *, enable, timestamp, prepare_hash):
        operator, username = normalize_username(operator), normalize_username(username)
        if not operator or not username or not valid_password(password) or type(enable) is not bool:
            raise ValueError("Invalid reset fields")
        if type(timestamp) is not int or timestamp < 0:
            raise ValueError("Recovery clock precedes stored events")

        def subjects(work):
            actor = work.accounts.by_username(operator)
            user = work.accounts.by_username(username)
            if actor is None or not actor["enabled"] or actor["role"] != "admin" or user is None:
                raise ValueError("Invalid account or operator")
            if work.has_activity_after(timestamp):
                raise ValueError("Recovery clock precedes stored events")
            return actor, user

        def preflight(work):
            subjects(work)

        self._persistence.query(preflight)
        password_hash = prepare_hash(password)
        event_id = str(uuid.uuid4())

        def reset(work):
            actor, user = subjects(work)
            user["password_hash"] = password_hash
            if enable:
                user["enabled"] = True
            work.accounts.save(user)
            work.sessions.revoke_user(user["id"])
            work.events.append(
                make_event(
                    event_id, actor["id"], "recovery", user["id"], "password_reset", timestamp
                )
            )

        self._persistence.run(reset)


class AdminAccountService:
    def __init__(self, persistence):
        self._persistence = persistence

    def users(self, token, query, *, timestamp):
        def listing(work, user, session):
            if user["role"] != "admin":
                return forbidden()
            # MultiDict.get historically takes the first repeated value.
            values = {}
            for key, value in query:
                values.setdefault(key, value)
            try:
                limit, offset = int(values.get("limit", "50")), int(values.get("offset", "0"))
                if (
                    not 1 <= limit <= 100
                    or not 0 <= offset <= 10000
                    or set(values) - {"limit", "offset"}
                ):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValidationError({"query": "Pagination invalide."}) from None
            return ServiceResult(
                200,
                body={
                    "users": [public_profile(row) for row in work.accounts.page(limit, offset)],
                    "total": work.accounts.count(),
                    "limit": limit,
                    "offset": offset,
                },
            )

        return authenticated(self._persistence, token, timestamp, listing)

    def get(self, token, identity, *, timestamp):
        def get(work, user, session):
            if user["role"] != "admin" and user["id"] != identity:
                return forbidden()
            target = work.accounts.get(identity) if canonical_identity(identity) else None
            return missing() if target is None else ServiceResult(200, body=public_profile(target))

        return authenticated(self._persistence, token, timestamp, get)

    def update(self, token, identity, data, *, timestamp):
        namespace = uuid.uuid4()

        def update(work, user, session):
            if user["role"] != "admin":
                return forbidden()
            attributes = payload(data, (), ("role", "enabled"))
            if (
                not attributes
                or (
                    "role" in attributes
                    and attributes["role"] not in ("member", "manager", "admin")
                )
                or ("enabled" in attributes and type(attributes["enabled"]) is not bool)
            ):
                raise ValidationError({"body": "Rôle ou statut invalide."})
            target = work.accounts.get(identity) if canonical_identity(identity) else None
            if target is None:
                return missing()
            candidate = {**target, **attributes}
            if (
                target["role"] == "admin"
                and target["enabled"]
                and (candidate["role"] != "admin" or not candidate["enabled"])
                and not work.accounts.has_other_enabled_admin(identity)
            ):
                return failure(
                    409, "last_admin", "Le dernier administrateur actif doit être conservé."
                )
            changed = any(target[key] != value for key, value in attributes.items())
            work.accounts.save(candidate)
            if not candidate["enabled"]:
                cancel_disabled_in_work(work, identity, user["id"], timestamp, namespace)
            if changed:
                work.sessions.revoke_user(identity)
            return ServiceResult(200, body=public_profile(candidate))

        return authenticated(self._persistence, token, timestamp, update)

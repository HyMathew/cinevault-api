"""Strict on-disk shapes for the implemented account subset."""

import re

from .validation import is_unicode

USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
HASH = re.compile(r"^[0-9a-f]{64}$")


def validate_auth(data, ids, valid_uuid):
    usernames, user_ids, token_hashes, attempt_keys = set(), set(), set(), set()
    if any(len(data[name]) > 10000 for name in ("users", "sessions", "auth_attempts")):
        raise ValueError("Account storage limit exceeded")
    for user in data["users"]:
        if not isinstance(user, dict) or set(user) != {
            "id",
            "username",
            "display_name",
            "password_hash",
            "role",
            "enabled",
            "created_at",
        }:
            raise ValueError("Invalid user fields")
        if not valid_uuid(user["id"]) or user["id"] in ids:
            raise ValueError("Invalid user id")
        if (
            not isinstance(user["username"], str)
            or not USERNAME.fullmatch(user["username"])
            or user["username"] in usernames
        ):
            raise ValueError("Invalid username")
        name, password_hash = user["display_name"], user["password_hash"]
        if (
            not isinstance(name, str)
            or not is_unicode(name)
            or not 1 <= len(name) <= 120
            or name.strip() != name
        ):
            raise ValueError("Invalid display name")
        if (
            not isinstance(password_hash, str)
            or not password_hash.startswith("$argon2id$")
            or len(password_hash) > 512
        ):
            raise ValueError("Invalid password hash")
        if (
            user["role"] not in ("member", "manager", "admin")
            or type(user["enabled"]) is not bool
            or type(user["created_at"]) is not int
            or user["created_at"] < 0
        ):
            raise ValueError("Invalid account state")
        ids.add(user["id"])
        user_ids.add(user["id"])
        usernames.add(user["username"])
    for session in data["sessions"]:
        if not isinstance(session, dict) or set(session) != {
            "id",
            "user_id",
            "token_hash",
            "created_at",
            "last_seen",
            "expires_at",
        }:
            raise ValueError("Invalid session fields")
        if (
            not valid_uuid(session["id"])
            or session["id"] in ids
            or not valid_uuid(session["user_id"])
            or session["user_id"] not in user_ids
        ):
            raise ValueError("Invalid session reference")
        digest = session["token_hash"]
        if not isinstance(digest, str) or not HASH.fullmatch(digest) or digest in token_hashes:
            raise ValueError("Invalid token digest")
        if (
            any(type(session[key]) is not int for key in ("created_at", "last_seen", "expires_at"))
            or not 0 <= session["created_at"] <= session["last_seen"] <= session["expires_at"]
        ):
            raise ValueError("Invalid session timestamps")
        ids.add(session["id"])
        token_hashes.add(digest)
    for attempt in data["auth_attempts"]:
        if not isinstance(attempt, dict) or set(attempt) != {"key", "window_start", "count"}:
            raise ValueError("Invalid attempt fields")
        key = attempt["key"]
        if (
            not isinstance(key, str)
            or not HASH.fullmatch(key)
            or key in attempt_keys
            or type(attempt["window_start"]) is not int
            or attempt["window_start"] < 0
            or type(attempt["count"]) is not int
            or not 1 <= attempt["count"] <= 5
        ):
            raise ValueError("Invalid attempt state")
        attempt_keys.add(key)

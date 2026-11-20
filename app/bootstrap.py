"""Explicit local first-administrator creation; no default credentials."""

import time

import portalocker

from .account_cli import (
    OperatorArgumentParser,
    add_store_arguments,
    hidden_input,
    open_operator_store,
    require_interactive,
    validate_store_arguments,
)
from .enterprise_auth import hasher
from .persistence.errors import CommitOutcomeUnknown
from .persistence.json import JsonPersistence
from .services.accounts import OperatorAccountService
from .storage_v2 import V2Store


def bootstrap_admin(store, username, display_name, password, *, timestamp=None):
    if isinstance(store, V2Store):
        store = JsonPersistence(store)
    return OperatorAccountService(store).bootstrap(
        username,
        display_name,
        password,
        timestamp=int(time.time()) if timestamp is None else timestamp,
        prepare_hash=hasher.hash,
    )


def main():
    parser = OperatorArgumentParser(
        description="Create the first administrator in an existing Enterprise store."
    )
    add_store_arguments(parser)
    args = parser.parse_args()
    validate_store_arguments(parser, args)
    require_interactive(parser)
    try:
        username = input("Username: ")
        display_name = input("Display name: ")
        password = hidden_input("Password: ")
        confirmation = hidden_input("Confirm password: ")
        if password != confirmation:
            parser.exit(1, "Passwords do not match.\n")
        with open_operator_store(args) as persistence:
            bootstrap_admin(persistence, username, display_name, password)
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, "Administrator creation cancelled.\n")
    except CommitOutcomeUnknown:
        parser.exit(1, "Administrator creation outcome is uncertain. Verify before retrying.\n")
    except (ValueError, OSError, RuntimeError, portalocker.exceptions.LockException):
        parser.exit(1, "Administrator creation failed. Verify the store before retrying.\n")
    print("Administrator created.")


if __name__ == "__main__":
    main()

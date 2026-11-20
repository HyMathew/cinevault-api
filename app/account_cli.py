"""Interactive storage selection shared by the offline account commands."""

import argparse
import getpass
import sys
import warnings
from contextlib import contextmanager

from .persistence.json import JsonPersistence


class OperatorArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        if message.startswith("unrecognized arguments:"):
            message = "Unrecognized arguments. Use --help to see the supported options."
        super().error(message)


def add_store_arguments(parser):
    parser.add_argument("--storage-backend", choices=("json", "mongodb"), default="json")
    parser.add_argument("--store", help="Existing Enterprise JSON file")
    parser.add_argument(
        "--mongo-uri-file", help="Private file containing the MongoDB connection URI"
    )
    parser.add_argument(
        "--mongo-database", help="Existing provisioned MongoDB application database"
    )


def validate_store_arguments(parser, args):
    if args.storage_backend == "json":
        if not args.store or args.mongo_uri_file is not None or args.mongo_database is not None:
            parser.error(
                "JSON requires --store; MongoDB options require --storage-backend mongodb."
            )
    elif args.store is not None or not args.mongo_uri_file or not args.mongo_database:
        parser.error("MongoDB requires --mongo-uri-file and --mongo-database without --store.")


def require_interactive(parser):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        parser.error("An interactive terminal is required; credentials cannot be piped.")


def hidden_input(prompt):
    with warnings.catch_warnings(action="error", category=getpass.GetPassWarning):
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise OSError("A terminal with hidden input is required") from None


@contextmanager
def open_operator_store(args):
    if args.storage_backend == "json":
        yield JsonPersistence(args.store)
        return

    from .persistence.factory import open_mongo_persistence

    persistence = open_mongo_persistence(
        {
            "STORAGE_BACKEND": "mongodb",
            "ENTERPRISE_ENABLED": True,
            "MONGODB_URI_FILE": args.mongo_uri_file,
            "MONGODB_DATABASE": args.mongo_database,
        }
    )
    try:
        yield persistence
    finally:
        persistence.close()

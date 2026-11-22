"""Select one configured business backend and load the MongoDB driver on demand."""

import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


def storage_backend(config):
    backend = config.get("STORAGE_BACKEND", "json")
    if backend == "json":
        if config.get("MONGODB_URI_FILE") is not None or config.get("MONGODB_DATABASE") is not None:
            raise RuntimeError("MongoDB settings require STORAGE_BACKEND=mongodb")
        return backend
    if backend != "mongodb":
        raise RuntimeError("STORAGE_BACKEND must be json or mongodb")
    if config.get("ENTERPRISE_ENABLED") is not True:
        raise RuntimeError("MongoDB requires Enterprise mode")
    if config.get("ENTERPRISE_DATA_FILE") is not None:
        raise RuntimeError("MongoDB mode cannot configure an Enterprise JSON data file")
    name = config.get("MONGODB_DATABASE")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name)
        or name in {"admin", "config", "local"}
    ):
        raise RuntimeError("MongoDB requires an explicit application database name")
    if not config.get("MONGODB_URI_FILE"):
        raise RuntimeError("MongoDB requires a connection secret file")
    return backend


def file_and_lock(path):
    return {Path(str(path) + suffix).resolve() for suffix in ("", ".lock")}


def protected_file_paths(config):
    paths = set()
    for key in (
        "DATA_FILE",
        "ENTERPRISE_DATA_FILE",
        "AUTH_THROTTLE_KEY_FILE",
        "CLIENT_CONTEXT_KEY_FILE",
        "MONGODB_URI_FILE",
    ):
        if config.get(key):
            paths.update(file_and_lock(config[key]))
    return paths


def _connection_secret(path, parse_uri):
    path = Path(path)
    if not path.is_file():
        raise ValueError("MongoDB connection secret must be a regular file")
    with path.open("rb") as stream:
        raw = stream.read(8193)
    if not 0 < len(raw) <= 8192:
        raise ValueError("Invalid MongoDB connection secret size")
    return validate_mongo_uri(raw.decode("utf-8").rstrip("\r\n"), parse_uri)


def validate_mongo_uri(uri, parse_uri):
    if (
        not 0 < len(uri.encode("utf-8")) <= 8192
        or not uri.startswith("mongodb://")
        or any(char.isspace() for char in uri)
    ):
        raise ValueError("Invalid MongoDB connection secret")
    query = urlsplit(uri).query
    keys = [key.lower() for key, _ in parse_qsl(query, strict_parsing=True, keep_blank_values=True)]
    allowed = {"authsource", "authmechanism", "replicaset", "directconnection", "tls", "tlscafile"}
    if len(keys) != len(set(keys)) or set(keys) - allowed:
        raise ValueError("Unsupported MongoDB options")
    parts = parse_uri(uri, validate=True)
    options = parts["options"]
    if (
        parts["database"] is not None
        or not parts["username"]
        or not parts["password"]
        or not options.get("authSource")
        or not options.get("replicaSet")
        or options.get("authMechanism") != "SCRAM-SHA-256"
    ):
        raise ValueError("MongoDB requires explicit SCRAM and replica-set options")
    if options.get("tls") is not True and (
        options.get("directConnection") is not True
        or len(parts["nodelist"]) != 1
        or parts["nodelist"][0][0] not in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ValueError("MongoDB requires verified TLS outside a direct loopback connection")
    return uri


def open_mongo_persistence(config):
    """Return one client-owned adapter. The worker closes it at exit."""
    if storage_backend(config) != "mongodb":
        raise RuntimeError("MongoDB is not the configured backend")
    try:
        from pymongo import MongoClient
        from pymongo.errors import ConfigurationError, InvalidURI
        from pymongo.uri_parser import parse_uri
    except ImportError:
        raise RuntimeError("MongoDB mode requires the MongoDB runtime dependencies") from None

    from .mongo import MongoPersistence

    try:
        uri = _connection_secret(config["MONGODB_URI_FILE"], parse_uri)
        client = MongoClient(
            uri,
            connect=False,
            connectTimeoutMS=5000,
            serverSelectionTimeoutMS=5000,
            appname="CineVault",
        )
    except (ConfigurationError, InvalidURI, OSError, ValueError, TypeError):
        # Driver and file diagnostics can contain connection material.
        raise RuntimeError("MongoDB connection configuration is invalid") from None
    try:
        return MongoPersistence(client[config["MONGODB_DATABASE"]])
    except (ConfigurationError, InvalidURI, ValueError, TypeError):
        client.close()
        raise RuntimeError("MongoDB database configuration is invalid") from None

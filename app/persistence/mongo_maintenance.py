"""Explicit maintenance provisioning; never called by application startup."""

import copy

from bson import Int64
from pymongo.errors import CollectionInvalid, DuplicateKeyError, OperationFailure

from ..storage_v2 import COLLECTIONS
from .mongo_model import INT64_MAX, collection_indexes, collection_schemas


def auxiliary_schemas():
    identity = copy.deepcopy(collection_schemas()["users"]["properties"]["_id"])
    return {
        "coordination": {
            "bsonType": "object",
            "additionalProperties": False,
            "required": ["_id", "revision"],
            "properties": {
                "_id": {"enum": ["business"]},
                "revision": {"bsonType": "long", "minimum": 0, "maximum": INT64_MAX},
            },
        },
        "entity_ids": {
            "bsonType": "object",
            "additionalProperties": False,
            "required": ["_id", "kind"],
            "properties": {
                "_id": identity,
                "kind": {"enum": [name for name in COLLECTIONS if name != "auth_attempts"]},
            },
        },
    }


def _check_options(options, schema):
    if (
        options.get("validator") != {"$jsonSchema": schema}
        or options.get("validationLevel", "strict") != "strict"
        or options.get("validationAction", "error") != "error"
        or options.get("capped", False)
        or options.get("collation", {"locale": "simple"}).get("locale") != "simple"
    ):
        raise ValueError("Existing MongoDB collection requires an explicit migration")


def _check_indexes(existing, required):
    by_name = {entry["name"]: entry for entry in existing}
    names = {entry["name"] for entry in required}
    for name, entry in by_name.items():
        if (
            name != "_id_"
            and name not in names
            and (entry.get("unique", False) or "expireAfterSeconds" in entry)
        ):
            raise ValueError("Unexpected constraining MongoDB index")
    for spec in required:
        previous = by_name.get(spec["name"])
        if previous and (
            list(previous["key"].items()) != spec["key"]
            or bool(previous.get("unique", False)) != bool(spec.get("unique", False))
            or previous.get("partialFilterExpression") != spec.get("partialFilterExpression")
            or "expireAfterSeconds" in previous
            or "collation" in previous
        ):
            raise ValueError("Existing MongoDB index requires an explicit migration")


def provision(database):
    """Create missing specifications; never collMod, replace data or reset revision.

    This is for a new candidate or an already provisioned database. Importing a
    pre-existing uncoordinated dataset requires its separate migration tool.
    """
    schemas = {**collection_schemas(), **auxiliary_schemas()}
    indexes = collection_indexes()
    existing = {entry["name"]: entry for entry in database.list_collections()}
    for name, schema in schemas.items():
        if name in existing:
            if existing[name]["type"] != "collection":
                raise ValueError("Persistence name already belongs to a non-collection")
            _check_options(existing[name].get("options", {}), schema)
            _check_indexes(list(database[name].list_indexes()), indexes.get(name, []))
    coordinated = "coordination" in existing and "entity_ids" in existing
    if coordinated:
        coordinated = database.coordination.find_one({"_id": "business"}) is not None
    if not coordinated and any(
        database[name].find_one({}, {"_id": 1}) is not None
        for name in collection_schemas()
        if name in existing
    ):
        raise ValueError("Existing MongoDB data requires a coordinated migration")

    for name, schema in schemas.items():
        if name not in existing:
            try:
                database.create_collection(
                    name,
                    validator={"$jsonSchema": schema},
                    validationLevel="strict",
                    validationAction="error",
                )
            except CollectionInvalid:
                _check_options(database[name].options(), schema)
            except OperationFailure as exc:
                if exc.code != 48:
                    raise
                _check_options(database[name].options(), schema)
        for spec in indexes.get(name, []):
            options = {key: value for key, value in spec.items() if key != "key"}
            database[name].create_index(spec["key"], **options)
    try:
        database.coordination.insert_one({"_id": "business", "revision": Int64(0)})
    except DuplicateKeyError:
        pass
    coordinate = database.coordination.find_one({"_id": "business"})
    if (
        coordinate is None
        or set(coordinate) != {"_id", "revision"}
        or type(coordinate["revision"]) is not Int64
        or not 0 <= coordinate["revision"] <= INT64_MAX
    ):
        raise ValueError("Invalid existing MongoDB coordination state")
    return {"collections": len(schemas), "indexes": sum(map(len, indexes.values()))}

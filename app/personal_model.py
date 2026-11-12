"""Pure private-collection values, query parsing and minimal audit construction."""

from dataclasses import dataclass

from .business_storage import KINDS, valid_collection_attributes
from .catalogue_query import query_values
from .validation import ValidationError, is_unicode

AUDIT_KEEP = 2000


def attributes(kind, data):
    name, supports = data.get("list_name", ""), data.get("supports", [])
    if not valid_collection_attributes(kind, name, supports):
        raise ValidationError({"body": "Type, nom de liste ou supports invalides."})
    return {"list_name": name, "supports": list(supports)}


def make_event(identity, actor_id, entity_type, entity_id, action, timestamp):
    return {
        "id": identity,
        "actor_id": actor_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action": entity_type + "." + action,
        "timestamp": timestamp,
    }


def page_values(values, default):
    result = []
    for name, fallback, lower, upper in (("limit", default, 1, 100), ("offset", 0, 0, 10000)):
        try:
            value = int(values.get(name, fallback))
            if not lower <= value <= upper:
                raise ValueError()
        except ValueError:
            raise ValidationError({name: "Valeur invalide."}) from None
        result.append(value)
    return tuple(result)


@dataclass(frozen=True)
class PersonalQuery:
    kind: str = ""
    name_key: str = ""
    limit: int = 24
    offset: int = 0

    def __post_init__(self):
        if (
            type(self.kind) is not str
            or self.kind not in ("", *KINDS)
            or type(self.name_key) is not str
            or not is_unicode(self.name_key)
            or len(self.name_key) > 240
            or self.name_key != self.name_key.casefold()
            or type(self.limit) is not int
            or not 1 <= self.limit <= 100
            or type(self.offset) is not int
            or not 0 <= self.offset <= 10000
        ):
            raise ValueError("Invalid private collection query")


def entries_query(parameters):
    values = query_values(parameters, {"kind", "list_name", "limit", "offset"})
    kind, name = values.get("kind", ""), values.get("list_name", "")
    if kind not in ("", *KINDS) or len(name) > 80:
        raise ValidationError({"query": "Filtre invalide."})
    limit, offset = page_values(values, 24)
    return PersonalQuery(kind, name.casefold(), limit, offset)


def audit_query(parameters):
    # Historical GET audit accepts repeated page parameters and uses the first.
    values = {}
    for name, value in parameters:
        if name not in {"limit", "offset"} or type(value) is not str or not is_unicode(value):
            raise ValidationError({"query": "Paramètres invalides."})
        values.setdefault(name, value)
    return page_values(values, 50)

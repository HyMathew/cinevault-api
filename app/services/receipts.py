"""Pure receipt policy shared with transitional document-based operations."""

import hashlib
import re

from ..validation import ValidationError
from .results import failure

KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")
RECEIPT_TTL = 86400
RECEIPT_CAPACITY = 10000


def key_digest(supplied):
    if supplied is None:
        return None
    if not isinstance(supplied, str) or not KEY.fullmatch(supplied):
        raise ValidationError({"Idempotency-Key": "Clé d’opération invalide."})
    return hashlib.sha256(supplied.encode("ascii")).hexdigest()


def cached_problem(cached, operation, request_hash, timestamp):
    if cached["created_at"] > timestamp:
        return failure(409, "clock_state", "Horloge incohérente avec cette opération.")
    if cached["operation"] != operation or cached["request_hash"] != request_hash:
        return failure(409, "operation_conflict", "Cette clé a déjà servi avec d’autres données.")
    return None

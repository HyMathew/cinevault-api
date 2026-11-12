import uuid

from .enterprise_auth import now
from .personal_model import AUDIT_KEEP, make_event


def record_event(document, actor_id, entity_type, entity_id, action, timestamp=None):
    document["audit_events"].append(
        make_event(
            str(uuid.uuid4()),
            actor_id,
            entity_type,
            entity_id,
            action,
            now() if timestamp is None else timestamp,
        )
    )
    document["audit_events"][:] = document["audit_events"][-AUDIT_KEEP:]
